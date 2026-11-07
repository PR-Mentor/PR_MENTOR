import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes.auth import current_claims
from app.schemas.github import ChangedFile, PullRequestBundle, PullRequestMetadata


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "api.sqlite3"))
    previous = app.dependency_overrides.get(current_claims)
    app.dependency_overrides[current_claims] = lambda: {"uid": "review-test-user"}
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous is None:
            app.dependency_overrides.pop(current_claims, None)
        else:
            app.dependency_overrides[current_claims] = previous


def test_demo_review_history_detail_and_diff_are_persisted(client):
    submitted_url = "https://github.com/example/project/pull/7"
    response = client.post("/api/reviews/analyze", json={"url": submitted_url, "mode": "demo", "include_learning": True})
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_mode"] == "demo" and body["is_demo"] is True
    assert "not describe" in body["warnings"][0].lower()
    assert {item["source"] for item in body["findings"]} == {"demo"}
    assert body["pull_request"]["pull_request_number"] == 7
    assert client.get("/api/reviews/history").json()[0]["review_id"] == body["review_id"]
    assert client.get(f"/api/reviews/{body['review_id']}").json()["review_id"] == body["review_id"]
    assert client.get(f"/api/reviews/{body['review_id']}/diff").json() == {"review_id": body["review_id"], "files": [], "warnings": body["warnings"]}

    second = client.post("/api/reviews/analyze", json={"url": submitted_url, "mode": "demo", "include_learning": False}).json()
    assert second["review_id"] != body["review_id"]
    assert all(not finding["learning_explanation"] for finding in second["findings"])
    assert len(client.get("/api/reviews/history").json()) == 2


def test_review_request_validation_and_unknown_records(client):
    assert client.post("/api/reviews/analyze", json={"mode": "demo"}).status_code == 422
    assert client.post("/api/reviews/analyze", json={"url": "https://example.com/a/b/pull/1"}).status_code == 400
    assert client.get("/api/reviews/not-found").status_code == 404
    assert client.get("/api/reviews/not-found/diff").status_code == 404
    assert client.get("/api/reviews/history?limit=0").status_code == 422


def test_saved_pr_create_list_duplicate_and_delete(client):
    url = "https://github.com/owner/repo/pull/12"
    created = client.post("/api/reviews/saved", json={"url": url})
    assert created.status_code == 201
    item = created.json()
    assert item == {"saved_id": item["saved_id"], "url": url, "temporary": False}
    assert client.post("/api/reviews/saved", json={"url": url}).json()["saved_id"] == item["saved_id"]
    assert client.get("/api/reviews/saved").json() == [item]
    assert client.delete(f"/api/reviews/saved/{item['saved_id']}").json() == {"deleted": True}
    assert client.delete(f"/api/reviews/saved/{item['saved_id']}").status_code == 404


def test_live_review_uses_ingestion_and_reports_unavailable_stages(client, monkeypatch):
    from app.services import review_orchestrator as service

    metadata = PullRequestMetadata(repository_owner="owner", repository_name="repo", repository_url="https://github.com/owner/repo", pull_request_number=2, pull_request_url="https://github.com/owner/repo/pull/2", title="Fix", description=None, author="dev", base_branch="main", head_branch="fix", head_sha="abc123", state="open", changed_files=1, additions=1, deletions=0)
    bundle = PullRequestBundle(metadata=metadata, files=[ChangedFile(filename="src/a.py", status="modified", additions=1, deletions=0, changes=1, patch="@@ -1 +1 @@\n-old\n+new", size=12)])

    async def fetch(_url):
        return bundle

    async def content(*_args):
        return "value = 1\n"

    def ruff(paths, *, root):
        return [{"file_path": str(root / paths[0]), "line": 1, "rule_id": "F401", "message": "unused import", "evidence": "import os"}]

    def bandit(_paths, *, root):
        return []

    monkeypatch.setattr(service, "fetch_pull_request", fetch)
    monkeypatch.setattr(service, "get_file_content", content)
    monkeypatch.setattr(service, "run_ruff", ruff)
    monkeypatch.setattr(service, "run_bandit", bandit)
    monkeypatch.setattr(service, "is_configured", lambda: False)
    response = client.post("/api/reviews/analyze", json={"url": metadata.pull_request_url, "mode": "live"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_demo"] is False and body["analysis_mode"] == "live"
    assert body["status"] == "partial"  # AI and isolated test execution are not configured.
    assert body["findings"][0]["file_path"] == "src/a.py"  # Never expose the temporary workspace path.
    assert body["findings"][0]["verification_status"] == "needs_review"
    assert any(tool["tool"] == "tests" and tool["executed"] is False for tool in body["tool_results"])
