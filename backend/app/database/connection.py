"""SQLite connection boundary with schema creation and cleanup."""

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

from app.config import database_path


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                user_id TEXT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saved_pull_requests (
                saved_id TEXT PRIMARY KEY,
                user_id TEXT,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, url)
            );
            CREATE TABLE IF NOT EXISTS learning_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                exercise_id TEXT NOT NULL,
                category TEXT NOT NULL,
                correct INTEGER NOT NULL CHECK(correct IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        for table in ("reviews", "saved_pull_requests"):
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
            if "user_id" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
        saved_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='saved_pull_requests'").fetchone()["sql"]
        if "url TEXT NOT NULL UNIQUE" in saved_sql:
            db.executescript("""
                ALTER TABLE saved_pull_requests RENAME TO saved_pull_requests_legacy;
                CREATE TABLE saved_pull_requests (
                    saved_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    url TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, url)
                );
                INSERT INTO saved_pull_requests(saved_id, user_id, url, created_at)
                    SELECT saved_id, user_id, url, created_at FROM saved_pull_requests_legacy;
                DROP TABLE saved_pull_requests_legacy;
            """)
        db.execute("CREATE INDEX IF NOT EXISTS reviews_user_created ON reviews(user_id, created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS saved_user_created ON saved_pull_requests(user_id, created_at DESC)")
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
