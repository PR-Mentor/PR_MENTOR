"""MongoDB implementation of the stable review and learning storage interface."""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure


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
    document.pop("user_id", None)
    document.pop("owner_id", None)
    return document


def create_indexes(database: Database | Any) -> None:
    # Older deployments enforced URL uniqueness globally; ownership makes it per-user.
    try:
        database["saved_pull_requests"].drop_index("saved_url_unique")
    except Exception:
        pass
    database["reviews"].create_index("review_id", unique=True, name="review_id_unique")
    database["reviews"].create_index([("created_at", -1)], name="review_created_at_desc")
    database["reviews"].create_index([("user_id", 1), ("created_at", -1)], name="review_user_created_desc")
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
        if ensure_indexes:
            create_indexes(database)

    def ping(self) -> None:
        self.database.command("ping")

    def save_review(
        self,
        review: dict[str, Any],
        user_id: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        document = dict(review)
        effective_id = user_id or owner_id or review.get("user_id") or review.get("owner_id")
        review_id = str(document["review_id"])
        document["review_id"] = review_id
        document["created_at"] = _utc_datetime(document["created_at"])
        # Remove ownership fields from the payload so they are managed separately.
        document.pop("user_id", None)
        document.pop("owner_id", None)

        # Use update_one with $set for payload fields and $setOnInsert for
        # ownership so that re-saves by a workspace editor don't overwrite the
        # original owner.
        update: dict[str, Any] = {"$set": document}
        if effective_id is not None:
            update["$setOnInsert"] = {"user_id": effective_id, "owner_id": effective_id}
        self.reviews.update_one(
            {"review_id": review_id},
            update,
            upsert=True,
        )
        return review

    def get_review(
        self,
        review_id: str,
        user_id: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> dict[str, Any] | None:
        effective_id = user_id or owner_id
        if effective_id is not None:
            query = {
                "review_id": review_id,
                "$or": [
                    {"user_id": effective_id},
                    {"owner_id": effective_id},
                    {"user_id": None, "owner_id": None},
                ],
            }
        else:
            query = {"review_id": review_id}
        doc = self.reviews.find_one(query)
        if doc is None and effective_id is not None:
            from app.database import workspaces

            role = workspaces.review_role(effective_id, review_id)
            if role is not None:
                doc = self.reviews.find_one({"review_id": review_id})
        return _without_id(doc)

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

        query: dict[str, Any] = {}
        if effective_id is not None:
            query["$or"] = [{"user_id": effective_id}, {"owner_id": effective_id}]
        if not include_demo:
            query["is_demo"] = {"$ne": True}
            query["analysis_mode"] = {"$ne": "demo"}

        cursor = self.reviews.find(query, {"_id": 0, "user_id": 0, "owner_id": 0}).sort(
            [("created_at", -1), ("_id", -1)]
        ).limit(bounded)
        return list(cursor)

    def review_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int:
        effective_id = user_id or owner_id
        query = {"$or": [{"user_id": effective_id}, {"owner_id": effective_id}]} if effective_id is not None else {}
        return self.reviews.count_documents(query)

    def live_review_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int:
        effective_id = user_id or owner_id
        query: dict[str, Any] = {"is_demo": {"$ne": True}, "analysis_mode": {"$ne": "demo"}}
        if effective_id is not None:
            query["$or"] = [{"user_id": effective_id}, {"owner_id": effective_id}]
        return self.reviews.count_documents(query)

    def live_pull_request_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int:
        effective_id = user_id or owner_id
        match: dict[str, Any] = {
            "is_demo": {"$ne": True},
            "analysis_mode": {"$ne": "demo"},
            "pull_request.repository_owner": {"$type": "string"},
            "pull_request.repository_name": {"$type": "string"},
            "pull_request.pull_request_number": {"$type": "number"},
        }
        if effective_id is not None:
            match["$or"] = [{"user_id": effective_id}, {"owner_id": effective_id}]
        rows = self.reviews.aggregate([
            {"$match": match},
            {"$group": {"_id": {
                "owner": {"$toLower": "$pull_request.repository_owner"},
                "repository": {"$toLower": "$pull_request.repository_name"},
                "number": "$pull_request.pull_request_number",
            }}},
            {"$count": "count"},
        ])
        result = next(iter(rows), None)
        return int(result["count"]) if result else 0

    def live_finding_count(self, user_id: str | None = None, *, owner_id: str | None = None) -> int:
        effective_id = user_id or owner_id
        match: dict[str, Any] = {"is_demo": {"$ne": True}, "analysis_mode": {"$ne": "demo"}}
        if effective_id is not None:
            match["$or"] = [{"user_id": effective_id}, {"owner_id": effective_id}]
        rows = self.reviews.aggregate([
            {"$match": match},
            {"$project": {
                "canonical_count": {"$cond": [
                    {"$isArray": "$canonical_issues"}, {"$size": "$canonical_issues"}, 0,
                ]},
                "finding_count": {"$cond": [
                    {"$isArray": "$findings"}, {"$size": "$findings"}, 0,
                ]},
            }},
            {"$group": {"_id": None, "count": {"$sum": {"$cond": [
                {"$gt": ["$canonical_count", 0]}, "$canonical_count", "$finding_count",
            ]}}}},
        ])
        result = next(iter(rows), None)
        return int(result["count"]) if result else 0

    def save_pull_request(
        self,
        url: str,
        user_id: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        effective_id = user_id or owner_id
        lookup: dict[str, Any] = {"url": url}
        if effective_id is not None:
            lookup["$or"] = [{"user_id": effective_id}, {"owner_id": effective_id}]
        existing = self.saved.find_one(lookup, {"_id": 0})
        if existing is not None:
            return {"saved_id": existing["saved_id"], "url": existing["url"], "temporary": False}

        saved_id = uuid4().hex
        document: dict[str, Any] = {
            "saved_id": saved_id,
            "url": url,
            "created_at": datetime.now(timezone.utc),
        }
        if effective_id is not None:
            document["user_id"] = effective_id
            document["owner_id"] = effective_id
        try:
            self.saved.insert_one(document)
        except DuplicateKeyError:
            pass

        existing = self.saved.find_one(lookup, {"_id": 0})
        if existing is None:
            raise RuntimeError("Saved pull request was not found after insert.")
        return {"saved_id": existing["saved_id"], "url": existing["url"], "temporary": False}

    def list_saved(
        self,
        user_id: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        effective_id = user_id or owner_id
        query = {"$or": [{"user_id": effective_id}, {"owner_id": effective_id}]} if effective_id is not None else {}
        cursor = self.saved.find(query, {"_id": 0, "user_id": 0, "owner_id": 0}).sort([("created_at", -1), ("_id", -1)])
        return [{"saved_id": item["saved_id"], "url": item["url"], "temporary": False} for item in cursor]

    def delete_saved(
        self,
        saved_id: str,
        user_id: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> bool:
        effective_id = user_id or owner_id
        query: dict[str, Any] = {"saved_id": saved_id}
        if effective_id is not None:
            query["$or"] = [{"user_id": effective_id}, {"owner_id": effective_id}]
        return self.saved.delete_one(query).deleted_count > 0

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
        item: dict[str, Any] = {
            "attempt_id": uuid4().hex,
            "exercise_id": exercise_id,
            "category": category,
            "correct": bool(correct),
            "created_at": datetime.now(timezone.utc),
        }
        if effective_id is not None:
            item["user_id"] = effective_id
            item["owner_id"] = effective_id
        self.attempts.insert_one(item)

    def learning_progress(self, user_id: str | None = None, *, owner_id: str | None = None) -> dict[str, Any]:
        effective_id = user_id or owner_id
        match = {"$or": [{"user_id": effective_id}, {"owner_id": effective_id}]} if effective_id is not None else {}
        count = self.attempts.count_documents(match)
        correct = self.attempts.count_documents({**match, "correct": True})
        completed = len(self.attempts.distinct("exercise_id", match))
        topics = {
            item["_id"]: item["count"]
            for item in self.attempts.aggregate([
                {"$match": match},
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

    def learning_history(
        self,
        limit: int = 100,
        user_id: str | None = None,
        *,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        effective_id = user_id or owner_id
        bounded = max(1, min(int(limit), 100))
        match = {"$or": [{"user_id": effective_id}, {"owner_id": effective_id}]} if effective_id is not None else {}
        cursor = self.attempts.find(match, {"_id": 0, "exercise_id": 1, "category": 1, "correct": 1})
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

    def import_learning_attempt(
        self,
        attempt_id: int,
        exercise_id: str,
        category: str,
        correct: bool,
        created_at: Any,
    ) -> None:
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
