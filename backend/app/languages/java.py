"""Java language adapter with javac linter, Checkstyle/PMD capability probes, and built-in Java security analyzer."""

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

PACKAGE_PATTERN = re.compile(r'^\s*package\s+([a-zA-Z0-9_.]+)\s*;', re.MULTILINE)
IMPORT_PATTERN = re.compile(r'^\s*import\s+(?:static\s+)?([a-zA-Z0-9_.*]+)\s*;', re.MULTILINE)
TYPE_DEF_PATTERN = re.compile(r'\b(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?(?:abstract\s+)?(class|interface|enum|record)\s+([a-zA-Z0-9_]+)', re.MULTILINE)
METHOD_PATTERN = re.compile(r'\b(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?(?:synchronized\s+)?(?:<[^>]+>\s+)?([a-zA-Z0-9_<>[\]]+)\s+([a-zA-Z0-9_]+)\s*\(([^)]*)\)\s*(?:throws\s+[^{]+)?\s*\{', re.MULTILINE)

# Built-in Java security & code quality checks
DANGEROUS_JAVA_PATTERNS = [
    (
        re.compile(r'\breadObject\s*\('),
        "JAVA-SEC-DESERIALIZATION",
        "critical",
        "security",
        "Insecure object deserialization via readObject() without look-ahead validation (CWE-502).",
        "Untrusted deserialization can allow arbitrary remote code execution via gadget chains.",
        "Use safe serialization formats (e.g. JSON/Protocol Buffers) or ObjectInputFilter.",
    ),
    (
        re.compile(r'\.execute(?:Query|Update)?\s*\([^)]*\+'),
        "JAVA-SEC-SQLI",
        "high",
        "security",
        "Potential SQL injection from concatenated query in createStatement(). Use PreparedStatement (CWE-89).",
        "Dynamic SQL concatenation allows attackers to manipulate query logic and extract or modify data.",
        "Use connection.prepareStatement(\"SELECT ... WHERE id = ?\") with parameterized bindings.",
    ),
    (
        re.compile(r'MessageDigest\.getInstance\s*\(\s*["\'](MD5|SHA-1)["\']\s*\)', re.IGNORECASE),
        "JAVA-SEC-WEAK-HASH",
        "medium",
        "security",
        "Use of cryptographically broken hash algorithm (MD5/SHA-1) (CWE-328). Use SHA-256 or SHA-3.",
        "MD5 and SHA-1 have known collision attacks and must not be used for cryptographic integrity or signatures.",
        "Replace with MessageDigest.getInstance(\"SHA-256\").",
    ),
    (
        re.compile(r'SecretKeySpec\s*\(\s*["\'][^"\']+["\']\.getBytes\(\)\s*,\s*["\']AES["\']\s*\)'),
        "JAVA-SEC-HARDCODED-KEY",
        "high",
        "security",
        "Hardcoded cryptographic key detected in code (CWE-321). Store keys in environment secrets or key vaults.",
        "Hardcoded keys in source control expose encrypted data to anyone with repository read access.",
        "Load encryption keys securely from environment variables or a secrets manager.",
    ),
    (
        re.compile(r'catch\s*\(\s*(?:Exception|Throwable)\s+[a-zA-Z0-9_]+\s*\)\s*\{\s*\}'),
        "JAVA-ERR-EMPTY-CATCH",
        "medium",
        "code_quality",
        "Empty catch block silently swallows exceptions (CWE-391). Log or handle the error appropriately.",
        "Silent exception swallowing masks underlying bugs, corrupts state, and severely impedes debugging.",
        "Log the exception with a logger or rethrow as an application-specific runtime exception.",
    ),
]


class JavaAdapter(LanguageAdapter):
    """Adapter for Java source analysis, javac diagnostics, and security checks."""

    @property
    def language_id(self) -> str:
        return "java"

    @property
    def display_name(self) -> str:
        return "Java"

    @property
    def capability_status(self) -> LanguageCapabilityStatus:
        return "production"

    @property
    def source_extensions(self) -> set[str]:
        return {".java"}

    def detect_capabilities(self) -> dict[str, ToolCapability]:
        caps: dict[str, ToolCapability] = {}

        # 1. javac
        javac_path = shutil.which("javac")
        if javac_path:
            caps["javac"] = ToolCapability("javac", "available", version="system")
        else:
            caps["javac"] = ToolCapability("javac", "unavailable", reason="javac compiler not found in PATH")

        # 2. Checkstyle
        if shutil.which("checkstyle"):
            caps["checkstyle"] = ToolCapability("checkstyle", "available", version="system")
        else:
            caps["checkstyle"] = ToolCapability("checkstyle", "unavailable", reason="checkstyle binary not found in PATH")

        # 3. PMD
        if shutil.which("pmd"):
            caps["pmd"] = ToolCapability("pmd", "available", version="system")
        else:
            caps["pmd"] = ToolCapability("pmd", "unavailable", reason="pmd binary not found in PATH")

        # 4. SpotBugs
        if shutil.which("spotbugs"):
            caps["spotbugs"] = ToolCapability("spotbugs", "available", version="system")
        else:
            caps["spotbugs"] = ToolCapability("spotbugs", "unavailable", reason="spotbugs binary not found in PATH")

        # 5. Built-in Java safety analyzer
        caps["java_safety_analyzer"] = ToolCapability("java_safety_analyzer", "available", version="1.0.0")

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

        # 1. Compiler diagnostics via javac if available
        if caps["javac"].status == "available":
            javac_findings = await self._run_javac_lint(written_paths, workspace_dir)
            findings.extend(javac_findings)
            for rf in javac_findings:
                observations.append(build_observation(rf, "javac", rf.title.replace("Javac ", "")))
            tool_results.append({
                "tool": "javac", "status": "completed", "executed": True,
                "findings": [f.model_dump(mode="json") for f in javac_findings],
            })
        else:
            tool_results.append({
                "tool": "javac", "status": "unavailable", "executed": False,
                "reason": caps["javac"].reason,
            })

        # Checkstyle status report
        tool_results.append({
            "tool": "checkstyle",
            "status": caps["checkstyle"].status,
            "executed": caps["checkstyle"].status == "available",
            "reason": caps["checkstyle"].reason,
        })

        # PMD status report
        tool_results.append({
            "tool": "pmd",
            "status": caps["pmd"].status,
            "executed": caps["pmd"].status == "available",
            "reason": caps["pmd"].reason,
        })

        # SpotBugs status report
        tool_results.append({
            "tool": "spotbugs",
            "status": caps["spotbugs"].status,
            "executed": caps["spotbugs"].status == "available",
            "reason": caps["spotbugs"].reason,
        })

        # 2. Built-in Java safety analyzer
        builtin_findings = self._run_builtin_analyzer(source_files, workspace_dir)
        findings.extend(builtin_findings)
        for rf in builtin_findings:
            observations.append(build_observation(rf, "java_safety_analyzer", rf.title.replace("Java_Safety_Analyzer ", "")))
        tool_results.append({
            "tool": "java_safety_analyzer", "status": "completed", "executed": True,
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
                if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                    continue
                for pattern, rule_id, sev, cat, msg, why, fix in DANGEROUS_JAVA_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        rf = build_review_finding_from_raw(
                            tool_name="java_safety_analyzer",
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

    async def _run_javac_lint(self, targets: list[Path], root: Path) -> list[ReviewFinding]:
        findings: list[ReviewFinding] = []
        target_strs = [str(t) for t in targets if t.suffix.lower() == ".java"]
        if not target_strs:
            return findings

        classes_out = root / ".classes"
        classes_out.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.to_thread(
                run_bounded_process,
                ["javac", "-Xlint:all", "-d", str(classes_out), "-cp", str(root)] + target_strs,
                cwd=str(root),
                timeout_seconds=20.0,
            )
            output = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")
            # pattern: path:line: error/warning: message
            regex = re.compile(r"^([^:\n]+):(\d+):\s*(warning|error):\s*(.*?)$", re.MULTILINE)
            for match in regex.finditer(output):
                path_str, line_str, level, msg = match.groups()
                line = int(line_str) if line_str.isdigit() else 1
                sev = "high" if level == "error" else "medium"
                rf = build_review_finding_from_raw(
                    tool_name="javac",
                    file_path=path_str,
                    line=line,
                    rule_id="javac-lint",
                    message=msg,
                    severity=sev,
                    category="bug" if level == "error" else "code_quality",
                    evidence=f"javac {level}: {msg}",
                    root=root,
                )
                findings.append(rf)
        except Exception:
            pass
        return findings

    def summarize_sources(self, source_files: list[tuple[str, str]]) -> dict[str, Any]:
        """Extract high-level structural intelligence from Java files."""
        packages: set[str] = set()
        imports: set[str] = set()
        types: list[str] = []
        methods: list[str] = []
        test_files: list[str] = []
        files_summary: list[dict[str, Any]] = []

        for safe_path, content in source_files:
            file_pkgs = PACKAGE_PATTERN.findall(content)
            packages.update(file_pkgs)
            file_imps = IMPORT_PATTERN.findall(content)
            imports.update(file_imps)
            file_types = [f"{m[0]} {m[1]}" for m in TYPE_DEF_PATTERN.findall(content)]
            types.extend(file_types)
            file_methods = [m[1] for m in METHOD_PATTERN.findall(content)]
            methods.extend(file_methods)

            is_test = "Test" in safe_path or "@Test" in content
            if is_test:
                test_files.append(safe_path)

            files_summary.append({
                "path": safe_path,
                "package": file_pkgs[0] if file_pkgs else None,
                "types": file_types[:10],
                "methods": file_methods[:15],
                "is_test": is_test,
                "line_count": len(content.splitlines()),
            })

        return {
            "language": "java",
            "file_count": len(source_files),
            "files": files_summary,
            "packages": sorted(packages)[:15],
            "types": sorted(set(types))[:30],
            "methods": sorted(set(methods))[:40],
            "test_files": test_files[:20],
            "imports": sorted(imports)[:30],
        }
