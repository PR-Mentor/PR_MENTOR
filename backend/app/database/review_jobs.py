"""Small durable bounded review queue backed by the configured application DB."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.database.connection import connection

MAX_ATTEMPTS = 3
LEASE_SECONDS = 120


def enqueue(owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = uuid4().hex
    now = time.time()
    if _mongo():
        _db()["review_jobs"].insert_one(
            {
                "job_id": job_id,
                "owner_id": owner_id,
                "payload": payload,
                "status": "queued",
                "attempts": 0,
                "max_attempts": MAX_ATTEMPTS,
                "created_at": datetime.fromtimestamp(now, UTC),
                "updated_at": datetime.fromtimestamp(now, UTC),
                "available_at": datetime.fromtimestamp(now, UTC),
                "lease_until": None,
                "lease_token": None,
                "review_id": None,
                "error_code": None,
            }
        )
    else:
        with connection() as db:
            db.execute(
                "INSERT INTO review_jobs(job_id, owner_id, payload, status, attempts, max_attempts, created_at, updated_at, available_at) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)",
                (
                    job_id,
                    owner_id,
                    json.dumps(payload, separators=(",", ":")),
                    MAX_ATTEMPTS,
                    now,
                    now,
                    now,
                ),
            )
    return {
        "job_id": job_id,
        "status": "queued",
        "attempts": 0,
        "created_at": datetime.fromtimestamp(now, UTC).isoformat(),
    }


def get_job(job_id: str, owner_id: str) -> dict[str, Any] | None:
    if _mongo():
        item = _db()["review_jobs"].find_one(
            {"job_id": job_id, "owner_id": owner_id}, {"_id": 0, "payload": 0}
        )
        if item:
            for key in ("created_at", "updated_at"):
                if isinstance(item.get(key), datetime):
                    item[key] = item[key].isoformat()
        return item
    with connection() as db:
        row = db.execute(
            "SELECT job_id, status, attempts, max_attempts, created_at, updated_at, review_id, error_code FROM review_jobs WHERE job_id = ? AND owner_id = ?",
            (job_id, owner_id),
        ).fetchone()
    if not row:
        return None
    return {
        **dict(row),
        "created_at": datetime.fromtimestamp(row["created_at"], UTC).isoformat(),
        "updated_at": datetime.fromtimestamp(row["updated_at"], UTC).isoformat(),
    }


def claim_next(
    *, now: float | None = None, lease_seconds: int = LEASE_SECONDS
) -> dict[str, Any] | None:
    current = now if now is not None else time.time()
    if _mongo():
        from pymongo import ReturnDocument

        moment = datetime.fromtimestamp(current, UTC)
        jobs = _db()["review_jobs"]
        jobs.update_many(
            {"status": "running", "lease_until": {"$lte": moment}},
            {
                "$set": {
                    "status": "failed",
                    "error_code": "worker_lease_expired",
                    "updated_at": moment,
                    "lease_until": None,
                    "lease_token": None,
                }
            },
        )
        lease_token = uuid4().hex
        claimed = jobs.find_one_and_update(
            {
                "attempts": {"$lt": MAX_ATTEMPTS},
                "$or": [
                    {"status": "queued", "available_at": {"$lte": moment}},
                    {"status": "running", "lease_until": {"$lte": moment}},
                ],
            },
            {
                "$set": {
                    "status": "running",
                    "updated_at": moment,
                    "lease_until": datetime.fromtimestamp(current + lease_seconds, UTC),
                    "lease_token": lease_token,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        return _without_id(claimed)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE review_jobs SET status='failed', error_code='worker_lease_expired', updated_at=?, lease_until=NULL, lease_token=NULL WHERE status='running' AND lease_until <= ?",
            (current, current),
        )
        lease_token = uuid4().hex
        row = db.execute(
            "SELECT job_id FROM review_jobs WHERE attempts < max_attempts AND ((status='queued' AND available_at <= ?) OR (status='running' AND lease_until <= ?)) ORDER BY created_at LIMIT 1",
            (current, current),
        ).fetchone()
        if not row:
            return None
        db.execute(
            "UPDATE review_jobs SET status='running', attempts=attempts+1, updated_at=?, lease_until=?, lease_token=? WHERE job_id=?",
            (current, current + lease_seconds, lease_token, row["job_id"]),
        )
        claimed = db.execute(
            "SELECT * FROM review_jobs WHERE job_id=?", (row["job_id"],)
        ).fetchone()
    item = dict(claimed)
    item["payload"] = json.loads(item["payload"])
    return item


def complete(job_id: str, status: str, review_id: str, lease_token: str) -> bool:
    if status not in {"completed", "partial"}:
        raise ValueError("review job completion status is invalid")
    now = time.time()
    if _mongo():
        changed = _db()["review_jobs"].update_one(
            {"job_id": job_id, "status": "running", "lease_token": lease_token, "lease_until": {"$gt": datetime.fromtimestamp(now, UTC)}},
            {
                "$set": {
                    "status": status,
                    "review_id": review_id,
                    "updated_at": datetime.fromtimestamp(now, UTC),
                    "lease_until": None,
                    "lease_token": None,
                }
            },
        )
    else:
        with connection() as db:
            changed = db.execute(
                "UPDATE review_jobs SET status=?, review_id=?, updated_at=?, lease_until=NULL, lease_token=NULL WHERE job_id=? AND status='running' AND lease_token=? AND lease_until>?",
                (status, review_id, now, job_id, lease_token, now),
            )
    if _mongo():
        return changed.modified_count > 0
    return changed.rowcount > 0


def heartbeat(
    job_id: str, lease_token: str, *, now: float | None = None, lease_seconds: int = LEASE_SECONDS
) -> bool:
    current = now if now is not None else time.time()
    if _mongo():
        moment = datetime.fromtimestamp(current, UTC)
        result = _db()["review_jobs"].update_one(
            {"job_id": job_id, "status": "running", "lease_token": lease_token, "lease_until": {"$gt": datetime.fromtimestamp(current, UTC)}},
            {
                "$set": {
                    "updated_at": moment,
                    "lease_until": datetime.fromtimestamp(current + lease_seconds, UTC),
                }
            },
        )
        return result.modified_count > 0
    with connection() as db:
        result = db.execute(
            "UPDATE review_jobs SET updated_at=?, lease_until=? WHERE job_id=? AND status='running' AND lease_token=? AND lease_until>?",
            (current, current + lease_seconds, job_id, lease_token, current),
        )
    return result.rowcount > 0


def fail(
    job: dict[str, Any], *, error_code: str = "review_failed", retryable: bool = False, now: float | None = None
) -> str:
    current = now if now is not None else time.time()
    retry = retryable and int(job.get("attempts", 0)) < int(job.get("max_attempts", MAX_ATTEMPTS))
    status = "queued" if retry else "failed"
    delay = min(30, 2 ** int(job.get("attempts", 1))) if retry else 0
    if _mongo():
        changed = _db()["review_jobs"].update_one(
            {"job_id": job["job_id"], "status": "running", "lease_token": job.get("lease_token"), "lease_until": {"$gt": datetime.fromtimestamp(current, UTC)}},
            {
                "$set": {
                    "status": status,
                    "updated_at": datetime.fromtimestamp(current, UTC),
                    "available_at": datetime.fromtimestamp(current + delay, UTC),
                    "lease_until": None,
                    "lease_token": None,
                    "error_code": error_code,
                }
            },
        )
        if not changed.modified_count:
            return "lease_lost"
    else:
        with connection() as db:
            changed = db.execute(
                "UPDATE review_jobs SET status=?, updated_at=?, available_at=?, lease_until=NULL, lease_token=NULL, error_code=? WHERE job_id=? AND status='running' AND lease_token=? AND lease_until>?",
                (status, current, current + delay, error_code, job["job_id"], job.get("lease_token"), current),
            )
        if not changed.rowcount:
            return "lease_lost"
    return status


def cancel_queued(job_id: str, owner_id: str) -> bool:
    if _mongo():
        result = _db()["review_jobs"].update_one(
            {"job_id": job_id, "owner_id": owner_id, "status": "queued"},
            {"$set": {"status": "cancelled", "updated_at": datetime.now(UTC)}},
        )
        return result.modified_count > 0
    with connection() as db:
        result = db.execute(
            "UPDATE review_jobs SET status='cancelled', updated_at=? WHERE job_id=? AND owner_id=? AND status='queued'",
            (time.time(), job_id, owner_id),
        )
    return result.rowcount > 0


def _mongo() -> bool:
    from app.config import storage_backend

    return storage_backend() == "mongodb"


def _db():
    from app.database.mongo_connection import get_mongo_database

    return get_mongo_database()


def _without_id(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item:
        item.pop("_id", None)
    return item
