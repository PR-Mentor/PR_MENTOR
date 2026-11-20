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


def test_pr_learning_spaces_and_coding_exercise_lifecycle(client):
    from app.data.sample_reviews import sample_review
    from app.database.reviews import get_store

    # 1. Dashboard empty state
    dash_res = client.get("/api/learning/dashboard")
    assert dash_res.status_code == 200
    dash = dash_res.json()
    assert "total_spaces" in dash
    assert "total_concepts" in dash

    # 2. Seed a review and resolve a space
    review = sample_review("owner", "repo", 42).model_dump(mode="json")
    review["canonical_issues"] = [
        {
            "issue_id": "iss-1",
            "file_path": "src/app.py",
            "category": "security",
            "normalized_description": "Command injection via subprocess shell=True",
        }
    ]
    get_store().save_review(review, owner_id=client.get("/api/auth/me").json()["user_id"])

    resolve_res = client.post("/api/learning/spaces/resolve", json={"review_id": review["review_id"]})
    assert resolve_res.status_code == 200
    space = resolve_res.json()
    assert space["canonical_repo"] == "owner/repo"
    assert space["pr_number"] == 42
    space_id = space["space_id"]

    # 3. List spaces and get space
    spaces_res = client.get("/api/learning/spaces")
    assert spaces_res.status_code == 200
    assert any(s["space_id"] == space_id for s in spaces_res.json())

    get_space_res = client.get(f"/api/learning/spaces/{space_id}")
    assert get_space_res.status_code == 200
    assert get_space_res.json()["space_id"] == space_id

    # 4. Concepts in space
    concepts_res = client.get(f"/api/learning/spaces/{space_id}/concepts")
    assert concepts_res.status_code == 200
    concepts = concepts_res.json()
    assert len(concepts) >= 1
    concept_id = concepts[0]["concept_id"]

    concept_detail = client.get(f"/api/learning/spaces/{space_id}/concepts/{concept_id}").json()
    assert concept_detail["concept_id"] == concept_id

    # 5. Exercises in space / concept
    exercises_res = client.get(f"/api/learning/spaces/{space_id}/exercises?concept_id={concept_id}")
    assert exercises_res.status_code == 200
    exercises = exercises_res.json()
    assert len(exercises) >= 1
    ex_id = exercises[0]["exercise_id"]

    # 6. Draft save and retrieve
    draft_save = client.post(f"/api/learning/coding-exercises/{ex_id}/draft", json={"code": "import subprocess\n# work in progress"})
    assert draft_save.status_code == 200
    assert draft_save.json()["code"] == "import subprocess\n# work in progress"

    draft_get = client.get(f"/api/learning/coding-exercises/{ex_id}/draft")
    assert draft_get.status_code == 200
    assert draft_get.json()["code"] == "import subprocess\n# work in progress"

    # 7. Submissions (failing and passing)
    sub_fail = client.post(
        f"/api/learning/coding-exercises/{ex_id}/submit",
        json={"code": "subprocess.Popen(cmd, shell=True)"},
    )
    assert sub_fail.status_code == 200
    assert sub_fail.json()["passed"] is False

    sub_pass = client.post(
        f"/api/learning/coding-exercises/{ex_id}/submit",
        json={"code": "subprocess.run(['tool', safe_file_path], check=True)"},
    )
    assert sub_pass.status_code == 200
    assert sub_pass.json()["passed"] is True

    # 8. List submissions
    subs_res = client.get(f"/api/learning/coding-exercises/{ex_id}/submissions")
    assert subs_res.status_code == 200
    assert len(subs_res.json()) == 2
