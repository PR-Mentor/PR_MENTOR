"""Tests for C language adapter, built-in safety analysis, and parser integration."""

import asyncio
from pathlib import Path

import pytest

from app.languages.c import CAdapter


@pytest.fixture
def c_adapter():
    return CAdapter()


def test_c_adapter_metadata(c_adapter):
    assert c_adapter.language_id == "c"
    assert c_adapter.display_name == "C"
    assert c_adapter.capability_status == "production"
    assert c_adapter.source_extensions == {".c"}
    assert c_adapter.header_extensions == {".h"}
    assert c_adapter.is_source_path("src/main.c") is True
    assert c_adapter.is_header_path("include/main.h") is True
    assert c_adapter.is_header_path("src/main.c") is False


def test_c_builtin_safety_analysis_detects_dangerous_patterns(c_adapter, tmp_path):
    c_source = """#include <stdio.h>
#include <string.h>
#include <stdlib.h>

void process_input() {
    char buf[64];
    // This is dangerous
    gets(buf);
    strcpy(buf, "input");
    sprintf(buf, "%s", "data");
    int val = atoi("123");
}
"""
    files = [("src/vuln.c", c_source)]
    findings = c_adapter._run_builtin_analyzer(files, tmp_path)

    rule_ids = {f.title for f in findings}
    assert any("C-SEC-GETS" in r for r in rule_ids)
    assert any("C-SEC-STRCPY" in r for r in rule_ids)
    assert any("C-ERR-ATOI" in r for r in rule_ids)

    # Verify severity and category
    gets_finding = next(f for f in findings if "C-SEC-GETS" in f.title)
    assert gets_finding.severity == "high"
    assert gets_finding.category == "security"
    assert gets_finding.line_start == 8
    assert gets_finding.file_path == "src/vuln.c"
    assert gets_finding.source == "static_analysis"


def test_c_safe_code_produces_no_builtin_findings(c_adapter, tmp_path):
    safe_source = """#include <stdio.h>

int safe_add(int a, int b) {
    return a + b;
}
"""
    files = [("src/safe.c", safe_source)]
    findings = c_adapter._run_builtin_analyzer(files, tmp_path)
    assert len(findings) == 0


def test_c_clang_tidy_output_parsing(c_adapter, tmp_path):
    mock_stdout = """src/main.c:12:5: warning: use of a dangerous function 'gets' [clang-diagnostic-deprecated-declarations]
    gets(buffer);
    ^
src/main.c:20:10: error: null pointer passed [clang-analyzer-core.NonNullParamChecker]
"""
    parsed = c_adapter._parse_clang_tidy_output(mock_stdout, tmp_path)
    assert len(parsed) == 2

    assert parsed[0].file_path == "src/main.c"
    assert parsed[0].line_start == 12
    assert parsed[0].severity == "medium"
    assert "clang-diagnostic-deprecated-declarations" in parsed[0].title

    assert parsed[1].line_start == 20
    assert parsed[1].severity == "high"
    assert parsed[1].category == "bug"


def test_c_capabilities_honestly_reported(c_adapter, monkeypatch):
    # Simulate clang-tidy and cppcheck missing
    monkeypatch.setattr("shutil.which", lambda name: None)
    caps = c_adapter.detect_capabilities()

    assert caps["clang_tidy"].status == "unavailable"
    assert "not found in PATH" in caps["clang_tidy"].reason

    assert caps["cppcheck"].status == "unavailable"
    assert "not found in PATH" in caps["cppcheck"].reason

    assert caps["c_safety_analyzer"].status == "available"


def test_c_source_summarization(c_adapter):
    header_content = """#ifndef BUFFER_H
#define BUFFER_H
#include <stddef.h>

struct Buffer {
    char *data;
    size_t size;
};

int buffer_init(struct Buffer *b);
#endif
"""
    source_content = """#include "buffer.h"
#include <stdlib.h>

int buffer_init(struct Buffer *b) {
    b->size = 0;
    return 0;
}
"""
    files = [("include/buffer.h", header_content), ("src/buffer.c", source_content)]
    summary = c_adapter.summarize_sources(files)

    assert summary["language"] == "c"
    assert summary["file_count"] == 2
    assert "buffer.h" in summary["includes"]
    assert "Buffer" in summary["structs"]
    assert "buffer_init" in summary["functions"]


def test_c_run_static_analysis_end_to_end(c_adapter, tmp_path):
    c_source = """#include <stdio.h>
void test() {
    char b[10];
    gets(b);
}
"""
    findings, obs, tool_results = asyncio.run(
        c_adapter.run_static_analysis([("src/test.c", c_source)], tmp_path)
    )

    # Tool results should contain status for clang_tidy, cppcheck, and c_safety_analyzer
    tool_names = {t["tool"] for t in tool_results}
    assert "clang_tidy" in tool_names
    assert "cppcheck" in tool_names
    assert "c_safety_analyzer" in tool_names

    # Built-in analyzer must produce the gets() finding
    assert any("C-SEC-GETS" in f.title for f in findings)
    assert len(obs) >= 1
    assert any(o.tool_name == "c_safety_analyzer" for o in obs)
