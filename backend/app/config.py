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
    defaults = "http://localhost:5173,http://127.0.0.1:5173,https://18rajritik.github.io,https://pr-mentor.github.io,https://prmentor.in,https://www.prmentor.in"
    value = os.getenv("PR_MENTOR_CORS_ORIGINS", defaults)
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def http_timeout() -> float:
    return max(1.0, min(float(os.getenv("PR_MENTOR_HTTP_TIMEOUT", "15")), 60.0))


def github_token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or None


def llm_configuration() -> tuple[str | None, str | None, str]:
    return os.getenv("LLM_API_KEY") or None, os.getenv("LLM_MODEL") or None, os.getenv("LLM_API_BASE_URL", "https://api.openai.com/v1/chat/completions")
