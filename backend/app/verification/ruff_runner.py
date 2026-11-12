"""Run Ruff in JSON mode and return parsed, tool-native findings."""

import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.verification.analyzer_common import (
    MAX_ANALYZER_TIMEOUT_SECONDS,
    MAX_TOOL_FINDINGS,
    MAX_TOOL_OUTPUT_BYTES,
    SourceToolRun,
    config_digest,
    run_tool_process,
    tool_version,
    validate_source_input,
    write_analysis_source,
)
from app.verification.bounded_process import BoundedProcessError

from .path_validation import validate_python_files


class ToolExecutionError(RuntimeError):
    """A static-analysis tool could not run or returned invalid output."""


def _path(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return Path(value.replace("\\", "/")).as_posix()


def _positive_line(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def run_ruff(
    paths: Sequence[str],
    timeout: float = 30,
    executable: str = "ruff",
    *,
    root: str | Path,
) -> list[dict[str, Any]]:
    """Run Ruff on validated checkout files without a shell.

    Ruff exit 1 means findings were detected. Absolute validated paths cannot
    be interpreted as options, including files whose names begin with ``-``.
    """
    target_paths = validate_python_files(paths, root)
    command = [executable, "check", "--output-format", "json", *target_paths]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise ToolExecutionError(f"Ruff executable not found: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(f"Ruff timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise ToolExecutionError(f"Ruff could not be started: {exc}") from exc
    if completed.returncode not in (0, 1):
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        raise ToolExecutionError(
            f"Ruff failed with exit code {completed.returncode}: {stderr.strip()}"
        )
    try:
        data = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ToolExecutionError("Ruff returned malformed JSON") from exc
    if not isinstance(data, list):
        raise ToolExecutionError("Ruff JSON output must be a list")
    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        location = item.get("location")
        if not isinstance(location, dict):
            location = {}
        code = item.get("code")
        message = item.get("message")
        source = item.get("source_code")
        if not isinstance(source, str):
            source = item.get("cell")
        results.append(
            {
                "file_path": _path(item.get("filename")),
                "line": _positive_line(location.get("row")),
                "rule_id": code if isinstance(code, str) else None,
                "message": message if isinstance(message, str) else "",
                "evidence": source if isinstance(source, str) else None,
            }
        )
    return results


def run_ruff_source(
    source: str,
    file_path: str,
    *,
    timeout: float = MAX_ANALYZER_TIMEOUT_SECONDS,
    max_output_bytes: int = 1_000_000,
    python: str | None = None,
) -> SourceToolRun:
    """Run isolated Ruff against one exact source string with bounded I/O."""
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    max_output_bytes = min(max_output_bytes, MAX_TOOL_OUTPUT_BYTES)
    source_bytes = validate_source_input(source, file_path)
    started = datetime.now(UTC)
    executable = python or sys.executable
    configuration = {
        "tool": "ruff",
        "mode": "check",
        "isolated": True,
        "cache": False,
        "output_format": "json",
        "default_ruleset": "ruff-defaults",
        "target": "single-python-file",
    }
    digest = config_digest(configuration)
    with tempfile.TemporaryDirectory(prefix="prmentor-ruff-") as temp_name:
        target = write_analysis_source(temp_name, source_bytes)
        version = tool_version("ruff", "ruff", timeout=timeout, python=executable)
        stdout, stderr, returncode, elapsed = run_tool_process(
            [
                executable,
                "-m",
                "ruff",
                "check",
                "--isolated",
                "--no-cache",
                "--output-format",
                "json",
                str(target),
            ],
            cwd=temp_name,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
    if returncode not in (0, 1):
        raise ToolExecutionError(f"Ruff exited with unexpected status {returncode}.")
    try:
        data = json.loads(stdout.decode("utf-8", errors="strict"))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise BoundedProcessError(
            "Ruff returned malformed JSON.", inconclusive=True
        ) from exc
    if not isinstance(data, list):
        raise BoundedProcessError(
            "Ruff JSON output was not a finding list.", inconclusive=True
        )
    if len(data) > MAX_TOOL_FINDINGS:
        raise BoundedProcessError(
            "Ruff exceeded the finding count limit.", inconclusive=True
        )
    if any(not isinstance(item, dict) for item in data):
        raise BoundedProcessError(
            "Ruff JSON contained a malformed finding record.", inconclusive=True
        )
    findings = tuple(_parse_ruff_finding(item, file_path) for item in data)
    return SourceToolRun(
        tool_name="ruff",
        tool_version=version,
        configuration_sha256=digest,
        findings=findings,
        stdout=stdout,
        stderr=stderr,
        started_at=started.isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
        elapsed_seconds=elapsed,
    )


def _parse_ruff_finding(item: dict[str, Any], file_path: str) -> dict[str, Any]:
    location = item.get("location")
    code = item.get("code")
    message = item.get("message")
    if not isinstance(location, dict):
        raise BoundedProcessError(
            "Ruff JSON finding lacked required identity or location fields.",
            inconclusive=True,
        )
    if (
        not isinstance(item.get("filename"), str)
        or not item["filename"]
        or not isinstance(code, str)
        or not code
        or not isinstance(message, str)
    ):
        raise BoundedProcessError(
            "Ruff JSON finding lacked required identity or location fields.",
            inconclusive=True,
        )
    if _positive_line(location.get("row")) is None:
        raise BoundedProcessError(
            "Ruff JSON finding lacked required identity or location fields.",
            inconclusive=True,
        )
    end_location = item.get("end_location")
    if not isinstance(end_location, dict):
        end_location = {}
    evidence = item.get("source_code")
    if not isinstance(evidence, str):
        evidence = item.get("cell") if isinstance(item.get("cell"), str) else None
    return {
        "tool": "ruff",
        "file_path": file_path,
        "line": _positive_line(location.get("row")),
        "column": _positive_line(location.get("column")),
        "end_line": _positive_line(end_location.get("row")),
        "end_column": _positive_line(end_location.get("column")),
        "rule_id": code,
        "message": message,
        "severity": None,
        "confidence": None,
        "test_name": None,
        "evidence": evidence,
        "context": evidence,
        "source": "ruff",
    }
