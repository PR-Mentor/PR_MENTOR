import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.schemas.finding import Finding
from app.verification.bandit_runner import run_bandit
from app.verification.normalizer import normalize_bandit, normalize_ruff
from app.verification.path_validation import PathValidationError
from app.verification.ruff_runner import ToolExecutionError, run_ruff
from app.verification.test_runner import run_tests


def completed(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def make_python_file(root, relative="a.py"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("value = 1\n", encoding="utf-8")
    return path


def test_ruff_json_parsing_and_findings_exit_code(tmp_path):
    make_python_file(tmp_path, "src/a.py")
    payload = [{"filename": "src/a.py", "location": {"row": 7, "column": 2}, "code": "F401", "message": "unused import", "source_code": "import os"}]
    with patch("app.verification.ruff_runner.subprocess.run", return_value=completed(json.dumps(payload), 1)) as call:
        result = run_ruff(["src/a.py"], root=tmp_path)
    assert result == [{"file_path": "src/a.py", "line": 7, "rule_id": "F401", "message": "unused import", "evidence": "import os"}]
    assert call.call_args.args[0][:4] == ["ruff", "check", "--output-format", "json"]
    assert call.call_args.args[0][-1] == str((tmp_path / "src/a.py").resolve())
    assert call.call_args.kwargs["shell"] is not True if "shell" in call.call_args.kwargs else True


def test_bandit_json_parsing_and_findings_exit_code(tmp_path):
    make_python_file(tmp_path, "src/a.py")
    payload = {"results": [{"filename": "src/a.py", "line_number": 9, "test_id": "B602", "issue_severity": "HIGH", "issue_text": "subprocess call", "code": "  eval(input())\n"}]}
    with patch("app.verification.bandit_runner.subprocess.run", return_value=completed(json.dumps(payload), 1)) as call:
        assert run_bandit(["src/a.py"], root=tmp_path) == [{"file_path": "src/a.py", "line": 9, "rule_id": "B602", "message": "subprocess call", "severity": "HIGH", "evidence": "  eval(input())"}]
    assert call.call_args.args[0][-1] == str((tmp_path / "src/a.py").resolve())


def test_normalizers_use_shared_schema_and_preserve_provenance():
    ruff = normalize_ruff([{"file_path": "a.py", "line": 2, "rule_id": "F401", "message": "unused", "evidence": "import os"}])[0]
    bandit = normalize_bandit([{"file_path": "b.py", "line": 3, "rule_id": "B101", "message": "assert", "severity": "LOW", "evidence": "assert True"}])[0]
    assert isinstance(ruff, Finding) and ruff.source == "ruff" and ruff.severity == "medium"
    assert ruff.evidence == "import os" and ruff.verification_status == "tool_flagged"
    assert bandit.source == "bandit" and bandit.severity == "low" and bandit.rule_id == "B101"
    assert bandit.evidence == "assert True" and bandit.verification_status == "tool_flagged"


@pytest.mark.parametrize("module,func", [("app.verification.ruff_runner", run_ruff), ("app.verification.bandit_runner", run_bandit)])
def test_missing_executable(module, func, tmp_path):
    make_python_file(tmp_path)
    with patch(f"{module}.subprocess.run", side_effect=FileNotFoundError), pytest.raises(ToolExecutionError, match="not found"):
        func(["a.py"], root=tmp_path)


@pytest.mark.parametrize("module,func", [("app.verification.ruff_runner", run_ruff), ("app.verification.bandit_runner", run_bandit)])
def test_timeout(module, func, tmp_path):
    make_python_file(tmp_path)
    with patch(f"{module}.subprocess.run", side_effect=subprocess.TimeoutExpired("tool", 1)), pytest.raises(ToolExecutionError, match="timed out"):
        func(["a.py"], timeout=1, root=tmp_path)


@pytest.mark.parametrize("module,func", [("app.verification.ruff_runner", run_ruff), ("app.verification.bandit_runner", run_bandit)])
def test_malformed_json(module, func, tmp_path):
    make_python_file(tmp_path)
    with patch(f"{module}.subprocess.run", return_value=completed("not json")), pytest.raises(ToolExecutionError, match="malformed JSON"):
        func(["a.py"], root=tmp_path)


def test_invalid_tool_exit_code_is_execution_error(tmp_path):
    make_python_file(tmp_path)
    with patch("app.verification.ruff_runner.subprocess.run", return_value=completed("[]", 2, "bad args")), pytest.raises(ToolExecutionError, match="exit code 2"):
        run_ruff(["a.py"], root=tmp_path)


def test_test_runner_never_executes_supplied_target_or_command():
    with patch("subprocess.run") as run:
        result = run_tests(target="untrusted/path", command="malicious command")
    run.assert_not_called()
    assert result["status"] == "unavailable"
    assert result["executed"] is False
    assert "isolated" in result["message"]

@pytest.mark.parametrize("module,func,stdout", [
    ("app.verification.ruff_runner", run_ruff, "{}"),
    ("app.verification.bandit_runner", run_bandit, '{"results": {}}'),
])
def test_invalid_json_structure_is_reported(module, func, stdout, tmp_path):
    make_python_file(tmp_path)
    with patch(f"{module}.subprocess.run", return_value=completed(stdout)), pytest.raises(ToolExecutionError):
        func(["a.py"], root=tmp_path)


def test_ruff_skips_non_objects_and_sanitizes_malformed_fields(tmp_path):
    make_python_file(tmp_path)
    payload = [None, {
        "filename": 42,
        "location": ["bad"],
        "code": 17,
        "message": {"bad": "type"},
        "source_code": ["not", "text"],
    }, {
        "filename": "src\\nested\\a.py",
        "location": {"row": "8"},
        "code": "F401",
        "message": "unused",
        "cell": "  import os",
    }]
    with patch("app.verification.ruff_runner.subprocess.run", return_value=completed(json.dumps(payload))):
        result = run_ruff(["a.py"], root=tmp_path)
    assert result == [
        {"file_path": "", "line": None, "rule_id": None, "message": "", "evidence": None},
        {"file_path": "src/nested/a.py", "line": None, "rule_id": "F401", "message": "unused", "evidence": "  import os"},
    ]


def test_bandit_sanitizes_malformed_fields_and_code_formats(tmp_path):
    make_python_file(tmp_path)
    payload = {"results": [
        None,
        {"filename": "src\\bad.py", "line_number": True, "test_id": [], "issue_severity": {}, "issue_text": 3, "code": 5},
        {"filename": "src\\lines.py", "line_number": 4, "test_id": "B101", "issue_severity": "LOW", "issue_text": "assert", "code": ["  first\n", None, "second\r\n"]},
        {"filename": "src\\missing.py", "code": None},
    ]}
    with patch("app.verification.bandit_runner.subprocess.run", return_value=completed(json.dumps(payload))):
        result = run_bandit(["a.py"], root=tmp_path)
    assert result == [
        {"file_path": "src/bad.py", "line": None, "rule_id": None, "message": "", "severity": None, "evidence": None},
        {"file_path": "src/lines.py", "line": 4, "rule_id": "B101", "message": "assert", "severity": "LOW", "evidence": "  first\nsecond"},
        {"file_path": "src/missing.py", "line": None, "rule_id": None, "message": "", "severity": None, "evidence": None},
    ]


def test_normalizers_sanitize_malformed_records_into_valid_findings():
    for findings in (
        normalize_ruff([None, {"file_path": [], "line": True, "rule_id": {}, "message": None, "evidence": 4}]),
        normalize_bandit([None, {"file_path": None, "line": "4", "rule_id": [], "severity": [], "message": {}, "evidence": {}}]),
    ):
        assert all(isinstance(finding, Finding) for finding in findings)
        assert findings[0].file_path == ""
        assert findings[0].line is None
        assert findings[0].rule_id is None
        assert findings[0].message == ""
        assert findings[0].evidence is None
        assert findings[0].verification_status == "tool_flagged"


def test_normalizers_accept_non_iterable_input_safely():
    assert normalize_ruff(None) == []
    assert normalize_bandit(None) == []


def test_bandit_invalid_exit_code_is_execution_error(tmp_path):
    make_python_file(tmp_path)
    with patch("app.verification.bandit_runner.subprocess.run", return_value=completed("{}", 2, "bad args")), pytest.raises(ToolExecutionError, match="exit code 2"):
        run_bandit(["a.py"], root=tmp_path)


@pytest.mark.parametrize("func", [run_ruff, run_bandit])
def test_runner_requires_explicit_repository_root(func):
    with pytest.raises(TypeError, match="root"):
        func(["a.py"])


@pytest.mark.parametrize("func", [run_ruff, run_bandit])
def test_runner_accepts_valid_in_root_python_file_and_absolute_path(func, tmp_path):
    target = make_python_file(tmp_path, "nested/module.py")
    output = "[]" if func is run_ruff else '{"results": []}'
    with patch("subprocess.run", return_value=completed(output)) as call:
        func(["nested/module.py"], root=tmp_path)
    args = call.call_args.args[0]
    assert args[-1] == str(target.resolve())
    assert Path(args[-1]).is_absolute()
    assert call.call_args.kwargs["shell"] is not True if "shell" in call.call_args.kwargs else True


@pytest.mark.parametrize("func", [run_ruff, run_bandit])
def test_runner_rejects_parent_traversal_and_outside_absolute_path(func, tmp_path):
    outside = make_python_file(tmp_path.parent, f"{tmp_path.name}-outside.py")
    with pytest.raises(PathValidationError, match="outside"):
        func([f"../{outside.name}"], root=tmp_path)
    with pytest.raises(PathValidationError, match="outside"):
        func([outside.resolve()], root=tmp_path)


@pytest.mark.parametrize("func", [run_ruff, run_bandit])
def test_runner_rejects_symlink_escaping_repository_root(func, tmp_path):
    outside = make_python_file(tmp_path.parent, f"{tmp_path.name}-symlink-target.py")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(PathValidationError, match="outside"):
        func(["linked.py"], root=tmp_path)


@pytest.mark.parametrize("func", [run_ruff, run_bandit])
def test_runner_handles_leading_dash_filename_as_validated_absolute_path(func, tmp_path):
    target = make_python_file(tmp_path, "-config.py")
    output = "[]" if func is run_ruff else '{"results": []}'
    with patch("subprocess.run", return_value=completed(output)) as call:
        func(["-config.py"], root=tmp_path)
    argument = call.call_args.args[0][-1]
    assert argument == str(target.resolve())
    assert Path(argument).is_absolute()
    assert not argument.startswith("-")


@pytest.mark.parametrize("func", [run_ruff, run_bandit])
def test_runner_rejects_directories_and_invalid_root(func, tmp_path):
    (tmp_path / "folder").mkdir()
    with pytest.raises(PathValidationError, match="not a file"):
        func(["folder"], root=tmp_path)
    with pytest.raises(PathValidationError, match="repository root"):
        func(["a.py"], root=tmp_path / "missing-root")
