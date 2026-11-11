"""Durable bounded execution-job storage shared by API and trusted worker."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

MAX_JOB_PAYLOAD_BYTES = 200_000
MAX_RESULT_BYTES = 4_500_000
LEASE_SECONDS = 180


def enqueue(owner_id: str, idempotency_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not owner_id or not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("execution owner and bounded idempotency key are required")
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > MAX_JOB_PAYLOAD_BYTES:
        raise ValueError("execution payload exceeds its storage limit")
    payload_digest = hashlib.sha256(encoded_bytes).hexdigest()
    now = time.time()
    execution_id = uuid4().hex
    if _mongo():
        collection = _db()["test_execution_jobs"]
        existing = collection.find_one({"owner_id": owner_id, "idempotency_key": idempotency_key})
        if existing:
            if existing.get("payload_sha256") != payload_digest:
                raise IdempotencyConflict("idempotency key was already used for a different execution payload")
            return _public(existing)
        try:
            collection.insert_one({
                "execution_id": execution_id,
                "owner_id": owner_id,
                "idempotency_key": idempotency_key,
                "payload_sha256": payload_digest,
                "payload": payload,
                "status": "queued",
                "created_at": datetime.fromtimestamp(now, UTC),
                "updated_at": datetime.fromtimestamp(now, UTC),
                "lease_until": None,
                "cancel_requested": False,
                "result": None,
                "error_code": None,
            })
            return {"execution_id": execution_id, "status": "queued", "created_at": datetime.fromtimestamp(now, UTC).isoformat()}
        except Exception:
            # The unique owner/key index resolves concurrent duplicate submits.
            existing = collection.find_one({"owner_id": owner_id, "idempotency_key": idempotency_key})
            if existing:
                if existing.get("payload_sha256") != payload_digest:
                    raise IdempotencyConflict("idempotency key was already used for a different execution payload")
                return _public(existing)
            raise
    from app.database.connection import connection

    with connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO test_execution_jobs(execution_id, owner_id, idempotency_key, payload_sha256, payload, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (execution_id, owner_id, idempotency_key, payload_digest, encoded, now, now),
        )
        row = db.execute(
            "SELECT execution_id, status, created_at, payload_sha256 FROM test_execution_jobs WHERE owner_id=? AND idempotency_key=?",
            (owner_id, idempotency_key),
        ).fetchone()
    if row["payload_sha256"] != payload_digest:
        raise IdempotencyConflict("idempotency key was already used for a different execution payload")
    return {"execution_id": row["execution_id"], "status": row["status"], "created_at": datetime.fromtimestamp(row["created_at"], UTC).isoformat()}


def get_job(execution_id: str, owner_id: str) -> dict[str, Any] | None:
    if _mongo():
        item = _db()["test_execution_jobs"].find_one(
            {"execution_id": execution_id, "owner_id": owner_id}, {"_id": 0, "owner_id": 0, "idempotency_key": 0}
        )
        return _public(item) if item else None
    from app.database.connection import connection

    with connection() as db:
        row = db.execute(
            "SELECT execution_id, status, created_at, updated_at, result, error_code, cancel_requested, payload FROM test_execution_jobs WHERE execution_id=? AND owner_id=?",
            (execution_id, owner_id),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for name in ("created_at", "updated_at"):
        if result.get(name) is not None:
            result[name] = datetime.fromtimestamp(result[name], UTC).isoformat()
    result["result"] = json.loads(result["result"]) if result["result"] else None
    payload = json.loads(result.pop("payload"))
    result["generated_test"] = payload.get("generated_test")
    for name in ("review_id", "issue_id", "fix_id"):
        if isinstance(payload.get(name), str):
            result[name] = payload[name]
    result["cancel_requested"] = bool(result["cancel_requested"])
    return result


def claim_next(*, now: float | None = None) -> dict[str, Any] | None:
    current = now if now is not None else time.time()
    if _mongo():
        from pymongo import ReturnDocument

        collection = _db()["test_execution_jobs"]
        moment = datetime.fromtimestamp(current, UTC)
        collection.update_many(
            {"status": "running", "lease_until": {"$lte": moment}},
            {"$set": {"status": "infrastructure_error", "error_code": "worker_lease_expired", "updated_at": moment, "lease_until": None}},
        )
        item = collection.find_one_and_update(
            {"status": "queued"},
            {"$set": {"status": "running", "updated_at": moment, "lease_until": datetime.fromtimestamp(current + LEASE_SECONDS, UTC)}},
            sort=[("created_at", 1)], return_document=ReturnDocument.AFTER,
        )
        return _without_id(item)
    from app.database.connection import connection

    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE test_execution_jobs SET status='infrastructure_error', error_code='worker_lease_expired', updated_at=?, lease_until=NULL WHERE status='running' AND lease_until <= ?",
            (current, current),
        )
        row = db.execute("SELECT execution_id FROM test_execution_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            return None
        db.execute(
            "UPDATE test_execution_jobs SET status='running', updated_at=?, lease_until=? WHERE execution_id=? AND status='queued'",
            (current, current + LEASE_SECONDS, row["execution_id"]),
        )
        claimed = db.execute("SELECT * FROM test_execution_jobs WHERE execution_id=?", (row["execution_id"],)).fetchone()
    item = dict(claimed)
    item["payload"] = json.loads(item["payload"])
    return item


def heartbeat(execution_id: str, *, now: float | None = None) -> bool:
    current = now if now is not None else time.time()
    if _mongo():
        moment = datetime.fromtimestamp(current, UTC)
        result = _db()["test_execution_jobs"].update_one(
            {"execution_id": execution_id, "status": "running", "lease_until": {"$gt": datetime.fromtimestamp(current, UTC)}},
            {"$set": {"updated_at": moment, "lease_until": datetime.fromtimestamp(current + LEASE_SECONDS, UTC)}},
        )
        return result.modified_count > 0
    from app.database.connection import connection

    with connection() as db:
        result = db.execute(
            "UPDATE test_execution_jobs SET updated_at=?, lease_until=? WHERE execution_id=? AND status='running' AND lease_until>?",
            (current, current + LEASE_SECONDS, execution_id, current),
        )
    return result.rowcount > 0


def cancellation_requested(execution_id: str) -> bool:
    if _mongo():
        item = _db()["test_execution_jobs"].find_one({"execution_id": execution_id}, {"cancel_requested": 1})
        return bool(item and item.get("cancel_requested"))
    from app.database.connection import connection

    with connection() as db:
        row = db.execute("SELECT cancel_requested FROM test_execution_jobs WHERE execution_id=?", (execution_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def complete(execution_id: str, status: str, result: dict[str, Any]) -> bool:
    allowed = {"passed", "failed", "timed_out", "cancelled", "unavailable", "rejected", "infrastructure_error"}
    if status not in allowed:
        raise ValueError("execution result status is invalid")
    encoded = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        status, encoded = "infrastructure_error", json.dumps({"reason": "Execution result exceeded its storage bound."})
    now = time.time()
    if _mongo():
        changed = _db()["test_execution_jobs"].update_one(
            {"execution_id": execution_id, "status": "running", "lease_until": {"$gt": datetime.fromtimestamp(now, UTC)}},
            {"$set": {"status": status, "result": json.loads(encoded), "updated_at": datetime.fromtimestamp(now, UTC), "lease_until": None}},
        )
        return changed.modified_count > 0
    from app.database.connection import connection

    with connection() as db:
        changed = db.execute(
            "UPDATE test_execution_jobs SET status=?, result=?, updated_at=?, lease_until=NULL WHERE execution_id=? AND status='running' AND lease_until>?",
            (status, encoded, now, execution_id, now),
        )
    return changed.rowcount > 0


def fail(execution_id: str, error_code: str) -> bool:
    safe_code = error_code[:80]
    now = time.time()
    if _mongo():
        changed = _db()["test_execution_jobs"].update_one(
            {"execution_id": execution_id, "status": "running", "lease_until": {"$gt": datetime.fromtimestamp(now, UTC)}},
            {"$set": {"status": "infrastructure_error", "error_code": safe_code, "updated_at": datetime.fromtimestamp(now, UTC), "lease_until": None}},
        )
        return changed.modified_count > 0
    from app.database.connection import connection

    with connection() as db:
        changed = db.execute(
            "UPDATE test_execution_jobs SET status='infrastructure_error', error_code=?, updated_at=?, lease_until=NULL WHERE execution_id=? AND status='running' AND lease_until>?",
            (safe_code, now, execution_id, now),
        )
    return changed.rowcount > 0


def cancel(execution_id: str, owner_id: str) -> str | None:
    """Cancel a queued execution or request cancellation of a running one."""
    if _mongo():
        collection = _db()["test_execution_jobs"]
        queued = collection.update_one(
            {"execution_id": execution_id, "owner_id": owner_id, "status": "queued"},
            {"$set": {"status": "cancelled", "updated_at": datetime.now(UTC)}},
        )
        if queued.modified_count:
            return "cancelled"
        running = collection.update_one(
            {"execution_id": execution_id, "owner_id": owner_id, "status": "running"},
            {"$set": {"cancel_requested": True, "updated_at": datetime.now(UTC)}},
        )
        return "cancellation_requested" if running.modified_count else None
    from app.database.connection import connection

    with connection() as db:
        queued = db.execute(
            "UPDATE test_execution_jobs SET status='cancelled', updated_at=? WHERE execution_id=? AND owner_id=? AND status='queued'",
            (time.time(), execution_id, owner_id),
        )
        if queued.rowcount:
            return "cancelled"
        running = db.execute(
            "UPDATE test_execution_jobs SET cancel_requested=1, updated_at=? WHERE execution_id=? AND owner_id=? AND status='running'",
            (time.time(), execution_id, owner_id),
        )
    return "cancellation_requested" if running.rowcount else None


def _mongo() -> bool:
    from app.config import storage_backend

    return storage_backend() == "mongodb"


class IdempotencyConflict(ValueError):
    """A caller reused an idempotency key with different execution content."""


def _db():
    from app.database.mongo_connection import get_mongo_database

    return get_mongo_database()


def _without_id(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item:
        item.pop("_id", None)
        item.pop("owner_id", None)
        item.pop("idempotency_key", None)
        item.pop("payload_sha256", None)
    return item


def _public(item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item)
    item.pop("_id", None)
    item.pop("owner_id", None)
    item.pop("idempotency_key", None)
    item.pop("payload_sha256", None)
    payload = item.pop("payload", {})
    if isinstance(payload, dict):
        item["generated_test"] = payload.get("generated_test")
        for name in ("review_id", "issue_id", "fix_id"):
            if isinstance(payload.get(name), str):
                item[name] = payload[name]
    for name in ("created_at", "updated_at"):
        value = item.get(name)
        if isinstance(value, datetime):
            item[name] = value.isoformat()
    return item
