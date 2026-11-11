from fastapi.testclient import TestClient

from app.main import app


def test_health_and_readiness_use_api_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "health.sqlite3"))
    client = TestClient(app)
    assert client.get("/api/health").json() == {"status": "ok", "service": "pr-mentor"}
    assert client.get("/api/health/ready").json() == {"status": "ready", "service": "pr-mentor"}


def test_cors_allows_frontend_development_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "cors.sqlite3"))
    response = TestClient(app).options("/api/health", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_keeps_custom_domain_when_origins_are_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "custom-domain-cors.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", "https://another-frontend.example")
    response = TestClient(app).options("/api/health", headers={
        "Origin": "https://prmentor.in",
        "Access-Control-Request-Method": "GET",
    })
    assert response.headers["access-control-allow-origin"] == "https://prmentor.in"
