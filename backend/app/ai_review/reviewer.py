"""Schema-validated AI candidate findings; provider transport stays replaceable."""

import hashlib
import json
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
                findings.append(ReviewFinding.model_validate(candidate))
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
