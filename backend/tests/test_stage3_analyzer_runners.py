import subprocess
import sys

import pytest

from app.verification import bandit_runner, ruff_runner
from app.verification.analyzer_common import controlled_environment
from app.verification.bandit_runner import run_bandit_source
from app.verification.bounded_process import BoundedProcessError, run_bounded_process
from app.verification.ruff_runner import run_ruff_source


def test_ruff_source_run_returns_clean_metadata_and_finds_unused_import():
    clean = run_ruff_source("value = 1\n", "pkg/sample.py", timeout=10)
    findings = run_ruff_source("import os\n", "pkg/sample.py", timeout=10)
    assert clean.tool_name == "ruff"
    assert clean.tool_version
    assert clean.configuration_sha256 == findings.configuration_sha256
    assert clean.findings == ()
    assert findings.findings[0]["rule_id"] == "F401"
    assert findings.findings[0]["file_path"] == "pkg/sample.py"
    assert findings.findings[0]["column"]


def test_bandit_source_run_preserves_severity_and_confidence():
    clean = run_bandit_source("value = 1\n", "pkg/sample.py", timeout=10)
    findings = run_bandit_source("assert True\n", "pkg/sample.py", timeout=10)
    assert clean.tool_name == "bandit"
    assert clean.tool_version
    assert clean.configuration_sha256 == findings.configuration_sha256
    assert clean.findings == ()
    assert findings.findings[0]["rule_id"] == "B101"
    assert findings.findings[0]["severity"] == "LOW"
    assert findings.findings[0]["confidence"] == "HIGH"
    assert findings.findings[0]["line"] == 1


def test_source_runners_enforce_target_path_and_size_boundaries():
    with pytest.raises(ValueError):
        run_ruff_source("x=1", "../escape.py")
    with pytest.raises(ValueError):
        run_bandit_source("x" * 100_001, "sample.py")


@pytest.mark.parametrize(
    "payload",
    [
        b"[null]",
        b'[{"filename":"sample.py","code":"F821","message":"name"}]',
    ],
)
def test_ruff_source_rejects_malformed_findings(monkeypatch, payload):
    monkeypatch.setattr(ruff_runner, "tool_version", lambda *_args, **_kwargs: "ruff 1")
    monkeypatch.setattr(
        ruff_runner,
        "run_tool_process",
        lambda *_args, **_kwargs: (payload, b"", 0, 0.01),
    )
    with pytest.raises(BoundedProcessError):
        run_ruff_source("value = 1\n", "sample.py")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"results":[null]}',
        b'{"results":[{"test_id":"B101","issue_text":"assert"}]}',
    ],
)
def test_bandit_source_rejects_malformed_findings(monkeypatch, payload):
    monkeypatch.setattr(bandit_runner, "tool_version", lambda *_args, **_kwargs: "bandit 1")
    monkeypatch.setattr(
        bandit_runner,
        "run_tool_process",
        lambda *_args, **_kwargs: (payload, b"", 0, 0.01),
    )
    with pytest.raises(BoundedProcessError):
        run_bandit_source("value = 1\n", "sample.py")


def test_controlled_environment_omits_application_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("LLM_API_KEY", "secret-llm-key")
    env = controlled_environment(tmp_path)
    assert "GITHUB_TOKEN" not in env
    assert "LLM_API_KEY" not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"


def test_bounded_process_caps_stdout_and_stderr(tmp_path):
    command = [
        sys.executable,
        "-c",
        "import sys;sys.stdout.write('x'*200000);sys.stderr.write('y'*200000)",
    ]
    with pytest.raises(BoundedProcessError, match="output limit"):
        run_bounded_process(
            command,
            cwd=tmp_path,
            env=controlled_environment(tmp_path),
            timeout=5,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )


def test_bounded_process_kills_timed_out_command(tmp_path):
    with pytest.raises(BoundedProcessError, match="timed out"):
        run_bounded_process(
            [sys.executable, "-c", "import time;time.sleep(10)"],
            cwd=tmp_path,
            env=controlled_environment(tmp_path),
            timeout=0.1,
        )


def test_bounded_process_cooperatively_cancels_child(tmp_path):
    result = run_bounded_process(
        [sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM, lambda *_: exit(0));time.sleep(30)"],
        cwd=tmp_path,
        env=controlled_environment(tmp_path),
        timeout=5,
        cancellation_check=lambda: True,
    )
    assert result.cancelled is True


@pytest.mark.parametrize("timeout", [0, float("inf"), float("nan"), True])
def test_bounded_process_rejects_invalid_timeouts(tmp_path, timeout):
    with pytest.raises(ValueError, match="limits must be positive"):
        run_bounded_process(
            [sys.executable, "-c", "print('unreachable')"],
            cwd=tmp_path,
            env=controlled_environment(tmp_path),
            timeout=timeout,
        )


def test_bounded_process_uses_no_shell_and_returns_exit_code(tmp_path, monkeypatch):
    calls = []
    original = subprocess.Popen

    def checked_popen(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", checked_popen)
    result = run_bounded_process(
        [sys.executable, "-c", "print('ok')"],
        cwd=tmp_path,
        env=controlled_environment(tmp_path),
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == [b"ok"]
    assert calls[0]["shell"] is False
    assert calls[0]["stdin"] == subprocess.DEVNULL
    assert calls[0]["cwd"] == str(tmp_path.resolve())
