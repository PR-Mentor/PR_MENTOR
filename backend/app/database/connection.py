"""SQLite connection boundary with schema creation and cleanup."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import database_path

_INITIALIZED_PATHS: set[str] = set()


def _init_db(db: sqlite3.Connection, path_str: str) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY,
            user_id TEXT,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            owner_id TEXT
        );
        CREATE TABLE IF NOT EXISTS saved_pull_requests (
            saved_id TEXT PRIMARY KEY,
            user_id TEXT,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            owner_id TEXT,
            UNIQUE(user_id, url)
        );
        CREATE TABLE IF NOT EXISTS learning_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id TEXT NOT NULL,
            category TEXT NOT NULL,
            correct INTEGER NOT NULL CHECK(correct IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            user_id TEXT,
            owner_id TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            firebase_uid TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_users (
            user_id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES auth_users(user_id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_rate_limits (
            bucket_key TEXT PRIMARY KEY,
            window_start INTEGER NOT NULL,
            hit_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id TEXT PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
            created_by TEXT NOT NULL REFERENCES auth_users(user_id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_members (
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES auth_users(user_id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('owner', 'admin', 'editor', 'viewer')),
            joined_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS workspace_member_user_idx ON workspace_members(user_id, workspace_id);
        CREATE TABLE IF NOT EXISTS workspace_invitations (
            token_hash TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'editor', 'viewer')),
            invited_by TEXT NOT NULL REFERENCES auth_users(user_id),
            expires_at TEXT NOT NULL,
            accepted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS workspace_invite_expiry_idx ON workspace_invitations(expires_at);
        CREATE TABLE IF NOT EXISTS workspace_review_shares (
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
            review_id TEXT NOT NULL,
            shared_by TEXT NOT NULL REFERENCES auth_users(user_id),
            shared_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, review_id)
        );
        CREATE INDEX IF NOT EXISTS workspace_share_review_idx ON workspace_review_shares(review_id);
        CREATE TABLE IF NOT EXISTS benchmark_datasets (
            dataset_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES auth_users(user_id) ON DELETE CASCADE,
            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
            cases_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS benchmark_dataset_owner_idx ON benchmark_datasets(owner_id, updated_at);
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            run_id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL REFERENCES benchmark_datasets(dataset_id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL REFERENCES auth_users(user_id) ON DELETE CASCADE,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS benchmark_run_owner_idx ON benchmark_runs(owner_id, created_at);
        CREATE TABLE IF NOT EXISTS review_jobs (
            job_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'partial', 'failed', 'cancelled')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 3),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            available_at REAL NOT NULL,
            lease_until REAL,
            lease_token TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
            review_id TEXT,
            error_code TEXT
        );
        CREATE INDEX IF NOT EXISTS review_jobs_claim_idx ON review_jobs(status, available_at, created_at);
        CREATE INDEX IF NOT EXISTS review_jobs_owner_idx ON review_jobs(owner_id, created_at);
        CREATE TABLE IF NOT EXISTS test_execution_jobs (
            execution_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'passed', 'failed', 'timed_out', 'cancelled', 'unavailable', 'rejected', 'infrastructure_error')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            lease_until REAL,
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
            result TEXT,
            error_code TEXT,
            UNIQUE(owner_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS test_execution_claim_idx ON test_execution_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS test_execution_owner_idx ON test_execution_jobs(owner_id, created_at);
        """
    )
    for table in ("reviews", "saved_pull_requests"):
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if "user_id" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
        if "owner_id" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN owner_id TEXT")
        db.execute(f"UPDATE {table} SET user_id = owner_id WHERE user_id IS NULL AND owner_id IS NOT NULL")
        db.execute(f"UPDATE {table} SET owner_id = user_id WHERE owner_id IS NULL AND user_id IS NOT NULL")

    learning_columns = {row["name"] for row in db.execute("PRAGMA table_info(learning_attempts)").fetchall()}
    if "owner_id" not in learning_columns:
        db.execute("ALTER TABLE learning_attempts ADD COLUMN owner_id TEXT")
    if "user_id" not in learning_columns:
        db.execute("ALTER TABLE learning_attempts ADD COLUMN user_id TEXT")

    execution_columns = {row["name"] for row in db.execute("PRAGMA table_info(test_execution_jobs)").fetchall()}
    if "payload_sha256" not in execution_columns:
        db.execute("ALTER TABLE test_execution_jobs ADD COLUMN payload_sha256 TEXT NOT NULL DEFAULT ''")

    review_job_columns = {row["name"] for row in db.execute("PRAGMA table_info(review_jobs)").fetchall()}
    if "lease_token" not in review_job_columns:
        db.execute("ALTER TABLE review_jobs ADD COLUMN lease_token TEXT")

    saved_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='saved_pull_requests'").fetchone()["sql"]
    if "url TEXT NOT NULL UNIQUE" in saved_sql:
        db.executescript(
            """
            ALTER TABLE saved_pull_requests RENAME TO saved_pull_requests_legacy;
            CREATE TABLE saved_pull_requests (
                saved_id TEXT PRIMARY KEY,
                user_id TEXT,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                owner_id TEXT,
                UNIQUE(user_id, url)
            );
            INSERT INTO saved_pull_requests(saved_id, user_id, url, created_at, owner_id)
                SELECT saved_id, COALESCE(user_id, owner_id), url, created_at, COALESCE(owner_id, user_id) FROM saved_pull_requests_legacy;
            DROP TABLE saved_pull_requests_legacy;
            """
        )
    db.execute("CREATE INDEX IF NOT EXISTS reviews_user_created ON reviews(user_id, created_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS saved_user_created ON saved_pull_requests(user_id, created_at DESC)")
    _INITIALIZED_PATHS.add(path_str)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path_str = str(path.resolve())
    db = sqlite3.connect(path, timeout=30.0)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 30000")
        if path_str not in _INITIALIZED_PATHS:
            _init_db(db, path_str)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ping() -> None:
    """Verify that the configured local SQLite database can be opened."""
    with connection() as db:
        db.execute("SELECT 1").fetchone()
