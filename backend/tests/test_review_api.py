import asyncio
from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from app.data.sample_reviews import sample_review
from app.database.reviews import get_store
from app.main import app
import hashlib
from app.database import auth as auth_store
from app.routes.auth import current_claims
from app.schemas.finding import ReviewFinding
from app.schemas.github import ChangedFile, PullRequestBundle, PullRequestMetadata


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    from app.routes import review as review_routes

    async def public_repository(_owner, _repo):
        return None

    monkeypatch.setattr(review_routes, "ensure_public_repository", public_repository)
    with TestClient(app) as instance:
        response = instance.post("/api/auth/register", json={"email": "reviewer@example.com", "password": "correct horse battery staple"})
        assert response.status_code == 201
        yield instance


def test_demo_review_history_detail_and_diff_are_persisted(client):
    submitted_url = "https://github.com/example/project/pull/7"
    response = client.post("/api/reviews/analyze", json={"url": submitted_url, "mode": "demo", "include_learning": True})
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_mode"] == "demo" and body["is_demo"] is True
    assert "not describe" in body["warnings"][0].lower()
    assert {item["source"] for item in body["findings"]} == {"demo"}
    assert body["unique_issue_count"] == len(body["canonical_issues"])
    assert body["raw_observation_count"] == len(body["findings"])
    assert sum(len(issue["observations"]) for issue in body["canonical_issues"]) == len(body["findings"])
    fix_result = next(item for item in body["tool_results"] if item["tool"] == "fix_generation")
    assert fix_result["status"] == "not_requested"
    assert fix_result["executed"] is False
    assert body["pull_request"]["pull_request_number"] == 7
    assert client.get("/api/reviews/history").json()[0]["review_id"] == body["review_id"]
    detail = client.get(f"/api/reviews/{body['review_id']}").json()
    assert detail["review_id"] == body["review_id"]
    assert detail["canonical_issues"] == body["canonical_issues"]
    assert client.get(f"/api/reviews/{body['review_id']}/diff").json() == {"review_id": body["review_id"], "files": [], "warnings": body["warnings"]}
    second = client.post("/api/reviews/analyze", json={"url": submitted_url, "mode": "demo", "include_learning": False}).json()
    assert second["review_id"] != body["review_id"]
    assert all(not finding["learning_explanation"] for finding in second["findings"])
    assert len(client.get("/api/reviews/history").json()) == 2


def test_execution_job_status_and_cancellation_are_authenticated_and_persistent(client):
    from app.database import test_execution_jobs

    user = client.get("/api/auth/me").json()
    job = test_execution_jobs.enqueue(
        user["user_id"],
        "api-execution-key-1234",
        {"generated_test": {"test_id": "test-api", "test_code": "assert True"}},
    )
    response = client.get(f"/api/reviews/execution-jobs/{job['execution_id']}")
    assert response.status_code == 200
    assert response.json()["generated_test"]["test_id"] == "test-api"
    cancelled = client.delete(f"/api/reviews/execution-jobs/{job['execution_id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.get(f"/api/reviews/execution-jobs/{job['execution_id']}").json()["status"] == "cancelled"


def test_execution_job_poll_includes_linked_validation_report(client):
    from app.database.test_execution_jobs import enqueue

    owner_id = client.get("/api/auth/me").json()["user_id"]
    record = client.post(
        "/api/reviews/analyze",
        json={"url": "https://github.com/example/linked-report/pull/2", "mode": "demo"},
    ).json()
    record["is_demo"] = False
    issue = record["canonical_issues"][0]
    job = enqueue(owner_id, "linked-report-idempotency", {
        "review_id": record["review_id"], "issue_id": issue["issue_id"],
        "fix_id": "fix-linked-report",
        "generated_test": {"test_id": "test-linked-report"},
    })
    report = {"validation_id": job["execution_id"], "status": "inconclusive"}
    issue["suggested_fixes"] = [{
        "fix_id": "fix-linked-report",
        "validation_reports": [report],
    }]
    get_store().save_review(record, owner_id=owner_id)

    response = client.get(f"/api/reviews/execution-jobs/{job['execution_id']}")
    assert response.status_code == 200
    assert response.json()["validation_report"] == report

def test_authenticated_review_data_is_isolated_between_accounts(client):
    response = client.post("/api/reviews/analyze", json={"url": "https://github.com/example/private/pull/9", "mode": "demo"})
    assert response.status_code == 200
    review_id = response.json()["review_id"]
    saved = client.post("/api/reviews/saved", json={"url": "https://github.com/example/private/pull/9"}).json()

    with TestClient(app) as another_user:
        assert another_user.post("/api/auth/register", json={"email": "other@example.com", "password": "correct horse battery staple"}).status_code == 201
        assert another_user.get("/api/reviews/history").json() == []
        assert another_user.get(f"/api/reviews/{review_id}").status_code == 404
        assert another_user.get("/api/reviews/saved").json() == []
        assert another_user.delete(f"/api/reviews/saved/{saved['saved_id']}").status_code == 404


def test_fix_decision_records_only_owner_intent_and_requires_current_snapshot(client):
    record = client.post("/api/reviews/analyze", json={"url": "https://github.com/example/decisions/pull/11", "mode": "demo"}).json()
    issue = record["canonical_issues"][0]
    fix = {
        "fix_id": "decision-fix", "canonical_issue_id": issue["issue_id"],
        "file_path": "src/example.py", "start_line": 1, "end_line": 1,
        "language": "python", "base_commit_sha": "a" * 40, "file_blob_sha": None,
        "expected_original_code": "value = 0", "proposed_code": "value = 1",
        "change_summary": "Correct the value", "rationale": "The current value is wrong.",
        "limitations": [], "confidence": 0.8, "generation_status": "generated",
        "decision_status": "pending", "generated_at": "2026-11-08T00:00:00Z", "decided_at": None,
        "validation_reports": [],
    }
    issue["suggested_fixes"] = [fix]
    owner_id = client.get("/api/auth/me").json()["user_id"]
    record["tool_results"].append({"tool": "fix_analysis", "freshness": {"per_proposal": [
        {"canonical_issue_id": issue["issue_id"], "fix_id": fix["fix_id"], "status": "current"},
    ]}})
    get_store().save_review(record, owner_id=owner_id)
    path = f"/api/reviews/{record['review_id']}/issues/{issue['issue_id']}/fixes/{fix['fix_id']}/decision"
    accepted = client.post(path, json={"decision": "accepted"})
    assert accepted.status_code == 200
    assert accepted.json()["decision_status"] == "accepted"
    assert accepted.json()["decided_at"]
    assert get_store().get_review(record["review_id"], owner_id=owner_id)["canonical_issues"][0]["suggested_fixes"][0]["decision_status"] == "accepted"

    record["tool_results"][-1]["freshness"]["per_proposal"][0]["status"] = "stale"
    record["canonical_issues"][0]["suggested_fixes"][0]["decision_status"] = "pending"
    get_store().save_review(record, owner_id=owner_id)
    assert client.post(path, json={"decision": "accepted"}).status_code == 409
    assert client.post(path, json={"decision": "rejected"}).status_code == 200


def test_fix_validation_route_persists_attempt_without_claiming_execution(client, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace

    from app.routes import review as review_routes
    from app.schemas.issue import FixValidationReport, FixValidationSnapshot
    from app.services.fix_eligibility import FixEligibilityResult

    record = client.post("/api/reviews/analyze", json={"url": "https://github.com/example/validation/pull/13", "mode": "demo"}).json()
    record["is_demo"] = False
    record["analysis_mode"] = "live"
    issue = record["canonical_issues"][0]
    fix = {
        "fix_id": "validation-fix", "canonical_issue_id": issue["issue_id"],
        "file_path": "src/example.py", "start_line": 1, "end_line": 1,
        "language": "python", "base_commit_sha": "a" * 40, "file_blob_sha": "b" * 40,
        "expected_original_code": "value = 0", "proposed_code": "value = 1",
        "change_summary": "Change value", "rationale": "A safer value.", "limitations": [],
        "confidence": 0.8, "generation_status": "generated", "decision_status": "pending",
        "generated_at": datetime.now(UTC).isoformat(), "validation_reports": [],
    }
    issue["suggested_fixes"] = [fix]
    owner_id = client.get("/api/auth/me").json()["user_id"]
    get_store().save_review(record, owner_id=owner_id)
    snapshot = FixValidationSnapshot(file_path=fix["file_path"], base_commit_sha="a" * 40, checked_head_sha="c" * 40, baseline_blob_sha="b" * 40, proposed_blob_sha="d" * 40)
    now = datetime.now(UTC)

    async def eligible(*_args, **_kwargs):
        return FixEligibilityResult("eligible", "Current source verified.", snapshot, "value = 0\n", "value = 1\n")

    report = FixValidationReport(
        validation_id="validation-attempt-1", fix_id=fix["fix_id"], attempt_number=1,
        status="unavailable", started_at=now, completed_at=now, snapshot=snapshot,
        report_failure={"code": "mocked_tool_unavailable", "status": "unavailable", "reason": "Analyzer unavailable in test."},
    )
    monkeypatch.setattr(review_routes, "evaluate_fix_eligibility", eligible)
    monkeypatch.setattr(review_routes, "validate_static_fix", lambda *_args, **_kwargs: SimpleNamespace(report=report, tool_results={"ruff": {"status": "unavailable"}}))

    response = client.post(f"/api/reviews/{record['review_id']}/issues/{issue['issue_id']}/fixes/{fix['fix_id']}/validate")
    assert response.status_code == 200, response.text
    assert response.json()["report"]["status"] == "unavailable"
    assert response.json()["execution"] == "not_run"
    persisted = get_store().get_review(record["review_id"], owner_id=owner_id)
    reports = persisted["canonical_issues"][0]["suggested_fixes"][0]["validation_reports"]
    assert len(reports) == 1 and reports[0]["validation_id"] == "validation-attempt-1"


def test_review_request_validation_and_unknown_records(client):
    assert client.post("/api/reviews/analyze", json={"mode": "demo"}).status_code == 422
    assert client.post("/api/reviews/analyze", json={"url": "https://example.com/a/b/pull/1"}).status_code == 400
    assert client.get("/api/reviews/not-found").status_code == 404
    assert client.get("/api/reviews/not-found/diff").status_code == 404
    assert client.get("/api/reviews/history?limit=0").status_code == 422


def test_live_review_rejects_private_repository_when_service_token_is_configured(client, monkeypatch):
    from app.github_service import access
    from app.routes import review as review_routes

    async def private_repo(*_args, **_kwargs):
        return {"private": True, "visibility": "private"}

    monkeypatch.setattr(access, "request_json", private_repo)
    monkeypatch.setattr(review_routes, "ensure_public_repository", access.ensure_public_repository)
    response = client.post("/api/reviews/analyze", json={
        "url": "https://github.com/secret-owner/private-repo/pull/1", "mode": "live",
    })
    assert response.status_code == 403
    assert "user-connected" in response.json()["detail"]


def test_saved_pr_create_list_duplicate_and_delete(client):
    url = "https://github.com/owner/repo/pull/12"
    created = client.post("/api/reviews/saved", json={"url": url})
    assert created.status_code == 201
    item = created.json()
    assert item == {"saved_id": item["saved_id"], "url": url, "temporary": False}
    assert client.post("/api/reviews/saved", json={"url": url}).json()["saved_id"] == item["saved_id"]
    assert client.get("/api/reviews/saved").json() == [item]
    assert client.delete(f"/api/reviews/saved/{item['saved_id']}").json() == {"deleted": True}
    assert client.delete(f"/api/reviews/saved/{item['saved_id']}").status_code == 404


def test_live_review_uses_ingestion_and_reports_unavailable_stages(client, monkeypatch):
    from app.services import review_orchestrator as service

    metadata = PullRequestMetadata(repository_owner="owner", repository_name="repo", repository_url="https://github.com/owner/repo", pull_request_number=2, pull_request_url="https://github.com/owner/repo/pull/2", title="Fix", description=None, author="dev", base_branch="main", head_branch="fix", head_sha="a" * 40, state="open", changed_files=1, additions=1, deletions=0)
    bundle = PullRequestBundle(metadata=metadata, files=[ChangedFile(filename="src/a.py", status="modified", additions=1, deletions=0, changes=1, patch="@@ -1 +1 @@\n-old\n+new", size=12)])

    async def fetch(_url):
        return bundle

    async def content(*_args):
        return "value = 1\n"

    def ruff(paths, *, root):
        return [{"file_path": str(root / paths[0]), "line": 1, "rule_id": "F401", "message": "unused import", "evidence": "import os"}]

    def bandit(_paths, *, root):
        return []

    monkeypatch.setattr(service, "fetch_pull_request", fetch)
    monkeypatch.setattr(service, "get_file_content", content)
    monkeypatch.setattr(service, "run_ruff", ruff)
    monkeypatch.setattr(service, "run_bandit", bandit)
    monkeypatch.setattr(service, "is_configured", lambda: False)

    async def explanation_must_not_run(*_args, **_kwargs):
        raise AssertionError("unconfigured LLM must not receive an explanation request")

    monkeypatch.setattr(service, "generate_issue_explanations", explanation_must_not_run)

    async def fix_generation_must_not_run(*_args, **_kwargs):
        raise AssertionError("default opt-out must not invoke fix generation")

    async def fix_freshness_must_not_run(*_args, **_kwargs):
        raise AssertionError("default opt-out must not invoke freshness verification")

    monkeypatch.setattr(service, "generate_suggested_fixes", fix_generation_must_not_run)
    monkeypatch.setattr(service, "verify_fix_freshness", fix_freshness_must_not_run)
    response = client.post("/api/reviews/analyze", json={"url": metadata.pull_request_url, "mode": "live"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_demo"] is False and body["analysis_mode"] == "live"
    assert body["status"] == "partial"  # AI and isolated test execution are not configured.
    assert body["findings"][0]["file_path"] == "src/a.py"  # Never expose the temporary workspace path.
    assert body["findings"][0]["verification_status"] == "needs_review"
    assert body["unique_issue_count"] == len(body["canonical_issues"])
    assert body["raw_observation_count"] == 1
    assert body["canonical_issues"][0]["observations"][0]["tool_name"] == "ruff"
    assert body["canonical_issues"][0]["observations"][0]["rule_id"] == "F401"
    assert any(tool["tool"] == "tests" and tool["executed"] is False for tool in body["tool_results"])
    assert body["canonical_issues"][0]["explanation"] is None
    explanation_status = next(tool for tool in body["tool_results"] if tool["tool"] == "issue_explanations")
    assert explanation_status["status"] == "unavailable"
    assert explanation_status["explained_issue_count"] == 0
    fix_status = next(tool for tool in body["tool_results"] if tool["tool"] == "fix_generation")
    assert fix_status["status"] == "not_requested"
    assert fix_status["executed"] is False
    analysis_status = next(tool for tool in body["tool_results"] if tool["tool"] == "fix_analysis")
    assert analysis_status["status"] == "not_requested"
    assert analysis_status["relationships"] == []


def test_live_review_skips_source_fetch_without_full_snapshot_identity(client, monkeypatch):
    from app.services import review_orchestrator as service

    metadata = PullRequestMetadata(
        repository_owner="owner", repository_name="repo", repository_url="https://github.com/owner/repo",
        pull_request_number=3, pull_request_url="https://github.com/owner/repo/pull/3", title="Fix",
        description=None, author="dev", base_branch="main", head_branch="fix", head_sha="not-a-full-sha",
        state="open", changed_files=1, additions=1, deletions=0,
    )
    bundle = PullRequestBundle(metadata=metadata, files=[ChangedFile(
        filename="src/a.py", status="modified", additions=1, deletions=0, changes=1,
        patch="@@ -1 +1 @@\n-old\n+new", size=12,
    )])

    async def fetch(_url):
        return bundle

    async def unexpected_content(*_args):
        raise AssertionError("source must not be fetched without a full immutable snapshot SHA")

    monkeypatch.setattr(service, "fetch_pull_request", fetch)
    monkeypatch.setattr(service, "get_file_content", unexpected_content)
    monkeypatch.setattr(service, "is_configured", lambda: False)
    response = client.post("/api/reviews/analyze", json={"url": metadata.pull_request_url, "mode": "live"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert any("snapshot identity" in warning for warning in body["warnings"])
    assert not any(item["tool"] in {"ruff", "bandit"} and item["executed"] for item in body["tool_results"])


def test_live_review_reconciles_static_and_ai_and_persists_observations(client, monkeypatch):
    from datetime import datetime

    from app.ai_review.fix_generation import FixGenerationResult, IssueFixOutcome
    from app.ai_review.issue_explanations import ExplanationGenerationResult
    from app.schemas.issue import IssueExplanation, SuggestedFix
    from app.services import review_orchestrator as service

    head_sha = "8" * 40
    metadata = PullRequestMetadata(repository_owner="owner", repository_name="repo", repository_url="https://github.com/owner/repo", pull_request_number=8, pull_request_url="https://github.com/owner/repo/pull/8", title="Fix query", description=None, author="dev", base_branch="main", head_branch="fix", head_sha=head_sha, state="open", changed_files=1, additions=1, deletions=0)
    bundle = PullRequestBundle(metadata=metadata, files=[ChangedFile(filename="src/query.py", status="modified", additions=1, deletions=0, changes=1, patch="@@ -1 +1 @@\n-old\n+new", size=12)])

    async def fetch(_url):
        return bundle

    async def content(*_args):
        return "import os\n"

    def ruff(paths, *, root):
        return [{"file_path": str(root / paths[0]), "line": 1, "rule_id": "SQL001", "message": "User input is interpolated into a SQL query.", "evidence": "import os"}]

    def bandit(_paths, *, root):
        return []

    async def ai(_metadata, _files, _static):
        return [ReviewFinding(
            id="ai-query-1", title="Unsafe SQL interpolation",
            description="User input is interpolated into a SQL query.",
            file_path="src/query.py", line_start=1, line_end=1,
            severity="high", category="code_quality", confidence=0.94,
            source="ai_candidate", verification_status="needs_review",
            evidence=["import os"], why_it_matters="The query can be manipulated.",
            suggested_fix="Use a parameterized query.", learning_explanation="Separate code from data.", limitations=[],
        )]

    issue_ids_seen_by_explainer = []
    observations_seen_by_explainer = []
    events = []

    async def explain(issues, **_kwargs):
        events.append("explanation")
        issue_ids_seen_by_explainer.extend(issue.issue_id for issue in issues)
        observations_seen_by_explainer.extend(
            [[observation.model_dump(mode="json") for observation in issue.observations] for issue in issues]
        )
        return ExplanationGenerationResult(explanations={
            issue.issue_id: IssueExplanation(
                summary="Untrusted input reaches command execution.",
                why_it_matters="The input may alter the command being run.",
                root_cause="The shell interprets untrusted text as syntax.",
                correction="Pass validated values as separate command arguments.",
                concept="Argument vectors keep data separate from command syntax.",
                caveats=["Only the reported line and evidence were available."],
            )
            for issue in issues
        })

    async def generate_fixes(issues, *, owner, repo, head_sha: str, diff_files):
        events.append("fix_generation")
        assert owner == "owner" and repo == "repo"
        assert head_sha == metadata.head_sha
        assert len(issues) == 1
        assert len(issues[0].observations) == 2
        assert issues[0].explanation is not None
        fix = SuggestedFix(
            fix_id="fix-query-1", canonical_issue_id=issues[0].issue_id,
            file_path=issues[0].file_path, start_line=1, end_line=1,
            language="python", base_commit_sha=head_sha,
            expected_original_code="import os", proposed_code="import sqlite3",
            change_summary="Use the database module needed for parameterized queries.",
            rationale="The replacement keeps data separate from SQL syntax.",
            confidence=0.82, generation_status="generated",
            generated_at=datetime.now(UTC),
        )
        return FixGenerationResult(
            outcomes=[IssueFixOutcome(issues[0].issue_id, "generated", fix=fix)],
            source_fetch_count=1, llm_call_count=1,
        )

    async def verify_freshness(issues, *, owner, repo, pull_request_number):
        assert len(issues) == 1 and owner == "owner" and repo == "repo"
        return {
            "status": "completed", "proposal_count": 1, "current_count": 1,
            "stale_count": 0, "unverifiable_count": 0,
            "outcome_counts": {"current": 1}, "checked_head_sha": head_sha,
            "file_fetch_count": 1,
            "per_proposal": [{
                "canonical_issue_id": issues[0].issue_id, "fix_id": "fix-query-1",
                "status": "current", "reason": "The current file matches its recorded snapshot.",
            }],
        }

    monkeypatch.setattr(service, "fetch_pull_request", fetch)
    monkeypatch.setattr(service, "get_file_content", content)
    monkeypatch.setattr(service, "run_ruff", ruff)
    monkeypatch.setattr(service, "run_bandit", bandit)
    monkeypatch.setattr(service, "is_configured", lambda: True)
    monkeypatch.setattr(service, "generate_candidates", ai)
    monkeypatch.setattr(service, "generate_issue_explanations", explain)
    monkeypatch.setattr(service, "generate_suggested_fixes", generate_fixes)
    monkeypatch.setattr(service, "verify_fix_freshness", verify_freshness)

    response = client.post("/api/reviews/analyze", json={"url": metadata.pull_request_url, "mode": "live", "include_suggested_fixes": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["findings"]) == 2  # Legacy field remains the raw finding list.
    assert body["unique_issue_count"] == 1
    assert body["raw_observation_count"] == 2
    assert body["observation_counts_by_source"] == {"static_analysis": 1, "ai_candidate": 1}
    issue = body["canonical_issues"][0]
    assert len(issue["observations"]) == 2
    assert {item["original_finding"]["source"] for item in issue["observations"]} == {"static_analysis", "ai_candidate"}
    assert [item["issue_id"] for item in body["canonical_issues"]] == issue_ids_seen_by_explainer
    assert [item["observations"] for item in body["canonical_issues"]] == observations_seen_by_explainer
    assert issue["explanation"]["summary"] == "Untrusted input reaches command execution."
    assert [item["tool"] for item in body["tool_results"][-3:]] == ["issue_explanations", "fix_generation", "fix_analysis"]
    explanation_tool = next(item for item in body["tool_results"] if item["tool"] == "issue_explanations")
    assert explanation_tool["status"] == "completed"
    assert explanation_tool["explained_issue_count"] == 1
    fix_tool = next(item for item in body["tool_results"] if item["tool"] == "fix_generation")
    assert fix_tool["status"] == "completed"
    assert fix_tool["generated_issue_count"] == 1
    assert fix_tool["outcome_counts"] == {"generated": 1}
    assert fix_tool["per_issue_outcomes"] == [{"issue_id": issue["issue_id"], "status": "generated", "reason": None}]
    analysis_tool = next(item for item in body["tool_results"] if item["tool"] == "fix_analysis")
    assert analysis_tool["status"] == "completed"
    assert analysis_tool["proposal_count"] == 1
    assert analysis_tool["relationships"] == []
    assert analysis_tool["freshness_status"] == "completed"
    assert analysis_tool["freshness"]["per_proposal"][0]["status"] == "current"
    assert events == ["explanation", "fix_generation"]
    assert issue["suggested_fixes"][0]["decision_status"] == "pending"
    assert issue["suggested_fixes"][0]["base_commit_sha"] == head_sha

    review_id = body["review_id"]
    stored = get_store().get_review(review_id)
    assert stored["canonical_issues"] == body["canonical_issues"]
    assert stored["findings"] == body["findings"]
    assert stored["tool_results"] == body["tool_results"]
    assert client.get("/api/reviews/history").json()[0]["unique_issue_count"] == 1
    assert client.get(f"/api/reviews/{review_id}/diff").status_code == 200
    retrieved = client.get(f"/api/reviews/{review_id}")
    assert retrieved.status_code == 200
    assert retrieved.json()["canonical_issues"][0]["explanation"] == issue["explanation"]
    assert retrieved.json()["canonical_issues"][0]["suggested_fixes"] == issue["suggested_fixes"]


def test_unowned_legacy_review_is_preserved_but_not_exposed_to_new_account(client):
    legacy = sample_review("owner", "legacy", 3).model_dump(mode="json")
    for field in (
        "canonical_issues", "unique_issue_count", "raw_observation_count",
        "observation_counts_by_source", "issue_counts_by_severity", "issue_counts_by_category",
    ):
        legacy.pop(field, None)
    get_store().save_review(legacy)

    detail = client.get(f"/api/reviews/{legacy['review_id']}")
    history = client.get("/api/reviews/history")

    assert detail.status_code == 404
    assert all(item["review_id"] != legacy["review_id"] for item in history.json())
    # Data with unknown former ownership is kept intact but never assigned to a new user.
    assert "canonical_issues" not in get_store().get_review(legacy["review_id"])


def test_explanation_service_failure_does_not_fail_or_drop_live_review(client, monkeypatch):
    from app.services import review_orchestrator as service

    metadata = PullRequestMetadata(
        repository_owner="owner", repository_name="repo", repository_url="https://github.com/owner/repo",
        pull_request_number=19, pull_request_url="https://github.com/owner/repo/pull/19", title="Update command",
        description=None, author="dev", base_branch="main", head_branch="fix", head_sha="1" * 40,
        state="open", changed_files=1, additions=1, deletions=0,
    )
    bundle = PullRequestBundle(metadata=metadata, files=[ChangedFile(
        filename="src/runner.py", status="modified", additions=1, deletions=0, changes=1,
        patch="@@ -3 +3 @@\n-old\n+new", size=12,
    )])

    async def fetch(_url):
        return bundle

    async def content(*_args):
        return "command = input()\n"

    def ruff(paths, *, root):
        return [{"file_path": str(root / paths[0]), "line": 1, "rule_id": "S602", "message": "Unsafe shell use", "evidence": "shell=True"}]

    def bandit(_paths, *, root):
        return []

    async def ai(*_args):
        return []

    async def explain(*_args, **_kwargs):
        raise RuntimeError("Unexpected internal failure")

    monkeypatch.setattr(service, "fetch_pull_request", fetch)
    monkeypatch.setattr(service, "get_file_content", content)
    monkeypatch.setattr(service, "run_ruff", ruff)
    monkeypatch.setattr(service, "run_bandit", bandit)
    monkeypatch.setattr(service, "is_configured", lambda: True)
    monkeypatch.setattr(service, "generate_candidates", ai)
    monkeypatch.setattr(service, "generate_issue_explanations", explain)

    response = client.post("/api/reviews/analyze", json={"url": metadata.pull_request_url, "mode": "live"})
    assert response.status_code == 200
    body = response.json()
    assert len(body["findings"]) == 1
    assert len(body["canonical_issues"]) == 1
    assert body["canonical_issues"][0]["explanation"] is None
    explanation_tool = next(item for item in body["tool_results"] if item["tool"] == "issue_explanations")
    assert explanation_tool["status"] == "unavailable"


def test_fix_generation_partial_success_preserves_review_and_reports_each_issue(client, monkeypatch):
    from datetime import datetime

    from app.ai_review.fix_generation import FixGenerationResult, IssueFixOutcome
    from app.ai_review.issue_explanations import ExplanationGenerationResult
    from app.schemas.issue import IssueExplanation, SuggestedFix
    from app.services import review_orchestrator as service

    head_sha = "9" * 40
    metadata = PullRequestMetadata(
        repository_owner="owner", repository_name="repo", repository_url="https://github.com/owner/repo",
        pull_request_number=23, pull_request_url="https://github.com/owner/repo/pull/23", title="Three issues",
        description=None, author="dev", base_branch="main", head_branch="fix", head_sha=head_sha,
        state="open", changed_files=1, additions=3, deletions=0,
    )
    bundle = PullRequestBundle(metadata=metadata, files=[ChangedFile(
        filename="src/a.py", status="modified", additions=3, deletions=0, changes=3,
        patch="@@ -1,3 +1,3 @@\n-a = 0\n+b = 0\n+c = 0", size=24,
    )])

    async def fetch(_url):
        return bundle

    async def content(*_args):
        return "a = 1\nb = 2\nc = 3\n"

    def ruff(paths, *, root):
        return [
            {"file_path": str(root / paths[0]), "line": line, "rule_id": rule, "message": message, "evidence": f"line {line}"}
            for line, rule, message in ((1, "R1", "First finding"), (1, "R2", "Second finding"), (3, "R3", "Third finding"))
        ]

    def bandit(_paths, *, root):
        return []

    async def ai(*_args):
        return []

    async def explain(issues, **_kwargs):
        return ExplanationGenerationResult(explanations={
            issue.issue_id: IssueExplanation(
                summary="A source issue.", why_it_matters="It may affect behavior.",
                root_cause="The source has an unsafe pattern.", correction="Use a safer form.",
                concept="Keep behavior and input boundaries explicit.",
            ) for issue in issues
        })

    async def generate(issues, *, owner, repo, head_sha: str, diff_files):
        assert len(issues) == 3
        assert head_sha == metadata.head_sha
        fix = SuggestedFix(
            fix_id="fix-1", canonical_issue_id=issues[0].issue_id, file_path="src/a.py",
            start_line=1, end_line=1, language="python", base_commit_sha=head_sha,
            expected_original_code="a = 1", proposed_code="a = 2", change_summary="Update the first value.",
            rationale="This addresses the first reported issue.", confidence=0.7,
            generation_status="generated", generated_at=datetime.now(UTC),
        )
        conflicting_fix = SuggestedFix(
            fix_id="fix-2", canonical_issue_id=issues[1].issue_id, file_path="src/a.py",
            start_line=1, end_line=1, language="python", base_commit_sha=head_sha,
            expected_original_code="a = 1", proposed_code="a = 3", change_summary="Choose another first value.",
            rationale="This is an alternate replacement for the same source line.", confidence=0.6,
            generation_status="generated", generated_at=datetime.now(UTC),
        )
        return FixGenerationResult(outcomes=[
            IssueFixOutcome(issues[0].issue_id, "generated", fix=fix),
            IssueFixOutcome(issues[1].issue_id, "generated", fix=conflicting_fix),
            IssueFixOutcome(issues[2].issue_id, "failed", "The provider did not return a valid response."),
        ], source_fetch_count=1, llm_call_count=2)

    async def verify_freshness(issues, *, owner, repo, pull_request_number):
        fixes = [fix for issue in issues for fix in issue.suggested_fixes]
        assert len(fixes) == 2
        return {
            "status": "completed", "proposal_count": 2, "current_count": 1,
            "stale_count": 1, "unverifiable_count": 0,
            "outcome_counts": {"current": 1, "stale": 1},
            "checked_head_sha": head_sha, "file_fetch_count": 1,
            "per_proposal": [
                {"canonical_issue_id": fix.canonical_issue_id, "fix_id": fix.fix_id,
                 "status": "current" if index == 0 else "stale", "reason": "Snapshot result."}
                for index, fix in enumerate(fixes)
            ],
        }

    monkeypatch.setattr(service, "fetch_pull_request", fetch)
    monkeypatch.setattr(service, "get_file_content", content)
    monkeypatch.setattr(service, "run_ruff", ruff)
    monkeypatch.setattr(service, "run_bandit", bandit)
    monkeypatch.setattr(service, "is_configured", lambda: True)
    monkeypatch.setattr(service, "generate_candidates", ai)
    monkeypatch.setattr(service, "generate_issue_explanations", explain)
    monkeypatch.setattr(service, "generate_suggested_fixes", generate)
    monkeypatch.setattr(service, "verify_fix_freshness", verify_freshness)
    monkeypatch.setattr(service, "run_tests", lambda: {"status": "completed", "executed": True, "message": "Tests completed."})

    response = client.post("/api/reviews/analyze", json={
        "url": metadata.pull_request_url, "mode": "live", "include_suggested_fixes": True,
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"  # Optional fix failures do not downgrade the review.
    assert len(body["findings"]) == 3
    assert len(body["canonical_issues"]) == 3
    assert all(issue["explanation"] is not None for issue in body["canonical_issues"])
    assert [len(issue["suggested_fixes"]) for issue in body["canonical_issues"]] == [1, 1, 0]
    assert [issue["suggested_fixes"][0]["generation_status"] for issue in body["canonical_issues"][:2]] == ["conflicted", "conflicted"]
    tool = next(item for item in body["tool_results"] if item["tool"] == "fix_generation")
    assert tool["status"] == "partial"
    assert tool["generated_issue_count"] == 2
    assert tool["outcome_counts"] == {"generated": 2, "failed": 1}
    assert [item["status"] for item in tool["per_issue_outcomes"]] == ["generated", "generated", "failed"]
    analysis = next(item for item in body["tool_results"] if item["tool"] == "fix_analysis")
    assert analysis["status"] == "completed"
    assert analysis["relationship_counts"] == {"definite_conflict": 1}
    assert analysis["relationships"][0]["relationship_type"] == "definite_conflict"
    assert len(analysis["relationships"][0]["fix_ids"]) == 2
    assert analysis["conflict_status"] == "completed"
    assert analysis["freshness_status"] == "completed"
    assert [item["status"] for item in analysis["freshness"]["per_proposal"]] == ["current", "stale"]
    assert len(analysis["relationships"]) == 1  # Freshness does not remove recorded conflicts.
    stored = get_store().get_review(body["review_id"])
    assert stored["canonical_issues"] == body["canonical_issues"]
    assert stored["tool_results"] == body["tool_results"]


def test_demo_explicit_fix_request_is_not_run_for_curated_reviews(client, monkeypatch):
    from app.services import review_orchestrator as service

    async def fix_generation_must_not_run(*_args, **_kwargs):
        raise AssertionError("demo reviews must not invoke fix generation")

    monkeypatch.setattr(service, "generate_suggested_fixes", fix_generation_must_not_run)
    response = client.post("/api/reviews/analyze", json={
        "url": "https://github.com/example/project/pull/24", "mode": "demo",
        "include_suggested_fixes": True,
    })

    assert response.status_code == 200
    result = next(item for item in response.json()["tool_results"] if item["tool"] == "fix_generation")
    assert result["status"] == "unavailable"
    assert result["requested"] is True
    assert result["executed"] is False


def test_fix_generation_cancellation_propagates(monkeypatch):
    from app.data.sample_reviews import sample_review
    from app.schemas.issue import FindingObservation
    from app.schemas.review import AnalyzeRequest
    from app.services import review_orchestrator as service

    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "generate_suggested_fixes", cancel)
    review = sample_review("owner", "repo", 25)
    review.canonical_issues = service.reconcile_observations([
        FindingObservation(original_finding=review.findings[0], tool_name="demo_dataset")
    ]).issues
    request = AnalyzeRequest(url=review.pull_request.pull_request_url, mode="live", include_suggested_fixes=True)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service._run_fix_generation(
            request, "owner", "repo", "a" * 40, review.canonical_issues, [],
        ))


def test_missing_head_snapshot_is_reported_without_source_or_llm_calls(monkeypatch):
    from app.ai_review import fix_generation
    from app.data.sample_reviews import sample_review
    from app.schemas.issue import FindingObservation
    from app.schemas.review import AnalyzeRequest
    from app.services import review_orchestrator as service

    review = sample_review("owner", "repo", 26)
    issues = service.reconcile_observations([
        FindingObservation(original_finding=review.findings[0], tool_name="demo_dataset")
    ]).issues
    request = AnalyzeRequest(url=review.pull_request.pull_request_url, mode="live", include_suggested_fixes=True)
    monkeypatch.setattr(fix_generation, "is_configured", lambda: True)

    async def unexpected_source_fetch(*_args, **_kwargs):
        raise AssertionError("a missing snapshot must stop before source retrieval")

    monkeypatch.setattr(fix_generation, "get_file_content", unexpected_source_fetch)
    result = asyncio.run(service._run_fix_generation(request, "owner", "repo", None, issues, []))

    assert result["executed"] is True
    assert result["outcome_counts"] == {"insufficient_context": 1}
    assert result["per_issue_outcomes"][0]["status"] == "insufficient_context"
    assert result["source_fetch_count"] == 0
    assert result["llm_call_count"] == 0


def test_fix_generation_service_exception_is_sanitized_and_bounded(monkeypatch):
    from app.data.sample_reviews import sample_review
    from app.schemas.issue import FindingObservation
    from app.schemas.review import AnalyzeRequest
    from app.services import review_orchestrator as service

    async def fail(*_args, **_kwargs):
        raise RuntimeError("private provider or token detail")

    monkeypatch.setattr(service, "generate_suggested_fixes", fail)
    review = sample_review("owner", "repo", 27)
    issues = service.reconcile_observations([
        FindingObservation(original_finding=review.findings[0], tool_name="demo_dataset")
    ]).issues
    request = AnalyzeRequest(url=review.pull_request.pull_request_url, mode="live", include_suggested_fixes=True)

    result = asyncio.run(service._run_fix_generation(request, "owner", "repo", "a" * 40, issues, []))

    assert result["status"] == "unavailable"
    assert result["outcome_counts"] == {"failed": 1}
    assert "private provider" not in result["per_issue_outcomes"][0]["reason"]


def test_skipped_limit_is_normalized_in_public_fix_outcomes(monkeypatch):
    from app.ai_review.fix_generation import FixGenerationResult, IssueFixOutcome
    from app.data.sample_reviews import sample_review
    from app.schemas.issue import FindingObservation
    from app.schemas.review import AnalyzeRequest
    from app.services import review_orchestrator as service

    review = sample_review("owner", "repo", 28)
    issues = service.reconcile_observations([
        FindingObservation(original_finding=review.findings[0], tool_name="demo_dataset")
    ]).issues

    async def skip(*_args, **_kwargs):
        return FixGenerationResult(outcomes=[
            IssueFixOutcome(issues[0].issue_id, "skipped_limit", "Per-request fix generation limit reached.")
        ])

    monkeypatch.setattr(service, "generate_suggested_fixes", skip)
    request = AnalyzeRequest(url=review.pull_request.pull_request_url, mode="live", include_suggested_fixes=True)
    result = asyncio.run(service._run_fix_generation(request, "owner", "repo", "a" * 40, issues, []))

    assert result["outcome_counts"] == {"skipped": 1}
    assert result["per_issue_outcomes"][0]["status"] == "skipped"
