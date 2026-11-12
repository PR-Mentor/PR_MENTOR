"""Stage 4 analyzers layered on the Stage 3 snapshot validation service."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.schemas.issue import (
    FixValidationCheckResult,
    FixValidationCheckStatus,
    FixValidationEvidenceReference,
    FixValidationFindingCounts,
    FixValidationReport,
    SuggestedFix,
)
from app.services.fix_eligibility import FixEligibilityResult
from app.services.fix_validation import aggregate_validation_status
from app.verification.analyzer_common import (
    MAX_ANALYZER_TIMEOUT_SECONDS,
    MAX_TOOL_OUTPUT_BYTES,
    SourceToolRun,
    config_digest,
)
from app.verification.bounded_process import BoundedProcessError
from app.verification.finding_comparison import FindingDelta, compare_findings
from app.verification.mypy_runner import mypy_configuration, run_mypy_source
from app.verification.ruff_runner import ToolExecutionError
from app.verification.source_analysis import (
    StaticValidationOutcome,
    validate_static_fix,
)

STAGE4_ENGINE_VERSION = "phase4-stage4/1"
MAX_ADDITIONAL_EVIDENCE = 28


@dataclass(frozen=True)
class AnalyzerRegistration:
    """Small registration record for a source analyzer with the shared runner API."""

    name: str
    check_id: str
    supported_languages: tuple[str, ...]
    source_context: str
    runner: Callable[..., SourceToolRun]


STAGE4_ANALYZERS = (
    AnalyzerRegistration(
        name="mypy",
        check_id="mypy-comparison",
        supported_languages=("python",),
        source_context="verified-single-file-snapshot",
        runner=run_mypy_source,
    ),
)


def validate_static_fix_with_quality(
    fix: SuggestedFix,
    eligibility: FixEligibilityResult,
    *,
    attempt_number: int | None = None,
    validation_id: str | None = None,
    timeout: float = MAX_ANALYZER_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> StaticValidationOutcome:
    """Run approved Stage 3 checks, then append independent Stage 4 checks.

    Type analysis only receives the two exact source strings verified by
    Stage 3. It does not import or install dependencies from the repository.
    """
    outcome = validate_static_fix(
        fix,
        eligibility,
        attempt_number=attempt_number,
        validation_id=validation_id,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )
    checks = list(outcome.report.checks)
    comparisons = dict(outcome.comparisons)
    results = dict(outcome.tool_results)
    payloads = dict(outcome.evidence_payloads)

    if outcome.report.report_failure is not None or outcome.report.snapshot is None:
        not_run = _not_run_check()
        checks.append(not_run)
        results["mypy"] = {
            "status": "not_run",
            "reason": "Stage 3 did not establish an eligible source snapshot.",
            "tool_name": "mypy",
            "configuration_sha256": config_digest(mypy_configuration()),
        }
        comparisons["mypy"] = ()
    else:
        details, check, deltas, evidence = _analyze_mypy_pair(
            eligibility.baseline_source or "",
            eligibility.proposed_source or "",
            fix.file_path,
            outcome.report.snapshot.model_dump(mode="json"),
            timeout,
            max_output_bytes,
        )
        checks.append(check)
        results["mypy"] = details
        comparisons["mypy"] = tuple(deltas)
        payloads.update(evidence)

    report_data = outcome.report.model_dump(mode="python")
    report_data.update(
        checks=checks,
        status=aggregate_validation_status(
            checks, report_failure=outcome.report.report_failure
        ),
        engine_version=STAGE4_ENGINE_VERSION,
    )
    report = FixValidationReport.model_validate(report_data)
    return StaticValidationOutcome(report, comparisons, results, payloads)


def _analyze_mypy_pair(
    baseline_source: str,
    proposed_source: str,
    file_path: str,
    snapshot: dict,
    timeout: float,
    max_output_bytes: int,
) -> tuple[dict, FixValidationCheckResult, list[FindingDelta], dict[str, bytes]]:
    started = datetime.now(UTC)
    baseline: SourceToolRun | None = None
    proposed: SourceToolRun | None = None
    try:
        baseline = run_mypy_source(
            baseline_source,
            file_path,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
        proposed = run_mypy_source(
            proposed_source,
            file_path,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
        if (
            baseline.tool_version != proposed.tool_version
            or baseline.configuration_sha256 != proposed.configuration_sha256
        ):
            return _failed_pair(
                "inconclusive",
                "Baseline and proposed analyses used different mypy versions or configurations.",
                started,
                snapshot,
                baseline,
                proposed,
            )

        deltas = compare_findings(list(baseline.findings), list(proposed.findings))
        incomplete = any(
            item.get("analysis_incomplete")
            for run in (baseline, proposed)
            for item in run.findings
        )
        introduced_errors = any(
            delta.category == "introduced"
            and delta.proposed is not None
            and delta.proposed.get("severity") == "error"
            and not delta.proposed.get("analysis_incomplete")
            for delta in deltas
        )
        introduced_warnings = any(
            delta.category == "introduced"
            and delta.proposed is not None
            and delta.proposed.get("severity") == "warning"
            for delta in deltas
        )
        uncertain = any(delta.category in {"modified", "uncertain"} for delta in deltas)
        if incomplete or uncertain or introduced_warnings:
            status: FixValidationCheckStatus = "inconclusive"
            reason = (
                "Type analysis is incomplete because imports or stubs are unavailable."
                if incomplete
                else "Some type diagnostics could not be classified conservatively."
            )
        elif introduced_errors:
            status = "failed"
            reason = "The proposed source introduces a new mypy type diagnostic."
        else:
            status = "passed"
            reason = (
                "No new mypy type diagnostics were detected in the available context."
            )

        counts = _finding_counts(baseline.findings, proposed.findings, deltas)
        references, evidence_payloads = _evidence("mypy", baseline, proposed, deltas)
        check = FixValidationCheckResult(
            check_id="mypy-comparison",
            check_name="mypy baseline/proposed type analysis",
            provenance_profile="source_analysis",
            effective_provenance_profile="source_analysis",
            status=status,
            required=True,
            tool_name="mypy",
            tool_version=baseline.tool_version,
            configuration_sha256=baseline.configuration_sha256,
            started_at=_timestamp(baseline.started_at),
            completed_at=_timestamp(proposed.completed_at),
            finding_counts=counts,
            evidence=references,
            reason=reason,
            limitations=[
                "Analysis uses one verified source file without repository or third-party import context.",
                "Missing imports and type stubs make the result inconclusive; no dependencies are installed.",
            ],
        )
        return (
            {
                "status": status,
                "tool_name": "mypy",
                "tool_version": baseline.tool_version,
                "configuration_sha256": baseline.configuration_sha256,
                "snapshot": snapshot,
                "baseline_findings": list(baseline.findings),
                "proposed_findings": list(proposed.findings),
                "deltas": [delta.model_data() for delta in deltas],
                "baseline_elapsed_seconds": baseline.elapsed_seconds,
                "proposed_elapsed_seconds": proposed.elapsed_seconds,
                "baseline_stdout_sha256": _sha256(baseline.stdout),
                "proposed_stdout_sha256": _sha256(proposed.stdout),
                "limitations": check.limitations,
            },
            check,
            deltas,
            evidence_payloads,
        )
    except BoundedProcessError as exc:
        status = "inconclusive" if exc.inconclusive else "unavailable"
        return _failed_pair(status, str(exc), started, snapshot, baseline, proposed)
    except (ToolExecutionError, OSError, ValueError, UnicodeError) as exc:
        status = (
            "inconclusive"
            if isinstance(exc, (ToolExecutionError, ValueError, UnicodeError))
            else "unavailable"
        )
        return _failed_pair(status, str(exc), started, snapshot, baseline, proposed)
    except Exception:  # noqa: BLE001 - analyzer failure must not discard Stage 3 evidence
        return _failed_pair(
            "inconclusive",
            "Unexpected mypy failure; raw execution details were suppressed.",
            started,
            snapshot,
            baseline,
            proposed,
        )


def _failed_pair(
    status: FixValidationCheckStatus,
    reason: str,
    started: datetime,
    snapshot: dict,
    baseline: SourceToolRun | None,
    proposed: SourceToolRun | None,
) -> tuple[dict, FixValidationCheckResult, list[FindingDelta], dict[str, bytes]]:
    evidence: dict[str, bytes] = {}
    references: list[FixValidationEvidenceReference] = []
    for label, run in (("baseline", baseline), ("proposed", proposed)):
        if run is None:
            continue
        for stream, body in (("stdout", run.stdout), ("stderr", run.stderr)):
            digest = _sha256(body)
            evidence_id = f"mypy-{label}-{stream}-{digest[:16]}"
            evidence[evidence_id] = body
            references.append(
                FixValidationEvidenceReference(
                    evidence_id=evidence_id,
                    kind="tool_result",
                    description=f"mypy {label} {stream} output digest.",
                    file_path=snapshot["file_path"],
                    sha256=digest,
                )
            )
    version = (
        baseline.tool_version
        if baseline
        else (proposed.tool_version if proposed else None)
    )
    config = (
        baseline.configuration_sha256
        if baseline
        else (
            proposed.configuration_sha256
            if proposed
            else config_digest(mypy_configuration())
        )
    )
    completed = datetime.now(UTC)
    check = FixValidationCheckResult(
        check_id="mypy-comparison",
        check_name="mypy baseline/proposed type analysis",
        provenance_profile="source_analysis",
        effective_provenance_profile="source_analysis",
        status=status,
        required=True,
        tool_name="mypy",
        tool_version=version,
        configuration_sha256=config,
        started_at=started,
        completed_at=completed,
        evidence=references,
        reason=reason[:1_000],
        limitations=["A complete, comparable mypy result is unavailable."],
    )
    return (
        {
            "status": status,
            "tool_name": "mypy",
            "tool_version": version,
            "configuration_sha256": config,
            "snapshot": snapshot,
            "reason": reason[:1_000],
            "baseline_findings": list(baseline.findings) if baseline else None,
            "proposed_findings": list(proposed.findings) if proposed else None,
        },
        check,
        [],
        evidence,
    )


def _not_run_check() -> FixValidationCheckResult:
    now = datetime.now(UTC)
    return FixValidationCheckResult(
        check_id="mypy-comparison",
        check_name="mypy baseline/proposed type analysis",
        provenance_profile="source_analysis",
        effective_provenance_profile="source_analysis",
        status="not_run",
        required=True,
        tool_name="mypy",
        configuration_sha256=config_digest(mypy_configuration()),
        started_at=now,
        completed_at=now,
        reason="Stage 3 did not establish an eligible source snapshot.",
        limitations=[
            "The analyzer did not execute because source eligibility was not established."
        ],
    )


def _finding_counts(
    baseline: tuple[dict, ...], proposed: tuple[dict, ...], deltas: list[FindingDelta]
) -> FixValidationFindingCounts | None:
    categories = Counter(delta.category for delta in deltas)
    if categories["uncertain"]:
        return None
    modified = categories["modified"]
    return FixValidationFindingCounts(
        baseline_count=len(baseline),
        proposed_count=len(proposed),
        introduced_count=categories["introduced"] + modified,
        resolved_count=categories["resolved"] + modified,
        unchanged_count=categories["unchanged"],
    )


def _evidence(
    tool: str,
    baseline: SourceToolRun,
    proposed: SourceToolRun,
    deltas: list[FindingDelta],
) -> tuple[list[FixValidationEvidenceReference], dict[str, bytes]]:
    payloads: dict[str, bytes] = {}
    references: list[FixValidationEvidenceReference] = []
    for label, run in (("baseline", baseline), ("proposed", proposed)):
        for stream, body in (("stdout", run.stdout), ("stderr", run.stderr)):
            digest = _sha256(body)
            evidence_id = f"{tool}-{label}-{stream}-{digest[:16]}"
            payloads[evidence_id] = body
            references.append(
                FixValidationEvidenceReference(
                    evidence_id=evidence_id,
                    kind="tool_result",
                    description=f"{tool} {label} {stream} output digest.",
                    file_path=run.findings[0]["file_path"] if run.findings else None,
                    sha256=digest,
                )
            )
    for delta in deltas[: MAX_ADDITIONAL_EVIDENCE - len(references)]:
        body = json.dumps(
            delta.model_data(), sort_keys=True, separators=(",", ":")
        ).encode()
        digest = _sha256(body)
        payloads[delta.delta_id] = body
        item = delta.proposed or delta.baseline or {}
        references.append(
            FixValidationEvidenceReference(
                evidence_id=delta.delta_id,
                kind="finding",
                description=f"mypy diagnostic comparison: {delta.category}.",
                file_path=item.get("file_path"),
                line_start=item.get("line"),
                line_end=item.get("end_line"),
                sha256=digest,
            )
        )
    return references, payloads


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
