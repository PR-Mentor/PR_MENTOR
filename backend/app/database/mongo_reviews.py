"""MongoDB implementation of the stable review and learning storage interface."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: FURB162 - Support serialized UTC Z timestamps across Python versions.
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    raise ValueError("Expected a datetime or ISO-8601 timestamp.")


def _without_id(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    document.pop("_id", None)
    document.pop("owner_id", None)
    return document


def create_indexes(database: Database | Any) -> None:
    database["reviews"].create_index("review_id", unique=True, name="review_id_unique")
    database["reviews"].create_index([("created_at", -1)], name="review_created_at_desc")
    database["saved_pull_requests"].create_index("saved_id", unique=True, name="saved_id_unique")
    saved = database["saved_pull_requests"]
    try:
        saved.drop_index("saved_url_unique")
    except OperationFailure:
        pass
    saved.create_index([("owner_id", 1), ("url", 1)], unique=True, name="saved_owner_url_unique", partialFilterExpression={"owner_id": {"$type": "string"}})
    saved.create_index("url", unique=True, name="saved_legacy_url_unique", partialFilterExpression={"owner_id": {"$exists": False}})
    database["saved_pull_requests"].create_index([("created_at", -1)], name="saved_created_at_desc")
    database["learning_attempts"].create_index("attempt_id", unique=True, name="attempt_id_unique")
    database["learning_attempts"].create_index([("created_at", -1)], name="attempt_created_at_desc")
    database["learning_attempts"].create_index("exercise_id", name="attempt_exercise_id")
    database["auth_users"].create_index("email", unique=True, name="auth_email_unique")
    database["auth_sessions"].create_index("token_hash", unique=True, name="auth_session_token_unique")
    database["auth_sessions"].create_index("expires_at", expireAfterSeconds=0, name="auth_session_expiry")
    database["auth_rate_limits"].create_index("expires_at", expireAfterSeconds=0, name="auth_rate_limit_expiry")
    database["workspaces"].create_index("workspace_id", unique=True, name="workspace_id_unique")
    database["workspace_members"].create_index([("workspace_id", 1), ("user_id", 1)], unique=True, name="workspace_member_unique")
    database["workspace_members"].create_index([("user_id", 1), ("workspace_id", 1)], name="workspace_member_user_idx")
    database["workspace_invitations"].create_index("token_hash", unique=True, name="workspace_invite_token_unique")
    database["workspace_invitations"].create_index("expires_at", expireAfterSeconds=0, name="workspace_invite_expiry_idx")
    database["workspace_review_shares"].create_index([("workspace_id", 1), ("review_id", 1)], unique=True, name="workspace_review_share_unique")
    database["workspace_review_shares"].create_index([("review_id", 1), ("workspace_id", 1)], name="workspace_review_id_idx")
    database["benchmark_datasets"].create_index("dataset_id", unique=True, name="benchmark_dataset_id_unique")
    database["benchmark_datasets"].create_index([("owner_id", 1), ("updated_at", -1)], name="benchmark_dataset_owner_idx")
    database["benchmark_runs"].create_index("run_id", unique=True, name="benchmark_run_id_unique")
    database["benchmark_runs"].create_index([("owner_id", 1), ("dataset_id", 1), ("created_at", -1)], name="benchmark_run_owner_idx")
    database["review_jobs"].create_index("job_id", unique=True, name="review_job_id_unique")
    database["review_jobs"].create_index([("status", 1), ("available_at", 1), ("created_at", 1)], name="review_job_claim_idx")
    database["review_jobs"].create_index([("owner_id", 1), ("created_at", -1)], name="review_job_owner_idx")
    database["test_execution_jobs"].create_index("execution_id", unique=True, name="test_execution_id_unique")
    database["test_execution_jobs"].create_index([("owner_id", 1), ("idempotency_key", 1)], unique=True, name="test_execution_owner_idempotency_unique")
    database["test_execution_jobs"].create_index([("status", 1), ("created_at", 1)], name="test_execution_claim_idx")
    database["test_execution_jobs"].create_index([("owner_id", 1), ("created_at", -1)], name="test_execution_owner_idx")


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

    def save_review(self, review: dict[str, Any], owner_id: str | None = None) -> dict[str, Any]:
        document = dict(review)
        document["review_id"] = str(document["review_id"])
        document["created_at"] = _utc_datetime(document["created_at"])
        existing = self.reviews.find_one({"review_id": document["review_id"]}, {"owner_id": 1})
        stored_owner = existing.get("owner_id") if existing else None
        if stored_owner is not None:
            document["owner_id"] = stored_owner
        elif owner_id is not None:
            document["owner_id"] = owner_id
        self.reviews.replace_one({"review_id": document["review_id"]}, document, upsert=True)
        return review

    def get_review(self, review_id: str, owner_id: str | None = None) -> dict[str, Any] | None:
        query = {"review_id": review_id}
        if owner_id is not None:
            query["owner_id"] = owner_id
        return _without_id(self.reviews.find_one(query))

    def list_reviews(self, limit: int = 50, owner_id: str | None = None, *, include_demo: bool = True) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        query: dict[str, Any] = {"owner_id": owner_id} if owner_id is not None else {}
        if not include_demo:
            query["is_demo"] = {"$ne": True}
            query["analysis_mode"] = {"$ne": "demo"}
        cursor = self.reviews.find(query, {"_id": 0, "owner_id": 0}).sort([("created_at", -1), ("_id", -1)]).limit(bounded)
        return list(cursor)

    def review_count(self, owner_id: str | None = None) -> int:
        return self.reviews.count_documents({"owner_id": owner_id} if owner_id is not None else {})

    def live_review_count(self, owner_id: str | None = None) -> int:
        query: dict[str, Any] = {"is_demo": {"$ne": True}, "analysis_mode": {"$ne": "demo"}}
        if owner_id is not None:
            query["owner_id"] = owner_id
        return self.reviews.count_documents(query)

    def live_pull_request_count(self, owner_id: str | None = None) -> int:
        match: dict[str, Any] = {
            "is_demo": {"$ne": True},
            "analysis_mode": {"$ne": "demo"},
            "pull_request.repository_owner": {"$type": "string"},
            "pull_request.repository_name": {"$type": "string"},
            "pull_request.pull_request_number": {"$type": "number"},
        }
        if owner_id is not None:
            match["owner_id"] = owner_id
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

    def live_finding_count(self, owner_id: str | None = None) -> int:
        match: dict[str, Any] = {"is_demo": {"$ne": True}, "analysis_mode": {"$ne": "demo"}}
        if owner_id is not None:
            match["owner_id"] = owner_id
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

    def save_pull_request(self, url: str, owner_id: str | None = None) -> dict[str, Any]:
        lookup = {"url": url, "owner_id": owner_id} if owner_id is not None else {"url": url, "owner_id": {"$exists": False}}
        existing = self.saved.find_one(lookup, {"_id": 0})
        if existing is not None:
            return {"saved_id": existing["saved_id"], "url": existing["url"], "temporary": False}
        saved_id = uuid4().hex
        document = {"saved_id": saved_id, "url": url, "created_at": datetime.now(UTC)}
        if owner_id is not None:
            document["owner_id"] = owner_id
        try:
            self.saved.insert_one(document)
        except DuplicateKeyError:
            # A repeated URL is an idempotent save; a concurrent duplicate insert
            # is resolved by reading the existing canonical record below.
            pass
        existing = self.saved.find_one(lookup, {"_id": 0})
        if existing is None:
            raise RuntimeError("Saved pull request was not found after insert.")
        return {"saved_id": existing["saved_id"], "url": existing["url"], "temporary": False}

    def list_saved(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        cursor = self.saved.find({"owner_id": owner_id} if owner_id is not None else {}, {"_id": 0}).sort([("created_at", -1), ("_id", -1)])
        return [{"saved_id": item["saved_id"], "url": item["url"], "temporary": False} for item in cursor]

    def delete_saved(self, saved_id: str, owner_id: str | None = None) -> bool:
        query = {"saved_id": saved_id}
        if owner_id is not None:
            query["owner_id"] = owner_id
        return self.saved.delete_one(query).deleted_count > 0

    def record_learning_attempt(self, exercise_id: str, category: str, correct: bool, owner_id: str | None = None) -> None:
        item = {
            "attempt_id": uuid4().hex,
            "exercise_id": exercise_id,
            "category": category,
            "correct": bool(correct),
            "created_at": datetime.now(UTC),
        }
        if owner_id is not None:
            item["owner_id"] = owner_id
        self.attempts.insert_one(item)

    def learning_progress(self, owner_id: str | None = None) -> dict[str, Any]:
        query = {"owner_id": owner_id} if owner_id is not None else {}
        count = self.attempts.count_documents(query)
        correct = self.attempts.count_documents({**query, "correct": True})
        completed = len(self.attempts.distinct("exercise_id", query))
        topics = {
            item["_id"]: item["count"]
            for item in self.attempts.aggregate([
                {"$match": query},
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

    def learning_history(self, limit: int = 100, owner_id: str | None = None) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        cursor = self.attempts.find({"owner_id": owner_id} if owner_id is not None else {}, {"_id": 0, "exercise_id": 1, "category": 1, "correct": 1})
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
