import asyncio
import json

import pytest

from app.ai_review import reviewer
from app.ai_review.client import AIReviewUnavailable
from app.ai_review.providers import ProviderConfig


def test_ai_is_honestly_unavailable_without_credentials(monkeypatch):
    from app.ai_review import client

    monkeypatch.setattr(client, "provider_registry", lambda _task=None: [])
    with pytest.raises(AIReviewUnavailable, match="not configured"):
        asyncio.run(client.complete_json("system", "user"))


def test_reviewer_validates_provider_findings_and_marks_them_unverified(monkeypatch):
    async def complete(*_args):
        return json.dumps({"findings": [{"title": "Possible bug", "description": "Check this branch.", "file_path": "src/a.py", "line_start": None, "line_end": None, "severity": "medium", "category": "bug", "confidence": 0.62, "evidence": ["The diff adds a conditional."], "why_it_matters": "The alternate case may be skipped.", "suggested_fix": "Add a case-specific test.", "learning_explanation": "Review both paths.", "limitations": ["Candidate requires human review."]}]})

    monkeypatch.setattr(reviewer, "complete_json", complete)
    findings = asyncio.run(reviewer.generate_candidates({"title": "T"}, [], []))
    assert findings[0].source == "ai_candidate"
    assert findings[0].verification_status == "needs_review"
    assert findings[0].line_start is None


def test_reviewer_rejects_malformed_provider_json(monkeypatch):
    async def complete(*_args):
        return "not json"

    monkeypatch.setattr(reviewer, "complete_json", complete)
    with pytest.raises(AIReviewUnavailable, match="invalid JSON"):
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
