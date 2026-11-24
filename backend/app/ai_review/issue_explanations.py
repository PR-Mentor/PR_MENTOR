"""Generate bounded, validated explanations for reconciled canonical issues."""

from __future__ import annotations

import asyncio
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

MAX_EXPLANATION_SECONDS = 16.0
MAX_ISSUES_PER_BATCH = 8
MAX_CONCURRENT_EXPLANATION_BATCHES = 3
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
    issue_statuses: dict[str, str] = field(default_factory=dict)
    issue_failure_reasons: dict[str, str] = field(default_factory=dict)
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
    explanation_level: str = "beginner",
    deadline: float | None = None,
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

    if explanation_level not in {"beginner", "intermediate", "advanced"}:
        explanation_level = "beginner"
    source_by_path = {(path.replace("\\", "/")): content for path, content in (source_files or [])}
    contexts: list[dict[str, Any]] = []
    for issue in issues:
        if issue.issue_id in ambiguous_ids:
            continue
        context = _issue_context(issue, source_by_path, diff_files or [], explanation_level)
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
    default_deadline = asyncio.get_running_loop().time() + MAX_EXPLANATION_SECONDS
    deadline = default_deadline if deadline is None else min(default_deadline, deadline)
    budget_reason = "Issue explanation time budget reached."
    budget_exhausted = False
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXPLANATION_BATCHES)

    async def run_batch(
        batch: list[dict[str, Any]],
    ) -> tuple[dict[str, IssueExplanation], int, str | None]:
        async with semaphore:
            async with asyncio.timeout_at(deadline):
                return await _process_batch_with_retry(batch, result)

    tasks = [asyncio.create_task(run_batch(batch)) for batch in batches]
    if tasks:
        done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for batch, task in zip(batches, tasks):
            if task not in done or task.cancelled():
                budget_exhausted = True
                result.failed_batch_count += 1
                continue
            try:
                explanations, rejected, failed_reason = task.result()
            except TimeoutError:
                budget_exhausted = True
                result.failed_batch_count += 1
                continue
            if failed_reason:
                result.failed_batch_count += 1
                result.failure_reasons.append(failed_reason)
                for item in batch:
                    issue_id = item["issue_id"]
                    if issue_id not in result.explanations:
                        result.issue_statuses[issue_id] = "failed"
                        result.issue_failure_reasons[issue_id] = failed_reason
            result.explanations.update(explanations)
            result.rejected_explanation_count += rejected
            for issue_id in explanations:
                result.issue_statuses[issue_id] = "completed"

        if budget_exhausted:
            result.failure_reasons.append(budget_reason)

    for issue in issues:
        if issue.issue_id in ambiguous_ids:
            result.issue_statuses[issue.issue_id] = "unavailable"
            result.issue_failure_reasons[issue.issue_id] = "Canonical issue ID was ambiguous."
        elif budget_exhausted and issue.issue_id not in result.explanations and (
            issue.issue_id not in result.issue_statuses
            or result.issue_statuses[issue.issue_id] == "completed"
        ):
            result.issue_statuses[issue.issue_id] = "unavailable"
            result.issue_failure_reasons[issue.issue_id] = budget_reason
        elif issue.issue_id not in result.explanations and issue.issue_id not in result.issue_statuses:
            result.issue_statuses[issue.issue_id] = "failed"
            result.issue_failure_reasons[issue.issue_id] = "Explanation could not be generated."

    return result


async def _execute_single_batch(
    batch: list[dict[str, Any]], result: ExplanationGenerationResult
) -> tuple[dict[str, IssueExplanation], int, str | None]:
    try:
        prompt = build_issue_explanation_prompt(batch)
        expected_ids = {item["issue_id"] for item in batch}
        code_context_ids = {item["issue_id"] for item in batch if item["code_context"]}

        def parse_for_batch(raw: str, expected=expected_ids, with_context=code_context_ids):
            return _parse_bounded_explanation_response(raw, expected, with_context)

        _raw, (explanations, rejected) = await complete_validated_json(
            "issue_explanation", CANONICAL_ISSUE_EXPLANATION_GUIDANCE, prompt,
            parse_for_batch,
            allow_top_level_array=True,
        )
        metadata = last_call_metadata()
        if metadata is not None:
            result.provider_calls.append(metadata)
        return explanations, rejected, None
    except AIReviewUnavailable as exc:
        metadata = last_call_metadata()
        if metadata is not None:
            result.provider_calls.append(metadata)
        return {}, 0, str(exc)
    except (ValueError, TypeError):
        metadata = last_call_metadata()
        if metadata is not None:
            result.provider_calls.append(metadata)
        return {}, 0, "AI provider returned an invalid explanation batch."


def _is_provider_connectivity_error(reason: str | None) -> bool:
    if not reason:
        return False
    lower = reason.lower()
    return any(
        phrase in lower
        for phrase in [
            "http 50", "http 429", "temporarily unavailable", "did not respond in time",
            "not configured", "endpoint must use https",
        ]
    )


async def _process_batch_with_retry(
    batch: list[dict[str, Any]], result: ExplanationGenerationResult
) -> tuple[dict[str, IssueExplanation], int, str | None]:
    """Execute a batch. If a multi-issue batch fails due to response validation or parsing,
    isolate failures by retrying each issue individually."""
    explanations, rejected, failed_reason = await _execute_single_batch(batch, result)
    if failed_reason is None:
        for item in batch:
            issue_id = item["issue_id"]
            if issue_id in explanations:
                result.issue_statuses[issue_id] = "completed"
            else:
                result.issue_statuses[issue_id] = "failed"
                result.issue_failure_reasons[issue_id] = "Explanation could not be validated for canonical issue."
        return explanations, rejected, None

    # If the provider itself failed with a connectivity or infrastructure error (HTTP 503, timeout, etc.),
    # do not hammer the failing provider with repeated individual requests.
    if _is_provider_connectivity_error(failed_reason):
        return {}, 0, failed_reason

    # If the batch contained multiple issues and failed, retry each individually to prevent
    # one malformed or oversized explanation response from failing the others.
    if len(batch) > 1:
        isolated_explanations: dict[str, IssueExplanation] = {}
        total_rejected = 0
        any_success = False
        last_sub_failure = failed_reason
        for item in batch:
            sub_expls, sub_rejected, sub_err = await _execute_single_batch([item], result)
            total_rejected += sub_rejected
            if sub_err is None and item["issue_id"] in sub_expls:
                isolated_explanations.update(sub_expls)
                any_success = True
                result.issue_statuses[item["issue_id"]] = "completed"
            else:
                last_sub_failure = sub_err or "Explanation could not be validated for canonical issue."
                result.issue_statuses[item["issue_id"]] = "failed"
                result.issue_failure_reasons[item["issue_id"]] = last_sub_failure
        if any_success:
            return isolated_explanations, total_rejected, None
        return {}, total_rejected, last_sub_failure

    return {}, 0, failed_reason


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
    explanation_level: str,
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
        "explanation_level": explanation_level,
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
        if not isinstance(diff, dict):
            continue
        diff_path = (diff.get("path") or diff.get("filename") or "").replace("\\", "/")
        if diff_path != path:
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
    """Sanitize and normalize explanation fields (code_example, learning_resources, caveats, synonyms).

    Optional content that fails validation or lacks source grounding is dropped
    or stripped rather than failing an otherwise valid canonical explanation.
    """
    sanitized = dict(raw_explanation)

    # Core explanation field synonyms
    if not sanitized.get("summary"):
        for k in ("description", "overview", "explanation", "message"):
            if sanitized.get(k):
                sanitized["summary"] = str(sanitized[k])
                break

    if not sanitized.get("why_it_matters"):
        for k in ("impact", "importance", "consequences", "impact_scope"):
            if sanitized.get(k):
                sanitized["why_it_matters"] = str(sanitized[k])
                break

    if not sanitized.get("root_cause"):
        for k in ("cause", "reason", "issue", "problem"):
            if sanitized.get(k):
                sanitized["root_cause"] = str(sanitized[k])
                break

    if not sanitized.get("correction"):
        for k in ("how_to_fix", "fix", "solution", "recommendation", "suggested_fix"):
            if sanitized.get(k):
                sanitized["correction"] = str(sanitized[k])
                break

    if not sanitized.get("concept"):
        for k in ("topic", "category", "theme", "learning_topic"):
            if sanitized.get(k):
                sanitized["concept"] = str(sanitized[k])
                break

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

    if isinstance(body, dict):
        for wrapper_key in ("data", "result", "response", "review"):
            wrapped = body.get(wrapper_key)
            if isinstance(wrapped, dict) and any(
                k in wrapped for k in ("explanations", "explanation", *expected_ids)
            ):
                body = wrapped
                break
            elif isinstance(wrapped, list):
                body = wrapped
                break

    entries: list[Any] | None = None
    if isinstance(body, dict):
        if isinstance(body.get("explanations"), list):
            entries = body["explanations"]
        elif isinstance(body.get("explanations"), dict):
            entries = []
            for k, v in body["explanations"].items():
                entries.append({"issue_id": k, "explanation": v})
        elif isinstance(body.get("explanation"), dict):
            # Single-issue explanation response without an 'explanations' wrapper
            body_id = body.get("issue_id") or body.get("id") or body.get("canonical_issue_id")
            if isinstance(body_id, str):
                entries = [{"issue_id": body_id, "explanation": body["explanation"]}]
            elif len(expected_ids) == 1:
                entries = [{"issue_id": next(iter(expected_ids)), "explanation": body["explanation"]}]
            else:
                entries = [{"explanation": body["explanation"]}]
        elif any(k in expected_ids for k in body.keys()):
            entries = []
            for k, v in body.items():
                entries.append({"issue_id": k, "explanation": v})
        elif isinstance(body.get("summary"), str) or isinstance(body.get("root_cause"), str):
            body_id = body.get("issue_id") or body.get("id") or body.get("canonical_issue_id")
            if isinstance(body_id, str):
                entries = [body]
            elif len(expected_ids) == 1:
                entries = [body]
            else:
                entries = [body]
        else:
            # Check if dict maps ordinal or unknown IDs to explanation dicts
            dict_entries = []
            for k, v in body.items():
                if isinstance(v, dict) and any(
                    field in v for field in ("summary", "root_cause", "description", "explanation", "impact", "cause", "fix")
                ):
                    dict_entries.append({"issue_id": k, "explanation": v})
            if dict_entries:
                entries = dict_entries
    elif isinstance(body, list):
        entries = body

    if entries is None:
        raise AIReviewUnavailable("AI provider explanation response must contain an explanations array.")
    if len(entries) > MAX_RESPONSE_ENTRIES:
        raise AIReviewUnavailable("AI provider returned too many explanations.")

    # Phase 1: Determine the candidate ID for each entry and detect duplicates and conflicts
    candidate_ids: list[str | None] = []
    conflict_indices: set[int] = set()

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            candidate_ids.append(None)
            continue

        outer_id = None
        for k in ("issue_id", "id", "canonical_issue_id"):
            val = entry.get(k)
            if isinstance(val, str) and val.strip():
                outer_id = val.strip()
                break

        raw_expl = entry.get("explanation")
        inner_id = None
        if isinstance(raw_expl, dict):
            for k in ("issue_id", "id", "canonical_issue_id"):
                val = raw_expl.get(k)
                if isinstance(val, str) and val.strip():
                    inner_id = val.strip()
                    break

        if outer_id and inner_id and outer_id != inner_id:
            conflict_indices.add(idx)
            candidate_ids.append(None)
            continue

        cid = outer_id or inner_id
        if not cid and len(expected_ids) == 1 and len(entries) == 1:
            cid = next(iter(expected_ids))
        candidate_ids.append(cid)

    # Detect duplicates among valid candidate IDs
    id_counts = Counter(cid for cid in candidate_ids if cid is not None)
    duplicates = {cid for cid, count in id_counts.items() if count > 1}

    # Phase 2: Validate each entry
    explanations: dict[str, IssueExplanation] = {}
    rejected = 0

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict) or idx in conflict_indices:
            rejected += 1
            continue

        cid = candidate_ids[idx]
        if not cid or cid not in expected_ids or cid in duplicates:
            rejected += 1
            continue

        raw_expl = entry.get("explanation")
        if raw_expl is None:
            raw_expl = {k: v for k, v in entry.items() if k not in ("issue_id", "id", "canonical_issue_id")}
        if not isinstance(raw_expl, dict):
            rejected += 1
            continue

        if cid in explanations:
            rejected += 1
            continue

        try:
            has_context = cid in code_context_ids
            sanitized = _sanitize_explanation_payload(raw_expl, has_context)
            explanation = IssueExplanation.model_validate(sanitized)
        except (ValidationError, TypeError, ValueError):
            rejected += 1
            continue

        explanations[cid] = explanation

    return explanations, rejected


def _parse_bounded_explanation_response(
    raw: str, expected_ids: set[str], code_context_ids: set[str]
) -> tuple[dict[str, IssueExplanation], int]:
    if not isinstance(raw, str) or len(raw) > MAX_EXPLANATION_RESPONSE_CHARS:
        raise AIReviewUnavailable("AI provider returned an oversized explanation response.")
    return _validate_batch_response(raw, expected_ids, code_context_ids)
