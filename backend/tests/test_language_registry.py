"""Tests for multi-language registry and adapter routing."""

import pytest

from app.languages.base import LanguageAdapter, ToolCapability
from app.languages.registry import LanguageRegistry, get_registry


def test_registry_singleton_contains_expected_languages():
    registry = get_registry()
    adapters = registry.all_adapters()
    lang_ids = {a.language_id for a in adapters}
    assert {"python", "c", "cpp", "java"}.issubset(lang_ids)


def test_extension_to_adapter_mapping():
    registry = get_registry()

    assert registry.detect_adapter_for_path("src/main.py").language_id == "python"
    assert registry.detect_adapter_for_path("pkg/types.pyi").language_id == "python"

    assert registry.detect_adapter_for_path("src/kernel.c").language_id == "c"
    assert registry.detect_adapter_for_path("include/kernel.h").language_id == "c"

    assert registry.detect_adapter_for_path("src/engine.cpp").language_id == "cpp"
    assert registry.detect_adapter_for_path("src/engine.cc").language_id == "cpp"
    assert registry.detect_adapter_for_path("include/engine.hpp").language_id == "cpp"

    assert registry.detect_adapter_for_path("com/app/Service.java").language_id == "java"

    # Unsupported or unknown
    assert registry.detect_adapter_for_path("readme.md") is None
    assert registry.detect_adapter_for_path("package.json") is None


def test_path_sanitization_rules():
    registry = get_registry()

    # Valid relative paths
    assert registry.sanitize_path("src/calc.c") == "src/calc.c"
    assert registry.sanitize_path("nested/deep/file.java") == "nested/deep/file.java"
    assert registry.sanitize_path("src\\windows\\path.cpp") == "src/windows/path.cpp"

    # Invalid paths: traversal, absolute, NUL byte, unsupported extension
    assert registry.sanitize_path("../escape.c") is None
    assert registry.sanitize_path("src/../../escape.c") is None
    assert registry.sanitize_path("/absolute/path.java") is None
    assert registry.sanitize_path("src/file\x00.c") is None
    assert registry.sanitize_path("unsupported.txt") is None


def test_partition_files_by_language():
    registry = get_registry()
    files = [
        ("src/app.py", "print('hello')"),
        ("src/driver.c", "int main() { return 0; }"),
        ("include/driver.h", "int main();"),
        ("src/model.cpp", "class Model {};"),
        ("src/Service.java", "public class Service {}"),
        ("docs/notes.txt", "some notes"),
    ]

    partitioned = registry.partition_files_by_language(files)

    assert "python" in partitioned
    assert len(partitioned["python"]) == 1
    assert partitioned["python"][0][0] == "src/app.py"

    assert "c" in partitioned
    assert len(partitioned["c"]) == 2

    assert "cpp" in partitioned
    assert len(partitioned["cpp"]) == 1

    assert "java" in partitioned
    assert len(partitioned["java"]) == 1

    assert "unknown" in partitioned
    assert len(partitioned["unknown"]) == 1


def test_capability_matrix_structure():
    registry = get_registry()
    matrix = registry.get_capability_matrix()

    for lang in ("python", "c", "cpp", "java"):
        assert lang in matrix
        entry = matrix[lang]
        assert "display_name" in entry
        assert "status" in entry
        assert "extensions" in entry
        assert "tools" in entry
        assert isinstance(entry["tools"], dict)
