"""Comprehensive tests for Learning Lab persistence using MongoDB."""

from datetime import UTC, datetime
from typing import Any

import mongomock
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database.mongo_learning import MongoLearningStore, create_learning_indexes
from app.database.sqlite_learning import SQLiteLearningStore
from app.main import app
from app.routes.auth import current_user
from app.schemas.learning_lab import CodingExercisePublic, PRLearningSpace
from app.services.learning_spaces import LearningSpaceService


def _sample_review(
    owner: str = "octocat", repo: str = "hello-world", pr_num: int = 101, review_id: str = "r-test-1"
) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "pull_request": {
            "repository_owner": owner,
            "repository_name": repo,
            "pull_request_number": pr_num,
            "title": "Fix security vulnerability in buffer handling",
            "pull_request_url": f"https://github.com/{owner}/{repo}/pull/{pr_num}",
        },
        "canonical_issues": [
            {
                "issue_id": "issue-c-1",
                "file_path": "src/buffer.c",
                "category": "security",
                "normalized_description": "Unsafe gets() function allows buffer overflow",
                "explanation": {"why_it_matters": "Unbounded string operations lead to memory corruption."},
            },
            {
                "issue_id": "issue-cpp-1",
                "file_path": "src/handler.cpp",
                "category": "maintainability",
                "normalized_description": "Exception slicing catch (std::exception e)",
            },
            {
                "issue_id": "issue-java-1",
                "file_path": "src/Repo.java",
                "category": "security",
                "normalized_description": "SQL injection in query parameterization",
            },
            {
                "issue_id": "issue-py-1",
                "file_path": "scripts/tool.py",
                "category": "bug",
                "normalized_description": "Command injection via subprocess shell=True",
            },
        ],
    }


@pytest.fixture
def mongo_db():
    client = mongomock.MongoClient(tz_aware=True)
    db = client["pr_mentor_test"]
    create_learning_indexes(db)
    return db


@pytest.fixture
def mongo_store(mongo_db):
    return MongoLearningStore(mongo_db, ensure_indexes=False)


@pytest.fixture
def mongo_service(mongo_store):
    return LearningSpaceService(store=mongo_store)


def test_mongo_space_resolution_and_deduplication(mongo_service, mongo_db):
    review = _sample_review()
    space1 = mongo_service.resolve_or_create_space(review, owner_id="user-alice")

    assert space1.canonical_repo == "octocat/hello-world"
    assert space1.pr_number == 101
    assert space1.review_count == 1
    assert "c" in space1.languages
    assert "cpp" in space1.languages

    # Check MongoDB direct document
    doc = mongo_db["learning_spaces"].find_one({"space_id": space1.space_id})
    assert doc is not None
    assert doc["owner_id"] == "user-alice"
    assert isinstance(doc["languages"], list)

    # Resolve again with new review snapshot
    review2 = _sample_review(review_id="r-test-2")
    space2 = mongo_service.resolve_or_create_space(review2, owner_id="user-alice")

    assert space2.space_id == space1.space_id
    assert space2.review_count == 2
    assert space2.latest_review_id == "r-test-2"


def test_mongo_user_isolation_on_identical_pull_request(mongo_service, mongo_db):
    review = _sample_review()
    space_alice = mongo_service.resolve_or_create_space(review, owner_id="user-alice")
    space_bob = mongo_service.resolve_or_create_space(review, owner_id="user-bob")

    assert space_alice.space_id != space_bob.space_id
    assert "alice" in space_alice.space_id
    assert "bob" in space_bob.space_id

    # Alice cannot list or view Bob's space
    alice_spaces = mongo_service.list_spaces(owner_id="user-alice")
    bob_spaces = mongo_service.list_spaces(owner_id="user-bob")

    assert [s.space_id for s in alice_spaces] == [space_alice.space_id]
    assert [s.space_id for s in bob_spaces] == [space_bob.space_id]

    assert mongo_service.get_space(space_bob.space_id, owner_id="user-alice") is None
    assert mongo_service.get_space(space_alice.space_id, owner_id="user-bob") is None


def test_mongo_concept_and_exercise_generation(mongo_service):
    review = _sample_review()
    space = mongo_service.resolve_or_create_space(review, owner_id="user-c")

    concepts = mongo_service.list_concepts(space.space_id, owner_id="user-c")
    assert len(concepts) == 4
    languages = {c.language for c in concepts}
    assert languages == {"c", "cpp", "java", "python"}

    exercises = mongo_service.list_exercises(space.space_id)
    assert len(exercises) == 4
    assert all(e.space_id == space.space_id for e in exercises)

    # Validation rules are present on internal model
    c_ex = next(e for e in exercises if e.language == "c")
    assert "forbidden" in c_ex.validation_rules
    assert "required_any" in c_ex.validation_rules


def test_mongo_draft_save_and_retrieve(mongo_service):
    review = _sample_review()
    space = mongo_service.resolve_or_create_space(review, owner_id="user-draft")
    exercise = mongo_service.list_exercises(space.space_id)[0]

    draft1 = mongo_service.save_draft(exercise.exercise_id, "user-draft", "code draft 1")
    assert draft1.code == "code draft 1"

    loaded = mongo_service.get_draft(exercise.exercise_id, "user-draft")
    assert loaded is not None
    assert loaded.code == "code draft 1"

    # Updating draft overwrites without creating duplicates
    mongo_service.save_draft(exercise.exercise_id, "user-draft", "code draft 2")
    updated = mongo_service.get_draft(exercise.exercise_id, "user-draft")
    assert updated.code == "code draft 2"

    # Other user does not see this draft
    assert mongo_service.get_draft(exercise.exercise_id, "other-user") is None


def test_mongo_submission_evaluation_and_concept_status_transition(mongo_service):
    review = _sample_review()
    space = mongo_service.resolve_or_create_space(review, owner_id="user-eval")
    c_exercise = next(e for e in mongo_service.list_exercises(space.space_id) if e.language == "c")

    # 1. Failing submission (contains forbidden strcpy)
    bad_code = "void safe_copy(const char *source, char *destination, size_t dest_size) { strcpy(destination, source); }"
    sub1 = mongo_service.submit_exercise(c_exercise.exercise_id, "user-eval", bad_code)

    assert sub1.passed is False
    assert sub1.status == "failed"
    assert "Vulnerable pattern still present" in sub1.feedback

    concept = mongo_service.get_concept(c_exercise.concept_id, owner_id="user-eval")
    assert concept.status == "in_progress"

    # 2. Passing submission
    import time
    time.sleep(0.01)
    good_code = "void safe_copy(const char *source, char *destination, size_t dest_size) { snprintf(destination, dest_size, \"%s\", source); }"
    sub2 = mongo_service.submit_exercise(c_exercise.exercise_id, "user-eval", good_code)

    assert sub2.passed is True
    assert sub2.status == "completed"
    assert "Static verification confirmed" in sub2.feedback

    concept = mongo_service.get_concept(c_exercise.concept_id, owner_id="user-eval")
    assert concept.status == "mastered"

    # Submissions history
    subs = mongo_service.list_submissions(c_exercise.exercise_id, "user-eval")
    assert len(subs) == 2
    assert subs[0].submission_id == sub2.submission_id
    assert subs[1].submission_id == sub1.submission_id


def test_finding_a_exercise_access_ownership_verification(mongo_service, monkeypatch):
    """Verify Finding A: list_space_exercises verifies space ownership."""
    review = _sample_review()
    space_alice = mongo_service.resolve_or_create_space(review, owner_id="user-alice")

    # Alice can list exercises
    alice_exercises = mongo_service.list_exercises(space_alice.space_id, owner_id="user-alice")
    assert len(alice_exercises) == 4

    # Bob passing his own owner_id cannot list exercises for Alice's space
    bob_exercises = mongo_service.list_exercises(space_alice.space_id, owner_id="user-bob")
    assert bob_exercises == []

    # API route test: Bob requests Alice's space exercises -> 404
    monkeypatch.setattr("app.routes.learning.get_learning_service", lambda: mongo_service)

    def bob_auth():
        return {"user_id": "user-bob", "email": "bob@example.com"}

    app.dependency_overrides[current_user] = bob_auth
    client = TestClient(app)
    try:
        resp = client.get(f"/api/learning/spaces/{space_alice.space_id}/exercises")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Learning space not found."

        # Alice requests Alice's space exercises -> 200
        def alice_auth():
            return {"user_id": "user-alice", "email": "alice@example.com"}

        app.dependency_overrides[current_user] = alice_auth
        resp = client.get(f"/api/learning/spaces/{space_alice.space_id}/exercises")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 4
        # Public schema does NOT leak validation_rules
        assert "validation_rules" not in data[0]
    finally:
        app.dependency_overrides.clear()


def test_finding_b_dashboard_metrics_scoped_to_user(mongo_service):
    """Verify Finding B: Dashboard metrics are strictly scoped to the authenticated user."""
    # Alice has 1 space with 4 concepts and passes 1 exercise
    review_a = _sample_review(owner="alice-org", repo="alice-repo", pr_num=10)
    space_a = mongo_service.resolve_or_create_space(review_a, owner_id="user-alice")
    ex_a = mongo_service.list_exercises(space_a.space_id)[0]
    mongo_service.submit_exercise(
        ex_a.exercise_id,
        "user-alice",
        "void safe_copy(const char *source, char *destination, size_t dest_size) { snprintf(destination, dest_size, \"%s\", source); }",
    )

    # Bob has 1 space with 4 concepts and passes 0 exercises
    review_b = _sample_review(owner="bob-org", repo="bob-repo", pr_num=20)
    space_b = mongo_service.resolve_or_create_space(review_b, owner_id="user-bob")

    # Alice's dashboard
    dash_a = mongo_service.get_dashboard(user_id="user-alice")
    assert dash_a.total_spaces == 1
    assert dash_a.total_concepts == 4
    assert dash_a.mastered_concepts == 1
    assert dash_a.total_exercises == 4
    assert dash_a.completed_exercises == 1
    assert len(dash_a.active_spaces) == 1
    assert dash_a.active_spaces[0].space_id == space_a.space_id

    # Bob's dashboard
    dash_b = mongo_service.get_dashboard(user_id="user-bob")
    assert dash_b.total_spaces == 1
    assert dash_b.total_concepts == 4
    assert dash_b.mastered_concepts == 0
    assert dash_b.total_exercises == 4
    assert dash_b.completed_exercises == 0
    assert len(dash_b.active_spaces) == 1
    assert dash_b.active_spaces[0].space_id == space_b.space_id


def test_validation_rules_never_exposed_via_public_api(mongo_service, monkeypatch):
    review = _sample_review()
    space = mongo_service.resolve_or_create_space(review, owner_id="user-sec")
    exercises = mongo_service.list_exercises(space.space_id)

    monkeypatch.setattr("app.routes.learning.get_learning_service", lambda: mongo_service)

    def user_auth():
        return {"user_id": "user-sec", "email": "sec@example.com"}

    app.dependency_overrides[current_user] = user_auth
    client = TestClient(app)
    try:
        # Endpoint 1: list exercises
        res1 = client.get(f"/api/learning/spaces/{space.space_id}/exercises")
        assert res1.status_code == 200
        for item in res1.json():
            assert "validation_rules" not in item

        # Endpoint 2: single exercise
        ex_id = exercises[0].exercise_id
        res2 = client.get(f"/api/learning/coding-exercises/{ex_id}")
        assert res2.status_code == 200
        assert "validation_rules" not in res2.json()
    finally:
        app.dependency_overrides.clear()


def test_mongo_anonymous_space_resolution_and_deduplication(mongo_service, mongo_db):
    """Verify owner_id=None (anonymous) space resolution and review snapshot deduplication."""
    review1 = _sample_review(review_id="r-anon-1")
    space1 = mongo_service.resolve_or_create_space(review1, owner_id=None)

    assert space1.canonical_repo == "octocat/hello-world"
    assert space1.pr_number == 101
    assert space1.review_count == 1
    assert space1.latest_review_id == "r-anon-1"

    # Verify document in MongoDB
    doc = mongo_db["learning_spaces"].find_one({"space_id": space1.space_id})
    assert doc is not None
    assert doc["owner_id"] is None
    assert doc["review_count"] == 1

    # Second snapshot with owner_id=None deduplicates to the same space
    review2 = _sample_review(review_id="r-anon-2")
    space2 = mongo_service.resolve_or_create_space(review2, owner_id=None)

    assert space2.space_id == space1.space_id
    assert space2.review_count == 2
    assert space2.latest_review_id == "r-anon-2"
    assert mongo_db["learning_spaces"].count_documents({}) == 1


def test_mongo_anonymous_and_authenticated_owner_isolation(mongo_service, mongo_db):
    """Verify complete isolation between anonymous (owner_id=None) and authenticated spaces."""
    review = _sample_review()
    space_anon = mongo_service.resolve_or_create_space(review, owner_id=None)
    space_alice = mongo_service.resolve_or_create_space(review, owner_id="user-alice")

    assert space_anon.space_id != space_alice.space_id

    doc_anon = mongo_db["learning_spaces"].find_one({"space_id": space_anon.space_id})
    assert doc_anon is not None
    assert doc_anon["owner_id"] is None

    doc_alice = mongo_db["learning_spaces"].find_one({"space_id": space_alice.space_id})
    assert doc_alice is not None
    assert doc_alice["owner_id"] == "user-alice"

    # Listing as Alice does not reveal the anonymous space
    alice_spaces = mongo_service.list_spaces(owner_id="user-alice")
    assert len(alice_spaces) == 1
    assert alice_spaces[0].space_id == space_alice.space_id

    # Alice cannot get the anonymous space with her owner_id filter
    assert mongo_service.get_space(space_anon.space_id, owner_id="user-alice") is None

    # Getting anonymous space with owner_id=None succeeds
    anon_fetched = mongo_service.get_space(space_anon.space_id, owner_id=None)
    assert anon_fetched is not None
    assert anon_fetched.space_id == space_anon.space_id

    assert mongo_db["learning_spaces"].count_documents({}) == 2


def test_mongo_anonymous_unique_index_semantics(mongo_db):
    """Verify unique compound index enforcement on (canonical_repo, pr_number, owner_id)."""
    now = datetime.now(UTC)
    doc_anon_1 = {
        "space_id": "space-anon-1",
        "canonical_repo": "octocat/hello-world",
        "pr_number": 101,
        "pr_title": "PR 101",
        "pr_url": "https://github.com/octocat/hello-world/pull/101",
        "latest_review_id": "r-1",
        "review_count": 1,
        "primary_language": "python",
        "languages": ["python"],
        "created_at": now,
        "updated_at": now,
        "owner_id": None,
    }
    mongo_db["learning_spaces"].insert_one(doc_anon_1)

    # Inserting a second document with identical (canonical_repo, pr_number, owner_id=None) violates unique index
    import pymongo.errors
    doc_anon_2 = dict(doc_anon_1)
    doc_anon_2.pop("_id", None)
    doc_anon_2["space_id"] = "space-anon-2"
    with pytest.raises((pymongo.errors.DuplicateKeyError, Exception)):
        mongo_db["learning_spaces"].insert_one(doc_anon_2)

    # Inserting document with same repo/pr but authenticated owner succeeds
    doc_auth = dict(doc_anon_1)
    doc_auth.pop("_id", None)
    doc_auth["space_id"] = "space-alice-1"
    doc_auth["owner_id"] = "user-alice"
    mongo_db["learning_spaces"].insert_one(doc_auth)
    assert mongo_db["learning_spaces"].count_documents({}) == 2

    # Inserting second document for same authenticated owner violates unique index
    doc_auth_2 = dict(doc_auth)
    doc_auth_2.pop("_id", None)
    doc_auth_2["space_id"] = "space-alice-2"
    with pytest.raises((pymongo.errors.DuplicateKeyError, Exception)):
        mongo_db["learning_spaces"].insert_one(doc_auth_2)
