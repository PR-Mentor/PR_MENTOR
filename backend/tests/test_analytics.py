from fastapi.testclient import TestClient

from app.database.reviews import get_store
from app.main import app
from app.routes.analytics import (
    _learning_insights,
    learning_insights,
    personal_analytics,
)


def test_learning_insights_connect_findings_fix_decisions_and_validation_outcomes():
    review = {
        "canonical_issues": [{
            "category": "bug",
            "suggested_fixes": [{
                "decision_status": "accepted",
                "validation_reports": [{"status": "passed"}, {"status": "inconclusive"}],
            }],
        }, {"category": "bug", "suggested_fixes": []}],
    }
    insights = _learning_insights([review], scope="personal")
    assert insights["scope"] == "personal"
    assert insights["suggested_topics"] == ["bug"]
    assert insights["fix_decisions"] == {"accepted": 1}
    assert insights["validation_reports_by_status"] == {"inconclusive": 1, "passed": 1}


def test_personal_analytics_and_learning_insights_use_owned_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "analytics.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as client:
        assert client.get("/api/analytics/me").status_code == 401
        assert (
            client.post(
                "/api/auth/register",
                json={
                    "email": "insights@example.com",
                    "password": "correct horse battery staple",
                },
            ).status_code
            == 201
        )
        for number in (1, 2):
            response = client.post(
                "/api/reviews/analyze",
                json={
                    "url": f"https://github.com/example/repo/pull/{number}",
                    "mode": "demo",
                },
            )
            assert response.status_code == 200
        account_id = client.get("/api/auth/me").json()["user_id"]
        get_store().save_review({
            "review_id": "live-analytics-record",
            "created_at": "2026-11-08T00:00:00Z",
            "is_demo": False,
            "pull_request": {
                "repository_owner": "example", "repository_name": "repo",
                "pull_request_number": 3,
            },
            "findings": [{"id": "live-finding", "category": "security", "severity": "high"}],
            "canonical_issues": [{"issue_id": "live-issue", "category": "security", "severity": "high", "suggested_fixes": []}],
        }, owner_id=account_id)
        analytics = client.get("/api/analytics/me").json()
        insights = client.get("/api/learning/insights").json()
        assert analytics["total_reviews"] == 1
        assert analytics["live_pull_request_count"] == 1
        assert analytics["live_finding_count"] == 1
        assert analytics["reviews_analyzed"] == 1
        assert analytics["findings_by_category"] == {"security": 1}
        assert analytics["model_cost"] is None
        assert insights["source_review_count"] == 1
        assert insights["scope"] == "personal"
        assert "not a measure of developer skill" in insights["basis"]


def test_personal_analytics_endpoints_exclude_demo_history_without_testclient(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "analytics-direct.sqlite3"))
    store = get_store()
    store.save_review({
        "review_id": "demo", "created_at": "2026-11-08T00:00:00Z", "is_demo": True,
        "canonical_issues": [{"issue_id": "demo-issue", "category": "security"}],
    }, owner_id="account")
    store.save_review({
        "review_id": "legacy-demo", "created_at": "2026-11-08T00:00:30Z", "analysis_mode": "demo",
        "canonical_issues": [{"issue_id": "legacy-demo-issue", "category": "security"}],
    }, owner_id="account")
    store.save_review({
        "review_id": "live", "created_at": "2026-11-08T00:01:00Z", "is_demo": False,
        "pull_request": {"repository_owner": "example", "repository_name": "repo", "pull_request_number": 4},
        "canonical_issues": [{"issue_id": "live-issue", "category": "bug", "suggested_fixes": []}],
    }, owner_id="account")

    analytics = personal_analytics({"user_id": "account"})
    insights = learning_insights({"user_id": "account"})

    assert analytics["total_reviews"] == 1
    assert analytics["live_pull_request_count"] == 1
    assert analytics["live_finding_count"] == 1
    assert analytics["findings_by_category"] == {"bug": 1}
    assert insights["source_review_count"] == 1
    assert insights["suggested_topics"] == []


def test_workspace_learning_insights_are_limited_to_shared_reviews_and_members(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "workspace-insights.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as client:
        client.post("/api/auth/register", json={"email": "owner-insights@example.com", "password": "correct horse battery staple"})
        response = client.post("/api/workspaces", json={"name": "Team evidence"})
        assert response.status_code == 201
        workspace_id = response.json()["workspace_id"]
        review = client.post("/api/reviews/analyze", json={"url": "https://github.com/example/repo/pull/91", "mode": "demo"}).json()
        assert client.post(f"/api/workspaces/{workspace_id}/reviews/{review['review_id']}").status_code == 201
        insights = client.get(f"/api/workspaces/{workspace_id}/learning-insights")
        assert insights.status_code == 200
        assert insights.json()["scope"] == "workspace"
        assert insights.json()["source_review_count"] == 1
        assert insights.json()["suggested_fix_count"] >= 0

        client.post("/api/auth/logout")
        client.post("/api/auth/register", json={"email": "outsider-insights@example.com", "password": "correct horse battery staple"})
        assert client.get(f"/api/workspaces/{workspace_id}/learning-insights").status_code == 404
