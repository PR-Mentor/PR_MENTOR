import pytest
from fastapi.testclient import TestClient

from app.config import cookie_secure
from app.database import auth as auth_store
from app.main import app


def test_account_lookup_for_profile_returns_only_public_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "profile-identity.sqlite3"))
    assert auth_store.create_user("profile-user", "profile@example.com", "private-password-hash")
    assert auth_store.get_user_by_id("profile-user") == {
        "user_id": "profile-user",
        "email": "profile@example.com",
    }
    assert auth_store.get_user_by_id("missing-user") is None


def test_registration_session_me_and_logout(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as client:
        assert client.get("/api/auth/me").status_code == 401
        registered = client.post("/api/auth/register", json={"email": "USER@example.com", "password": "correct horse battery staple"})
        assert registered.status_code == 201
        assert registered.json()["email"] == "user@example.com"
        cookie = registered.cookies.get("prm_session")
        assert cookie and len(cookie) > 32
        assert "httponly" in registered.headers["set-cookie"].lower()
        me = client.get("/api/auth/me").json()
        assert me == {"user_id": registered.json()["user_id"], "email": "user@example.com"}
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/auth/me").status_code == 401


def test_login_generic_failure_and_unique_email(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as client:
        account = {"email": "user@example.com", "password": "correct horse battery staple"}
        assert client.post("/api/auth/register", json=account).status_code == 201
        assert client.post("/api/auth/register", json=account).status_code == 409
        assert client.post("/api/auth/login", json={**account, "password": "incorrect password"}).status_code == 401
        assert client.post("/api/auth/login", json=account).status_code == 200


def test_review_data_requires_authentication():
    with TestClient(app) as client:
        assert client.get("/api/reviews/history").status_code == 401
        assert client.get("/api/reviews/saved").status_code == 401
        assert client.get("/api/learning/progress").status_code == 401


def test_session_cookie_is_secure_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "secure-cookie.sqlite3"))
    monkeypatch.delenv("PR_MENTOR_COOKIE_SECURE", raising=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/register",
            json={"email": "secure@example.com", "password": "correct horse battery staple"},
        )
    assert response.status_code == 201
    assert "; Secure" in response.headers["set-cookie"]


def test_cookie_secure_rejects_unrecognized_configuration(monkeypatch):
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "sometimes")
    with pytest.raises(ValueError, match="must be 'true' or 'false'"):
        cookie_secure()
