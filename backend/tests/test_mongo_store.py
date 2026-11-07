from datetime import datetime, timedelta

import mongomock
import pytest
from fastapi import HTTPException
from pymongo.errors import ServerSelectionTimeoutError

from app.config import storage_backend
from app.database import mongo_connection
from app.database.mongo_connection import StorageUnavailable
from app.database.mongo_reviews import MongoReviewStore
from app.database.reviews import get_store
from scripts.migrate_sqlite_to_mongodb import migrate
from app.database.reviews import SQLiteReviewStore


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
