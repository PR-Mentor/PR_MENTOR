"""Validated Stage 6 worker configuration; unset values use conservative limits."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.test_execution.contracts import ExecutionResourceLimits

_IMAGE_DIGEST = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ExecutionSettings:
    image_digest: str
    limits: ExecutionResourceLimits
    docker_executable: str = "docker"


def load_execution_settings(
    environ: Mapping[str, str] | None = None,
) -> ExecutionSettings:
    values = os.environ if environ is None else environ
    image = values.get("PR_MENTOR_TEST_EXECUTION_IMAGE", "")
    if image and not _IMAGE_DIGEST.fullmatch(image):
        raise ValueError(
            "PR_MENTOR_TEST_EXECUTION_IMAGE must use an immutable lowercase SHA-256 digest"
        )
    docker = values.get("PR_MENTOR_DOCKER_EXECUTABLE", "docker")
    if not docker or "\x00" in docker:
        raise ValueError(
            "PR_MENTOR_DOCKER_EXECUTABLE must be a nonblank executable name"
        )
    defaults = {
        "timeout_seconds": 30,
        "cpu_count": 1,
        "memory_bytes": 536_870_912,
        "process_count": 32,
        "scratch_bytes": 67_108_864,
        "output_bytes": 128_000,
        "concurrent_executions": 2,
    }
    names = {
        "timeout_seconds": "TIMEOUT_SECONDS",
        "cpu_count": "CPU_COUNT",
        "memory_bytes": "MEMORY_BYTES",
        "process_count": "PROCESS_COUNT",
        "scratch_bytes": "SCRATCH_BYTES",
        "output_bytes": "OUTPUT_BYTES",
        "concurrent_executions": "CONCURRENCY",
    }
    parsed: dict[str, int] = {}
    for field, default in defaults.items():
        raw = values.get("PR_MENTOR_TEST_EXECUTION_" + names[field])
        try:
            parsed[field] = default if raw is None else int(raw, 10)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"PR_MENTOR_TEST_EXECUTION_{names[field]} must be an integer"
            ) from exc
    return ExecutionSettings(
        image_digest=image,
        limits=ExecutionResourceLimits(**parsed),
        docker_executable=docker,
    )
