"""Tests for SQLite to MongoDB migration script with Learning Lab support."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import mongomock
import pytest

from app.database.mongo_reviews import MongoReviewStore
from scripts.migrate_sqlite_to_mongodb import migrate, migrate_learning_lab


@pytest.fixture
def mock_target():
    client = mongomock.MongoClient(tz_aware=True)
    db = client["migration_target_test"]
    store = MongoReviewStore(db, ensure_indexes=False)
    return db, store


@pytest.fixture
def populated_sqlite_path(tmp_path):
    db_file = tmp_path / "test_source.sqlite3"
    with closing(sqlite3.connect(db_file)) as conn:
        conn.executescript(
            """
            CREATE TABLE reviews (
                review_id TEXT PRIMARY KEY,
                user_id TEXT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                owner_id TEXT
            );
            CREATE TABLE saved_pull_requests (
                saved_id TEXT PRIMARY KEY,
                user_id TEXT,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                owner_id TEXT
            );
            CREATE TABLE learning_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                exercise_id TEXT NOT NULL,
                category TEXT NOT NULL,
                correct INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT,
                owner_id TEXT
            );
            CREATE TABLE learning_spaces (
                space_id TEXT PRIMARY KEY,
                canonical_repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                pr_title TEXT,
                pr_url TEXT,
                latest_review_id TEXT,
                review_count INTEGER DEFAULT 1,
                primary_language TEXT,
                languages_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                owner_id TEXT
            );
            CREATE TABLE learning_concepts (
                concept_id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                why_it_matters TEXT,
                category TEXT,
                language TEXT,
                canonical_issue_ids_json TEXT,
                exercise_ids_json TEXT,
                order_index INTEGER DEFAULT 0,
                status TEXT DEFAULT 'not_started',
                owner_id TEXT
            );
            CREATE TABLE learning_exercises (
                exercise_id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                concept_id TEXT NOT NULL,
                canonical_issue_id TEXT,
                title TEXT NOT NULL,
                description TEXT,
                instructions TEXT,
                language TEXT NOT NULL,
                difficulty TEXT,
                category TEXT,
                starter_code TEXT,
                buggy_code TEXT,
                expected_behavior TEXT,
                hints_json TEXT,
                explanation TEXT,
                completion_criteria_json TEXT,
                validation_rules_json TEXT,
                points INTEGER DEFAULT 10
            );
            CREATE TABLE learning_drafts (
                draft_id TEXT PRIMARY KEY,
                exercise_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                code TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE learning_submissions (
                submission_id TEXT PRIMARY KEY,
                exercise_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                code TEXT NOT NULL,
                status TEXT NOT NULL,
                passed INTEGER NOT NULL,
                feedback TEXT,
                eval_details_json TEXT,
                submitted_at TEXT NOT NULL
            );
            """
        )

        # Seed data
        conn.execute(
            "INSERT INTO reviews VALUES ('r-1', 'u-1', '2026-11-10T10:00:00Z', '{\"review_id\": \"r-1\", \"created_at\": \"2026-11-10T10:00:00Z\", \"findings\": []}', 'u-1')"
        )
        conn.execute(
            "INSERT INTO saved_pull_requests VALUES ('s-1', 'u-1', 'https://github.com/org/repo/pull/1', '2026-11-10T10:00:00Z', 'u-1')"
        )
        conn.execute(
            "INSERT INTO learning_attempts VALUES (1, 'ex-1', 'security', 1, '2026-11-10T10:00:00Z', 'u-1', 'u-1')"
        )

        # Valid Learning Lab hierarchy
        conn.execute(
            """
            INSERT INTO learning_spaces VALUES (
                'space-valid-1', 'org/repo', 1, 'PR 1', 'https://github.com/org/repo/pull/1',
                'r-1', 1, 'python', '["python"]', '2026-11-10T10:00:00+00:00', '2026-11-10T10:00:00+00:00', 'u-1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_concepts VALUES (
                'concept-valid-1', 'space-valid-1', 'Buffer Safety', 'Summary', 'Why it matters',
                'security', 'c', '["issue-1"]', '[]', 1, 'not_started', 'u-1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_exercises VALUES (
                'exercise-valid-1', 'space-valid-1', 'concept-valid-1', 'issue-1',
                'Practice Exercise', 'Desc', 'Inst', 'c', 'beginner', 'security',
                'void f() {}', 'buggy()', 'expected', '["hint 1"]', 'explanation',
                '["crit 1"]', '{"forbidden": ["strcpy"]}', 10
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_drafts VALUES (
                'draft-1', 'exercise-valid-1', 'u-1', 'draft code', '2026-11-10T10:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_submissions VALUES (
                'sub-1', 'exercise-valid-1', 'u-1', 'submitted code', 'completed', 1,
                'Passed feedback', '{"check": "ok"}', '2026-11-10T10:00:00+00:00'
            )
            """
        )

        # Orphaned records (must be skipped during migration)
        conn.execute(
            """
            INSERT INTO learning_concepts VALUES (
                'concept-orphan-1', 'space-nonexistent', 'Orphan Concept', '', '',
                'security', 'c', '[]', '[]', 2, 'not_started', 'u-1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_exercises VALUES (
                'exercise-orphan-1', 'space-valid-1', 'concept-nonexistent', 'issue-2',
                'Orphan Ex', '', '', 'c', 'beginner', 'security', '', '', '', '[]', '', '[]', '{}', 10
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_drafts VALUES (
                'draft-orphan-1', 'exercise-nonexistent', 'u-1', 'code', '2026-11-10T10:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_submissions VALUES (
                'sub-orphan-1', 'exercise-nonexistent', 'u-1', 'code', 'failed', 0,
                '', '{}', '2026-11-10T10:00:00+00:00'
            )
            """
        )

        conn.commit()

    return db_file


def test_dry_run_does_not_modify_mongodb(mock_target, populated_sqlite_path):
    db, store = mock_target
    counts = migrate(
        populated_sqlite_path,
        store,
        dry_run=True,
        include_learning_lab=True,
        reconcile=False,
    )

    # Valid records were counted
    assert counts["reviews"] == 1
    assert counts["saved_pull_requests"] == 1
    assert counts["learning_attempts"] == 1
    assert counts["learning_spaces"] == 1
    assert counts["learning_concepts"] == 1  # 1 valid, 1 orphan skipped
    assert counts["learning_exercises"] == 1  # 1 valid, 1 orphan skipped
    assert counts["learning_drafts"] == 1  # 1 valid, 1 orphan skipped
    assert counts["learning_submissions"] == 1  # 1 valid, 1 orphan skipped

    # MongoDB collections remain completely empty
    for col in (
        "reviews",
        "saved_pull_requests",
        "learning_attempts",
        "learning_spaces",
        "learning_concepts",
        "learning_exercises",
        "learning_drafts",
        "learning_submissions",
    ):
        assert db[col].count_documents({}) == 0


def test_migration_transforms_types_and_skips_orphans(mock_target, populated_sqlite_path):
    db, store = mock_target
    counts = migrate(
        populated_sqlite_path,
        store,
        dry_run=False,
        include_learning_lab=True,
        reconcile=True,
    )

    assert counts["learning_spaces"] == 1
    assert counts["learning_concepts"] == 1
    assert counts["learning_exercises"] == 1
    assert counts["learning_drafts"] == 1
    assert counts["learning_submissions"] == 1

    # Check transformed document shapes in MongoDB
    space_doc = db["learning_spaces"].find_one({"space_id": "space-valid-1"})
    assert space_doc is not None
    assert isinstance(space_doc["languages"], list)
    assert space_doc["languages"] == ["python"]
    assert isinstance(space_doc["created_at"], datetime)
    assert isinstance(space_doc["updated_at"], datetime)

    concept_doc = db["learning_concepts"].find_one({"concept_id": "concept-valid-1"})
    assert concept_doc is not None
    assert isinstance(concept_doc["canonical_issue_ids"], list)
    assert concept_doc["canonical_issue_ids"] == ["issue-1"]

    exercise_doc = db["learning_exercises"].find_one({"exercise_id": "exercise-valid-1"})
    assert exercise_doc is not None
    assert isinstance(exercise_doc["hints"], list)
    assert isinstance(exercise_doc["completion_criteria"], list)
    assert isinstance(exercise_doc["validation_rules"], dict)
    assert exercise_doc["validation_rules"] == {"forbidden": ["strcpy"]}

    draft_doc = db["learning_drafts"].find_one({"draft_id": "draft-1"})
    assert draft_doc is not None
    assert isinstance(draft_doc["updated_at"], datetime)

    sub_doc = db["learning_submissions"].find_one({"submission_id": "sub-1"})
    assert sub_doc is not None
    assert isinstance(sub_doc["passed"], bool)
    assert sub_doc["passed"] is True
    assert isinstance(sub_doc["eval_details"], dict)
    assert sub_doc["eval_details"] == {"check": "ok"}
    assert isinstance(sub_doc["submitted_at"], datetime)

    # Verify orphans were NOT inserted into MongoDB
    assert db["learning_concepts"].find_one({"concept_id": "concept-orphan-1"}) is None
    assert db["learning_exercises"].find_one({"exercise_id": "exercise-orphan-1"}) is None
    assert db["learning_drafts"].find_one({"draft_id": "draft-orphan-1"}) is None
    assert db["learning_submissions"].find_one({"submission_id": "sub-orphan-1"}) is None


def test_migration_is_idempotent_on_rerun(mock_target, populated_sqlite_path):
    db, store = mock_target
    first = migrate(
        populated_sqlite_path,
        store,
        dry_run=False,
        include_learning_lab=True,
        reconcile=True,
    )
    second = migrate(
        populated_sqlite_path,
        store,
        dry_run=False,
        include_learning_lab=True,
        reconcile=True,
    )

    assert first == second
    # Collections do not have duplicated documents
    assert db["learning_spaces"].count_documents({}) == 1
    assert db["learning_concepts"].count_documents({}) == 1
    assert db["learning_exercises"].count_documents({}) == 1
    assert db["learning_drafts"].count_documents({}) == 1
    assert db["learning_submissions"].count_documents({}) == 1


def test_reconciliation_detects_count_deficit(mock_target, populated_sqlite_path, monkeypatch):
    db, store = mock_target

    # Simulate collection count mismatch
    original_replace = db["learning_submissions"].replace_one

    def fake_replace(*args, **kwargs):
        # Do not write to collection to induce count deficit
        pass

    monkeypatch.setattr(db["learning_submissions"], "replace_one", fake_replace)

    with pytest.raises(RuntimeError, match="Reconciliation error for learning_submissions"):
        migrate(
            populated_sqlite_path,
            store,
            dry_run=False,
            include_learning_lab=True,
            reconcile=True,
        )
