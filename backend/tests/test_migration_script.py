"""Tests for SQLite to MongoDB migration script with Learning Lab support."""

import hashlib
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
    assert counts["learning_drafts"] == 1  # 1 valid, 1 orphan quarantined
    assert counts["learning_submissions"] == 1  # 1 valid, 1 orphan skipped
    assert counts["learning_drafts_quarantine"] == 1  # 1 orphan draft quarantined

    # Audit metadata counts
    assert counts.source_counts["learning_drafts"] == 2
    assert counts.migrated_counts["learning_drafts"] == 1
    assert counts.quarantined_counts["learning_drafts"] == 1
    assert counts.skipped_counts["learning_concepts"] == 1
    assert counts.skipped_counts["learning_exercises"] == 1
    assert counts.skipped_counts["learning_submissions"] == 1

    # MongoDB collections remain completely empty (zero writes)
    for col in (
        "reviews",
        "saved_pull_requests",
        "learning_attempts",
        "learning_spaces",
        "learning_concepts",
        "learning_exercises",
        "learning_drafts",
        "learning_drafts_quarantine",
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
    assert counts["learning_drafts_quarantine"] == 1

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

    # Verify orphans were NOT inserted into main Learning Lab collections
    assert db["learning_concepts"].find_one({"concept_id": "concept-orphan-1"}) is None
    assert db["learning_exercises"].find_one({"exercise_id": "exercise-orphan-1"}) is None
    assert db["learning_drafts"].find_one({"draft_id": "draft-orphan-1"}) is None
    assert db["learning_submissions"].find_one({"submission_id": "sub-orphan-1"}) is None

    # Verify orphan draft was quarantined with correct schema and metadata
    quarantine_doc = db["learning_drafts_quarantine"].find_one({"draft_id": "draft-orphan-1"})
    assert quarantine_doc is not None
    assert quarantine_doc["draft_id"] == "draft-orphan-1"
    assert quarantine_doc["exercise_id"] == "exercise-nonexistent"
    assert quarantine_doc["user_id"] == "u-1"
    assert quarantine_doc["code"] == "code"
    assert isinstance(quarantine_doc["updated_at"], datetime)
    assert quarantine_doc["_quarantine_reason"] == "missing_parent_exercise: exercise-nonexistent"
    assert quarantine_doc["_source_table"] == "learning_drafts"
    assert isinstance(quarantine_doc["_quarantined_at"], datetime)


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
    assert db["learning_drafts_quarantine"].count_documents({}) == 1
    assert db["learning_submissions"].count_documents({}) == 1


def test_quarantine_write_once_preserves_existing_record_and_metadata(mock_target, populated_sqlite_path):
    """Verify quarantine uses write-once $setOnInsert: existing records, timestamps, and admin notes are preserved."""
    db, store = mock_target
    existing_quarantined_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    existing_doc = {
        "draft_id": "draft-orphan-1",
        "exercise_id": "exercise-nonexistent",
        "user_id": "u-1",
        "code": "original code from initial quarantine",
        "updated_at": datetime(2026, 1, 10, 10, 0, 0, tzinfo=UTC),
        "_quarantine_reason": "missing_parent_exercise: exercise-nonexistent",
        "_source_table": "learning_drafts",
        "_quarantined_at": existing_quarantined_at,
        "admin_note": "Recovery investigation started",
        "tags": ["needs_review", "orphan_triage"],
    }
    db["learning_drafts_quarantine"].insert_one(existing_doc)

    result = migrate(
        populated_sqlite_path,
        store,
        dry_run=False,
        include_learning_lab=True,
        reconcile=True,
    )

    # Verify no duplicate document is created in quarantine
    assert db["learning_drafts_quarantine"].count_documents({}) == 1

    # Verify existing document remains completely unchanged
    quarantined = db["learning_drafts_quarantine"].find_one({"draft_id": "draft-orphan-1"})
    assert quarantined is not None
    assert quarantined["_quarantined_at"] == existing_quarantined_at
    assert quarantined["admin_note"] == "Recovery investigation started"
    assert quarantined["tags"] == ["needs_review", "orphan_triage"]
    assert quarantined["code"] == "original code from initial quarantine"

    # Verify migration summary remains accurate
    assert result["learning_drafts_quarantine"] == 1
    assert result.quarantined_counts["learning_drafts"] == 1
    assert len(result.quarantine_records) == 1
    assert result.quarantine_records[0]["id"] == "draft-orphan-1"


def test_quarantine_record_and_skip_reason_details(mock_target, populated_sqlite_path):
    """Verify detailed skip and quarantine reasons recorded during migration."""
    db, store = mock_target
    result = migrate(
        populated_sqlite_path,
        store,
        dry_run=False,
        include_learning_lab=True,
        reconcile=True,
    )

    # Quarantined records
    assert len(result.quarantine_records) == 1
    assert result.quarantine_records[0] == {
        "table": "learning_drafts",
        "id": "draft-orphan-1",
        "target_collection": "learning_drafts_quarantine",
        "reason": "missing_parent_exercise: exercise-nonexistent",
    }

    # Skipped records
    assert len(result.skip_reasons) == 3
    skip_map = {item["id"]: item for item in result.skip_reasons}
    assert skip_map["concept-orphan-1"]["reason"] == "missing_parent_space: space-nonexistent"
    assert skip_map["exercise-orphan-1"]["reason"] == "missing_parent_concept: concept-nonexistent"
    assert skip_map["sub-orphan-1"]["reason"] == "missing_parent_exercise: exercise-nonexistent"


def test_existing_records_not_overwritten_by_unrelated_migration(mock_target, populated_sqlite_path):
    """Verify existing MongoDB records not in source database are never overwritten or deleted."""
    db, store = mock_target
    now = datetime.now(UTC)
    db["learning_spaces"].insert_one({
        "space_id": "space-preexisting-unrelated",
        "canonical_repo": "other/repo",
        "pr_number": 999,
        "pr_title": "Unrelated PR",
        "pr_url": "https://github.com/other/repo/pull/999",
        "latest_review_id": "r-unrelated",
        "review_count": 1,
        "primary_language": "go",
        "languages": ["go"],
        "created_at": now,
        "updated_at": now,
        "owner_id": "unrelated-owner",
    })

    migrate(
        populated_sqlite_path,
        store,
        dry_run=False,
        include_learning_lab=True,
        reconcile=True,
    )

    # Pre-existing record is preserved
    unrelated = db["learning_spaces"].find_one({"space_id": "space-preexisting-unrelated"})
    assert unrelated is not None
    assert unrelated["owner_id"] == "unrelated-owner"

    # Migrated record also present
    migrated = db["learning_spaces"].find_one({"space_id": "space-valid-1"})
    assert migrated is not None
    assert db["learning_spaces"].count_documents({}) == 2


@pytest.fixture
def representative_sqlite_path(tmp_path):
    """Representative SQLite database with valid records across all collections and an orphan draft."""
    db_file = tmp_path / "representative_pr_mentor.sqlite3"
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

        conn.execute(
            "INSERT INTO reviews VALUES ('r-rep-1', 'u-rep-1', '2026-11-10T10:00:00Z', '{\"review_id\": \"r-rep-1\", \"created_at\": \"2026-11-10T10:00:00Z\", \"findings\": []}', 'u-rep-1')"
        )
        conn.execute(
            "INSERT INTO reviews VALUES ('r-rep-2', 'u-rep-2', '2026-11-10T11:00:00Z', '{\"review_id\": \"r-rep-2\", \"created_at\": \"2026-11-10T11:00:00Z\", \"findings\": []}', 'u-rep-2')"
        )
        conn.execute(
            "INSERT INTO saved_pull_requests VALUES ('s-rep-1', 'u-rep-1', 'https://github.com/org/repo/pull/10', '2026-11-10T10:00:00Z', 'u-rep-1')"
        )
        conn.execute(
            "INSERT INTO learning_attempts VALUES (1, 'ex-rep-1', 'security', 1, '2026-11-10T10:00:00Z', 'u-rep-1', 'u-rep-1')"
        )
        conn.execute(
            """
            INSERT INTO learning_spaces VALUES (
                'space-rep-1', 'org/repo', 10, 'PR 10', 'https://github.com/org/repo/pull/10',
                'r-rep-1', 1, 'python', '["python"]', '2026-11-10T10:00:00+00:00', '2026-11-10T10:00:00+00:00', 'u-rep-1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_concepts VALUES (
                'concept-rep-1', 'space-rep-1', 'Input Validation', 'Summary', 'Why it matters',
                'security', 'python', '["issue-rep-1"]', '[]', 1, 'not_started', 'u-rep-1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_exercises VALUES (
                'ex-rep-1', 'space-rep-1', 'concept-rep-1', 'issue-rep-1',
                'Sanitize Input', 'Desc', 'Inst', 'python', 'beginner', 'security',
                'def sanitize(x): pass', 'def sanitize(x): return x', 'expected',
                '["hint 1"]', 'explanation', '["crit 1"]', '{}', 10
            )
            """
        )
        # Valid draft
        conn.execute(
            """
            INSERT INTO learning_drafts VALUES (
                'draft-valid-1', 'ex-rep-1', 'u-rep-1', 'def sanitize(x): return clean(x)', '2026-11-10T12:00:00+00:00'
            )
            """
        )
        # Orphaned draft referencing missing exercise ex-test-draft-1
        conn.execute(
            """
            INSERT INTO learning_drafts VALUES (
                'draft-ex-test-draft-1-user-alice', 'ex-test-draft-1', 'user-alice', 'orphan draft code', '2026-11-10T13:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO learning_submissions VALUES (
                'sub-rep-1', 'ex-rep-1', 'u-rep-1', 'def sanitize(x): return clean(x)', 'completed', 1,
                'Passed feedback', '{"check": "ok"}', '2026-11-10T14:00:00+00:00'
            )
            """
        )
        conn.commit()

    return db_file


def test_real_sqlite_file_dry_run_and_quarantine_reporting(mock_target, representative_sqlite_path):
    """Verify migration dry run, quarantine reporting, and source database immutability using a representative SQLite fixture."""
    db, store = mock_target

    # Compute source SQLite hash and record counts prior to dry run
    sha256_before = hashlib.sha256(representative_sqlite_path.read_bytes()).hexdigest()
    with closing(sqlite3.connect(representative_sqlite_path)) as conn:
        counts_before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "reviews",
                "saved_pull_requests",
                "learning_attempts",
                "learning_spaces",
                "learning_concepts",
                "learning_exercises",
                "learning_drafts",
                "learning_submissions",
            )
        }

    result = migrate(
        representative_sqlite_path,
        store,
        dry_run=True,
        include_learning_lab=True,
        reconcile=False,
    )

    # Immutability verification: source SQLite database file must not be modified in dry-run
    sha256_after = hashlib.sha256(representative_sqlite_path.read_bytes()).hexdigest()
    assert sha256_before == sha256_after, "Dry run must not modify the source SQLite database file"

    with closing(sqlite3.connect(representative_sqlite_path)) as conn:
        counts_after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_before
        }
    assert counts_before == counts_after, "Source database table row counts must remain invariant during dry run"

    # Migration counts match representative dataset
    assert result["reviews"] == 2
    assert result["saved_pull_requests"] == 1
    assert result["learning_attempts"] == 1
    assert result["learning_spaces"] == 1
    assert result["learning_concepts"] == 1
    assert result["learning_exercises"] == 1
    assert result["learning_drafts"] == 1  # 1 valid draft migrated
    assert result["learning_submissions"] == 1
    assert result["learning_drafts_quarantine"] == 1

    # Accounting consistency: Source = Migrated + Skipped + Quarantined across all tracked collections
    for table_name, src_count in result.source_counts.items():
        mig_count = result.migrated_counts.get(table_name, 0)
        skip_count = result.skipped_counts.get(table_name, 0)
        quar_count = result.quarantined_counts.get(table_name, 0)
        assert (
            src_count == mig_count + skip_count + quar_count
        ), f"Accounting discrepancy in {table_name}: source {src_count} != migrated {mig_count} + skipped {skip_count} + quarantined {quar_count}"

    assert result.source_counts["learning_drafts"] == 2
    assert result.migrated_counts["learning_drafts"] == 1
    assert result.quarantined_counts["learning_drafts"] == 1

    # Orphan draft was detected and routed to quarantine
    assert len(result.quarantine_records) == 1
    assert result.quarantine_records[0]["table"] == "learning_drafts"
    assert result.quarantine_records[0]["id"] == "draft-ex-test-draft-1-user-alice"
    assert result.quarantine_records[0]["reason"] == "missing_parent_exercise: ex-test-draft-1"
    assert result.quarantine_records[0]["target_collection"] == "learning_drafts_quarantine"

    # Zero writes performed to MongoDB
    for col in (
        "reviews",
        "saved_pull_requests",
        "learning_attempts",
        "learning_spaces",
        "learning_concepts",
        "learning_exercises",
        "learning_drafts",
        "learning_drafts_quarantine",
        "learning_submissions",
    ):
        assert db[col].count_documents({}) == 0

    # Summary table formatting renders correctly
    table_str = result.format_summary_table(dry_run=True)
    assert "[DRY-RUN]" in table_str
    assert "draft-ex-test-draft-1-user-alice" in table_str
    assert "missing_parent_exercise: ex-test-draft-1" in table_str


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
