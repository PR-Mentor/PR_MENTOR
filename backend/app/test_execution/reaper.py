"""Crash recovery for stale Stage 6 containers and host-side test artifacts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONTAINER_PREFIX = "prmentor-test-"
CONTAINER_LABEL = "org.prmentor.execution"
CONTAINER_VALUE = "phase4-stage6"
TEMP_PREFIX = "prmentor-stage6-"
MAX_CAPTURE_BYTES = 64_000


def _docker(args: list[str], *, timeout: float = 8.0) -> str:
    try:
        result = subprocess.run(
            [os.getenv("PR_MENTOR_DOCKER_EXECUTABLE", "docker"), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Docker reaper command was unavailable or timed out."
        ) from exc
    if len(result.stdout) + len(result.stderr) > MAX_CAPTURE_BYTES or result.returncode:
        raise RuntimeError("Docker reaper command failed or exceeded its output bound.")
    return result.stdout.strip()


def reap_orphan_containers(
    *, older_than_seconds: int = 120, now: datetime | None = None
) -> int:
    if older_than_seconds < 60:
        raise ValueError("orphan age threshold must be at least 60 seconds")
    current = now or datetime.now(UTC)
    cutoff = current.timestamp() - older_than_seconds
    raw_ids = _docker(
        ["ps", "-aq", "--filter", f"label={CONTAINER_LABEL}={CONTAINER_VALUE}"]
    )
    ids = [
        item
        for item in raw_ids.splitlines()
        if re.fullmatch(r"[a-fA-F0-9]{12,64}", item)
    ]
    reaped = 0
    for container_id in ids[:256]:
        raw = _docker(["inspect", "--format", "{{json .}}", container_id])
        try:
            item: Any = json.loads(raw)
            if not isinstance(item, dict):
                continue
            name = str(item.get("Name", "")).lstrip("/")
            labels = item.get("Config", {}).get("Labels", {})
            created = datetime.fromisoformat(
                str(item.get("Created", "")).replace("Z", "+00:00")  # noqa: FURB162 - Docker emits RFC 3339 Z timestamps.
            )
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
        except (ValueError, TypeError, AttributeError):
            continue
        # Both the dedicated runtime label and the generated name are required.
        if labels.get(CONTAINER_LABEL) != CONTAINER_VALUE or not name.startswith(
            CONTAINER_PREFIX
        ):
            continue
        if created.timestamp() >= cutoff:
            continue
        _docker(["rm", "--force", container_id], timeout=12.0)
        reaped += 1
    return reaped


def reap_stale_artifacts(
    *, older_than_seconds: int = 3600, now: datetime | None = None
) -> int:
    if older_than_seconds < 60:
        raise ValueError("artifact age threshold must be at least 60 seconds")
    root = Path(os.getenv("PR_MENTOR_STAGE6_TEMP_ROOT", tempfile.gettempdir())).resolve(
        strict=True
    )
    cutoff = (now or datetime.now(UTC)).timestamp() - older_than_seconds
    removed = 0
    for candidate in root.glob(TEMP_PREFIX + "*"):
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            resolved = candidate.resolve(strict=True)
            if resolved.parent != root or not resolved.name.startswith(TEMP_PREFIX):
                continue
            if resolved.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(resolved)
            removed += 1
        except (OSError, RuntimeError):
            # A concurrent cleanup or inaccessible path is retried next pass.
            continue
    return removed


def run_reaper() -> dict[str, int]:
    return {
        "containers_removed": reap_orphan_containers(),
        "artifacts_removed": reap_stale_artifacts(),
    }


if __name__ == "__main__":
    print(json.dumps(run_reaper(), sort_keys=True))
