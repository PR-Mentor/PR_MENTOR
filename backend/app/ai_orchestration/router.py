"""Hybrid model router combining task policies, model capabilities, and provider health."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai_orchestration.health import ProviderHealthTracker, get_health_tracker
from app.ai_orchestration.models import ModelCapability, ModelDescriptor, ModelRegistry, get_model_registry
from app.ai_orchestration.tasks import TaskDefinition, TaskRegistry, get_task_registry
from app.ai_review.providers import ProviderConfig, provider_registry


@dataclass
class RoutedCandidate:
    provider: ProviderConfig
    model: ModelDescriptor
    is_healthy: bool
    remaining_cooldown: float = 0.0
    status_note: str = "Ready"


class HybridRouter:
    """Predictable, capability-aware, health-governed hybrid model routing engine."""

    def __init__(
        self,
        task_registry: TaskRegistry | None = None,
        model_registry: ModelRegistry | None = None,
        health_tracker: ProviderHealthTracker | None = None,
    ) -> None:
        self._task_registry = task_registry or get_task_registry()
        self._model_registry = model_registry or get_model_registry()
        self._health_tracker = health_tracker or get_health_tracker()

    def route_candidates(
        self,
        task_id: str | None = None,
        exclude_providers: list[str] | None = None,
    ) -> list[RoutedCandidate]:
        task = self._task_registry.get(task_id)
        from app.ai_review import client
        configured_providers = client.provider_registry(task.task_id)

        excluded_set = set(p.lower() for p in (exclude_providers or []))
        eligible_healthy: list[RoutedCandidate] = []
        eligible_cooling_down: list[RoutedCandidate] = []

        for provider in configured_providers:
            if provider.name.lower() in excluded_set:
                continue

            model_desc = self._model_registry.get_or_create(provider.model, provider.name)

            # Capability verification: check if model satisfies task requirements
            if not model_desc.has_capabilities(task.required_capabilities):
                # Model lacks required capabilities for this specific task
                continue

            health_state = self._health_tracker.get_state(provider.name)
            is_healthy = self._health_tracker.is_available(provider.name)

            candidate = RoutedCandidate(
                provider=provider,
                model=model_desc,
                is_healthy=is_healthy,
                remaining_cooldown=health_state.remaining_cooldown_seconds,
                status_note="Ready" if is_healthy else f"Cooling down ({health_state.remaining_cooldown_seconds}s)",
            )

            if is_healthy:
                eligible_healthy.append(candidate)
            else:
                eligible_cooling_down.append(candidate)

        # Priority: Healthy candidates first in configured order, then cooling-down candidates as last resort
        return eligible_healthy + eligible_cooling_down

    def explain_routing(self, task_id: str | None = None) -> list[dict[str, Any]]:
        """Explain routing choices and candidates for diagnostics without exposing credentials."""
        candidates = self.route_candidates(task_id)
        task = self._task_registry.get(task_id)
        return [
            {
                "task": task.task_id,
                "provider": c.provider.name,
                "model": c.provider.model,
                "capabilities": [cap.value for cap in c.model.capabilities],
                "is_healthy": c.is_healthy,
                "cooldown_seconds": c.remaining_cooldown,
                "status": c.status_note,
            }
            for c in candidates
        ]


# Global router
_global_router = HybridRouter()


def get_hybrid_router() -> HybridRouter:
    return _global_router
