"""Schema-validated AI candidate findings; provider transport stays replaceable."""

import json
from uuid import uuid4
from typing import Any

from app.ai_review.client import AIReviewUnavailable, complete_json
from app.ai_review.prompts import SYSTEM_PROMPT, build_user_prompt
from app.schemas.finding import ReviewFinding


async def generate_candidates(metadata: dict[str, Any], files: list[dict[str, Any]], verification: list[dict[str, Any]]) -> list[ReviewFinding]:
    raw = await complete_json(SYSTEM_PROMPT, build_user_prompt(metadata, files, verification))
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIReviewUnavailable("AI provider returned invalid JSON.") from exc
    if not isinstance(body, dict) or not isinstance(body.get("findings"), list):
        raise AIReviewUnavailable("AI provider response must contain a findings array.")
    findings: list[ReviewFinding] = []
    for item in body["findings"][:100]:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        candidate.update({"id": str(candidate.get("id") or uuid4().hex), "source": "ai_candidate", "verification_status": "needs_review"})
        try:
            findings.append(ReviewFinding.model_validate(candidate))
        except Exception as exc:
            raise AIReviewUnavailable("AI provider returned a finding that did not match the review schema.") from exc
    return findings
