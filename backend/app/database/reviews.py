"""Replaceable persistence interface and process-independent SQLite store."""

import json
from collections import Counter
from typing import Any, Protocol
from uuid import uuid4

from app.database.connection import connection


class ReviewStore(Protocol):
    def save_review(self, review: dict[str, Any], user_id: str) -> dict[str, Any]: ...
    def get_review(self, review_id: str, user_id: str) -> dict[str, Any] | None: ...
    def list_reviews(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]: ...
    def save_pull_request(self, url: str, user_id: str) -> dict[str, Any]: ...
    def list_saved(self, user_id: str) -> list[dict[str, Any]]: ...
    def delete_saved(self, saved_id: str, user_id: str) -> bool: ...
    def record_learning_attempt(self, exercise_id: str, category: str, correct: bool) -> None: ...
    def learning_progress(self) -> dict[str, Any]: ...
    def learning_history(self, limit: int = 100) -> dict[str, Any]: ...
    def ping(self) -> None: ...


class SQLiteReviewStore:
    """SQLite implementation. A new instance is cheap; connections are per operation."""

    def ping(self) -> None:
        from app.database.connection import ping

        ping()

    def save_review(self, review: dict[str, Any], user_id: str) -> dict[str, Any]:
        review_id = review["review_id"]
        created_at = str(review.get("created_at", ""))
        payload = json.dumps(review, ensure_ascii=False, separators=(",", ":"), default=str)
        with connection() as db:
            db.execute("INSERT INTO reviews(review_id, user_id, created_at, payload) VALUES (?, ?, ?, ?) ON CONFLICT(review_id) DO UPDATE SET created_at=excluded.created_at, payload=excluded.payload WHERE reviews.user_id=excluded.user_id", (review_id, user_id, created_at, payload))
        return review

    def get_review(self, review_id: str, user_id: str) -> dict[str, Any] | None:
        with connection() as db:
            row = db.execute("SELECT payload FROM reviews WHERE review_id = ? AND user_id = ?", (review_id, user_id)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_reviews(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        with connection() as db:
            rows = db.execute("SELECT payload FROM reviews WHERE user_id = ? ORDER BY rowid DESC LIMIT ?", (user_id, bounded)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_pull_request(self, url: str, user_id: str) -> dict[str, Any]:
        with connection() as db:
            row = db.execute("SELECT saved_id, url FROM saved_pull_requests WHERE url = ? AND user_id = ?", (url, user_id)).fetchone()
            if row is None:
                saved_id = uuid4().hex
                db.execute("INSERT INTO saved_pull_requests(saved_id, user_id, url) VALUES (?, ?, ?)", (saved_id, user_id, url))
            else:
                saved_id = row["saved_id"]
        return {"saved_id": saved_id, "url": url, "temporary": False}

    def list_saved(self, user_id: str) -> list[dict[str, Any]]:
        with connection() as db:
            rows = db.execute("SELECT saved_id, url FROM saved_pull_requests WHERE user_id = ? ORDER BY created_at DESC, rowid DESC", (user_id,)).fetchall()
        return [{"saved_id": row["saved_id"], "url": row["url"], "temporary": False} for row in rows]

    def delete_saved(self, saved_id: str, user_id: str) -> bool:
        with connection() as db:
            cursor = db.execute("DELETE FROM saved_pull_requests WHERE saved_id = ? AND user_id = ?", (saved_id, user_id))
        return cursor.rowcount > 0

    def record_learning_attempt(self, exercise_id: str, category: str, correct: bool) -> None:
        with connection() as db:
            db.execute("INSERT INTO learning_attempts(exercise_id, category, correct) VALUES (?, ?, ?)", (exercise_id, category, int(correct)))

    def learning_progress(self) -> dict[str, Any]:
        with connection() as db:
            rows = db.execute("SELECT category, correct FROM learning_attempts").fetchall()
            completed = db.execute("SELECT COUNT(DISTINCT exercise_id) AS count FROM learning_attempts").fetchone()["count"]
        count = len(rows)
        correct = sum(row["correct"] for row in rows)
        topics = dict(Counter(row["category"] for row in rows))
        return {"temporary": False, "exercises_completed": completed, "correct_answers": correct, "attempt_count": count, "accuracy": correct / count if count else 0, "topics_practiced": topics}

    def learning_history(self, limit: int = 100) -> dict[str, Any]:
        with connection() as db:
            rows = db.execute("SELECT exercise_id, category, correct FROM learning_attempts ORDER BY attempt_id DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
        return {"temporary": False, "attempts": [{"exercise_id": row["exercise_id"], "category": row["category"], "correct": bool(row["correct"])} for row in rows]}


def get_store() -> ReviewStore:
    """Return the configured storage adapter without silent backend fallback."""
    from app.config import storage_backend

    if storage_backend() == "mongodb":
        from app.database.mongo_reviews import MongoReviewStore
        from app.database.mongo_connection import get_mongo_database

        return MongoReviewStore(get_mongo_database(), ensure_indexes=False)
    return SQLiteReviewStore()
