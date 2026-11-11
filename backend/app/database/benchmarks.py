"""Owner-scoped persistence for human-labelled benchmark datasets and runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.config import storage_backend
from app.database.connection import connection


def _mongo():
    from app.database.mongo_connection import get_mongo_database

    return get_mongo_database()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_dataset(owner_id: str, name: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    dataset_id, now = uuid4().hex, _now()
    value = {"dataset_id": dataset_id, "owner_id": owner_id, "name": name, "cases": cases, "created_at": now, "updated_at": now}
    if storage_backend() == "mongodb":
        _mongo()["benchmark_datasets"].insert_one({**value, "_id": dataset_id})
    else:
        with connection() as db:
            db.execute("INSERT INTO benchmark_datasets VALUES (?, ?, ?, ?, ?, ?)", (dataset_id, owner_id, name, json.dumps(cases), now, now))
    return value


def get_dataset(owner_id: str, dataset_id: str) -> dict[str, Any] | None:
    if storage_backend() == "mongodb":
        item = _mongo()["benchmark_datasets"].find_one({"owner_id": owner_id, "dataset_id": dataset_id}, {"_id": 0})
        return item
    with connection() as db:
        row = db.execute("SELECT * FROM benchmark_datasets WHERE dataset_id=? AND owner_id=?", (dataset_id, owner_id)).fetchone()
    return {"dataset_id": row["dataset_id"], "owner_id": row["owner_id"], "name": row["name"], "cases": json.loads(row["cases_json"]), "created_at": row["created_at"], "updated_at": row["updated_at"]} if row else None


def list_datasets(owner_id: str) -> list[dict[str, Any]]:
    if storage_backend() == "mongodb":
        return list(_mongo()["benchmark_datasets"].find({"owner_id": owner_id}, {"_id": 0}).sort("updated_at", -1).limit(100))
    with connection() as db:
        rows = db.execute("SELECT * FROM benchmark_datasets WHERE owner_id=? ORDER BY updated_at DESC LIMIT 100", (owner_id,)).fetchall()
    return [{"dataset_id": row["dataset_id"], "owner_id": row["owner_id"], "name": row["name"], "cases": json.loads(row["cases_json"]), "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in rows]


def replace_cases(owner_id: str, dataset_id: str, cases: list[dict[str, Any]]) -> dict[str, Any] | None:
    now = _now()
    if storage_backend() == "mongodb":
        result = _mongo()["benchmark_datasets"].update_one({"owner_id": owner_id, "dataset_id": dataset_id}, {"$set": {"cases": cases, "updated_at": now}})
        return get_dataset(owner_id, dataset_id) if result.matched_count else None
    with connection() as db:
        result = db.execute("UPDATE benchmark_datasets SET cases_json=?, updated_at=? WHERE dataset_id=? AND owner_id=?", (json.dumps(cases), now, dataset_id, owner_id))
    return get_dataset(owner_id, dataset_id) if result.rowcount else None


def save_run(owner_id: str, dataset_id: str, payload: dict[str, Any]) -> None:
    if storage_backend() == "mongodb":
        _mongo()["benchmark_runs"].insert_one({**payload, "owner_id": owner_id, "dataset_id": dataset_id, "_id": payload["run_id"]})
    else:
        with connection() as db:
            db.execute("INSERT INTO benchmark_runs VALUES (?, ?, ?, ?, ?)", (payload["run_id"], dataset_id, owner_id, json.dumps(payload), payload["created_at"]))


def list_runs(owner_id: str, dataset_id: str) -> list[dict[str, Any]]:
    if storage_backend() == "mongodb":
        return list(_mongo()["benchmark_runs"].find({"owner_id": owner_id, "dataset_id": dataset_id}, {"_id": 0, "owner_id": 0, "dataset_id": 0}).sort("created_at", -1).limit(100))
    with connection() as db:
        rows = db.execute("SELECT payload FROM benchmark_runs WHERE owner_id=? AND dataset_id=? ORDER BY created_at DESC LIMIT 100", (owner_id, dataset_id)).fetchall()
    return [json.loads(row["payload"]) for row in rows]
