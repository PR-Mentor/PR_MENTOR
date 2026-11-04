"""MongoDB implementation of the stable review and learning storage interface."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    raise ValueError("Expected a datetime or ISO-8601 timestamp.")


def _without_id(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    document.pop("_id", None)
    return document


def create_indexes(database: Database | Any) -> None:
    # Older deployments enforced URL uniqueness globally; ownership makes it per-user.
    try:
        database["saved_pull_requests"].drop_index("saved_url_unique")
    except Exception:
        pass
    database["reviews"].create_index("review_id", unique=True, name="review_id_unique")
    database["reviews"].create_index([("created_at", -1)], name="review_created_at_desc")
    database["saved_pull_requests"].create_index("saved_id", unique=True, name="saved_id_unique")
    database["saved_pull_requests"].create_index([("user_id", 1), ("url", 1)], unique=True, name="saved_user_url_unique")
    database["saved_pull_requests"].create_index([("created_at", -1)], name="saved_created_at_desc")
    database["learning_attempts"].create_index("attempt_id", unique=True, name="attempt_id_unique")
    database["learning_attempts"].create_index([("created_at", -1)], name="attempt_created_at_desc")
    database["learning_attempts"].create_index("exercise_id", name="attempt_exercise_id")


class MongoReviewStore:
    """Synchronous PyMongo adapter; FastAPI runs sync routes in its threadpool."""

    def __init__(self, database: Database | Any, *, ensure_indexes: bool = True):
        self.database = database
        self.reviews: Collection = database["reviews"]
        self.saved: Collection = database["saved_pull_requests"]
        self.attempts: Collection = database["learning_attempts"]
        # create_index is idempotent. Direct adapter use remains safe in tests;
        # the configured factory initializes indexes once per process/database.
        if ensure_indexes:
            create_indexes(database)

    def ping(self) -> None:
        self.database.command("ping")

    def save_review(self, review: dict[str, Any], user_id: str) -> dict[str, Any]:
        document = dict(review)
        document["review_id"] = str(document["review_id"])
        document["created_at"] = _utc_datetime(document["created_at"])
        document["user_id"] = user_id
        self.reviews.replace_one({"review_id": document["review_id"], "user_id": user_id}, document, upsert=True)
        return review

    def get_review(self, review_id: str, user_id: str) -> dict[str, Any] | None:
        return _without_id(self.reviews.find_one({"review_id": review_id, "user_id": user_id}))

    def list_reviews(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        cursor = self.reviews.find({"user_id": user_id}, {"_id": 0}).sort([("created_at", -1), ("_id", -1)]).limit(bounded)
        return list(cursor)

    def save_pull_request(self, url: str, user_id: str) -> dict[str, Any]:
        saved_id = uuid4().hex
        document = {"saved_id": saved_id, "url": url, "user_id": user_id, "created_at": datetime.now(timezone.utc)}
        try:
            self.saved.insert_one(document)
        except DuplicateKeyError:
            # A repeated URL is an idempotent save; a concurrent duplicate insert
            # is resolved by reading the existing canonical record below.
            pass
        existing = self.saved.find_one({"url": url, "user_id": user_id}, {"_id": 0})
        if existing is None:
            raise RuntimeError("Saved pull request was not found after insert.")
        return {"saved_id": existing["saved_id"], "url": existing["url"], "temporary": False}

    def list_saved(self, user_id: str) -> list[dict[str, Any]]:
        cursor = self.saved.find({"user_id": user_id}, {"_id": 0}).sort([("created_at", -1), ("_id", -1)])
        return [{"saved_id": item["saved_id"], "url": item["url"], "temporary": False} for item in cursor]

    def delete_saved(self, saved_id: str, user_id: str) -> bool:
        return self.saved.delete_one({"saved_id": saved_id, "user_id": user_id}).deleted_count > 0

    def record_learning_attempt(self, exercise_id: str, category: str, correct: bool) -> None:
        self.attempts.insert_one({
            "attempt_id": uuid4().hex,
            "exercise_id": exercise_id,
            "category": category,
            "correct": bool(correct),
            "created_at": datetime.now(timezone.utc),
        })

    def learning_progress(self) -> dict[str, Any]:
        count = self.attempts.count_documents({})
        correct = self.attempts.count_documents({"correct": True})
        completed = len(self.attempts.distinct("exercise_id"))
        topics = {
            item["_id"]: item["count"]
            for item in self.attempts.aggregate([
                {"$group": {"_id": "$category", "count": {"$sum": 1}}},
            ])
            if isinstance(item.get("_id"), str)
        }
        return {
            "temporary": False,
            "exercises_completed": completed,
            "correct_answers": correct,
            "attempt_count": count,
            "accuracy": correct / count if count else 0,
            "topics_practiced": topics,
        }

    def learning_history(self, limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        cursor = self.attempts.find({}, {"_id": 0, "exercise_id": 1, "category": 1, "correct": 1})
        cursor = cursor.sort([("created_at", -1), ("_id", -1)]).limit(bounded)
        return {
            "temporary": False,
            "attempts": [
                {"exercise_id": item["exercise_id"], "category": item["category"], "correct": bool(item["correct"])}
                for item in cursor
            ],
        }

    def import_saved_record(self, saved_id: str, url: str, created_at: Any) -> None:
        self.saved.update_one(
            {"url": url},
            {"$setOnInsert": {"saved_id": saved_id, "url": url, "created_at": _utc_datetime(created_at)}},
            upsert=True,
        )

    def import_learning_attempt(self, attempt_id: int, exercise_id: str, category: str, correct: bool, created_at: Any) -> None:
        stable_id = f"sqlite:{attempt_id}"
        self.attempts.update_one(
            {"attempt_id": stable_id},
            {"$setOnInsert": {
                "attempt_id": stable_id,
                "exercise_id": exercise_id,
                "category": category,
                "correct": bool(correct),
                "created_at": _utc_datetime(created_at),
            }},
            upsert=True,
        )
