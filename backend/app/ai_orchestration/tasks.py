"""Task definitions and registry for AI-powered workflows."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from app.ai_orchestration.models import ModelCapability


@dataclass
class TaskDefinition:
    task_id: str
    description: str
    required_capabilities: set[ModelCapability] = field(default_factory=set)
    default_timeout_seconds: float = 20.0
    default_max_retries: int = 1
    max_response_bytes: int = 1_000_000
    expected_output_type: str = "json_object"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "required_capabilities": [c.value for c in self.required_capabilities],
            "default_timeout_seconds": self.default_timeout_seconds,
            "default_max_retries": self.default_max_retries,
            "max_response_bytes": self.max_response_bytes,
            "expected_output_type": self.expected_output_type,
        }


STANDARD_TASKS: list[TaskDefinition] = [
    TaskDefinition(
        task_id="code_review",
        description="Analyzes pull request diffs and analyzer findings to surface candidate review findings.",
        required_capabilities={ModelCapability.JSON_OUTPUT, ModelCapability.CODE_REVIEW},
        default_timeout_seconds=25.0,
        default_max_retries=1,
        max_response_bytes=1_000_000,
    ),
    TaskDefinition(
        task_id="issue_explanation",
        description="Generates beginner-friendly, evidence-grounded educational explanations for canonical issues.",
        required_capabilities={ModelCapability.JSON_OUTPUT, ModelCapability.CODE_REVIEW},
        default_timeout_seconds=20.0,
        default_max_retries=1,
        max_response_bytes=500_000,
    ),
    TaskDefinition(
        task_id="fix_generation",
        description="Proposes bounded, syntax-valid code refactorings that eliminate identified issues.",
        required_capabilities={ModelCapability.JSON_OUTPUT, ModelCapability.CODE_GENERATION},
        default_timeout_seconds=30.0,
        default_max_retries=1,
        max_response_bytes=500_000,
    ),
    TaskDefinition(
        task_id="test_generation",
        description="Generates unit tests verifying whether proposed fixes prevent reproduced vulnerabilities.",
        required_capabilities={ModelCapability.JSON_OUTPUT, ModelCapability.TEST_GENERATION},
        default_timeout_seconds=30.0,
        default_max_retries=1,
        max_response_bytes=500_000,
    ),
    TaskDefinition(
        task_id="semantic_review",
        description="Provides independent second-opinion semantic risk assessment of proposed fixes.",
        required_capabilities={ModelCapability.JSON_OUTPUT, ModelCapability.CODE_REVIEW},
        default_timeout_seconds=20.0,
        default_max_retries=1,
        max_response_bytes=250_000,
    ),
    TaskDefinition(
        task_id="learning_lab",
        description="Generates PR-connected educational concepts and practical defensive coding exercises.",
        required_capabilities={ModelCapability.JSON_OUTPUT, ModelCapability.CODE_GENERATION},
        default_timeout_seconds=25.0,
        default_max_retries=1,
        max_response_bytes=500_000,
    ),
    TaskDefinition(
        task_id="documentation",
        description="Provides context-aware guidance and documentation summaries.",
        required_capabilities={ModelCapability.JSON_OUTPUT},
        default_timeout_seconds=15.0,
        default_max_retries=1,
        max_response_bytes=250_000,
    ),
]


class TaskRegistry:
    """Registry of AI tasks and operational constraints."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskDefinition] = {t.task_id: t for t in STANDARD_TASKS}

    def register(self, task: TaskDefinition) -> None:
        self._tasks[task.task_id.lower()] = task

    def get(self, task_id: str | None) -> TaskDefinition:
        if not task_id:
            # Default generic task
            return self._tasks.get(
                "code_review",
                TaskDefinition(
                    task_id="default",
                    description="General purpose AI review task",
                    required_capabilities={ModelCapability.JSON_OUTPUT},
                ),
            )
        normalized = task_id.lower().replace("-", "_")
        if normalized in self._tasks:
            return self._tasks[normalized]
        # Synthesize on demand
        synthesized = TaskDefinition(
            task_id=normalized,
            description=f"Dynamically registered task: {normalized}",
            required_capabilities={ModelCapability.JSON_OUTPUT},
        )
        self.register(synthesized)
        return synthesized

    def list_tasks(self) -> list[TaskDefinition]:
        return list(self._tasks.values())


# Global singleton
_global_task_registry = TaskRegistry()


def get_task_registry() -> TaskRegistry:
    return _global_task_registry
