"""Tests for C++ language adapter, modern C++ safety checks, and tool integration."""

import asyncio
from pathlib import Path

import pytest

from app.languages.cpp import CppAdapter


@pytest.fixture
def cpp_adapter():
    return CppAdapter()


def test_cpp_adapter_metadata(cpp_adapter):
    assert cpp_adapter.language_id == "cpp"
    assert cpp_adapter.display_name == "C++"
    assert cpp_adapter.capability_status == "production"
    assert {".cpp", ".cc", ".cxx", ".c++"}.issubset(cpp_adapter.source_extensions)
    assert {".hpp", ".hh", ".hxx"}.issubset(cpp_adapter.header_extensions)
    assert cpp_adapter.is_source_path("src/engine.cpp") is True
    assert cpp_adapter.is_header_path("include/engine.hpp") is True


def test_cpp_builtin_safety_analysis_detects_patterns(cpp_adapter, tmp_path):
    cpp_source = """#include <memory>
#include <stdexcept>
#include <iostream>

void legacy_process() {
    std::auto_ptr<int> p(new int(42));
    try {
        throw std::runtime_error("failed");
    } catch (std::exception ex) {
        std::cout << ex.what() << std::endl;
    }
    char buf[10];
    gets(buf);
}
"""
    files = [("src/engine.cpp", cpp_source)]
    findings = cpp_adapter._run_builtin_analyzer(files, tmp_path)

    rule_ids = {f.title for f in findings}
    assert any("CPP-DEP-AUTOPTR" in r for r in rule_ids)
    assert any("CPP-ERR-SLICING" in r for r in rule_ids)
    assert any("CPP-SEC-GETS" in r for r in rule_ids)

    slicing = next(f for f in findings if "CPP-ERR-SLICING" in f.title)
    assert slicing.severity == "medium"
    assert slicing.category == "bug"
    assert slicing.line_start == 9


def test_cpp_safe_code_produces_no_findings(cpp_adapter, tmp_path):
    safe_code = """#include <memory>
#include <stdexcept>
#include <string_view>

void modern_process(std::string_view name) {
    auto ptr = std::make_unique<int>(100);
    try {
        // do work
    } catch (const std::exception& ex) {
        // safe const reference
    }
}
"""
    files = [("src/safe.cpp", safe_code)]
    findings = cpp_adapter._run_builtin_analyzer(files, tmp_path)
    assert len(findings) == 0


def test_cpp_source_summarization(cpp_adapter):
    source = """#include <vector>
#include <string>

namespace app::core {

class SessionManager {
public:
    void startSession(int id) {
    }
};

}
"""
    files = [("src/session.cpp", source)]
    summary = cpp_adapter.summarize_sources(files)

    assert summary["language"] == "cpp"
    assert summary["file_count"] == 1
    assert "app::core" in summary["namespaces"]
    assert "SessionManager" in summary["classes"]
    assert "vector" in summary["includes"]


def test_cpp_run_static_analysis_end_to_end(cpp_adapter, tmp_path):
    cpp_source = """#include <memory>
void run() {
    std::auto_ptr<int> a;
}
"""
    findings, obs, tool_results = asyncio.run(
        cpp_adapter.run_static_analysis([("src/run.cpp", cpp_source)], tmp_path)
    )

    tool_names = {t["tool"] for t in tool_results}
    assert "clang_tidy" in tool_names
    assert "cppcheck" in tool_names
    assert "cpp_safety_analyzer" in tool_names

    assert any("CPP-DEP-AUTOPTR" in f.title for f in findings)
    assert len(obs) >= 1
