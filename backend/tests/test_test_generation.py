import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.ai_review import generated_tests as test_generation
from app.ai_review.generated_test_context import (
    InsufficientTestContext,
    build_test_generation_context,
)
from app.ai_review.generated_test_quality import review_candidate
from app.ai_review.providers import ProviderConfig
from app.github_service.pull_requests import git_blob_sha
from app.schemas.generated_test import TestGenerationResult
from app.schemas.issue import (
    CanonicalIssue,
    FixValidationReport,
    FixValidationSnapshot,
    SuggestedFix,
)
from app.services.fix_eligibility import FixEligibilityResult
from app.services.fix_validation import aggregate_validation_status

SOURCE = "def compute(value):\n    if value < 0:\n        return 0\n    return value\n"
FIXED = "def compute(value):\n    if value < 0:\n        return 0\n    return value + 1\n"
HEAD = "a" * 40


def make_records():
    now = datetime.now(UTC)
    fix = SuggestedFix(
        fix_id="fix-1", canonical_issue_id="issue-1", file_path="src/math_ops.py",
        start_line=4, end_line=4, language="python", base_commit_sha=HEAD,
        file_blob_sha=git_blob_sha(SOURCE.encode()), expected_original_code="    return value",
        proposed_code="    return value + 1", change_summary="Correct the returned value.",
        rationale="The issue says the result is off by one.", confidence=0.8,
        generation_status="generated", generated_at=now,
    )
    issue = CanonicalIssue(
        issue_id="issue-1", file_path=fix.file_path, line_start=4, line_end=4,
        category="bug", severity="medium", normalized_description="Compute the corrected value.",
        suggested_fixes=[fix],
    )
    snapshot = {
        "file_path": fix.file_path, "base_commit_sha": HEAD, "checked_head_sha": HEAD,
        "baseline_blob_sha": git_blob_sha(SOURCE.encode()),
        "proposed_blob_sha": git_blob_sha(FIXED.encode()),
    }
    eligibility = FixEligibilityResult(
        "eligible", "verified", snapshot=FixValidationSnapshot(**snapshot),
        baseline_source=SOURCE, proposed_source=FIXED,
    )
    return fix, issue, eligibility


def _configure_provider_transport(monkeypatch, response):
    """Exercise the real provider orchestration with a deterministic transport."""
    provider = ProviderConfig(
        "test_provider", "test-key", "test-model", "https://provider.invalid/chat", True, 1
    )
    monkeypatch.setenv("LLM_PROVIDER_RETRIES", "1")

    monkeypatch.setattr(
        test_generation.client, "provider_registry", lambda _task=None: [provider]
    )

    calls = []

    async def request_provider(*_args, **_kwargs):
        calls.append(True)
        if isinstance(response, Exception):
            raise response
        return response, {"total_tokens": 5}, 12

    monkeypatch.setattr(test_generation.client, "_request_provider", request_provider)
    return calls


GOOD = {
    "test_name": "test_compute_applies_corrected_value",
    "test_code": "from src.math_ops import compute\n\ndef test_compute_applies_corrected_value():\n    assert compute(2) == 3\n",
    "category": "fix_verification", "description": "Checks the changed return behavior.",
    "rationale": "The proposed branch returns the input plus one.",
    "expected_behavior": "compute(2) returns 3.",
    "source_references": [{"source_version": "proposed", "line_start": 1, "line_end": 4, "description": "Changed function body."},
                          {"source_version": "issue", "line_start": None, "line_end": None, "description": "Issue expectation."}],
}


def test_context_rejects_snapshot_hash_mismatch():
    fix, issue, eligibility = make_records()
    eligibility.snapshot.baseline_blob_sha = "b" * 40
    with pytest.raises(InsufficientTestContext, match="Baseline source"):
        build_test_generation_context(fix, issue, owner="acme", repo="repo", eligibility=eligibility)


def test_context_contains_baseline_proposed_and_labels_ai_explanation():
    fix, issue, eligibility = make_records()
    context = build_test_generation_context(fix, issue, owner="acme", repo="repo", eligibility=eligibility)
    assert context.prompt_data["source_snapshot"]["baseline_excerpt"] != context.prompt_data["source_snapshot"]["proposed_excerpt"]
    assert context.repository_id == "acme/repo"
    assert len(context.source_snapshot_id) == 41
    assert context.prompt_data["source_snapshot"]["related_tests"].startswith("Unavailable")


def test_quality_accepts_grounded_candidate_without_running_code():
    fix, issue, eligibility = make_records()
    context = build_test_generation_context(fix, issue, owner="acme", repo="repo", eligibility=eligibility)
    reviewed = review_candidate(GOOD, context, source_path=fix.file_path)
    assert reviewed.accepted
    assert reviewed.assessment.outcome == "ready_for_review"
    assert reviewed.references[0].line_start == 1


def test_quality_rejects_candidate_unrelated_to_verified_source_symbols():
    fix, issue, eligibility = make_records()
    context = build_test_generation_context(fix, issue, owner="acme", repo="repo", eligibility=eligibility)
    candidate = {
        **GOOD,
        "test_name": "test_unrelated",
        "test_code": "def test_unrelated():\n    assert unrelated(1) == 1\n",
    }
    reviewed = review_candidate(candidate, context, source_path=fix.file_path)
    assert not reviewed.accepted
    assert "does not reference a symbol" in reviewed.rejection_reason


@pytest.mark.parametrize("code", [
    "x = open('x', 'w')\ndef test_a():\n    assert True\n",
    "import subprocess\ndef test_a():\n    assert 1 == 1\n",
    "def test_a():\n    assert True\n",
    "def test_a():\n    eval('1 + 1')\n    assert 1 == 1\n",
    "@dangerous()\ndef test_a():\n    assert 1 == 1\n",
    "def test_a(value=dangerous()):\n    assert value == 1\n",
])
def test_quality_rejects_unsafe_or_meaningless_candidates(code):
    fix, issue, eligibility = make_records()
    context = build_test_generation_context(fix, issue, owner="acme", repo="repo", eligibility=eligibility)
    candidate = {**GOOD, "test_code": code}
    result = review_candidate(candidate, context, source_path=fix.file_path)
    assert not result.accepted
    assert result.assessment.outcome == "rejected"


def test_generation_returns_unexecuted_test_and_does_not_change_fix_history(monkeypatch):
    fix, issue, eligibility = make_records()
    history = fix.model_dump(mode="json")["validation_reports"]

    async def eligible(*args, **kwargs):
        return eligibility

    async def complete(*args, **kwargs):
        return json.dumps({"ambiguities": [], "tests": [GOOD]})

    monkeypatch.setattr(test_generation, "evaluate_fix_eligibility", eligible)
    monkeypatch.setattr(test_generation.client, "is_configured", lambda: True)
    monkeypatch.setattr(test_generation.client, "configured_model", lambda *_args: "test-model")
    monkeypatch.setattr(test_generation.client, "complete_json", complete)
    result = asyncio.run(test_generation.generate_tests_for_fix(
        fix, issue, owner="acme", repo="repo", pull_request_number=2,
    ))
    assert result.status == "generated"
    assert result.repository_id == "acme/repo"
    assert len(result.tests) == 1 and result.tests[0].execution_status == "not_run"
    assert TestGenerationResult.model_validate_json(result.model_dump_json()) == result
    assert result.validation_check.status == "not_run" and not result.validation_check.required
    assert all(ref.kind != "test_result" for ref in result.validation_check.evidence)
    report = FixValidationReport(
        validation_id="validation-generated-test-check", fix_id=fix.fix_id, attempt_number=1,
        status=aggregate_validation_status([result.validation_check]), started_at=result.generation_timestamp,
        completed_at=result.completed_at, checks=[result.validation_check],
    )
    assert report.status == "not_run"
    assert fix.model_dump(mode="json")["validation_reports"] == history


def test_generation_marks_malformed_provider_response_unavailable(monkeypatch):
    fix, issue, eligibility = make_records()

    async def eligible(*args, **kwargs):
        return eligibility

    monkeypatch.setattr(test_generation, "evaluate_fix_eligibility", eligible)
    monkeypatch.setattr(test_generation.client, "is_configured", lambda: True)
    monkeypatch.setattr(test_generation.client, "configured_model", lambda *_args: "test-model")
    calls = _configure_provider_transport(
        monkeypatch,
        test_generation.AIReviewUnavailable(
            "AI provider returned malformed structured response."
        ),
    )
    result = asyncio.run(test_generation.generate_tests_for_fix(
        fix, issue, owner="acme", repo="repo", pull_request_number=2,
    ))
    assert result.status == "unavailable"
    assert result.provider_call_count == len(calls) == 2
    assert result.tests == [] and result.execution_status == "not_run"


def test_generation_reports_partial_acceptance_without_claiming_execution(monkeypatch):
    fix, issue, eligibility = make_records()

    async def eligible(*args, **kwargs):
        return eligibility

    async def complete(*args, **kwargs):
        invalid = {**GOOD, "test_code": "def test_empty():\n    assert True\n"}
        return json.dumps({"ambiguities": ["Expected behavior may depend on caller input."], "tests": [GOOD, invalid]})

    monkeypatch.setattr(test_generation, "evaluate_fix_eligibility", eligible)
    monkeypatch.setattr(test_generation.client, "is_configured", lambda: True)
    monkeypatch.setattr(test_generation.client, "configured_model", lambda *_args: "test-model")
    monkeypatch.setattr(test_generation.client, "complete_json", complete)
    result = asyncio.run(test_generation.generate_tests_for_fix(
        fix, issue, owner="acme", repo="repo", pull_request_number=2,
    ))
    assert result.status == "partial"
    assert len(result.tests) == len(result.rejected_tests) == 1
    assert result.ambiguities
    assert result.validation_check.status == "not_run"


def test_stale_fix_is_not_attempted_and_provider_is_not_called(monkeypatch):
    fix, issue, _ = make_records()

    async def stale(*args, **kwargs):
        return FixEligibilityResult("stale", "source changed")

    def unexpected_call(*args, **kwargs):
        raise AssertionError("provider must not be called for a stale fix")

    monkeypatch.setattr(test_generation, "evaluate_fix_eligibility", stale)
    monkeypatch.setattr(test_generation.client, "complete_json", unexpected_call)
    result = asyncio.run(test_generation.generate_tests_for_fix(
        fix, issue, owner="acme", repo="repo", pull_request_number=2,
    ))
    assert result.status == "not_attempted"
    assert result.provider_call_count == 0


def test_provider_failure_is_unavailable_and_still_not_run(monkeypatch):
    fix, issue, eligibility = make_records()

    async def eligible(*args, **kwargs):
        return eligibility

    monkeypatch.setattr(test_generation, "evaluate_fix_eligibility", eligible)
    monkeypatch.setattr(test_generation.client, "is_configured", lambda: True)
    monkeypatch.setattr(test_generation.client, "configured_model", lambda *_args: "test-model")
    calls = _configure_provider_transport(
        monkeypatch, test_generation.AIReviewUnavailable("provider down")
    )
    result = asyncio.run(test_generation.generate_tests_for_fix(
        fix, issue, owner="acme", repo="repo", pull_request_number=2,
    ))
    assert result.status == "unavailable"
    assert result.provider_call_count == len(calls) == 2
    assert result.execution_status == result.validation_check.status == "not_run"


def test_all_candidates_rejected_are_not_reported_as_failed_execution(monkeypatch):
    fix, issue, eligibility = make_records()

    async def eligible(*args, **kwargs):
        return eligibility

    async def complete(*args, **kwargs):
        invalid = {**GOOD, "test_code": "def test_bad():\n    assert True\n", "test_name": "test_bad"}
        return json.dumps({"ambiguities": [], "tests": [invalid]})

    monkeypatch.setattr(test_generation, "evaluate_fix_eligibility", eligible)
    monkeypatch.setattr(test_generation.client, "is_configured", lambda: True)
    monkeypatch.setattr(test_generation.client, "configured_model", lambda *_args: "test-model")
    monkeypatch.setattr(test_generation.client, "complete_json", complete)
    result = asyncio.run(test_generation.generate_tests_for_fix(
        fix, issue, owner="acme", repo="repo", pull_request_number=2,
    ))
    assert result.status == "rejected_by_quality"
    assert len(result.rejected_tests) == 1 and not result.tests
    assert result.execution_status == result.validation_check.status == "not_run"
