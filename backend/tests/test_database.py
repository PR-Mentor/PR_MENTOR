from app.database.reviews import SQLiteReviewStore


def test_sqlite_store_persists_reviews_saved_links_and_learning(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "persistent.sqlite3"))
    first = SQLiteReviewStore()
    review = {"review_id": "r1", "created_at": "2026-01-01T00:00:00Z", "findings": []}
    first.save_review(review, "test-user")
    first.save_pull_request("https://github.com/o/r/pull/1", "test-user")
    first.record_learning_attempt("exercise-1", "security", True)
    first.record_learning_attempt("exercise-1", "security", False)

    second = SQLiteReviewStore()
    assert second.get_review("r1", "test-user") == review
    assert second.get_review("r1", "another-user") is None
    assert second.list_reviews("test-user")[0] == review
    assert second.list_reviews("another-user") == []
    assert second.list_saved("test-user")[0]["url"] == "https://github.com/o/r/pull/1"
    assert second.list_saved("another-user") == []
    progress = second.learning_progress()
    assert progress["correct_answers"] == 1
    assert progress["attempt_count"] == 2
    assert progress["exercises_completed"] == 1
    assert second.learning_history()["attempts"][0]["correct"] is False
