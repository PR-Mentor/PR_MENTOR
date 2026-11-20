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


def test_cors_respects_custom_configured_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "custom-domain-cors.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", "https://another-frontend.example")
    client = TestClient(app)
    # Configured custom origin is allowed
    response = client.options("/api/health", headers={
        "Origin": "https://another-frontend.example",
        "Access-Control-Request-Method": "GET",
    })
    assert response.headers["access-control-allow-origin"] == "https://another-frontend.example"

    # Unconfigured production origin is NOT permitted when custom origins are configured
    unconfigured_response = client.options("/api/health", headers={
        "Origin": "https://prmentor.in",
        "Access-Control-Request-Method": "GET",
    })
    assert "access-control-allow-origin" not in unconfigured_response.headers
