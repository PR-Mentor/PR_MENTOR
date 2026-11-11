"""Small environment-backed configuration for the API service."""

import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def database_path() -> Path:
    value = os.getenv("PR_MENTOR_DATABASE_PATH")
    return Path(value).expanduser().resolve() if value else BACKEND_ROOT / "data" / "pr_mentor.sqlite3"


def storage_backend() -> str:
    value = os.getenv("PR_MENTOR_STORAGE", "sqlite").strip().lower()
    if value not in {"sqlite", "mongodb"}:
        raise ValueError("PR_MENTOR_STORAGE must be either 'sqlite' or 'mongodb'.")
    return value


def mongodb_uri() -> str | None:
    return os.getenv("MONGODB_URI") or None


def mongodb_database() -> str:
    return os.getenv("MONGODB_DATABASE", "pr_mentor").strip() or "pr_mentor"


def cors_origins() -> list[str]:
    value = os.getenv("PR_MENTOR_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return [origin.strip() for origin in value.split(",") if origin.strip()]


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
