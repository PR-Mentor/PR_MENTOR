from datetime import datetime, timedelta

import mongomock
import pytest
from fastapi import HTTPException
from pymongo.errors import ServerSelectionTimeoutError

from app.config import storage_backend
from app.database import mongo_connection, workspaces
from app.database.mongo_connection import StorageUnavailable
from app.database.mongo_reviews import MongoReviewStore
from app.database.reviews import SQLiteReviewStore, get_store
from scripts.migrate_sqlite_to_mongodb import migrate


@pytest.fixture
def store():
    client = mongomock.MongoClient(tz_aware=True)
    return MongoReviewStore(client["pr_mentor_test"])


def test_mongo_review_upsert_and_history_hide_object_ids(store):
    review = {"review_id": "r-1", "created_at": "2026-10-18T10:00:00Z", "findings": []}
    store.save_review(review, "test-user")
    owned_review = store.get_review("r-1", "test-user")
    assert owned_review["review_id"] == "r-1"
    assert "_id" not in owned_review
    assert store.get_review("r-1", "another-user") is None

    store.save_review({"review_id": "r-2", "created_at": "2026-10-19T10:00:00Z", "findings": []}, "test-user")
    assert [item["review_id"] for item in store.list_reviews("test-user")] == ["r-2", "r-1"]
    assert store.list_reviews("another-user") == []
    assert isinstance(store.get_review("r-1", "test-user")["created_at"], datetime)
    assert store.get_review("r-1", "test-user")["created_at"].utcoffset() == timedelta(0)


def test_mongo_shared_review_update_preserves_original_account_owner(store):
    review = {"review_id": "shared-owner", "created_at": "2026-11-08T10:00:00Z", "findings": []}
    store.save_review(review, owner_id="original-owner")
    store.save_review({**review, "summary": "Updated by workspace editor."}, owner_id="workspace-editor")
    assert store.get_review("shared-owner", owner_id="original-owner")["summary"] == "Updated by workspace editor."
    assert store.get_review("shared-owner", owner_id="workspace-editor") is None


def test_mongo_counts_unique_live_pull_requests_without_demo_or_other_accounts(store):
    pull_request = {"repository_owner": "Example", "repository_name": "Repo", "pull_request_number": 17}
    for review_id in ("live-first", "live-repeat"):
        store.save_review({"review_id": review_id, "created_at": "2026-11-08T00:00:00Z", "is_demo": False, "pull_request": pull_request}, owner_id="profile-user")
    store.save_review({"review_id": "live-next", "created_at": "2026-11-08T00:00:00Z", "is_demo": False, "pull_request": {**pull_request, "pull_request_number": 18}}, owner_id="profile-user")
    store.save_review({"review_id": "demo", "created_at": "2026-11-08T00:00:00Z", "is_demo": True, "pull_request": {**pull_request, "pull_request_number": 19}}, owner_id="profile-user")
    store.save_review({"review_id": "legacy-demo", "created_at": "2026-11-08T00:00:00Z", "analysis_mode": "demo", "pull_request": {**pull_request, "pull_request_number": 21}}, owner_id="profile-user")
    store.save_review({"review_id": "other-user", "created_at": "2026-11-08T00:00:00Z", "is_demo": False, "pull_request": {**pull_request, "pull_request_number": 20}}, owner_id="another-user")
    assert store.live_pull_request_count(owner_id="profile-user") == 2


def test_mongo_counts_canonical_or_legacy_live_findings_without_demo_or_other_accounts(store):
    store.save_review({
        "review_id": "canonical", "created_at": "2026-11-08T00:00:00Z", "is_demo": False,
        "canonical_issues": [{"issue_id": "i1"}, {"issue_id": "i2"}],
        "findings": [{"id": "f1"}, {"id": "f2"}, {"id": "f3"}],
    }, owner_id="profile-user")
    store.save_review({
        "review_id": "legacy", "created_at": "2026-11-08T00:00:00Z", "is_demo": False,
        "findings": [{"id": "f4"}],
    }, owner_id="profile-user")
    store.save_review({
        "review_id": "demo", "created_at": "2026-11-08T00:00:00Z", "is_demo": True,
        "canonical_issues": [{"issue_id": "demo-issue"}],
    }, owner_id="profile-user")
    store.save_review({
        "review_id": "legacy-demo", "created_at": "2026-11-08T00:00:00Z", "analysis_mode": "demo",
        "canonical_issues": [{"issue_id": "legacy-demo-issue"}],
    }, owner_id="profile-user")
    store.save_review({
        "review_id": "other-user", "created_at": "2026-11-08T00:00:00Z", "is_demo": False,
        "findings": [{"id": "other-finding"}],
    }, owner_id="another-user")
    assert store.live_finding_count(owner_id="profile-user") == 3


def test_mongo_personal_review_window_and_count_exclude_demos(store):
    store.save_review({"review_id": "old-live", "created_at": "2026-11-07T00:00:00Z", "is_demo": False}, owner_id="profile-user")
    store.save_review({"review_id": "demo", "created_at": "2026-11-08T00:00:00Z", "is_demo": True}, owner_id="profile-user")
    store.save_review({"review_id": "legacy-demo", "created_at": "2026-11-08T00:00:00Z", "analysis_mode": "demo"}, owner_id="profile-user")
    store.save_review({"review_id": "legacy-live", "created_at": "2026-11-09T00:00:00Z"}, owner_id="profile-user")
    assert store.live_review_count(owner_id="profile-user") == 2
    assert [review["review_id"] for review in store.list_reviews(
        100, owner_id="profile-user", include_demo=False
    )] == ["legacy-live", "old-live"]


def test_mongo_review_round_trips_canonical_issues_and_legacy_records(store):
    canonical = {
        "review_id": "r-canonical",
        "created_at": "2026-11-06T10:00:00Z",
        "findings": [{"id": "f1", "source": "static_analysis"}],
        "canonical_issues": [{
            "issue_id": "issue-1",
            "explanation": {
                "summary": "Untrusted input reaches this operation.",
                "why_it_matters": "It may affect the operation's behavior.",
                "root_cause": "Input is combined with instructions.",
                "correction": "Separate values from executable syntax.",
                "concept": "Parameterization separates values from instructions.",
                "learning_resources": [],
                "caveats": ["Context is limited."],
            },
            "observations": [{
                "tool_name": "ruff", "rule_id": "F401",
                "original_finding": {"id": "f1", "source": "static_analysis"},
            }],
            "suggested_fixes": [{
                "fix_id": "fix-1", "canonical_issue_id": "issue-1", "file_path": "src/a.py",
                "start_line": 3, "end_line": 3, "language": "python", "base_commit_sha": "a" * 40,
                "file_blob_sha": "b" * 40, "expected_original_code": "value = input()",
                "proposed_code": "value = str(input())", "change_summary": "Make input conversion explicit.",
                "rationale": "The value is used as text.", "limitations": [], "confidence": 0.8,
                "generation_status": "generated", "decision_status": "pending",
                "generated_at": "2026-11-06T10:00:00Z",
            }],
        }],
        "unique_issue_count": 1,
        "raw_observation_count": 1,
        "tool_results": [{
            "tool": "fix_generation", "status": "completed", "executed": True,
            "requested": True, "generated_issue_count": 1,
            "outcome_counts": {"generated": 1},
            "per_issue_outcomes": [{"issue_id": "issue-1", "status": "generated", "reason": None}],
        }, {
            "tool": "fix_analysis", "status": "completed", "executed": True,
            "proposal_count": 2, "analyzed_proposal_count": 2, "pair_count": 1,
            "relationship_counts": {"duplicate": 1},
            "relationships": [{
                "relationship_id": "fix-rel-duplicate", "fix_ids": ["fix-1", "fix-2"],
                "canonical_issue_ids": ["issue-1", "issue-2"], "file_path": "src/a.py",
                "line_ranges": [
                    {"canonical_issue_id": "issue-1", "fix_id": "fix-1", "start_line": 3, "end_line": 3},
                    {"canonical_issue_id": "issue-2", "fix_id": "fix-2", "start_line": 3, "end_line": 3},
                ],
                "relationship_type": "duplicate", "explanation": "Same replacement.",
                "detection_method": "same_snapshot_exact_range_and_replacement_comparison",
            }],
            "conflict_status": "completed", "freshness_status": "completed",
            "freshness": {
                "status": "completed", "proposal_count": 1, "current_count": 1,
                "stale_count": 0, "unverifiable_count": 0,
                "checked_head_sha": "b" * 40, "file_fetch_count": 1,
                "per_proposal": [{"canonical_issue_id": "issue-1", "fix_id": "fix-1", "status": "current", "reason": "File matches."}],
            },
        }],
    }
    legacy = {"review_id": "r-legacy", "created_at": "2026-11-05T10:00:00Z", "findings": [{"id": "old-finding"}]}

    store.save_review(canonical)
    store.save_review(legacy)

    retrieved = store.get_review("r-canonical")
    assert retrieved["canonical_issues"] == canonical["canonical_issues"]
    assert retrieved["canonical_issues"][0]["explanation"]["summary"] == "Untrusted input reaches this operation."
    assert retrieved["unique_issue_count"] == 1
    assert retrieved["raw_observation_count"] == 1
    assert retrieved["canonical_issues"][0]["suggested_fixes"][0]["fix_id"] == "fix-1"
    assert retrieved["tool_results"] == canonical["tool_results"]
    assert retrieved["tool_results"][1]["relationships"][0]["relationship_type"] == "duplicate"
    assert retrieved["tool_results"][1]["freshness"]["per_proposal"][0]["status"] == "current"
    legacy_result = store.get_review("r-legacy")
    assert legacy_result["review_id"] == legacy["review_id"]
    assert legacy_result["findings"] == legacy["findings"]
    assert isinstance(legacy_result["created_at"], datetime)
    assert "canonical_issues" not in legacy_result
    assert [item["review_id"] for item in store.list_reviews()] == ["r-canonical", "r-legacy"]


def test_saved_urls_are_idempotent_and_deletable(store):
    url = "https://github.com/owner/repo/pull/12"
    first = store.save_pull_request(url, "test-user")
    second = store.save_pull_request(url, "test-user")
    assert first == second
    assert first["temporary"] is False
    assert store.list_saved("test-user") == [first]
    assert store.list_saved("another-user") == []
    assert store.delete_saved(first["saved_id"], "another-user") is False
    assert store.delete_saved(first["saved_id"], "test-user") is True
    assert store.delete_saved(first["saved_id"], "test-user") is False


def test_user_owned_reviews_saved_links_and_learning_are_isolated(store):
    first = {"review_id": "private-a", "created_at": "2026-11-08T10:00:00Z", "findings": []}
    second = {"review_id": "private-b", "created_at": "2026-11-08T10:01:00Z", "findings": []}
    store.save_review(first, owner_id="user-a")
    store.save_review(second, owner_id="user-b")
    assert store.get_review("private-a", owner_id="user-b") is None
    assert [item["review_id"] for item in store.list_reviews(owner_id="user-a")] == ["private-a"]
    assert "owner_id" not in store.get_review("private-a", owner_id="user-a")

    url = "https://github.com/owner/repo/pull/44"
    saved_a = store.save_pull_request(url, owner_id="user-a")
    saved_b = store.save_pull_request(url, owner_id="user-b")
    assert saved_a["saved_id"] != saved_b["saved_id"]
    assert store.list_saved(owner_id="user-a") == [saved_a]
    assert store.delete_saved(saved_a["saved_id"], owner_id="user-b") is False

    store.record_learning_attempt("exercise-1", "security", True, owner_id="user-a")
    store.record_learning_attempt("exercise-2", "testing", False, owner_id="user-b")
    assert store.learning_progress(owner_id="user-a")["attempt_count"] == 1
    assert store.learning_progress(owner_id="user-b")["attempt_count"] == 1


def test_learning_progress_and_history_preserve_frontend_contract(store):
    store.record_learning_attempt("exercise-1", "security", True)
    store.record_learning_attempt("exercise-1", "security", False)
    store.record_learning_attempt("exercise-2", "bug", True)
    progress = store.learning_progress()
    assert progress == {
        "temporary": False,
        "exercises_completed": 2,
        "correct_answers": 2,
        "attempt_count": 3,
        "accuracy": 2 / 3,
        "topics_practiced": {"security": 2, "bug": 1},
    }
    assert store.learning_history()["attempts"][0] == {
        "exercise_id": "exercise-2", "category": "bug", "correct": True,
    }


def test_storage_selection_uses_mongo_without_connecting_to_live_database(monkeypatch):
    client = mongomock.MongoClient(tz_aware=True)
    database = client["pr_mentor_test"]
    monkeypatch.setenv("PR_MENTOR_STORAGE", "mongodb")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setattr(mongo_connection, "get_mongo_database", lambda: database)
    assert storage_backend() == "mongodb"
    assert isinstance(get_store(), MongoReviewStore)


def test_mongo_selection_requires_uri_and_never_falls_back(monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "mongodb")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    with pytest.raises(StorageUnavailable, match="requires MONGODB_URI"):
        get_store()


def test_mongo_client_is_process_scoped_bounded_and_closed(monkeypatch):
    class FakeClient:
        def __init__(self, uri, **options):
            self.uri = uri
            self.options = options
            self.closed = False

        def close(self):
            self.closed = True

    clients = []

    def make_client(uri, **options):
        client = FakeClient(uri, **options)
        clients.append(client)
        return client

    mongo_connection.close_mongo_client()
    monkeypatch.setattr(mongo_connection, "MongoClient", make_client)
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    first = mongo_connection.get_mongo_client()
    assert mongo_connection.get_mongo_client() is first
    assert len(clients) == 1
    assert first.options["serverSelectionTimeoutMS"] == 5000
    assert first.options["connectTimeoutMS"] == 5000
    assert first.options["socketTimeoutMS"] == 10000
    mongo_connection.close_mongo_client()
    assert first.closed is True


@pytest.mark.parametrize("uri", [
    "https://example.com",
    "mongodb+srv://cluster.example.net/?tls=false",
    "mongodb://db.example.net/?tls=false",
    "mongodb://db.example.net/?tlsInsecure=true",
    "mongodb://db.example.net/?tlsAllowInvalidCertificates=true",
])
def test_invalid_or_insecure_remote_uris_are_rejected(uri):
    with pytest.raises(StorageUnavailable):
        mongo_connection._validate_uri(uri)


def test_mongo_readiness_failure_is_sanitized(monkeypatch):
    class UnavailableDatabase:
        def command(self, _command):
            raise ServerSelectionTimeoutError("do not expose connection details")

    monkeypatch.setattr(mongo_connection, "get_mongo_database", lambda: UnavailableDatabase())
    with pytest.raises(StorageUnavailable, match="readiness check failed") as error:
        mongo_connection.ping_mongo()
    assert "connection details" not in str(error.value)


def test_ready_endpoint_reports_mongo_failure_without_falling_back(monkeypatch):
    from app.main import readiness

    monkeypatch.setenv("PR_MENTOR_STORAGE", "mongodb")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    with pytest.raises(HTTPException) as error:
        readiness()
    assert error.value.status_code == 503
    assert error.value.detail == "Configured storage is unavailable."


def test_invalid_storage_name_is_rejected(monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "mongoddb")
    with pytest.raises(ValueError, match="PR_MENTOR_STORAGE"):
        storage_backend()


def test_sqlite_migration_is_repeatable_and_preserves_source(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "legacy.sqlite3"
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(sqlite_path))
    source = SQLiteReviewStore()
    source.save_review({"review_id": "legacy-review", "created_at": "2026-10-18T10:00:00Z", "findings": []}, "source-user")
    saved = source.save_pull_request("https://github.com/owner/repo/pull/1", "source-user")
    source.record_learning_attempt("exercise-1", "security", True)

    mongo_client = mongomock.MongoClient(tz_aware=True)
    target = MongoReviewStore(mongo_client["migration_test"])
    first = migrate(sqlite_path, target)
    second = migrate(sqlite_path, target)

    assert first == second == {"reviews": 1, "saved_pull_requests": 1, "learning_attempts": 1}
    assert target.get_review("legacy-review", "legacy:unassigned")["review_id"] == "legacy-review"
    migrated_saved = target.saved.find_one({"saved_id": saved["saved_id"]})
    assert migrated_saved["url"] == "https://github.com/owner/repo/pull/1"
    assert "user_id" not in migrated_saved
    assert target.learning_progress()["attempt_count"] == 1
    assert sqlite_path.exists()


def test_workspace_membership_invitation_and_review_share_round_trip_mongodb(monkeypatch):
    database = mongomock.MongoClient(tz_aware=True)["workspace_test"]
    database["auth_users"].insert_many([
        {"user_id": "owner", "email": "owner@example.com"},
        {"user_id": "member", "email": "member@example.com"},
    ])
    monkeypatch.setattr(workspaces, "_mongo", lambda: True)
    monkeypatch.setattr(workspaces, "_db", lambda: database)

    workspace = workspaces.create_workspace("owner", "Shared review team")
    workspace_id = workspace["workspace_id"]
    assert workspaces.get_role(workspace_id, "owner") == "owner"
    assert workspaces.list_workspaces("owner")[0]["name"] == "Shared review team"

    token_hash = workspaces.hash_invitation("opaque-invitation-token")
    workspaces.create_invitation(
        token_hash, workspace_id, "member@example.com", "editor", "owner"
    )
    assert workspaces.accept_invitation(
        token_hash, "member", "member@example.com"
    ) == workspace_id
    assert workspaces.accept_invitation(
        token_hash, "member", "member@example.com"
    ) is None
    assert workspaces.get_role(workspace_id, "member") == "editor"

    workspaces.link_review(workspace_id, "review-shared", "owner")
    assert workspaces.review_role("member", "review-shared") == "editor"
    assert workspaces.list_shared_review_ids(workspace_id) == ["review-shared"]
    assert workspaces.unlink_review(workspace_id, "review-shared") is True
    assert workspaces.review_role("member", "review-shared") is None
