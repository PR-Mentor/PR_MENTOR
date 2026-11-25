"""Durable per-issue explanation job storage."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.database.connection import connection

MAX_ATTEMPTS = 3
LEASE_SECONDS = 120
MAX_BACKOFF_SECONDS = 60


def enqueue_many(
    owner_id: str,
    review_id: str,
    jobs: list[dict[str, Any]],
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Insert one idempotent explanation job per issue.

    Each item in ``jobs`` must contain ``issue_id`` and a JSON-serializable
    ``payload``. The payload should be small; the worker can load issue evidence
    and the saved diff from the review record.
    """
    owner_id = _required_text(owner_id, "owner_id")
    review_id = _required_text(review_id, "review_id")
    current = now if now is not None else time.time()
    prepared: list[tuple[str, dict[str, Any]]] = []

    for item in jobs:
        if not isinstance(item, dict):
            raise ValueError("Each explanation job must be an object.")
        issue_id = _required_text(item.get("issue_id"), "issue_id")
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("Explanation job payload must be an object.")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 32_000:
            raise ValueError("Explanation job payload exceeds 32 KB.")
        prepared.append((issue_id, payload))

    if _mongo():
        jobs_collection = _db()["issue_explanation_jobs"]
        results = []

        for issue_id, payload in prepared:
            job_id = uuid4().hex
            document = {
                "job_id": job_id,
                "owner_id": owner_id,
                "review_id": review_id,
                "issue_id": issue_id,
                "payload": payload,
                "status": "queued",
                "attempts": 0,
                "max_attempts": MAX_ATTEMPTS,
                "created_at": current,
                "updated_at": current,
                "available_at": current,
                "lease_until": None,
                "lease_token": None,
                "error_code": None,
            }
            try:
                jobs_collection.update_one(
                    {"review_id": review_id, "issue_id": issue_id},
                    {"$setOnInsert": document},
                    upsert=True,
                )
            except DuplicateKeyError:
                pass

            saved = jobs_collection.find_one(
                {"review_id": review_id, "issue_id": issue_id},
                {"_id": 0},
            )
            if saved is None or saved.get("owner_id") != owner_id:
                raise RuntimeError("Explanation job could not be enqueued.")
            results.append(saved)

        return results

    results = []
    with connection() as db:
        for issue_id, payload in prepared:
            job_id = uuid4().hex
            db.execute(
                """
                INSERT OR IGNORE INTO issue_explanation_jobs (
                    job_id, owner_id, review_id, issue_id, payload, status,
                    attempts, max_attempts, created_at, updated_at, available_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    owner_id,
                    review_id,
                    issue_id,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    MAX_ATTEMPTS,
                    current,
                    current,
                    current,
                ),
            )
            row = db.execute(
                """
                SELECT * FROM issue_explanation_jobs
                WHERE review_id = ? AND issue_id = ? AND owner_id = ?
                """,
                (review_id, issue_id, owner_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Explanation job could not be enqueued.")
            results.append(_sqlite_job(row))

    return results


def claim_next(
    *,
    now: float | None = None,
    lease_seconds: int = LEASE_SECONDS,
) -> dict[str, Any] | None:
    """Claim the oldest available job, recovering expired worker leases."""
    current = now if now is not None else time.time()
    if not 1 <= lease_seconds <= 600:
        raise ValueError("lease_seconds must be between 1 and 600.")

    if _mongo():
        jobs = _db()["issue_explanation_jobs"]
        jobs.update_many(
            {
                "status": "running",
                "lease_until": {"$lte": current},
                "$expr": {"$lt": ["$attempts", "$max_attempts"]},
            },
            {
                "$set": {
                    "status": "queued",
                    "available_at": current,
                    "updated_at": current,
                    "lease_until": None,
                    "lease_token": None,
                    "error_code": "worker_lease_expired",
                }
            },
        )
        jobs.update_many(
            {
                "status": "running",
                "lease_until": {"$lte": current},
                "$expr": {"$gte": ["$attempts", "$max_attempts"]},
            },
            {
                "$set": {
                    "status": "failed",
                    "updated_at": current,
                    "lease_until": None,
                    "lease_token": None,
                    "error_code": "worker_lease_expired",
                }
            },
        )
        claimed = jobs.find_one_and_update(
            {
                "status": "queued",
                "available_at": {"$lte": current},
                "$expr": {"$lt": ["$attempts", "$max_attempts"]},
            },
            {
                "$set": {
                    "status": "running",
                    "updated_at": current,
                    "lease_until": current + lease_seconds,
                    "lease_token": uuid4().hex,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        return claimed

    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            UPDATE issue_explanation_jobs
            SET status = 'queued', available_at = ?, updated_at = ?,
                lease_until = NULL, lease_token = NULL,
                error_code = 'worker_lease_expired'
            WHERE status = 'running' AND lease_until <= ?
              AND attempts < max_attempts
            """,
            (current, current, current),
        )
        db.execute(
            """
            UPDATE issue_explanation_jobs
            SET status = 'failed', updated_at = ?,
                lease_until = NULL, lease_token = NULL,
                error_code = 'worker_lease_expired'
            WHERE status = 'running' AND lease_until <= ?
              AND attempts >= max_attempts
            """,
            (current, current),
        )
        row = db.execute(
            """
            SELECT job_id FROM issue_explanation_jobs
            WHERE status = 'queued' AND available_at <= ?
              AND attempts < max_attempts
            ORDER BY created_at LIMIT 1
            """,
            (current,),
        ).fetchone()
        if row is None:
            return None

        token = uuid4().hex
        db.execute(
            """
            UPDATE issue_explanation_jobs
            SET status = 'running', attempts = attempts + 1,
                updated_at = ?, lease_until = ?, lease_token = ?
            WHERE job_id = ?
            """,
            (current, current + lease_seconds, token, row["job_id"]),
        )
        claimed = db.execute(
            "SELECT * FROM issue_explanation_jobs WHERE job_id = ?",
            (row["job_id"],),
        ).fetchone()

    return _sqlite_job(claimed) if claimed else None


def complete(job: dict[str, Any], *, now: float | None = None) -> bool:
    """Mark a job complete only if the caller still owns its lease."""
    current = now if now is not None else time.time()
    job_id = _required_text(job.get("job_id"), "job_id")
    lease_token = _required_text(job.get("lease_token"), "lease_token")

    if _mongo():
        result = _db()["issue_explanation_jobs"].update_one(
            {
                "job_id": job_id,
                "status": "running",
                "lease_token": lease_token,
                "lease_until": {"$gt": current},
            },
            {
                "$set": {
                    "status": "completed",
                    "updated_at": current,
                    "lease_until": None,
                    "lease_token": None,
                    "error_code": None,
                }
            },
        )
        return result.modified_count > 0

    with connection() as db:
        result = db.execute(
            """
            UPDATE issue_explanation_jobs
            SET status = 'completed', updated_at = ?, lease_until = NULL,
                lease_token = NULL, error_code = NULL
            WHERE job_id = ? AND status = 'running' AND lease_token = ?
              AND lease_until > ?
            """,
            (current, job_id, lease_token, current),
        )
    return result.rowcount > 0


def fail(
    job: dict[str, Any],
    *,
    error_code: str = "provider_error",
    retryable: bool = True,
    now: float | None = None,
) -> str:
    """Retry a failed job with backoff, or mark it terminally failed."""
    current = now if now is not None else time.time()
    job_id = _required_text(job.get("job_id"), "job_id")
    lease_token = _required_text(job.get("lease_token"), "lease_token")
    attempts = int(job.get("attempts", 1))
    max_attempts = int(job.get("max_attempts", MAX_ATTEMPTS))
    retry = retryable and attempts < max_attempts
    status = "queued" if retry else "failed"
    delay = min(MAX_BACKOFF_SECONDS, 2 ** max(1, attempts)) if retry else 0
    safe_error = re.sub(r"[^a-zA-Z0-9_]", "_", str(error_code))[:80] or "provider_error"

    values = {
        "status": status,
        "updated_at": current,
        "available_at": current + delay,
        "lease_until": None,
        "lease_token": None,
        "error_code": safe_error,
    }

    if _mongo():
        result = _db()["issue_explanation_jobs"].update_one(
            {
                "job_id": job_id,
                "status": "running",
                "lease_token": lease_token,
                "lease_until": {"$gt": current},
            },
            {"$set": values},
        )
        return status if result.modified_count else "lease_lost"

    with connection() as db:
        result = db.execute(
            """
            UPDATE issue_explanation_jobs
            SET status = ?, updated_at = ?, available_at = ?,
                lease_until = NULL, lease_token = NULL, error_code = ?
            WHERE job_id = ? AND status = 'running' AND lease_token = ?
              AND lease_until > ?
            """,
            (
                status,
                current,
                current + delay,
                safe_error,
                job_id,
                lease_token,
                current,
            ),
        )
    return status if result.rowcount else "lease_lost"


def get_job(job_id: str, owner_id: str) -> dict[str, Any] | None:
    """Return one owner-scoped job."""
    job_id = _required_text(job_id, "job_id")
    owner_id = _required_text(owner_id, "owner_id")

    if _mongo():
        item = _db()["issue_explanation_jobs"].find_one(
            {"job_id": job_id, "owner_id": owner_id},
            {"_id": 0},
        )
        return item

    with connection() as db:
        row = db.execute(
            """
            SELECT * FROM issue_explanation_jobs
            WHERE job_id = ? AND owner_id = ?
            """,
            (job_id, owner_id),
        ).fetchone()
    return _sqlite_job(row) if row else None


def list_for_review(review_id: str, owner_id: str) -> list[dict[str, Any]]:
    """Return all explanation jobs for one owned review."""
    review_id = _required_text(review_id, "review_id")
    owner_id = _required_text(owner_id, "owner_id")

    if _mongo():
        return list(
            _db()["issue_explanation_jobs"].find(
                {"review_id": review_id, "owner_id": owner_id},
                {"_id": 0},
            ).sort("created_at", 1)
        )

    with connection() as db:
        rows = db.execute(
            """
            SELECT * FROM issue_explanation_jobs
            WHERE review_id = ? AND owner_id = ?
            ORDER BY created_at
            """,
            (review_id, owner_id),
        ).fetchall()
    return [_sqlite_job(row) for row in rows]


def _sqlite_job(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value.strip()


def _mongo() -> bool:
    from app.config import storage_backend

    return storage_backend() == "mongodb"


def _db():
    from app.database.mongo_connection import get_mongo_database

    return get_mongo_database()
