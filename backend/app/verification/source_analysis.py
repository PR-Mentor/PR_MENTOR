"""Stage 3 baseline/proposed Ruff, Bandit, and Python syntax analysis."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.github_service.pull_requests import (
    MAX_SOURCE_BYTES,
    GitHubFileSnapshotError,
    git_blob_sha,
    validate_repository_python_path,
)
from app.schemas.issue import (
    FixValidationCheckResult,
    FixValidationCheckStatus,
    FixValidationEvidenceReference,
    FixValidationFindingCounts,
    FixValidationReport,
    FixValidationReportFailure,
    FixValidationReportFailureStatus,
    SuggestedFix,
)
from app.services.fix_eligibility import FixEligibilityResult
from app.services.fix_validation import aggregate_validation_status
from app.verification.analyzer_common import (
    MAX_ANALYZER_TIMEOUT_SECONDS,
    SourceToolRun,
    config_digest,
)
from app.verification.bandit_runner import run_bandit_source
from app.verification.bounded_process import BoundedProcessError
from app.verification.diff_mapping import added_lines_by_file
from app.verification.finding_comparison import FindingDelta, compare_findings
from app.verification.ruff_runner import ToolExecutionError, run_ruff_source

MAX_VALIDATION_EVIDENCE = 30
MAX_DELTA_EVIDENCE = 26


@dataclass(frozen=True)
class SyntaxRun:
    findings: tuple[dict[str, Any], ...]
    started_at: datetime
    completed_at: datetime
    version: str
    configuration_sha256: str


@dataclass(frozen=True)
class StaticValidationOutcome:
    report: FixValidationReport
    comparisons: dict[str, tuple[FindingDelta, ...]]
    tool_results: dict[str, dict[str, Any]]
    evidence_payloads: dict[str, bytes]


def validate_static_fix(
    fix: SuggestedFix,
    eligibility: FixEligibilityResult,
    *,
    attempt_number: int | None = None,
    validation_id: str | None = None,
    timeout: float = MAX_ANALYZER_TIMEOUT_SECONDS,
    max_output_bytes: int = 1_000_000,
) -> StaticValidationOutcome:
    """Analyze only a Stage 2 verified baseline/proposed source pair.

    No code is imported or executed. Tool failures remain isolated to their
    checks. The report is validated against the approved Stage 1 contract.
    """
    started = datetime.now(UTC)
    report_id = validation_id or uuid4().hex
    if attempt_number is None:
        attempt_number = (
            max(
                (report.attempt_number for report in fix.validation_reports),
                default=0,
            )
            + 1
        )
    if eligibility.status != "eligible":
        return _report_failure(
            fix,
            report_id,
            attempt_number,
            started,
            "fix_not_eligible",
            "unavailable",
            eligibility.reason,
        )
    snapshot = eligibility.snapshot
    if (
        snapshot is None
        or not eligibility.baseline_source
        or eligibility.proposed_source is None
        or snapshot.file_path != fix.file_path
    ):
        return _report_failure(
            fix,
            report_id,
            attempt_number,
            started,
            "verified_source_missing",
            "inconclusive",
            "Stage 2 did not provide both verified source versions and their snapshot.",
        )

    baseline_blob_sha = snapshot.baseline_blob_sha
    proposed_blob_sha = snapshot.proposed_blob_sha
    if (
        not isinstance(baseline_blob_sha, str)
        or not isinstance(proposed_blob_sha, str)
        or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", baseline_blob_sha)
        or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", proposed_blob_sha)
    ):
        return _report_failure(
            fix,
            report_id,
            attempt_number,
            started,
            "snapshot_hash_missing",
            "inconclusive",
            "Stage 2 snapshot is missing a full baseline or proposed blob identity.",
        )

    try:
        validate_repository_python_path(fix.file_path)
        baseline_bytes = _validated_source(eligibility.baseline_source)
        proposed_bytes = _validated_source(eligibility.proposed_source)
        reconstructed = _reconstruct_from_stage2(fix, eligibility.baseline_source)
        if reconstructed != eligibility.proposed_source:
            raise GitHubFileSnapshotError(
                "Stage 2 proposed source differs from independent in-memory reconstruction."
            )
        if (
            git_blob_sha(baseline_bytes).casefold()
            != baseline_blob_sha.casefold()
        ):
            raise GitHubFileSnapshotError(
                "Baseline source does not match its Stage 2 blob identity."
            )
        if (
            git_blob_sha(proposed_bytes).casefold()
            != proposed_blob_sha.casefold()
        ):
            raise GitHubFileSnapshotError(
                "Proposed source does not match its Stage 2 blob identity."
            )
    except (
        GitHubFileSnapshotError,
        AttributeError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        return _report_failure(
            fix,
            report_id,
            attempt_number,
            started,
            "snapshot_integrity_mismatch",
            "inconclusive",
            str(exc),
            snapshot=snapshot,
        )

    added_lines, diff_limitations = _changed_lines(
        fix.file_path, eligibility.baseline_source, eligibility.proposed_source
    )
    checks: list[FixValidationCheckResult] = []
    comparisons: dict[str, tuple[FindingDelta, ...]] = {}
    tool_results: dict[str, dict[str, Any]] = {}
    evidence_payloads: dict[str, bytes] = {}

    for tool_name, runner in (("ruff", run_ruff_source), ("bandit", run_bandit_source)):
        tool_result, check, deltas = _analyze_tool_pair(
            tool_name,
            runner,
            fix.file_path,
            eligibility.baseline_source,
            eligibility.proposed_source,
            snapshot,
            started,
            timeout,
            max_output_bytes,
            added_lines,
            diff_limitations,
        )
        checks.append(check)
        tool_results[tool_name] = tool_result
        comparisons[tool_name] = tuple(deltas)
        evidence_payloads.update(tool_result.get("evidence_payloads", {}))

    syntax_started = datetime.now(UTC)
    baseline_syntax = _parse_python(fix.file_path, eligibility.baseline_source)
    proposed_syntax = _parse_python(fix.file_path, eligibility.proposed_source)
    syntax_completed = datetime.now(UTC)
    syntax_deltas = compare_findings(
        list(baseline_syntax.findings), list(proposed_syntax.findings)
    )
    syntax_check, syntax_details, syntax_payloads = _syntax_check(
        fix.file_path,
        baseline_syntax,
        proposed_syntax,
        syntax_deltas,
        syntax_started,
        syntax_completed,
        added_lines,
        diff_limitations,
    )
    checks.append(syntax_check)
    comparisons["python_ast"] = tuple(syntax_deltas)
    tool_results["python_ast"] = syntax_details
    evidence_payloads.update(syntax_payloads)

    completed = datetime.now(UTC)
    report = FixValidationReport(
        schema_version="2",
        validation_id=report_id,
        fix_id=fix.fix_id,
        attempt_number=attempt_number,
        trigger="manual",
        status=aggregate_validation_status(checks),
        engine_version="phase4-stage3/1",
        started_at=started,
        completed_at=completed,
        snapshot=snapshot,
        checks=checks,
        limitations=_bounded_limitations(diff_limitations),
    )
    return StaticValidationOutcome(report, comparisons, tool_results, evidence_payloads)


def _analyze_tool_pair(
    tool_name: str,
    runner,
    file_path: str,
    baseline_source: str,
    proposed_source: str,
    snapshot,
    report_started: datetime,
    timeout: float,
    max_output_bytes: int,
    added_lines: set[int] | None,
    diff_limitations: list[str],
) -> tuple[dict[str, Any], FixValidationCheckResult, list[FindingDelta]]:
    check_started = datetime.now(UTC)
    baseline: SourceToolRun | None = None
    proposed: SourceToolRun | None = None
    try:
        baseline = runner(
            baseline_source,
            file_path,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
        proposed = runner(
            proposed_source,
            file_path,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
        if (
            baseline.configuration_sha256 != proposed.configuration_sha256
            or baseline.tool_version != proposed.tool_version
        ):
            return _tool_failure(
                tool_name,
                "inconclusive",
                "Baseline and proposed analyses used different tool versions or configurations.",
                check_started,
                datetime.now(UTC),
                snapshot,
                baseline=baseline,
                proposed=proposed,
            )
        deltas = compare_findings(list(baseline.findings), list(proposed.findings))
        attribution = [
            _with_attribution(delta, added_lines, diff_limitations) for delta in deltas
        ]
        status, reason, counts = _comparison_status(
            baseline.findings, proposed.findings, deltas
        )
        evidence, payloads = _tool_evidence(tool_name, baseline, proposed, attribution)
        limitations = _bounded_limitations(diff_limitations)
        check = FixValidationCheckResult(
            check_id=f"{tool_name}-comparison",
            check_name=f"{tool_name.title()} baseline/proposed comparison",
            provenance_profile="source_analysis",
            effective_provenance_profile="source_analysis",
            status=status,
            required=True,
            tool_name=tool_name,
            tool_version=baseline.tool_version,
            configuration_sha256=baseline.configuration_sha256,
            started_at=_datetime(baseline.started_at),
            completed_at=_datetime(proposed.completed_at),
            finding_counts=counts,
            evidence=evidence,
            reason=reason,
            limitations=limitations,
        )
        result = {
            "status": "completed",
            "tool_name": tool_name,
            "tool_version": baseline.tool_version,
            "configuration_sha256": baseline.configuration_sha256,
            "snapshot": snapshot.model_dump(mode="json"),
            "baseline_findings": list(baseline.findings),
            "proposed_findings": list(proposed.findings),
            "deltas": [item.model_data() for item in attribution],
            "baseline_stdout_sha256": _sha256(baseline.stdout),
            "proposed_stdout_sha256": _sha256(proposed.stdout),
            "baseline_stderr_sha256": _sha256(baseline.stderr),
            "proposed_stderr_sha256": _sha256(proposed.stderr),
            "baseline_elapsed_seconds": baseline.elapsed_seconds,
            "proposed_elapsed_seconds": proposed.elapsed_seconds,
            "evidence_payloads": payloads,
        }
        return result, check, deltas
    except BoundedProcessError as exc:
        status = "inconclusive" if exc.inconclusive else "unavailable"
        return _tool_failure(
            tool_name,
            status,
            str(exc),
            check_started,
            datetime.now(UTC),
            snapshot,
            baseline=baseline,
            proposed=proposed,
        )
    except (ToolExecutionError, OSError, ValueError, UnicodeError) as exc:
        return _tool_failure(
            tool_name,
            "inconclusive"
            if isinstance(exc, (ToolExecutionError, ValueError, UnicodeError))
            else "unavailable",
            str(exc),
            check_started,
            datetime.now(UTC),
            snapshot,
            baseline=baseline,
            proposed=proposed,
        )
    except Exception:  # noqa: BLE001 - isolate any analyzer failure from sibling checks
        return _tool_failure(
            tool_name,
            "inconclusive",
            "Unexpected analyzer failure; raw execution details were suppressed.",
            check_started,
            datetime.now(UTC),
            snapshot,
            baseline=baseline,
            proposed=proposed,
        )


def _tool_failure(
    tool_name: str,
    status: FixValidationCheckStatus,
    reason: str,
    started_at: datetime,
    completed_at: datetime,
    snapshot,
    *,
    baseline: SourceToolRun | None = None,
    proposed: SourceToolRun | None = None,
) -> tuple[dict[str, Any], FixValidationCheckResult, list[FindingDelta]]:
    evidence: list[FixValidationEvidenceReference] = []
    payloads: dict[str, bytes] = {}
    for label, run in (("baseline", baseline), ("proposed", proposed)):
        if run is None:
            continue
        for stream_name, body in (("stdout", run.stdout), ("stderr", run.stderr)):
            payload_id = f"{tool_name}-{label}-{stream_name}-{_sha256(body)[:16]}"
            payloads[payload_id] = body
            evidence.append(
                FixValidationEvidenceReference(
                    evidence_id=payload_id,
                    kind="tool_result",
                    description=f"{tool_name.title()} {label} {stream_name} digest.",
                    file_path=snapshot.file_path,
                    sha256=_sha256(body),
                )
            )
    tool_version_value = (
        baseline.tool_version
        if baseline
        else (proposed.tool_version if proposed else None)
    )
    config_value = (
        baseline.configuration_sha256
        if baseline
        else (proposed.configuration_sha256 if proposed else None)
    )
    check = FixValidationCheckResult(
        check_id=f"{tool_name}-comparison",
        check_name=f"{tool_name.title()} baseline/proposed comparison",
        provenance_profile="source_analysis",
        effective_provenance_profile="source_analysis",
        status=status,
        required=True,
        tool_name=tool_name,
        tool_version=tool_version_value,
        configuration_sha256=config_value,
        started_at=started_at,
        completed_at=completed_at,
        evidence=evidence,
        reason=reason[:1_000],
        limitations=["A complete, comparable finding set is unavailable."],
    )
    return (
        {
            "status": status,
            "tool_name": tool_name,
            "reason": reason,
            "snapshot": snapshot.model_dump(mode="json"),
            "tool_version": tool_version_value,
            "configuration_sha256": config_value,
            "baseline_findings": list(baseline.findings) if baseline else None,
            "proposed_findings": list(proposed.findings) if proposed else None,
            "baseline_stdout_sha256": _sha256(baseline.stdout) if baseline else None,
            "proposed_stdout_sha256": _sha256(proposed.stdout) if proposed else None,
            "evidence_payloads": payloads,
        },
        check,
        [],
    )


def _comparison_status(
    baseline: tuple[dict[str, Any], ...],
    proposed: tuple[dict[str, Any], ...],
    deltas: list[FindingDelta],
) -> tuple[FixValidationCheckStatus, str, FixValidationFindingCounts | None]:
    categories = Counter(item.category for item in deltas)
    has_uncertain = categories["uncertain"] > 0
    modified = categories["modified"]
    counts = (
        None
        if has_uncertain
        else FixValidationFindingCounts(
            baseline_count=len(baseline),
            proposed_count=len(proposed),
            introduced_count=categories["introduced"] + modified,
            resolved_count=categories["resolved"] + modified,
            unchanged_count=categories["unchanged"],
        )
    )
    if categories["introduced"]:
        return (
            "failed",
            "The proposed source contains newly detected analyzer findings.",
            counts,
        )
    if modified or has_uncertain:
        return (
            "inconclusive",
            "Some baseline/proposed finding relationships could not be treated as unchanged.",
            counts,
        )
    return (
        "passed",
        "No newly introduced findings were detected by this analyzer.",
        counts,
    )


def _tool_evidence(
    tool_name: str,
    baseline: SourceToolRun,
    proposed: SourceToolRun,
    deltas: list[FindingDelta],
) -> tuple[list[FixValidationEvidenceReference], dict[str, bytes]]:
    evidence: list[FixValidationEvidenceReference] = []
    payloads: dict[str, bytes] = {}
    for label, run in (("baseline", baseline), ("proposed", proposed)):
        for stream_name, body in (("stdout", run.stdout), ("stderr", run.stderr)):
            digest = _sha256(body)
            evidence_id = f"{tool_name}-{label}-{stream_name}-{digest[:16]}"
            payloads[evidence_id] = body
            evidence.append(
                FixValidationEvidenceReference(
                    evidence_id=evidence_id,
                    kind="tool_result",
                    description=f"{tool_name.title()} {label} {stream_name} output digest.",
                    file_path=run.findings[0]["file_path"] if run.findings else None,
                    sha256=digest,
                )
            )
    for delta in deltas[:MAX_DELTA_EVIDENCE]:
        value = delta.model_data()
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        digest = _sha256(body)
        payloads[delta.delta_id] = body
        item = delta.proposed or delta.baseline or {}
        evidence.append(
            FixValidationEvidenceReference(
                evidence_id=delta.delta_id,
                kind="finding",
                description=f"{tool_name.title()} finding comparison: {delta.category}.",
                file_path=item.get("file_path"),
                line_start=item.get("line"),
                line_end=item.get("end_line"),
                sha256=digest,
            )
        )
    return evidence[:MAX_VALIDATION_EVIDENCE], payloads


def _parse_python(file_path: str, source: str) -> SyntaxRun:
    started = datetime.now(UTC)
    findings: tuple[dict[str, Any], ...] = ()
    try:
        ast.parse(source, filename=file_path, mode="exec", type_comments=True)
    except SyntaxError as exc:
        line_text = exc.text.rstrip("\r\n") if isinstance(exc.text, str) else None
        findings = (
            {
                "tool": "python_ast",
                "file_path": file_path,
                "line": exc.lineno,
                "column": exc.offset,
                "end_line": exc.end_lineno,
                "end_column": exc.end_offset,
                "rule_id": "PYTHON_SYNTAX",
                "message": exc.msg or "Invalid Python syntax.",
                "severity": "error",
                "confidence": None,
                "test_name": None,
                "evidence": line_text,
                "context": line_text,
                "source": "python_ast",
            },
        )
    completed = datetime.now(UTC)
    return SyntaxRun(
        findings=findings,
        started_at=started,
        completed_at=completed,
        version=f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        configuration_sha256=config_digest(
            {
                "tool": "python-ast",
                "mode": "exec",
                "type_comments": True,
                "check": "syntax-parse-only",
            }
        ),
    )


def _syntax_check(
    file_path: str,
    baseline: SyntaxRun,
    proposed: SyntaxRun,
    deltas: list[FindingDelta],
    started_at: datetime,
    completed_at: datetime,
    added_lines: set[int] | None,
    diff_limitations: list[str],
) -> tuple[FixValidationCheckResult, dict[str, Any], dict[str, bytes]]:
    status: FixValidationCheckStatus = "failed" if proposed.findings else "passed"
    reason = (
        "Proposed source contains a Python syntax error."
        if proposed.findings
        else "Proposed source parses as Python without executing repository code."
    )
    counts = _counts_for_structural(baseline.findings, proposed.findings, deltas)
    evidence: list[FixValidationEvidenceReference] = []
    payloads: dict[str, bytes] = {}
    for label, run in (("baseline", baseline), ("proposed", proposed)):
        body = json.dumps(
            list(run.findings), sort_keys=True, separators=(",", ":")
        ).encode()
        digest = _sha256(body)
        evidence_id = f"python-ast-{label}-{digest[:16]}"
        payloads[evidence_id] = body
        evidence.append(
            FixValidationEvidenceReference(
                evidence_id=evidence_id,
                kind="tool_result",
                description=f"Python AST {label} parse result digest.",
                file_path=file_path,
                sha256=digest,
            )
        )
        for finding in run.findings:
            error_body = json.dumps(
                finding, sort_keys=True, separators=(",", ":")
            ).encode()
            error_digest = _sha256(error_body)
            error_id = f"python-syntax-{label}-{error_digest[:16]}"
            payloads[error_id] = error_body
            evidence.append(
                FixValidationEvidenceReference(
                    evidence_id=error_id,
                    kind="syntax_error",
                    description=f"Python syntax diagnostic in the {label} source.",
                    file_path=file_path,
                    line_start=finding.get("line"),
                    line_end=finding.get("end_line"),
                    sha256=error_digest,
                )
            )
    for delta in deltas[:MAX_DELTA_EVIDENCE]:
        body = json.dumps(
            delta.model_data(), sort_keys=True, separators=(",", ":")
        ).encode()
        digest = _sha256(body)
        payloads[delta.delta_id] = body
        evidence.append(
            FixValidationEvidenceReference(
                evidence_id=delta.delta_id,
                kind="finding",
                description=f"Python syntax comparison: {delta.category}.",
                file_path=file_path,
                line_start=(delta.proposed or delta.baseline or {}).get("line"),
                sha256=digest,
            )
        )
    check = FixValidationCheckResult(
        check_id="python-syntax",
        check_name="Python syntax comparison",
        provenance_profile="source_analysis",
        effective_provenance_profile="source_analysis",
        status=status,
        required=True,
        tool_name="python-ast",
        tool_version=proposed.version,
        configuration_sha256=proposed.configuration_sha256,
        started_at=started_at,
        completed_at=completed_at,
        finding_counts=counts,
        evidence=evidence[:MAX_VALIDATION_EVIDENCE],
        reason=reason,
        limitations=["Parsing does not establish runtime or semantic correctness."],
    )
    return (
        check,
        {
            "status": "completed",
            "tool_name": "python-ast",
            "tool_version": proposed.version,
            "configuration_sha256": proposed.configuration_sha256,
            "baseline_findings": list(baseline.findings),
            "proposed_findings": list(proposed.findings),
            "deltas": [
                _with_attribution(delta, added_lines, diff_limitations).model_data()
                for delta in deltas
            ],
        },
        payloads,
    )


def _counts_for_structural(
    baseline: tuple[dict[str, Any], ...],
    proposed: tuple[dict[str, Any], ...],
    deltas: list[FindingDelta],
) -> FixValidationFindingCounts | None:
    categories = Counter(item.category for item in deltas)
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


def _changed_lines(
    file_path: str, baseline: str, proposed: str
) -> tuple[set[int] | None, list[str]]:
    old_lines = baseline.splitlines()
    new_lines = proposed.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, n=3, lineterm=""))
    hunk_lines: list[str] = []
    inside_hunk = False
    for line in diff:
        if line.startswith("@@"):
            inside_hunk = True
        if inside_hunk:
            hunk_lines.append(line)
    if not hunk_lines:
        return set(), []
    changed, limitations = added_lines_by_file(
        [
            {
                "filename": file_path,
                "status": "modified",
                "patch": "\n".join(hunk_lines),
            }
        ]
    )
    if file_path not in changed:
        return None, list(limitations.values()) or [
            "Changed-line attribution could not be established."
        ]
    return changed[file_path], list(limitations.values())


def _with_attribution(
    delta: FindingDelta,
    added_lines: set[int] | None,
    limitations: list[str],
) -> FindingDelta:
    item = delta.proposed or delta.baseline or {}
    line = item.get("line")
    if (
        delta.category == "uncertain"
        or added_lines is None
        or not isinstance(line, int)
    ):
        attribution = "uncertain"
    elif delta.category == "resolved":
        attribution = "replaced_range_or_deleted_context"
    elif delta.category in {"introduced", "modified"}:
        attribution = "changed_line" if line in added_lines else "outside_added_lines"
    elif delta.category == "unchanged" and delta.relocated:
        attribution = "same_finding_relocated"
    else:
        attribution = "unchanged_context"
    return FindingDelta(
        delta.delta_id,
        delta.category,
        delta.baseline,
        delta.proposed,
        delta.confidence,
        relocated=delta.relocated,
        candidate_count=delta.candidate_count,
        attribution=attribution,
    )


def _report_failure(
    fix: SuggestedFix,
    validation_id: str,
    attempt_number: int,
    started: datetime,
    code: str,
    status: FixValidationReportFailureStatus,
    reason: str,
    *,
    snapshot=None,
) -> StaticValidationOutcome:
    completed = datetime.now(UTC)
    failure = FixValidationReportFailure(
        code=code,
        status=status,
        reason=reason[:1_000],
    )
    report = FixValidationReport(
        schema_version="2",
        validation_id=validation_id,
        fix_id=fix.fix_id,
        attempt_number=attempt_number,
        trigger="manual",
        status=aggregate_validation_status([], report_failure=failure),
        engine_version="phase4-stage3/1",
        started_at=started,
        completed_at=completed,
        snapshot=snapshot,
        checks=[],
        report_failure=failure,
        limitations=[
            "Static checks were not started because verified source was unavailable."
        ],
    )
    return StaticValidationOutcome(
        report, {}, {}, {}
    )


def _reconstruct_from_stage2(fix: SuggestedFix, source: str) -> str:
    from app.verification.fix_snapshot import reconstruct_proposed_source

    return reconstruct_proposed_source(source, fix).text


def _validated_source(source: str) -> bytes:
    if not isinstance(source, str) or "\x00" in source:
        raise GitHubFileSnapshotError(
            "Verified source must be UTF-8 text without NUL bytes."
        )
    encoded = source.encode("utf-8", errors="strict")
    if len(encoded) > MAX_SOURCE_BYTES:
        raise GitHubFileSnapshotError(
            "Verified source exceeds the Stage 2 source size limit."
        )
    return encoded


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded_limitations(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result and len(result) < 5:
            result.append(value[:500])
    return result
