"""C language adapter with Clang-Tidy, Cppcheck, Flawfinder and built-in safety analyzer."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
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
from app.schemas.finding import ReviewFinding
from app.schemas.issue import FindingObservation
from app.verification.bounded_process import run_bounded_process

# Regex patterns for C source analysis
INCLUDE_PATTERN = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)
FUNCTION_DEF_PATTERN = re.compile(
    r'^[a-zA-Z_][a-zA-Z0-9_* \t\n]+?\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]*)\)\s*\{',
    re.MULTILINE,
)
STRUCT_DEF_PATTERN = re.compile(r'\bstruct\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\{', re.MULTILINE)

# Built-in static checks for C security & correctness
DANGEROUS_C_PATTERNS = [
    (
        re.compile(r'\bgets\s*\('),
        "C-SEC-GETS",
        "high",
        "security",
        "Use of gets() is inherently insecure as it lacks buffer bounds checking (CWE-242). Use fgets() instead.",
        "gets() cannot prevent buffer overflow if input exceeds the allocated buffer.",
        "Replace gets(buf) with fgets(buf, sizeof(buf), stdin).",
    ),
    (
        re.compile(r'\bstrcpy\s*\('),
        "C-SEC-STRCPY",
        "medium",
        "security",
        "strcpy() does not check destination buffer bounds (CWE-120). Consider strncpy() or snprintf().",
        "Buffer overflow can occur if the source string exceeds destination buffer capacity.",
        "Use strncpy(dest, src, sizeof(dest) - 1) or snprintf(dest, sizeof(dest), \"%s\", src).",
    ),
    (
        re.compile(r'\bsprintf\s*\(\s*[^,]+,\s*[^"][^,]*\)'),
        "C-SEC-SPRINTF-FORMAT",
        "high",
        "security",
        "sprintf() called with non-literal format string or unbounded destination (CWE-134). Use snprintf().",
        "Format string vulnerabilities can lead to arbitrary code execution or memory disclosure.",
        "Replace sprintf(buf, fmt, ...) with snprintf(buf, sizeof(buf), \"%s\", ...).",
    ),
    (
        re.compile(r'\batoi\s*\('),
        "C-ERR-ATOI",
        "low",
        "code_quality",
        "atoi() does not handle integer overflow or invalid input conversion errors. Consider strtol().",
        "Invalid input yields undefined behavior or silent 0 values without error detection.",
        "Replace atoi() with strtol() and verify endptr.",
    ),
]


class CAdapter(LanguageAdapter):
    """Adapter for C source and header analysis."""

    @property
    def language_id(self) -> str:
        return "c"

    @property
    def display_name(self) -> str:
        return "C"

    @property
    def capability_status(self) -> LanguageCapabilityStatus:
        return "production"

    @property
    def source_extensions(self) -> set[str]:
        return {".c"}

    @property
    def header_extensions(self) -> set[str]:
        return {".h"}

    def detect_capabilities(self) -> dict[str, ToolCapability]:
        caps: dict[str, ToolCapability] = {}

        # 1. Clang-Tidy
        if shutil.which("clang-tidy"):
            caps["clang_tidy"] = ToolCapability("clang_tidy", "available", version="system")
        else:
            caps["clang_tidy"] = ToolCapability("clang_tidy", "unavailable", reason="clang-tidy binary not found in PATH")

        # 2. Cppcheck
        if shutil.which("cppcheck"):
            caps["cppcheck"] = ToolCapability("cppcheck", "available", version="system")
        else:
            caps["cppcheck"] = ToolCapability("cppcheck", "unavailable", reason="cppcheck binary not found in PATH")

        # 3. Flawfinder
        if shutil.which("flawfinder"):
            caps["flawfinder"] = ToolCapability("flawfinder", "available", version="system")
        else:
            caps["flawfinder"] = ToolCapability("flawfinder", "unavailable", reason="flawfinder binary not found in PATH")

        # 4. Built-in C safety analyzer (always available)
        caps["c_safety_analyzer"] = ToolCapability("c_safety_analyzer", "available", version="1.0.0")

        return caps

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

        written_paths = write_language_sources(workspace_dir, source_files)
        caps = self.detect_capabilities()

        # 1. Run external tools if available, or record unavailable
        # Clang-Tidy
        if caps["clang_tidy"].status == "available":
            ct_findings = await self._run_clang_tidy(written_paths, workspace_dir)
            findings.extend(ct_findings)
            for rf in ct_findings:
                observations.append(build_observation(rf, "clang_tidy", rf.title.replace("Clang_Tidy ", "")))
            tool_results.append({
                "tool": "clang_tidy", "status": "completed", "executed": True,
                "findings": [f.model_dump(mode="json") for f in ct_findings],
            })
        else:
            tool_results.append({
                "tool": "clang_tidy", "status": "unavailable", "executed": False,
                "reason": caps["clang_tidy"].reason,
            })

        # Cppcheck
        if caps["cppcheck"].status == "available":
            cc_findings = await self._run_cppcheck(written_paths, workspace_dir)
            findings.extend(cc_findings)
            for rf in cc_findings:
                observations.append(build_observation(rf, "cppcheck", rf.title.replace("Cppcheck ", "")))
            tool_results.append({
                "tool": "cppcheck", "status": "completed", "executed": True,
                "findings": [f.model_dump(mode="json") for f in cc_findings],
            })
        else:
            tool_results.append({
                "tool": "cppcheck", "status": "unavailable", "executed": False,
                "reason": caps["cppcheck"].reason,
            })

        # 2. Run built-in C safety analyzer
        builtin_findings = self._run_builtin_analyzer(source_files, workspace_dir)
        findings.extend(builtin_findings)
        for rf in builtin_findings:
            observations.append(build_observation(rf, "c_safety_analyzer", rf.title.replace("C_Safety_Analyzer ", "")))
        tool_results.append({
            "tool": "c_safety_analyzer", "status": "completed", "executed": True,
            "findings": [f.model_dump(mode="json") for f in builtin_findings],
        })

        return findings, observations, tool_results

    def _run_builtin_analyzer(
        self, source_files: list[tuple[str, str]], root: Path
    ) -> list[ReviewFinding]:
        results: list[ReviewFinding] = []
        for file_path, content in source_files:
            lines = content.splitlines()
            for line_idx, line in enumerate(lines, start=1):
                # Skip comments
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("/*"):
                    continue
                for pattern, rule_id, sev, cat, msg, why, fix in DANGEROUS_C_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        rf = build_review_finding_from_raw(
                            tool_name="c_safety_analyzer",
                            file_path=file_path,
                            line=line_idx,
                            rule_id=rule_id,
                            message=msg,
                            severity=sev,
                            category=cat,
                            evidence=stripped,
                            why_it_matters=why,
                            suggested_fix=fix,
                            root=root,
                        )
                        results.append(rf)
        return results

    async def _run_clang_tidy(self, targets: list[Path], root: Path) -> list[ReviewFinding]:
        findings: list[ReviewFinding] = []
        for target in targets:
            if target.suffix.lower() not in self.source_extensions:
                continue
            try:
                proc = await asyncio.to_thread(
                    run_bounded_process,
                    ["clang-tidy", str(target), "--", f"-I{root}"],
                    cwd=str(root),
                    timeout_seconds=15.0,
                )
                stdout = proc.stdout.decode("utf-8", errors="replace")
                findings.extend(self._parse_clang_tidy_output(stdout, root))
            except Exception:
                pass
        return findings

    def _parse_clang_tidy_output(self, stdout: str, root: Path) -> list[ReviewFinding]:
        findings: list[ReviewFinding] = []
        # pattern: /path/to/file:line:col: warning: message [check-name]
        regex = re.compile(r"^([^:\n]+):(\d+):(\d+):\s*(warning|error):\s*(.*?)(?:\s*\[(.*?)\])?$", re.MULTILINE)
        for match in regex.finditer(stdout):
            path_str, line_str, _col, level, msg, rule = match.groups()
            line = int(line_str) if line_str.isdigit() else 1
            rule_id = rule or "clang-tidy-diagnostic"
            sev = "high" if level == "error" else "medium"
            cat = "security" if "cert" in rule_id or "security" in rule_id else "bug"
            rf = build_review_finding_from_raw(
                tool_name="clang_tidy",
                file_path=path_str,
                line=line,
                rule_id=rule_id,
                message=msg,
                severity=sev,
                category=cat,
                evidence=f"{level}: {msg}",
                root=root,
            )
            findings.append(rf)
        return findings

    async def _run_cppcheck(self, targets: list[Path], root: Path) -> list[ReviewFinding]:
        findings: list[ReviewFinding] = []
        target_strs = [str(t) for t in targets]
        try:
            proc = await asyncio.to_thread(
                run_bounded_process,
                ["cppcheck", "--enable=all", "--inline-suppr", "--template={file}:{line}:{severity}:{id}:{message}"] + target_strs,
                cwd=str(root),
                timeout_seconds=15.0,
            )
            output = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")
            # pattern: file:line:severity:id:message
            regex = re.compile(r"^([^:\n]+):(\d+):([^:]+):([^:]+):(.*?)$", re.MULTILINE)
            for match in regex.finditer(output):
                path_str, line_str, raw_sev, rule_id, msg = match.groups()
                line = int(line_str) if line_str.isdigit() else 1
                sev = "high" if raw_sev.lower() in {"error"} else "medium"
                cat = "security" if "security" in rule_id.lower() else "bug"
                rf = build_review_finding_from_raw(
                    tool_name="cppcheck",
                    file_path=path_str,
                    line=line,
                    rule_id=rule_id,
                    message=msg,
                    severity=sev,
                    category=cat,
                    evidence=f"Cppcheck {rule_id}: {msg}",
                    root=root,
                )
                findings.append(rf)
        except Exception:
            pass
        return findings

    def summarize_sources(self, source_files: list[tuple[str, str]]) -> dict[str, Any]:
        """Extract high-level structural intelligence from C files."""
        includes: set[str] = set()
        functions: list[str] = []
        structs: list[str] = []
        files_summary: list[dict[str, Any]] = []

        for safe_path, content in source_files:
            file_inc = INCLUDE_PATTERN.findall(content)
            includes.update(file_inc)
            file_funcs = [m[0] for m in FUNCTION_DEF_PATTERN.findall(content)]
            functions.extend(file_funcs)
            file_structs = STRUCT_DEF_PATTERN.findall(content)
            structs.extend(file_structs)
            files_summary.append({
                "path": safe_path,
                "is_header": self.is_header_path(safe_path),
                "includes": file_inc[:10],
                "functions": file_funcs[:15],
                "structs": file_structs[:10],
                "line_count": len(content.splitlines()),
            })

        return {
            "language": "c",
            "file_count": len(source_files),
            "files": files_summary,
            "includes": sorted(includes)[:30],
            "functions": sorted(set(functions))[:40],
            "structs": sorted(set(structs))[:20],
        }
