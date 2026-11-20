"""C++ language adapter with Clang-Tidy, Cppcheck and built-in modern C++ safety analyzer."""

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

INCLUDE_PATTERN = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)
NAMESPACE_PATTERN = re.compile(r'\bnamespace\s+([a-zA-Z_][a-zA-Z0-9_:]*)\s*\{', re.MULTILINE)
CLASS_PATTERN = re.compile(r'\bclass\s+([a-zA-Z_][a-zA-Z0-9_]*)(?:\s*:\s*[^{]+)?\s*\{', re.MULTILINE)
FUNCTION_PATTERN = re.compile(
    r'^[a-zA-Z_][a-zA-Z0-9_*:& \t\n]+?\s+([a-zA-Z_][a-zA-Z0-9_:]*)\s*\(([^)]*)\)\s*(?:const)?\s*(?:noexcept)?\s*\{',
    re.MULTILINE,
)

# Built-in modern C++ static checks
DANGEROUS_CPP_PATTERNS = [
    (
        re.compile(r'\bstd::auto_ptr\b'),
        "CPP-DEP-AUTOPTR",
        "medium",
        "code_quality",
        "std::auto_ptr was deprecated in C++11 and removed in C++17. Use std::unique_ptr instead.",
        "auto_ptr has broken copy semantics that transfer ownership silently on assignment.",
        "Replace std::auto_ptr<T> with std::unique_ptr<T>.",
    ),
    (
        re.compile(r'\bcatch\s*\(\s*(?:std::)?(?:exception|runtime_error|invalid_argument|logic_error)\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\)'),
        "CPP-ERR-SLICING",
        "medium",
        "bug",
        "Catching exception objects by value causes object slicing. Catch by const reference instead.",
        "Object slicing discards derived exception details and polymorphism.",
        "Replace 'catch (std::exception e)' with 'catch (const std::exception& e)'.",
    ),
    (
        re.compile(r'\bnew\s+[a-zA-Z0-9_:]+\s*(?:\[[^\]]*\])?(?:\([^)]*\))?(?!\s*;)'),
        "CPP-MEM-RAW-NEW",
        "low",
        "maintainability",
        "Raw 'new' expression detected. Prefer std::make_unique() or std::make_shared() to ensure RAII.",
        "Raw allocations risk memory leaks if exceptions occur before explicit delete.",
        "Use std::make_unique<T>() or modern smart pointer containers.",
    ),
    (
        re.compile(r'\bgets\s*\('),
        "CPP-SEC-GETS",
        "high",
        "security",
        "Use of gets() is inherently insecure (CWE-242). Use std::cin, std::getline(), or fgets().",
        "gets() has no boundary checks and guarantees buffer overflow on oversized input.",
        "Replace gets() with std::getline(std::cin, str).",
    ),
    (
        re.compile(r'\bstrcpy\s*\('),
        "CPP-SEC-STRCPY",
        "medium",
        "security",
        "Use of unsafe C string function strcpy() in C++. Use std::string or strncpy().",
        "Unbounded string copy risks buffer overflow and memory corruption.",
        "Use std::string copy/assignment or std::string_view.",
    ),
]


class CppAdapter(LanguageAdapter):
    """Adapter for C++ source and header analysis."""

    @property
    def language_id(self) -> str:
        return "cpp"

    @property
    def display_name(self) -> str:
        return "C++"

    @property
    def capability_status(self) -> LanguageCapabilityStatus:
        return "production"

    @property
    def source_extensions(self) -> set[str]:
        return {".cpp", ".cc", ".cxx", ".c++"}

    @property
    def header_extensions(self) -> set[str]:
        return {".hpp", ".hh", ".hxx", ".h++"}

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

        # 3. Built-in C++ safety analyzer
        caps["cpp_safety_analyzer"] = ToolCapability("cpp_safety_analyzer", "available", version="1.0.0")

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

        # 1. Clang-Tidy with C++17 flags
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

        # 2. Cppcheck with --language=c++
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

        # 3. Built-in C++ safety analyzer
        builtin_findings = self._run_builtin_analyzer(source_files, workspace_dir)
        findings.extend(builtin_findings)
        for rf in builtin_findings:
            observations.append(build_observation(rf, "cpp_safety_analyzer", rf.title.replace("Cpp_Safety_Analyzer ", "")))
        tool_results.append({
            "tool": "cpp_safety_analyzer", "status": "completed", "executed": True,
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
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("/*"):
                    continue
                for pattern, rule_id, sev, cat, msg, why, fix in DANGEROUS_CPP_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        rf = build_review_finding_from_raw(
                            tool_name="cpp_safety_analyzer",
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
                    ["clang-tidy", str(target), "--", "-std=c++17", f"-I{root}"],
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
                ["cppcheck", "--language=c++", "--enable=all", "--inline-suppr", "--template={file}:{line}:{severity}:{id}:{message}"] + target_strs,
                cwd=str(root),
                timeout_seconds=15.0,
            )
            output = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")
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
        """Extract high-level structural intelligence from C++ files."""
        includes: set[str] = set()
        namespaces: set[str] = set()
        classes: list[str] = []
        functions: list[str] = []
        files_summary: list[dict[str, Any]] = []

        for safe_path, content in source_files:
            file_inc = INCLUDE_PATTERN.findall(content)
            includes.update(file_inc)
            file_ns = NAMESPACE_PATTERN.findall(content)
            namespaces.update(file_ns)
            file_classes = CLASS_PATTERN.findall(content)
            classes.extend(file_classes)
            file_funcs = [m[0] for m in FUNCTION_PATTERN.findall(content)]
            functions.extend(file_funcs)

            files_summary.append({
                "path": safe_path,
                "is_header": self.is_header_path(safe_path),
                "namespaces": file_ns[:5],
                "classes": file_classes[:10],
                "functions": file_funcs[:15],
                "line_count": len(content.splitlines()),
            })

        return {
            "language": "cpp",
            "file_count": len(source_files),
            "files": files_summary,
            "namespaces": sorted(namespaces)[:20],
            "classes": sorted(set(classes))[:30],
            "functions": sorted(set(functions))[:40],
            "includes": sorted(includes)[:30],
        }
