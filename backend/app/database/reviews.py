"""Replaceable persistence interface and process-independent SQLite store."""

import json
from collections import Counter
from typing import Any, Protocol
from uuid import uuid4

from app.database.connection import connection


class ReviewStore(Protocol):
    def save_review(self, review: dict[str, Any], user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any]: ...
    def get_review(self, review_id: str, user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any] | None: ...
    def list_reviews(self, user_id: str | int | None = None, limit: int = 50, *, owner_id: str | None = None, include_demo: bool = True) -> list[dict[str, Any]]: ...
    def review_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int: ...
    def live_review_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int: ...
    def live_pull_request_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int: ...
    def live_finding_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int: ...
    def save_pull_request(self, url: str, user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any]: ...
    def list_saved(self, user_id: str | None = None, *, owner_id: str | None = None) -> list[dict[str, Any]]: ...
    def delete_saved(self, saved_id: str, user_id: str | None = None, *, owner_id: str | None = None) -> bool: ...
    def record_learning_attempt(self, exercise_id: str, category: str, correct: bool, user_id: str | None = None, *, owner_id: str | None = None) -> None: ...
    def learning_progress(self, user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any]: ...
    def learning_history(self, limit: int = 100, user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any]: ...
    def ping(self) -> None: ...


class SQLiteReviewStore:
    """SQLite implementation. A new instance is cheap; connections are per operation."""

    def ping(self) -> None:
        from app.database.connection import ping

        ping()

    def save_review(self, review: dict[str, Any], user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any]:
        review_id = review["review_id"]
        effective_id = user_id or owner_id or review.get("user_id") or review.get("owner_id")
        created_at = str(review.get("created_at", ""))
        payload = json.dumps(review, ensure_ascii=False, separators=(",", ":"), default=str)
        with connection() as db:
            db.execute(
                "INSERT INTO reviews(review_id, user_id, created_at, payload, owner_id) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(review_id) DO UPDATE SET "
                "created_at=excluded.created_at, "
                "payload=excluded.payload, "
                "user_id=COALESCE(reviews.user_id, excluded.user_id), "
                "owner_id=COALESCE(reviews.owner_id, excluded.owner_id)",
                (review_id, effective_id, created_at, payload, effective_id),
            )
        return review

    def get_review(self, review_id: str, user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any] | None:
        effective_id = user_id or owner_id
        with connection() as db:
            row = db.execute(
                "SELECT payload FROM reviews WHERE review_id = ? AND (? IS NULL OR user_id = ? OR owner_id = ?)",
                (review_id, effective_id, effective_id, effective_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_reviews(
        self,
        user_id: str | int | None = None,
        limit: int = 50,
        *,
        owner_id: str | None = None,
        include_demo: bool = True,
    ) -> list[dict[str, Any]]:
        if isinstance(user_id, int) and not isinstance(user_id, bool):
            bounded = max(1, min(int(user_id), 100))
            effective_id = str(limit) if isinstance(limit, str) else owner_id
        else:
            bounded = max(1, min(int(limit), 100))
            effective_id = user_id or owner_id

        with connection() as db:
            if include_demo:
                rows = db.execute(
                    "SELECT payload FROM reviews WHERE (? IS NULL OR user_id = ? OR owner_id = ?) "
                    "ORDER BY rowid DESC LIMIT ?",
                    (effective_id, effective_id, effective_id, bounded),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT payload FROM reviews WHERE (? IS NULL OR user_id = ? OR owner_id = ?) "
                    "AND COALESCE(json_extract(payload, '$.is_demo'), 0) = 0 "
                    "AND COALESCE(json_extract(payload, '$.analysis_mode'), '') <> 'demo' "
                    "ORDER BY rowid DESC LIMIT ?",
                    (effective_id, effective_id, effective_id, bounded),
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def review_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int:
        effective_id = user_id or owner_id
        with connection() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM reviews WHERE (? IS NULL OR user_id = ? OR owner_id = ?)",
                (effective_id, effective_id, effective_id),
            ).fetchone()
        return int(row["count"])

    def live_review_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int:
        effective_id = user_id or owner_id
        with connection() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM reviews WHERE (? IS NULL OR user_id = ? OR owner_id = ?) "
                "AND COALESCE(json_extract(payload, '$.is_demo'), 0) = 0 "
                "AND COALESCE(json_extract(payload, '$.analysis_mode'), '') <> 'demo'",
                (effective_id, effective_id, effective_id),
            ).fetchone()
        return int(row["count"])

    def live_pull_request_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int:
        effective_id = user_id or owner_id
        with connection() as db:
            # Use a composite key (owner/name/number) since pull_request_url may not be set
            row = db.execute(
                "SELECT COUNT(DISTINCT "
                "  COALESCE(json_extract(payload, '$.pull_request.pull_request_url'), "
                "    json_extract(payload, '$.pull_request.repository_owner') || '/' || "
                "    json_extract(payload, '$.pull_request.repository_name') || '#' || "
                "    CAST(json_extract(payload, '$.pull_request.pull_request_number') AS TEXT)"
                "  )"
                ") AS count "
                "FROM reviews WHERE (? IS NULL OR user_id = ? OR owner_id = ?) "
                "AND json_extract(payload, '$.pull_request') IS NOT NULL "
                "AND COALESCE(json_extract(payload, '$.is_demo'), 0) = 0 "
                "AND COALESCE(json_extract(payload, '$.analysis_mode'), '') <> 'demo'",
                (effective_id, effective_id, effective_id),
            ).fetchone()
        return int(row["count"])

    def live_finding_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int:
        reviews = self.list_reviews(user_id=user_id, owner_id=owner_id, limit=100, include_demo=False)
        total = 0
        for item in reviews:
            if "canonical_issues" in item:
                total += len(item["canonical_issues"])
            else:
                total += len(item.get("findings", []))
        return total

    def save_pull_request(self, url: str, user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any]:
        effective_id = user_id or owner_id
        with connection() as db:
            row = db.execute(
                "SELECT saved_id, url FROM saved_pull_requests WHERE url = ? AND (? IS NULL OR user_id = ? OR owner_id = ?)",
                (url, effective_id, effective_id, effective_id),
            ).fetchone()
            if row is None:
                saved_id = uuid4().hex
                db.execute(
                    "INSERT INTO saved_pull_requests(saved_id, user_id, url, owner_id) VALUES (?, ?, ?, ?)",
                    (saved_id, effective_id, url, effective_id),
                )
            else:
                saved_id = row["saved_id"]
        return {"saved_id": saved_id, "url": url, "temporary": False}

    def list_saved(self, user_id: str | None = None, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        effective_id = user_id or owner_id
        with connection() as db:
            rows = db.execute(
                "SELECT saved_id, url FROM saved_pull_requests WHERE (? IS NULL OR user_id = ? OR owner_id = ?) "
                "ORDER BY created_at DESC, rowid DESC",
                (effective_id, effective_id, effective_id),
            ).fetchall()
        return [{"saved_id": row["saved_id"], "url": row["url"], "temporary": False} for row in rows]

    def delete_saved(self, saved_id: str, user_id: str | None = None, *, owner_id: str | None = None) -> bool:
        effective_id = user_id or owner_id
        with connection() as db:
            cursor = db.execute(
                "DELETE FROM saved_pull_requests WHERE saved_id = ? AND (? IS NULL OR user_id = ? OR owner_id = ?)",
                (saved_id, effective_id, effective_id, effective_id),
            )
        return cursor.rowcount > 0

    def record_learning_attempt(
        self,
        exercise_id: str,
        category: str,
        correct: bool,
        user_id: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> None:
        effective_id = user_id or owner_id
        with connection() as db:
            db.execute(
                "INSERT INTO learning_attempts(exercise_id, category, correct, user_id, owner_id) VALUES (?, ?, ?, ?, ?)",
                (exercise_id, category, int(correct), effective_id, effective_id),
            )

    def learning_progress(self, user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any]:
        effective_id = user_id or owner_id
        with connection() as db:
            rows = db.execute(
                "SELECT category, correct FROM learning_attempts WHERE (? IS NULL OR user_id = ? OR owner_id = ?)",
                (effective_id, effective_id, effective_id),
            ).fetchall()
            completed = db.execute(
                "SELECT COUNT(DISTINCT exercise_id) AS count FROM learning_attempts WHERE (? IS NULL OR user_id = ? OR owner_id = ?)",
                (effective_id, effective_id, effective_id),
            ).fetchone()["count"]
        count = len(rows)
        correct = sum(row["correct"] for row in rows)
        topics = dict(Counter(row["category"] for row in rows))
        return {
            "temporary": False,
            "exercises_completed": completed,
            "correct_answers": correct,
            "attempt_count": count,
            "accuracy": correct / count if count else 0,
            "topics_practiced": topics,
        }

    def learning_history(
        self,
        limit: int = 100,
        user_id: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        effective_id = user_id or owner_id
        with connection() as db:
            rows = db.execute(
                "SELECT exercise_id, category, correct FROM learning_attempts "
                "WHERE (? IS NULL OR user_id = ? OR owner_id = ?) "
                "ORDER BY attempt_id DESC LIMIT ?",
                (effective_id, effective_id, effective_id, max(1, min(limit, 100))),
            ).fetchall()
        return {
            "temporary": False,
            "attempts": [
                {
                    "exercise_id": row["exercise_id"],
                    "category": row["category"],
                    "correct": bool(row["correct"]),
                }
                for row in rows
            ],
        }


def get_store() -> ReviewStore:
    """Return the configured storage adapter without silent backend fallback."""
    from app.config import storage_backend

    if storage_backend() == "mongodb":
        from app.database.mongo_connection import get_mongo_database
        from app.database.mongo_reviews import MongoReviewStore

        return MongoReviewStore(get_mongo_database(), ensure_indexes=False)
    return SQLiteReviewStore()
