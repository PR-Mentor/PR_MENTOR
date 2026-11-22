"""Schema-validated AI candidate findings; provider transport stays replaceable."""

import hashlib
import json
import math
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.ai_review.client import (
    AIReviewUnavailable,
    annotate_last_call,
    complete_for_task,
    complete_json,
    last_call_metadata,
)
from app.ai_review.prompts import SYSTEM_PROMPT, build_user_prompt
from app.ai_review.providers import provider_registry
from app.schemas.finding import ReviewFinding


def _finding_supported_by_diff(finding: ReviewFinding, files: list[dict[str, Any]]) -> bool:
    """Keep AI candidates only when their path, location, and evidence map to added PR code."""
    raw_path = finding.file_path.replace("\\", "/")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or not path.parts or ".." in path.parts or ":" in path.parts[0]:
        return False
    normalized_path = path.as_posix()

    added_lines: dict[int, str] = {}
    for changed_file in files:
        candidate_path = changed_file.get("path")
        if not isinstance(candidate_path, str):
            continue
        normalized_changed_path = PurePosixPath(candidate_path.replace("\\", "/"))
        if normalized_changed_path.as_posix() != normalized_path:
            continue
        for hunk in changed_file.get("hunks", []):
            if not isinstance(hunk, dict):
                continue
            for line in hunk.get("lines", []):
                if (
                    isinstance(line, dict)
                    and line.get("kind") == "added"
                    and isinstance(line.get("line"), int)
                    and isinstance(line.get("text"), str)
                ):
                    added_lines[line["line"]] = line["text"]
        break
    if not added_lines or not finding.evidence:
        return False

    start, end = finding.line_start, finding.line_end
    if start is not None or end is not None:
        start = start if start is not None else end
        end = end if end is not None else start
        if start is None or end is None or start < 1 or end < start:
            return False
        relevant_lines = [
            text for number, text in added_lines.items() if start <= number <= end
        ]
        if not relevant_lines:
            return False
    else:
        relevant_lines = list(added_lines.values())

    source_text = "\n".join(" ".join(line.split()).casefold() for line in relevant_lines)
    return any(
        len(normalized := " ".join(evidence.split()).casefold()) >= 8
        and normalized in source_text
        for evidence in finding.evidence
    )


def _extract_findings_array(body: Any) -> list[dict[str, Any]]:
    """Extract findings list from provider JSON, supporting standard and wrapped structures."""
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        if isinstance(body.get("findings"), list):
            return [item for item in body["findings"] if isinstance(item, dict)]
        if isinstance(body.get("candidates"), list):
            return [item for item in body["candidates"] if isinstance(item, dict)]
        for key in ("review", "result", "response", "data"):
            val = body.get(key)
            if isinstance(val, dict) and isinstance(val.get("findings"), list):
                return [item for item in val["findings"] if isinstance(item, dict)]
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict)]
    raise TypeError("findings array is missing")


def _normalize_candidate_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize common provider synonyms, types, and formatting into ReviewFinding schema."""
    normalized = dict(item)

    # 1. file_path synonym
    if not normalized.get("file_path"):
        for k in ("file", "path", "filename"):
            if normalized.get(k):
                normalized["file_path"] = str(normalized[k])
                break

    # 2. line numbers
    if normalized.get("line_start") is None:
        for k in ("line", "line_number", "start_line"):
            if normalized.get(k) is not None:
                try:
                    normalized["line_start"] = int(normalized[k])
                except (ValueError, TypeError):
                    pass
                break
    elif isinstance(normalized.get("line_start"), str):
        try:
            normalized["line_start"] = int(normalized["line_start"])
        except ValueError:
            normalized["line_start"] = None

    if normalized.get("line_end") is None:
        if normalized.get("line_start") is not None:
            normalized["line_end"] = normalized["line_start"]
    elif isinstance(normalized.get("line_end"), str):
        try:
            normalized["line_end"] = int(normalized["line_end"])
        except ValueError:
            normalized["line_end"] = None

    # 3. description synonyms
    if not normalized.get("description"):
        for k in ("message", "details", "summary"):
            if normalized.get(k):
                normalized["description"] = str(normalized[k])
                break

    # 4. why_it_matters synonyms
    if not normalized.get("why_it_matters"):
        for k in ("explanation", "impact", "reason"):
            if normalized.get(k):
                normalized["why_it_matters"] = str(normalized[k])
                break
        if not normalized.get("why_it_matters") and normalized.get("description"):
            normalized["why_it_matters"] = str(normalized["description"])

    # 5. suggested_fix synonyms
    if not normalized.get("suggested_fix"):
        for k in ("fix", "recommendation", "solution", "proposed_code"):
            if normalized.get(k):
                normalized["suggested_fix"] = str(normalized[k])
                break
        if normalized.get("suggested_fix") is None:
            normalized["suggested_fix"] = ""

    # 6. learning_explanation
    if not normalized.get("learning_explanation"):
        if normalized.get("learning"):
            normalized["learning_explanation"] = str(normalized["learning"])
        else:
            normalized["learning_explanation"] = ""

    # 7. evidence
    ev = normalized.get("evidence")
    if isinstance(ev, str):
        normalized["evidence"] = [ev] if ev.strip() else []
    elif not isinstance(ev, list):
        normalized["evidence"] = []
    else:
        normalized["evidence"] = [str(x) for x in ev if x is not None]

    # 8. limitations
    lim = normalized.get("limitations")
    if isinstance(lim, str):
        normalized["limitations"] = [lim] if lim.strip() else []
    elif not isinstance(lim, list):
        normalized["limitations"] = []
    else:
        normalized["limitations"] = [str(x) for x in lim if x is not None]

    # 9. confidence
    conf = normalized.get("confidence")
    if isinstance(conf, str):
        c_str = conf.strip()
        if c_str.endswith("%"):
            try:
                pct_val = float(c_str[:-1].strip())
                if not (math.isnan(pct_val) or math.isinf(pct_val)):
                    conf = pct_val / 100.0
            except ValueError:
                pass
        else:
            try:
                num_val = float(c_str)
                if not (math.isnan(num_val) or math.isinf(num_val)):
                    if c_str.isdigit() and 1 < int(c_str) <= 100:
                        conf = int(c_str) / 100.0
                    else:
                        conf = num_val
            except ValueError:
                pass
    elif isinstance(conf, int) and not isinstance(conf, bool):
        if 1 < conf <= 100:
            conf = conf / 100.0
        else:
            conf = float(conf)
    elif isinstance(conf, float):
        pass

    normalized["confidence"] = conf

    # 10. severity normalization
    sev = normalized.get("severity")
    if isinstance(sev, str):
        sev_clean = sev.strip().lower()
        sev_mapping = {
            "info": "informational",
            "information": "informational",
            "warn": "medium",
            "warning": "medium",
            "error": "high",
        }
        normalized["severity"] = sev_mapping.get(sev_clean, sev_clean)

    # 11. category normalization
    cat = normalized.get("category")
    if isinstance(cat, str):
        cat_clean = cat.strip().lower()
        cat_mapping = {
            "style": "code_quality",
            "best_practice": "code_quality",
            "best_practices": "code_quality",
            "syntax": "bug",
            "correctness": "bug",
            "vulnerability": "security",
            "security_vulnerability": "security",
            "performance_issue": "performance",
            "optimization": "performance",
            "maintainability_issue": "maintainability",
            "doc": "documentation",
            "docs": "documentation",
        }
        normalized["category"] = cat_mapping.get(cat_clean, cat_clean)

    normalized["id"] = str(normalized.get("id") or uuid4().hex)
    normalized["source"] = "ai_candidate"
    normalized["verification_status"] = "needs_review"
    if "attribution" not in normalized:
        normalized["attribution"] = "contextual"

    allowed_fields = {
        "id", "title", "description", "file_path", "line_start", "line_end",
        "severity", "category", "confidence", "source", "verification_status",
        "evidence", "why_it_matters", "suggested_fix", "learning_explanation",
        "limitations", "attribution"
    }
    return {k: v for k, v in normalized.items() if k in allowed_fields}


async def generate_candidates(
    metadata: dict[str, Any],
    files: list[dict[str, Any]],
    verification: list[dict[str, Any]],
    repository_structure: dict[str, Any] | None = None,
) -> list[ReviewFinding]:
    try:
        prompt = build_user_prompt(metadata, files, verification, repository_structure)
    except ValueError as exc:
        raise AIReviewUnavailable(str(exc)) from exc
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    system_prompt_sha256 = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    providers = provider_registry("code_review")
    excluded: list[str] = []
    for _ in range(max(1, len(providers))):
        kwargs = {"exclude_providers": excluded} if excluded else {}
        raw = await complete_for_task("code_review", complete_json, SYSTEM_PROMPT, prompt, **kwargs)
        cause: Exception | None = None
        try:
            cleaned_raw = raw.strip()
            if cleaned_raw.startswith("```"):
                lines = cleaned_raw.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned_raw = "\n".join(lines).strip()
            body = json.loads(cleaned_raw)
            items = _extract_findings_array(body)
            findings: list[ReviewFinding] = []
            seen_signatures: set[tuple[str, int | None, int | None, str, str]] = set()
            for item in items[:100]:
                if not isinstance(item, dict):
                    continue
                candidate = _normalize_candidate_payload(item)
                finding = ReviewFinding.model_validate(candidate)
                sig = (
                    finding.file_path,
                    finding.line_start,
                    finding.line_end,
                    finding.category,
                    " ".join(finding.title.lower().split()),
                )
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)
                if _finding_supported_by_diff(finding, files):
                    findings.append(finding)
            annotate_last_call(
                prompt_version="code_review_v1",
                system_prompt_sha256=system_prompt_sha256,
                user_prompt_sha256=prompt_sha256,
            )
            return findings
        except json.JSONDecodeError as exc:
            cause = exc
            error = "AI provider returned invalid JSON."
        except (TypeError, ValidationError) as exc:
            cause = exc
            error = "AI provider returned a finding that did not match the review schema."
        metadata = last_call_metadata() or {}
        attempted = metadata.get("providers_attempted", [])
        provider = metadata.get("provider")
        newly_excluded = [name for name in attempted if isinstance(name, str) and name not in excluded]
        if isinstance(provider, str) and provider not in excluded and provider not in newly_excluded:
            newly_excluded.append(provider)
        if not newly_excluded:
            raise AIReviewUnavailable(error) from cause
        excluded.extend(newly_excluded)
        if len(excluded) >= len(providers):
            raise AIReviewUnavailable(error) from cause
    raise AIReviewUnavailable("All configured providers returned responses that failed review schema validation.")
