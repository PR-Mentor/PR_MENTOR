from datetime import UTC, datetime, timedelta

import mongomock
import pytest
from pydantic import ValidationError

from app.data.sample_reviews import sample_review
from app.database.mongo_reviews import MongoReviewStore
from app.database.reviews import SQLiteReviewStore
from app.schemas.issue import (
    CanonicalIssue,
    FixValidationCheckResult,
    FixValidationEvidenceReference,
    FixValidationFindingCounts,
    FixValidationReport,
    FixValidationReportFailure,
    FixValidationSnapshot,
    SuggestedFix,
)
from app.services.fix_validation import aggregate_validation_status

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def check(status, *, required=True, check_id="ruff"):
    values = {
        "check_id": check_id, "check_name": check_id.title(), "provenance_profile": "non_source",
        "status": status, "required": required,
    }
    if status == "passed":
        values.update(
            started_at=NOW, completed_at=NOW + timedelta(seconds=1),
            evidence=[FixValidationEvidenceReference(
                evidence_id=f"{check_id}-evidence", kind="tool_result", description="Recorded result",
            )],
        )
    return FixValidationCheckResult(**values)


@pytest.mark.parametrize(("checks", "expected"), [
    ([], "not_run"),
    ([check("passed")], "passed"),
    ([check("passed"), check("passed", check_id="syntax")], "passed"),
    ([check("failed", required=False)], "failed"),
    ([check("passed"), check("failed", required=False)], "failed"),
    ([check("inconclusive")], "inconclusive"),
    ([check("passed"), check("unavailable")], "partial"),
    ([check("unavailable")], "unavailable"),
    ([check("skipped")], "not_run"),
    ([check("not_run")], "not_run"),
    ([check("skipped", required=False)], "not_run"),
    ([check("partial")], "partial"),
    ([check("unavailable"), check("skipped")], "unavailable"),
])
def test_aggregate_validation_status_is_deterministic(checks, expected):
    assert aggregate_validation_status(checks) == expected


def test_report_contract_captures_snapshot_tools_and_evidence():
    evidence = FixValidationEvidenceReference(
        evidence_id="ruff-findings", kind="tool_result", description="Ruff JSON result",
        file_path="src/query.py", line_start=10, line_end=11, sha256="d" * 64,
    )
    check_result = FixValidationCheckResult(
        check_id="ruff-diff", check_name="Ruff baseline comparison", provenance_profile="source_analysis", status="passed",
        tool_name="ruff", tool_version="0.14.0", configuration_sha256="c" * 64,
        started_at=NOW, completed_at=NOW + timedelta(seconds=1),
        finding_counts=FixValidationFindingCounts(
            baseline_count=2, proposed_count=1, introduced_count=0, resolved_count=1, unchanged_count=1,
        ),
        evidence=[evidence],
    )
    report = FixValidationReport(
        validation_id="validation-1", fix_id="fix-001", attempt_number=1, status="passed",
        started_at=NOW, completed_at=NOW + timedelta(seconds=2),
        snapshot=FixValidationSnapshot(
            file_path="src/query.py", base_commit_sha="a" * 40,
            checked_head_sha="b" * 40, baseline_blob_sha="c" * 40, proposed_blob_sha="d" * 40,
        ), checks=[check_result],
    )

    restored = FixValidationReport.model_validate(report.model_dump(mode="json"))
    assert restored == report
    assert restored.checks[0].finding_counts.resolved_count == 1
    assert restored.checks[0].evidence[0].sha256 == "d" * 64


@pytest.mark.parametrize("payload", [
    {"file_path": "../secret.py"},
    {"file_path": "src\\query.py"},
    {"file_path": "/src/query.py"},
    {"file_path": "src/./query.py"},
    {"file_path": "src/query.py", "base_commit_sha": "short"},
    {"file_path": "src/query.py", "checked_head_sha": "short"},
    {"file_path": "src/query.py", "baseline_blob_sha": "short"},
    {"file_path": "src/query.py", "proposed_blob_sha": "short"},
])
def test_snapshot_rejects_invalid_paths_or_partial_hashes(payload):
    with pytest.raises(ValidationError):
        FixValidationSnapshot.model_validate(payload)


@pytest.mark.parametrize("digest", ["g" * 40, "z" * 64])
def test_snapshot_rejects_exact_length_non_hex_hashes(digest):
    with pytest.raises(ValidationError, match="full 40- or 64-character hexadecimal"):
        FixValidationSnapshot(file_path="src/query.py", checked_head_sha=digest)


@pytest.mark.parametrize("digest", ["a" * 40, "A" * 40, "aB09" * 16, "Ff01" * 16])
def test_snapshot_accepts_full_sha1_and_sha256_hashes_in_mixed_case(digest):
    snapshot = FixValidationSnapshot(file_path="src/query.py", checked_head_sha=digest)
    assert snapshot.checked_head_sha == digest


def test_evidence_and_checks_reject_invalid_metadata():
    with pytest.raises(ValidationError):
        FixValidationEvidenceReference(evidence_id="e", kind="other", description="x", sha256="bad")
    with pytest.raises(ValidationError):
        FixValidationCheckResult(
            check_id="x", check_name="x", status="passed", started_at=datetime(2026, 1, 1),
        )
    with pytest.raises(ValidationError):
        FixValidationCheckResult(
            check_id="x", check_name="x", status="passed", started_at=NOW,
            completed_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError):
        FixValidationFindingCounts(
            baseline_count=2, proposed_count=3, introduced_count=1, resolved_count=0, unchanged_count=1,
        )


@pytest.mark.parametrize(("field", "value"), [
    ("tool_name", " "), ("tool_name", "\t"), ("tool_name", "\n"), ("tool_name", "bad\x00tool"),
    ("tool_version", " "), ("tool_version", "\t"), ("tool_version", "\n"), ("tool_version", "1\x00.0"),
])
def test_tool_identity_rejects_blank_or_nul_metadata(field, value):
    values = _passed_check_for_profile("source_analysis")
    values[field] = value
    with pytest.raises(ValidationError, match="tool identity"):
        FixValidationCheckResult(**values)


@pytest.mark.parametrize("value", [" ", "\t", "\n", "engine\x00version"])
def test_engine_version_rejects_blank_or_nul_metadata(value):
    with pytest.raises(ValidationError, match="engine_version"):
        FixValidationReport(
            validation_id="v", fix_id="f", attempt_number=1, status="not_run",
            started_at=NOW, engine_version=value,
        )


def test_valid_tool_and_engine_metadata_are_preserved():
    check_result = FixValidationCheckResult(**_passed_check_for_profile("source_analysis"))
    report = FixValidationReport(
        validation_id="v", fix_id="f", attempt_number=1, status="passed", started_at=NOW,
        completed_at=NOW + timedelta(seconds=2), engine_version="PR Mentor 1.0",
        snapshot=FixValidationSnapshot(
            file_path="src/query.py", checked_head_sha="a" * 40,
            baseline_blob_sha="b" * 40, proposed_blob_sha="c" * 40,
        ), checks=[check_result],
    )
    assert report.engine_version == "PR Mentor 1.0"
    assert report.checks[0].tool_name == "ruff"
    assert report.checks[0].tool_version == "1.0"


def test_report_rejects_duplicate_checks_and_bad_timestamp_order():
    duplicate_checks = [check("passed", check_id="same"), check("passed", check_id="same")]
    with pytest.raises(ValidationError, match="check IDs must be unique"):
        FixValidationReport(
            validation_id="v", fix_id="f", attempt_number=1, status="passed",
            started_at=NOW, checks=duplicate_checks,
        )
    with pytest.raises(ValidationError, match="completed_at"):
        FixValidationReport(
            validation_id="v", fix_id="f", attempt_number=1, status="passed",
            started_at=NOW, completed_at=NOW - timedelta(seconds=1),
        )


def test_report_cannot_claim_pass_with_incomplete_or_failed_checks():
    with pytest.raises(ValidationError, match="contradicts its checks"):
        FixValidationReport(
            validation_id="v", fix_id="f", attempt_number=1, status="passed",
            started_at=NOW, checks=[check("unavailable")],
        )


@pytest.mark.parametrize(("check_status", "correct_report_status"), [
    ("passed", "passed"),
    ("failed", "failed"),
    ("partial", "partial"),
    ("inconclusive", "inconclusive"),
    ("unavailable", "unavailable"),
    ("not_run", "not_run"),
    ("skipped", "not_run"),
])
def test_report_status_must_match_authoritative_aggregation_on_create_and_restore(
    check_status, correct_report_status,
):
    outcomes = {"passed", "failed", "partial", "inconclusive", "unavailable", "not_run"}
    expected = FixValidationReport(
        validation_id="v", fix_id="f", attempt_number=1,
        status=correct_report_status, started_at=NOW, completed_at=NOW + timedelta(seconds=2),
        checks=[check(check_status)],
    )
    assert aggregate_validation_status(expected.checks) == expected.status
    for contradictory_status in outcomes - {correct_report_status}:
        payload = expected.model_dump(mode="json")
        payload["status"] = contradictory_status
        with pytest.raises(ValidationError, match="contradicts its checks"):
            FixValidationReport.model_validate(payload)
        with pytest.raises(ValidationError, match="contradicts its checks"):
            FixValidationReport(**{**payload, "started_at": NOW, "checks": [check(check_status)]})


@pytest.mark.parametrize(("checks", "failure_status", "expected"), [
    ([], "failed", "failed"),
    ([], "unavailable", "unavailable"),
    ([], "inconclusive", "inconclusive"),
    (["passed"], "failed", "failed"),
    (["passed"], "unavailable", "partial"),
    (["passed"], "inconclusive", "inconclusive"),
    (["failed"], "unavailable", "failed"),
    (["inconclusive"], "unavailable", "inconclusive"),
    (["unavailable"], "unavailable", "unavailable"),
])
def test_report_level_failures_are_distinct_and_have_explicit_precedence(checks, failure_status, expected):
    failure = FixValidationReportFailure(
        code="snapshot_retrieval_failed", status=failure_status, reason="The source snapshot was unavailable.",
    )
    values = [check(status, check_id=f"check-{index}") for index, status in enumerate(checks)]
    assert aggregate_validation_status(values, report_failure=failure) == expected
    report = FixValidationReport(
        validation_id="v", fix_id="f", attempt_number=1, status=expected,
        started_at=NOW, completed_at=NOW + timedelta(seconds=2), checks=values, report_failure=failure,
    )
    restored = FixValidationReport.model_validate(report.model_dump(mode="json"))
    assert restored.report_failure == failure


def _passed_check_for_profile(profile, *, omit=()):
    values = {
        "check_id": f"check-{profile}", "check_name": profile,
        "provenance_profile": profile, "status": "passed", "required": True,
        "started_at": NOW, "completed_at": NOW + timedelta(seconds=1),
        "evidence": [FixValidationEvidenceReference(
            evidence_id="evidence-1",
            kind="source_snapshot" if profile == "snapshot_integrity" else "tool_result",
            description="Recorded result",
        )],
    }
    if profile in {"source_analysis", "ai_assessment"}:
        values.update(tool_name="ruff" if profile == "source_analysis" else "model-x", tool_version="1.0")
    if profile == "source_analysis":
        values["configuration_sha256"] = "c" * 64
    for key in omit:
        values.pop(key, None)
    return values


@pytest.mark.parametrize("profile", ["source_analysis", "snapshot_integrity", "ai_assessment", "non_source"])
def test_each_provenance_profile_accepts_its_defined_metadata(profile):
    passed_check = FixValidationCheckResult(**_passed_check_for_profile(profile))
    snapshot = None if profile == "non_source" else FixValidationSnapshot(
        file_path="src/query.py", checked_head_sha="a" * 40,
        baseline_blob_sha="b" * 40, proposed_blob_sha="c" * 40,
    )
    report = FixValidationReport(
        validation_id="v", fix_id="f", attempt_number=1, status="passed", started_at=NOW,
        completed_at=NOW + timedelta(seconds=2), snapshot=snapshot, checks=[passed_check],
    )
    assert report.status == "passed"


@pytest.mark.parametrize(("profile", "missing"), [
    ("non_source", "evidence"),
    ("non_source", "started_at"),
    ("source_analysis", "tool_name"),
    ("source_analysis", "tool_version"),
    ("source_analysis", "configuration_sha256"),
    ("snapshot_integrity", "completed_at"),
    ("ai_assessment", "tool_name"),
    ("ai_assessment", "tool_version"),
])
def test_passed_check_rejects_missing_profile_required_provenance(profile, missing):
    with pytest.raises(ValidationError, match="missing required provenance"):
        FixValidationCheckResult(**_passed_check_for_profile(profile, omit=(missing,)))


@pytest.mark.parametrize(("profile", "allowed_kind"), [
    ("source_analysis", "tool_result"),
    ("snapshot_integrity", "source_snapshot"),
    ("ai_assessment", "tool_result"),
])
def test_passed_profile_requires_relevant_evidence_kind(profile, allowed_kind):
    values = _passed_check_for_profile(profile)
    values["evidence"] = [FixValidationEvidenceReference(
        evidence_id="evidence-1", kind="other", description="Unclassified artifact",
    )]
    with pytest.raises(ValidationError, match="evidence kind"):
        FixValidationCheckResult(**values)
    values["evidence"] = [FixValidationEvidenceReference(
        evidence_id="evidence-1", kind=allowed_kind, description="Relevant artifact",
    )]
    FixValidationCheckResult(**values)


@pytest.mark.parametrize("profile", ["source_analysis", "snapshot_integrity", "ai_assessment"])
def test_source_based_passed_check_requires_report_snapshot(profile):
    passed_check = FixValidationCheckResult(**_passed_check_for_profile(profile))
    with pytest.raises(ValidationError, match="require a report snapshot"):
        FixValidationReport(
            validation_id="v", fix_id="f", attempt_number=1, status="passed",
            started_at=NOW, completed_at=NOW + timedelta(seconds=2),
            checks=[passed_check],
        )


@pytest.mark.parametrize("missing_field", ["checked_head_sha", "baseline_blob_sha", "proposed_blob_sha"])
def test_source_based_report_rejects_each_missing_snapshot_identifier(missing_field):
    check_result = FixValidationCheckResult(**_passed_check_for_profile("source_analysis"))
    snapshot_values = {
        "file_path": "src/query.py", "checked_head_sha": "a" * 40,
        "baseline_blob_sha": "b" * 40, "proposed_blob_sha": "c" * 40,
    }
    snapshot_values[missing_field] = None
    with pytest.raises(ValidationError, match="complete snapshot identifiers"):
        FixValidationReport(
            validation_id="v", fix_id="f", attempt_number=1, status="passed",
            started_at=NOW, completed_at=NOW + timedelta(seconds=2),
            snapshot=FixValidationSnapshot(**snapshot_values), checks=[check_result],
        )


def test_validation_snapshot_must_match_suggested_fix_path():
    legacy_fix = {
        "fix_id": "fix-001", "canonical_issue_id": "issue-001", "file_path": "src/query.py",
        "start_line": 1, "end_line": 1, "language": "python", "base_commit_sha": "a" * 40,
        "expected_original_code": "old", "proposed_code": "new", "change_summary": "Change",
        "rationale": "Reason", "confidence": 0.8, "generation_status": "generated",
        "generated_at": NOW,
    }
    check_result = FixValidationCheckResult(**_passed_check_for_profile("snapshot_integrity"))
    report = FixValidationReport(
        validation_id="v", fix_id="fix-001", attempt_number=1, status="passed",
        started_at=NOW, completed_at=NOW + timedelta(seconds=2),
        snapshot=FixValidationSnapshot(
            file_path="src/other.py", checked_head_sha="a" * 40,
            baseline_blob_sha="b" * 40, proposed_blob_sha="c" * 40,
        ), checks=[check_result],
    )
    with pytest.raises(ValidationError, match="suggested fix file"):
        CanonicalIssue(
            issue_id="issue-001", file_path="src/query.py", category="bug", severity="high",
            normalized_description="Issue", suggested_fixes=[{**legacy_fix, "validation_reports": [report]}],
        )


def _v1_report_payload(*, declared_profile=None, complete_snapshot=False):
    legacy_payload = {
        "schema_version": "1", "validation_id": "old-validation", "fix_id": "fix-001",
        "attempt_number": 1, "status": "passed", "started_at": "2026-11-07T12:00:00Z",
        "snapshot": {"file_path": "src/query.py"},
        "checks": [{"check_id": "old-check", "check_name": "Old check", "status": "passed", "required": True}],
    }
    if declared_profile is not None:
        legacy_payload["checks"][0].update({
            "provenance_profile": declared_profile,
            "started_at": "2026-11-07T12:00:00Z",
            "completed_at": "2026-11-07T12:00:01Z",
            "tool_name": "ruff", "tool_version": "0.14", "configuration_sha256": "c" * 64,
            "evidence": [{"evidence_id": "old-evidence", "kind": "tool_result", "description": "Legacy result"}],
        })
    if complete_snapshot:
        legacy_payload["snapshot"].update({
            "checked_head_sha": "a" * 40,
            "baseline_blob_sha": "b" * 40,
            "proposed_blob_sha": "c" * 40,
        })
    return legacy_payload


def test_v1_report_without_profile_deserializes_as_explicit_legacy():
    restored = FixValidationReport.model_validate(_v1_report_payload())

    assert restored.schema_version == "1"
    assert restored.status == "passed"
    assert restored.checks[0].provenance_profile is None
    assert restored.checks[0].effective_provenance_profile == "legacy_unverified"


def test_v1_source_report_preserves_declared_profile_and_classifies_incomplete_snapshot_as_legacy():
    legacy_payload = _v1_report_payload(declared_profile="source_analysis")

    restored = FixValidationReport.model_validate(legacy_payload)

    assert restored.schema_version == "1"
    assert restored.status == "passed"  # Historical result remains unchanged.
    assert restored.checks[0].provenance_profile == "source_analysis"
    assert restored.checks[0].effective_provenance_profile == "legacy_unverified"

    serialized = restored.model_dump(mode="json")
    reloaded = FixValidationReport.model_validate(serialized)
    assert reloaded.status == "passed"
    assert reloaded.schema_version == "1"
    assert reloaded.checks[0].provenance_profile == "source_analysis"
    assert reloaded.checks[0].effective_provenance_profile == "legacy_unverified"


def test_v1_report_with_complete_historical_snapshot_remains_currently_classified():
    payload = _v1_report_payload(declared_profile="source_analysis", complete_snapshot=True)
    restored = FixValidationReport.model_validate(payload)

    assert restored.status == "passed"
    assert restored.checks[0].provenance_profile == "source_analysis"
    assert restored.checks[0].effective_provenance_profile == "source_analysis"
    serialized = restored.model_dump(mode="json")
    reloaded = FixValidationReport.model_validate(serialized)
    assert reloaded.schema_version == "1"
    assert reloaded.checks[0].provenance_profile == "source_analysis"
    assert reloaded.checks[0].effective_provenance_profile == "source_analysis"


def test_v2_report_requires_current_provenance_profile():
    payload = {
        "schema_version": "2", "validation_id": "new-validation", "fix_id": "fix-001",
        "attempt_number": 1, "status": "passed", "started_at": NOW,
        "completed_at": NOW + timedelta(seconds=2),
        "snapshot": {
            "file_path": "src/query.py", "checked_head_sha": "a" * 40,
            "baseline_blob_sha": "b" * 40, "proposed_blob_sha": "c" * 40,
        },
        "checks": [_passed_check_for_profile("source_analysis")],
    }
    payload["checks"][0].pop("provenance_profile")

    with pytest.raises(ValidationError, match="requires current declared and effective"):
        FixValidationReport.model_validate(payload)
    with pytest.raises(ValidationError, match="requires current declared and effective"):
        FixValidationReport(**payload)


def test_v2_report_cannot_explicitly_use_legacy_effective_profile():
    payload = {
        "schema_version": "2", "validation_id": "new-validation", "fix_id": "fix-001",
        "attempt_number": 1, "status": "not_run", "started_at": NOW,
        "checks": [{
            "check_id": "check", "check_name": "Check", "provenance_profile": "non_source",
            "effective_provenance_profile": "legacy_unverified", "status": "not_run",
        }],
    }
    with pytest.raises(ValidationError, match="requires current declared and effective"):
        FixValidationReport.model_validate(payload)


def test_ai_assessment_does_not_require_tool_configuration_digest():
    values = _passed_check_for_profile("ai_assessment")
    assert "configuration_sha256" not in values
    FixValidationCheckResult(**values)


def test_reports_are_additive_and_keep_lifecycle_states_independent():
    legacy_fix = SuggestedFix.model_validate({
        "fix_id": "fix-001", "canonical_issue_id": "issue-001", "file_path": "src/query.py",
        "start_line": 10, "end_line": 11, "language": "python", "base_commit_sha": "a" * 40,
        "expected_original_code": "old\ncode", "proposed_code": "new\ncode",
        "change_summary": "Change", "rationale": "Reason", "confidence": 0.8,
        "generation_status": "generated", "decision_status": "accepted", "generated_at": NOW,
    })
    assert legacy_fix.validation_reports == []
    report = FixValidationReport(
        validation_id="v1", fix_id="fix-001", attempt_number=1, status="failed", started_at=NOW,
        report_failure=FixValidationReportFailure(
            code="orchestration_error", status="failed", reason="The attempt could not complete.",
        ),
    )
    issue = CanonicalIssue(
        issue_id="issue-001", file_path="src/query.py", category="bug", severity="high",
        normalized_description="Potential defect", status="dismissed",
        suggested_fixes=[{**legacy_fix.model_dump(), "validation_reports": [report]}],
    )
    fix = issue.suggested_fixes[0]
    assert fix.generation_status == "generated"
    assert fix.decision_status == "accepted"
    assert issue.status == "dismissed"
    assert fix.validation_reports[0].status == "failed"


def test_report_must_reference_containing_fix_and_have_unique_attempts():
    report = FixValidationReport(
        validation_id="v1", fix_id="another-fix", attempt_number=1, status="not_run", started_at=NOW,
    )
    with pytest.raises(ValidationError, match="containing suggested fix"):
        CanonicalIssue(
            issue_id="issue-001", file_path="src/query.py", category="bug", severity="high",
            normalized_description="Potential defect", suggested_fixes=[{
                "fix_id": "fix-001", "canonical_issue_id": "issue-001", "file_path": "src/query.py",
                "start_line": 1, "end_line": 1, "language": "python", "base_commit_sha": "a" * 40,
                "expected_original_code": "old", "proposed_code": "new", "change_summary": "Change",
                "rationale": "Reason", "confidence": 0.8, "generation_status": "generated",
                "generated_at": NOW, "validation_reports": [report],
            }],
        )


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_validation_reports_round_trip_through_existing_review_stores(backend, tmp_path, monkeypatch):
    passed_check = FixValidationCheckResult(**_passed_check_for_profile("source_analysis"))
    passed_report = FixValidationReport(
        validation_id="validation-1", fix_id="fix-001", attempt_number=1,
        status="passed", started_at=NOW, completed_at=NOW + timedelta(seconds=2),
        snapshot=FixValidationSnapshot(
            file_path="src/query.py", checked_head_sha="a" * 40,
            baseline_blob_sha="b" * 40, proposed_blob_sha="c" * 40,
        ), checks=[passed_check],
    )
    failure_report = FixValidationReport(
        validation_id="validation-2", fix_id="fix-001", attempt_number=2,
        status="unavailable", started_at=NOW + timedelta(minutes=1),
        completed_at=NOW + timedelta(minutes=1, seconds=2),
        report_failure=FixValidationReportFailure(
            code="snapshot_retrieval_failed", status="unavailable", reason="The source snapshot was unavailable.",
        ),
    )
    legacy_payload = _v1_report_payload(declared_profile="source_analysis")
    legacy_payload.update(validation_id="validation-legacy", fix_id="fix-001", attempt_number=3)
    legacy_report = FixValidationReport.model_validate(legacy_payload)
    review = sample_review("owner", f"validation-{backend}", 42)
    review.canonical_issues = [CanonicalIssue(
        issue_id="issue-001", file_path="src/query.py", category="bug", severity="high",
        normalized_description="Potential defect", suggested_fixes=[{
            "fix_id": "fix-001", "canonical_issue_id": "issue-001", "file_path": "src/query.py",
            "start_line": 1, "end_line": 1, "language": "python", "base_commit_sha": "a" * 40,
            "expected_original_code": "old", "proposed_code": "new", "change_summary": "Change",
            "rationale": "Reason", "confidence": 0.8, "generation_status": "generated",
            "generated_at": NOW, "validation_reports": [passed_report, failure_report, legacy_report],
        }],
    )]
    record = review.model_dump(mode="json")
    if backend == "sqlite":
        monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "validation.sqlite3"))
        store = SQLiteReviewStore()
    else:
        store = MongoReviewStore(mongomock.MongoClient(tz_aware=True)["validation_test"])

    store.save_review(record)
    restored = CanonicalIssue.model_validate(store.get_review(record["review_id"])["canonical_issues"][0])

    reports = restored.suggested_fixes[0].validation_reports
    assert reports == [passed_report, failure_report, legacy_report]
    assert reports[0].snapshot.checked_head_sha == "a" * 40
    assert reports[0].snapshot.baseline_blob_sha == "b" * 40
    assert reports[0].snapshot.proposed_blob_sha == "c" * 40
    assert reports[0].checks[0].configuration_sha256 == "c" * 64
    assert reports[0].checks[0].evidence[0].evidence_id == "evidence-1"
    assert reports[1].report_failure.code == "snapshot_retrieval_failed"
    assert reports[2].schema_version == "1"
    assert reports[2].status == "passed"
    assert reports[2].checks[0].provenance_profile == "source_analysis"
    assert reports[2].checks[0].effective_provenance_profile == "legacy_unverified"
