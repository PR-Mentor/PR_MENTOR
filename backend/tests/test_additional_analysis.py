from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.github_service.pull_requests import git_blob_sha
from app.schemas.issue import FixValidationSnapshot, SuggestedFix
from app.services.fix_eligibility import FixEligibilityResult
from app.verification import additional_analysis
from app.verification.additional_analysis import validate_static_fix_with_quality
from app.verification.analyzer_common import SourceToolRun, config_digest
from app.verification.bounded_process import BoundedProcessError
from app.verification.fix_snapshot import reconstruct_proposed_source

PATH = "src/math_ops.py"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def make_pair(replacement="    return value + 1"):
    baseline = "def increment(value: int) -> int:\n    return value + 1\n"
    fix = SuggestedFix(
        fix_id="fix-stage4",
        canonical_issue_id="issue-stage4",
        file_path=PATH,
        start_line=2,
        end_line=2,
        language="python",
        base_commit_sha=BASE_SHA,
        file_blob_sha=git_blob_sha(baseline.encode()),
        expected_original_code="    return value + 1",
        proposed_code=replacement,
        change_summary="Change increment behavior",
        rationale="Exercise type validation",
        confidence=0.8,
        generation_status="generated",
        generated_at=datetime.now(UTC),
    )
    proposed = reconstruct_proposed_source(baseline, fix).text
    snapshot = FixValidationSnapshot(
        file_path=PATH,
        base_commit_sha=BASE_SHA,
        checked_head_sha=HEAD_SHA,
        baseline_blob_sha=git_blob_sha(baseline.encode()),
        proposed_blob_sha=git_blob_sha(proposed.encode()),
    )
    eligibility = FixEligibilityResult(
        "eligible", "verified", snapshot, baseline, proposed
    )
    return fix, eligibility


def source_run(tool, findings, *, version="mypy 2.4.1", digest=None):
    now = datetime.now(UTC).isoformat()
    return SourceToolRun(
        tool_name=tool,
        tool_version=version,
        configuration_sha256=digest or config_digest({"tool": tool}),
        findings=tuple(findings),
        stdout=b"diagnostics",
        stderr=b"",
        started_at=now,
        completed_at=now,
        elapsed_seconds=0.01,
    )


def type_finding(
    *, line=2, message="type mismatch", rule="assignment", severity="error"
):
    return {
        "tool": "mypy",
        "file_path": PATH,
        "line": line,
        "column": 5,
        "end_line": line,
        "end_column": None,
        "rule_id": rule,
        "message": message,
        "severity": severity,
        "confidence": None,
        "test_name": None,
        "evidence": message,
        "context": None,
        "source": "mypy",
        "analysis_incomplete": False,
    }


def stubbed_stage3(monkeypatch):
    now = datetime.now(UTC).isoformat()

    def run(tool, *_args, **_kwargs):
        return SourceToolRun(
            tool_name=tool,
            tool_version=f"{tool} 1.0",
            configuration_sha256=config_digest({"tool": tool}),
            findings=(),
            stdout=b"[]",
            stderr=b"",
            started_at=now,
            completed_at=now,
            elapsed_seconds=0.01,
        )

    monkeypatch.setattr(
        "app.verification.source_analysis.run_ruff_source", lambda *a, **k: run("ruff")
    )
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source",
        lambda *a, **k: run("bandit"),
    )


def test_real_mypy_result_is_added_to_stage1_report_and_round_trips(monkeypatch):
    fix, eligibility = make_pair("    return value + 2")
    stubbed_stage3(monkeypatch)
    result = validate_static_fix_with_quality(fix, eligibility, timeout=10)
    assert result.report.status == "passed"
    assert result.report.engine_version == "phase4-stage4/1"
    assert {check.check_id for check in result.report.checks} == {
        "ruff-comparison",
        "bandit-comparison",
        "python-syntax",
        "mypy-comparison",
    }
    mypy = next(check for check in result.report.checks if check.tool_name == "mypy")
    assert mypy.status == "passed"
    assert mypy.effective_provenance_profile == "source_analysis"
    assert mypy.finding_counts.baseline_count == mypy.finding_counts.proposed_count == 0
    restored = type(result.report).model_validate(result.report.model_dump(mode="json"))
    assert restored.status == "passed"
    assert restored.snapshot.proposed_blob_sha == eligibility.snapshot.proposed_blob_sha
    assert all(ref.evidence_id in result.evidence_payloads for ref in mypy.evidence)


def test_introduced_mypy_type_error_fails_without_erasing_stage3_evidence(monkeypatch):
    fix, eligibility = make_pair("    return value + 'bad'")
    stubbed_stage3(monkeypatch)
    result = validate_static_fix_with_quality(fix, eligibility, timeout=10)
    assert result.report.status == "failed"
    assert all(
        check.status == "passed"
        for check in result.report.checks
        if check.check_id != "mypy-comparison"
    )
    mypy = next(
        check for check in result.report.checks if check.check_id == "mypy-comparison"
    )
    assert mypy.status == "failed"
    assert result.comparisons["mypy"][0].category == "introduced"


def test_unresolved_import_makes_check_inconclusive_not_failed(monkeypatch):
    fix, eligibility = make_pair()
    stubbed_stage3(monkeypatch)
    diagnostic = type_finding(
        message="Cannot find implementation or library stub for module named 'private_dep'",
        rule="import-not-found",
    )
    diagnostic["analysis_incomplete"] = True
    calls = 0

    def mypy(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return source_run("mypy", [diagnostic])

    monkeypatch.setattr(additional_analysis, "run_mypy_source", mypy)
    result = validate_static_fix_with_quality(fix, eligibility)
    check = next(
        check for check in result.report.checks if check.check_id == "mypy-comparison"
    )
    assert check.status == "inconclusive"
    assert result.report.status == "inconclusive"
    assert result.tool_results["mypy"]["status"] == "inconclusive"


@pytest.mark.parametrize(
    "baseline, proposed, category",
    [
        ([type_finding(line=2)], [type_finding(line=8)], "unchanged"),
        ([type_finding()], [], "resolved"),
        ([], [type_finding()], "introduced"),
    ],
)
def test_mypy_comparison_reuses_stage3_matcher(
    monkeypatch, baseline, proposed, category
):
    _fix, eligibility = make_pair()
    calls = 0

    def mypy(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return source_run("mypy", baseline if calls == 1 else proposed)

    monkeypatch.setattr(additional_analysis, "run_mypy_source", mypy)
    _details, check, deltas, _payloads = additional_analysis._analyze_mypy_pair(
        eligibility.baseline_source,
        eligibility.proposed_source,
        PATH,
        eligibility.snapshot.model_dump(mode="json"),
        5,
        100_000,
    )
    assert deltas[0].category == category
    assert check.status in {"passed", "failed", "inconclusive"}


def test_mypy_failure_is_independent_of_successful_stage3_checks(monkeypatch):
    fix, eligibility = make_pair()
    stubbed_stage3(monkeypatch)
    monkeypatch.setattr(
        additional_analysis,
        "run_mypy_source",
        lambda *_a, **_k: (_ for _ in ()).throw(
            BoundedProcessError("Analyzer timed out.", inconclusive=True)
        ),
    )
    result = validate_static_fix_with_quality(fix, eligibility)
    assert result.report.status == "inconclusive"
    assert all(
        check.status == "passed"
        for check in result.report.checks
        if check.check_id != "mypy-comparison"
    )
    assert (
        next(c for c in result.report.checks if c.check_id == "mypy-comparison").status
        == "inconclusive"
    )


def test_mypy_not_run_when_stage3_rejects_snapshot(monkeypatch):
    fix, eligibility = make_pair()
    rejected = FixEligibilityResult(
        "stale", "head_changed", eligibility.snapshot, None, None
    )
    monkeypatch.setattr(
        additional_analysis,
        "run_mypy_source",
        lambda *_a, **_k: pytest.fail("mypy must not run without eligible source"),
    )
    result = validate_static_fix_with_quality(fix, rejected)
    assert result.report.report_failure is not None
    assert result.report.status == "unavailable"
    assert (
        next(c for c in result.report.checks if c.check_id == "mypy-comparison").status
        == "not_run"
    )


def test_mypy_configuration_mismatch_is_inconclusive(monkeypatch):
    _fix, eligibility = make_pair()
    calls = 0

    def mypy(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return source_run("mypy", [], digest=config_digest({"run": calls}))

    monkeypatch.setattr(additional_analysis, "run_mypy_source", mypy)
    _details, check, deltas, _evidence = additional_analysis._analyze_mypy_pair(
        eligibility.baseline_source,
        eligibility.proposed_source,
        PATH,
        eligibility.snapshot.model_dump(mode="json"),
        5,
        100_000,
    )
    assert check.status == "inconclusive"
    assert not deltas
