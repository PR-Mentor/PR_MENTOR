"""Commit-keyed repository intelligence index backed by the shared SQLite store.

Each entry is keyed by (owner, repository, commit_sha) and stores the full
JSON output of ``analyze_repository``.  The table is created lazily on first
access via the same ``connection()`` context manager used by the rest of the
application, so no migration script is required.

Concurrency
-----------
SQLite handles concurrent readers safely.  Writes are serialised by SQLite's
write lock; because analysis results are idempotent for a given commit SHA,
racing writers will produce identical JSON and the ``ON CONFLICT`` clause
makes the final outcome deterministic (last writer wins, which is fine here).

Cache failures
--------------
Every public function catches and swallows storage exceptions so that a cache
failure never breaks an in-progress review.  Callers receive ``None`` on a
cache miss or on any storage error.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from app.database.connection import connection

logger = logging.getLogger(__name__)

# Maximum age of a cached entry in seconds (default: 7 days).
DEFAULT_TTL_SECONDS: int = 7 * 24 * 60 * 60

# Hard upper bound for the JSON payload stored per entry (~10 MB).
_MAX_PAYLOAD_BYTES: int = 10 * 1024 * 1024

_DDL = """
CREATE TABLE IF NOT EXISTS repository_intelligence_index (
    owner          TEXT NOT NULL,
    repository     TEXT NOT NULL,
    commit_sha     TEXT NOT NULL,
    analysis_json  TEXT NOT NULL,
    created_at     REAL NOT NULL,
    accessed_at    REAL NOT NULL,
    PRIMARY KEY (owner, repository, commit_sha)
);
CREATE INDEX IF NOT EXISTS rii_accessed_idx
    ON repository_intelligence_index(accessed_at);
"""


def _ensure_table(db: sqlite3.Connection) -> None:
    """Create the cache table and index if they do not exist."""
    db.executescript(_DDL)


def get_cached(owner: str, repository: str, commit_sha: str) -> dict[str, Any] | None:
    """Return the cached intelligence result, or ``None`` on a miss or error.

    Also updates ``accessed_at`` so that recently-read entries are not evicted
    while still in active use.
    """
    try:
        with connection() as db:
            _ensure_table(db)
            row = db.execute(
                "SELECT analysis_json FROM repository_intelligence_index"
                " WHERE owner = ? AND repository = ? AND commit_sha = ?",
                (owner, repository, commit_sha),
            ).fetchone()
            if row is None:
                return None
            # Touch accessed_at so hot entries survive eviction windows.
            db.execute(
                "UPDATE repository_intelligence_index"
                " SET accessed_at = ?"
                " WHERE owner = ? AND repository = ? AND commit_sha = ?",
                (time.time(), owner, repository, commit_sha),
            )
            return json.loads(row["analysis_json"])
    except Exception:  # noqa: BLE001 — cache failure must not break callers.
        logger.exception(
            "repository_intelligence_index: get_cached failed for %s/%s@%s",
            owner,
            repository,
            commit_sha,
        )
        return None


def put_cached(
    owner: str,
    repository: str,
    commit_sha: str,
    data: dict[str, Any],
) -> None:
    """Persist an intelligence result.  Silently skips on any storage error.

    The payload must be a non-empty dict (guards against storing failed or
    partial analysis results).
    """
    if not isinstance(data, dict) or not data:
        logger.warning(
            "repository_intelligence_index: refusing to cache empty or non-dict"
            " result for %s/%s@%s",
            owner,
            repository,
            commit_sha,
        )
        return
    try:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            logger.warning(
                "repository_intelligence_index: payload exceeds %d bytes for"
                " %s/%s@%s — skipping cache write",
                _MAX_PAYLOAD_BYTES,
                owner,
                repository,
                commit_sha,
            )
            return
        now = time.time()
        with connection() as db:
            _ensure_table(db)
            db.execute(
                "INSERT INTO repository_intelligence_index"
                "  (owner, repository, commit_sha, analysis_json, created_at, accessed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(owner, repository, commit_sha) DO UPDATE"
                "  SET analysis_json = excluded.analysis_json,"
                "      accessed_at   = excluded.accessed_at",
                (owner, repository, commit_sha, payload, now, now),
            )
    except Exception:  # noqa: BLE001 — cache failure must not break callers.
        logger.exception(
            "repository_intelligence_index: put_cached failed for %s/%s@%s",
            owner,
            repository,
            commit_sha,
        )


def evict_old(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
    """Delete entries not accessed within ``ttl_seconds``.

    Returns the number of rows deleted, or 0 on error.
    """
    cutoff = time.time() - max(0, ttl_seconds)
    try:
        with connection() as db:
            _ensure_table(db)
            cursor = db.execute(
                "DELETE FROM repository_intelligence_index WHERE accessed_at <= ?",
                (cutoff,),
            )
            return cursor.rowcount
    except Exception:  # noqa: BLE001 — eviction failure is non-critical.
        logger.exception("repository_intelligence_index: evict_old failed")
        return 0
