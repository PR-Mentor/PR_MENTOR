"""Tests for the asynchronous, LLM_API_KEY-based AI review path."""

import asyncio
import json

import pytest

from app.ai_review import client, reviewer
from app.ai_review.client import AIReviewUnavailable
from app.ai_review.prompts import SYSTEM_PROMPT, build_user_prompt


def test_ai_is_unavailable_without_credentials(monkeypatch):
    monkeypatch.setattr(
        client,
        "llm_configuration",
        lambda: (None, None, "https://api.openai.com/v1/chat/completions"),
    )

    with pytest.raises(AIReviewUnavailable, match="LLM_API_KEY and LLM_MODEL"):
        asyncio.run(client.complete_json("system", "user"))


def test_prompt_wraps_context_as_untrusted_json():
    prompt = build_user_prompt(
        {"title": "Example"},
        [{"filename": "src/example.py", "patch": "+return 1"}],
        [],
    )

    assert "<untrusted_pr_json>" in prompt
    assert '"title": "Example"' in prompt
    assert "untrusted pr context" in prompt.lower()
    assert "Do not claim tests were run" in SYSTEM_PROMPT


def _candidate(**overrides):
    finding = {
        "title": "Possible bug",
        "description": "Check this branch.",
        "file_path": "src/example.py",
        "line_start": None,
        "line_end": None,
        "severity": "medium",
        "category": "bug",
        "confidence": 0.62,
        "evidence": ["The diff adds a conditional."],
        "why_it_matters": "The alternate case may be skipped.",
        "suggested_fix": "Add a case-specific test.",
        "learning_explanation": "Review both paths.",
        "limitations": ["Candidate requires human review."],
    }
    finding.update(overrides)
    return finding


def test_reviewer_validates_candidates_and_marks_them_unverified(monkeypatch):
    async def complete_json(*_args):
        return json.dumps({"findings": [_candidate()]})

    monkeypatch.setattr(reviewer, "complete_json", complete_json)
    findings = asyncio.run(
        reviewer.generate_candidates({"title": "Example"}, [], [])
    )

    assert len(findings) == 1
    assert findings[0].source == "ai_candidate"
    assert findings[0].verification_status == "needs_review"
    assert findings[0].line_start is None


def test_reviewer_rejects_malformed_provider_json(monkeypatch):
    async def complete_json(*_args):
        return "not JSON"

    monkeypatch.setattr(reviewer, "complete_json", complete_json)

    with pytest.raises(AIReviewUnavailable, match="invalid JSON"):
        asyncio.run(reviewer.generate_candidates({}, [], []))


def test_reviewer_rejects_findings_that_do_not_match_the_schema(monkeypatch):
    async def complete_json(*_args):
        return json.dumps({"findings": [_candidate(severity="impossible")]})

    monkeypatch.setattr(reviewer, "complete_json", complete_json)

    with pytest.raises(AIReviewUnavailable, match="did not match the review schema"):
        asyncio.run(reviewer.generate_candidates({}, [], []))
