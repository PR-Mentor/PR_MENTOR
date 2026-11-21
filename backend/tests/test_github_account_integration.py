"""Exercise the app's GitHub workflow at the HTTP boundary using upstream fixtures."""

from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import mongomock
import pytest
from cryptography.fernet import Fernet
 codex/phone-otp-login
from fastapi import HTTPException

 main
from fastapi.testclient import TestClient

from app.database import github as store
from app.github_service import account, pull_requests
from app.github_service import client as github_client
from app.main import app
from app.routes import auth, github, review, workspaces
from app.services import review_orchestrator

ALICE = {"Authorization": "Bearer e30.eyJpc3MiOiJzdXBhYmFzZSJ9.alice"}
BOB = {"Authorization": "Bearer e30.eyJpc3MiOiJzdXBhYmFzZSJ9.bob"}
ALICE_TOKEN = "fixture-alice-provider-credential"
BOB_TOKEN = "fixture-bob-provider-credential"
PROOF = "a" * 43
DATE = "2025-08-16T10:00:00Z"


def repo(number: int, owner: str, name: str, visibility: str = "public"):
    return {
        "id": number,
        "name": name,
        "full_name": f"{owner}/{name}",
        "description": "Repository description",
        "owner": {
            "login": owner,
            "avatar_url": "https://avatars.githubusercontent.com/u/123",
        },
        "visibility": visibility,
        "private": visibility != "public",
        "language": "Python",
        "stargazers_count": 5,
        "forks_count": 2,
        "default_branch": "main",
        "updated_at": DATE,
        "open_issues_count": 3,
    }


def pull():
    return {
        "number": 7,
        "title": "Improve parser",
        "body": "Handle empty inputs",
        "created_at": DATE,
        "updated_at": DATE,
        "user": {"login": "contributor"},
        "head": {"ref": "parser", "sha": "a" * 40},
        "base": {"ref": "main"},
        "draft": False,
        "state": "open",
        "changed_files": 1,
        "additions": 1,
        "deletions": 1,
    }


@pytest.fixture
def integration(monkeypatch, tmp_path):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv(
        "PR_MENTOR_DATABASE_PATH", str(tmp_path / "github-integration.sqlite3")
    )
    monkeypatch.setenv("SUPABASE_URL", "https://fixture.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "fixture-public-key")
    monkeypatch.setenv("PR_MENTOR_GITHUB_RETURN_URL", "http://localhost:5173/")
    monkeypatch.setenv("PR_MENTOR_GITHUB_TOKEN_KEY", Fernet.generate_key().decode())
    account._repositories.clear()
    github_client._response_cache.clear()

    def claims(authorization):
        user = "bob" if authorization == BOB["Authorization"] else "alice"
        return {
            "uid": f"supabase:{user}",
            "email": f"{user}@example.com",
            "email_verified": True,
            "auth_provider": "supabase",
            "firebase": {"sign_in_provider": "github"},
        }

    exchanges = []

    async def exchange(code, verifier):
        exchanges.append((code, verifier))
        return {
            "provider_token": ALICE_TOKEN,
            "access_token": "supabase-session",
            "refresh_token": "supabase-refresh",
        }

    events = []
    revoked = set()
    secret = repo(1000, "private-org", "internal-api", "private")
    public = repo(2000, "os-org", "public-api")
    first_page = [repo(number, "alice", f"repo-{number}") for number in range(1, 101)]
    collaborator = repo(3000, "partner", "collaboration", "private")

    def upstream(request: httpx.Request):
        assert request.url.host == "api.github.com"
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        path = request.url.path
        events.append((token, path, dict(request.url.params)))
        if token in revoked:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path == "/user":
            return httpx.Response(
                200,
                json={
                    "id": 10,
                    "login": "alice",
                    "avatar_url": "https://avatars.githubusercontent.com/u/10",
                },
            )
        if path == "/user/emails":
            return httpx.Response(
                200,
                json=[
                    {"email": "wrong@example.com", "primary": True, "verified": False},
                    {"email": "alice@example.com", "primary": True, "verified": True},
                ],
            )
        if path == "/user/repos":
            if token != ALICE_TOKEN:
                return httpx.Response(200, json=[public])
            return httpx.Response(
                200,
                json=first_page
                if request.url.params["page"] == "1"
                else [secret, collaborator],
            )
        if path.startswith("/repos/private-org/internal-api") and token != ALICE_TOKEN:
            return httpx.Response(404)
        if path in {"/repos/private-org/internal-api", "/repos/os-org/public-api"}:
            return httpx.Response(200, json=secret if "private-org" in path else public)
        if path.endswith("/branches"):
            return httpx.Response(200, json=[{"name": "main", "protected": True}])
        if path.endswith("/pulls"):
            entry = pull()
            for key in ("changed_files", "additions", "deletions"):
                entry.pop(key)
            return httpx.Response(200, json=[entry])
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json=pull())
        if path.endswith("/pulls/7/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "README.md",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "changes": 2,
                        "patch": "@@ -1 +1 @@\n-old\n+new",
                    }
                ],
            )
        return httpx.Response(404)

    @asynccontextmanager
    async def outbound(token=None):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(upstream),
            headers=github_client.headers(token),
        ) as client:
            client._prm_personal = token is not None
            yield client

    monkeypatch.setattr(auth, "verify_supabase_token", claims)
    monkeypatch.setattr(github, "verify_supabase_token", claims)
    monkeypatch.setattr(github, "_exchange", exchange)
    for module in (account, github, review, pull_requests):
        monkeypatch.setattr(module, "async_client", outbound)

    with TestClient(app) as http:
        yield http, events, exchanges, revoked
    account._repositories.clear()
    github_client._response_cache.clear()


def begin(http, intent="login", headers=None):
    response = http.post(
        "/api/github/oauth/start",
        headers=headers,
        json={"challenge": github._challenge(PROOF), "intent": intent},
    )
    assert response.status_code == 200, response.text
    url = urlsplit(response.json()["authorization_url"])
    params = parse_qs(url.query)
    assert url.hostname == "fixture.supabase.co"
    assert params["provider"] == ["github"]
    assert set(params["scopes"][0].split()) == {"read:user", "user:email", "repo"}
    state = parse_qs(urlsplit(params["redirect_to"][0]).query)["prm_github_state"][0]
    return {
        "state": state,
        "proof": PROOF,
        "code": "fixture-code",
        "intent": intent,
    }, params["code_challenge"][0]


def test_mongo_oauth_state_and_connection_rotation(monkeypatch):
    database = mongomock.MongoClient(tz_aware=True).pr_mentor
    monkeypatch.setattr(store, "storage_backend", lambda: "mongodb")
    monkeypatch.setattr(store, "get_mongo_database", lambda: database)
    monkeypatch.setenv("PR_MENTOR_GITHUB_TOKEN_KEY", Fernet.generate_key().decode())
    user = "supabase:mongodb-audit"
    store.save_flow("state", "challenge", user, "verifier")
    assert store.consume_flow("state", "challenge", "wrong-user") is None
    assert store.consume_flow("state", "challenge", user) == "verifier"
    assert store.consume_flow("state", "challenge", user) is None
    account.save_connection(user, {"login": "alice"}, ALICE_TOKEN, None)
    old = store.get_connection(user)
    account.save_connection(user, {"login": "alice"}, BOB_TOKEN, None)
    assert account.disconnect(old) is False
    current = store.get_connection(user)
    assert account.access_token(current) == BOB_TOKEN
    assert account.disconnect(current) is True
    assert store.get_connection(user)["encrypted_token"] is None


def test_oauth_return_url_must_match_a_configured_frontend(integration, monkeypatch):
    http, _, _, _ = integration
    monkeypatch.setenv(
        "PR_MENTOR_GITHUB_RETURN_URL",
        "http://localhost:5173/,https://example.com/PR_MENTOR/",
    )
    payload = {
        "challenge": github._challenge(PROOF),
        "intent": "login",
        "return_url": "https://example.com/PR_MENTOR/",
    }
    endpoint = "/api/github/oauth/start"
    response = http.post(
        endpoint, headers={"Origin": "https://example.com"}, json=payload
    )
    assert response.status_code == 200
    params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    assert params["redirect_to"][0].startswith(
        "https://example.com/PR_MENTOR/?prm_github_state="
    )
    assert (
        http.post(
            endpoint, headers={"Origin": "http://localhost:5173"}, json=payload
        ).status_code
        == 400
    )
    assert (
        http.post(
            endpoint, json={**payload, "return_url": "https://example.com/unlisted/"}
        ).status_code
        == 400
    )


codex/phone-otp-login
def test_oauth_return_url_can_use_an_allowed_origin_when_exact_url_is_unset(monkeypatch):
    monkeypatch.delenv("PR_MENTOR_GITHUB_RETURN_URL", raising=False)
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", "https://example.com")

    assert github._return_url(
        "https://example.com/PR_MENTOR/", "https://example.com"
    ) == "https://example.com/PR_MENTOR/"

    with pytest.raises(HTTPException, match="return URL is not configured"):
        github._return_url("https://unlisted.example/", "https://unlisted.example")

main
def test_login_state_is_single_use_and_provider_token_stays_server_side(integration):
    http, _, exchanges, _ = integration
    payload, code_challenge = begin(http)
    assert (
        http.post(
            "/api/github/oauth/callback", json={**payload, "proof": "b" * 43}
        ).status_code
        == 400
    )
    assert not exchanges
    complete = http.post("/api/github/oauth/callback", json=payload)
    assert complete.status_code == 200, complete.text
    assert complete.json() == {
        "session": {
            "access_token": "supabase-session",
            "refresh_token": "supabase-refresh",
        }
    }
    assert github._challenge(exchanges[0][1]) == code_challenge
    assert http.post("/api/github/oauth/callback", json=payload).status_code == 400
    record = store.get_connection("supabase:alice")
    assert record["encrypted_token"] != ALICE_TOKEN
    assert account.access_token(record) == ALICE_TOKEN
    profile = http.get("/api/github/connection", headers=ALICE)
    assert profile.json()["profile"]["email"] == "alice@example.com"
    assert profile.json()["profile"]["id"] == 10
    assert profile.headers["cache-control"] == "private, no-store"
    assert ALICE_TOKEN not in profile.text
    assert http.get("/api/github/connection").status_code == 401


def test_connection_cannot_be_finished_by_another_account(integration, monkeypatch):
    http, _, exchanges, _ = integration
    payload, _ = begin(http, "connect", ALICE)
    assert (
        http.post("/api/github/oauth/callback", headers=BOB, json=payload).status_code
        == 400
    )
    assert not exchanges
    complete = http.post("/api/github/oauth/callback", headers=ALICE, json=payload)
    assert complete.json() == {"session": None}
    assert http.get("/api/github/connection", headers=BOB).json() == {
        "connected": False,
        "profile": None,
    }

    async def revoke(token):
        assert token == ALICE_TOKEN
        return True

    monkeypatch.setattr(account, "revoke", revoke)
    assert http.delete("/api/github/connection", headers=ALICE).json() == {
        "connected": False,
        "revoked": True,
    }
    assert store.get_connection("supabase:alice")["encrypted_token"] is None
    assert http.get("/api/github/repositories", headers=ALICE).status_code == 409


def test_repository_pagination_cache_and_revocation_are_per_user(integration):
    http, events, _, revoked = integration
    account.save_connection("supabase:alice", {"login": "alice"}, ALICE_TOKEN, None)
    account.save_connection("supabase:bob", {"login": "bob"}, BOB_TOKEN, None)
    path = "/api/github/repositories"
    alice_data = http.get(path, headers=ALICE).json()
    assert len(alice_data) == 102
    private = {
        item["full_name"] for item in alice_data if item["visibility"] == "private"
    }
    assert private == {"private-org/internal-api", "partner/collaboration"}
    requests = [entry for entry in events if entry[0] == ALICE_TOKEN]
    assert [entry[2]["page"] for entry in requests] == ["1", "2"]
    assert requests[0][2]["per_page"] == "100"
    assert requests[0][2]["visibility"] == "all"
    assert requests[0][2]["affiliation"] == "owner,collaborator,organization_member"
    assert http.get(path, headers=ALICE).json() == alice_data
    assert len(events) == 2
    assert [item["full_name"] for item in http.get(path, headers=BOB).json()] == [
        "os-org/public-api"
    ]

    revoked.add(ALICE_TOKEN)
    response = http.post(f"{path}/refresh", headers=ALICE)
    assert response.status_code == 409 and "revoked" in response.json()["detail"]
    assert (
        http.get("/api/github/connection", headers=ALICE).json()["connected"] is False
    )
    assert http.get(path, headers=ALICE).status_code == 409
    assert http.get(path, headers=BOB).status_code == 200


@pytest.mark.parametrize(
    "owner, name, private",
    [("private-org", "internal-api", True), ("os-org", "public-api", False)],
)
def test_repository_workspace_runs_existing_ai_review(
    integration, monkeypatch, owner, name, private
):
    http, events, _, _ = integration
    account.save_connection("supabase:alice", {"login": "alice"}, ALICE_TOKEN, None)
    account.save_connection("supabase:bob", {"login": "bob"}, BOB_TOKEN, None)
    path = f"/api/github/repositories/{owner}/{name}"
    assert http.get(path, headers=ALICE).json()["default_branch"] == "main"
    assert http.get(f"{path}/branches", headers=ALICE).json() == {
        "items": [{"name": "main", "protected": True}],
        "has_next": False,
    }
    listing = http.get(f"{path}/pulls", headers=ALICE).json()["items"][0]
    assert listing["author"] == "contributor" and listing["changed_files"] is None
    detail = http.get(f"{path}/pulls/7", headers=ALICE).json()
    assert detail["changed_files"] == detail["additions"] == detail["deletions"] == 1

    calls = []

    async def ai(metadata, diff, static):
        calls.append((metadata, diff, static))
        return []

    async def context(_owner, _repo, _head, source):
        return review_orchestrator.summarize_python_sources(source)

    monkeypatch.setattr(review_orchestrator, "generate_candidates", ai)
    monkeypatch.setattr(review_orchestrator, "is_configured", lambda: True)
    monkeypatch.setattr(review_orchestrator, "build_review_repository_context", context)
    request = {
        "url": detail["html_url"],
        "mode": "live",
        "use_github_connection": True,
        "include_learning": False,
    }
    response = http.post("/api/reviews/analyze", headers=ALICE, json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["repository_private"] is private
    assert len(calls) == 1 and calls[0][1]
    assert any(
        tool["tool"] == "ai_review" and tool["status"] == "completed"
        for tool in result["tool_results"]
    )
    assert (
        http.get(f"/api/reviews/{result['review_id']}", headers=BOB).status_code == 404
    )
    if private:
        for segment in ("", "/branches", "/pulls", "/pulls/7"):
            assert http.get(f"{path}{segment}", headers=BOB).status_code == 404
        assert (
            http.post("/api/reviews/analyze", headers=BOB, json=request).status_code
            == 404
        )
        assert all(
            token in {ALICE_TOKEN, BOB_TOKEN}
            for token, route, _ in events
            if route.startswith("/repos/private-org/")
        )
        monkeypatch.setattr(workspaces, "_require_workspace_role", lambda *args: None)
        assert (
            http.post(
                f"/api/workspaces/example/reviews/{result['review_id']}", headers=ALICE
            ).status_code
            == 403
        )
