import os
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.ai_review.generated_test_context import build_test_generation_context
from app.github_service.pull_requests import git_blob_sha
from app.schemas.generated_test import (
    GeneratedTestCase,
    GeneratedTestQualityAssessment,
    GeneratedTestSourceReference,
)
from app.schemas.issue import FixValidationSnapshot, SuggestedFix
from app.services.fix_eligibility import FixEligibilityResult
from app.test_execution.contracts import ExecutionResourceLimits, TestExecutionResult
from app.test_execution.runtime import (
    DockerGVisorRuntime,
    RuntimeCompletion,
    RuntimeHandle,
    RuntimeMetadata,
    RuntimeUnavailable,
)
from app.test_execution.settings import load_execution_settings
from app.test_execution.worker import (
    DEFAULT_LIMITS,
    ExecutionInputRejected,
    build_execution_artifact,
    execute_generated_test,
)

BASELINE = "def compute(value):\n    return value\n"
PROPOSED = "def compute(value):\n    return value + 1\n"
SHA = "a" * 40
IMAGE = "registry.example/pr-mentor-test@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def isolated_worker_environment(monkeypatch):
    monkeypatch.setenv("PR_MENTOR_EXECUTION_WORKER", "1")
    for name in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "MONGODB_URI",
        "MONGODB_DATABASE",
        "DATABASE_URL",
        "SQLALCHEMY_DATABASE_URI",
        "PR_MENTOR_STORAGE",
        "PR_MENTOR_DATABASE_PATH",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def records():
    fix = SuggestedFix(
        fix_id="fix-1",
        canonical_issue_id="issue-1",
        file_path="src/math_ops.py",
        start_line=2,
        end_line=2,
        language="python",
        base_commit_sha=SHA,
        file_blob_sha=git_blob_sha(BASELINE.encode()),
        expected_original_code="    return value",
        proposed_code="    return value + 1",
        change_summary="Correct the result.",
        rationale="The returned value is incorrect.",
        confidence=0.8,
        generation_status="generated",
        generated_at=datetime.now(UTC),
    )
    snapshot = FixValidationSnapshot(
        file_path=fix.file_path,
        base_commit_sha=SHA,
        checked_head_sha="b" * 40,
        baseline_blob_sha=git_blob_sha(BASELINE.encode()),
        proposed_blob_sha=git_blob_sha(PROPOSED.encode()),
    )
    eligibility = FixEligibilityResult(
        "eligible", "verified", snapshot, BASELINE, PROPOSED
    )
    context = build_test_generation_context(
        fix, _issue(fix), owner="acme", repo="calculator", eligibility=eligibility
    )
    quality = GeneratedTestQualityAssessment(
        relevance="satisfied",
        traceability="satisfied",
        specificity="satisfied",
        assertion_quality="satisfied",
        independence="satisfied",
        completeness="satisfied",
        determinism="satisfied",
        safety="satisfied",
        outcome="ready_for_review",
    )
    test = GeneratedTestCase(
        test_id="test-1",
        suggested_fix_id=fix.fix_id,
        canonical_issue_id=fix.canonical_issue_id,
        repository_id="acme/calculator",
        source_snapshot_id=context.source_snapshot_id,
        base_commit_sha=SHA,
        checked_head_sha="b" * 40,
        baseline_blob_sha=git_blob_sha(BASELINE.encode()),
        proposed_blob_sha=git_blob_sha(PROPOSED.encode()),
        source_file_path=fix.file_path,
        test_name="test_compute_increments_value",
        test_code="from src.math_ops import compute\n\ndef test_compute_increments_value():\n    assert compute(2) == 3\n",
        test_category="fix_verification",
        test_description="Checks the fix.",
        rationale="The proposal increments the value.",
        expected_behavior="Returns three.",
        source_references=[
            GeneratedTestSourceReference(
                source_version="proposed",
                file_path=fix.file_path,
                line_start=1,
                line_end=2,
                description="The changed function.",
            )
        ],
        generation_model="model",
        generation_provider="provider",
        generation_configuration_sha256="c" * 64,
        generation_timestamp=datetime.now(UTC),
        quality_assessment=quality,
    )
    return fix, test, eligibility


def _issue(fix):
    from app.schemas.issue import CanonicalIssue

    return CanonicalIssue(
        issue_id=fix.canonical_issue_id,
        file_path=fix.file_path,
        line_start=2,
        line_end=2,
        category="bug",
        severity="medium",
        normalized_description="Increment the computed value.",
        suggested_fixes=[fix],
    )


class FakeRuntime:
    def __init__(
        self,
        completion=None,
        *,
        cleanup=True,
        preflight_error=None,
        completion_by_source=None,
        metadata_sequence=None,
    ):
        if completion_by_source is not None:
            self.completion_by_source = completion_by_source
        elif completion is None:
            self.completion_by_source = {
                BASELINE: RuntimeCompletion(1, "1 failed", "assertion mismatch"),
                PROPOSED: RuntimeCompletion(0, "1 passed", ""),
            }
        else:
            self.completion_by_source = {BASELINE: completion, PROPOSED: completion}
        self.cleanup_result = cleanup
        self.preflight_error = preflight_error
        self.calls = []
        self.metadata = RuntimeMetadata("fake-gvisor", "1", IMAGE, "3.12.10", "8.4.2")
        self.metadata_sequence = metadata_sequence
        self.preflight_count = 0
        self.current_source = ""
        self.container_count = 0

    def preflight(self):
        if self.preflight_error:
            raise self.preflight_error
        if self.metadata_sequence is not None:
            metadata = self.metadata_sequence[self.preflight_count]
            self.preflight_count += 1
            return metadata
        return self.metadata

    def create(self, input_directory, **kwargs):
        self.calls.append(("create", input_directory, kwargs))
        self.current_source = (input_directory / kwargs["source_relative_path"]).read_text()
        assert self.current_source in {BASELINE, PROPOSED}
        assert (input_directory / kwargs["test_relative_path"]).is_file()
        assert (
            not (input_directory / kwargs["source_relative_path"]).stat().st_mode
            & 0o222
        )
        self.container_count += 1
        return RuntimeHandle(
            f"container-{self.container_count}",
            self.metadata,
            input_directory,
            0,
            kwargs["limits"].output_bytes,
        )

    def wait(self, handle, **kwargs):
        self.calls.append(("wait", kwargs))
        return self.completion_by_source[self.current_source]

    def collect(self, handle):
        return self.completion

    def cancel(self, handle):
        self.calls.append(("cancel", handle))

    def cleanup(self, handle):
        self.calls.append(("cleanup", handle))
        return self.cleanup_result


def test_input_artifact_is_identity_bound_and_contains_only_verified_proposed_source():
    fix, test, eligibility = records()
    artifact = build_execution_artifact(
        fix, test, eligibility, owner="acme", repo="calculator"
    )
    assert artifact.proposed_source == PROPOSED
    assert artifact.proposed_blob_sha == git_blob_sha(PROPOSED.encode())
    assert artifact.repository_id == "acme/calculator"


@pytest.mark.parametrize(
    "change",
    [
        {"source_snapshot_id": "snapshot-" + "d" * 32},
        {"proposed_blob_sha": "e" * 40},
        {"suggested_fix_id": "different-fix"},
        {"canonical_issue_id": "different-issue"},
        {"source_file_path": "../escape.py"},
    ],
)
def test_snapshot_identity_hash_and_unsafe_path_mismatches_are_rejected(change):
    fix, test, eligibility = records()
    altered = test.model_copy(update=change)
    with pytest.raises(ExecutionInputRejected):
        build_execution_artifact(
            fix, altered, eligibility, owner="acme", repo="calculator"
        )


def test_proposed_source_blob_mismatch_is_rejected():
    fix, test, eligibility = records()
    wrong = FixEligibilityResult(
        "eligible",
        "verified",
        eligibility.snapshot,
        BASELINE,
        "def compute(value):\n    return 9\n",
    )
    with pytest.raises(ExecutionInputRejected, match="(?i)proposed source"):
        build_execution_artifact(fix, test, wrong, owner="acme", repo="calculator")


def test_worker_pass_is_execution_only_and_cleans_temporary_input():
    fix, test, eligibility = records()
    runtime = FakeRuntime()
    result = execute_generated_test(
        fix,
        test,
        eligibility,
        owner="acme",
        repo="calculator",
        runtime=runtime,
        execution_id="execution-1",
    )
    assert result.status == "passed"
    assert result.exit_code == 0
    assert result.timeout_enforced and result.cleanup_confirmed
    assert result.execution_id == "execution-1"
    assert [call[0] for call in runtime.calls] == [
        "create", "wait", "cleanup", "create", "wait", "cleanup",
    ]
    assert not runtime.calls[0][1].exists()
    assert result.regression_status == "detected"
    assert result.baseline_execution is not None
    assert result.baseline_execution.status == "failed"
    assert result.status == "passed"
    assert test.execution_status == "not_run"


@pytest.mark.parametrize(
    ("baseline_exit", "proposed_exit", "expected"),
    [
        (1, 0, "detected"),
        (1, 1, "not_fixed"),
        (0, 0, "not_reproduced"),
        (0, 1, "regression_introduced"),
    ],
)
def test_paired_execution_classifies_comparable_outcomes(
    baseline_exit, proposed_exit, expected
):
    fix, test, eligibility = records()
    runtime = FakeRuntime(
        completion_by_source={
            BASELINE: RuntimeCompletion(baseline_exit, "baseline", ""),
            PROPOSED: RuntimeCompletion(proposed_exit, "proposed", ""),
        }
    )
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator", runtime=runtime,
        review_id="review-paired",
    )
    assert result.regression_status == expected
    assert result.baseline_execution is not None
    assert result.review_id == result.baseline_execution.review_id == "review-paired"
    assert result.baseline_execution.source_snapshot_id == result.source_snapshot_id
    assert len([call for call in runtime.calls if call[0] == "create"]) == 2
    assert len({call[2]["execution_id"] for call in runtime.calls if call[0] == "create"}) == 2
    artifact_roots = [call[1] for call in runtime.calls if call[0] == "create"]
    assert len({root.name for root in artifact_roots}) == 2
    assert all(not root.exists() for root in artifact_roots)
    assert test.execution_status == "not_run"


def test_paired_execution_never_detects_with_unavailable_or_inconclusive_evidence():
    fix, test, eligibility = records()
    unavailable = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator",
        runtime=FakeRuntime(preflight_error=RuntimeUnavailable("runsc unavailable")),
    )
    assert unavailable.regression_status == "unavailable"

    inconclusive = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator",
        runtime=FakeRuntime(
            completion_by_source={
                BASELINE: RuntimeCompletion(
                    None, "", "", timed_out=True, resource_limit_reason="wall_clock_timeout"
                ),
                PROPOSED: RuntimeCompletion(0, "1 passed", ""),
            }
        ),
    )
    assert inconclusive.regression_status == "inconclusive"


def test_paired_execution_rejects_snapshot_or_environment_mismatch_on_deserialization():
    fix, test, eligibility = records()
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator", runtime=FakeRuntime()
    )
    assert result.regression_status == "detected"
    malformed = result.model_dump(mode="json")
    malformed["baseline_execution"]["source_snapshot_id"] = "snapshot-" + "f" * 32
    with pytest.raises(ValidationError, match="baseline execution source_snapshot_id"):
        TestExecutionResult.model_validate(malformed)

    different_image = IMAGE.replace("a" * 64, "b" * 64)
    runtime = FakeRuntime(
        metadata_sequence=[
            RuntimeMetadata("fake-gvisor", "1", IMAGE, "3.12.10", "8.4.2"),
            RuntimeMetadata("fake-gvisor", "1", different_image, "3.12.10", "8.4.2"),
        ]
    )
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator", runtime=runtime
    )
    assert result.regression_status == "inconclusive"


def test_missing_baseline_result_cannot_be_deserialized_as_detected():
    fix, test, eligibility = records()
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator", runtime=FakeRuntime()
    )
    malformed = result.model_dump()
    malformed["baseline_execution"] = None
    malformed["regression_status"] = "detected"
    with pytest.raises(ValidationError, match="requires a persisted baseline"):
        TestExecutionResult.model_validate(malformed)


def test_durable_worker_sanitizes_child_environment_and_persists_bound_result(monkeypatch):
    from app.test_execution import execution_worker
    from app.verification.bounded_process import BoundedProcessResult

    fix, test, eligibility = records()
    expected = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator",
        runtime=FakeRuntime(), execution_id="execution-durable", review_id="review-durable",
    )
    payload = {
        "owner": "acme", "repo": "calculator", "review_id": "review-durable",
        "fix": fix.model_dump(mode="json"),
        "generated_test": test.model_dump(mode="json"),
        "eligibility": {
            "status": eligibility.status, "reason": eligibility.reason,
            "snapshot": eligibility.snapshot.model_dump(mode="json"),
            "baseline_source": eligibility.baseline_source,
            "proposed_source": eligibility.proposed_source,
        },
    }
    job = {"execution_id": "execution-durable", "payload": payload}
    saved = {}
    monkeypatch.setattr(execution_worker.jobs, "claim_next", lambda: job)
    monkeypatch.setattr(execution_worker.jobs, "heartbeat", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(execution_worker.jobs, "cancellation_requested", lambda _job_id: False)
    monkeypatch.setattr(execution_worker.jobs, "complete", lambda job_id, status, result: saved.update(job_id=job_id, status=status, result=result) or True)
    monkeypatch.setattr(execution_worker.jobs, "fail", lambda *_args, **_kwargs: pytest.fail("job should complete"))
    monkeypatch.setenv("GITHUB_TOKEN", "host-secret")
    monkeypatch.setenv("DATABASE_URL", "db-secret")
    monkeypatch.setenv("TMPDIR", "/var/lib/pr-mentor/execution-tmp")

    def fake_child(command, **kwargs):
        assert "GITHUB_TOKEN" not in kwargs["env"]
        assert "DATABASE_URL" not in kwargs["env"]
        assert kwargs["env"]["PR_MENTOR_EXECUTION_WORKER"] == "1"
        assert kwargs["env"].get("TMPDIR") == os.environ.get("TMPDIR")
        return BoundedProcessResult(0, expected.model_dump_json().encode(), b"", 0.1)

    monkeypatch.setattr(execution_worker, "run_bounded_process", fake_child)
    assert execution_worker.process_one() is True
    assert saved["job_id"] == "execution-durable"
    assert saved["status"] == "passed"
    assert saved["result"]["execution_id"] == "execution-durable"


def test_execution_report_is_persisted_in_owning_fix_history_once(tmp_path, monkeypatch):
    from app.database.reviews import get_store
    from app.test_execution.execution_reports import persist_execution_report

    fix, test, eligibility = records()
    execution = execute_generated_test(
        fix,
        test,
        eligibility,
        owner="acme",
        repo="calculator",
        runtime=FakeRuntime(),
        execution_id="execution-report-link",
        review_id="review-report-link",
    )
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "reviews.sqlite3"))
    fix_record = fix.model_dump(mode="json")
    review = {
        "review_id": "review-report-link",
        "created_at": datetime.now(UTC).isoformat(),
        "is_demo": False,
        "canonical_issues": [{
            "issue_id": "issue-1",
            "suggested_fixes": [fix_record],
        }],
    }
    store = get_store()
    store.save_review(review, owner_id="user-1")
    payload = {
        "owner_id": "user-1", "review_id": "review-report-link",
        "issue_id": "issue-1", "fix_id": "fix-1",
        "owner": "acme", "repo": "calculator",
        "eligibility": {"snapshot": eligibility.snapshot.model_dump(mode="json")},
    }

    report = persist_execution_report(payload, execution)
    repeated = persist_execution_report(payload, execution)
    assert report is not None and report.status == "inconclusive"
    assert report.report_failure is not None
    assert report.report_failure.code == "source_validation_not_included"
    assert repeated is not None and repeated.validation_id == execution.execution_id
    persisted = store.get_review("review-report-link", owner_id="user-1")
    reports = persisted["canonical_issues"][0]["suggested_fixes"][0]["validation_reports"]
    assert len(reports) == 1
    assert reports[0]["checks"][-1]["provenance_profile"] == "test_execution"
    assert reports[0]["checks"][-1]["evidence"][0]["kind"] == "test_result"
    assert reports[0]["checks"][-1]["evidence"][0]["sha256"]


def test_execution_report_can_pass_with_same_snapshot_source_validation(tmp_path, monkeypatch):
    from app.database.reviews import get_store
    from app.schemas.issue import (
        FixValidationCheckResult,
        FixValidationEvidenceReference,
        FixValidationReport,
    )
    from app.test_execution.execution_reports import persist_execution_report

    fix, test, eligibility = records()
    execution = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator",
        runtime=FakeRuntime(), execution_id="execution-with-source-validation",
        review_id="review-source-validation",
    )
    not_reproduced = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator",
        runtime=FakeRuntime(RuntimeCompletion(0, "1 passed", "")),
        execution_id="execution-not-reproduced",
        review_id="review-source-validation",
    )
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "reviews.sqlite3"))
    now = datetime.now(UTC)
    source_check = FixValidationCheckResult(
        check_id="ruff-static", check_name="Ruff differential analysis",
        provenance_profile="source_analysis", effective_provenance_profile="source_analysis",
        status="passed", required=True, tool_name="ruff", tool_version="0.9.0",
        configuration_sha256="b" * 64, started_at=now, completed_at=now,
        evidence=[FixValidationEvidenceReference(
            evidence_id="ruff-result", kind="tool_result", description="Ruff result.",
            sha256="c" * 64,
        )],
    )
    source_report = FixValidationReport(
        schema_version="2", validation_id="source-validation", fix_id="fix-1",
        attempt_number=1, status="passed", started_at=now, completed_at=now,
        snapshot=eligibility.snapshot, checks=[source_check],
    )
    fix_record = fix.model_dump(mode="json")
    fix_record["validation_reports"] = [source_report.model_dump(mode="json")]
    review = {
        "review_id": "review-source-validation", "created_at": now.isoformat(),
        "is_demo": False,
        "canonical_issues": [{"issue_id": "issue-1", "suggested_fixes": [fix_record]}],
    }
    store = get_store()
    store.save_review(review, owner_id="user-1")
    report = persist_execution_report({
        "owner_id": "user-1", "review_id": "review-source-validation",
        "issue_id": "issue-1", "fix_id": "fix-1", "owner": "acme", "repo": "calculator",
        "eligibility": {"snapshot": eligibility.snapshot.model_dump(mode="json")},
    }, execution)

    assert report is not None and report.status == "passed"
    assert report.report_failure is None
    assert {check.check_id for check in report.checks} == {
        "ruff-static", "stage6-execution-with-source-validation",
    }

    assert not_reproduced.regression_status == "not_reproduced", (
        not_reproduced.status,
        not_reproduced.baseline_execution.status if not_reproduced.baseline_execution else None,
        not_reproduced.reason,
    )
    inconclusive_report = persist_execution_report({
        "owner_id": "user-1", "review_id": "review-source-validation",
        "issue_id": "issue-1", "fix_id": "fix-1", "owner": "acme", "repo": "calculator",
        "eligibility": {"snapshot": eligibility.snapshot.model_dump(mode="json")},
    }, not_reproduced)
    assert inconclusive_report is not None
    assert inconclusive_report.status == "inconclusive"
    assert inconclusive_report.checks[-1].status == "inconclusive"


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        (RuntimeCompletion(1, "1 failed", ""), "failed"),
        (
            RuntimeCompletion(
                None, "", "", timed_out=True, resource_limit_reason="wall_clock_timeout"
            ),
            "timed_out",
        ),
        (RuntimeCompletion(None, "", "", cancelled=True), "cancelled"),
        (
            RuntimeCompletion(137, "", "", resource_limit_reason="memory_limit"),
            "infrastructure_error",
        ),
        (
            RuntimeCompletion(0, "x" * 10_000, "", output_truncated=True),
            "infrastructure_error",
        ),
    ],
)
def test_execution_status_distinguishes_test_failure_timeout_cancellation_and_infra(
    completion, expected
):
    fix, test, eligibility = records()
    result = execute_generated_test(
        fix,
        test,
        eligibility,
        owner="acme",
        repo="calculator",
        runtime=FakeRuntime(completion),
        limits=ExecutionResourceLimits(
            **{**DEFAULT_LIMITS.model_dump(), "output_bytes": 1_024}
        ),
    )
    assert result.status == expected
    assert result.cleanup_confirmed
    if expected == "infrastructure_error":
        assert result.infrastructure_error


def test_cleanup_failure_overrides_normal_pytest_pass():
    fix, test, eligibility = records()
    result = execute_generated_test(
        fix,
        test,
        eligibility,
        owner="acme",
        repo="calculator",
        runtime=FakeRuntime(cleanup=False),
    )
    assert result.status == "infrastructure_error"
    assert not result.cleanup_confirmed
    assert "cleanup" in result.infrastructure_error


def test_missing_runtime_is_unavailable_and_does_not_fall_back_to_host(monkeypatch):
    fix, test, eligibility = records()
    monkeypatch.delenv("PR_MENTOR_TEST_EXECUTION_IMAGE", raising=False)
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator"
    )
    assert result.status == "unavailable"
    assert result.exit_code is None
    assert not result.timeout_enforced
    assert "immutable image digest" in result.reason


def test_normal_backend_process_and_credentialed_worker_fail_closed(monkeypatch):
    fix, test, eligibility = records()
    monkeypatch.delenv("PR_MENTOR_EXECUTION_WORKER", raising=False)
    runtime = FakeRuntime()
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator", runtime=runtime
    )
    assert result.status == "unavailable"
    assert not runtime.calls
    monkeypatch.setenv("PR_MENTOR_EXECUTION_WORKER", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-present")
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator", runtime=runtime
    )
    assert result.status == "unavailable"
    assert not runtime.calls


def test_preflight_failure_is_unavailable_and_never_creates_execution():
    fix, test, eligibility = records()
    runtime = FakeRuntime(preflight_error=RuntimeUnavailable("runsc unavailable"))
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator", runtime=runtime
    )
    assert result.status == "unavailable"
    assert "runsc" in result.reason
    assert not any(call[0] == "create" for call in runtime.calls)


def test_result_serialization_round_trip_and_contract_guards():
    fix, test, eligibility = records()
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator", runtime=FakeRuntime()
    )
    restored = TestExecutionResult.model_validate_json(result.model_dump_json())
    assert restored == result
    with pytest.raises(ValidationError):
        TestExecutionResult.model_validate(
            {**result.model_dump(), "execution_image_digest": "mutable:latest"}
        )
    with pytest.raises(ValidationError):
        TestExecutionResult.model_validate(
            {**result.model_dump(), "status": "passed", "exit_code": 1}
        )
    with pytest.raises(ValidationError):
        TestExecutionResult.model_validate(
            {
                **result.model_dump(),
                "status": "passed",
                "cleanup_confirmed": False,
            }
        )


def test_all_execution_status_values_are_representable_without_promoting_fix_or_test():
    fix, test, eligibility = records()
    result = execute_generated_test(
        fix, test, eligibility, owner="acme", repo="calculator", runtime=FakeRuntime()
    )
    for status in (
        "passed",
        "failed",
        "timed_out",
        "cancelled",
        "unavailable",
        "rejected",
        "infrastructure_error",
        "not_run",
    ):
        data = result.model_dump()
        data.update(
            status=status,
            exit_code=0 if status == "passed" else 1 if status == "failed" else None,
            regression_status={
                "passed": "detected",
                "failed": "not_fixed",
                "timed_out": "inconclusive",
                "cancelled": "unavailable",
                "unavailable": "unavailable",
                "rejected": "unavailable",
                "infrastructure_error": "inconclusive",
                "not_run": "unavailable",
            }[status],
        )
        if status == "failed":
            data.update(
                output_truncated=False,
                resource_limit_reason=None,
                infrastructure_error=None,
            )
        if status == "timed_out":
            data.update(
                timeout_enforced=True, resource_limit_reason="wall_clock_timeout"
            )
        if status == "infrastructure_error":
            data.update(infrastructure_error="simulated infrastructure failure")
        if status == "passed":
            data.update(
                output_truncated=False,
                resource_limit_reason=None,
                infrastructure_error=None,
            )
        parsed = TestExecutionResult.model_validate(data)
        assert parsed.status == status
    assert test.execution_status == "not_run"


def test_limits_are_bounded_and_configuration_is_validated():
    assert load_execution_settings({}).limits == DEFAULT_LIMITS
    settings = load_execution_settings(
        {
            "PR_MENTOR_TEST_EXECUTION_IMAGE": IMAGE,
            "PR_MENTOR_TEST_EXECUTION_TIMEOUT_SECONDS": "15",
            "PR_MENTOR_TEST_EXECUTION_CPU_COUNT": "2",
        }
    )
    assert settings.limits.timeout_seconds == 15
    assert settings.limits.cpu_count == 2
    for env in (
        {"PR_MENTOR_TEST_EXECUTION_IMAGE": "python:latest"},
        {"PR_MENTOR_TEST_EXECUTION_TIMEOUT_SECONDS": "0"},
        {"PR_MENTOR_TEST_EXECUTION_MEMORY_BYTES": "999"},
        {"PR_MENTOR_TEST_EXECUTION_CPU_COUNT": "4"},
    ):
        with pytest.raises(ValueError):
            load_execution_settings(env)


def test_container_runtime_fails_closed_when_docker_cli_missing():
    with pytest.raises(RuntimeUnavailable, match="Docker CLI"):
        DockerGVisorRuntime(
            image_digest=IMAGE, docker_executable="prmentor-docker-not-installed"
        )


def test_docker_runtime_preflight_requires_cgroups_rootless_and_runsc(monkeypatch):
    runtime = DockerGVisorRuntime.__new__(DockerGVisorRuntime)
    runtime.image_digest = IMAGE
    runtime.docker = "docker"
    monkeypatch.setattr("app.test_execution.runtime.Path.is_file", lambda _path: True)
    monkeypatch.setattr(
        "app.test_execution.runtime.Path.read_text",
        lambda *_args, **_kwargs: "cpu memory pids",
    )
    answers = iter(
        [
            '["name=rootless"]|2|{"runsc":{"path":"runsc"}}',
            '{"org.prmentor.python-version":"3.12.10","org.prmentor.pytest-version":"8.4.2"}',
            "27.0.1",
        ]
    )
    monkeypatch.setattr(runtime, "_call", lambda *_args, **_kwargs: next(answers))
    assert runtime.preflight().name == "rootless-docker-gvisor"

    monkeypatch.setattr(
        runtime, "_call", lambda *_args, **_kwargs: '["name=rootless"]|1|{}'
    )
    with pytest.raises(RuntimeUnavailable, match="cgroups v2"):
        runtime.preflight()
    monkeypatch.setattr(
        runtime, "_call", lambda *_args, **_kwargs: '["name=rootless"]|2|{}'
    )
    with pytest.raises(RuntimeUnavailable, match="runsc"):
        runtime.preflight()


def test_docker_command_enforces_no_network_read_only_and_limits(tmp_path, monkeypatch):
    runtime = DockerGVisorRuntime.__new__(DockerGVisorRuntime)
    runtime.image_digest = IMAGE
    runtime.docker = "docker"
    metadata = RuntimeMetadata(
        "rootless-docker-gvisor", "27", IMAGE, "3.12.10", "8.4.2"
    )
    monkeypatch.setattr(runtime, "preflight", lambda: metadata)
    captured = []
    monkeypatch.setattr(
        runtime, "_call", lambda args, _timeout: captured.extend(args) or "container-id"
    )
    handle = runtime.create(
        tmp_path,
        source_relative_path="src/module.py",
        test_relative_path="test_generated.py",
        execution_id="ignored",
        limits=DEFAULT_LIMITS,
    )
    command = " ".join(captured)
    assert "--runtime=runsc" in command
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--pids-limit 32" in command
    assert "--memory 536870912" in command
    assert "--cpu-quota 100000" in command
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in command
    assert "python -I /opt/prmentor/runner.py" in command
    assert "--timeout-seconds 30" in command
    assert IMAGE in command
    assert handle.runtime_id.startswith("prmentor-test-")


def test_container_environment_never_inherits_application_secrets(monkeypatch):
    runtime = DockerGVisorRuntime.__new__(DockerGVisorRuntime)
    runtime.docker = "docker"
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("DATABASE_URL", "secret")
    env = runtime._worker_environment()
    assert not {"GITHUB_TOKEN", "LLM_API_KEY", "DATABASE_URL"} & env.keys()
