"""Centralized Model Registry and capability definitions."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ModelCapability(str, Enum):
    JSON_OUTPUT = "json_output"
    CODE_REVIEW = "code_review"
    CODE_GENERATION = "code_generation"
    REASONING = "reasoning"
    FAST_INFERENCE = "fast_inference"
    LARGE_CONTEXT = "large_context"
    TEST_GENERATION = "test_generation"


@dataclass(frozen=True)
class ModelPricing:
    """Pricing in USD per 1,000,000 tokens."""
    input_cost_per_million: float
    output_cost_per_million: float
    currency: str = "USD"

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        input_cost = (prompt_tokens / 1_000_000.0) * self.input_cost_per_million
        output_cost = (completion_tokens / 1_000_000.0) * self.output_cost_per_million
        return round(input_cost + output_cost, 6)


@dataclass
class ModelDescriptor:
    model_id: str
    provider_id: str
    display_name: str
    capabilities: set[ModelCapability] = field(default_factory=set)
    context_window_tokens: int = 128_000
    max_output_tokens: int = 4_096
    supports_structured_json: bool = True
    pricing: ModelPricing | None = None
    limitations: list[str] = field(default_factory=list)

    def has_capabilities(self, required: set[ModelCapability]) -> bool:
        return required.issubset(self.capabilities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "capabilities": [c.value for c in self.capabilities],
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
            "supports_structured_json": self.supports_structured_json,
            "has_pricing": self.pricing is not None,
            "limitations": self.limitations,
        }


# Standard pre-registered models across prominent providers
KNOWN_MODELS: list[ModelDescriptor] = [
    # OpenAI
    ModelDescriptor(
        model_id="gpt-4o-mini",
        provider_id="openai",
        display_name="GPT-4o Mini",
        capabilities={
            ModelCapability.JSON_OUTPUT,
            ModelCapability.CODE_REVIEW,
            ModelCapability.CODE_GENERATION,
            ModelCapability.FAST_INFERENCE,
            ModelCapability.TEST_GENERATION,
        },
        context_window_tokens=128_000,
        max_output_tokens=16_384,
        supports_structured_json=True,
        pricing=ModelPricing(input_cost_per_million=0.15, output_cost_per_million=0.60),
    ),
    ModelDescriptor(
        model_id="gpt-4o",
        provider_id="openai",
        display_name="GPT-4o",
        capabilities={
            ModelCapability.JSON_OUTPUT,
            ModelCapability.CODE_REVIEW,
            ModelCapability.CODE_GENERATION,
            ModelCapability.REASONING,
            ModelCapability.LARGE_CONTEXT,
            ModelCapability.TEST_GENERATION,
        },
        context_window_tokens=128_000,
        max_output_tokens=16_384,
        supports_structured_json=True,
        pricing=ModelPricing(input_cost_per_million=2.50, output_cost_per_million=10.00),
    ),
    # Anthropic (via OpenAI-compatible endpoint or adapter)
    ModelDescriptor(
        model_id="claude-3-5-sonnet",
        provider_id="anthropic",
        display_name="Claude 3.5 Sonnet",
        capabilities={
            ModelCapability.JSON_OUTPUT,
            ModelCapability.CODE_REVIEW,
            ModelCapability.CODE_GENERATION,
            ModelCapability.REASONING,
            ModelCapability.LARGE_CONTEXT,
            ModelCapability.TEST_GENERATION,
        },
        context_window_tokens=200_000,
        max_output_tokens=8_192,
        supports_structured_json=True,
        pricing=ModelPricing(input_cost_per_million=3.00, output_cost_per_million=15.00),
    ),
    ModelDescriptor(
        model_id="claude-3-5-haiku",
        provider_id="anthropic",
        display_name="Claude 3.5 Haiku",
        capabilities={
            ModelCapability.JSON_OUTPUT,
            ModelCapability.CODE_REVIEW,
            ModelCapability.FAST_INFERENCE,
            ModelCapability.TEST_GENERATION,
        },
        context_window_tokens=200_000,
        max_output_tokens=8_192,
        supports_structured_json=True,
        pricing=ModelPricing(input_cost_per_million=0.80, output_cost_per_million=4.00),
    ),
    # Google Gemini
    ModelDescriptor(
        model_id="gemini-1.5-flash",
        provider_id="gemini",
        display_name="Gemini 1.5 Flash",
        capabilities={
            ModelCapability.JSON_OUTPUT,
            ModelCapability.CODE_REVIEW,
            ModelCapability.CODE_GENERATION,
            ModelCapability.FAST_INFERENCE,
            ModelCapability.LARGE_CONTEXT,
            ModelCapability.TEST_GENERATION,
        },
        context_window_tokens=1_000_000,
        max_output_tokens=8_192,
        supports_structured_json=True,
        pricing=ModelPricing(input_cost_per_million=0.075, output_cost_per_million=0.30),
    ),
    ModelDescriptor(
        model_id="gemini-1.5-pro",
        provider_id="gemini",
        display_name="Gemini 1.5 Pro",
        capabilities={
            ModelCapability.JSON_OUTPUT,
            ModelCapability.CODE_REVIEW,
            ModelCapability.CODE_GENERATION,
            ModelCapability.REASONING,
            ModelCapability.LARGE_CONTEXT,
            ModelCapability.TEST_GENERATION,
        },
        context_window_tokens=2_000_000,
        max_output_tokens=8_192,
        supports_structured_json=True,
        pricing=ModelPricing(input_cost_per_million=1.25, output_cost_per_million=5.00),
    ),
    # Groq / Open Source
    ModelDescriptor(
        model_id="llama-3.3-70b-versatile",
        provider_id="groq",
        display_name="Llama 3.3 70B (Groq)",
        capabilities={
            ModelCapability.JSON_OUTPUT,
            ModelCapability.CODE_REVIEW,
            ModelCapability.FAST_INFERENCE,
            ModelCapability.CODE_GENERATION,
        },
        context_window_tokens=128_000,
        max_output_tokens=8_192,
        supports_structured_json=True,
        pricing=ModelPricing(input_cost_per_million=0.59, output_cost_per_million=0.79),
    ),
]


class ModelRegistry:
    """Centralized model repository and capability evaluator."""

    def __init__(self) -> None:
        self._models: dict[str, ModelDescriptor] = {}
        for descriptor in KNOWN_MODELS:
            self.register(descriptor)

    def register(self, descriptor: ModelDescriptor) -> None:
        key = f"{descriptor.provider_id}:{descriptor.model_id}".lower()
        self._models[key] = descriptor
        # Also index by model_id alone if not conflicting
        if descriptor.model_id.lower() not in self._models:
            self._models[descriptor.model_id.lower()] = descriptor

    def get_or_create(self, model_id: str, provider_id: str = "openai_compatible") -> ModelDescriptor:
        """Find registered model or synthesize a safe descriptor for custom/unknown models."""
        key = f"{provider_id}:{model_id}".lower()
        if key in self._models:
            return self._models[key]
        if model_id.lower() in self._models:
            return self._models[model_id.lower()]

        # Synthesize fallback descriptor for unlisted models
        synthetic = ModelDescriptor(
            model_id=model_id,
            provider_id=provider_id,
            display_name=f"{model_id} ({provider_id})",
            capabilities={
                ModelCapability.JSON_OUTPUT,
                ModelCapability.CODE_REVIEW,
                ModelCapability.CODE_GENERATION,
                ModelCapability.TEST_GENERATION,
            },
            context_window_tokens=128_000,
            max_output_tokens=4_096,
            supports_structured_json=True,
            pricing=None,
            limitations=["Unlisted model; capabilities assumed with standard OpenAI-compatible defaults."],
        )
        self.register(synthetic)
        return synthetic

    def list_models(self) -> list[ModelDescriptor]:
        unique = {m.model_id: m for m in self._models.values()}
        return list(unique.values())

    def estimate_cost(
        self,
        model_id: str,
        provider_id: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float | None:
        desc = self.get_or_create(model_id, provider_id)
        if desc.pricing is not None:
            return desc.pricing.estimate_cost(prompt_tokens, completion_tokens)
        return None


# Global singleton
_global_model_registry = ModelRegistry()


def get_model_registry() -> ModelRegistry:
    return _global_model_registry
