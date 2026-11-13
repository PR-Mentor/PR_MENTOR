"""Schema-validated AI candidate findings; provider transport stays replaceable."""

import hashlib
import json
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


async def generate_candidates(
    metadata: dict[str, Any],
    files: list[dict[str, Any]],
    verification: list[dict[str, Any]],
    repository_structure: dict[str, Any] | None = None,
) -> list[ReviewFinding]:
    prompt = build_user_prompt(metadata, files, verification, repository_structure)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    system_prompt_sha256 = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    providers = provider_registry("code_review")
    excluded: list[str] = []
    for _ in range(max(1, len(providers))):
        kwargs = {"exclude_providers": excluded} if excluded else {}
        raw = await complete_for_task("code_review", complete_json, SYSTEM_PROMPT, prompt, **kwargs)
        cause: Exception | None = None
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get("findings"), list):
                raise TypeError("findings array is missing")
            findings: list[ReviewFinding] = []
            for item in body["findings"][:100]:
                if not isinstance(item, dict):
                    continue
                candidate = dict(item)
                candidate.update({"id": str(candidate.get("id") or uuid4().hex), "source": "ai_candidate", "verification_status": "needs_review"})
                finding = ReviewFinding.model_validate(candidate)
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
