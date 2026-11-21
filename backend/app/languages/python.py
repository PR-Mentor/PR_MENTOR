"""Python language adapter wrapping Ruff and Bandit."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

from app.languages.base import (
    LanguageAdapter,
    LanguageCapabilityStatus,
    ToolCapability,
)
from app.languages.common import (
    build_observation,
    build_review_finding_from_raw,
    write_language_sources,
)
from app.schemas.finding import Finding, ReviewFinding
from app.schemas.issue import FindingObservation
from app.services.repository_intelligence import summarize_python_sources
from app.verification.bandit_runner import run_bandit
from app.verification.normalizer import normalize_bandit, normalize_ruff
from app.verification.path_validation import PathValidationError
from app.verification.ruff_runner import ToolExecutionError, run_ruff


def _resolve_tool_path(tool_name: str) -> str:
    found = shutil.which(tool_name)
    if found:
        return found
    scripts_dir = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")
    candidate = scripts_dir / (f"{tool_name}.exe" if sys.platform == "win32" else tool_name)
    if candidate.is_file():
        return str(candidate)
    return tool_name


def _get_runner(name: str):
    import sys
    orch = sys.modules.get("app.services.review_orchestrator")
    attr = f"run_{name}"
    if orch and hasattr(orch, attr):
        return getattr(orch, attr)
    return run_ruff if name == "ruff" else run_bandit


class PythonAdapter(LanguageAdapter):
    """Adapter for Python source analysis using Ruff and Bandit."""

    @property
    def language_id(self) -> str:
        return "python"

    @property
    def display_name(self) -> str:
        return "Python"

    @property
    def capability_status(self) -> LanguageCapabilityStatus:
        return "production"

    @property
    def source_extensions(self) -> set[str]:
        return {".py", ".pyi"}

    def detect_capabilities(self) -> dict[str, ToolCapability]:
        capabilities: dict[str, ToolCapability] = {}

        # Ruff
        ruff_path = _resolve_tool_path("ruff")
        if ruff_path and (shutil.which(ruff_path) or Path(ruff_path).is_file()):
            capabilities["ruff"] = ToolCapability("ruff", "available", version="available")
        else:
            capabilities["ruff"] = ToolCapability("ruff", "unavailable", reason="ruff binary not found in PATH")

        # Bandit
        bandit_path = _resolve_tool_path("bandit")
        if bandit_path and (shutil.which(bandit_path) or Path(bandit_path).is_file()):
            capabilities["bandit"] = ToolCapability("bandit", "available", version="available")
        else:
            capabilities["bandit"] = ToolCapability("bandit", "unavailable", reason="bandit binary not found in PATH")

        return capabilities

    async def run_static_analysis(
        self,
        source_files: list[tuple[str, str]],
        workspace_dir: Path,
    ) -> tuple[list[ReviewFinding], list[FindingObservation], list[dict[str, Any]]]:
        findings: list[ReviewFinding] = []
        observations: list[FindingObservation] = []
        tool_results: list[dict[str, Any]] = []

        if not source_files:
            return findings, observations, tool_results

        targets = write_language_sources(workspace_dir, source_files)
        if not targets:
            return findings, observations, tool_results

        target_paths = [p.relative_to(workspace_dir).as_posix() for p in targets]
        for name, normalizer in (("ruff", normalize_ruff), ("bandit", normalize_bandit)):
            runner = _get_runner(name)
            kwargs: dict[str, Any] = {"root": workspace_dir}
            resolved = _resolve_tool_path(name)
            if resolved != name:
                import inspect
                try:
                    sig = inspect.signature(runner)
                    if "executable" in sig.parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                    ):
                        kwargs["executable"] = resolved
                except (ValueError, TypeError):
                    pass
            try:
                raw_results = await asyncio.to_thread(runner, target_paths, **kwargs)
                legacy_findings: list[Finding] = normalizer(raw_results)
                for item in legacy_findings:
                    is_sec = name == "bandit" or bool(item.rule_id and item.rule_id.upper().startswith("S"))
                    sev = "high" if item.severity == "critical" else item.severity
                    if sev not in {"low", "medium", "high"}:
                        sev = "medium"
                    rf = build_review_finding_from_raw(
                        tool_name=name,
                        file_path=item.file_path,
                        line=item.line,
                        rule_id=item.rule_id,
                        message=item.message or "Python static analyzer diagnostic.",
                        severity=sev,
                        category="security" if is_sec else "code_quality",
                        evidence=item.evidence,
                        root=workspace_dir,
                    )
                    findings.append(rf)
                    observations.append(build_observation(rf, tool_name=name, rule_id=item.rule_id))

                rendered_findings = [
                    f.model_dump(mode="json")
                    for f in findings
                    if f.title.lower().startswith(name.lower())
                ]
                tool_results.append({
                    "tool": name,
                    "status": "completed",
                    "executed": True,
                    "findings": rendered_findings,
                })
            except (ToolExecutionError, PathValidationError, OSError):
                tool_results.append({
                    "tool": name,
                    "status": "unavailable",
                    "executed": False,
                    "reason": f"{name.title()} execution failed or was unavailable; local execution details are not exposed.",
                })

        return findings, observations, tool_results

    def summarize_sources(self, source_files: list[tuple[str, str]]) -> dict[str, Any]:
        return summarize_python_sources(source_files)
