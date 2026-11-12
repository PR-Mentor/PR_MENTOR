"""Explicit registry and task-specific routing for OpenAI-compatible providers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from app.config import llm_configuration

MAX_PROVIDER_CHAIN = 4


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str = field(repr=False)
    model: str
    endpoint: str
    supports_json: bool
    max_concurrency: int


def _provider_names(task: str | None) -> list[str]:
    suffix = task.upper().replace("-", "_") if task else ""
    selected = os.getenv(f"LLM_PROVIDER_{suffix}") if suffix else None
    selected = selected or os.getenv("LLM_PROVIDER") or "openai_compatible"
    fallback = os.getenv(f"LLM_FALLBACK_PROVIDERS_{suffix}") if suffix else None
    fallback = fallback or os.getenv("LLM_FALLBACK_PROVIDERS") or ""
    names = [selected.strip().lower(), *(name.strip().lower() for name in fallback.split(",") if name.strip())]
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name) for name in names):
        raise ValueError("Provider names must be lowercase identifiers.")
    return list(dict.fromkeys(names))[:MAX_PROVIDER_CHAIN]


def provider_registry(task: str | None = None) -> list[ProviderConfig]:
    """Return configured provider capabilities in explicit priority order."""
    providers: list[ProviderConfig] = []
    for name in _provider_names(task):
        if name == "openai_compatible":
            api_key, model, endpoint = llm_configuration() if task is None else llm_configuration(task)
            prefix = "LLM_OPENAI_COMPATIBLE"
        else:
            prefix = "LLM_" + name.upper()
            api_key = os.getenv(prefix + "_API_KEY")
            endpoint = os.getenv(prefix + "_API_BASE_URL", "https://api.openai.com/v1/chat/completions")
            task_model = f"LLM_MODEL_{task.upper().replace('-', '_')}_{name.upper()}" if task else ""
            model = (os.getenv(task_model) if task_model else None) or os.getenv(prefix + "_MODEL")
            if model is None:
                _legacy_key, model, _legacy_endpoint = llm_configuration(task)
        if not api_key or not model:
            continue
        try:
            concurrency = int(os.getenv(prefix + "_MAX_CONCURRENCY", "4"))
        except ValueError:
            concurrency = 4
        providers.append(ProviderConfig(name, api_key, model, endpoint, True, max(1, min(concurrency, 32))))
    return providers


def public_provider_configuration(task: str | None = None) -> list[dict[str, str | bool | int]]:
    """Expose only safe provider identity/capability metadata, never credentials."""
    return [
        {"name": item.name, "model": item.model, "supports_json": item.supports_json, "max_concurrency": item.max_concurrency}
        for item in provider_registry(task)
    ]
