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


def test_standardized_educational_content_and_progressive_hints():
    service = LearningSpaceService()
    review = sample_review("multi", "project", 5).model_dump(mode="json")
    review["canonical_issues"] = [
        {
            "issue_id": "issue-c-1",
            "file_path": "src/buffer.c",
            "category": "security",
            "normalized_description": "Unsafe gets() function allows buffer overflow",
        }
    ]
    space = service.resolve_or_create_space(review, owner_id="user-edu")
    exercises = service.list_exercises(space.space_id)
    assert len(exercises) >= 1
    ex = exercises[0]

    # Verify 8-part beginner-friendly standard
    assert ex.title == "Safe String Copying with Buffer Bounds Checking"
    assert ex.real_world_analogy and len(ex.real_world_analogy) > 20
    assert "pitcher into a" in ex.real_world_analogy.lower() or "glass" in ex.real_world_analogy.lower()
    assert ex.problem_explanation and "overflow" in ex.problem_explanation.lower()
    assert ex.mission and "refactor" in ex.mission.lower()
    assert ex.example_input is not None
    assert ex.example_output is not None
    assert len(ex.hints) >= 3
    assert "Hint 1:" in ex.hints[0]
    assert "Hint 2:" in ex.hints[1]
    assert "Hint 3:" in ex.hints[2]
    assert ex.explanation and "cwe-120" in ex.explanation.lower() or "bounded" in ex.explanation.lower()
    assert len(ex.completion_criteria) >= 3


def test_unused_variable_issue_generates_matching_cleanup_exercise():
    service = LearningSpaceService()
    review = sample_review("py-project", "unused-test", 7).model_dump(mode="json")
    review["canonical_issues"] = [
        {
            "issue_id": "issue-unused-1",
            "file_path": "test_2.py",
            "category": "code_quality",
            "normalized_description": "Local variable `unused_variable` is assigned to but never used",
        }
    ]
    space = service.resolve_or_create_space(review, owner_id="user-unused")
    exercises = service.list_exercises(space.space_id)
    assert len(exercises) >= 1
    ex = exercises[0]

    # Verify the exercise title, code, and validation rules match the unused_variable issue
    assert ex.title == "Clean Up Unused Local Variables"
    assert "unused_variable" in ex.starter_code
    assert "unused_variable =" in ex.validation_rules["forbidden"]
    assert ex.category == "code_quality"
    assert ex.difficulty == "beginner"
    assert ex.real_world_analogy and "counter" in ex.real_world_analogy.lower()

    # Test submission
    failing_sub = service.submit_exercise(ex.exercise_id, "user-unused", ex.starter_code)
    assert failing_sub.passed is False

    clean_code = """def calculate_total(prices: list[float], tax_rate: float) -> float:
    subtotal = sum(prices)
    total = subtotal * (1 + tax_rate)
    return total
"""
    passing_sub = service.submit_exercise(ex.exercise_id, "user-unused", clean_code)
    assert passing_sub.passed is True


def test_generator_fallback_and_topic_isolation():
    service = LearningSpaceService()

    # 1. Unused variable
    ex1 = service._create_exercise_for_issue(
        "ex-1", "space-1", "concept-1",
        {"issue_id": "i-unused", "normalized_description": "Unused local variable count", "file_path": "util.py"},
        "python",
    )
    assert ex1.title == "Clean Up Unused Local Variables"
    assert "unused_variable" in ex1.starter_code
    assert "unused_variable =" in ex1.validation_rules.get("forbidden", [])

    # 2. Subprocess / Command Injection
    ex2 = service._create_exercise_for_issue(
        "ex-2", "space-1", "concept-2",
        {"issue_id": "i-cmd", "normalized_description": "Command injection via subprocess shell=True", "file_path": "runner.py"},
        "python",
    )
    assert ex2.title == "Preventing Command Injection in Subprocess Execution"
    assert "subprocess" in ex2.starter_code
    assert "shell=True" in ex2.validation_rules.get("forbidden", [])

    # 2b. os.system and popen command execution vulnerabilities
    ex_ossys = service._create_exercise_for_issue(
        "ex-ossys", "space-1", "concept-ossys",
        {"issue_id": "i-ossys", "normalized_description": "os.system call invokes external shell unsafely", "file_path": "script.py"},
        "python",
    )
    assert ex_ossys.title == "Preventing Command Injection in Subprocess Execution"
    assert "subprocess" in ex_ossys.starter_code

    ex_popen = service._create_exercise_for_issue(
        "ex-popen", "space-1", "concept-popen",
        {"issue_id": "i-popen", "normalized_description": "Unvalidated popen call allows command execution", "file_path": "launcher.py"},
        "python",
    )
    assert ex_popen.title == "Preventing Command Injection in Subprocess Execution"

    # 2c. Unrelated uses of word 'command' must map to generic defensive-input fallback
    ex_cqs = service._create_exercise_for_issue(
        "ex-cqs", "space-1", "concept-cqs",
        {"issue_id": "i-cqs", "normalized_description": "Follow Command-Query Separation principle", "file_path": "service.py"},
        "python",
    )
    assert ex_cqs.title == "Defensive Input Validation & Error Handling"
    assert "subprocess" not in ex_cqs.starter_code
    assert "shell=True" not in str(ex_cqs.validation_rules)
    assert service._concept_title_for_issue({"normalized_description": "Follow Command-Query Separation"}, "python") != "Safe Subprocess & Argument Separation"

    ex_cli = service._create_exercise_for_issue(
        "ex-cli", "space-1", "concept-cli",
        {"issue_id": "i-cli", "normalized_description": "Refactor CLI command flags", "file_path": "cli.py"},
        "python",
    )
    assert ex_cli.title == "Defensive Input Validation & Error Handling"
    assert "subprocess" not in ex_cli.starter_code
    assert "shell=True" not in str(ex_cli.validation_rules)
    assert service._concept_title_for_issue({"normalized_description": "Refactor CLI command flags"}, "python") != "Safe Subprocess & Argument Separation"

    # 3. Buffer overflow
    ex3 = service._create_exercise_for_issue(
        "ex-3", "space-1", "concept-3",
        {"issue_id": "i-buf", "normalized_description": "Unsafe gets() function allows buffer overflow", "file_path": "buf.c"},
        "c",
    )
    assert ex3.title == "Safe String Copying with Buffer Bounds Checking"
    assert "gets(" in ex3.validation_rules.get("forbidden", [])

    # 4. Exception slicing
    ex4 = service._create_exercise_for_issue(
        "ex-4", "space-1", "concept-4",
        {"issue_id": "i-exc", "normalized_description": "Exception slicing catch (std::exception e)", "file_path": "engine.cpp"},
        "cpp",
    )
    assert ex4.title == "Catching Exceptions by Const Reference"
    assert "std::exception e)" in ex4.validation_rules.get("forbidden", [])

    # 5. SQL injection
    ex5 = service._create_exercise_for_issue(
        "ex-5", "space-1", "concept-5",
        {"issue_id": "i-sql", "normalized_description": "SQL injection in executeQuery concatenation", "file_path": "Dao.java"},
        "java",
    )
    assert ex5.title == "Preventing SQL Injection with Prepared Statements"
    assert "prepareStatement" in ex5.validation_rules.get("required_any", [])

    # 6. Path traversal (must not map to command injection or subprocess)
    ex6 = service._create_exercise_for_issue(
        "ex-6", "space-1", "concept-6",
        {"issue_id": "i-path", "normalized_description": "Path traversal vulnerability allows directory escape", "file_path": "fs.py"},
        "python",
    )
    assert ex6.title == "Guarding File Access Against Path Traversal Escapes"
    assert "is_relative_to" in ex6.validation_rules.get("required", [])
    assert "subprocess" not in ex6.starter_code
    assert "shell=True" not in str(ex6.validation_rules)

    # 7. Missing description (None)
    ex7 = service._create_exercise_for_issue(
        "ex-7", "space-1", "concept-7",
        {"issue_id": "i-none", "normalized_description": None, "file_path": "mod.py"},
        "python",
    )
    assert ex7.title == "Defensive Input Validation & Error Handling"
    assert "subprocess" not in ex7.starter_code
    assert "shell=True" not in str(ex7.validation_rules)

    # 8. Empty description ("")
    ex8 = service._create_exercise_for_issue(
        "ex-8", "space-1", "concept-8",
        {"issue_id": "i-empty", "normalized_description": "", "file_path": "mod.py"},
        "python",
    )
    assert ex8.title == "Defensive Input Validation & Error Handling"
    assert "subprocess" not in ex8.starter_code
    assert "shell=True" not in str(ex8.validation_rules)

    # 9. Unknown issue type (e.g. cyclomatic complexity, missing docstrings)
    ex9 = service._create_exercise_for_issue(
        "ex-9", "space-1", "concept-9",
        {"issue_id": "i-complex", "category": "complexity", "normalized_description": "Cyclomatic complexity exceeds 15", "file_path": "parser.py"},
        "python",
    )
    assert ex9.title == "Defensive Input Validation & Error Handling"
    assert "subprocess" not in ex9.starter_code
    assert "shell=True" not in str(ex9.validation_rules)

    # 10. Malformed issue metadata ({})
    ex10 = service._create_exercise_for_issue(
        "ex-10", "space-1", "concept-10",
        {},
        "python",
    )
    assert ex10.title == "Defensive Input Validation & Error Handling"
    assert "subprocess" not in ex10.starter_code
    assert "shell=True" not in str(ex10.validation_rules)

    # Verify submission evaluation for generic fallback exercise
    service.store.save_exercises([ex7])
    fail_sub = service.submit_exercise(ex7.exercise_id, "user-test", "def sanitize_input(val):\n    pass\n")
    assert fail_sub.passed is False

    pass_code = """def sanitize_input(value: str | None) -> str:
    if not value:
        return ""
    return value.strip()
"""
    pass_sub = service.submit_exercise(ex7.exercise_id, "user-test", pass_code)
    assert pass_sub.passed is True


def test_learning_defensive_input_verification_precision():
    """Reproduce and verify the production issue where unguarded .strip() previously passed.

    Verify that:
    1. Submitting starter code (unguarded return value.strip()) FAILS.
    2. Submitting strip() called before the guard FAILS.
    3. Submitting invalid Python syntax FAILS with a clear syntax error.
    4. Submitting properly guarded code PASSES with structural AST confirmation.
    5. eval_details accurately conveys static limitations and mandatory checks.
    """
    service = LearningSpaceService()
    review = sample_review("owner", "repo", 42).model_dump(mode="json")
    review["canonical_issues"] = [
        {
            "issue_id": "issue-def-1",
            "file_path": "util.py",
            "category": "code_quality",
            "normalized_description": "Input validation missing",
        }
    ]
    space = service.resolve_or_create_space(review, owner_id="user-prec")
    exercises = service.list_exercises(space.space_id)
    assert len(exercises) >= 1
    ex = exercises[0]
    assert ex.title == "Defensive Input Validation & Error Handling"

    # 1. Starter code should not contain the answer and MUST fail submission
    assert "if not value" not in ex.starter_code
    sub_starter = service.submit_exercise(ex.exercise_id, "user-prec", ex.starter_code)
    assert sub_starter.passed is False
    assert sub_starter.status == "failed"
    assert "Missing defensive input guard" in sub_starter.feedback

    # 2. Retaining unguarded .strip() before guard MUST fail
    unguarded_strip = """def sanitize_input(value: str | None) -> str:
    res = value.strip()
    if not value:
        return ""
    return res
"""
    sub_unguarded = service.submit_exercise(ex.exercise_id, "user-prec", unguarded_strip)
    assert sub_unguarded.passed is False
    assert "Unguarded .strip() called before defensive input check" in sub_unguarded.feedback

    # 3. Invalid syntax MUST fail
    bad_syntax = """def sanitize_input(value: str | None) -> str:
    if not value
        return ""
    return value.strip()
"""
    sub_syntax = service.submit_exercise(ex.exercise_id, "user-prec", bad_syntax)
    assert sub_syntax.passed is False
    assert "Syntax error" in sub_syntax.feedback

    # 4. None-only guard MUST fail because empty strings are not handled
    none_only = """def sanitize_input(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()
"""
    sub_none_only = service.submit_exercise(ex.exercise_id, "user-prec", none_only)
    assert sub_none_only.passed is False
    assert sub_none_only.status == "failed"
    assert "does not safely handle empty string input" in sub_none_only.feedback

    # 5. Empty-string-only guard MUST fail because None is not handled
    empty_only = """def sanitize_input(value: str | None) -> str:
    if value == "":
        return ""
    return value.strip()
"""
    sub_empty_only = service.submit_exercise(ex.exercise_id, "user-prec", empty_only)
    assert sub_empty_only.passed is False
    assert sub_empty_only.status == "failed"
    assert "does not safely handle None/null input" in sub_empty_only.feedback

    # 6. Valid guarded code using 'not value' MUST pass (handles both None and "")
    good_code = """def sanitize_input(value: str | None) -> str:
    if not value:
        return ""
    return value.strip()
"""
    sub_good = service.submit_exercise(ex.exercise_id, "user-prec", good_code)
    assert sub_good.passed is True
    assert sub_good.status == "completed"
    assert "Static verification confirmed" in sub_good.feedback
    assert "Runtime test sandbox execution is disabled" in sub_good.feedback
    assert "Valid Python syntax" in sub_good.eval_details["mandatory_checks_passed"]
    assert any("safely handling both None and empty string" in s for s in sub_good.eval_details["mandatory_checks_passed"])

    # 7. Valid guarded code using explicit disjunction ('value is None or value == ""') MUST pass
    good_explicit = """def sanitize_input(value: str | None) -> str:
    if value is None or value == "":
        return ""
    return value.strip()
"""
    sub_explicit = service.submit_exercise(ex.exercise_id, "user-prec", good_explicit)
    assert sub_explicit.passed is True
    assert sub_explicit.status == "completed"




