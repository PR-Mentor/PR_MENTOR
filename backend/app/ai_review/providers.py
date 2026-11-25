"""Explicit registry and task-specific routing for LLM providers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from app.config import (
    DEFAULT_LLM_PROVIDER_ORDER,
    LLM_PROVIDER_DEFAULTS,
    llm_configuration,
)

MAX_PROVIDER_CHAIN = 4


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str = field(repr=False)
    model: str
    endpoint: str
    supports_json: bool
    max_concurrency: int


def _configured_named_providers() -> list[str]:
    """Return named providers with a key, preserving the default priority."""
    configured = []

    for name in DEFAULT_LLM_PROVIDER_ORDER:
        defaults = LLM_PROVIDER_DEFAULTS[name]
        prefix = "LLM_" + name.upper()
        if os.getenv(prefix + "_API_KEY") or os.getenv(defaults["api_key_env"]):
            configured.append(name)

    return configured


def _provider_names(task: str | None) -> list[str]:
    suffix = task.upper().replace("-", "_") if task else ""

    selected = os.getenv(f"LLM_PROVIDER_{suffix}") if suffix else None
    selected = selected or os.getenv("LLM_PROVIDER")

    fallback = os.getenv(f"LLM_FALLBACK_PROVIDERS_{suffix}") if suffix else None
    fallback = fallback or os.getenv("LLM_FALLBACK_PROVIDERS")

    configured_named = _configured_named_providers()

    if selected:
        selected = selected.strip().lower()
    elif configured_named:
        selected = configured_named[0]
    else:
        # Preserve the legacy single-provider setup when no named provider key
        # has been configured.
        selected = "openai_compatible"

    if fallback is None:
        fallback_names = [
            name for name in DEFAULT_LLM_PROVIDER_ORDER if name != selected
        ]
        if selected == "openai_compatible":
            fallback_names = configured_named
    else:
        fallback_names = [
            name.strip().lower()
            for name in fallback.split(",")
            if name.strip()
        ]

    names = [selected, *fallback_names]

    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name) for name in names):
        raise ValueError("Provider names must be lowercase identifiers.")

    return list(dict.fromkeys(names))[:MAX_PROVIDER_CHAIN]


def provider_registry(task: str | None = None) -> list[ProviderConfig]:
    """Return configured providers in priority order, skipping missing keys."""
    providers: list[ProviderConfig] = []

    for name in _provider_names(task):
        if name == "openai_compatible":
            api_key, model, endpoint = (
                llm_configuration() if task is None else llm_configuration(task)
            )
            prefix = "LLM_OPENAI_COMPATIBLE"
        else:
            defaults = LLM_PROVIDER_DEFAULTS.get(name)
            prefix = "LLM_" + name.upper()

            if defaults is None:
                api_key = os.getenv(prefix + "_API_KEY")
                endpoint = os.getenv(
                    prefix + "_API_BASE_URL",
                    "https://api.openai.com/v1/chat/completions",
                )
                default_model = None
            else:
                api_key = (
                    os.getenv(prefix + "_API_KEY")
                    or os.getenv(defaults["api_key_env"])
                )
                endpoint = os.getenv(
                    prefix + "_API_BASE_URL",
                    defaults["endpoint"],
                )
                default_model = defaults["model"]

            task_model_key = (
                f"LLM_MODEL_{task.upper().replace('-', '_')}_{name.upper()}"
                if task
                else ""
            )
            model = (
                (os.getenv(task_model_key) if task_model_key else None)
                or os.getenv(prefix + "_MODEL")
                or default_model
            )

            if model is None:
                _legacy_key, model, _legacy_endpoint = llm_configuration(task)

        if not api_key or not model:
            continue

        try:
            concurrency = int(os.getenv(prefix + "_MAX_CONCURRENCY", "4"))
        except ValueError:
            concurrency = 4

        providers.append(
            ProviderConfig(
                name=name,
                api_key=api_key,
                model=model,
                endpoint=endpoint.strip(),
                supports_json=True,
                max_concurrency=max(1, min(concurrency, 32)),
            )
        )

    return providers


def public_provider_configuration(
    task: str | None = None,
) -> list[dict[str, str | bool | int]]:
    """Expose safe provider metadata without credentials."""
    return [
        {
            "name": item.name,
            "model": item.model,
            "supports_json": item.supports_json,
            "max_concurrency": item.max_concurrency,
        }
        for item in provider_registry(task)
    ]
