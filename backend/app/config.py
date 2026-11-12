"""Small environment-backed configuration for the API service."""

import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def database_path() -> Path:
    value = os.getenv("PR_MENTOR_DATABASE_PATH")
    return Path(value).expanduser().resolve() if value else BACKEND_ROOT / "data" / "pr_mentor.sqlite3"


def storage_backend() -> str:
    # Render's default filesystem is ephemeral: SQLite data can disappear on
    # restart or redeploy. Use the managed MongoDB database there and fail
    # closed if it has not been configured, instead of silently accepting a
    # local database that cannot provide durable, cross-device history.
    if os.getenv("RENDER", "").strip().lower() == "true" or os.getenv("RENDER_SERVICE_ID"):
        if not mongodb_uri():
            raise ValueError("Render requires MONGODB_URI for durable review storage.")
        return "mongodb"

    configured = os.getenv("PR_MENTOR_STORAGE")
    # If a managed database URI is provided locally or on another host, prefer
    # it unless the operator explicitly selected a storage backend.
    value = (configured or ("mongodb" if mongodb_uri() else "sqlite")).strip().lower()
    if value not in {"sqlite", "mongodb"}:
        raise ValueError("PR_MENTOR_STORAGE must be either 'sqlite' or 'mongodb'.")
    return value


def mongodb_uri() -> str | None:
    return os.getenv("MONGODB_URI") or None


def mongodb_database() -> str:
    return os.getenv("MONGODB_DATABASE", "pr_mentor").strip() or "pr_mentor"


def cors_origins() -> list[str]:
    defaults = "http://localhost:5173,http://127.0.0.1:5173,https://18rajritik.github.io,https://pr-mentor.github.io,https://prmentor.in,https://www.prmentor.in"
    value = os.getenv("PR_MENTOR_CORS_ORIGINS", defaults)
    origins = [origin.strip() for origin in value.split(",") if origin.strip()]
    # Keep this app's production domains enabled when the host supplies a
    # custom PR_MENTOR_CORS_ORIGINS list.
    for origin in ("https://prmentor.in", "https://www.prmentor.in"):
        if origin not in origins:
            origins.append(origin)
    return origins


def http_timeout() -> float:
    return max(1.0, min(float(os.getenv("PR_MENTOR_HTTP_TIMEOUT", "15")), 60.0))


def github_token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or None


def cookie_secure() -> bool:
    value = os.getenv("PR_MENTOR_COOKIE_SECURE", "true").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("PR_MENTOR_COOKIE_SECURE must be 'true' or 'false'.")
    return value == "true"


def llm_configuration(task: str | None = None) -> tuple[str | None, str | None, str]:
    """Resolve task-specific model selection with the legacy model as fallback."""
    model_key = f"LLM_MODEL_{task.upper().replace('-', '_')}" if task else "LLM_MODEL"
    model = os.getenv(model_key) or os.getenv("LLM_MODEL") or None
    return os.getenv("LLM_API_KEY") or None, model, os.getenv("LLM_API_BASE_URL", "https://api.openai.com/v1/chat/completions")
