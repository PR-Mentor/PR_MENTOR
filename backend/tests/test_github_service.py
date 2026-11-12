import httpx
import pytest
from app.github_service.client import GitHubClient, GitHubServiceError
from app.github_service.files import fetch_pull_request_files
from app.github_service.parser import parse_pull_request_url
from app.github_service.pull_requests import ingest_pull_request


def sample_pr_response():
    return {
        "number": 12,
        "title": "Improve parser",
        "body": "Handle edge cases",
        "state": "open",
        "html_url": "https://github.com/acme/widget/pull/12",
        "user": {"login": "contributor"},
        "base": {"ref": "main", "sha": "base-sha"},
        "head": {"ref": "fix/parser", "sha": "head-sha"},
        "changed_files": 1,
    }


def sample_file_response():
    return {
        "filename": "src/parser.py",
        "previous_filename": None,
        "status": "modified",
        "additions": 2,
        "deletions": 1,
        "changes": 3,
        "sha": "file-sha",
        "patch": "@@ -1,2 +1,3 @@\n+return True",
    }


def test_parse_pull_request_url_accepts_github_tabs():
    coords = parse_pull_request_url("https://github.com/acme/widget/pull/12/files")

    assert coords.owner == "acme"
    assert coords.repo == "widget"
    assert coords.number == 12


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widget/pull/12",
        "https://github.com/acme/widget/issues/12",
        "https://evil.example/acme/widget/pull/12",
        "https://github.com/acme/widget/pull/0",
        "https://user@github.com/acme/widget/pull/12",
    ],
)
def test_parse_pull_request_url_rejects_non_pr_or_unsafe_urls(url):
    with pytest.raises(ValueError):
        parse_pull_request_url(url)


def test_ingest_pull_request_builds_agreed_payload():
    def handler(request):
        assert request.headers["Accept"] == "application/vnd.github+json"
        if request.url.path.endswith("/pulls/12"):
            return httpx.Response(200, json=sample_pr_response())
        if request.url.path.endswith("/pulls/12/files"):
            assert request.url.params["per_page"] == "100"
            assert request.url.params["page"] == "1"
            return httpx.Response(200, json=[sample_file_response()])
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        payload = ingest_pull_request(
            "https://github.com/acme/widget/pull/12",
            client=GitHubClient(http_client=http_client, token="test-token"),
        )

    assert payload["pull_request"] == {
        "owner": "acme",
        "repo": "widget",
        "number": 12,
        "title": "Improve parser",
        "body": "Handle edge cases",
        "state": "open",
        "html_url": "https://github.com/acme/widget/pull/12",
        "author": "contributor",
        "base_ref": "main",
        "base_sha": "base-sha",
        "head_ref": "fix/parser",
        "head_sha": "head-sha",
        "changed_files": 1,
    }
    assert payload["files"] == [
        {
            **sample_file_response(),
            "source_content": None,
            "content_unavailable_reason": None,
        }
    ]
    assert payload["files_truncated"] is False


def test_ingest_marks_file_list_truncated_at_page_limit():
    class FullPageClient:
        def get_json(self, path, *, params):
            assert path.endswith("/pulls/12/files")
            return [sample_file_response()] * 100

    files, truncated = fetch_pull_request_files(
        FullPageClient(), "acme", "widget", 12, max_pages=1
    )

    assert len(files) == 100
    assert truncated is True


def test_github_404_is_reported_without_leaking_request_details():
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ) as http_client:
        client = GitHubClient(http_client=http_client, token="secret-token")

        with pytest.raises(GitHubServiceError, match="not found") as error:
            client.get_json("/repos/acme/widget/pulls/12")

    assert error.value.status_code == 404
    assert "secret-token" not in str(error.value)


def test_malformed_pull_request_response_is_an_upstream_error():
    class MalformedResponseClient:
        def get_json(self, _path, *, params=None):
            return {}

    with pytest.raises(GitHubServiceError, match="malformed") as error:
        ingest_pull_request(
            "https://github.com/acme/widget/pull/12",
            client=MalformedResponseClient(),
        )

    assert error.value.status_code == 502

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from app.github_service import client as github_client
from app.github_service import pull_requests
from app.github_service.client import GitHubAPIError, request_json
from app.github_service.files import parse_patch
from app.github_service.parser import InvalidPullRequestURL, parse_pull_request_url


@pytest.mark.parametrize("value", [
    "http://github.com/o/r/pull/1", "https://github.com.evil.test/o/r/pull/1",
    "https://user@github.com/o/r/pull/1", "https://github.com/o/r/issues/1",
    "https://github.com/o/r/pull/0", "https://github.com/o/r/pull/1/extra",
    "https://127.0.0.1/o/r/pull/1",
])
def test_parser_rejects_unsafe_or_non_pr_urls(value):
    with pytest.raises(InvalidPullRequestURL):
        parse_pull_request_url(value)


def test_parser_accepts_canonical_github_url():
    assert parse_pull_request_url("https://github.com/owner/repo/pull/42?tab=files") == ("owner", "repo", 42)
    assert parse_pull_request_url("https://github.com/owner/repo/pull/42#diff") == ("owner", "repo", 42)


@pytest.mark.anyio
async def test_fetch_file_snapshot_verifies_exact_commit_blob_and_utf8(monkeypatch, anyio_backend):
    content = "print('✓')\r\n".encode()
    sha = pull_requests.git_blob_sha(content)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    calls = []

    async def response(_client, path, *, params=None, use_cache=True):
        calls.append((path, params, use_cache))
        if path.endswith("/git/commits/" + "a" * 40):
            return {"tree": {"sha": "e" * 40}}
        if path.endswith("/git/trees/" + "e" * 40):
            return {"tree": [{"path": "src", "mode": "040000", "type": "tree", "sha": "f" * 40}]}
        if path.endswith("/git/trees/" + "f" * 40):
            return {"tree": [{"path": "a.py", "mode": "100644", "type": "blob", "sha": sha}]}
        return {
            "type": "file", "path": "src/a.py", "sha": sha,
            "size": len(content), "encoding": "base64",
            "content": base64.b64encode(content).decode(),
        }

    monkeypatch.setattr(pull_requests, "async_client", FakeClient)
    monkeypatch.setattr(pull_requests, "request_json", response)
    snapshot = await pull_requests.fetch_file_snapshot("org", "repo", "src/a.py", "a" * 40)
    assert snapshot.text == content.decode()
    assert snapshot.content == content
    assert snapshot.blob_sha == sha
    assert [call[0] for call in calls] == [
        "/repos/org/repo/git/commits/" + "a" * 40,
        "/repos/org/repo/git/trees/" + "e" * 40,
        "/repos/org/repo/git/trees/" + "f" * 40,
        "/repos/org/repo/contents/src/a.py",
    ]
    assert calls[-1][1:] == ({"ref": "a" * 40}, False)


@pytest.mark.parametrize("response_patch", [
    {"type": "symlink"},
    {"path": "src/other.py"},
    {"sha": "z" * 40},
    {"size": 100_001},
    {"size": 99},
    {"content": "%%%"},
    {"raw_content": b"\xff"},
    {"tree_sha": "0" * 40},
    {"tree_mode": "120000"},
    {"tree_mode": "160000", "tree_type": "commit"},
    {"tree_mode": "040000", "tree_type": "tree"},
])
@pytest.mark.anyio
async def test_fetch_file_snapshot_rejects_unverified_github_objects(monkeypatch, response_patch, anyio_backend):
    content = response_patch.get("raw_content", b"print(1)\n")
    result = {
        "type": "file", "path": "src/a.py",
        "sha": pull_requests.git_blob_sha(content), "size": len(content),
        "encoding": "base64", "content": base64.b64encode(content).decode(),
    }
    result.update(response_patch)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def response(_client, path, **_kwargs):
        if path.endswith("/git/commits/" + "a" * 40):
            return {"tree": {"sha": "e" * 40}}
        if path.endswith("/git/trees/" + "e" * 40):
            return {"tree": [{"path": "src", "mode": "040000", "type": "tree", "sha": "f" * 40}]}
        if path.endswith("/git/trees/" + "f" * 40):
            mode = response_patch.get("tree_mode", "100644")
            entry_type = response_patch.get("tree_type", "blob")
            blob_sha = response_patch.get("tree_sha", result["sha"])
            return {"tree": [{"path": "a.py", "mode": mode, "type": entry_type, "sha": blob_sha}]}
        return result

    monkeypatch.setattr(pull_requests, "async_client", FakeClient)
    monkeypatch.setattr(pull_requests, "request_json", response)
    with pytest.raises(pull_requests.GitHubFileSnapshotError):
        await pull_requests.fetch_file_snapshot("org", "repo", "src/a.py", "a" * 40)


@pytest.mark.anyio
async def test_fetch_file_snapshot_rejects_missing_tree_entry(monkeypatch, anyio_backend):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def response(_client, path, **_kwargs):
        if "/git/commits/" in path:
            return {"tree": {"sha": "e" * 40}}
        return {"tree": []}

    monkeypatch.setattr(pull_requests, "async_client", FakeClient)
    monkeypatch.setattr(pull_requests, "request_json", response)
    with pytest.raises(pull_requests.GitHubFileSnapshotError, match="does not exist"):
        await pull_requests.fetch_file_snapshot("org", "repo", "src/a.py", "a" * 40)


@pytest.mark.anyio
async def test_fetch_file_snapshot_preserves_github_api_failures(monkeypatch, anyio_backend):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def response(*_args, **_kwargs):
        raise GitHubAPIError("rate limited", 429)

    monkeypatch.setattr(pull_requests, "async_client", FakeClient)
    monkeypatch.setattr(pull_requests, "request_json", response)
    with pytest.raises(GitHubAPIError) as exc:
        await pull_requests.fetch_file_snapshot("org", "repo", "src/a.py", "a" * 40)
    assert exc.value.status_code == 429


def test_patch_parser_tracks_added_removed_and_context_lines():
    parsed = parse_patch({"filename": "a.py", "previous_filename": "old.py", "status": "renamed", "additions": 1, "deletions": 1, "patch": "@@ -4,2 +4,2 @@ def f():\n keep\n-old\n+new"})
    assert parsed["previous_path"] == "old.py" and parsed["truncated"] is False
    assert parsed["hunks"][0]["lines"] == [
        {"kind": "context", "line": 4, "text": "keep"},
        {"kind": "removed", "line": 5, "text": "old"},
        {"kind": "added", "line": 5, "text": "new"},
    ]


def test_patch_parser_marks_missing_patch_as_truncated():
    assert parse_patch({"filename": "image.png", "status": "added"})["truncated"] is True


def test_github_client_uses_fixed_host_and_maps_rate_limit():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, json={"message": "rate limit"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            with pytest.raises(GitHubAPIError) as error:
                await request_json(client, "/repos/owner/repo/pulls/1")
        return error.value

    exc = asyncio.run(run())
    assert exc.status_code == 429
    assert requests[0].url.host == "api.github.com"


def test_primary_rate_limit_includes_headers_and_reset_without_long_retry(monkeypatch):
    github_client._response_cache.clear()
    requests = []
    reset = "2000000000"

    def handler(request):
        requests.append(request)
        return httpx.Response(403, headers={
            "X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "0",
            "X-RateLimit-Resource": "core", "X-RateLimit-Reset": reset,
        }, json={"message": "API rate limit exceeded"})

    async def no_sleep(_delay):
        raise AssertionError("A far-future reset must not trigger a long wait")

    monkeypatch.setattr(github_client.asyncio, "sleep", no_sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(GitHubAPIError) as error:
                await request_json(client, "/repos/primary-limit/pulls/1")
        return error.value

    exc = asyncio.run(run())
    assert exc.kind == "primary"
    assert exc.status_code == 429
    assert exc.rate_limit == {
        "x-ratelimit-limit": "60", "x-ratelimit-remaining": "0",
        "x-ratelimit-resource": "core", "x-ratelimit-reset": reset,
    }
    assert "primary rate limit" in str(exc)
    assert "Try again after" in str(exc)
    assert len(requests) == 1


def test_secondary_rate_limit_retries_with_bounded_backoff(monkeypatch):
    github_client._response_cache.clear()
    requests = []
    delays = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01", "X-RateLimit-Resource": "core"}, json={"message": "You have exceeded a secondary rate limit."})
        return httpx.Response(200, json={"ok": True})

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(github_client.asyncio, "sleep", record_sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await request_json(client, "/repos/secondary-limit/pulls/1")

    assert asyncio.run(run()) == {"ok": True}
    assert len(requests) == 2
    assert delays == [0.25]


def test_secondary_rate_limit_error_surfaces_retry_after(monkeypatch):
    github_client._response_cache.clear()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"message": "secondary rate limit"})

    async def no_sleep(_delay):
        raise AssertionError("Retry-After exceeding the bounded wait must return immediately")

    monkeypatch.setattr(github_client.asyncio, "sleep", no_sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(GitHubAPIError) as error:
                await request_json(client, "/repos/secondary-later/pulls/1")
        return error.value

    exc = asyncio.run(run())
    assert exc.kind == "secondary"
    assert exc.rate_limit["retry-after"] == "30"
    assert "Retry after 30 seconds" in str(exc)
    assert len(requests) == 1


def test_secondary_403_with_retry_after_is_classified():
    response = httpx.Response(403, headers={"Retry-After": "0"})
    assert github_client._rate_limit_kind(response, github_client._rate_limit_headers(response), "Forbidden") == "secondary"


def test_ordinary_403_is_not_classified_as_rate_limit():
    response = httpx.Response(403, json={"message": "Resource forbidden"})
    assert github_client._rate_limit_kind(response, github_client._rate_limit_headers(response), "Resource forbidden") is None

    github_client._response_cache.clear()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: response)) as client:
            with pytest.raises(GitHubAPIError) as error:
                await request_json(client, "/repos/ordinary-forbidden/pulls/1")
        return error.value

    error = asyncio.run(run())
    assert error.status_code == 403
    assert error.kind is None


def test_retry_after_http_date_uses_timezone_aware_bounded_delay(monkeypatch):
    now = datetime.now(UTC)
    date_value = format_datetime(now + timedelta(seconds=1), usegmt=True)
    monkeypatch.setattr(github_client, "BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setattr(github_client, "BACKOFF_MAX_SECONDS", 2.0)
    delay = github_client._retry_delay(0, {"retry-after": date_value}, "secondary")
    assert delay is not None and 0 < delay <= 1.1
    assert github_client._retry_delay(0, {"retry-after": "not-a-date"}, "secondary") == 0.01


def test_retry_after_zero_and_malformed_values_use_bounded_backoff(monkeypatch):
    monkeypatch.setattr(github_client, "BACKOFF_BASE_SECONDS", 0.1)
    assert github_client._retry_after_seconds("0") == 0
    assert github_client._retry_delay(0, {"retry-after": "0"}, "secondary") == 0.1
    assert github_client._retry_after_seconds("nonsense") is None
    assert github_client._retry_delay(0, {"retry-after": "nonsense"}, "secondary") == 0.1
    assert github_client._retry_after_seconds("NaN") is None


def test_cache_returns_mutation_isolated_values():
    github_client._response_cache.clear()
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(200, json={"nested": {"values": [1, 2]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await request_json(client, "/repos/cache-isolation/pulls/1")
            first["nested"]["values"].append(3)
            second = await request_json(client, "/repos/cache-isolation/pulls/1")
            second["nested"]["values"].append(4)
            third = await request_json(client, "/repos/cache-isolation/pulls/1")
            return first, second, third

    first, second, third = asyncio.run(run())
    assert first["nested"]["values"] == [1, 2, 3]
    assert second["nested"]["values"] == [1, 2, 4]
    assert third["nested"]["values"] == [1, 2]
    assert len(calls) == 1


def test_concurrent_identical_requests_are_coalesced():
    github_client._response_cache.clear()
    github_client._inflight_requests.clear()
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    class BarrierTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(1)
            started.set()
            await release.wait()
            return httpx.Response(200, json={"value": []}, request=request)

    async def run():
        async with httpx.AsyncClient(transport=BarrierTransport()) as client:
            first = asyncio.create_task(request_json(client, "/repos/coalesced/pulls/1"))
            await started.wait()
            second = asyncio.create_task(request_json(client, "/repos/coalesced/pulls/1"))
            release.set()
            return await asyncio.gather(first, second)

    first, second = asyncio.run(run())
    first["value"].append(1)
    assert second["value"] == []
    assert len(calls) == 1
    assert not github_client._inflight_requests


def test_cancelled_waiter_does_not_cancel_shared_request():
    github_client._response_cache.clear()
    github_client._inflight_requests.clear()
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    class BarrierTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(1)
            started.set()
            await release.wait()
            return httpx.Response(200, json={"value": "ok"}, request=request)

    async def run():
        async with httpx.AsyncClient(transport=BarrierTransport()) as client:
            owner = asyncio.create_task(request_json(client, "/repos/cancel-safe/pulls/1"))
            await started.wait()
            waiter = asyncio.create_task(request_json(client, "/repos/cancel-safe/pulls/1"))
            await asyncio.sleep(0)
            owner.cancel()
            await asyncio.sleep(0)
            assert not owner.done()
            release.set()
            owner_result, waiter_result = await asyncio.gather(owner, waiter, return_exceptions=True)
            return owner_result, waiter_result

    owner_result, waiter_result = asyncio.run(run())
    assert isinstance(owner_result, asyncio.CancelledError)
    assert waiter_result == {"value": "ok"}
    assert len(calls) == 1
    assert not github_client._inflight_requests


def test_concurrent_different_keys_remain_independent():
    github_client._response_cache.clear()
    github_client._inflight_requests.clear()
    paths = set()
    both_started = asyncio.Event()
    release = asyncio.Event()

    class BarrierTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            paths.add(request.url.path)
            if len(paths) == 2:
                both_started.set()
            await release.wait()
            return httpx.Response(200, json={"path": request.url.path}, request=request)

    async def run():
        async with httpx.AsyncClient(transport=BarrierTransport()) as client:
            tasks = [
                asyncio.create_task(request_json(client, "/repos/independent-a/pulls/1")),
                asyncio.create_task(request_json(client, "/repos/independent-b/pulls/1")),
            ]
            await asyncio.wait_for(both_started.wait(), timeout=1)
            release.set()
            return await asyncio.gather(*tasks)

    results = asyncio.run(run())
    assert len(paths) == 2
    assert len(results) == 2


def test_inflight_registry_cleans_up_after_exception():
    github_client._response_cache.clear()
    github_client._inflight_requests.clear()

    def handler(_request):
        raise httpx.ConnectError("connection failed")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(GitHubAPIError):
                await request_json(client, "/repos/exception-cleanup/pulls/1")
            await asyncio.sleep(0)

    asyncio.run(run())
    assert not github_client._inflight_requests


def test_rate_limit_retries_stop_at_exact_limit(monkeypatch):
    github_client._response_cache.clear()
    github_client._inflight_requests.clear()
    requests = []
    delays = []

    def handler(_request):
        requests.append(1)
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "secondary rate limit"})

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(github_client.asyncio, "sleep", record_sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(GitHubAPIError):
                await request_json(client, "/repos/retry-limit/pulls/1")

    asyncio.run(run())
    assert len(requests) == github_client.MAX_RETRIES + 1
    assert delays == [github_client.BACKOFF_BASE_SECONDS, github_client.BACKOFF_BASE_SECONDS * 2]
