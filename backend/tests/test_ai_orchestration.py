"""Comprehensive unit and integration tests for Milestone 5: Hybrid Multi-Model Orchestration."""

import asyncio
import json
import time

import httpx
import pytest
from pydantic import BaseModel, Field

from app.ai_orchestration import (
    AIOrchestrator,
    AIReviewUnavailable,
    ExecutionRecord,
    FailureCategory,
    HybridRouter,
    ModelCapability,
    ModelDescriptor,
    ModelPricing,
    ModelRegistry,
    ProviderHealthTracker,
    TaskDefinition,
    TaskRegistry,
    get_ai_orchestrator,
    get_health_tracker,
    get_hybrid_router,
    get_model_registry,
    get_task_registry,
)
from app.ai_orchestration.validation import (
    extract_and_parse_json,
    validate_pydantic_model,
    verify_evidence_grounding,
)
from app.ai_review import client
from app.ai_review.providers import ProviderConfig


# ==============================================================================
# 1. MODEL REGISTRY & CAPABILITY TESTS
# ==============================================================================

def test_model_registration_and_capabilities():
    registry = ModelRegistry()
    desc = ModelDescriptor(
        model_id="custom-model",
        provider_id="custom-provider",
        display_name="Custom Model",
        capabilities={ModelCapability.JSON_OUTPUT, ModelCapability.CODE_REVIEW},
        pricing=ModelPricing(input_cost_per_million=1.0, output_cost_per_million=2.0),
    )
    registry.register(desc)

    fetched = registry.get_or_create("custom-model", "custom-provider")
    assert fetched.model_id == "custom-model"
    assert fetched.has_capabilities({ModelCapability.CODE_REVIEW}) is True
    assert fetched.has_capabilities({ModelCapability.CODE_GENERATION}) is False


def test_cost_estimation_calculation():
    registry = ModelRegistry()
    desc = ModelDescriptor(
        model_id="priced-model",
        provider_id="openai",
        display_name="Priced Model",
        capabilities={ModelCapability.JSON_OUTPUT},
        pricing=ModelPricing(input_cost_per_million=2.50, output_cost_per_million=10.00),
    )
    registry.register(desc)

    # 1,000 prompt tokens ($0.0025) + 500 completion tokens ($0.005) = $0.0075
    cost = registry.estimate_cost("priced-model", "openai", prompt_tokens=1_000, completion_tokens=500)
    assert cost == 0.0075

    # Unpriced model returns None
    unpriced_cost = registry.estimate_cost("unknown-free-model", "local", 1_000, 500)
    assert unpriced_cost is None


# ==============================================================================
# 2. TASK REGISTRY & HYBRID ROUTING TESTS
# ==============================================================================

def test_task_registry_defaults_and_dynamic_registration():
    registry = TaskRegistry()
    code_review_task = registry.get("code_review")
    assert ModelCapability.CODE_REVIEW in code_review_task.required_capabilities

    dynamic_task = registry.get("special_custom_task")
    assert dynamic_task.task_id == "special_custom_task"
    assert ModelCapability.JSON_OUTPUT in dynamic_task.required_capabilities


def test_hybrid_router_capability_filtering(monkeypatch):
    router = HybridRouter()
    # Configure two providers: one has code_generation, one only has code_review
    model_reg = get_model_registry()
    model_reg.register(
        ModelDescriptor(
            model_id="review-only-model",
            provider_id="p1",
            display_name="Review Only",
            capabilities={ModelCapability.JSON_OUTPUT, ModelCapability.CODE_REVIEW},
        )
    )
    model_reg.register(
        ModelDescriptor(
            model_id="gen-model",
            provider_id="p2",
            display_name="Generation Model",
            capabilities={ModelCapability.JSON_OUTPUT, ModelCapability.CODE_GENERATION},
        )
    )

    p1 = ProviderConfig("p1", "sec1", "review-only-model", "https://p1.test", True, 2)
    p2 = ProviderConfig("p2", "sec2", "gen-model", "https://p2.test", True, 2)
    monkeypatch.setattr(client, "provider_registry", lambda task=None: [p1, p2])

    # Route for fix_generation: requires CODE_GENERATION, so p1 should be filtered out!
    candidates_fix = router.route_candidates("fix_generation")
    assert len(candidates_fix) == 1
    assert candidates_fix[0].provider.name == "p2"

    # Route for code_review: requires CODE_REVIEW, so only p1 qualifies
    candidates_review = router.route_candidates("code_review")
    assert len(candidates_review) == 1
    assert candidates_review[0].provider.name == "p1"


# ==============================================================================
# 3. PROVIDER HEALTH & FAILURE CLASSIFICATION TESTS
# ==============================================================================

def test_health_tracker_success_and_cooldown():
    tracker = ProviderHealthTracker()
    assert tracker.is_available("test_provider") is True

    # Record rate limit with 429
    tracker.record_failure("test_provider", FailureCategory.RATE_LIMIT, retry_after_seconds=5.0)
    assert tracker.is_available("test_provider") is False
    assert tracker.get_state("test_provider").remaining_cooldown_seconds > 0

    # Reset/success clears cooldown
    tracker.record_success("test_provider")
    assert tracker.is_available("test_provider") is True
    assert tracker.get_state("test_provider").consecutive_failures == 0


def test_health_tracker_exception_classification():
    tracker = ProviderHealthTracker()

    timeout_exc = httpx.TimeoutException("Read timed out")
    cat, retryable, retry_after = tracker.classify_exception(timeout_exc)
    assert cat == FailureCategory.TRANSIENT_NETWORK
    assert retryable is True

    # HTTP 429 with Retry-After header
    req = httpx.Request("POST", "https://example.com")
    resp_429 = httpx.Response(429, request=req, headers={"Retry-After": "12"})
    status_429 = httpx.HTTPStatusError("Rate limited", request=req, response=resp_429)
    cat_429, retryable_429, retry_after_429 = tracker.classify_exception(status_429)
    assert cat_429 == FailureCategory.RATE_LIMIT
    assert retryable_429 is True
    assert retry_after_429 == 12.0

    # HTTP 401 permanent auth failure
    resp_401 = httpx.Response(401, request=req)
    status_401 = httpx.HTTPStatusError("Unauthorized", request=req, response=resp_401)
    cat_401, retryable_401, _ = tracker.classify_exception(status_401)
    assert cat_401 == FailureCategory.AUTHENTICATION_ERROR
    assert retryable_401 is False


def _patch_client_transport(monkeypatch, transport):
    real_async_client = httpx.AsyncClient

    class CustomAsyncClient(real_async_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(client.httpx, "AsyncClient", CustomAsyncClient)


# ==============================================================================
# 4. RESILIENCE: RETRY & SEQUENTIAL FALLBACK TESTS
# ==============================================================================

def test_orchestrator_sequential_fallback_on_503(monkeypatch):
    p1 = ProviderConfig("primary", "sec-1", "model-1", "https://p1.test/chat", True, 1)
    p2 = ProviderConfig("fallback", "sec-2", "model-2", "https://p2.test/chat", True, 1)
    monkeypatch.setattr(client, "provider_registry", lambda task=None: [p1, p2])
    monkeypatch.setenv("LLM_PROVIDER_RETRIES", "0")

    def mock_handler(request: httpx.Request):
        if "p1.test" in str(request.url):
            return httpx.Response(503, content=b"Service Temporarily Unavailable")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"success": true}'}}]})

    transport = httpx.MockTransport(mock_handler)
    _patch_client_transport(monkeypatch, transport)

    orchestrator = get_ai_orchestrator()
    res = asyncio.run(orchestrator.complete("system", "user", task="code_review"))
    assert res == '{"success": true}'

    meta = orchestrator.get_last_record()
    assert meta["provider"] == "fallback"
    assert meta["fallback_used"] is True
    assert "primary" in meta["providers_attempted"]
    assert "fallback" in meta["providers_attempted"]


def test_orchestrator_permanent_auth_failure_advances_immediately_without_retrying(monkeypatch):
    p1 = ProviderConfig("bad_auth", "invalid-key", "model-1", "https://p1.test/chat", True, 1)
    p2 = ProviderConfig("good_auth", "valid-key", "model-2", "https://p2.test/chat", True, 1)
    monkeypatch.setattr(client, "provider_registry", lambda task=None: [p1, p2])
    # Set retries to 2: bad_auth should not waste 2 retries because 401 is permanent!
    monkeypatch.setenv("LLM_PROVIDER_RETRIES", "2")

    call_counts = {"p1": 0, "p2": 0}

    def mock_handler(request: httpx.Request):
        if "p1.test" in str(request.url):
            call_counts["p1"] += 1
            return httpx.Response(401, content=b"Unauthorized")
        call_counts["p2"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"auth": "ok"}'}}]})

    transport = httpx.MockTransport(mock_handler)
    _patch_client_transport(monkeypatch, transport)

    orchestrator = get_ai_orchestrator()
    res = asyncio.run(orchestrator.complete("system", "user", task="code_review"))
    assert res == '{"auth": "ok"}'
    # p1 must only have been attempted once (no retries on 401!)
    assert call_counts["p1"] == 1
    assert call_counts["p2"] == 1


# ==============================================================================
# 5. STRUCTURED-OUTPUT & EVIDENCE GROUNDING TESTS
# ==============================================================================

class SampleSchema(BaseModel):
    findings: list[str]
    severity: str = Field(default="medium")


def test_extract_and_parse_json_with_fences_and_preamble():
    raw_markdown = """Here is the review result:
```json
{
    "findings": ["memory leak in parser", "missing null check"],
    "severity": "high"
}
```
Hope this helps!"""

    parsed = extract_and_parse_json(raw_markdown)
    assert parsed["severity"] == "high"
    assert len(parsed["findings"]) == 2

    # Validate against schema
    validated = validate_pydantic_model(parsed, SampleSchema)
    assert validated.severity == "high"
    assert len(validated.findings) == 2


def test_validate_pydantic_model_missing_fields_rejection():
    bad_data = {"severity": "low"}  # Missing "findings"
    with pytest.raises(ValueError, match="missing required schema fields"):
        validate_pydantic_model(bad_data, SampleSchema)


def test_evidence_grounding_verification():
    diff_files = [
        {
            "path": "src/net.c",
            "hunks": [
                {
                    "lines": [
                        {"kind": "added", "line": 15, "text": "    gets(buffer);"},
                        {"kind": "added", "line": 16, "text": "    printf(\"%s\", buffer);"},
                    ]
                }
            ],
        }
    ]

    # 1. Grounded finding: path, line, and evidence match added lines
    is_grounded = verify_evidence_grounding(
        file_path="src/net.c",
        line_start=15,
        line_end=15,
        diff_files=diff_files,
        evidence=["gets(buffer)"],
    )
    assert is_grounded is True

    # 2. Ungrounded finding: references file not in PR diff
    is_fake_file = verify_evidence_grounding(
        file_path="src/nonexistent.c",
        line_start=15,
        line_end=15,
        diff_files=diff_files,
        evidence=["gets(buffer)"],
    )
    assert is_fake_file is False

    # 3. Ungrounded finding: evidence line is hallucinated
    is_fake_evidence = verify_evidence_grounding(
        file_path="src/net.c",
        line_start=15,
        line_end=15,
        diff_files=diff_files,
        evidence=["system(\"rm -rf /\")"],
    )
    assert is_fake_evidence is False


# ==============================================================================
# 6. OBSERVABILITY & SECRET REDACTION TESTS
# ==============================================================================

def test_telemetry_captures_tokens_and_redacts_credentials(monkeypatch):
    p = ProviderConfig("secure_prov", "super-secret-api-key-12345", "gpt-4o-mini", "https://secure.test/chat", True, 2)
    monkeypatch.setattr(client, "provider_registry", lambda task=None: [p])

    def mock_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 150, "completion_tokens": 50, "total_tokens": 200},
            },
        )

    transport = httpx.MockTransport(mock_handler)
    _patch_client_transport(monkeypatch, transport)

    orchestrator = get_ai_orchestrator()
    res = asyncio.run(orchestrator.complete("system", "user", task="code_review"))
    assert res == '{"ok": true}'

    record = orchestrator.get_last_record()
    assert record["prompt_tokens"] == 150
    assert record["completion_tokens"] == 50
    assert record["total_tokens"] == 200
    assert record["estimated_cost_usd"] is not None
    assert record["estimated_cost_usd"] > 0

    # Ensure secret API key is never in record
    dumped = json.dumps(record)
    assert "super-secret-api-key-12345" not in dumped
    assert "api_key" not in dumped
