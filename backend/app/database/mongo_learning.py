"""MongoDB persistence for Learning Lab spaces, concepts, exercises, drafts, and submissions."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.schemas.learning_lab import (
    CodingExercise,
    ExerciseDraft,
    ExerciseSubmission,
    LearningConcept,
    LearningLabDashboard,
    PRLearningSpace,
)


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    raise ValueError("Expected a datetime or ISO-8601 timestamp.")


def _clean_doc(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    doc = dict(document)
    doc.pop("_id", None)
    return doc


def create_learning_indexes(database: Database | Any) -> None:
    """Create all required compound and unique indexes for Learning Lab collections."""
    database["learning_spaces"].create_index("space_id", unique=True, name="space_id_unique")
    database["learning_spaces"].create_index(
        [("canonical_repo", 1), ("pr_number", 1), ("owner_id", 1)],
        unique=True,
        name="canonical_repo_pr_owner_unique",
    )
    database["learning_spaces"].create_index([("owner_id", 1), ("updated_at", -1)], name="space_owner_updated_desc")
    database["learning_spaces"].create_index([("updated_at", -1)], name="space_updated_desc")

    database["learning_concepts"].create_index("concept_id", unique=True, name="concept_id_unique")
    database["learning_concepts"].create_index([("space_id", 1), ("order_index", 1)], name="concept_space_order_asc")
    database["learning_concepts"].create_index([("owner_id", 1), ("status", 1)], name="concept_owner_status")

    database["learning_exercises"].create_index("exercise_id", unique=True, name="exercise_id_unique")
    database["learning_exercises"].create_index([("space_id", 1), ("concept_id", 1)], name="exercise_space_concept")
    database["learning_exercises"].create_index("space_id", name="exercise_space_id")

    database["learning_drafts"].create_index("draft_id", unique=True, name="draft_id_unique")
    database["learning_drafts"].create_index(
        [("exercise_id", 1), ("user_id", 1)],
        unique=True,
        name="draft_exercise_user_unique",
    )
    database["learning_drafts"].create_index([("user_id", 1), ("updated_at", -1)], name="draft_user_updated_desc")

    database["learning_submissions"].create_index("submission_id", unique=True, name="submission_id_unique")
    database["learning_submissions"].create_index(
        [("exercise_id", 1), ("user_id", 1), ("submitted_at", -1)],
        name="submission_exercise_user_submitted_desc",
    )
    database["learning_submissions"].create_index([("user_id", 1), ("submitted_at", -1)], name="submission_user_submitted_desc")
    database["learning_submissions"].create_index([("user_id", 1), ("passed", 1)], name="submission_user_passed")


class MongoLearningStore:
    """MongoDB storage implementation for PR-connected Learning Lab entities."""

    def __init__(self, database: Database | Any, *, ensure_indexes: bool = True):
        self.database = database
        self.spaces: Collection = database["learning_spaces"]
        self.concepts: Collection = database["learning_concepts"]
        self.exercises: Collection = database["learning_exercises"]
        self.drafts: Collection = database["learning_drafts"]
        self.submissions: Collection = database["learning_submissions"]
        if ensure_indexes:
            create_learning_indexes(database)

    def ping(self) -> None:
        self.database.command("ping")

    def resolve_or_create_space_record(
        self,
        canonical_repo: str,
        pr_number: int,
        pr_title: str,
        pr_url: str,
        latest_review_id: str,
        primary_language: str,
        languages: list[str],
        owner_id: str | None = None,
    ) -> tuple[PRLearningSpace, bool]:
        """Look up existing space for canonical_repo + pr_number + owner_id or create one."""
        query = {
            "canonical_repo": canonical_repo,
            "pr_number": pr_number,
            "owner_id": owner_id,
        }
        existing = self.spaces.find_one(query)
        now = datetime.now(UTC)

        if existing:
            space_id = existing["space_id"]
            review_count = existing.get("review_count", 1) + 1
            self.spaces.update_one(
                {"space_id": space_id},
                {
                    "$set": {
                        "latest_review_id": latest_review_id,
                        "review_count": review_count,
                        "updated_at": now,
                    }
                },
            )
            doc = self.spaces.find_one({"space_id": space_id})
            return PRLearningSpace.model_validate(_clean_doc(doc)), False

        slug = re.sub(r"[^a-z0-9]+", "-", canonical_repo.lower()).strip("-")
        if owner_id:
            owner_slug = re.sub(r"[^a-z0-9]+", "-", owner_id.lower()).strip("-")
            space_id = f"space-{slug}-{pr_number}-{owner_slug[:12]}"
        else:
            space_id = f"space-{slug}-{pr_number}"

        doc = {
            "space_id": space_id,
            "canonical_repo": canonical_repo,
            "pr_number": pr_number,
            "pr_title": pr_title,
            "pr_url": pr_url,
            "latest_review_id": latest_review_id,
            "review_count": 1,
            "primary_language": primary_language,
            "languages": sorted(languages),
            "created_at": now,
            "updated_at": now,
            "owner_id": owner_id,
        }

        try:
            self.spaces.insert_one(doc)
            return PRLearningSpace.model_validate(_clean_doc(doc)), True
        except DuplicateKeyError:
            # Race condition: another request created the space concurrently
            existing = self.spaces.find_one(query)
            if existing:
                return PRLearningSpace.model_validate(_clean_doc(existing)), False
            raise

    def get_space(self, space_id: str, owner_id: str | None = None) -> PRLearningSpace | None:
        query: dict[str, Any] = {"space_id": space_id}
        if owner_id is not None:
            query["owner_id"] = owner_id
        doc = self.spaces.find_one(query)
        if not doc:
            return None
        return PRLearningSpace.model_validate(_clean_doc(doc))

    def list_spaces(self, owner_id: str | None = None) -> list[PRLearningSpace]:
        query: dict[str, Any] = {}
        if owner_id is not None:
            query["owner_id"] = owner_id
        cursor = self.spaces.find(query).sort("updated_at", -1)
        return [PRLearningSpace.model_validate(_clean_doc(d)) for d in cursor]

    def save_concepts(self, concepts: list[LearningConcept], owner_id: str | None = None) -> None:
        for concept in concepts:
            doc = concept.model_dump()
            doc["owner_id"] = owner_id
            self.concepts.update_one(
                {"concept_id": concept.concept_id},
                {"$setOnInsert": doc},
                upsert=True,
            )

    def list_concepts(self, space_id: str, owner_id: str | None = None) -> list[LearningConcept]:
        query: dict[str, Any] = {"space_id": space_id}
        if owner_id is not None:
            query["owner_id"] = owner_id
        cursor = self.concepts.find(query).sort("order_index", 1)
        return [LearningConcept.model_validate(_clean_doc(d)) for d in cursor]

    def get_concept(self, concept_id: str, owner_id: str | None = None) -> LearningConcept | None:
        query: dict[str, Any] = {"concept_id": concept_id}
        if owner_id is not None:
            query["owner_id"] = owner_id
        doc = self.concepts.find_one(query)
        if not doc:
            return None
        return LearningConcept.model_validate(_clean_doc(doc))

    def update_concept_status(
        self, concept_id: str, status: str, only_if_status: str | None = None
    ) -> None:
        query: dict[str, Any] = {"concept_id": concept_id}
        if only_if_status is not None:
            query["status"] = only_if_status
        self.concepts.update_one(query, {"$set": {"status": status}})

    def save_exercises(self, exercises: list[CodingExercise]) -> None:
        for exercise in exercises:
            doc = exercise.model_dump()
            self.exercises.update_one(
                {"exercise_id": exercise.exercise_id},
                {"$setOnInsert": doc},
                upsert=True,
            )

    def list_exercises(
        self, space_id: str, concept_id: str | None = None
    ) -> list[CodingExercise]:
        query: dict[str, Any] = {"space_id": space_id}
        if concept_id is not None:
            query["concept_id"] = concept_id
        cursor = self.exercises.find(query)
        return [CodingExercise.model_validate(_clean_doc(d)) for d in cursor]

    def get_exercise(self, exercise_id: str) -> CodingExercise | None:
        doc = self.exercises.find_one({"exercise_id": exercise_id})
        if not doc:
            return None
        return CodingExercise.model_validate(_clean_doc(doc))

    def save_draft(self, exercise_id: str, user_id: str, code: str) -> ExerciseDraft:
        draft_id = f"draft-{exercise_id}-{user_id}"
        now = datetime.now(UTC)
        doc = {
            "draft_id": draft_id,
            "exercise_id": exercise_id,
            "user_id": user_id,
            "code": code,
            "updated_at": now,
        }
        self.drafts.replace_one(
            {"exercise_id": exercise_id, "user_id": user_id},
            doc,
            upsert=True,
        )
        return ExerciseDraft(
            exercise_id=exercise_id, user_id=user_id, code=code, updated_at=now
        )

    def get_draft(self, exercise_id: str, user_id: str) -> ExerciseDraft | None:
        doc = self.drafts.find_one({"exercise_id": exercise_id, "user_id": user_id})
        if not doc:
            return None
        return ExerciseDraft.model_validate(_clean_doc(doc))

    def save_submission(self, submission: ExerciseSubmission) -> None:
        doc = submission.model_dump()
        doc["submitted_at"] = _utc_datetime(doc["submitted_at"])
        self.submissions.replace_one(
            {"submission_id": submission.submission_id},
            doc,
            upsert=True,
        )

    def list_submissions(
        self, exercise_id: str | None, user_id: str, limit: int = 50
    ) -> list[ExerciseSubmission]:
        query: dict[str, Any] = {"user_id": user_id}
        if exercise_id is not None:
            query["exercise_id"] = exercise_id
        cursor = self.submissions.find(query).sort([("submitted_at", -1), ("_id", -1)]).limit(limit)
        return [ExerciseSubmission.model_validate(_clean_doc(d)) for d in cursor]

    def get_dashboard(self, user_id: str | None = None) -> LearningLabDashboard:
        spaces = self.list_spaces(owner_id=user_id)
        if user_id is not None:
            space_ids = [s.space_id for s in spaces]
            concept_count = self.concepts.count_documents({"owner_id": user_id})
            mastered_count = self.concepts.count_documents({"owner_id": user_id, "status": "mastered"})
            exercise_count = self.exercises.count_documents({"space_id": {"$in": space_ids}})
            completed_subs = len(self.submissions.distinct("exercise_id", {"user_id": user_id, "passed": True}))
            recent_subs = self.list_submissions(exercise_id=None, user_id=user_id, limit=10)
        else:
            concept_count = self.concepts.count_documents({})
            mastered_count = self.concepts.count_documents({"status": "mastered"})
            exercise_count = self.exercises.count_documents({})
            completed_subs = 0
            recent_subs = []

        all_exercises: list[CodingExercise] = []
        for s in spaces[:5]:
            all_exercises.extend(self.list_exercises(s.space_id)[:2])

        return LearningLabDashboard(
            total_spaces=len(spaces),
            total_concepts=concept_count,
            mastered_concepts=mastered_count,
            total_exercises=exercise_count,
            completed_exercises=completed_subs,
            active_spaces=spaces[:10],
            recent_submissions=recent_subs,
            recommended_exercises=all_exercises[:6],
        )
