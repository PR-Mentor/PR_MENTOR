"""Pure aggregation for evidence attached to suggested-fix validation attempts.

This module has no GitHub, LLM, filesystem, subprocess, or persistence behavior.
It only maps already-recorded check outcomes to the report-level status.
"""

from collections.abc import Sequence

from app.schemas.issue import (
    FixValidationCheckResult,
    FixValidationReportFailure,
    FixValidationStatus,
)


def aggregate_validation_status(
    checks: Sequence[FixValidationCheckResult],
    *,
    report_failure: FixValidationReportFailure | None = None,
) -> FixValidationStatus:
    """Return the deterministic report status for a set of check results.

    Semantics:
    - ``failed``: any executed check reported a failure. Optional checks cannot
      erase a deterministic failure from the report.
    - ``passed``: at least one required check exists and every required check
      passed. Optional checks may be unavailable or skipped; their limitations
      remain visible on the individual check result.
    - ``inconclusive``: a required check ran but could not reach a conclusion.
    - ``partial``: a required check passed or returned a partial result, while
      another required check remains incomplete; an unavailable report-level
      failure also produces partial when required checks already passed.
    - ``unavailable``: required checks could not run, with no required check
      passing or producing an inconclusive result.
    - ``not_run``: there are no required checks, or all required checks were
      deliberately skipped/not run. This never means the fix passed.
    """
    if any(check.status == "failed" for check in checks):
        return "failed"
    if report_failure is not None and report_failure.status == "failed":
        return "failed"

    if report_failure is not None and report_failure.status == "inconclusive":
        return "inconclusive"

    required = [check for check in checks if check.required]
    if not required:
        return "unavailable" if report_failure is not None else "not_run"

    statuses = [check.status for check in required]
    if "inconclusive" in statuses:
        return "inconclusive"
    if report_failure is not None and report_failure.status == "unavailable":
        if any(status in {"passed", "partial"} for status in statuses):
            return "partial"
        return "unavailable"
    if all(status == "passed" for status in statuses):
        return "passed"
    if any(status in {"passed", "partial"} for status in statuses):
        return "partial"
    if all(status in {"not_run", "skipped"} for status in statuses):
        return "not_run"
    if "unavailable" in statuses:
        return "unavailable"
    # A required check with a partial result but no passed sibling still has
    # useful evidence, but is not a complete pass.
    if "partial" in statuses:
        return "partial"
    return "inconclusive"
