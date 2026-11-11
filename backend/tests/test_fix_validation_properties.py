"""Finite property checks for the evidence aggregator's safety invariants."""

from itertools import product

from app.schemas.issue import FixValidationCheckResult, FixValidationCheckStatus
from app.services.fix_validation import aggregate_validation_status

CHECK_STATUSES: tuple[FixValidationCheckStatus, ...] = (
    "passed",
    "failed",
    "partial",
    "inconclusive",
    "unavailable",
    "not_run",
    "skipped",
)


def _check(
    index: int, status: FixValidationCheckStatus, required: bool
) -> FixValidationCheckResult:
    return FixValidationCheckResult(
        check_id=f"check-{index}",
        check_name=f"Check {index}",
        status=status,
        required=required,
    )


def test_aggregation_is_order_invariant_and_never_passes_incomplete_required_checks():
    """Exhaust all one- to three-check status/required combinations."""
    for count in range(1, 4):
        states = product(CHECK_STATUSES, (False, True))
        combinations = list(states)
        for combination in product(combinations, repeat=count):
            checks = [
                _check(index, status, required)
                for index, (status, required) in enumerate(combination)
            ]
            result = aggregate_validation_status(checks)
            required_checks = [check for check in checks if check.required]

            if result == "passed":
                assert required_checks
                assert all(check.status == "passed" for check in required_checks)
                assert all(check.status != "failed" for check in checks)

            reversed_result = aggregate_validation_status(list(reversed(checks)))
            assert result == reversed_result


def test_report_failure_cannot_promote_a_nonpassing_check_set():
    """Report-level failure states preserve the same no-false-pass property."""
    from app.schemas.issue import FixValidationReportFailure

    for status in ("failed", "unavailable", "inconclusive"):
        failure = FixValidationReportFailure(
            code="orchestration_failure",
            status=status,
            reason="The report-level operation did not complete.",
        )
        checks = [_check(0, "passed", True)]
        assert aggregate_validation_status(checks, report_failure=failure) != "passed"
