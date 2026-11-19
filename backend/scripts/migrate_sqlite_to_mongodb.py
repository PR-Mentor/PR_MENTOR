"""Opt-in, repeatable import of an existing SQLite database into MongoDB."""

import argparse
import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from pymongo.database import Database

from app.config import database_path, storage_backend
from app.database.mongo_connection import close_mongo_client, get_mongo_database
from app.database.mongo_learning import create_learning_indexes
from app.database.mongo_reviews import MongoReviewStore, create_indexes

logger = logging.getLogger(__name__)


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    raise ValueError(f"Expected a datetime or ISO-8601 timestamp, got: {value!r}")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cursor = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def migrate_learning_lab(
    source: sqlite3.Connection,
    database: Database | Any,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Migrate the 5 Learning Lab collections with parent-child integrity validation and type transformations."""
    counts = {
        "learning_spaces": 0,
        "learning_concepts": 0,
        "learning_exercises": 0,
        "learning_drafts": 0,
        "learning_submissions": 0,
    }
    orphans = {
        "learning_concepts": 0,
        "learning_exercises": 0,
        "learning_drafts": 0,
        "learning_submissions": 0,
    }

    if not _table_exists(source, "learning_spaces"):
        return counts

    valid_space_ids: set[str] = set()
    valid_concept_ids: set[str] = set()
    valid_exercise_ids: set[str] = set()

    # 1. Spaces
    spaces_cursor = source.execute("SELECT * FROM learning_spaces")
    for row in spaces_cursor:
        space_id = str(row["space_id"])
        valid_space_ids.add(space_id)
        langs = []
        try:
            langs = json.loads(row["languages_json"] or "[]")
        except Exception:
            pass

        doc = {
            "space_id": space_id,
            "canonical_repo": str(row["canonical_repo"]),
            "pr_number": int(row["pr_number"]),
            "pr_title": str(row["pr_title"] or "Pull Request"),
            "pr_url": str(row["pr_url"]),
            "latest_review_id": str(row["latest_review_id"] or ""),
            "review_count": int(row["review_count"] or 1),
            "primary_language": str(row["primary_language"] or "python"),
            "languages": langs,
            "created_at": _utc_datetime(row["created_at"]),
            "updated_at": _utc_datetime(row["updated_at"]),
            "owner_id": row["owner_id"],
        }
        if not dry_run:
            database["learning_spaces"].replace_one({"space_id": space_id}, doc, upsert=True)
        counts["learning_spaces"] += 1

    # 2. Concepts
    if _table_exists(source, "learning_concepts"):
        concepts_cursor = source.execute("SELECT * FROM learning_concepts")
        for row in concepts_cursor:
            space_id = str(row["space_id"])
            concept_id = str(row["concept_id"])
            if space_id not in valid_space_ids:
                logger.warning("Skipping orphaned concept %s: space %s not found", concept_id, space_id)
                orphans["learning_concepts"] += 1
                continue

            valid_concept_ids.add(concept_id)
            issues = []
            exercises = []
            try:
                issues = json.loads(row["canonical_issue_ids_json"] or "[]")
                exercises = json.loads(row["exercise_ids_json"] or "[]")
            except Exception:
                pass

            doc = {
                "concept_id": concept_id,
                "space_id": space_id,
                "title": str(row["title"]),
                "summary": str(row["summary"] or ""),
                "why_it_matters": str(row["why_it_matters"] or ""),
                "category": str(row["category"] or "code_quality"),
                "language": str(row["language"] or "python"),
                "canonical_issue_ids": issues,
                "exercise_ids": exercises,
                "order_index": int(row["order_index"] or 0),
                "status": str(row["status"] or "not_started"),
                "owner_id": row["owner_id"],
            }
            if not dry_run:
                database["learning_concepts"].replace_one({"concept_id": concept_id}, doc, upsert=True)
            counts["learning_concepts"] += 1

    # 3. Exercises
    if _table_exists(source, "learning_exercises"):
        exercises_cursor = source.execute("SELECT * FROM learning_exercises")
        for row in exercises_cursor:
            space_id = str(row["space_id"])
            concept_id = str(row["concept_id"])
            exercise_id = str(row["exercise_id"])
            if space_id not in valid_space_ids or concept_id not in valid_concept_ids:
                logger.warning(
                    "Skipping orphaned exercise %s: space %s or concept %s not found",
                    exercise_id,
                    space_id,
                    concept_id,
                )
                orphans["learning_exercises"] += 1
                continue

            valid_exercise_ids.add(exercise_id)
            hints = []
            criteria = []
            rules = {}
            try:
                hints = json.loads(row["hints_json"] or "[]")
                criteria = json.loads(row["completion_criteria_json"] or "[]")
                rules = json.loads(row["validation_rules_json"] or "{}")
            except Exception:
                pass

            doc = {
                "exercise_id": exercise_id,
                "space_id": space_id,
                "concept_id": concept_id,
                "canonical_issue_id": row["canonical_issue_id"],
                "title": str(row["title"]),
                "description": str(row["description"] or ""),
                "instructions": str(row["instructions"] or ""),
                "language": str(row["language"]),
                "difficulty": str(row["difficulty"] or "beginner"),
                "category": str(row["category"] or "code_quality"),
                "starter_code": str(row["starter_code"] or ""),
                "buggy_code": row["buggy_code"],
                "expected_behavior": str(row["expected_behavior"] or ""),
                "hints": hints,
                "explanation": str(row["explanation"] or ""),
                "completion_criteria": criteria,
                "validation_rules": rules,
                "points": int(row["points"] or 10),
            }
            if not dry_run:
                database["learning_exercises"].replace_one({"exercise_id": exercise_id}, doc, upsert=True)
            counts["learning_exercises"] += 1

    # 4. Drafts
    if _table_exists(source, "learning_drafts"):
        drafts_cursor = source.execute("SELECT * FROM learning_drafts")
        for row in drafts_cursor:
            exercise_id = str(row["exercise_id"])
            draft_id = str(row["draft_id"])
            user_id = str(row["user_id"])
            if exercise_id not in valid_exercise_ids:
                logger.warning("Skipping orphaned draft %s: exercise %s not found", draft_id, exercise_id)
                orphans["learning_drafts"] += 1
                continue

            doc = {
                "draft_id": draft_id,
                "exercise_id": exercise_id,
                "user_id": user_id,
                "code": str(row["code"]),
                "updated_at": _utc_datetime(row["updated_at"]),
            }
            if not dry_run:
                database["learning_drafts"].replace_one(
                    {"exercise_id": exercise_id, "user_id": user_id}, doc, upsert=True
                )
            counts["learning_drafts"] += 1

    # 5. Submissions
    if _table_exists(source, "learning_submissions"):
        subs_cursor = source.execute("SELECT * FROM learning_submissions")
        for row in subs_cursor:
            exercise_id = str(row["exercise_id"])
            submission_id = str(row["submission_id"])
            user_id = str(row["user_id"])
            if exercise_id not in valid_exercise_ids:
                logger.warning("Skipping orphaned submission %s: exercise %s not found", submission_id, exercise_id)
                orphans["learning_submissions"] += 1
                continue

            eval_details = {}
            try:
                eval_details = json.loads(row["eval_details_json"] or "{}")
            except Exception:
                pass

            doc = {
                "submission_id": submission_id,
                "exercise_id": exercise_id,
                "user_id": user_id,
                "code": str(row["code"]),
                "status": str(row["status"]),
                "passed": bool(row["passed"]),
                "feedback": str(row["feedback"] or ""),
                "eval_details": eval_details,
                "submitted_at": _utc_datetime(row["submitted_at"]),
            }
            if not dry_run:
                database["learning_submissions"].replace_one({"submission_id": submission_id}, doc, upsert=True)
            counts["learning_submissions"] += 1

    if any(orphans.values()):
        logger.info("Orphaned records skipped: %s", orphans)

    return counts


def migrate(
    source_path: Path,
    store: MongoReviewStore | Database | Any,
    *,
    dry_run: bool = False,
    include_learning_lab: bool = False,
    reconcile: bool = True,
) -> dict[str, int]:
    """Copy persisted data from SQLite without changing or deleting its source."""
    source_path = source_path.expanduser().resolve(strict=True)
    uri = source_path.as_uri() + "?mode=ro"

    database: Database | Any
    review_store: MongoReviewStore
    if isinstance(store, MongoReviewStore):
        review_store = store
        database = store.database
    else:
        database = store
        review_store = MongoReviewStore(database, ensure_indexes=False)

    if not dry_run:
        create_indexes(database)
        create_learning_indexes(database)

    counts = {"reviews": 0, "saved_pull_requests": 0, "learning_attempts": 0}

    with closing(sqlite3.connect(uri, uri=True)) as source:
        source.row_factory = sqlite3.Row

        if _table_exists(source, "reviews"):
            for row in source.execute("SELECT payload FROM reviews"):
                review: dict[str, Any] = json.loads(row["payload"])
                if not dry_run:
                    review_store.save_review(review, "legacy:unassigned")
                counts["reviews"] += 1

        if _table_exists(source, "saved_pull_requests"):
            for row in source.execute("SELECT saved_id, url, created_at FROM saved_pull_requests"):
                if not dry_run:
                    review_store.import_saved_record(row["saved_id"], row["url"], row["created_at"])
                counts["saved_pull_requests"] += 1

        if _table_exists(source, "learning_attempts"):
            for row in source.execute(
                "SELECT attempt_id, exercise_id, category, correct, created_at FROM learning_attempts"
            ):
                if not dry_run:
                    review_store.import_learning_attempt(
                        row["attempt_id"],
                        row["exercise_id"],
                        row["category"],
                        bool(row["correct"]),
                        row["created_at"],
                    )
                counts["learning_attempts"] += 1

        if include_learning_lab:
            ll_counts = migrate_learning_lab(source, database, dry_run=dry_run)
            counts.update(ll_counts)

    if reconcile and not dry_run:
        for collection_name, expected_count in counts.items():
            actual_count = database[collection_name].count_documents({})
            if actual_count < expected_count:
                raise RuntimeError(
                    f"Reconciliation error for {collection_name}: expected at least {expected_count}, found {actual_count}"
                )

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=database_path(),
        help="SQLite file to read (default: PR_MENTOR_DATABASE_PATH)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform read-only validation without writing to MongoDB",
    )
    parser.add_argument(
        "--skip-learning-lab",
        action="store_true",
        help="Skip Learning Lab collections migration",
    )
    parser.add_argument(
        "--no-reconcile",
        action="store_true",
        help="Skip post-migration count reconciliation",
    )
    args = parser.parse_args()

    if storage_backend() != "mongodb":
        parser.error("Set PR_MENTOR_STORAGE=mongodb and configure MONGODB_URI before migrating.")

    try:
        database = get_mongo_database()
        store = MongoReviewStore(database, ensure_indexes=False)
        counts = migrate(
            args.source,
            store,
            dry_run=args.dry_run,
            include_learning_lab=not args.skip_learning_lab,
            reconcile=not args.no_reconcile,
        )
        mode_str = "[DRY-RUN] " if args.dry_run else ""
        print(f"{mode_str}SQLite import complete: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        return 0
    finally:
        close_mongo_client()


if __name__ == "__main__":
    raise SystemExit(main())
