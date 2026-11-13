"""Generate bounded, validated explanations for reconciled canonical issues."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.ai_review.client import (
    AIReviewUnavailable,
    complete_validated_json,
    last_call_metadata,
)
from app.ai_review.prompts import (
    CANONICAL_ISSUE_EXPLANATION_GUIDANCE,
    MAX_EXPLANATION_REQUEST_CHARS,
    build_issue_explanation_prompt,
)
from app.schemas.issue import (
    MAX_CAVEATS,
    MAX_CAVEAT_CHARS,
    MAX_LEARNING_RESOURCES,
    CanonicalIssue,
    IssueCodeExample,
    IssueExplanation,
    LearningResource,
)

MAX_ISSUES_PER_BATCH = 3
MAX_OBSERVATIONS_PER_ISSUE = 6
MAX_EVIDENCE_ITEMS_PER_OBSERVATION = 2
MAX_OBSERVATION_TEXT_CHARS = 800
MAX_EVIDENCE_CHARS = 300
MAX_CODE_CONTEXT_CHARS = 1_600
MAX_CODE_CONTEXT_LINES = 10
MAX_CODE_LINE_CHARS = 240
MAX_EXPLANATION_RESPONSE_CHARS = 96_000
MAX_RESPONSE_ENTRIES = 12


@dataclass
class ExplanationGenerationResult:
    explanations: dict[str, IssueExplanation] = field(default_factory=dict)
    batch_count: int = 0
    failed_batch_count: int = 0
    rejected_explanation_count: int = 0
    context_failure_count: int = 0
    failure_reasons: list[str] = field(default_factory=list)
    provider_calls: list[dict[str, Any]] = field(default_factory=list)


async def generate_issue_explanations(
    issues: list[CanonicalIssue],
    *,
    source_files: list[tuple[str, str]] | None = None,
    diff_files: list[dict[str, Any]] | None = None,
) -> ExplanationGenerationResult:
    """Explain each canonical issue once, sharing bounded LLM calls by batch."""
    result = ExplanationGenerationResult()
    if not issues:
        return result

    issue_id_counts = Counter(issue.issue_id for issue in issues)
    ambiguous_ids = {issue_id for issue_id, count in issue_id_counts.items() if count > 1}
    result.context_failure_count = sum(issue_id_counts[issue_id] for issue_id in ambiguous_ids)
    if ambiguous_ids:
        result.failure_reasons.append("Canonical issue IDs were ambiguous; no explanation was attached to those issues.")

    source_by_path = {(path.replace("\\", "/")): content for path, content in (source_files or [])}
    contexts: list[dict[str, Any]] = []
    for issue in issues:
        if issue.issue_id in ambiguous_ids:
            continue
        context = _issue_context(issue, source_by_path, diff_files or [])
        try:
            # Validate each compacted issue in isolation before batching. This
            # guarantees one oversized item cannot poison all later batches.
            build_issue_explanation_prompt([context])
        except ValueError as exc:
            result.context_failure_count += 1
            result.failure_reasons.append(str(exc))
            continue
        contexts.append(context)

    batches = _batch_contexts(contexts)
    result.batch_count = len(batches)
    for batch in batches:
        try:
            prompt = build_issue_explanation_prompt(batch)
            expected_ids = {item["issue_id"] for item in batch}
            code_context_ids = {item["issue_id"] for item in batch if item["code_context"]}

            def parse_for_batch(raw: str, expected=expected_ids, with_context=code_context_ids):
                return _parse_bounded_explanation_response(raw, expected, with_context)

            _raw, (explanations, rejected) = await complete_validated_json(
                "issue_explanation", CANONICAL_ISSUE_EXPLANATION_GUIDANCE, prompt,
                parse_for_batch,
            )
            metadata = last_call_metadata()
            if metadata is not None:
                result.provider_calls.append(metadata)
        except AIReviewUnavailable as exc:
            metadata = last_call_metadata()
            if metadata is not None:
                result.provider_calls.append(metadata)
            result.failed_batch_count += 1
            result.failure_reasons.append(str(exc))
            continue
        except (ValueError, TypeError):
            metadata = last_call_metadata()
            if metadata is not None:
                result.provider_calls.append(metadata)
            # Serialization or parsing errors for one batch are isolated from
            # other batches and from the review itself.
            result.failed_batch_count += 1
            result.failure_reasons.append("AI provider returned an invalid explanation batch.")
            continue
        result.explanations.update(explanations)
        result.rejected_explanation_count += rejected
    return result


def _batch_contexts(contexts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for context in contexts:
        candidate = [*current, context]
        within_issue_limit = len(candidate) <= MAX_ISSUES_PER_BATCH
        try:
            within_size_limit = len(build_issue_explanation_prompt(candidate)) <= MAX_EXPLANATION_REQUEST_CHARS
        except ValueError:
            within_size_limit = False
        if current and (not within_issue_limit or not within_size_limit):
            batches.append(current)
            current = [context]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def _issue_context(
    issue: CanonicalIssue,
    source_by_path: dict[str, str],
    diff_files: list[dict[str, Any]],
) -> dict[str, Any]:
    code_context, code_context_source = _code_context(issue, source_by_path, diff_files)
    observations = []
    all_observations = issue.observations
    for observation in all_observations[:MAX_OBSERVATIONS_PER_ISSUE]:
        finding = observation.original_finding
        observations.append({
            "finding_id": _bounded(finding.id, 120),
            "source": finding.source,
            "tool_name": _bounded(observation.tool_name, 100),
            "provider_name": _bounded(observation.provider_name, 100),
            "rule_id": _bounded(observation.rule_id, 100),
            "title": _bounded(finding.title, 200),
            "message": _bounded(finding.description, MAX_OBSERVATION_TEXT_CHARS),
            "message_truncated": len(finding.description) > MAX_OBSERVATION_TEXT_CHARS,
            "severity": finding.severity,
            "confidence": finding.confidence,
            "verification_status": finding.verification_status,
            "line_start": finding.line_start,
            "line_end": finding.line_end,
            "evidence": [
                _bounded(item, MAX_EVIDENCE_CHARS)
                for item in finding.evidence[:MAX_EVIDENCE_ITEMS_PER_OBSERVATION]
            ],
            "omitted_evidence_count": max(0, len(finding.evidence) - MAX_EVIDENCE_ITEMS_PER_OBSERVATION),
            "truncated_evidence_count": sum(
                len(item) > MAX_EVIDENCE_CHARS for item in finding.evidence[:MAX_EVIDENCE_ITEMS_PER_OBSERVATION]
            ),
        })
    return {
        "issue_id": issue.issue_id,
        "title": _bounded(issue.normalized_description, 1_000),
        "description": _bounded(issue.normalized_description, 1_000),
        "severity": issue.severity,
        "category": issue.category,
        "file_path": _bounded(issue.file_path, 300),
        "line_start": issue.line_start,
        "line_end": issue.line_end,
        "observations": observations,
        "omitted_observation_count": max(0, len(all_observations) - MAX_OBSERVATIONS_PER_ISSUE),
        "code_context": code_context,
        "code_context_source": code_context_source,
    }


def _code_context(
    issue: CanonicalIssue,
    source_by_path: dict[str, str],
    diff_files: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    focus = issue.line_start if issue.line_start is not None else issue.line_end
    if focus is None:
        return None, None
    path = issue.file_path.replace("\\", "/")
    source = source_by_path.get(path)
    if source is not None:
        lines = source.splitlines()
        if 1 <= focus <= len(lines):
            start = max(1, focus - 3)
            end = min(len(lines), max(focus + 3, issue.line_end or focus))
            end = min(end, start + MAX_CODE_CONTEXT_LINES - 1)
            excerpt = _render_code_lines([(number, "context", lines[number - 1]) for number in range(start, end + 1)])
            if excerpt and _excerpt_includes_line(excerpt, focus):
                return excerpt, "current_source"

    for diff in diff_files:
        if not isinstance(diff, dict) or str(diff.get("path", "")).replace("\\", "/") != path:
            continue
        selected: list[tuple[int, str, str]] = []
        start = max(1, focus - 3)
        end = max(focus + 3, issue.line_end or focus)
        for hunk in diff.get("hunks", []):
            for line in hunk.get("lines", []):
                number = line.get("line")
                kind = line.get("kind")
                if isinstance(number, int) and kind in {"added", "context"} and start <= number <= end:
                    selected.append((number, kind, str(line.get("text", ""))))
        selected.sort(key=lambda item: (abs(item[0] - focus), item[0], item[1]))
        excerpt = _render_code_lines(selected[:MAX_CODE_CONTEXT_LINES])
        if excerpt and _excerpt_includes_line(excerpt, focus):
            return excerpt, "pull_request_diff"
    return None, None


def _render_code_lines(lines: list[tuple[int, str, str]]) -> str | None:
    rendered: list[str] = []
    total = 0
    for number, kind, text in lines:
        if len(text) > MAX_CODE_LINE_CHARS:
            continue
        marker = "+" if kind == "added" else " "
        line = f"{number}: {marker}{text}"
        if total + len(line) + (1 if rendered else 0) > MAX_CODE_CONTEXT_CHARS:
            break
        rendered.append(line)
        total += len(line) + (1 if len(rendered) > 1 else 0)
    return "\n".join(rendered) if rendered else None


def _excerpt_includes_line(excerpt: str, line_number: int) -> bool:
    return any(line.startswith(f"{line_number}: ") for line in excerpt.splitlines())


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit]


def _sanitize_explanation_payload(
    raw_explanation: dict[str, Any], has_code_context: bool
) -> dict[str, Any]:
    """Sanitize optional fields (code_example, learning_resources, caveats).

    Optional content that fails validation or lacks source grounding is dropped
    or stripped rather than failing an otherwise valid canonical explanation.
    """
    sanitized = dict(raw_explanation)

    # 1. Code example:
    # If the issue lacked code context in the prompt, ungrounded code examples are dropped.
    if not has_code_context:
        sanitized["code_example"] = None
    elif "code_example" in sanitized and sanitized["code_example"] is not None:
        if isinstance(sanitized["code_example"], dict):
            try:
                IssueCodeExample.model_validate(sanitized["code_example"])
            except (ValidationError, TypeError, ValueError):
                sanitized["code_example"] = None
        else:
            sanitized["code_example"] = None

    # 2. Learning resources:
    # Validate each resource individually; unwhitelisted or malformed URLs are dropped.
    if "learning_resources" in sanitized and sanitized["learning_resources"] is not None:
        if isinstance(sanitized["learning_resources"], list):
            valid_resources: list[dict[str, Any]] = []
            for item in sanitized["learning_resources"]:
                if isinstance(item, dict):
                    try:
                        valid_item = LearningResource.model_validate(item)
                        valid_resources.append(valid_item.model_dump(mode="python"))
                    except (ValidationError, TypeError, ValueError):
                        continue
            sanitized["learning_resources"] = valid_resources[:MAX_LEARNING_RESOURCES]
        else:
            sanitized["learning_resources"] = []

    # 3. Caveats:
    if "caveats" in sanitized and sanitized["caveats"] is not None:
        if isinstance(sanitized["caveats"], list):
            valid_caveats = [
                str(c)
                for c in sanitized["caveats"]
                if isinstance(c, str) and c.strip() and "\x00" not in c and len(c) <= MAX_CAVEAT_CHARS
            ]
            sanitized["caveats"] = valid_caveats[:MAX_CAVEATS]
        else:
            sanitized["caveats"] = []

    return sanitized


def _validate_batch_response(
    raw: str,
    expected_ids: set[str],
    code_context_ids: set[str],
) -> tuple[dict[str, IssueExplanation], int]:
    cleaned_raw = raw.strip()
    if cleaned_raw.startswith("```"):
        lines = cleaned_raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned_raw = "\n".join(lines).strip()
    try:
        body = json.loads(cleaned_raw)
    except json.JSONDecodeError as exc:
        raise AIReviewUnavailable("AI provider returned invalid explanation JSON.") from exc

    entries: list[Any] | None = None
    if isinstance(body, dict):
        if isinstance(body.get("explanations"), list):
            entries = body["explanations"]
        elif isinstance(body.get("explanation"), dict):
            # Single-issue explanation response without an 'explanations' wrapper
            if isinstance(body.get("issue_id"), str):
                entries = [{"issue_id": body["issue_id"], "explanation": body["explanation"]}]
            elif len(expected_ids) == 1:
                entries = [{"issue_id": next(iter(expected_ids)), "explanation": body["explanation"]}]
    elif isinstance(body, list):
        entries = body

    if entries is None:
        raise AIReviewUnavailable("AI provider explanation response must contain an explanations array.")
    if len(entries) > MAX_RESPONSE_ENTRIES:
        raise AIReviewUnavailable("AI provider returned too many explanations.")

    ids = [entry.get("issue_id") for entry in entries if isinstance(entry, dict) and isinstance(entry.get("issue_id"), str)]
    duplicates = {issue_id for issue_id, count in Counter(ids).items() if count > 1}
    explanations: dict[str, IssueExplanation] = {}
    rejected = 0
    for entry in entries:
        if not isinstance(entry, dict) or "issue_id" not in entry or "explanation" not in entry:
            rejected += 1
            continue
        issue_id = entry["issue_id"]
        if not isinstance(issue_id, str) or issue_id not in expected_ids or issue_id in duplicates:
            rejected += 1
            continue
        raw_expl = entry["explanation"]
        if not isinstance(raw_expl, dict):
            rejected += 1
            continue
        try:
            has_context = issue_id in code_context_ids
            sanitized = _sanitize_explanation_payload(raw_expl, has_context)
            explanation = IssueExplanation.model_validate(sanitized)
        except (ValidationError, TypeError, ValueError):
            rejected += 1
            continue
        explanations[issue_id] = explanation
    return explanations, rejected


def _parse_bounded_explanation_response(
    raw: str, expected_ids: set[str], code_context_ids: set[str]
) -> tuple[dict[str, IssueExplanation], int]:
    if not isinstance(raw, str) or len(raw) > MAX_EXPLANATION_RESPONSE_CHARS:
        raise AIReviewUnavailable("AI provider returned an oversized explanation response.")
    return _validate_batch_response(raw, expected_ids, code_context_ids)
