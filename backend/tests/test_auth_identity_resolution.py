"""Regression tests for authentication identity resolution and Learning Lab validation protection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from fastapi.testclient import TestClient

from app.database.auth import (
    create_user,
    get_identity,
    get_user_by_email,
    get_user_by_id,
    list_identities_for_user,
    resolve_or_create_identity,
    upsert_external_auth_user,
)
from app.database.connection import connection
from app.database.reviews import get_store
from app.database.workspaces import (
    accept_invitation,
    create_invitation,
    create_workspace,
    get_role,
    hash_invitation,
    user_email,
)
from app.main import app
from app.routes.auth import current_claims, current_user
from app.schemas.learning_lab import CodingExercise, CodingExercisePublic
from app.services.learning_spaces import get_learning_service


def test_local_user_followed_by_supabase_login_with_verified_email(tmp_path, monkeypatch):
    """Local user followed by Supabase login with same verified email maps to canonical user_id."""
    db_path = tmp_path / "auth_test.sqlite3"
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")

    # 1. Create local user
    email = "alice@example.com"
    local_uid = "local-alice-uuid-1"
    ok = create_user(local_uid, email, "hashed_password")
    assert ok is True

    # 2. Supabase login with same verified email
    supabase_claims = {
        "uid": "supabase:alice-sb-99",
        "email": "alice@example.com",
        "email_verified": True,
        "auth_provider": "supabase",
        "firebase": {"sign_in_provider": "github"},
    }
    resolved = resolve_or_create_identity(supabase_claims)

    # Must resolve to the existing canonical internal user_id
    assert resolved["user_id"] == local_uid
    assert resolved["email"] == email
    assert resolved["email_verified"] is True

    # Check identity mapping record
    ident = get_identity("supabase", "supabase:alice-sb-99")
    assert ident is not None
    assert ident["user_id"] == local_uid
    assert ident["email"] == email

    # Repeating login reuses the link
    again = resolve_or_create_identity(supabase_claims)
    assert again["user_id"] == local_uid


def test_local_user_followed_by_firebase_login_with_verified_email(tmp_path, monkeypatch):
    """Local user followed by Firebase login with same verified email maps to canonical user_id."""
    db_path = tmp_path / "auth_test.sqlite3"
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")

    email = "bob@example.com"
    local_uid = "local-bob-uuid-2"
    assert create_user(local_uid, email, "hashed_password_bob") is True

    firebase_claims = {
        "uid": "firebase_bob_12345",
        "email": "bob@example.com",
        "email_verified": True,
        "auth_provider": "firebase",
        "firebase": {"sign_in_provider": "google.com"},
    }
    resolved = resolve_or_create_identity(firebase_claims)
    assert resolved["user_id"] == local_uid
    assert resolved["email"] == email

    ident = get_identity("firebase", "firebase_bob_12345")
    assert ident is not None
    assert ident["user_id"] == local_uid


def test_same_email_with_unverified_external_identity_prevents_takeover(tmp_path, monkeypatch):
    """An unverified external email MUST NOT link to an existing account (account takeover defense)."""
    db_path = tmp_path / "auth_test.sqlite3"
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")

    # Legitimate user
    email = "victim@example.com"
    victim_uid = "local-victim-uuid"
    assert create_user(victim_uid, email, "victim_secret_pass") is True

    # Attacker signs up on external provider with victim's email, but has NOT verified it
    attacker_claims = {
        "uid": "supabase:attacker-malicious-id",
        "email": "victim@example.com",
        "email_verified": False,  # UNVERIFIED
        "auth_provider": "supabase",
        "firebase": {"sign_in_provider": "password"},
    }
    resolved = resolve_or_create_identity(attacker_claims)

    # Must NOT link to victim
    assert resolved["user_id"] != victim_uid
    assert resolved["user_id"] == "supabase:attacker-malicious-id"
    assert resolved["email_verified"] is False
    assert resolved["email"].startswith("unverified+")

    # Verify that victim's account is unaltered
    victim_user = get_user_by_id(victim_uid)
    assert victim_user["email"] == email

    # Attacker's identity record in auth_identities points to attacker, not victim
    ident = get_identity("supabase", "supabase:attacker-malicious-id")
    assert ident is not None
    assert ident["user_id"] == "supabase:attacker-malicious-id"
    assert ident["email_verified"] in (0, False)


def test_existing_workspace_invitation_acceptance_with_linked_account(tmp_path, monkeypatch):
    """A user who logs in via external verified auth can accept invitations sent to their canonical email."""
    db_path = tmp_path / "auth_test.sqlite3"
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")

    # 1. Workspace owner creates workspace
    owner_uid = "owner-uid-1"
    create_user(owner_uid, "owner@example.com", "pass")
    ws_dict = create_workspace(owner_uid, "Security Team")
    ws_id = ws_dict["workspace_id"]

    # 2. Owner invites carol@example.com
    carol_email = "carol@example.com"
    raw_token = "secure-invite-token-1234"
    token_hash = hash_invitation(raw_token)
    create_invitation(token_hash, ws_id, carol_email, "editor", owner_uid)

    # 3. Carol logs in via Supabase with verified email
    carol_claims = {
        "uid": "supabase:carol-sb-uuid",
        "email": carol_email,
        "email_verified": True,
        "auth_provider": "supabase",
        "firebase": {"sign_in_provider": "github"},
    }
    resolved = resolve_or_create_identity(carol_claims)
    carol_internal_uid = resolved["user_id"]

    # 4. Check user_email resolution
    resolved_email = user_email(carol_internal_uid)
    assert resolved_email == carol_email

    # 5. Carol accepts workspace invitation
    accepted_ws_id = accept_invitation(token_hash, carol_internal_uid, carol_email)
    assert accepted_ws_id == ws_id
    assert get_role(ws_id, carol_internal_uid) == "editor"


def test_data_ownership_preservation_across_linked_identities(tmp_path, monkeypatch):
    """Reviews and learning spaces created under internal identity are preserved when logging in via external verified auth."""
    db_path = tmp_path / "auth_test.sqlite3"
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")

    email = "dave@example.com"
    local_uid = "dave-local-id"
    create_user(local_uid, email, "pass")

    # Dave creates a review and learning space under dave-local-id
    store = get_store()
    review = {
        "review_id": "review-dave-1",
        "status": "completed",
        "analysis_mode": "live",
        "is_demo": False,
        "pull_request": {
            "repository_owner": "dave-org",
            "repository_name": "repo",
            "repository_url": "https://github.com/dave-org/repo",
            "pull_request_number": 7,
            "pull_request_url": "https://github.com/dave-org/repo/pull/7",
            "title": "Fix memory leak",
            "base_branch": "main",
            "head_branch": "fix",
        },
        "canonical_issues": [],
        "created_at": datetime.now(UTC).isoformat(),
        "user_id": local_uid,
        "owner_id": local_uid,
    }
    store.save_review(review, user_id=local_uid, owner_id=local_uid)

    learning_service = get_learning_service()
    space = learning_service.resolve_or_create_space(review, owner_id=local_uid)
    assert "dave-local" in space.space_id

    # Dave now signs in via Supabase with verified email dave@example.com
    supabase_claims = {
        "uid": "supabase:dave-new-device",
        "email": email,
        "email_verified": True,
        "auth_provider": "supabase",
    }
    resolved = resolve_or_create_identity(supabase_claims)
    assert resolved["user_id"] == local_uid

    # Querying reviews and spaces with the resolved user ID returns Dave's existing assets
    dave_reviews = store.list_reviews(user_id=resolved["user_id"])
    assert len(dave_reviews) == 1
    assert dave_reviews[0]["review_id"] == "review-dave-1"

    dave_spaces = learning_service.list_spaces(owner_id=resolved["user_id"])
    assert len(dave_spaces) == 1
    assert dave_spaces[0].space_id == space.space_id


def test_coding_exercise_public_schema_omits_validation_rules():
    """Public CodingExercisePublic schema must never leak validation_rules to API clients."""
    exercise = CodingExercise(
        exercise_id="ex-123",
        space_id="space-test-1",
        concept_id="concept-1",
        title="SQL Injection Defense",
        description="Prevent SQL injection in Java JDBC.",
        instructions="Use PreparedStatement parameterized queries.",
        language="java",
        difficulty="beginner",
        category="security",
        starter_code="String sql = \"SELECT * FROM users WHERE id = \" + id;",
        expected_behavior="Prepared statement with parameter binding.",
        hints=["Use connection.prepareStatement(...)"],
        explanation="Prepared statements separate code from data.",
        completion_criteria=["PreparedStatement is used."],
        validation_rules={
            "forbidden": ["+ id", "Statement stmt"],
            "required": ["prepareStatement", "?"],
        },
        points=15,
    )

    public_view = exercise.to_public()
    dump = public_view.model_dump()

    # Public dump must contain user-facing material
    assert dump["exercise_id"] == "ex-123"
    assert dump["title"] == "SQL Injection Defense"
    assert dump["hints"] == ["Use connection.prepareStatement(...)"]
    assert dump["instructions"] == "Use PreparedStatement parameterized queries."

    # Public dump MUST NOT contain sensitive validation rules
    assert "validation_rules" not in dump

    # Check serialization via JSON
    json_str = public_view.model_dump_json()
    assert "validation_rules" not in json_str
    assert "Statement stmt" not in json_str  # forbidden pattern not leaked


def test_api_exercise_endpoints_do_not_leak_validation_rules(tmp_path, monkeypatch):
    """Calling the FastAPI learning exercises endpoints returns public schemas without validation_rules."""
    db_path = tmp_path / "learning_leak_test.sqlite3"
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")

    user_id = "user-leak-test"
    create_user(user_id, "leak@example.com", "pass")

    store = get_store()
    review = {
        "review_id": "review-leak-test",
        "status": "completed",
        "analysis_mode": "live",
        "is_demo": False,
        "pull_request": {
            "repository_owner": "sec",
            "repository_name": "app",
            "repository_url": "https://github.com/sec/app",
            "pull_request_number": 42,
            "pull_request_url": "https://github.com/sec/app/pull/42",
            "title": "Fix security vulnerability",
            "base_branch": "main",
            "head_branch": "fix",
        },
        "canonical_issues": [{
            "issue_id": "issue-leak-1",
            "file_path": "src/App.java",
            "line_start": 10,
            "line_end": 15,
            "category": "security",
            "severity": "high",
            "normalized_description": "SQL Injection vulnerability in query construction",
            "status": "open",
            "observations": [],
            "explanation": {
                "why_it_matters": "Untrusted input concatenated into SQL queries allows attackers to bypass auth.",
            },
        }],
        "created_at": datetime.now(UTC).isoformat(),
        "user_id": user_id,
        "owner_id": user_id,
    }
    store.save_review(review, user_id=user_id, owner_id=user_id)

    service = get_learning_service()
    space = service.resolve_or_create_space(review, owner_id=user_id)
    exercises = service.list_exercises(space.space_id)
    assert len(exercises) >= 1
    ex_id = exercises[0].exercise_id

    # Make request to the API
    app.dependency_overrides[current_user] = lambda: {"user_id": user_id, "email": "leak@example.com"}
    try:
        with TestClient(app) as client:
            # 1. Test list exercises
            res_list = client.get(f"/api/learning/spaces/{space.space_id}/exercises")
            assert res_list.status_code == 200
            data_list = res_list.json()
            assert len(data_list) >= 1
            for item in data_list:
                assert "validation_rules" not in item
                assert "title" in item
                assert "starter_code" in item

            # 2. Test get single exercise
            res_single = client.get(f"/api/learning/coding-exercises/{ex_id}")
            assert res_single.status_code == 200
            data_single = res_single.json()
            assert "validation_rules" not in data_single
            assert data_single["exercise_id"] == ex_id
            assert "hints" in data_single
            assert "instructions" in data_single
    finally:
        app.dependency_overrides.pop(current_user, None)
