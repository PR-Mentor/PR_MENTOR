"""Integration tests for mixed-language reviews containing Python, C, C++, and Java."""

import asyncio
from pathlib import Path

import pytest

from app.languages.registry import get_registry
from app.services.finding_reconciliation import reconcile_observations


def test_mixed_language_file_partitioning_and_analysis(tmp_path):
    registry = get_registry()

    files = [
        ("src/backend.py", "import os\ncmd = 'echo ' + os.environ['USER']\nos.system(cmd)\n"),
        ("src/native.c", "#include <stdio.h>\nvoid fn() { char b[10]; gets(b); }\n"),
        ("src/compute.cpp", "#include <memory>\nvoid run() { std::auto_ptr<int> x; }\n"),
        ("src/DataService.java", "package com.app;\npublic class DataService { void f() { try {} catch (Exception e) {} } }\n"),
    ]

    partitioned = registry.partition_files_by_language(files)
    assert set(partitioned.keys()) == {"python", "c", "cpp", "java"}

    all_findings = []
    all_observations = []
    all_tool_results = []

    for lang_id, lang_files in partitioned.items():
        adapter = registry.get_adapter(lang_id)
        assert adapter is not None
        findings, obs, tool_results = asyncio.run(
            adapter.run_static_analysis(lang_files, tmp_path)
        )
        all_findings.extend(findings)
        all_observations.extend(obs)
        all_tool_results.extend(tool_results)

    # 1. We must have findings from each language
    paths_with_findings = {f.file_path for f in all_findings}
    assert "src/backend.py" in paths_with_findings
    assert "src/native.c" in paths_with_findings
    assert "src/compute.cpp" in paths_with_findings
    assert "src/DataService.java" in paths_with_findings

    # 2. Check reconciliation works seamlessly across all languages
    reconciled = reconcile_observations(all_observations)
    assert reconciled.unique_issue_count >= 4

    canonical_paths = {issue.file_path for issue in reconciled.issues}
    assert "src/backend.py" in canonical_paths
    assert "src/native.c" in canonical_paths
    assert "src/compute.cpp" in canonical_paths
    assert "src/DataService.java" in canonical_paths


def test_unsupported_files_are_safely_ignored():
    registry = get_registry()
    unsupported = [
        ("README.md", "# Title"),
        ("Makefile", "all: build"),
        ("data/schema.sql", "CREATE TABLE users (id INT);"),
        ("package.json", '{"name": "frontend"}'),
    ]

    partitioned = registry.partition_files_by_language(unsupported)
    assert "unknown" in partitioned
    assert len(partitioned["unknown"]) == 4

    for path, _ in unsupported:
        assert registry.sanitize_path(path) is None


def test_single_language_c_only_review(tmp_path):
    registry = get_registry()
    adapter = registry.get_adapter("c")
    assert adapter is not None

    files = [("src/native.c", "#include <stdio.h>\nvoid test() { char buf[16]; gets(buf); }\n")]
    findings, obs, tool_results = asyncio.run(adapter.run_static_analysis(files, tmp_path))

    tool_names = {t["tool"] for t in tool_results}
    assert "c_safety_analyzer" in tool_names
    assert "clang_tidy" in tool_names
    assert "cppcheck" in tool_names
    assert any("C-SEC-GETS" in f.title for f in findings)
    assert len(obs) >= 1


def test_single_language_cpp_only_review(tmp_path):
    registry = get_registry()
    adapter = registry.get_adapter("cpp")
    assert adapter is not None

    files = [("src/core.cpp", "#include <memory>\nvoid test() { std::auto_ptr<int> a; }\n")]
    findings, obs, tool_results = asyncio.run(adapter.run_static_analysis(files, tmp_path))

    tool_names = {t["tool"] for t in tool_results}
    assert "cpp_safety_analyzer" in tool_names
    assert "clang_tidy" in tool_names
    assert "cppcheck" in tool_names
    assert any("CPP-DEP-AUTOPTR" in f.title for f in findings)
    assert len(obs) >= 1


def test_single_language_java_only_review(tmp_path):
    registry = get_registry()
    adapter = registry.get_adapter("java")
    assert adapter is not None

    files = [("src/Insecure.java", "package com.app;\npublic class Insecure { void f(java.io.ObjectInputStream in) throws Exception { in.readObject(); } }\n")]
    findings, obs, tool_results = asyncio.run(adapter.run_static_analysis(files, tmp_path))

    tool_names = {t["tool"] for t in tool_results}
    assert "java_safety_analyzer" in tool_names
    assert "javac" in tool_names
    assert any("JAVA-SEC-DESERIALIZATION" in f.title for f in findings)
    assert len(obs) >= 1


def test_single_language_python_only_review(tmp_path):
    registry = get_registry()
    adapter = registry.get_adapter("python")
    assert adapter is not None

    files = [("src/service.py", "import os\ncmd = 'ls ' + os.environ.get('USER', '')\nos.system(cmd)\n")]
    findings, obs, tool_results = asyncio.run(adapter.run_static_analysis(files, tmp_path))

    tool_names = {t["tool"] for t in tool_results}
    assert "ruff" in tool_names
    assert "bandit" in tool_names
    assert len(findings) >= 1


def test_get_file_content_source_between_100kb_and_1mb(tmp_path, monkeypatch):
    import base64
    from app.github_service import pull_requests

    # Realistic ~170 KB C++ header fixture (OpenCV core.hpp was 168.9 KB)
    header_preamble = (
        "#ifndef OPENCV_CORE_HPP\n"
        "#define OPENCV_CORE_HPP\n\n"
        "#include <iostream>\n"
        "#include <vector>\n"
        "#include <string>\n\n"
        "namespace cv {\n\n"
    )
    class_block = (
        "class MatBlock {\n"
        "public:\n"
        "    int rows;\n"
        "    int cols;\n"
        "    int channels() const { return 1; }\n"
        "    void process() const {}\n"
        "};\n\n"
    )
    # 124 bytes per block * 1370 blocks = ~170 KB (OpenCV core.hpp was 168.9 KB)
    header_body = class_block * 1370
    header_closing = "} // namespace cv\n\n#endif // OPENCV_CORE_HPP\n"
    content_str = header_preamble + header_body + header_closing
    content_bytes = content_str.encode("utf-8")
    assert 150_000 < len(content_bytes) < 200_000

    b64_payload = base64.b64encode(content_bytes).decode("ascii")

    async def mock_request_json(client, path, params=None, *, use_cache=True, **kwargs):
        return {
            "size": len(content_bytes),
            "encoding": "base64",
            "content": b64_payload,
        }

    monkeypatch.setattr(pull_requests, "request_json", mock_request_json)

    # 1. Verify retrieval and base64 decoding through existing pull_requests path
    result = asyncio.run(
        pull_requests.get_file_content("opencv", "opencv", "modules/core/include/opencv2/core.hpp", "a" * 40)
    )
    assert result is not None
    assert len(result) == len(content_str)
    assert result.startswith("#ifndef OPENCV_CORE_HPP")
    assert result.endswith("#endif // OPENCV_CORE_HPP\n")

    # 2. Feed retrieved content through the actual C++ analysis adapter
    registry = get_registry()
    cpp_adapter = registry.get_adapter("cpp")
    assert cpp_adapter is not None

    files = [("modules/core/include/opencv2/core.hpp", result)]
    findings, obs, tool_results = asyncio.run(
        cpp_adapter.run_static_analysis(files, tmp_path)
    )

    # 3. Verify complete source reaches the adapter and is written without truncation
    written_file = tmp_path / "modules/core/include/opencv2/core.hpp"
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == result
    assert len(written_file.read_text(encoding="utf-8")) == len(content_str)

    # 4. Verify static analysis completed cleanly with expected tool structure
    tool_names = {t["tool"] for t in tool_results}
    assert "cpp_safety_analyzer" in tool_names
    assert "clang_tidy" in tool_names
    assert "cppcheck" in tool_names
    assert isinstance(findings, list)
    assert isinstance(obs, list)

    # 5. Verify source summarization processes the 170 KB source without exception
    summary = cpp_adapter.summarize_sources(files)
    assert "cv" in summary["namespaces"]
    assert "iostream" in summary["includes"]
    assert "vector" in summary["includes"]
    assert any(f["path"] == "modules/core/include/opencv2/core.hpp" for f in summary["files"])


def test_get_file_content_source_exceeding_1mb(monkeypatch):
    import base64
    from app.github_service import pull_requests

    # 1.1 MB content
    large_size = 1_100_000
    b64_payload = base64.b64encode(b"X" * large_size).decode("ascii")

    async def mock_request_json(client, path, params=None, *, use_cache=True, **kwargs):
        return {
            "size": large_size,
            "encoding": "base64",
            "content": b64_payload,
        }

    monkeypatch.setattr(pull_requests, "request_json", mock_request_json)

    result = asyncio.run(
        pull_requests.get_file_content("opencv", "opencv", "large_file.cpp", "a" * 40)
    )
    assert result is None


def test_empty_source_fallback_language_aware():
    from unittest.mock import MagicMock
    from app.services import review_orchestrator

    # Helper simulating the review_orchestrator language-aware fallback logic
    registry = get_registry()

    def run_fallback(bundle_filenames, source_files):
        bundle_files = [MagicMock(filename=fn, status="modified") for fn in bundle_filenames]
        partitioned = registry.partition_files_by_language(source_files)
        pr_languages: set[str] = set()
        for item in bundle_files:
            adapter = registry.detect_adapter_for_path(item.filename)
            if adapter and item.status != "removed":
                pr_languages.add(adapter.language_id)

        target_languages = sorted(pr_languages | set(partitioned.keys()))
        tool_results = []
        if not target_languages:
            tool_results.append({
                "tool": "ruff", "status": "skipped", "executed": False,
                "reason": "No eligible Python source was available.",
            })
            tool_results.append({
                "tool": "bandit", "status": "skipped", "executed": False,
                "reason": "No eligible Python source was available.",
            })
        else:
            for lang_id in target_languages:
                adapter = registry.get_adapter(lang_id)
                if adapter is None:
                    continue
                if lang_id not in partitioned:
                    caps = adapter.detect_capabilities()
                    for tool_name in caps:
                        tool_results.append({
                            "tool": tool_name,
                            "status": "skipped",
                            "executed": False,
                            "reason": f"No eligible {adapter.display_name} source was available for analysis.",
                        })
        return tool_results

    # 1. C-only PR with empty source
    c_results = run_fallback(["src/native.c"], [])
    c_tools = {t["tool"] for t in c_results}
    assert "c_safety_analyzer" in c_tools
    assert "clang_tidy" in c_tools
    assert "ruff" not in c_tools
    assert "bandit" not in c_tools
    assert all(t["status"] == "skipped" for t in c_results)
    assert all("C source" in t["reason"] for t in c_results)

    # 2. C++-only PR with empty source (OpenCV scenario)
    cpp_results = run_fallback(["include/opencv2/core.hpp", "src/copy.cpp"], [])
    cpp_tools = {t["tool"] for t in cpp_results}
    assert "cpp_safety_analyzer" in cpp_tools
    assert "clang_tidy" in cpp_tools
    assert "cppcheck" in cpp_tools
    assert "ruff" not in cpp_tools
    assert "bandit" not in cpp_tools
    assert all(t["status"] == "skipped" for t in cpp_results)
    assert all("C++ source" in t["reason"] for t in cpp_results)

    # 3. Java-only PR with empty source
    java_results = run_fallback(["src/Main.java"], [])
    java_tools = {t["tool"] for t in java_results}
    assert "java_safety_analyzer" in java_tools
    assert "javac" in java_tools
    assert "ruff" not in java_tools
    assert all(t["status"] == "skipped" for t in java_results)
    assert all("Java source" in t["reason"] for t in java_results)

    # 4. Python-only PR with empty source
    py_results = run_fallback(["app/main.py"], [])
    py_tools = {t["tool"] for t in py_results}
    assert "ruff" in py_tools
    assert "bandit" in py_tools
    assert "clang_tidy" not in py_tools
    assert all(t["status"] == "skipped" for t in py_results)
    assert all("Python source" in t["reason"] for t in py_results)

    # 5. Mixed-language PR with empty source
    mixed_results = run_fallback(["src/main.cpp", "app/main.py"], [])
    mixed_tools = {t["tool"] for t in mixed_results}
    assert "cpp_safety_analyzer" in mixed_tools
    assert "ruff" in mixed_tools
    assert "bandit" in mixed_tools

    # 6. Non-code PR (e.g. README.md)
    doc_results = run_fallback(["README.md", "docs/index.md"], [])
    doc_tools = {t["tool"] for t in doc_results}
    assert "ruff" in doc_tools
    assert "bandit" in doc_tools


def test_diff_attribution_distinguishes_introduced_pre_existing_and_contextual():
    from app.schemas.finding import ReviewFinding
    from app.services.review_orchestrator import _determine_finding_attribution
    from app.services.finding_reconciliation import _build_issue
    from app.schemas.issue import FindingObservation

    added_lines_map = {
        "src/compute.cpp": {145, 146, 147},
        "src/util.c": {20},
    }
    changed_files = {"src/compute.cpp", "src/util.c", "src/unmodified_patch.java"}

    # 1. Finding directly in added line -> introduced
    f_introduced = ReviewFinding(
        id="f1",
        title="Cpp_Safety_Analyzer CPP-MEM-RAW-NEW",
        description="Raw new",
        file_path="src/compute.cpp",
        line_start=145,
        line_end=145,
        severity="low",
        category="maintainability",
        confidence=0.75,
        source="static_analysis",
        verification_status="needs_review",
        why_it_matters="why",
        suggested_fix="fix",
    )
    attr1 = _determine_finding_attribution(f_introduced, added_lines_map, changed_files)
    assert attr1 == "introduced"
    f_introduced.attribution = attr1

    # 2. Finding in surrounding unchanged line -> pre_existing
    f_pre_existing = ReviewFinding(
        id="f2",
        title="Cpp_Safety_Analyzer CPP-MEM-RAW-NEW",
        description="Raw new in surrounding code",
        file_path="src/compute.cpp",
        line_start=2548,
        line_end=2548,
        severity="low",
        category="maintainability",
        confidence=0.75,
        source="static_analysis",
        verification_status="needs_review",
        why_it_matters="why",
        suggested_fix="fix",
    )
    attr2 = _determine_finding_attribution(f_pre_existing, added_lines_map, changed_files)
    assert attr2 == "pre_existing"
    f_pre_existing.attribution = attr2

    # 3. File-level finding without line numbers -> contextual
    f_contextual = ReviewFinding(
        id="f3",
        title="Static Diagnostic",
        description="General warning",
        file_path="src/other.py",
        line_start=None,
        line_end=None,
        severity="informational",
        category="code_quality",
        confidence=0.5,
        source="static_analysis",
        verification_status="needs_review",
        why_it_matters="why",
        suggested_fix="fix",
    )
    attr3 = _determine_finding_attribution(f_contextual, added_lines_map, changed_files)
    assert attr3 == "contextual"
    f_contextual.attribution = attr3

    # Check canonical issue aggregation
    obs1 = FindingObservation(original_finding=f_introduced, tool_name="cpp_safety_analyzer")
    issue1 = _build_issue([obs1])
    assert issue1.attribution == "introduced"

    obs2 = FindingObservation(original_finding=f_pre_existing, tool_name="cpp_safety_analyzer")
    issue2 = _build_issue([obs2])
    assert issue2.attribution == "pre_existing"

    obs3 = FindingObservation(original_finding=f_contextual, tool_name="static")
    issue3 = _build_issue([obs3])
    assert issue3.attribution == "contextual"

    # 4. Modified file with missing/malformed patch must fail safe as CONTEXTUAL, not pre_existing!
    f_missing_patch = ReviewFinding(
        id="f4",
        title="Diagnostic",
        description="Line in file with missing patch",
        file_path="src/unmodified_patch.java",
        line_start=50,
        line_end=50,
        severity="low",
        category="code_quality",
        confidence=0.5,
        source="static_analysis",
        verification_status="needs_review",
        why_it_matters="why",
        suggested_fix="fix",
    )
    attr4 = _determine_finding_attribution(f_missing_patch, added_lines_map, changed_files)
    assert attr4 == "contextual", f"Expected contextual for missing patch, got {attr4}"

    # 5. Deserialization of legacy records without attribution field must default to CONTEXTUAL
    legacy_finding_data = {
        "id": "legacy-1",
        "title": "Legacy Finding",
        "description": "Found long ago",
        "file_path": "legacy.py",
        "line_start": 10,
        "line_end": 10,
        "severity": "medium",
        "category": "bug",
        "confidence": 0.8,
        "source": "static_analysis",
        "verification_status": "needs_review",
        "why_it_matters": "legacy",
        "suggested_fix": "fix",
    }
    deserialized_finding = ReviewFinding.model_validate(legacy_finding_data)
    assert deserialized_finding.attribution == "contextual"

    from app.schemas.issue import CanonicalIssue
    legacy_issue_data = {
        "issue_id": "legacy-issue-1",
        "file_path": "legacy.py",
        "line_start": 10,
        "line_end": 10,
        "category": "bug",
        "severity": "medium",
        "normalized_description": "Legacy issue",
    }
    deserialized_issue = CanonicalIssue.model_validate(legacy_issue_data)
    assert deserialized_issue.attribution == "contextual"



