"""Account and opaque-session persistence for the first-party API."""

import hashlib
import time
from datetime import UTC, datetime
from typing import Any

from app.database.connection import connection


def create_user(user_id: str, email: str, password_hash: str) -> bool:
    if _mongo():
        from pymongo.errors import DuplicateKeyError

        users = _mongo_db()["auth_users"]
        try:
            users.insert_one(
                {
                    "user_id": user_id,
                    "email": email,
                    "password_hash": password_hash,
                    "created_at": datetime.now(UTC),
                }
            )
        except DuplicateKeyError:
            return False
        return True
    with connection() as db:
        try:
            db.execute(
                "INSERT INTO auth_users(user_id, email, password_hash) VALUES (?, ?, ?)",
                (user_id, email, password_hash),
            )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                return False
            raise
    return True


def get_user_by_email(email: str) -> dict[str, Any] | None:
    if _mongo():
        return _mongo_db()["auth_users"].find_one({"email": email}, {"_id": 0})
    with connection() as db:
        row = db.execute(
            "SELECT user_id, email, password_hash FROM auth_users WHERE email = ?",
            (email,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict[str, str] | None:
    """Return public account identity without password or session material."""
    if _mongo():
        item = _mongo_db()["auth_users"].find_one(
            {"user_id": user_id}, {"_id": 0, "user_id": 1, "email": 1}
        )
        return {"user_id": str(item["user_id"]), "email": str(item["email"])} if item else None
    with connection() as db:
        row = db.execute(
            "SELECT user_id, email FROM auth_users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def save_session(token_hash: str, user_id: str, expires_at: datetime) -> None:
    if _mongo():
        _mongo_db()["auth_sessions"].replace_one(
            {"token_hash": token_hash},
            {"token_hash": token_hash, "user_id": user_id, "expires_at": expires_at},
            upsert=True,
        )
        return
    with connection() as db:
        db.execute(
            "INSERT INTO auth_sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, expires_at.isoformat()),
        )


def get_session(token_hash: str) -> dict[str, Any] | None:
    if _mongo():
        return _mongo_db()["auth_sessions"].find_one(
            {"token_hash": token_hash}, {"_id": 0}
        )
    with connection() as db:
        row = db.execute(
            "SELECT token_hash, user_id, expires_at FROM auth_sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token_hash: str) -> None:
    if _mongo():
        _mongo_db()["auth_sessions"].delete_one({"token_hash": token_hash})
        return
    with connection() as db:
        db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))


def allow_attempt(
    scope: str, key: str, *, limit: int = 10, window_seconds: int = 900
) -> bool:
    """Atomic fixed-window limiter; sensitive identifiers are never stored raw."""
    digest = hashlib.sha256(f"{scope}:{key}".encode()).hexdigest()
    window_start = int(time.time()) // window_seconds * window_seconds
    if _mongo():
        bucket_id = f"{digest}:{window_start}"
        bucket = _mongo_db()["auth_rate_limits"]
        bucket.update_one(
            {"_id": bucket_id},
            {
                "$inc": {"hit_count": 1},
                "$setOnInsert": {
                    "expires_at": datetime.fromtimestamp(
                        window_start + window_seconds, UTC
                    )
                },
            },
            upsert=True,
        )
        return (
            bucket.find_one({"_id": bucket_id}, {"hit_count": 1})["hit_count"] <= limit
        )
    with connection() as db:
        db.execute(
            "DELETE FROM auth_rate_limits WHERE window_start < ?",
            (window_start - window_seconds,),
        )
        db.execute(
            "INSERT INTO auth_rate_limits(bucket_key, window_start, hit_count) VALUES (?, ?, 1) "
            "ON CONFLICT(bucket_key) DO UPDATE SET hit_count=CASE WHEN window_start=excluded.window_start THEN hit_count+1 ELSE 1 END, window_start=excluded.window_start",
            (digest, window_start),
        )
        row = db.execute(
            "SELECT hit_count FROM auth_rate_limits WHERE bucket_key = ?", (digest,)
        ).fetchone()
    return row["hit_count"] <= limit


def _mongo() -> bool:
    from app.config import storage_backend

    return storage_backend() == "mongodb"


def _mongo_db():
    from app.database.mongo_connection import get_mongo_database

    return get_mongo_database()
