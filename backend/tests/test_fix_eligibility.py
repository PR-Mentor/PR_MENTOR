import asyncio
import base64
from datetime import UTC, datetime

import pytest

from app.github_service import pull_requests
from app.github_service.pull_requests import GitHubFileSnapshot, git_blob_sha
from app.schemas.issue import SuggestedFix
from app.services import fix_eligibility
from app.verification.fix_snapshot import reconstruct_proposed_source

BASE = "a" * 40
HEAD = "b" * 40
SOURCE = "value = 1\r\nprint(value)\r\n"


def make_fix(**overrides):
    data = {
        "fix_id": "f1",
        "canonical_issue_id": "i1",
        "file_path": "src/a.py",
        "start_line": 1,
        "end_line": 1,
        "language": "python",
        "base_commit_sha": BASE,
        "file_blob_sha": git_blob_sha(SOURCE.encode()),
        "expected_original_code": "value = 1",
        "proposed_code": "value = 2",
        "change_summary": "Change value",
        "rationale": "Correct value",
        "confidence": 0.8,
        "generation_status": "generated",
        "generated_at": datetime.now(UTC),
    }
    data.update(overrides)
    return SuggestedFix(**data)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_reconstruction_preserves_crlf_and_final_newline():
    result = reconstruct_proposed_source(SOURCE, make_fix())
    assert result.text == "value = 2\r\nprint(value)\r\n"
    assert result.blob_sha == git_blob_sha(result.content)


@pytest.mark.parametrize("source", ["other = 1\n"])
def test_reconstruction_requires_exact_line_range(source):
    with pytest.raises(ValueError):
        reconstruct_proposed_source(source, make_fix())


def test_reconstruction_changes_only_the_declared_occurrence():
    result = reconstruct_proposed_source("value = 1\nvalue = 1\n", make_fix())
    assert result.text == "value = 2\nvalue = 1\n"


@pytest.mark.parametrize(
    "path",
    [
        "/root.py",
        "../a.py",
        "src/../a.py",
        "src\\a.py",
        "C:/a.py",
        "src//a.py",
        "src/a.txt",
    ],
)
def test_paths_reject_unsafe_or_non_python_paths(path):
    with pytest.raises(ValueError):
        reconstruct_proposed_source(SOURCE, make_fix(file_path=path))


def test_reconstruction_preserves_no_final_newline():
    result = reconstruct_proposed_source("value = 1", make_fix())
    assert result.text == "value = 2"


def test_reconstruction_handles_lf_unicode_and_empty_source():
    result = reconstruct_proposed_source(
        "# café\nvalue = 1\n", make_fix(start_line=2, end_line=2)
    )
    assert result.text == "# café\nvalue = 2\n"
    assert result.blob_sha == git_blob_sha(result.content)
    with pytest.raises(ValueError, match="range is outside"):
        reconstruct_proposed_source("", make_fix())


def test_reconstruction_rejects_invalid_line_range():
    with pytest.raises(ValueError, match="outside the source"):
        reconstruct_proposed_source("value = 1\n", make_fix(start_line=2, end_line=2))


@pytest.mark.anyio
async def test_eligibility_accepts_changed_head_when_file_unchanged(
    monkeypatch, anyio_backend
):
    async def head(*_args):
        return HEAD

    async def snapshot(owner, repo, path, commit):
        assert (owner, repo, path) == ("org", "repo", "src/a.py")
        assert commit in {BASE, HEAD}
        return GitHubFileSnapshot(
            path, commit, git_blob_sha(SOURCE.encode()), SOURCE.encode(), SOURCE
        )

    monkeypatch.setattr(fix_eligibility, "fetch_current_pull_request_head_sha", head)
    monkeypatch.setattr(fix_eligibility, "fetch_file_snapshot", snapshot)
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org", repo="repo", pull_request_number=1
    )
    assert result.status == "eligible"
    assert result.snapshot.checked_head_sha == HEAD
    assert result.snapshot.baseline_blob_sha == git_blob_sha(SOURCE.encode())
    assert result.snapshot.proposed_blob_sha == git_blob_sha(
        b"value = 2\r\nprint(value)\r\n"
    )


@pytest.mark.anyio
async def test_eligibility_marks_changed_target_stale(monkeypatch, anyio_backend):
    changed = "value = 3\nprint(value)\n"

    async def head(*_args):
        return HEAD

    async def snapshot(_owner, _repo, path, commit):
        text = SOURCE if commit == BASE else changed
        content = text.encode()
        return GitHubFileSnapshot(path, commit, git_blob_sha(content), content, text)

    monkeypatch.setattr(fix_eligibility, "fetch_current_pull_request_head_sha", head)
    monkeypatch.setattr(fix_eligibility, "fetch_file_snapshot", snapshot)
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org", repo="repo", pull_request_number=1
    )
    assert result.status == "stale"
    assert result.snapshot is None


@pytest.mark.anyio
async def test_eligibility_rejects_original_blob_mismatch(monkeypatch, anyio_backend):
    async def head(*_args):
        return BASE

    async def snapshot(_owner, _repo, path, commit):
        other = b"value = 0\n"
        return GitHubFileSnapshot(
            path, commit, git_blob_sha(other), other, other.decode()
        )

    monkeypatch.setattr(fix_eligibility, "fetch_current_pull_request_head_sha", head)
    monkeypatch.setattr(fix_eligibility, "fetch_file_snapshot", snapshot)
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org", repo="repo", pull_request_number=1
    )
    assert result.status == "conflicted"


@pytest.mark.anyio
async def test_eligibility_accepts_unchanged_head_without_second_snapshot(
    monkeypatch, anyio_backend
):
    calls = []

    async def head(*_args):
        return BASE

    async def snapshot(*args):
        calls.append(args[-1])
        content = SOURCE.encode()
        return GitHubFileSnapshot(
            "src/a.py", BASE, git_blob_sha(content), content, SOURCE
        )

    monkeypatch.setattr(fix_eligibility, "fetch_current_pull_request_head_sha", head)
    monkeypatch.setattr(fix_eligibility, "fetch_file_snapshot", snapshot)
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org", repo="repo", pull_request_number=1
    )
    assert result.status == "eligible"
    assert calls == [BASE]


@pytest.mark.anyio
async def test_eligibility_integrates_exact_commit_tree_and_content_reads(
    monkeypatch, anyio_backend
):
    content = SOURCE.encode()
    blob_sha = git_blob_sha(content)
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def head(*_args):
        return HEAD

    async def request(_client, path, *, params=None, use_cache=True):
        calls.append((path, params, use_cache))
        if path.endswith("/git/commits/" + BASE):
            return {"tree": {"sha": "e" * 40}}
        if path.endswith("/git/commits/" + HEAD):
            return {"tree": {"sha": "f" * 40}}
        if path.endswith(("/git/trees/" + "e" * 40, "/git/trees/" + "f" * 40)):
            return {
                "tree": [
                    {"path": "src", "mode": "040000", "type": "tree", "sha": "c" * 40}
                ]
            }
        if path.endswith("/git/trees/" + "c" * 40):
            return {
                "tree": [
                    {"path": "a.py", "mode": "100644", "type": "blob", "sha": blob_sha}
                ]
            }
        return {
            "type": "file",
            "path": "src/a.py",
            "sha": blob_sha,
            "size": len(content),
            "encoding": "base64",
            "content": base64.b64encode(content).decode(),
        }

    monkeypatch.setattr(fix_eligibility, "fetch_current_pull_request_head_sha", head)
    monkeypatch.setattr(pull_requests, "async_client", FakeClient)
    monkeypatch.setattr(pull_requests, "request_json", request)
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org", repo="repo", pull_request_number=1
    )
    assert result.status == "eligible"
    assert result.snapshot.checked_head_sha == HEAD
    assert result.baseline_source == SOURCE
    assert result.proposed_source == "value = 2\r\nprint(value)\r\n"
    assert calls and all(use_cache is False for _path, _params, use_cache in calls)


@pytest.mark.anyio
async def test_eligibility_rejects_invalid_repository_identity_without_requests(
    monkeypatch, anyio_backend
):
    async def unexpected(*_args):
        raise AssertionError(
            "Invalid identities must be rejected before network access"
        )

    monkeypatch.setattr(
        fix_eligibility, "fetch_current_pull_request_head_sha", unexpected
    )
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org/pulls/other", repo="repo", pull_request_number="1"
    )
    assert result.status == "unavailable"
    assert result.snapshot is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error, expected",
    [
        (fix_eligibility.GitHubAPIError("missing", 404), "unavailable"),
        (fix_eligibility.GitHubAPIError("forbidden", 403), "unavailable"),
        (fix_eligibility.GitHubAPIError("rate limit", 429), "unavailable"),
        (OSError("network down"), "unavailable"),
    ],
)
async def test_eligibility_maps_retrieval_failures(
    monkeypatch, anyio_backend, error, expected
):
    async def head(*_args):
        return BASE

    async def snapshot(*_args):
        raise error

    monkeypatch.setattr(fix_eligibility, "fetch_current_pull_request_head_sha", head)
    monkeypatch.setattr(fix_eligibility, "fetch_file_snapshot", snapshot)
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org", repo="repo", pull_request_number=1
    )
    assert result.status == expected
    assert result.snapshot is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change, expected",
    [("deleted", "stale"), ("renamed", "stale"), (None, "unavailable")],
)
async def test_missing_current_source_requires_delete_or_rename_confirmation(
    monkeypatch, anyio_backend, change, expected
):
    async def head(*_args):
        return HEAD

    async def snapshot(_owner, _repo, _path, commit):
        if commit == HEAD:
            raise fix_eligibility.GitHubAPIError("missing", 404)
        content = SOURCE.encode()
        return GitHubFileSnapshot(
            "src/a.py", BASE, git_blob_sha(content), content, SOURCE
        )

    async def current_change(*_args):
        return change

    monkeypatch.setattr(fix_eligibility, "fetch_current_pull_request_head_sha", head)
    monkeypatch.setattr(fix_eligibility, "fetch_file_snapshot", snapshot)
    monkeypatch.setattr(
        fix_eligibility, "fetch_current_pull_request_file_change", current_change
    )
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org", repo="repo", pull_request_number=1
    )
    assert result.status == expected
    assert result.snapshot is None


@pytest.mark.anyio
async def test_eligibility_timeout_is_explicit_and_cancels_work(
    monkeypatch, anyio_backend
):
    cancelled = False

    async def head(*_args):
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(fix_eligibility, "MAX_GITHUB_OPERATION_SECONDS", 0.01)
    monkeypatch.setattr(fix_eligibility, "fetch_current_pull_request_head_sha", head)
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org", repo="repo", pull_request_number=1
    )
    assert result.status == "unavailable"
    assert "time limit" in result.reason
    assert cancelled


@pytest.mark.anyio
async def test_snapshot_http_call_is_uncached_and_cancellable(
    monkeypatch, anyio_backend
):
    cancelled = False

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def head(*_args):
        return BASE

    async def delayed_request(_client, _path, *, params=None, use_cache=True):
        nonlocal cancelled
        assert use_cache is False
        try:
            await __import__("asyncio").sleep(1)
        except __import__("asyncio").CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(fix_eligibility, "MAX_GITHUB_OPERATION_SECONDS", 0.01)
    monkeypatch.setattr(fix_eligibility, "fetch_current_pull_request_head_sha", head)
    monkeypatch.setattr(pull_requests, "async_client", FakeClient)
    monkeypatch.setattr(pull_requests, "request_json", delayed_request)
    result = await fix_eligibility.evaluate_fix_eligibility(
        make_fix(), owner="org", repo="repo", pull_request_number=1
    )
    assert result.status == "unavailable"
    assert cancelled
