from app.database.reviews import SQLiteReviewStore


def test_sqlite_store_persists_reviews_saved_links_and_learning(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "persistent.sqlite3"))
    first = SQLiteReviewStore()
    review = {"review_id": "r1", "created_at": "2026-01-01T00:00:00Z", "findings": []}
    first.save_review(review)
    first.save_pull_request("https://github.com/o/r/pull/1")
    first.record_learning_attempt("exercise-1", "security", True)
    first.record_learning_attempt("exercise-1", "security", False)

    second = SQLiteReviewStore()
    assert second.get_review("r1") == review
    assert second.list_reviews()[0] == review
    assert second.list_saved()[0]["url"] == "https://github.com/o/r/pull/1"
    progress = second.learning_progress()
    assert progress["correct_answers"] == 1
    assert progress["attempt_count"] == 2
    assert progress["exercises_completed"] == 1
    assert second.learning_history()["attempts"][0]["correct"] is False


def test_sqlite_user_owned_records_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "owned.sqlite3"))
    store = SQLiteReviewStore()
    store.save_review({"review_id": "one", "created_at": "2026-11-08T10:00:00Z", "findings": []}, owner_id="user-a")
    store.save_review({"review_id": "two", "created_at": "2026-11-08T10:01:00Z", "findings": []}, owner_id="user-b")
    assert store.get_review("one", owner_id="user-b") is None
    assert [item["review_id"] for item in store.list_reviews(owner_id="user-a")] == ["one"]

    url = "https://github.com/owner/repo/pull/42"
    saved_a = store.save_pull_request(url, owner_id="user-a")
    saved_b = store.save_pull_request(url, owner_id="user-b")
    assert saved_a["saved_id"] != saved_b["saved_id"]
    assert store.list_saved(owner_id="user-a") == [saved_a]
    assert store.delete_saved(saved_a["saved_id"], owner_id="user-b") is False

    store.record_learning_attempt("exercise-1", "security", True, owner_id="user-a")
    store.record_learning_attempt("exercise-2", "testing", False, owner_id="user-b")
    assert store.learning_progress(owner_id="user-a")["attempt_count"] == 1
    assert store.learning_progress(owner_id="user-b")["attempt_count"] == 1


def test_sqlite_counts_unique_live_pull_requests_without_demo_or_other_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "profile-counts.sqlite3"))
    store = SQLiteReviewStore()
    pull_request = {"repository_owner": "Example", "repository_name": "Repo", "pull_request_number": 17}
    for review_id in ("live-first", "live-repeat"):
        store.save_review({"review_id": review_id, "created_at": "2026-11-08T00:00:00Z", "is_demo": False, "pull_request": pull_request}, owner_id="profile-user")
    store.save_review({"review_id": "live-next", "created_at": "2026-11-08T00:00:00Z", "is_demo": False, "pull_request": {**pull_request, "pull_request_number": 18}}, owner_id="profile-user")
    store.save_review({"review_id": "demo", "created_at": "2026-11-08T00:00:00Z", "is_demo": True, "pull_request": {**pull_request, "pull_request_number": 19}}, owner_id="profile-user")
    store.save_review({"review_id": "legacy-demo", "created_at": "2026-11-08T00:00:00Z", "analysis_mode": "demo", "pull_request": {**pull_request, "pull_request_number": 21}}, owner_id="profile-user")
    store.save_review({"review_id": "other-user", "created_at": "2026-11-08T00:00:00Z", "is_demo": False, "pull_request": {**pull_request, "pull_request_number": 20}}, owner_id="another-user")
    assert store.live_pull_request_count(owner_id="profile-user") == 2


def test_sqlite_counts_canonical_or_legacy_live_findings_without_demo_or_other_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "profile-findings.sqlite3"))
    store = SQLiteReviewStore()
    store.save_review({
        "review_id": "canonical", "is_demo": False,
        "canonical_issues": [{"issue_id": "i1"}, {"issue_id": "i2"}],
        "findings": [{"id": "f1"}, {"id": "f2"}, {"id": "f3"}],
    }, owner_id="profile-user")
    store.save_review({
        "review_id": "legacy", "is_demo": False,
        "findings": [{"id": "f4"}],
    }, owner_id="profile-user")
    store.save_review({
        "review_id": "demo", "is_demo": True,
        "canonical_issues": [{"issue_id": "demo-issue"}],
    }, owner_id="profile-user")
    store.save_review({
        "review_id": "legacy-demo", "analysis_mode": "demo",
        "canonical_issues": [{"issue_id": "legacy-demo-issue"}],
    }, owner_id="profile-user")
    store.save_review({
        "review_id": "other-user", "is_demo": False,
        "findings": [{"id": "other-finding"}],
    }, owner_id="another-user")
    assert store.live_finding_count(owner_id="profile-user") == 3


def test_sqlite_personal_review_window_and_count_exclude_demos(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "profile-live-window.sqlite3"))
    store = SQLiteReviewStore()
    store.save_review({"review_id": "old-live", "is_demo": False}, owner_id="profile-user")
    store.save_review({"review_id": "demo", "is_demo": True}, owner_id="profile-user")
    store.save_review({"review_id": "legacy-demo", "analysis_mode": "demo"}, owner_id="profile-user")
    store.save_review({"review_id": "legacy-live"}, owner_id="profile-user")
    assert store.live_review_count(owner_id="profile-user") == 2
    assert [review["review_id"] for review in store.list_reviews(
        100, owner_id="profile-user", include_demo=False
    )] == ["legacy-live", "old-live"]


def test_sqlite_shared_review_update_preserves_original_account_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "shared-owner.sqlite3"))
    store = SQLiteReviewStore()
    review = {"review_id": "shared-owner", "created_at": "2026-11-08T10:00:00Z", "findings": []}
    store.save_review(review, owner_id="original-owner")
    store.save_review({**review, "summary": "Updated by workspace editor."}, owner_id="workspace-editor")
    assert store.get_review("shared-owner", owner_id="original-owner")["summary"] == "Updated by workspace editor."
    assert store.get_review("shared-owner", owner_id="workspace-editor") is None


def test_sqlite_review_round_trips_canonical_issues_and_observations(tmp_path, monkeypatch):
    from app.schemas.finding import ReviewFinding
    from app.schemas.issue import (
        CanonicalIssue,
        FindingObservation,
        IssueExplanation,
        SuggestedFix,
    )

    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "issues.sqlite3"))
    finding = ReviewFinding(
        id="finding-1", title="Issue", description="Unsafe input.", file_path="src/a.py",
        line_start=3, line_end=3, severity="high", category="security", confidence=0.9,
        source="static_analysis", verification_status="needs_review", evidence=["input"],
        why_it_matters="Risk", suggested_fix="Validate.", learning_explanation="", limitations=[],
    )
    issue = CanonicalIssue(
        issue_id="issue-1", file_path="src/a.py", line_start=3, line_end=3,
        category="security", severity="high", normalized_description="Unsafe input.",
        observations=[FindingObservation(original_finding=finding, tool_name="bandit", rule_id="B999")],
        explanation=IssueExplanation(
            summary="Untrusted input reaches this operation.", why_it_matters="It may change the operation's behavior.",
            root_cause="Input is used without separating data from syntax.", correction="Use a parameterized API.",
            concept="Parameterization separates values from instructions.", caveats=["Context was limited to the reported line."],
        ),
    )
    issue.suggested_fixes.append(SuggestedFix.model_validate({
        "fix_id": "fix-issue-1", "canonical_issue_id": "issue-1", "file_path": "src/a.py",
        "start_line": 3, "end_line": 3, "language": "python", "base_commit_sha": "a" * 40,
        "file_blob_sha": "b" * 40, "expected_original_code": "value = input()",
        "proposed_code": "value = str(input())", "change_summary": "Make the input conversion explicit.",
        "rationale": "The value is consumed as text.", "limitations": [], "confidence": 0.8,
        "generation_status": "generated", "decision_status": "pending",
        "generated_at": "2026-11-06T00:00:00Z",
    }))
    record = {
        "review_id": "canonical-review", "created_at": "2026-11-06T00:00:00Z",
        "findings": [finding.model_dump(mode="json")],
        "canonical_issues": [issue.model_dump(mode="json")],
        "unique_issue_count": 1, "raw_observation_count": 1,
        "tool_results": [{
            "tool": "fix_generation", "status": "completed", "executed": True,
            "requested": True, "generated_issue_count": 1,
            "outcome_counts": {"generated": 1},
            "per_issue_outcomes": [{"issue_id": "issue-1", "status": "generated", "reason": None}],
        }, {
            "tool": "fix_analysis", "status": "completed", "executed": True,
            "proposal_count": 2, "analyzed_proposal_count": 2, "pair_count": 1,
            "relationship_counts": {"definite_conflict": 1},
            "relationships": [{
                "relationship_id": "fix-rel-example", "fix_ids": ["fix-issue-1", "fix-peer"],
                "canonical_issue_ids": ["issue-1", "issue-2"], "file_path": "src/a.py",
                "line_ranges": [
                    {"canonical_issue_id": "issue-1", "fix_id": "fix-issue-1", "start_line": 3, "end_line": 3},
                    {"canonical_issue_id": "issue-2", "fix_id": "fix-peer", "start_line": 3, "end_line": 3},
                ],
                "relationship_type": "definite_conflict", "explanation": "Overlapping replacements.",
                "detection_method": "same_snapshot_inclusive_line_range_overlap",
            }],
            "conflict_status": "completed", "freshness_status": "partial",
            "freshness": {
                "status": "partial", "proposal_count": 1, "current_count": 0,
                "stale_count": 0, "unverifiable_count": 1,
                "checked_head_sha": "b" * 40, "file_fetch_count": 1,
                "per_proposal": [{"canonical_issue_id": "issue-1", "fix_id": "fix-1", "status": "unverifiable", "reason": "Rate limited."}],
            },
        }],
    }

    first = SQLiteReviewStore()
    first.save_review(record)
    second = SQLiteReviewStore()
    retrieved = second.get_review("canonical-review")

    assert retrieved["canonical_issues"] == record["canonical_issues"]
    assert retrieved["canonical_issues"][0]["observations"][0]["rule_id"] == "B999"
    assert retrieved["canonical_issues"][0]["explanation"]["summary"] == "Untrusted input reaches this operation."
    assert retrieved["unique_issue_count"] == 1
    assert retrieved["raw_observation_count"] == 1
    assert retrieved["canonical_issues"][0]["suggested_fixes"][0]["fix_id"] == "fix-issue-1"
    assert retrieved["canonical_issues"][0]["suggested_fixes"][0]["decision_status"] == "pending"
    assert retrieved["tool_results"] == record["tool_results"]
    assert retrieved["tool_results"][1]["relationships"][0]["relationship_type"] == "definite_conflict"
    assert retrieved["tool_results"][1]["freshness"]["per_proposal"][0]["status"] == "unverifiable"
