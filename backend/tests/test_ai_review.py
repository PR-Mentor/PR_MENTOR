"""Tests for the asynchronous, LLM_API_KEY-based AI review path."""

import asyncio
import json

import pytest

from app.ai_review import client, reviewer
from app.ai_review.client import AIReviewUnavailable
from app.ai_review.prompts import SYSTEM_PROMPT, build_user_prompt
from app.ai_review.providers import ProviderConfig


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


def test_reviewer_schema_failure_falls_back_and_records_actual_provider(monkeypatch):
    import httpx

    from app.ai_review import client

    providers = [
        ProviderConfig("primary", "secret-1", "model-1", "https://primary.example/chat", True, 1),
        ProviderConfig("secondary", "secret-2", "model-2", "https://secondary.example/chat", True, 1),
    ]
    monkeypatch.setattr(client, "provider_registry", lambda _task=None: providers)
    monkeypatch.setattr(reviewer, "provider_registry", lambda _task=None: providers)
    monkeypatch.setattr(client, "llm_configuration", lambda: ("secret-1", "model-1", "https://primary.example/chat"))
    monkeypatch.setenv("LLM_PROVIDER_RETRIES", "0")

    def respond(request):
        content = "{}" if "primary.example" in str(request.url) else '{"findings":[]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient

    class RoutedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(client.httpx, "AsyncClient", RoutedClient)

    async def call():
        result = await reviewer.generate_candidates({}, [], [])
        return result, client.last_call_metadata()

    findings, metadata = asyncio.run(call())
    assert findings == []
    assert metadata["provider"] == "secondary"
    assert metadata["fallback_used"] is True
    assert metadata["prompt_version"] == "code_review_v1"
    assert len(metadata["system_prompt_sha256"]) == 64
    assert len(metadata["user_prompt_sha256"]) == 64
