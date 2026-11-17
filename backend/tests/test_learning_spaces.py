"""Unit and integration tests for PR-connected Learning Lab spaces and coding exercises."""

import pytest

from app.data.sample_reviews import sample_review
from app.schemas.issue import CanonicalIssue
from app.services.learning_spaces import (
    LearningSpaceService,
    get_learning_service,
    normalize_canonical_repo_pr,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_learning.sqlite3"
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(test_db))


def test_canonical_repo_pr_normalization():
    assert normalize_canonical_repo_pr("https://github.com/owner/repo/pull/42") == (
        "owner/repo",
        42,
        "https://github.com/owner/repo/pull/42",
    )
    # Case insensitivity
    assert normalize_canonical_repo_pr("https://github.com/OWNER/Repo/pull/42") == (
        "owner/repo",
        42,
        "https://github.com/owner/repo/pull/42",
    )
    # Trailing slash
    assert normalize_canonical_repo_pr("https://github.com/owner/repo/pull/42/") == (
        "owner/repo",
        42,
        "https://github.com/owner/repo/pull/42",
    )
    # SSH repo URL + PR number
    assert normalize_canonical_repo_pr("git@github.com:owner/repo.git", 42) == (
        "owner/repo",
        42,
        "https://github.com/owner/repo/pull/42",
    )


def test_space_creation_and_deduplication(tmp_path):
    service = LearningSpaceService()
    review = sample_review("octocat", "hello-world", 101).model_dump(mode="json")

    # 1. Create first space from review
    space1 = service.resolve_or_create_space(review, owner_id="user-1")
    assert space1.canonical_repo == "octocat/hello-world"
    assert space1.pr_number == 101
    assert space1.review_count == 1

    # 2. Re-resolve with same PR URL: must deduplicate and increment review count
    review2 = sample_review("octocat", "hello-world", 101).model_dump(mode="json")
    review2["review_id"] = "review-2"
    space2 = service.resolve_or_create_space(review2, owner_id="user-1")

    assert space2.space_id == space1.space_id
    assert space2.review_count == 2
    assert space2.latest_review_id == "review-2"


def test_concept_and_exercise_generation_across_languages():
    service = LearningSpaceService()

    # Create multi-language issues
    c_issue = {
        "issue_id": "issue-c-1",
        "file_path": "src/buffer.c",
        "category": "security",
        "normalized_description": "Unsafe gets() function allows buffer overflow",
    }
    cpp_issue = {
        "issue_id": "issue-cpp-1",
        "file_path": "src/engine.cpp",
        "category": "bug",
        "normalized_description": "Exception slicing catch (std::exception e)",
    }
    java_issue = {
        "issue_id": "issue-java-1",
        "file_path": "src/UserDao.java",
        "category": "security",
        "normalized_description": "SQL injection in executeQuery concatenation",
    }
    py_issue = {
        "issue_id": "issue-py-1",
        "file_path": "src/runner.py",
        "category": "security",
        "normalized_description": "Command injection via subprocess shell=True",
    }

    review = sample_review("multi", "project", 5).model_dump(mode="json")
    review["canonical_issues"] = [c_issue, cpp_issue, java_issue, py_issue]

    space = service.resolve_or_create_space(review, owner_id="user-multi")

    concepts = service.list_concepts(space.space_id, "user-multi")
    assert len(concepts) == 4

    concept_langs = {c.language for c in concepts}
    assert {"c", "cpp", "java", "python"}.issubset(concept_langs)

    exercises = service.list_exercises(space.space_id)
    assert len(exercises) == 4

    c_ex = next(e for e in exercises if e.language == "c")
    assert "safe_copy" in c_ex.starter_code
    assert "gets(" in c_ex.validation_rules["forbidden"]

    cpp_ex = next(e for e in exercises if e.language == "cpp")
    assert "const std::exception&" in cpp_ex.validation_rules.get("required_any", cpp_ex.validation_rules.get("required", []))

    java_ex = next(e for e in exercises if e.language == "java")
    assert "prepareStatement" in java_ex.validation_rules.get("required_any", java_ex.validation_rules.get("required", []))


def test_draft_saving_and_retrieval():
    service = LearningSpaceService()
    exercise_id = "ex-test-draft-1"
    user_id = "user-alice"

    # Save draft
    saved = service.save_draft(exercise_id, user_id, "int main() { return 0; }")
    assert saved.code == "int main() { return 0; }"

    # Retrieve draft
    retrieved = service.get_draft(exercise_id, user_id)
    assert retrieved is not None
    assert retrieved.code == "int main() { return 0; }"

    # Different user has no draft
    assert service.get_draft(exercise_id, "user-bob") is None


def test_exercise_submission_and_truthful_static_verification():
    service = LearningSpaceService()

    c_issue = {
        "issue_id": "issue-c-sub",
        "file_path": "src/net.c",
        "category": "security",
        "normalized_description": "Unsafe gets() function allows buffer overflow",
    }
    review = sample_review("sec", "project", 1).model_dump(mode="json")
    review["canonical_issues"] = [c_issue]
    space = service.resolve_or_create_space(review, owner_id="user-sub")

    exercises = service.list_exercises(space.space_id)
    assert len(exercises) >= 1
    ex = exercises[0]

    # 1. Failing submission (still has gets())
    bad_code = """void safe_copy(const char *src, char *dst, size_t sz) {
    gets(dst);
}"""
    sub_fail = service.submit_exercise(ex.exercise_id, "user-sub", bad_code)
    assert sub_fail.passed is False
    assert sub_fail.status == "failed"
    assert "Vulnerable pattern still present" in sub_fail.feedback

    # 2. Passing submission (uses snprintf)
    good_code = """void safe_copy(const char *src, char *dst, size_t sz) {
    snprintf(dst, sz, "%s", src);
}"""
    sub_pass = service.submit_exercise(ex.exercise_id, "user-sub", good_code)
    assert sub_pass.passed is True
    assert sub_pass.status == "completed"
    assert "Static verification confirmed" in sub_pass.feedback
    assert "Runtime test sandbox execution is disabled" in sub_pass.feedback

    # Submissions history
    history = service.list_submissions(ex.exercise_id, "user-sub")
    assert len(history) == 2
    assert history[0].passed is True  # Latest submission first


def test_dashboard_metrics_aggregation():
    service = LearningSpaceService()

    # Seed space and completed exercise
    review = sample_review("user-dash", "repo-dash", 5).model_dump(mode="json")
    review["canonical_issues"] = [
        {
            "issue_id": "issue-dash-1",
            "file_path": "main.py",
            "category": "security",
            "normalized_description": "SQL injection vulnerability in query",
        }
    ]
    space = service.resolve_or_create_space(review, owner_id="user-sub")
    exercises = service.list_exercises(space.space_id)
    assert len(exercises) >= 1
    # Submitting with parameterized pattern
    service.submit_exercise(exercises[0].exercise_id, "user-sub", "cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))")

    dash = service.get_dashboard("user-sub")

    assert dash.total_spaces >= 1
    assert dash.total_concepts >= 1
    assert dash.total_exercises >= 1
    assert len(dash.active_spaces) >= 1
