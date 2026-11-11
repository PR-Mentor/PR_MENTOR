"""Replaceable persistence interface and process-independent SQLite store."""

import json
from collections import Counter
from typing import Any, Protocol
from uuid import uuid4

from app.database.connection import connection


class ReviewStore(Protocol):
    def save_review(self, review: dict[str, Any], owner_id: str | None = None) -> dict[str, Any]: ...
    def get_review(self, review_id: str, owner_id: str | None = None) -> dict[str, Any] | None: ...
    def list_reviews(self, limit: int = 50, owner_id: str | None = None, *, include_demo: bool = True) -> list[dict[str, Any]]: ...
    def review_count(self, owner_id: str | None = None) -> int: ...
    def live_review_count(self, owner_id: str | None = None) -> int: ...
    def live_pull_request_count(self, owner_id: str | None = None) -> int: ...
    def live_finding_count(self, owner_id: str | None = None) -> int: ...
    def save_pull_request(self, url: str, owner_id: str | None = None) -> dict[str, Any]: ...
    def list_saved(self, owner_id: str | None = None) -> list[dict[str, Any]]: ...
    def delete_saved(self, saved_id: str, owner_id: str | None = None) -> bool: ...
    def record_learning_attempt(self, exercise_id: str, category: str, correct: bool, owner_id: str | None = None) -> None: ...
    def learning_progress(self, owner_id: str | None = None) -> dict[str, Any]: ...
    def learning_history(self, limit: int = 100, owner_id: str | None = None) -> dict[str, Any]: ...
    def ping(self) -> None: ...


class SQLiteReviewStore:
    """SQLite implementation. A new instance is cheap; connections are per operation."""

    def ping(self) -> None:
        from app.database.connection import ping

        ping()

    def save_review(self, review: dict[str, Any], owner_id: str | None = None) -> dict[str, Any]:
        review_id = review["review_id"]
        created_at = str(review.get("created_at", ""))
        payload = json.dumps(review, ensure_ascii=False, separators=(",", ":"), default=str)
        with connection() as db:
            db.execute("INSERT INTO reviews(review_id, created_at, payload, owner_id) VALUES (?, ?, ?, ?) ON CONFLICT(review_id) DO UPDATE SET created_at=excluded.created_at, payload=excluded.payload, owner_id=COALESCE(reviews.owner_id, excluded.owner_id)", (review_id, created_at, payload, owner_id))
        return review

    def get_review(self, review_id: str, owner_id: str | None = None) -> dict[str, Any] | None:
        with connection() as db:
            row = db.execute("SELECT payload FROM reviews WHERE review_id = ? AND (? IS NULL OR owner_id = ?)", (review_id, owner_id, owner_id)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_reviews(self, limit: int = 50, owner_id: str | None = None, *, include_demo: bool = True) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        with connection() as db:
            if include_demo:
                rows = db.execute(
                    "SELECT payload FROM reviews WHERE (? IS NULL OR owner_id = ?) "
                    "ORDER BY rowid DESC LIMIT ?",
                    (owner_id, owner_id, bounded),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT payload FROM reviews WHERE (? IS NULL OR owner_id = ?) "
                    "AND COALESCE(json_extract(payload, '$.is_demo'), 0) = 0 "
                    "AND COALESCE(json_extract(payload, '$.analysis_mode'), '') <> 'demo' "
                    "ORDER BY rowid DESC LIMIT ?",
                    (owner_id, owner_id, bounded),
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def review_count(self, owner_id: str | None = None) -> int:
        with connection() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM reviews WHERE (? IS NULL OR owner_id = ?)", (owner_id, owner_id)).fetchone()
        return int(row["count"])

    def live_review_count(self, owner_id: str | None = None) -> int:
        with connection() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM reviews WHERE (? IS NULL OR owner_id = ?) "
                "AND COALESCE(json_extract(payload, '$.is_demo'), 0) = 0 "
                "AND COALESCE(json_extract(payload, '$.analysis_mode'), '') <> 'demo'",
                (owner_id, owner_id),
            ).fetchone()
        return int(row["count"])

    def live_pull_request_count(self, owner_id: str | None = None) -> int:
        with connection() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM ("
                "SELECT lower(json_extract(payload, '$.pull_request.repository_owner')) AS owner, "
                "lower(json_extract(payload, '$.pull_request.repository_name')) AS repository, "
                "json_extract(payload, '$.pull_request.pull_request_number') AS number "
                "FROM reviews WHERE (? IS NULL OR owner_id = ?) "
                "AND COALESCE(json_extract(payload, '$.is_demo'), 0) = 0 "
                "AND COALESCE(json_extract(payload, '$.analysis_mode'), '') <> 'demo' "
                "AND json_type(payload, '$.pull_request.repository_owner') = 'text' "
                "AND json_type(payload, '$.pull_request.repository_name') = 'text' "
                "AND json_type(payload, '$.pull_request.pull_request_number') IN ('integer', 'real') "
                "GROUP BY owner, repository, number)",
                (owner_id, owner_id),
            ).fetchone()
        return int(row["count"])

    def live_finding_count(self, owner_id: str | None = None) -> int:
        with connection() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN COALESCE(json_array_length(json_extract(payload, '$.canonical_issues')), 0) > 0 "
                "THEN json_array_length(json_extract(payload, '$.canonical_issues')) "
                "ELSE COALESCE(json_array_length(json_extract(payload, '$.findings')), 0) END), 0) AS count "
                "FROM reviews WHERE (? IS NULL OR owner_id = ?) "
                "AND COALESCE(json_extract(payload, '$.is_demo'), 0) = 0 "
                "AND COALESCE(json_extract(payload, '$.analysis_mode'), '') <> 'demo'",
                (owner_id, owner_id),
            ).fetchone()
        return int(row["count"])

    def save_pull_request(self, url: str, owner_id: str | None = None) -> dict[str, Any]:
        with connection() as db:
            row = db.execute("SELECT saved_id, url FROM saved_pull_requests WHERE url = ? AND (? IS NULL OR owner_id = ?)", (url, owner_id, owner_id)).fetchone()
            if row is None:
                saved_id = uuid4().hex
                db.execute("INSERT INTO saved_pull_requests(saved_id, url, owner_id) VALUES (?, ?, ?)", (saved_id, url, owner_id))
            else:
                saved_id = row["saved_id"]
        return {"saved_id": saved_id, "url": url, "temporary": False}

    def list_saved(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        with connection() as db:
            rows = db.execute("SELECT saved_id, url FROM saved_pull_requests WHERE (? IS NULL OR owner_id = ?) ORDER BY created_at DESC, rowid DESC", (owner_id, owner_id)).fetchall()
        return [{"saved_id": row["saved_id"], "url": row["url"], "temporary": False} for row in rows]

    def delete_saved(self, saved_id: str, owner_id: str | None = None) -> bool:
        with connection() as db:
            cursor = db.execute("DELETE FROM saved_pull_requests WHERE saved_id = ? AND (? IS NULL OR owner_id = ?)", (saved_id, owner_id, owner_id))
        return cursor.rowcount > 0

    def record_learning_attempt(self, exercise_id: str, category: str, correct: bool, owner_id: str | None = None) -> None:
        with connection() as db:
            db.execute("INSERT INTO learning_attempts(exercise_id, category, correct, owner_id) VALUES (?, ?, ?, ?)", (exercise_id, category, int(correct), owner_id))

    def learning_progress(self, owner_id: str | None = None) -> dict[str, Any]:
        with connection() as db:
            rows = db.execute("SELECT category, correct FROM learning_attempts WHERE (? IS NULL OR owner_id = ?)", (owner_id, owner_id)).fetchall()
            completed = db.execute("SELECT COUNT(DISTINCT exercise_id) AS count FROM learning_attempts WHERE (? IS NULL OR owner_id = ?)", (owner_id, owner_id)).fetchone()["count"]
        count = len(rows)
        correct = sum(row["correct"] for row in rows)
        topics = dict(Counter(row["category"] for row in rows))
        return {"temporary": False, "exercises_completed": completed, "correct_answers": correct, "attempt_count": count, "accuracy": correct / count if count else 0, "topics_practiced": topics}

    def learning_history(self, limit: int = 100, owner_id: str | None = None) -> dict[str, Any]:
        with connection() as db:
            rows = db.execute("SELECT exercise_id, category, correct FROM learning_attempts WHERE (? IS NULL OR owner_id = ?) ORDER BY attempt_id DESC LIMIT ?", (owner_id, owner_id, max(1, min(limit, 100)))).fetchall()
        return {"temporary": False, "attempts": [{"exercise_id": row["exercise_id"], "category": row["category"], "correct": bool(row["correct"])} for row in rows]}


def get_store() -> ReviewStore:
    """Return the configured storage adapter without silent backend fallback."""
    from app.config import storage_backend

    if storage_backend() == "mongodb":
        from app.database.mongo_connection import get_mongo_database
        from app.database.mongo_reviews import MongoReviewStore

        return MongoReviewStore(get_mongo_database(), ensure_indexes=False)
    return SQLiteReviewStore()
