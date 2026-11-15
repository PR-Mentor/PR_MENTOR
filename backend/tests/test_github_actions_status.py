from __future__ import annotations

import asyncio

import pytest

from app.github_service import actions_status
from app.verification.test_runner import run_tests


def test_review_placeholder_defers_to_github_actions_without_sandbox_warning():
    result = run_tests(target="ignored", command="ignored")
    assert result["status"] == "pending"
    assert result["executed"] is False
    assert "GitHub Actions" in result["message"]
    assert "sandbox" not in result["message"].lower()

def _run(sha: str, **overrides):
    value = {
        "id": 123, "run_number": 7, "name": "PR Tests",
        "path": ".github/workflows/pr-tests.yml", "head_sha": sha,
        "status": "completed", "conclusion": "success",
        "html_url": "https://github.com/acme/project/actions/runs/123",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(("run", "expected"), [
    ({"status": "queued", "conclusion": None}, "queued"),
    ({"status": "in_progress", "conclusion": None}, "in_progress"),
    ({"status": "completed", "conclusion": "success"}, "passed"),
    ({"status": "completed", "conclusion": "failure"}, "failed"),
    ({"status": "completed", "conclusion": "cancelled"}, "cancelled"),
    ({"status": "completed", "conclusion": "timed_out"}, "timed_out"),
    ({"status": "completed", "conclusion": "skipped"}, "skipped"),
])
def test_status_mapping(monkeypatch, run, expected):
    sha = "a" * 40
    async def fake_json(*_args, **_kwargs):
        return {"workflow_runs": [_run(sha, **run)]}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
    monkeypatch.setattr(actions_status, "async_client", lambda: Client())
    monkeypatch.setattr(actions_status, "request_json", fake_json)
    result = asyncio.run(actions_status.get_pr_test_status("acme", "project", 1, sha))
    assert result["status"] == expected
    assert (result["status"] == "passed") is (result["executed"] and result["conclusion"] == "success")


def test_sha_and_workflow_identity_are_required(monkeypatch):
    sha = "b" * 40
    async def fake_json(*_args, **_kwargs):
        return {"workflow_runs": [_run(sha, path=".github/workflows/other.yml")]}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
    monkeypatch.setattr(actions_status, "async_client", lambda: Client())
    monkeypatch.setattr(actions_status, "request_json", fake_json)
    assert asyncio.run(actions_status.get_pr_test_status("acme", "project", 1, "short"))["status"] == "unavailable"
    assert asyncio.run(actions_status.get_pr_test_status("acme", "project", 1, sha))["status"] == "not_configured"


@pytest.mark.parametrize("bad_sha", [None, 1, [], "short"])
def test_invalid_sha_fails_closed_without_raising(bad_sha):
    result = asyncio.run(actions_status.get_pr_test_status("acme", "project", 1, bad_sha))
    assert result["status"] == "unavailable"
    assert result["executed"] is False
    assert result["head_sha"] is None


def test_invalid_external_link_is_not_returned(monkeypatch):
    sha = "c" * 40
    async def fake_json(*_args, **_kwargs):
        return {"workflow_runs": [_run(sha, html_url="https://evil.example/run")]}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
    monkeypatch.setattr(actions_status, "async_client", lambda: Client())
    monkeypatch.setattr(actions_status, "request_json", fake_json)
    result = asyncio.run(actions_status.get_pr_test_status("acme", "project", 1, sha))
    assert result["status"] == "passed"
    assert result["html_url"] is None
