from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.verification import mypy_runner
from app.verification.analyzer_common import config_digest
from app.verification.bounded_process import BoundedProcessError
from app.verification.mypy_runner import run_mypy_source


def test_mypy_analyzes_clean_and_invalid_type_source_with_real_tool():
    clean = run_mypy_source(
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        "src/math_ops.py",
        timeout=10,
    )
    invalid = run_mypy_source(
        "def add(left: int, right: int) -> int:\n    return left + 'bad'\n",
        "src/math_ops.py",
        timeout=10,
    )
    assert clean.tool_name == "mypy"
    assert clean.tool_version.startswith("mypy ")
    assert clean.configuration_sha256 == invalid.configuration_sha256
    assert clean.findings == ()
    assert invalid.findings[0]["rule_id"] == "operator"
    assert invalid.findings[0]["line"] == 2
    assert invalid.findings[0]["column"] is not None
    assert invalid.findings[0]["severity"] == "error"
    assert invalid.findings[0]["file_path"] == "src/math_ops.py"


def test_mypy_marks_unresolved_import_and_missing_stub_as_incomplete():
    result = run_mypy_source(
        "import prmentor_missing_library\n\ndef f(value: int) -> int:\n    return value\n",
        "src/module.py",
        timeout=10,
    )
    import_diagnostics = [
        finding
        for finding in result.findings
        if finding["rule_id"] == "import-not-found"
    ]
    assert import_diagnostics
    assert import_diagnostics[0]["analysis_incomplete"] is True


def test_mypy_classifies_missing_type_stubs_as_incomplete():
    findings = mypy_runner._parse_mypy_output(
        b'analysis_target.py:1: error: Library stubs not installed for "vendor" [import-untyped]\n',
        b"",
        "src/module.py",
    )
    assert findings[0]["rule_id"] == "import-untyped"
    assert findings[0]["analysis_incomplete"] is True


@pytest.mark.parametrize("path", ["../escape.py", "src/file.txt", "C:/outside.py"])
def test_mypy_rejects_unsupported_repository_paths(path):
    with pytest.raises(ValueError):
        run_mypy_source("value = 1\n", path)


def test_mypy_rejects_oversized_source_and_invalid_output_limit():
    with pytest.raises(ValueError, match="source size"):
        run_mypy_source("x" * 100_001, "src/file.py")
    with pytest.raises(ValueError, match="max_output_bytes"):
        run_mypy_source("x = 1\n", "src/file.py", max_output_bytes=0)


@pytest.mark.parametrize(
    "source",
    [
        "# mypy: ignore-errors\nvalue = 1\n",
        "value = unknown  # type: ignore[name-defined]\n",
    ],
)
def test_mypy_rejects_source_controlled_suppressions_as_incomplete(source):
    with pytest.raises(
        BoundedProcessError, match="inline configuration or type-ignore"
    ):
        run_mypy_source(source, "src/file.py")


def test_mypy_enforces_actual_tool_output_limit():
    with pytest.raises(BoundedProcessError, match="output limit"):
        run_mypy_source(
            "def f(value: int) -> int:\n    return value + 'a type error'\n",
            "src/file.py",
            max_output_bytes=32,
        )


def test_mypy_fails_closed_on_malformed_output(monkeypatch):
    monkeypatch.setattr(mypy_runner, "tool_version", lambda *_args, **_kwargs: "mypy 2")
    monkeypatch.setattr(
        mypy_runner,
        "run_tool_process",
        lambda *_args, **_kwargs: (b"unexpected output\n", b"", 1, 0.01),
    )
    with pytest.raises(BoundedProcessError, match="unrecognized diagnostic"):
        run_mypy_source("x = 1\n", "src/file.py")


def test_mypy_propagates_timeout_and_missing_executable(monkeypatch):
    monkeypatch.setattr(mypy_runner, "tool_version", lambda *_args, **_kwargs: "mypy 2")
    monkeypatch.setattr(
        mypy_runner,
        "run_tool_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BoundedProcessError(
                "Analyzer timed out after 1 seconds.", inconclusive=True
            )
        ),
    )
    with pytest.raises(BoundedProcessError, match="timed out"):
        run_mypy_source("x = 1\n", "src/file.py")

    monkeypatch.setattr(
        mypy_runner,
        "tool_version",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BoundedProcessError("mypy executable is unavailable.")
        ),
    )
    with pytest.raises(BoundedProcessError, match="unavailable"):
        run_mypy_source("x = 1\n", "src/file.py")


def test_mypy_runner_uses_shared_bounded_process_and_configuration(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        mypy_runner, "tool_version", lambda *_args, **_kwargs: "mypy 2.4.1"
    )

    def process(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return b"", b"", 0, 0.01

    monkeypatch.setattr(mypy_runner, "run_tool_process", process)
    result = run_mypy_source("value = 1\n", "src/file.py", python=sys.executable)
    assert result.configuration_sha256 == config_digest(
        mypy_runner.mypy_configuration()
    )
    assert "--no-incremental" in captured["command"]
    assert "--no-site-packages" in captured["command"]
    assert "--check-untyped-defs" in captured["command"]
    assert captured["kwargs"]["max_output_bytes"] == 1_000_000
    assert captured["kwargs"]["cwd"] != str(Path.cwd())


def test_mypy_configuration_does_not_enable_plugins_or_ignore_imports():
    configuration = mypy_runner.mypy_configuration()
    assert configuration["plugins"] == []
    assert configuration["ignore_missing_imports"] is False
    assert configuration["no_site_packages"] is True
    assert configuration["check_untyped_defs"] is True
    assert configuration["context"] == "single-file-snapshot"
