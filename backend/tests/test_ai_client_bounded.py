import asyncio
import json

import httpx
import pytest

from app.ai_review import client
from app.ai_review.client import AIReviewUnavailable
from app.ai_review.providers import ProviderConfig, public_provider_configuration


def test_task_specific_model_configuration_falls_back_to_legacy_model(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "default-model")
    monkeypatch.setenv("LLM_MODEL_TEST_GENERATION", "test-model")
    assert client.configured_model("test_generation") == "test-model"
    assert client.configured_model("code_review") == "default-model"


def test_named_provider_registry_routes_tasks_and_limits_capabilities(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_CODE_REVIEW", "primary")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDERS_CODE_REVIEW", "secondary")
    monkeypatch.setenv("LLM_PRIMARY_API_KEY", "primary-secret")
    monkeypatch.setenv("LLM_PRIMARY_API_BASE_URL", "https://primary.example/chat")
    monkeypatch.setenv("LLM_PRIMARY_MAX_CONCURRENCY", "99")
    monkeypatch.setenv("LLM_MODEL_CODE_REVIEW_PRIMARY", "review-v3")
    monkeypatch.setenv("LLM_SECONDARY_API_KEY", "secondary-secret")
    monkeypatch.setenv("LLM_MODEL", "fallback-model")
    configured = client.provider_registry("code_review")
    assert [(item.name, item.model) for item in configured] == [("primary", "review-v3"), ("secondary", "fallback-model")]
    assert configured[0].max_concurrency == 32
    assert configured[0].supports_json is True
    public = public_provider_configuration("code_review")
    assert "api_key" not in str(public) and "primary-secret" not in str(public)


def test_provider_concurrency_semaphore_is_shared_per_loop_and_loop_scoped():
    provider = ProviderConfig("bounded", "secret", "model", "https://provider.example/chat", True, 2)

    async def get_semaphore():
        first = client._semaphore(provider)
        assert client._semaphore(provider) is first
        return first

    first_loop_semaphore = asyncio.run(get_semaphore())
    second_loop_semaphore = asyncio.run(get_semaphore())
    assert second_loop_semaphore is not first_loop_semaphore
def _set_transport(monkeypatch, body, *, status=200):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=body))
    real_client = httpx.AsyncClient

    class TransportClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(client.httpx, "AsyncClient", TransportClient)
    monkeypatch.setattr(client, "provider_registry", lambda _task=None: [ProviderConfig("test-provider", "secret", "test-model", "https://example.test/chat", True, 2)])


def test_bounded_client_streams_successful_json_response(monkeypatch):
    _set_transport(monkeypatch, b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}')
    async def call():
        value = await client.complete_json("system", "user", timeout=2, max_response_bytes=500)
        return value, client.last_call_metadata()
    value, metadata = asyncio.run(call())
    assert value == '{"ok":true}'
    assert metadata["provider"] == "test-provider"


def test_provider_registry_fallback_is_ordered_bounded_and_observable(monkeypatch):
    primary = ProviderConfig("primary", "secret-1", "model-1", "https://primary.test/chat", True, 1)
    fallback = ProviderConfig("fallback", "secret-2", "model-2", "https://fallback.test/chat", True, 2)
    monkeypatch.setattr(client, "provider_registry", lambda _task=None: [primary, fallback])
    monkeypatch.setenv("LLM_PROVIDER_RETRIES", "0")
    real_client = httpx.AsyncClient
    def respond(request):
        if "primary.test" in str(request.url):
            return httpx.Response(503, content=b"busy")
        return httpx.Response(200, content=b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}')
    transport = httpx.MockTransport(respond)
    class RoutedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(client.httpx, "AsyncClient", RoutedClient)
    async def call():
        content = await client.complete_json("system", "user")
        return content, client.last_call_metadata()
    content, metadata = asyncio.run(call())
    assert content == '{"ok":true}'
    assert metadata["provider"] == "fallback"
    assert metadata["fallback_used"] is True
    assert metadata["usage"]["total_tokens"] == 8
    assert isinstance(metadata["latency_ms"], int)


def test_structured_schema_failure_tries_next_provider(monkeypatch):
    primary = ProviderConfig("primary", "secret-1", "model-1", "https://primary.test/chat", True, 1)
    fallback = ProviderConfig("fallback", "secret-2", "model-2", "https://fallback.test/chat", True, 2)
    monkeypatch.setattr(client, "provider_registry", lambda _task=None: [primary, fallback])
    monkeypatch.setenv("LLM_PROVIDER_RETRIES", "0")
    real_client = httpx.AsyncClient

    def respond(request):
        content = '{"wrong":true}' if "primary.test" in str(request.url) else '{"result":7}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    transport = httpx.MockTransport(respond)

    class RoutedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(client.httpx, "AsyncClient", RoutedClient)
    def validate(content):
        parsed = json.loads(content)
        if not isinstance(parsed, dict) or set(parsed) != {"result"}:
            raise ValueError("schema")
        return parsed["result"]

    async def validated_call():
        raw, value = await client.complete_validated_json(
            "sample", "system", "user", validate
        )
        return raw, value, client.last_call_metadata()

    raw, value, metadata = asyncio.run(validated_call())
    assert raw == '{"result":7}'
    assert value == 7
    assert metadata["provider"] == "fallback"
    assert metadata["fallback_used"] is True


def test_bounded_client_rejects_oversized_stream(monkeypatch):
    _set_transport(monkeypatch, b"x" * 101)
    with pytest.raises(AIReviewUnavailable, match="size limit"):
        asyncio.run(client.complete_json("system", "user", timeout=2, max_response_bytes=100))


@pytest.mark.parametrize("timeout,limit", [(0, 10), (float("inf"), 10), (1, 0), (1, True)])
def test_bounded_client_rejects_invalid_limits(monkeypatch, timeout, limit):
    _set_transport(monkeypatch, b"{}")
    with pytest.raises(ValueError):
        asyncio.run(client.complete_json("system", "user", timeout=timeout, max_response_bytes=limit))
