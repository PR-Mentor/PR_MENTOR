from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_otp_routes_fail_closed_without_delivery_or_storage():
    client = TestClient(app)
    sent = client.post("/api/auth/otp/send", json={"identity": "user@example.com", "channel": "email"})
    assert sent.status_code == 503
    assert "not configured" in sent.json()["detail"].lower()
    verified = client.post("/api/auth/otp/verify", json={"challenge_id": "any", "code": "123456"})
    assert verified.status_code == 503
    assert "not configured" in verified.json()["detail"].lower()


def test_firebase_profile_lifecycle_and_missing_token(tmp_path, monkeypatch):
    import app.routes.auth as auth_routes
    from app.database.users import get_user
    from app.routes.auth import current_claims

    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    claims = {
        "uid": "firebase-user-1", "name": "Ada Lovelace", "email": "ada@example.com",
        "email_verified": True, "picture": "https://example.test/avatar.png",
        "firebase": {"sign_in_provider": "google.com"},
    }
    monkeypatch.setattr(auth_routes, "delete_firebase_identity", lambda _uid: None)
    app.dependency_overrides[current_claims] = lambda: claims
    client = TestClient(app)
    try:
        created = client.post("/api/auth/firebase/verify")
        assert created.status_code == 200
        user = created.json()["user"]
        assert user["firebase_uid"] == "firebase-user-1"
        assert user["email"] == "ada@example.com"
        assert user["providers"] == ["google.com"]
        assert client.get("/api/auth/me").json()["user"]["display_name"] == "Ada Lovelace"

        updated = client.patch("/api/users/me", json={"display_name": "Ada"})
        assert updated.status_code == 200
        assert updated.json()["user"]["display_name"] == "Ada"
        assert client.post("/api/auth/firebase/verify").json()["user"]["display_name"] == "Ada"

        claims["auth_time"] = int(datetime.now(UTC).timestamp())
        assert client.delete("/api/users/me").json() == {"deleted": True}
        assert get_user("firebase-user-1") is None
    finally:
        app.dependency_overrides.pop(current_claims, None)


def test_private_profile_rejects_missing_bearer_token():
    response = TestClient(app).get("/api/auth/me")
    assert response.status_code == 401
    assert "token" in response.json()["detail"].lower()


@pytest.mark.parametrize("token_error", ["invalid", "expired"])
def test_invalid_or_expired_firebase_token_is_rejected(monkeypatch, token_error):
    from firebase_admin import auth as firebase_auth

    import app.routes.auth as auth_routes

    monkeypatch.setattr(auth_routes, "_firebase_app", lambda: object())
    error = firebase_auth.InvalidIdTokenError("invalid token") if token_error == "invalid" else firebase_auth.ExpiredIdTokenError("expired token", None)

    def reject_token(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(firebase_auth, "verify_id_token", reject_token)
    response = TestClient(app).get("/api/auth/me", headers={"Authorization": "Bearer untrusted"})
    assert response.status_code == 401
    assert "invalid" in response.json()["detail"].lower()


def test_delete_requires_recent_authentication():
    from app.routes.auth import current_claims

    app.dependency_overrides[current_claims] = lambda: {"uid": "stale-user", "auth_time": 1}
    try:
        response = TestClient(app).delete("/api/users/me")
        assert response.status_code == 403
        assert "reauthenticate" in response.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(current_claims, None)


@pytest.mark.parametrize(
    ("provider", "claim_fields", "expected_field"),
    [
        ("github.com", {"name": "octocat", "email": "octo@example.com", "email_verified": True}, "email"),
        ("google.com", {"name": "Ada", "email": "ada@example.com", "email_verified": True, "picture": "https://example.test/ada.png"}, "email"),
        ("phone", {"phone_number": "+919876543210"}, "phone_number"),
    ],
)
def test_provider_claims_create_one_profile(tmp_path, monkeypatch, provider, claim_fields, expected_field):
    from app.routes.auth import current_claims

    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / f"{provider}.sqlite3"))
    claims = {"uid": f"uid-{provider}", "firebase": {"sign_in_provider": provider}, **claim_fields}
    app.dependency_overrides[current_claims] = lambda: claims
    try:
        client = TestClient(app)
        profile = client.post("/api/auth/firebase/verify").json()["user"]
        assert profile["firebase_uid"] == claims["uid"]
        assert profile[expected_field] == claim_fields[expected_field]
        assert provider in profile["providers"]
        assert client.post("/api/auth/firebase/verify").json()["user"]["firebase_uid"] == claims["uid"]
    finally:
        app.dependency_overrides.pop(current_claims, None)


def test_linked_provider_login_preserves_verified_profile_fields(tmp_path, monkeypatch):
    from app.routes.auth import current_claims

    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "linked.sqlite3"))
    client = TestClient(app)
    try:
        first = {"uid": "linked-uid", "email": "ada@example.com", "email_verified": True, "name": "Ada", "firebase": {"sign_in_provider": "google.com"}}
        app.dependency_overrides[current_claims] = lambda: first
        client.post("/api/auth/firebase/verify")
        second = {"uid": "linked-uid", "phone_number": "+919876543210", "firebase": {"sign_in_provider": "phone"}}
        app.dependency_overrides[current_claims] = lambda: second
        profile = client.post("/api/auth/firebase/verify").json()["user"]
        assert profile["email"] == "ada@example.com"
        assert profile["email_verified"] is True
        assert profile["phone_number"] == "+919876543210"
        assert profile["providers"] == ["google.com", "phone"]
    finally:
        app.dependency_overrides.pop(current_claims, None)
