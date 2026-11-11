"""Bounded mypy analysis of one immutable Python source snapshot."""

from __future__ import annotations

import re
import sys
import tempfile
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
from app.verification.ruff_runner import ToolExecutionError

_DIAGNOSTIC = re.compile(
    r"^(?P<path>.*?\.py):(?P<line>[1-9]\d*)(?::(?P<column>[1-9]\d*))?: "
    r"(?P<severity>error|warning|note): (?P<message>.*?)(?: \[(?P<code>[a-z0-9_-]+)\])?$"
)
_INCOMPLETE_CODES = frozenset({"import-not-found", "import-untyped"})
_UNTRUSTED_INLINE_DIRECTIVE = re.compile(
    rb"(?im)(?:^\s*#\s*mypy\s*:|#\s*type\s*:\s*ignore\b)"
)


def mypy_configuration() -> dict[str, Any]:
    """Return the complete fixed policy; repository mypy configuration is ignored."""
    return {
        "tool": "mypy",
        "python_version": "3.12",
        "strict": False,
        "check_untyped_defs": True,
        "no_site_packages": True,
        "follow_imports": "normal",
        "ignore_missing_imports": False,
        "incremental": False,
        "plugins": [],
        "context": "single-file-snapshot",
        "output_format": "text",
        "show_error_codes": True,
        "show_column_numbers": True,
        "error_summary": False,
    }


def run_mypy_source(
    source: str,
    file_path: str,
    *,
    timeout: float = MAX_ANALYZER_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_TOOL_OUTPUT_BYTES,
    python: str | None = None,
) -> SourceToolRun:
    """Run mypy without loading project config, plugins, or third-party packages."""
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes < 1
    ):
        raise ValueError("max_output_bytes must be a positive integer")
    max_output_bytes = min(max_output_bytes, MAX_TOOL_OUTPUT_BYTES)
    source_bytes = validate_source_input(source, file_path)
    if _UNTRUSTED_INLINE_DIRECTIVE.search(source_bytes):
        raise BoundedProcessError(
            "mypy source contains inline configuration or type-ignore directives; analysis is incomplete.",
            inconclusive=True,
        )
    started = datetime.now(UTC)
    executable = python or sys.executable
    configuration = mypy_configuration()
    digest = config_digest(configuration)
    config_text = (
        "[mypy]\n"
        "python_version = 3.12\n"
        "strict = False\n"
        "check_untyped_defs = True\n"
        "no_site_packages = True\n"
        "follow_imports = normal\n"
        "ignore_missing_imports = False\n"
        "incremental = False\n"
    )
    with tempfile.TemporaryDirectory(prefix="prmentor-mypy-") as temp_name:
        root = Path(temp_name).resolve(strict=True)
        target = write_analysis_source(root, source_bytes)
        config_path = root / "mypy.ini"
        config_path.write_text(config_text, encoding="utf-8")
        version = tool_version("mypy", "mypy", timeout=timeout, python=executable)
        stdout, stderr, returncode, elapsed = run_tool_process(
            [
                executable,
                "-m",
                "mypy",
                "--config-file",
                str(config_path),
                "--no-incremental",
                "--no-site-packages",
                "--check-untyped-defs",
                "--show-error-codes",
                "--show-column-numbers",
                "--no-error-summary",
                str(target),
            ],
            cwd=root,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
    if returncode not in (0, 1, 2):
        raise ToolExecutionError(f"mypy exited with unexpected status {returncode}.")
    findings = _parse_mypy_output(stdout, stderr, file_path)
    if returncode in (1, 2) and not findings:
        raise BoundedProcessError(
            "mypy reported failure without parseable diagnostics.", inconclusive=True
        )
    return SourceToolRun(
        tool_name="mypy",
        tool_version=version,
        configuration_sha256=digest,
        findings=tuple(findings),
        stdout=stdout,
        stderr=stderr,
        started_at=started.isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
        elapsed_seconds=elapsed,
    )


def _parse_mypy_output(
    stdout: bytes, stderr: bytes, file_path: str
) -> list[dict[str, Any]]:
    try:
        output = stdout.decode("utf-8", errors="strict")
        stderr_text = stderr.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BoundedProcessError(
            "mypy returned invalid UTF-8 output.", inconclusive=True
        ) from exc
    findings: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        match = _DIAGNOSTIC.match(raw_line)
        if not match:
            raise BoundedProcessError(
                "mypy output contained an unrecognized diagnostic.", inconclusive=True
            )
        if match.group("severity") == "note":
            continue
        code = match.group("code") or "mypy"
        message = match.group("message").strip()
        incomplete = (
            code in _INCOMPLETE_CODES or "missing library stubs" in message.casefold()
        )
        findings.append(
            {
                "tool": "mypy",
                "file_path": file_path,
                "line": int(match.group("line")),
                "column": int(match.group("column")) if match.group("column") else None,
                "end_line": int(match.group("line")),
                "end_column": None,
                "rule_id": code,
                "message": message,
                "severity": match.group("severity"),
                "confidence": None,
                "test_name": None,
                "evidence": raw_line[:2_000],
                "context": None,
                "source": "mypy",
                "analysis_incomplete": incomplete,
            }
        )
        if len(findings) > MAX_TOOL_FINDINGS:
            raise BoundedProcessError(
                "mypy exceeded the finding count limit.", inconclusive=True
            )
    if stderr_text.strip():
        # Warnings from mypy may affect trust in the analysis even if diagnostics parse.
        raise BoundedProcessError(
            "mypy emitted unexpected stderr output.", inconclusive=True
        )
    return findings
