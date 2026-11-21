"""Regression tests for environment-aware CORS isolation."""

import pytest
from fastapi.testclient import TestClient

from app.config import cors_origins, is_staging
from app.main import app

VERCEL_STAGING_PREVIEW = (
    "https://pr-mentor-git-feature-pr-me-92917c-ritik-rajs-projects-75d0ad67.vercel.app"
)
PROD_DOMAIN = "https://prmentor.in"
PROD_WWW_DOMAIN = "https://www.prmentor.in"


def test_production_default_origins_when_unconfigured(monkeypatch):
    """When PR_MENTOR_CORS_ORIGINS is unset in production, default production origins are preserved."""
    monkeypatch.delenv("PR_MENTOR_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("PR_MENTOR_ENV", raising=False)
    monkeypatch.setenv("MONGODB_DATABASE", "pr_mentor")

    origins = cors_origins()
    assert is_staging() is False
    assert PROD_DOMAIN in origins
    assert PROD_WWW_DOMAIN in origins
    assert "http://localhost:5173" in origins
    assert "https://pr-mentor.github.io" in origins


def test_explicit_production_origins(monkeypatch):
    """When PR_MENTOR_CORS_ORIGINS is explicitly configured with production domains, they are parsed and respected."""
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", f"{PROD_DOMAIN}, {PROD_WWW_DOMAIN}")
    monkeypatch.setenv("MONGODB_DATABASE", "pr_mentor")

    origins = cors_origins()
    assert origins == [PROD_DOMAIN, PROD_WWW_DOMAIN]


def test_staging_preview_origin_permitted(monkeypatch):
    """Staging permits the configured Vercel preview origin."""
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", VERCEL_STAGING_PREVIEW)
    monkeypatch.setenv("MONGODB_DATABASE", "pr_mentor_staging")

    origins = cors_origins()
    assert is_staging() is True
    assert origins == [VERCEL_STAGING_PREVIEW]


def test_production_domains_absent_from_staging_configuration(monkeypatch):
    """When staging configures custom origins, production domains are strictly absent."""
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", VERCEL_STAGING_PREVIEW)
    monkeypatch.setenv("MONGODB_DATABASE", "pr_mentor_staging")

    origins = cors_origins()
    assert PROD_DOMAIN not in origins
    assert PROD_WWW_DOMAIN not in origins
    assert "https://pr-mentor.github.io" not in origins


def test_multiple_comma_separated_origins(monkeypatch):
    """Multiple comma-separated origins are parsed into distinct entries."""
    origins_input = f"{VERCEL_STAGING_PREVIEW},http://localhost:5173,https://other.example.com"
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", origins_input)

    origins = cors_origins()
    assert origins == [
        VERCEL_STAGING_PREVIEW,
        "http://localhost:5173",
        "https://other.example.com",
    ]


def test_whitespace_and_empty_segments_handling(monkeypatch):
    """Whitespace surrounding origins is trimmed and empty comma segments are ignored."""
    origins_input = f"  {VERCEL_STAGING_PREVIEW}  ,   ,  http://localhost:5173   "
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", origins_input)

    origins = cors_origins()
    assert origins == [VERCEL_STAGING_PREVIEW, "http://localhost:5173"]


def test_missing_staging_configuration_does_not_inherit_production_domains(monkeypatch):
    """When staging has missing PR_MENTOR_CORS_ORIGINS, production domains are NOT inherited."""
    monkeypatch.delenv("PR_MENTOR_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("MONGODB_DATABASE", "pr_mentor_staging")

    origins = cors_origins()
    assert is_staging() is True
    assert PROD_DOMAIN not in origins
    assert PROD_WWW_DOMAIN not in origins
    assert "https://pr-mentor.github.io" not in origins
    # Should only default to safe local dev origins
    assert origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_empty_staging_configuration_behaviour(monkeypatch):
    """When PR_MENTOR_CORS_ORIGINS is explicitly empty or whitespace, no production domains are allowed."""
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", "   ")
    monkeypatch.setenv("MONGODB_DATABASE", "pr_mentor_staging")

    origins = cors_origins()
    assert origins == []
    assert PROD_DOMAIN not in origins
    assert PROD_WWW_DOMAIN not in origins


def test_cors_middleware_integration_staging_isolation(monkeypatch):
    """Test full HTTP preflight behavior through FastAPI TestClient."""
    monkeypatch.setenv("PR_MENTOR_CORS_ORIGINS", VERCEL_STAGING_PREVIEW)
    monkeypatch.setenv("MONGODB_DATABASE", "pr_mentor_staging")
    client = TestClient(app)

    # Vercel preview origin receives CORS allow header
    preview_res = client.options(
        "/api/health",
        headers={
            "Origin": VERCEL_STAGING_PREVIEW,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preview_res.headers.get("access-control-allow-origin") == VERCEL_STAGING_PREVIEW
    assert preview_res.headers.get("access-control-allow-credentials") == "true"

    # Production domain is rejected (no CORS allow header)
    prod_res = client.options(
        "/api/health",
        headers={
            "Origin": PROD_DOMAIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in prod_res.headers
