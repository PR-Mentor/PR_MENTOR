"""Small environment-backed configuration for the API service."""

import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LLM_PROVIDER_DEFAULTS = {
    "gemini": {
        "api_key_env": "GEMINI_API_KEY",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-3.8-flash",
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-flash",
    },
    "groq": {
        "api_key_env": "GROQ_API",
        "endpoint": "https://api.groq.com/openai/v1/chat/completions",
        "model": "openai/gpt-oss-20b",
    },
    "nvidia": {
        "api_key_env": "NVIDIA_API",
        "endpoint": "https://integrate.api.nvidia.com/v1/chat/completions",
        "model": "openai/gpt-oss-20b",
    },
}

DEFAULT_LLM_PROVIDER_ORDER = ("gemini", "deepseek", "groq", "nvidia")


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


def is_staging() -> bool:
    env = (os.getenv("PR_MENTOR_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    return env == "staging" or mongodb_database() == "pr_mentor_staging"


def cors_origins() -> list[str]:
    raw_value = os.getenv("PR_MENTOR_CORS_ORIGINS")
    if raw_value is not None:
        return [origin.strip() for origin in raw_value.split(",") if origin.strip()]

    if is_staging():
        defaults = "http://localhost:5173,http://127.0.0.1:5173"
    else:
        defaults = (
            "http://localhost:5173,http://127.0.0.1:5173,"
            "https://18rajritik.github.io,https://pr-mentor.github.io,"
            "https://prmentor.in,https://www.prmentor.in"
        )

    return [origin.strip() for origin in defaults.split(",") if origin.strip()]


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
    """Resolve the legacy generic LLM configuration.

    Named-provider configuration for Gemini, DeepSeek, Groq, and NVIDIA is
    handled by the provider registry. These generic variables remain
    supported for backward compatibility.
    """
    model_key = f"LLM_MODEL_{task.upper().replace('-', '_')}" if task else "LLM_MODEL"
    model = os.getenv(model_key) or os.getenv("LLM_MODEL")

    generic_key = os.getenv("LLM_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")
    use_groq_alias = bool(groq_key and not generic_key)

    if use_groq_alias and not model:
        model = LLM_PROVIDER_DEFAULTS["groq"]["model"]

    default_endpoint = (
        LLM_PROVIDER_DEFAULTS["groq"]["endpoint"]
        if use_groq_alias
        else "https://api.openai.com/v1/chat/completions"
    )

    return (
        generic_key or groq_key or None,
        model,
        os.getenv("LLM_API_BASE_URL") or default_endpoint,
    )
