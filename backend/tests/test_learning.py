import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "learning.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as instance:
        assert instance.post("/api/auth/register", json={"email": "learner@example.com", "password": "correct horse battery staple"}).status_code == 201
        yield instance


def test_exercises_hide_answer_and_submission_updates_persistent_progress(client):
    exercises = client.get("/api/learning/exercises").json()
    assert exercises and all("correct_answer" not in item and "explanation" not in item for item in exercises)
    exercise = exercises[0]
    result = client.post(f"/api/learning/exercises/{exercise['id']}/submit", json={"answer": exercise["options"][0]})
    assert result.status_code == 200
    body = result.json()
    assert body["correct"] is True and body["progress"]["attempt_count"] == 1
    assert client.get("/api/learning/progress").json()["correct_answers"] == 1
    assert client.get("/api/learning/history").json()["attempts"][0]["exercise_id"] == exercise["id"]


def test_learning_input_and_unknown_exercise_errors(client):
    assert client.get("/api/learning/exercises/nope").status_code == 404
    assert client.post("/api/learning/exercises/nope/submit", json={"answer": "x"}).status_code == 404
    assert client.post("/api/learning/exercises/empty-average-1/submit", json={"answer": ""}).status_code == 422
    assert client.get("/api/learning/exercises?difficulty=impossible").json() == []
