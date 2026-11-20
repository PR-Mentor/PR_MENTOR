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
