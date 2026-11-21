"""Endgame integration and hardening tests verifying cross-module cohesion for Milestone 6."""

import asyncio
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data.sample_reviews import sample_review
from app.database.reviews import get_store
from app.main import app
from app.services.learning_spaces import LearningSpaceService, get_learning_service


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "integration.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as instance:
        yield instance


def _register_user(client: TestClient, email: str) -> dict:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert resp.status_code == 201
    return resp.json()


# ==============================================================================
# A. AI REVIEWER -> LEARNING LAB CROSS-MODULE INTEGRATION
# ==============================================================================

def test_ai_reviewer_automatically_enriches_learning_space_and_isolates_users(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "cross_module.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")

    service = LearningSpaceService()

    # User 1 analyzes an active PR with C buffer overflow
    review_u1 = sample_review("org", "security-repo", 10).model_dump(mode="json")
    review_u1["review_id"] = "rev-u1-1"
    review_u1["canonical_issues"] = [
        {
            "issue_id": "c-overflow-1",
            "file_path": "src/buffer.c",
            "category": "security",
            "normalized_description": "Unsafe gets() function allows remote buffer overflow",
        }
    ]
    space_u1 = service.resolve_or_create_space(review_u1, owner_id="user-alice")
    assert space_u1.review_count == 1
    concepts_u1 = service.list_concepts(space_u1.space_id, owner_id="user-alice")
    assert len(concepts_u1) >= 1
    assert concepts_u1[0].language == "c"

    # User 1 analyzes the SAME PR again (snapshot 2): should update count, NOT duplicate
    review_u1_snap2 = sample_review("org", "security-repo", 10).model_dump(mode="json")
    review_u1_snap2["review_id"] = "rev-u1-2"
    space_u1_updated = service.resolve_or_create_space(review_u1_snap2, owner_id="user-alice")
    assert space_u1_updated.space_id == space_u1.space_id
    assert space_u1_updated.review_count == 2
    assert space_u1_updated.latest_review_id == "rev-u1-2"

    # User 2 analyzes the SAME PR: should get an isolated space, NOT overwrite user 1
    review_u2 = sample_review("org", "security-repo", 10).model_dump(mode="json")
    review_u2["review_id"] = "rev-u2-1"
    space_u2 = service.resolve_or_create_space(review_u2, owner_id="user-bob")
    assert space_u2.space_id != space_u1.space_id
    assert space_u2.review_count == 1

    # User 1's space remains intact with 2 reviews
    re_read_u1 = service.get_space(space_u1.space_id, owner_id="user-alice")
    assert re_read_u1.review_count == 2


# ==============================================================================
# B. MULTI-LANGUAGE ENGINE -> LEARNING LAB & DEVELOPER PROFILE
# ==============================================================================

def test_multi_language_findings_reflect_in_profile_and_learning_exercises(test_client):
    user = _register_user(test_client, "polyglot@example.com")
    uid = user["user_id"]

    # Seed multi-language review (Python, C, C++, Java)
    review = sample_review("acme", "multi-lang-tool", 7).model_dump(mode="json")
    review["review_id"] = "rev-poly-1"
    review["is_demo"] = False
    review["analysis_mode"] = "live"
    review["canonical_issues"] = [
        {"issue_id": "py-1", "file_path": "scripts/run.py", "category": "security", "normalized_description": "Command injection"},
        {"issue_id": "c-1", "file_path": "core/net.c", "category": "security", "normalized_description": "Buffer overflow in gets"},
        {"issue_id": "cpp-1", "file_path": "engine/calc.cpp", "category": "bug", "normalized_description": "Object slicing"},
        {"issue_id": "java-1", "file_path": "srv/UserDao.java", "category": "security", "normalized_description": "SQL injection in query"},
    ]
    get_store().save_review(review, owner_id=uid)

    # 1. Check personal analytics has all 4 languages categorized
    analytics_resp = test_client.get("/api/analytics/me")
    assert analytics_resp.status_code == 200
    analytics = analytics_resp.json()
    langs = analytics["findings_by_language"]
    assert langs.get("python") == 1
    assert langs.get("c") == 1
    assert langs.get("cpp") == 1
    assert langs.get("java") == 1

    # 2. Check resolve learning space creates multi-language exercises
    space_resp = test_client.post("/api/learning/spaces/resolve", json={"review_id": "rev-poly-1"})
    assert space_resp.status_code == 200
    space = space_resp.json()
    assert {"python", "c", "cpp", "java"}.issubset(set(space["languages"]))

    exercises_resp = test_client.get(f"/api/learning/spaces/{space['space_id']}/exercises")
    assert exercises_resp.status_code == 200
    ex_langs = {e["language"] for e in exercises_resp.json()}
    assert {"python", "c", "cpp", "java"}.issubset(ex_langs)


# ==============================================================================
# C. MULTI-MODEL ORCHESTRATION -> ANALYTICS TELEMETRY
# ==============================================================================

def test_orchestration_telemetry_aggregates_into_analytics(test_client):
    user = _register_user(test_client, "orch_user@example.com")
    uid = user["user_id"]

    # Seed review containing AI execution metadata
    review = sample_review("org", "repo", 99).model_dump(mode="json")
    review["review_id"] = "rev-orch-1"
    review["is_demo"] = False
    review["analysis_mode"] = "live"
    review["ai_execution_metadata"] = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "prompt_tokens": 1200,
        "completion_tokens": 300,
        "total_tokens": 1500,
        "estimated_cost_usd": 0.00036,
        "fallback_used": True,
    }
    get_store().save_review(review, owner_id=uid)

    analytics_resp = test_client.get("/api/analytics/me")
    assert analytics_resp.status_code == 200
    orch_data = analytics_resp.json()["ai_orchestration"]
    assert orch_data["total_tokens"] == 1500
    assert orch_data["prompt_tokens"] == 1200
    assert orch_data["completion_tokens"] == 300
    assert orch_data["estimated_cost_usd"] > 0
    assert "gpt-4o-mini" in orch_data["models_used"]
    assert "openai" in orch_data["providers_used"]
    assert orch_data["fallback_count"] == 1


# ==============================================================================
# D. LEARNING LAB -> DEVELOPER PROFILE INTEGRATION
# ==============================================================================

def test_learning_lab_submissions_update_dashboard_and_profile_progress(test_client):
    user = _register_user(test_client, "learner_dev@example.com")
    uid = user["user_id"]

    review = sample_review("learn", "lab-repo", 1).model_dump(mode="json")
    review["review_id"] = "rev-learn-1"
    review["is_demo"] = False
    review["analysis_mode"] = "live"
    review["canonical_issues"] = [
        {
            "issue_id": "c-1",
            "file_path": "main.c",
            "category": "security",
            "normalized_description": "Unsafe gets buffer overflow",
        }
    ]
    get_store().save_review(review, owner_id=uid)

    space = test_client.post("/api/learning/spaces/resolve", json={"review_id": "rev-learn-1"}).json()
    exercises = test_client.get(f"/api/learning/spaces/{space['space_id']}/exercises").json()
    assert len(exercises) >= 1

    # Submit valid solution
    good_c_code = """void safe_copy(const char *src, char *dst, size_t sz) {
        snprintf(dst, sz, "%s", src);
    }"""
    sub_resp = test_client.post(
        f"/api/learning/coding-exercises/{exercises[0]['exercise_id']}/submit",
        json={"code": good_c_code},
    )
    assert sub_resp.status_code == 200
    assert sub_resp.json()["passed"] is True

    # Check dashboard reports completed exercise
    dash = test_client.get("/api/learning/dashboard").json()
    assert dash["total_spaces"] >= 1
    assert dash["completed_exercises"] >= 1


# ==============================================================================
# E. WORKSPACES AUTHORIZATION & USER ISOLATION
# ==============================================================================

def test_workspace_and_user_data_isolation(test_client):
    alice = _register_user(test_client, "alice@example.com")
    bob_client = TestClient(app)
    bob = _register_user(bob_client, "bob@example.com")

    # Alice creates a private review
    alice_review = sample_review("alice-org", "private-repo", 1).model_dump(mode="json")
    alice_review["review_id"] = "alice-rev-1"
    get_store().save_review(alice_review, owner_id=alice["user_id"])

    # Alice creates a workspace and does NOT invite Bob
    ws = test_client.post("/api/workspaces", json={"name": "Alice Private Team"}).json()
    ws_id = ws["workspace_id"]

    # Bob cannot access Alice's workspace reviews or insights
    bob_ws_insights = bob_client.get(f"/api/workspaces/{ws_id}/learning-insights")
    assert bob_ws_insights.status_code == 404

    # Bob cannot access Alice's review
    bob_review = bob_client.get("/api/reviews/alice-rev-1")
    assert bob_review.status_code in (403, 404)
