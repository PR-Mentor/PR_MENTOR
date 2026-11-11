"""Persist isolated execution evidence in its owning suggested-fix history."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast

from app.database.reviews import get_store
from app.schemas.issue import (
    FixValidationCheckResult,
    FixValidationCheckStatus,
    FixValidationEvidenceReference,
    FixValidationReport,
    FixValidationReportFailure,
    FixValidationSnapshot,
)
from app.services.fix_validation import aggregate_validation_status
from app.services.review_access import resolve_review_access
from app.test_execution.contracts import TestExecutionResult


def persist_execution_report(
    job_payload: dict[str, Any], execution: TestExecutionResult
) -> FixValidationReport | None:
    """Attach one result idempotently, refusing mismatched owner or snapshot data."""
    owner_id = job_payload.get("owner_id")
    review_id = job_payload.get("review_id")
    issue_id = job_payload.get("issue_id")
    fix_id = job_payload.get("fix_id")
    if not all(
        isinstance(value, str) and value
        for value in (owner_id, review_id, issue_id, fix_id)
    ):
        return None
    assert isinstance(owner_id, str)
    assert isinstance(review_id, str)
    assert isinstance(issue_id, str)
    assert isinstance(fix_id, str)
    if execution.review_id != review_id or execution.baseline_execution is None:
        return None

    store = get_store()
    review, access_role = resolve_review_access(review_id, owner_id, write=True)
    if access_role is None or access_role == "forbidden":
        return None
    if not isinstance(review, dict) or review.get("is_demo"):
        return None
    issue = next(
        (item for item in review.get("canonical_issues", []) if item.get("issue_id") == issue_id),
        None,
    )
    fix_data = next(
        (item for item in (issue or {}).get("suggested_fixes", []) if item.get("fix_id") == fix_id),
        None,
    )
    if fix_data is None:
        return None
    if issue is None:
        return None
    fix = next(
        (item for item in issue["suggested_fixes"] if item.get("fix_id") == fix_id),
        None,
    )
    if fix is None or execution.suggested_fix_id != fix_id or execution.canonical_issue_id != issue_id:
        return None
    owner = job_payload.get("owner")
    repo = job_payload.get("repo")
    if (
        not isinstance(owner, str)
        or not owner
        or not isinstance(repo, str)
        or not repo
        or execution.repository_id.casefold() != f"{owner}/{repo}".casefold()
    ):
        return None

    snapshot = FixValidationSnapshot.model_validate(job_payload["eligibility"]["snapshot"])
    if snapshot.file_path != fix.get("file_path") or any(
        getattr(snapshot, name) != getattr(execution, name)
        for name in ("base_commit_sha", "checked_head_sha", "baseline_blob_sha", "proposed_blob_sha")
    ):
        return None

    reports_data = fix.get("validation_reports", [])
    if not isinstance(reports_data, list):
        return None
    for record in reports_data:
        if isinstance(record, dict) and record.get("validation_id") == execution.execution_id:
            return FixValidationReport.model_validate(record)
    if len(reports_data) >= 20:
        return None

    previous = [FixValidationReport.model_validate(item) for item in reports_data]
    checks: list[FixValidationCheckResult] = []
    prior_checks_omitted = False
    for report in reversed(previous):
        if report.snapshot == snapshot:
            current_checks = [
                check for check in report.checks
                if check.provenance_profile not in {None, "legacy_unverified", "test_execution"}
                and check.effective_provenance_profile == check.provenance_profile
            ]
            checks.extend(current_checks)
            prior_checks_omitted = len(current_checks) != len(report.checks)
            break
    if len(checks) >= 12:
        prior_checks_omitted = True
        checks.clear()

    status_map = {
        "detected": "passed",
        "not_fixed": "failed",
        "regression_introduced": "failed",
        "not_reproduced": "inconclusive",
        "unavailable": "unavailable",
        "inconclusive": "inconclusive",
    }
    result_json = execution.model_dump(mode="json")
    result_digest = hashlib.sha256(
        json.dumps(result_json, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    configuration = {
        "image_digest": execution.execution_image_digest,
        "limits": execution.resource_limits.model_dump(mode="json"),
        "timeout_enforced": execution.timeout_enforced,
    }
    configuration_digest = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    check = FixValidationCheckResult(
        check_id=f"stage6-{execution.execution_id}",
        check_name=f"Baseline comparison for generated test {execution.test_id}",
        provenance_profile="test_execution",
        effective_provenance_profile="test_execution",
        status=cast(FixValidationCheckStatus, status_map[execution.regression_status]),
        required=True,
        tool_name="pytest",
        tool_version=execution.pytest_version or execution.runtime_version or execution.runtime_name,
        configuration_sha256=configuration_digest,
        started_at=(
            execution.baseline_execution.started_at
            if execution.baseline_execution is not None
            else execution.started_at
        ),
        completed_at=execution.completed_at,
        evidence=[
            FixValidationEvidenceReference(
                evidence_id=f"execution-result-{execution.execution_id}",
                kind="test_result",
                description=(
                    "Persisted baseline/proposed Stage 7 regression evidence "
                    f"({execution.regression_status}; proposed execution {execution.status})."
                ),
                file_path=fix["file_path"],
                sha256=result_digest,
            ),
            FixValidationEvidenceReference(
                evidence_id=f"execution-snapshot-{execution.execution_id}",
                kind="source_snapshot",
                description="Immutable source snapshot verified before isolated execution.",
                file_path=fix["file_path"],
            ),
        ],
        reason=execution.reason or execution.infrastructure_error,
        limitations=[
            "A generated test passing is evidence for that test only; it does not prove the suggested fix is correct."
        ],
    )
    if any(existing.check_id == check.check_id for existing in checks):
        return None
    checks.append(check)
    has_deterministic_source_check = any(
        item.check_id != check.check_id
        and item.required
        and item.provenance_profile in {"source_analysis", "snapshot_integrity"}
        for item in checks
    )
    report_failure = None
    if execution.regression_status == "detected" and not has_deterministic_source_check:
        report_failure = FixValidationReportFailure(
            code="source_validation_not_included",
            status="inconclusive",
            reason="The generated test distinguished baseline from proposed source, but no same-snapshot deterministic source validation was available to support a report-level pass.",
        )
    now = datetime.now(UTC)
    report = FixValidationReport(
        schema_version="2",
        validation_id=execution.execution_id,
        fix_id=fix_id,
        attempt_number=max((item.attempt_number for item in previous), default=0) + 1,
        trigger="manual",
        status=aggregate_validation_status(checks, report_failure=report_failure),
        started_at=min((item.started_at for item in checks if item.started_at), default=execution.started_at),
        completed_at=now,
        snapshot=snapshot,
        checks=checks,
        report_failure=report_failure,
        limitations=[
            "The report includes only deterministic checks from the same source snapshot and this execution attempt."
        ] + ([
            "Some earlier same-snapshot checks were omitted because they lacked current provenance or the per-report check limit was reached."
        ] if prior_checks_omitted else []),
    )
    fix.setdefault("validation_reports", []).append(report.model_dump(mode="json"))
    store.save_review(review, owner_id=owner_id)
    return report
