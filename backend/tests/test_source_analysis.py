from datetime import UTC, datetime

import pytest

from app.github_service.pull_requests import GitHubFileSnapshot, git_blob_sha
from app.schemas.issue import SuggestedFix
from app.services import fix_eligibility
from app.services.fix_eligibility import FixEligibilityResult
from app.services.fix_validation import aggregate_validation_status
from app.verification.analyzer_common import SourceToolRun, config_digest
from app.verification.fix_snapshot import reconstruct_proposed_source
from app.verification.source_analysis import (
    _parse_python,
    validate_static_fix,
)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
PATH = "src/target.py"


def make_pair(baseline="value = 1\n", proposed_code="value = 2"):
    original_lines = baseline.splitlines()
    replacement_line = original_lines.index("value = 1") + 1
    fix = SuggestedFix(
        fix_id="fix-stage3",
        canonical_issue_id="issue-stage3",
        file_path=PATH,
        start_line=replacement_line,
        end_line=replacement_line,
        language="python",
        base_commit_sha=BASE_SHA,
        file_blob_sha=git_blob_sha(baseline.encode()),
        expected_original_code="value = 1",
        proposed_code=proposed_code,
        change_summary="Update the value",
        rationale="Use the corrected value",
        confidence=0.8,
        generation_status="generated",
        generated_at=datetime.now(UTC),
    )
    proposed = reconstruct_proposed_source(baseline, fix).text
    snapshot = {
        "file_path": PATH,
        "base_commit_sha": BASE_SHA,
        "checked_head_sha": HEAD_SHA,
        "baseline_blob_sha": git_blob_sha(baseline.encode()),
        "proposed_blob_sha": git_blob_sha(proposed.encode()),
    }
    from app.schemas.issue import FixValidationSnapshot

    eligibility = FixEligibilityResult(
        "eligible",
        "verified",
        FixValidationSnapshot(**snapshot),
        baseline,
        proposed,
    )
    return fix, eligibility


def fake_run(tool, findings, *, version="1.2.3", configuration=None):
    now = datetime.now(UTC).isoformat()
    return SourceToolRun(
        tool_name=tool,
        tool_version=version,
        configuration_sha256=configuration or config_digest({"tool": tool}),
        findings=tuple(findings),
        stdout=b"[]",
        stderr=b"",
        started_at=now,
        completed_at=now,
        elapsed_seconds=0.01,
    )


def finding(tool, *, line=1, message="diagnostic", rule="R001", context="value"):
    return {
        "tool": tool,
        "file_path": PATH,
        "line": line,
        "column": 1,
        "end_line": line,
        "end_column": 5,
        "rule_id": rule,
        "message": message,
        "severity": "HIGH" if tool == "bandit" else None,
        "confidence": "MEDIUM" if tool == "bandit" else None,
        "test_name": "test_rule" if tool == "bandit" else None,
        "evidence": context,
        "context": context,
        "source": tool,
    }


def test_valid_source_pair_builds_stage1_report_and_evidence(monkeypatch):
    fix, eligibility = make_pair()
    monkeypatch.setattr(
        "app.verification.source_analysis.run_ruff_source",
        lambda *_args, **_kwargs: fake_run("ruff", []),
    )
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source",
        lambda *_args, **_kwargs: fake_run("bandit", []),
    )
    outcome = validate_static_fix(
        fix, eligibility, validation_id="stage3-test", attempt_number=2
    )
    assert outcome.report.status == "passed"
    assert outcome.report.schema_version == "2"
    assert outcome.report.snapshot.checked_head_sha == HEAD_SHA
    assert {check.check_id for check in outcome.report.checks} == {
        "ruff-comparison",
        "bandit-comparison",
        "python-syntax",
    }
    assert all(
        check.provenance_profile == "source_analysis" for check in outcome.report.checks
    )
    assert all(check.tool_version for check in outcome.report.checks)
    assert all(check.configuration_sha256 for check in outcome.report.checks)
    assert all(check.evidence for check in outcome.report.checks)
    restored = type(outcome.report).model_validate(
        outcome.report.model_dump(mode="json")
    )
    assert restored.status == "passed"
    for check in restored.checks:
        for reference in check.evidence:
            assert reference.evidence_id in outcome.evidence_payloads


def test_stage2_proposed_blob_mismatch_stops_before_analyzers(monkeypatch):
    fix, eligibility = make_pair()
    eligibility = FixEligibilityResult(
        "eligible",
        "verified",
        eligibility.snapshot.model_copy(update={"proposed_blob_sha": "c" * 40}),
        eligibility.baseline_source,
        eligibility.proposed_source,
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Analyzer must not run for a mismatched Stage 2 snapshot")

    monkeypatch.setattr("app.verification.source_analysis.run_ruff_source", unexpected)
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source", unexpected
    )
    outcome = validate_static_fix(fix, eligibility)
    assert outcome.report.status == "inconclusive"
    assert outcome.report.report_failure.code == "snapshot_integrity_mismatch"
    assert not outcome.report.checks


def test_introduced_finding_fails_only_its_independent_analyzer(monkeypatch):
    fix, eligibility = make_pair()
    calls = {"ruff": 0}

    def ruff(*_args, **_kwargs):
        calls["ruff"] += 1
        return fake_run(
            "ruff",
            [] if calls["ruff"] == 1 else [finding("ruff", rule="F401")],
        )

    monkeypatch.setattr("app.verification.source_analysis.run_ruff_source", ruff)
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source",
        lambda *_args, **_kwargs: fake_run("bandit", []),
    )
    outcome = validate_static_fix(fix, eligibility)
    assert outcome.report.status == "failed"
    assert (
        next(c for c in outcome.report.checks if c.check_id == "ruff-comparison").status
        == "failed"
    )
    assert (
        next(
            c for c in outcome.report.checks if c.check_id == "bandit-comparison"
        ).status
        == "passed"
    )
    assert outcome.comparisons["ruff"][0].category == "introduced"


def test_analyzer_failure_isolated_and_not_a_pass(monkeypatch):
    fix, eligibility = make_pair()

    def fail(*_args, **_kwargs):
        raise FileNotFoundError("missing executable")

    monkeypatch.setattr("app.verification.source_analysis.run_ruff_source", fail)
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source",
        lambda *_args, **_kwargs: fake_run("bandit", []),
    )
    outcome = validate_static_fix(fix, eligibility)
    assert outcome.report.status == "partial"
    assert (
        next(c for c in outcome.report.checks if c.check_id == "ruff-comparison").status
        == "unavailable"
    )
    assert (
        next(
            c for c in outcome.report.checks if c.check_id == "bandit-comparison"
        ).status
        == "passed"
    )


def test_proposed_failure_retains_completed_baseline_output(monkeypatch):
    fix, eligibility = make_pair()
    calls = {"n": 0}

    def ruff(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return fake_run("ruff", [finding("ruff", rule="F821")])
        raise FileNotFoundError("missing after baseline")

    monkeypatch.setattr("app.verification.source_analysis.run_ruff_source", ruff)
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source",
        lambda *_args, **_kwargs: fake_run("bandit", []),
    )
    outcome = validate_static_fix(fix, eligibility)
    ruff_result = outcome.tool_results["ruff"]
    assert ruff_result["status"] == "unavailable"
    assert ruff_result["baseline_findings"][0]["rule_id"] == "F821"
    assert any(
        key.startswith("ruff-baseline-stdout") for key in outcome.evidence_payloads
    )


def test_configuration_or_version_mismatch_is_inconclusive(monkeypatch):
    fix, eligibility = make_pair()
    calls = {"n": 0}

    def ruff(*_args, **_kwargs):
        calls["n"] += 1
        return fake_run("ruff", [], version="1.2.3" if calls["n"] == 1 else "1.2.4")

    monkeypatch.setattr("app.verification.source_analysis.run_ruff_source", ruff)
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source",
        lambda *_args, **_kwargs: fake_run("bandit", []),
    )
    outcome = validate_static_fix(fix, eligibility)
    assert (
        next(c for c in outcome.report.checks if c.check_id == "ruff-comparison").status
        == "inconclusive"
    )


@pytest.mark.parametrize(
    "baseline, replacement, expected_status, expected_category",
    [
        ("value = 1\n", "def broken(:", "failed", "introduced"),
        ("def broken(:\nvalue = 1\n", "value = 2", "failed", "unchanged"),
    ],
)
def test_syntax_errors_are_compared_without_execution(
    monkeypatch, baseline, replacement, expected_status, expected_category
):
    fix, eligibility = make_pair(baseline, replacement)
    monkeypatch.setattr(
        "app.verification.source_analysis.run_ruff_source",
        lambda *_args, **_kwargs: fake_run("ruff", []),
    )
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source",
        lambda *_args, **_kwargs: fake_run("bandit", []),
    )
    outcome = validate_static_fix(fix, eligibility)
    syntax = next(c for c in outcome.report.checks if c.check_id == "python-syntax")
    assert syntax.status == expected_status
    assert outcome.comparisons["python_ast"][0].category == expected_category


def test_ast_parser_handles_unicode_empty_and_does_not_import_modules():
    valid = _parse_python(PATH, "# café\nimport nonexistent_local_module\n")
    empty = _parse_python(PATH, "")
    invalid = _parse_python(PATH, "x = (\n")
    assert valid.findings == ()
    assert empty.findings == ()
    assert invalid.findings[0]["rule_id"] == "PYTHON_SYNTAX"


def test_changed_line_attribution_uses_added_lines(monkeypatch):
    fix, eligibility = make_pair("value = 1\nother = 1\n", "value = 2")
    calls = {"n": 0}

    def ruff(*_args, **_kwargs):
        calls["n"] += 1
        diagnostics = [] if calls["n"] == 1 else [finding("ruff", line=2, rule="F821")]
        return fake_run("ruff", diagnostics)

    monkeypatch.setattr("app.verification.source_analysis.run_ruff_source", ruff)
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source",
        lambda *_args, **_kwargs: fake_run("bandit", []),
    )
    outcome = validate_static_fix(fix, eligibility)
    delta = outcome.tool_results["ruff"]["deltas"][0]
    assert delta["category"] == "introduced"
    assert delta["attribution"] == "outside_added_lines"


def test_required_check_aggregation_stays_stage1_authoritative(monkeypatch):
    fix, eligibility = make_pair()
    monkeypatch.setattr(
        "app.verification.source_analysis.run_ruff_source",
        lambda *_args, **_kwargs: fake_run("ruff", []),
    )
    monkeypatch.setattr(
        "app.verification.source_analysis.run_bandit_source",
        lambda *_args, **_kwargs: fake_run("bandit", []),
    )
    outcome = validate_static_fix(fix, eligibility)
    assert outcome.report.status == aggregate_validation_status(outcome.report.checks)


@pytest.mark.anyio
async def test_stage2_eligibility_feeds_real_stage3_analyzers(
    monkeypatch, anyio_backend
):
    fix, _ = make_pair()

    async def current_head(*_args):
        return BASE_SHA

    async def fetch_snapshot(_owner, _repo, path, commit):
        content = b"value = 1\n"
        return GitHubFileSnapshot(
            path,
            commit,
            git_blob_sha(content),
            content,
            content.decode("utf-8"),
        )

    monkeypatch.setattr(
        fix_eligibility, "fetch_current_pull_request_head_sha", current_head
    )
    monkeypatch.setattr(fix_eligibility, "fetch_file_snapshot", fetch_snapshot)
    eligible = await fix_eligibility.evaluate_fix_eligibility(
        fix, owner="example", repo="repo", pull_request_number=12
    )
    outcome = validate_static_fix(fix, eligible, validation_id="stage2-stage3")
    assert eligible.status == "eligible"
    assert outcome.report.status == "passed"
    assert outcome.report.snapshot.baseline_blob_sha == fix.file_blob_sha
    assert (
        outcome.report.snapshot.proposed_blob_sha == eligible.snapshot.proposed_blob_sha
    )
    assert outcome.tool_results["ruff"]["tool_version"]
    assert outcome.tool_results["bandit"]["tool_version"]
    assert all(outcome.report.checks[index].configuration_sha256 for index in (0, 1))
