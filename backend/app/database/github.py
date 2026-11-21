"""Persistence for single-use OAuth flows and encrypted GitHub connections."""

import json
import time
from datetime import UTC, datetime
from typing import Any

from app.config import storage_backend
from app.database.connection import connection
from app.database.mongo_connection import get_mongo_database


def save_flow(state_hash: str, challenge: str, user_id: str, verifier: str) -> None:
    expires_at = int(time.time()) + 600
    if storage_backend() == "mongodb":
        get_mongo_database()["github_oauth_flows"].insert_one(
            {
                "_id": state_hash,
                "challenge": challenge,
                "user_id": user_id,
                "verifier": verifier,
                "expires_at": datetime.fromtimestamp(expires_at, UTC),
            }
        )
        return
    with connection() as db:
        db.execute(
            "DELETE FROM github_oauth_flows WHERE expires_at <= ?", (int(time.time()),)
        )
        db.execute(
            "INSERT INTO github_oauth_flows VALUES (?, ?, ?, ?, ?)",
            (state_hash, challenge, user_id, verifier, expires_at),
        )


def consume_flow(state_hash: str, challenge: str, user_id: str) -> str | None:
    if storage_backend() == "mongodb":
        row = get_mongo_database()["github_oauth_flows"].find_one_and_delete(
            {
                "_id": state_hash,
                "challenge": challenge,
                "user_id": user_id,
                "expires_at": {"$gt": datetime.now(UTC)},
            }
        )
        return row["verifier"] if row else None
    with connection() as db:
        row = db.execute(
            "DELETE FROM github_oauth_flows WHERE state_hash = ? AND challenge = ? AND user_id = ? "
            "AND expires_at > ? RETURNING verifier",
            (state_hash, challenge, user_id, int(time.time())),
        ).fetchone()
        return row["verifier"] if row else None


def get_connection(user_id: str) -> dict[str, Any] | None:
    if storage_backend() == "mongodb":
        return get_mongo_database()["github_connections"].find_one(
            {"_id": user_id}, {"_id": 0}
        )
    with connection() as db:
        row = db.execute(
            "SELECT payload FROM github_connections WHERE user_id = ?", (user_id,)
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def save_connection(record: dict[str, Any]) -> None:
    user_id, version = record["user_id"], record["version"]
    if storage_backend() == "mongodb":
        get_mongo_database()["github_connections"].update_one(
            {"_id": user_id},
            {"$set": record},
            upsert=True,
        )
        return
    with connection() as db:
        db.execute(
            "INSERT INTO github_connections VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET version = excluded.version, payload = excluded.payload",
            (user_id, version, json.dumps(record)),
        )


def disconnect(record: dict[str, Any]) -> bool:
    user_id, version = record["user_id"], record["version"]
    updates = {
        "connected": False,
        "encrypted_token": None,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if storage_backend() == "mongodb":
        result = get_mongo_database()["github_connections"].update_one(
            {"_id": user_id, "version": version, "connected": True},
            {"$set": updates},
        )
        return bool(result.modified_count)
    with connection() as db:
        result = db.execute(
            "UPDATE github_connections SET payload = ? WHERE user_id = ? AND version = ? "
            "AND json_extract(payload, '$.connected') = 1",
            (json.dumps({**record, **updates}), user_id, version),
        )
        return bool(result.rowcount)
