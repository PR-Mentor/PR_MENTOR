"""Run Bandit in JSON mode and return parsed, tool-native findings."""

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
from .ruff_runner import ToolExecutionError


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


def run_bandit(
    paths: Sequence[str],
    timeout: float = 30,
    executable: str = "bandit",
    *,
    root: str | Path,
) -> list[dict[str, Any]]:
    """Run Bandit on validated checkout files without a shell.

    Exit 1 is a normal findings result. Absolute validated paths cannot be
    interpreted as options, including files whose names begin with ``-``.
    """
    target_paths = validate_python_files(paths, root)
    command = [executable, "-f", "json", *target_paths]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise ToolExecutionError(f"Bandit executable not found: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(f"Bandit timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise ToolExecutionError(f"Bandit could not be started: {exc}") from exc
    if completed.returncode not in (0, 1):
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        raise ToolExecutionError(
            f"Bandit failed with exit code {completed.returncode}: {stderr.strip()}"
        )
    try:
        data = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ToolExecutionError("Bandit returned malformed JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ToolExecutionError("Bandit JSON output must contain a results list")
    results = []
    for item in data["results"]:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if isinstance(code, str):
            evidence = code.rstrip("\r\n") or None
        elif isinstance(code, list):
            lines = [line.rstrip("\r\n") for line in code if isinstance(line, str)]
            evidence = "\n".join(lines).rstrip("\r\n") or None
        else:
            evidence = None
        rule_id = item.get("test_id")
        message = item.get("issue_text")
        severity = item.get("issue_severity")
        results.append(
            {
                "file_path": _path(item.get("filename")),
                "line": _positive_line(item.get("line_number")),
                "rule_id": rule_id if isinstance(rule_id, str) else None,
                "message": message if isinstance(message, str) else "",
                "severity": severity if isinstance(severity, str) else None,
                "evidence": evidence,
            }
        )
    return results


def run_bandit_source(
    source: str,
    file_path: str,
    *,
    timeout: float = MAX_ANALYZER_TIMEOUT_SECONDS,
    max_output_bytes: int = 1_000_000,
    python: str | None = None,
) -> SourceToolRun:
    """Run Bandit with a fixed explicit configuration and bounded capture."""
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    max_output_bytes = min(max_output_bytes, MAX_TOOL_OUTPUT_BYTES)
    source_bytes = validate_source_input(source, file_path)
    started = datetime.now(UTC)
    executable = python or sys.executable
    configuration = {
        "tool": "bandit",
        "config": {},
        "format": "json",
        "severity_threshold": "low",
        "confidence_threshold": "low",
        "target": "single-python-file",
    }
    digest = config_digest(configuration)
    with tempfile.TemporaryDirectory(prefix="prmentor-bandit-") as temp_name:
        target = write_analysis_source(temp_name, source_bytes)
        config_path = Path(temp_name) / "bandit.yaml"
        config_path.write_text("{}\n", encoding="utf-8")
        version = tool_version("bandit", "bandit", timeout=timeout, python=executable)
        stdout, stderr, returncode, elapsed = run_tool_process(
            [
                executable,
                "-m",
                "bandit",
                "-f",
                "json",
                "-c",
                str(config_path),
                "-l",
                "-i",
                str(target),
            ],
            cwd=temp_name,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
    if returncode not in (0, 1):
        raise ToolExecutionError(f"Bandit exited with unexpected status {returncode}.")
    try:
        data = json.loads(stdout.decode("utf-8", errors="strict"))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise BoundedProcessError(
            "Bandit returned malformed JSON.", inconclusive=True
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise BoundedProcessError(
            "Bandit JSON output lacked its findings list.", inconclusive=True
        )
    if len(data["results"]) > MAX_TOOL_FINDINGS:
        raise BoundedProcessError(
            "Bandit exceeded the finding count limit.", inconclusive=True
        )
    if any(not isinstance(item, dict) for item in data["results"]):
        raise BoundedProcessError(
            "Bandit JSON contained a malformed finding record.", inconclusive=True
        )
    findings = tuple(_parse_bandit_finding(item, file_path) for item in data["results"])
    return SourceToolRun(
        tool_name="bandit",
        tool_version=version,
        configuration_sha256=digest,
        findings=findings,
        stdout=stdout,
        stderr=stderr,
        started_at=started.isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
        elapsed_seconds=elapsed,
    )


def _parse_bandit_finding(item: dict[str, Any], file_path: str) -> dict[str, Any]:
    required_text_fields = ("filename", "test_id", "test_name", "issue_text")
    if any(not isinstance(item.get(field), str) or not item[field] for field in required_text_fields):
        raise BoundedProcessError(
            "Bandit JSON finding lacked required identity fields.", inconclusive=True
        )
    if _positive_line(item.get("line_number")) is None:
        raise BoundedProcessError(
            "Bandit JSON finding lacked a valid source line.", inconclusive=True
        )
    if item.get("issue_severity") not in {"LOW", "MEDIUM", "HIGH"}:
        raise BoundedProcessError(
            "Bandit JSON finding had an invalid severity.", inconclusive=True
        )
    if item.get("issue_confidence") not in {"LOW", "MEDIUM", "HIGH"}:
        raise BoundedProcessError(
            "Bandit JSON finding had an invalid confidence.", inconclusive=True
        )
    raw_code = item.get("code")
    if isinstance(raw_code, str):
        evidence = raw_code.rstrip("\r\n") or None
    elif isinstance(raw_code, list):
        evidence = (
            "\n".join(
                line.rstrip("\r\n") for line in raw_code if isinstance(line, str)
            ).rstrip("\r\n")
            or None
        )
    else:
        evidence = None
    raw_range = item.get("line_range")
    valid_range = (
        [
            line
            for line in raw_range
            if isinstance(line, int) and not isinstance(line, bool) and line > 0
        ]
        if isinstance(raw_range, list)
        else []
    )
    start_line = _positive_line(item.get("line_number"))
    end_line = max(valid_range) if valid_range else start_line
    return {
        "tool": "bandit",
        "file_path": file_path,
        "line": start_line,
        "column": _positive_line(item.get("col_offset")),
        "end_line": end_line,
        "end_column": _positive_line(item.get("end_col_offset")),
        "rule_id": item.get("test_id")
        if isinstance(item.get("test_id"), str)
        else None,
        "message": item.get("issue_text")
        if isinstance(item.get("issue_text"), str)
        else "",
        "severity": item.get("issue_severity")
        if isinstance(item.get("issue_severity"), str)
        else None,
        "confidence": item.get("issue_confidence")
        if isinstance(item.get("issue_confidence"), str)
        else None,
        "test_name": item.get("test_name")
        if isinstance(item.get("test_name"), str)
        else None,
        "evidence": evidence,
        "context": evidence,
        "source": "bandit",
    }
