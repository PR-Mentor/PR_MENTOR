import base64
import sqlite3
import httpx
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from app.config import github_token
from app.database.mongo_connection import StorageUnavailable
from app.github_service import client as github_client
from app.github_service import pull_requests as pr_service
from app.github_service.client import _error_message, async_client, headers, request_json
from app.github_service.pull_requests import _request_json, fetch_pull_request, get_file_content
from app.main import app
from app.schemas.review import AnalyzeRequest


@pytest.fixture(autouse=True)
def clear_github_cache():
    github_client._response_cache.clear()
    github_client._inflight_requests.clear()
    yield
    github_client._response_cache.clear()
    github_client._inflight_requests.clear()


def make_mock_client_factory(handler, token=None):
    client = httpx.AsyncClient(
        base_url=github_client.API_ROOT,
        headers=headers(token),
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.anyio
async def test_fetch_pull_request_with_supplied_token(monkeypatch):
    captured_requests = []

    def handler(request: httpx.Request):
        captured_requests.append(request)
        url = str(request.url)
        if "/files" in url:
            return httpx.Response(200, json=[{
                "filename": "app/core.py",
                "status": "modified",
                "additions": 3,
                "deletions": 1,
                "patch": "@@ -1,2 +1,4 @@\n+line1\n+line2",
            }])
        return httpx.Response(200, json={
            "title": "Add feature",
            "body": "Detailed description",
            "state": "open",
            "user": {"login": "contributor"},
            "base": {"ref": "main"},
            "head": {"ref": "feature", "sha": "1234567890123456789012345678901234567890"},
            "changed_files": 1,
            "additions": 3,
            "deletions": 1,
        })

    def mock_async_client(token=None):
        return make_mock_client_factory(handler, token=token)

    monkeypatch.setattr(pr_service, "async_client", mock_async_client)

    bundle = await fetch_pull_request(
        "https://github.com/acme/repo/pull/42",
        github_token="gho_supplied_test_token_123",
    )

    assert bundle.metadata.title == "Add feature"
    assert bundle.metadata.pull_request_number == 42
    assert len(bundle.files) == 1
    assert bundle.files[0].filename == "app/core.py"
    assert len(captured_requests) >= 2
    for req in captured_requests:
        assert req.headers.get("authorization") == "Bearer gho_supplied_test_token_123"


@pytest.mark.anyio
async def test_fetch_pull_request_without_supplied_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    captured_requests = []

    def handler(request: httpx.Request):
        captured_requests.append(request)
        url = str(request.url)
        if "/files" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={
            "title": "Public PR",
            "body": None,
            "state": "open",
            "user": {"login": "user"},
            "base": {"ref": "main"},
            "head": {"ref": "dev", "sha": "a" * 40},
            "changed_files": 0,
            "additions": 0,
            "deletions": 0,
        })

    def mock_async_client(token=None):
        return make_mock_client_factory(handler, token=token)

    monkeypatch.setattr(pr_service, "async_client", mock_async_client)

    bundle = await fetch_pull_request(
        "https://github.com/acme/repo/pull/7",
        github_token=None,
    )

    assert bundle.metadata.title == "Public PR"
    for req in captured_requests:
        assert "authorization" not in req.headers


def test_authorization_header_precedence_and_error_redaction(monkeypatch):
    # 1. No token, no env -> no header
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert "Authorization" not in headers(None)

    # 2. Env token -> header with env token
    monkeypatch.setenv("GITHUB_TOKEN", "env-server-token")
    assert headers(None)["Authorization"] == "Bearer env-server-token"

    # 3. Explicit token overrides env token
    assert headers("caller-user-token")["Authorization"] == "Bearer caller-user-token"

    # 4. Error message redacts user token and env token
    req = httpx.Request(
        "GET",
        "https://api.github.com/test",
        headers={"Authorization": "Bearer caller-user-token"},
    )
    resp = httpx.Response(
        403,
        request=req,
        json={"message": "Forbidden for token caller-user-token and env-server-token"},
    )
    sanitized = _error_message(resp)
    assert "caller-user-token" not in sanitized
    assert "env-server-token" not in sanitized
    assert "[redacted]" in sanitized


@pytest.mark.anyio
async def test_get_file_content_with_supplied_token(monkeypatch):
    captured_requests = []
    sample_content = b"def test_app():\n    return 42\n"

    def handler(request: httpx.Request):
        captured_requests.append(request)
        return httpx.Response(200, json={
            "encoding": "base64",
            "content": base64.b64encode(sample_content).decode("utf-8"),
            "size": len(sample_content),
        })

    def mock_async_client(token=None):
        return make_mock_client_factory(handler, token=token)

    monkeypatch.setattr(pr_service, "async_client", mock_async_client)

    content = await get_file_content(
        "acme",
        "repo",
        "tests/test_app.py",
        "a" * 40,
        github_token="gho_content_token_999",
    )

    assert content == sample_content.decode("utf-8")
    assert len(captured_requests) == 1
    assert captured_requests[0].headers.get("authorization") == "Bearer gho_content_token_999"


@pytest.mark.anyio
async def test_request_json_never_receives_unsupported_github_token_kwarg(monkeypatch):
    original_request_json = github_client.request_json
    calls = []

    async def spy_request_json(client, path, params=None, *, use_cache=True, **kwargs):
        calls.append((path, kwargs))
        assert "github_token" not in kwargs, "request_json must never receive 'github_token' keyword argument"
        return await original_request_json(client, path, params=params, use_cache=use_cache)

    monkeypatch.setattr(github_client, "request_json", spy_request_json)
    monkeypatch.setattr(pr_service, "request_json", spy_request_json)

    def handler(request: httpx.Request):
        url = str(request.url)
        if "/contents" in url:
            return httpx.Response(200, json={
                "encoding": "base64",
                "content": base64.b64encode(b"x = 1").decode("utf-8"),
            })
        if "/files" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={
            "title": "PR",
            "body": None,
            "state": "open",
            "user": {"login": "dev"},
            "base": {"ref": "main"},
            "head": {"ref": "dev", "sha": "a" * 40},
            "changed_files": 0,
            "additions": 0,
            "deletions": 0,
        })

    def mock_async_client(token=None):
        return make_mock_client_factory(handler, token=token)

    monkeypatch.setattr(pr_service, "async_client", mock_async_client)

    # 1. Test fetch_pull_request
    await fetch_pull_request("https://github.com/acme/repo/pull/1", github_token="token-xyz")
    # 2. Test get_file_content
    await get_file_content("acme", "repo", "main.py", "a" * 40, github_token="token-xyz")
    # 3. Test _request_json directly
    async with mock_async_client() as c:
        await _request_json(c, "/repos/acme/repo", github_token="token-xyz")

    assert len(calls) >= 3


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "auth_test.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    from app.routes import review as review_routes

    async def public_repository(_owner, _repo):
        return None

    monkeypatch.setattr(review_routes, "ensure_public_repository", public_repository)
    with TestClient(app) as instance:
        response = instance.post(
            "/api/auth/register",
            json={"email": "reviewer_test@example.com", "password": "correct horse battery staple"},
        )
        assert response.status_code == 201
        yield instance


def test_live_review_endpoint_with_x_github_token_header(monkeypatch, auth_client):
    from app.routes import review as review_route

    captured_tokens = []

    async def mock_analyze(request, user_id=None, github_token=None, *, owner_id=None):
        captured_tokens.append(github_token)
        from app.data.sample_reviews import sample_review
        resp = sample_review("acme", "repo", 42)
        resp.analysis_mode = "live"
        resp.is_demo = False
        return resp

    monkeypatch.setattr(review_route, "analyze", mock_analyze)

    headers = {
        "X-GitHub-Token": "gho_live_e2e_token_456",
    }
    response = auth_client.post(
        "/api/reviews/analyze",
        json={"url": "https://github.com/acme/repo/pull/42", "mode": "live"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert captured_tokens == ["gho_live_e2e_token_456"]
    body = response.json()
    assert body["analysis_mode"] == "live"


def test_unexpected_programming_exceptions_return_500_not_503(monkeypatch, auth_client):
    from app.routes import review as review_route

    async def broken_analyze(*args, **kwargs):
        raise TypeError("Unexpected code defect: NoneType object has no attribute 'items'")

    monkeypatch.setattr(review_route, "analyze", broken_analyze)

    response = auth_client.post(
        "/api/reviews/analyze",
        json={"url": "https://github.com/acme/repo/pull/42", "mode": "live"},
    )

    # Must be 500 internal error, NOT 503 persistence failure!
    assert response.status_code == 500
    assert response.json()["detail"] == "Internal review processing failure."


@pytest.mark.parametrize(
    "storage_exc",
    [
        StorageUnavailable("Database connection timed out."),
        PyMongoError("Cluster connection error."),
        sqlite3.OperationalError("database is locked"),
    ],
)
def test_genuine_storage_failures_remain_503(monkeypatch, auth_client, storage_exc):
    from app.routes import review as review_route

    async def storage_broken_analyze(*args, **kwargs):
        raise storage_exc

    monkeypatch.setattr(review_route, "analyze", storage_broken_analyze)

    response = auth_client.post(
        "/api/reviews/analyze",
        json={"url": "https://github.com/acme/repo/pull/42", "mode": "live"},
    )

    # Genuine storage failures MUST return 503
    assert response.status_code == 503
    assert response.json()["detail"] == "Review could not be completed or persisted."
