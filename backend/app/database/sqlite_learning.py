"""SQLite persistence for Learning Lab spaces, concepts, exercises, drafts, and submissions."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from app.database.connection import connection
from app.schemas.learning_lab import (
    CodingExercise,
    ExerciseDraft,
    ExerciseSubmission,
    LearningConcept,
    LearningLabDashboard,
    PRLearningSpace,
)


class LearningStore(Protocol):
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
    ) -> tuple[PRLearningSpace, bool]: ...
    def get_space(self, space_id: str, owner_id: str | None = None) -> PRLearningSpace | None: ...
    def list_spaces(self, owner_id: str | None = None) -> list[PRLearningSpace]: ...
    def save_concepts(self, concepts: list[LearningConcept], owner_id: str | None = None) -> None: ...
    def list_concepts(self, space_id: str, owner_id: str | None = None) -> list[LearningConcept]: ...
    def get_concept(self, concept_id: str, owner_id: str | None = None) -> LearningConcept | None: ...
    def update_concept_status(self, concept_id: str, status: str, only_if_status: str | None = None) -> None: ...
    def save_exercises(self, exercises: list[CodingExercise]) -> None: ...
    def list_exercises(self, space_id: str, concept_id: str | None = None) -> list[CodingExercise]: ...
    def get_exercise(self, exercise_id: str) -> CodingExercise | None: ...
    def save_draft(self, exercise_id: str, user_id: str, code: str) -> ExerciseDraft: ...
    def get_draft(self, exercise_id: str, user_id: str) -> ExerciseDraft | None: ...
    def save_submission(self, submission: ExerciseSubmission) -> None: ...
    def list_submissions(self, exercise_id: str | None, user_id: str, limit: int = 50) -> list[ExerciseSubmission]: ...
    def get_dashboard(self, user_id: str | None = None) -> LearningLabDashboard: ...
    def ping(self) -> None: ...


class SQLiteLearningStore:
    """SQLite implementation of Learning Lab persistence."""

    def ping(self) -> None:
        from app.database.connection import ping

        ping()

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
        now_str = datetime.now(UTC).isoformat()
        is_new = False
        with connection() as db:
            row = db.execute(
                "SELECT * FROM learning_spaces WHERE canonical_repo = ? AND pr_number = ? AND ((owner_id IS NULL AND ? IS NULL) OR owner_id = ?)",
                (canonical_repo, pr_number, owner_id, owner_id),
            ).fetchone()

            if row:
                space_id = row["space_id"]
                review_count = row["review_count"] + 1
                db.execute(
                    """
                    UPDATE learning_spaces
                    SET latest_review_id = ?, review_count = ?, updated_at = ?
                    WHERE space_id = ?
                    """,
                    (latest_review_id, review_count, now_str, space_id),
                )
            else:
                is_new = True
                slug = re.sub(r"[^a-z0-9]+", "-", canonical_repo.lower()).strip("-")
                if owner_id:
                    owner_slug = re.sub(r"[^a-z0-9]+", "-", owner_id.lower()).strip("-")
                    space_id = f"space-{slug}-{pr_number}-{owner_slug[:12]}"
                else:
                    space_id = f"space-{slug}-{pr_number}"

                db.execute(
                    """
                    INSERT OR REPLACE INTO learning_spaces
                    (space_id, canonical_repo, pr_number, pr_title, pr_url, latest_review_id, review_count,
                     primary_language, languages_json, created_at, updated_at, owner_id)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        space_id,
                        canonical_repo,
                        pr_number,
                        pr_title,
                        pr_url,
                        latest_review_id,
                        primary_language,
                        json.dumps(sorted(languages)),
                        now_str,
                        now_str,
                        owner_id,
                    ),
                )

        space = self.get_space(space_id, owner_id)
        assert space is not None
        return space, is_new

    def get_space(self, space_id: str, owner_id: str | None = None) -> PRLearningSpace | None:
        with connection() as db:
            row = db.execute(
                "SELECT * FROM learning_spaces WHERE space_id = ? AND (? IS NULL OR owner_id = ?)",
                (space_id, owner_id, owner_id),
            ).fetchone()
        if not row:
            return None
        langs: list[str] = []
        try:
            langs = json.loads(row["languages_json"] or "[]")
        except Exception:
            pass
        return PRLearningSpace(
            space_id=row["space_id"],
            canonical_repo=row["canonical_repo"],
            pr_number=row["pr_number"],
            pr_title=row["pr_title"] or "Pull Request",
            pr_url=row["pr_url"],
            latest_review_id=row["latest_review_id"] or "",
            review_count=row["review_count"],
            primary_language=row["primary_language"] or "python",
            languages=langs,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_spaces(self, owner_id: str | None = None) -> list[PRLearningSpace]:
        with connection() as db:
            rows = db.execute(
                "SELECT * FROM learning_spaces WHERE (? IS NULL OR owner_id = ?) ORDER BY updated_at DESC",
                (owner_id, owner_id),
            ).fetchall()
        result: list[PRLearningSpace] = []
        for r in rows:
            langs: list[str] = []
            try:
                langs = json.loads(r["languages_json"] or "[]")
            except Exception:
                pass
            result.append(
                PRLearningSpace(
                    space_id=r["space_id"],
                    canonical_repo=r["canonical_repo"],
                    pr_number=r["pr_number"],
                    pr_title=r["pr_title"] or "Pull Request",
                    pr_url=r["pr_url"],
                    latest_review_id=r["latest_review_id"] or "",
                    review_count=r["review_count"],
                    primary_language=r["primary_language"] or "python",
                    languages=langs,
                    created_at=datetime.fromisoformat(r["created_at"]),
                    updated_at=datetime.fromisoformat(r["updated_at"]),
                )
            )
        return result

    def save_concepts(self, concepts: list[LearningConcept], owner_id: str | None = None) -> None:
        with connection() as db:
            for concept in concepts:
                db.execute(
                    """
                    INSERT OR IGNORE INTO learning_concepts
                    (concept_id, space_id, title, summary, why_it_matters, category, language,
                     canonical_issue_ids_json, exercise_ids_json, order_index, status, owner_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        concept.concept_id,
                        concept.space_id,
                        concept.title,
                        concept.summary,
                        concept.why_it_matters,
                        concept.category,
                        concept.language,
                        json.dumps(concept.canonical_issue_ids),
                        json.dumps(concept.exercise_ids),
                        concept.order_index,
                        concept.status,
                        owner_id,
                    ),
                )

    def list_concepts(self, space_id: str, owner_id: str | None = None) -> list[LearningConcept]:
        with connection() as db:
            rows = db.execute(
                "SELECT * FROM learning_concepts WHERE space_id = ? AND (? IS NULL OR owner_id = ?) ORDER BY order_index ASC",
                (space_id, owner_id, owner_id),
            ).fetchall()
        result: list[LearningConcept] = []
        for r in rows:
            issues: list[str] = []
            exercises: list[str] = []
            try:
                issues = json.loads(r["canonical_issue_ids_json"] or "[]")
                exercises = json.loads(r["exercise_ids_json"] or "[]")
            except Exception:
                pass
            result.append(
                LearningConcept(
                    concept_id=r["concept_id"],
                    space_id=r["space_id"],
                    title=r["title"],
                    summary=r["summary"] or "",
                    why_it_matters=r["why_it_matters"] or "",
                    category=r["category"] or "code_quality",
                    language=r["language"] or "python",
                    canonical_issue_ids=issues,
                    exercise_ids=exercises,
                    order_index=r["order_index"],
                    status=r["status"] or "not_started",
                )
            )
        return result

    def get_concept(self, concept_id: str, owner_id: str | None = None) -> LearningConcept | None:
        with connection() as db:
            row = db.execute(
                "SELECT * FROM learning_concepts WHERE concept_id = ? AND (? IS NULL OR owner_id = ?)",
                (concept_id, owner_id, owner_id),
            ).fetchone()
        if not row:
            return None
        issues: list[str] = []
        exercises: list[str] = []
        try:
            issues = json.loads(row["canonical_issue_ids_json"] or "[]")
            exercises = json.loads(row["exercise_ids_json"] or "[]")
        except Exception:
            pass
        return LearningConcept(
            concept_id=row["concept_id"],
            space_id=row["space_id"],
            title=row["title"],
            summary=row["summary"] or "",
            why_it_matters=row["why_it_matters"] or "",
            category=row["category"] or "code_quality",
            language=row["language"] or "python",
            canonical_issue_ids=issues,
            exercise_ids=exercises,
            order_index=row["order_index"],
            status=row["status"] or "not_started",
        )

    def update_concept_status(
        self, concept_id: str, status: str, only_if_status: str | None = None
    ) -> None:
        with connection() as db:
            if only_if_status is not None:
                db.execute(
                    "UPDATE learning_concepts SET status = ? WHERE concept_id = ? AND status = ?",
                    (status, concept_id, only_if_status),
                )
            else:
                db.execute(
                    "UPDATE learning_concepts SET status = ? WHERE concept_id = ?",
                    (status, concept_id),
                )

    def save_exercises(self, exercises: list[CodingExercise]) -> None:
        with connection() as db:
            for exercise in exercises:
                db.execute(
                    """
                    INSERT OR IGNORE INTO learning_exercises
                    (exercise_id, space_id, concept_id, canonical_issue_id, title, description,
                     instructions, language, difficulty, category, starter_code, buggy_code,
                     expected_behavior, hints_json, explanation, completion_criteria_json,
                     validation_rules_json, points)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        exercise.exercise_id,
                        exercise.space_id,
                        exercise.concept_id,
                        exercise.canonical_issue_id,
                        exercise.title,
                        exercise.description,
                        exercise.instructions,
                        exercise.language,
                        exercise.difficulty,
                        exercise.category,
                        exercise.starter_code,
                        exercise.buggy_code,
                        exercise.expected_behavior,
                        json.dumps(exercise.hints),
                        exercise.explanation,
                        json.dumps(exercise.completion_criteria),
                        json.dumps(exercise.validation_rules),
                        exercise.points,
                    ),
                )

    def list_exercises(
        self, space_id: str, concept_id: str | None = None
    ) -> list[CodingExercise]:
        with connection() as db:
            if concept_id:
                rows = db.execute(
                    "SELECT * FROM learning_exercises WHERE space_id = ? AND concept_id = ?",
                    (space_id, concept_id),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM learning_exercises WHERE space_id = ?",
                    (space_id,),
                ).fetchall()
        result: list[CodingExercise] = []
        for r in rows:
            hints: list[str] = []
            criteria: list[str] = []
            rules: dict[str, Any] = {}
            try:
                hints = json.loads(r["hints_json"] or "[]")
                criteria = json.loads(r["completion_criteria_json"] or "[]")
                rules = json.loads(r["validation_rules_json"] or "{}")
            except Exception:
                pass
            result.append(
                CodingExercise(
                    exercise_id=r["exercise_id"],
                    space_id=r["space_id"],
                    concept_id=r["concept_id"],
                    canonical_issue_id=r["canonical_issue_id"],
                    title=r["title"],
                    description=r["description"] or "",
                    instructions=r["instructions"] or "",
                    language=r["language"],
                    difficulty=r["difficulty"] or "beginner",
                    category=r["category"] or "code_quality",
                    starter_code=r["starter_code"] or "",
                    buggy_code=r["buggy_code"],
                    expected_behavior=r["expected_behavior"] or "",
                    hints=hints,
                    explanation=r["explanation"] or "",
                    completion_criteria=criteria,
                    validation_rules=rules,
                    points=r["points"] or 10,
                )
            )
        return result

    def get_exercise(self, exercise_id: str) -> CodingExercise | None:
        with connection() as db:
            row = db.execute(
                "SELECT * FROM learning_exercises WHERE exercise_id = ?",
                (exercise_id,),
            ).fetchone()
        if not row:
            return None
        hints: list[str] = []
        criteria: list[str] = []
        rules: dict[str, Any] = {}
        try:
            hints = json.loads(row["hints_json"] or "[]")
            criteria = json.loads(row["completion_criteria_json"] or "[]")
            rules = json.loads(row["validation_rules_json"] or "{}")
        except Exception:
            pass
        return CodingExercise(
            exercise_id=row["exercise_id"],
            space_id=row["space_id"],
            concept_id=row["concept_id"],
            canonical_issue_id=row["canonical_issue_id"],
            title=row["title"],
            description=row["description"] or "",
            instructions=row["instructions"] or "",
            language=row["language"],
            difficulty=row["difficulty"] or "beginner",
            category=row["category"] or "code_quality",
            starter_code=row["starter_code"] or "",
            buggy_code=row["buggy_code"],
            expected_behavior=row["expected_behavior"] or "",
            hints=hints,
            explanation=row["explanation"] or "",
            completion_criteria=criteria,
            validation_rules=rules,
            points=row["points"] or 10,
        )

    def save_draft(self, exercise_id: str, user_id: str, code: str) -> ExerciseDraft:
        draft_id = f"draft-{exercise_id}-{user_id}"
        now_str = datetime.now(UTC).isoformat()
        with connection() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO learning_drafts (draft_id, exercise_id, user_id, code, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (draft_id, exercise_id, user_id, code, now_str),
            )
        return ExerciseDraft(
            exercise_id=exercise_id,
            user_id=user_id,
            code=code,
            updated_at=datetime.fromisoformat(now_str),
        )

    def get_draft(self, exercise_id: str, user_id: str) -> ExerciseDraft | None:
        with connection() as db:
            row = db.execute(
                "SELECT * FROM learning_drafts WHERE exercise_id = ? AND user_id = ?",
                (exercise_id, user_id),
            ).fetchone()
        if not row:
            return None
        return ExerciseDraft(
            exercise_id=row["exercise_id"],
            user_id=row["user_id"],
            code=row["code"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_submission(self, submission: ExerciseSubmission) -> None:
        now_str = submission.submitted_at.isoformat()
        with connection() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO learning_submissions
                (submission_id, exercise_id, user_id, code, status, passed, feedback, eval_details_json, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission.submission_id,
                    submission.exercise_id,
                    submission.user_id,
                    submission.code,
                    submission.status,
                    1 if submission.passed else 0,
                    submission.feedback,
                    json.dumps(submission.eval_details),
                    now_str,
                ),
            )

    def list_submissions(
        self, exercise_id: str | None, user_id: str, limit: int = 50
    ) -> list[ExerciseSubmission]:
        with connection() as db:
            if exercise_id:
                rows = db.execute(
                    "SELECT * FROM learning_submissions WHERE exercise_id = ? AND user_id = ? ORDER BY submitted_at DESC",
                    (exercise_id, user_id),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM learning_submissions WHERE user_id = ? ORDER BY submitted_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
        result: list[ExerciseSubmission] = []
        for r in rows:
            details: dict[str, Any] = {}
            try:
                details = json.loads(r["eval_details_json"] or "{}")
            except Exception:
                pass
            result.append(
                ExerciseSubmission(
                    submission_id=r["submission_id"],
                    exercise_id=r["exercise_id"],
                    user_id=r["user_id"],
                    code=r["code"],
                    status=r["status"],
                    passed=bool(r["passed"]),
                    feedback=r["feedback"] or "",
                    eval_details=details,
                    submitted_at=datetime.fromisoformat(r["submitted_at"]),
                )
            )
        return result

    def get_dashboard(self, user_id: str | None = None) -> LearningLabDashboard:
        spaces = self.list_spaces(owner_id=user_id)
        with connection() as db:
            if user_id is not None:
                concept_count = db.execute(
                    "SELECT COUNT(*) AS c FROM learning_concepts WHERE owner_id = ?",
                    (user_id,),
                ).fetchone()["c"]
                mastered_count = db.execute(
                    "SELECT COUNT(*) AS c FROM learning_concepts WHERE owner_id = ? AND status = 'mastered'",
                    (user_id,),
                ).fetchone()["c"]
                exercise_count = db.execute(
                    """
                    SELECT COUNT(*) AS c FROM learning_exercises
                    WHERE space_id IN (SELECT space_id FROM learning_spaces WHERE owner_id = ?)
                    """,
                    (user_id,),
                ).fetchone()["c"]
                completed_subs = db.execute(
                    "SELECT COUNT(DISTINCT exercise_id) AS c FROM learning_submissions WHERE user_id = ? AND passed = 1",
                    (user_id,),
                ).fetchone()["c"]
                recent_subs = self.list_submissions(None, user_id, limit=10)
            else:
                concept_count = db.execute("SELECT COUNT(*) AS c FROM learning_concepts").fetchone()["c"]
                mastered_count = db.execute(
                    "SELECT COUNT(*) AS c FROM learning_concepts WHERE status = 'mastered'"
                ).fetchone()["c"]
                exercise_count = db.execute("SELECT COUNT(*) AS c FROM learning_exercises").fetchone()["c"]
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


def get_learning_store(database: Any | None = None) -> LearningStore:
    """Return the configured Learning Lab storage adapter without silent fallback."""
    from app.config import storage_backend

    if storage_backend() == "mongodb":
        if database is not None:
            from app.database.mongo_learning import MongoLearningStore

            return MongoLearningStore(database, ensure_indexes=False)
        from app.database.mongo_connection import get_mongo_database
        from app.database.mongo_learning import MongoLearningStore

        return MongoLearningStore(get_mongo_database(), ensure_indexes=False)
    return SQLiteLearningStore()
