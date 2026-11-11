"""Shared execution boundaries and metadata for source analyzers."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.github_service.pull_requests import (
    MAX_SOURCE_BYTES,
    validate_repository_python_path,
)
from app.verification.bounded_process import (
    DEFAULT_MAX_OUTPUT_BYTES,
    BoundedProcessError,
    run_bounded_process,
)

MAX_ANALYZER_TIMEOUT_SECONDS = 20.0
MAX_TOOL_OUTPUT_BYTES = 1_000_000
MAX_TOOL_FINDINGS = 2_000


@dataclass(frozen=True)
class SourceToolRun:
    tool_name: str
    tool_version: str
    configuration_sha256: str
    findings: tuple[dict[str, Any], ...]
    stdout: bytes
    stderr: bytes
    started_at: str
    completed_at: str
    elapsed_seconds: float


def controlled_environment(temp_dir: str | Path) -> dict[str, str]:
    """Return an analyzer-only environment without application credentials."""
    root = str(Path(temp_dir).resolve())
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "TEMP": root,
        "TMP": root,
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def config_digest(configuration: dict[str, Any]) -> str:
    encoded = json.dumps(
        configuration, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_source_input(source: str, file_path: str) -> bytes:
    validate_repository_python_path(file_path)
    if not isinstance(source, str) or "\x00" in source:
        raise ValueError("Source must be valid UTF-8 text without NUL characters.")
    content = source.encode("utf-8", errors="strict")
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Source exceeds the Stage 2 source size limit.")
    return content


def write_analysis_source(directory: str | Path, content: bytes) -> Path:
    """Write only into an isolated temporary directory with a fixed safe name."""
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Analyzer workspace must be a directory.")
    target = root / "analysis_target.py"
    target.write_bytes(content)
    if target.resolve(strict=True).parent != root:
        raise ValueError("Analyzer source escaped its temporary workspace.")
    return target


def tool_version(
    tool_name: str, module_name: str, *, timeout: float, python: str | None = None
) -> str:
    """Read a bounded version string from the installed Python module tool."""
    executable = python or sys.executable
    with tempfile.TemporaryDirectory(
        prefix=f"prmentor-{tool_name}-version-"
    ) as temp_name:
        result = run_bounded_process(
            [executable, "-m", module_name, "--version"],
            cwd=temp_name,
            env=controlled_environment(temp_name),
            timeout=timeout,
            max_stdout_bytes=2_048,
            max_stderr_bytes=2_048,
        )
    if result.returncode != 0:
        raise BoundedProcessError(
            f"{tool_name} version query returned {result.returncode}."
        )
    try:
        version = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise BoundedProcessError(
            f"{tool_name} returned an invalid version string.", inconclusive=True
        ) from exc
    if not version or len(version) > 256:
        raise BoundedProcessError(
            f"{tool_name} returned an invalid version string.", inconclusive=True
        )
    return version


def run_tool_process(
    command: list[str],
    *,
    cwd: str | Path,
    timeout: float,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[bytes, bytes, int, float]:
    result = run_bounded_process(
        command,
        cwd=cwd,
        env=controlled_environment(cwd),
        timeout=min(max(timeout, 0.05), MAX_ANALYZER_TIMEOUT_SECONDS),
        max_stdout_bytes=max_output_bytes,
        max_stderr_bytes=max_output_bytes,
    )
    return result.stdout, result.stderr, result.returncode, result.elapsed_seconds
