import json

import pytest
from pydantic import ValidationError

from app.schemas.issue import (
    MAX_CAVEAT_CHARS,
    MAX_CODE_EXAMPLE_CHARS,
    MAX_EXPLANATION_TEXT,
    CanonicalIssue,
    IssueCodeExample,
    IssueExplanation,
    LearningResource,
)
from app.schemas.review import ReviewResponse


def _explanation(**overrides):
    values = {
        "summary": "Untrusted input can change the query being executed.",
        "why_it_matters": "An attacker may read or modify data outside the intended request.",
        "root_cause": "SQL syntax and user-provided values are combined into one string.",
        "correction": "Use the database driver's parameter binding for user values.",
        "concept": "Parameterized queries separate SQL syntax from data.",
        "learning_resources": [
            {"title": "Python DB-API", "url": "https://docs.python.org/3/library/sqlite3.html", "description": "See parameter substitution."}
        ],
        "caveats": ["The database driver is not identified in the supplied code."],
    }
    values.update(overrides)
    return values


def _issue(**overrides):
    values = {
        "issue_id": "issue-1",
        "file_path": "src/data.py",
        "line_start": 12,
        "line_end": 13,
        "category": "security",
        "severity": "high",
        "normalized_description": "User input is interpolated into a query.",
        "observations": [],
    }
    values.update(overrides)
    return values


def test_valid_issue_explanation_and_optional_code_example():
    explanation = IssueExplanation.model_validate(_explanation(code_example={
        "before": "query = f'SELECT * FROM t WHERE id={user_id}'",
        "after": "cursor.execute('SELECT * FROM t WHERE id=?', (user_id,))",
        "language": "python",
    }))

    assert explanation.summary.startswith("Untrusted input")
    assert explanation.code_example is not None
    assert explanation.code_example.language == "python"
    assert explanation.learning_resources[0].url == "https://docs.python.org/3/library/sqlite3.html"


def test_code_example_is_optional():
    assert IssueExplanation.model_validate(_explanation()).code_example is None
    assert IssueCodeExample.model_validate({"before": "unsafe()", "after": "safe()"}).language is None


@pytest.mark.parametrize("value", [None, "missing"])
def test_canonical_issue_accepts_missing_or_null_explanation(value):
    payload = _issue()
    if value == "missing":
        issue = CanonicalIssue.model_validate(payload)
    else:
        payload["explanation"] = value
        issue = CanonicalIssue.model_validate(payload)

    assert issue.explanation is None


@pytest.mark.parametrize("field,value", [
    ("summary", 42),
    ("why_it_matters", None),
    ("root_cause", []),
    ("correction", {"text": "fix"}),
    ("concept", True),
    ("caveats", [4]),
    ("learning_resources", "not-a-list"),
    ("code_example", {"before": "only one side"}),
])
def test_invalid_explanation_field_types_are_rejected(field, value):
    payload = _explanation()
    payload[field] = value
    with pytest.raises(ValidationError):
        IssueExplanation.model_validate(payload)


@pytest.mark.parametrize("field,limit", [
    ("summary", MAX_EXPLANATION_TEXT),
    ("why_it_matters", MAX_EXPLANATION_TEXT),
    ("root_cause", MAX_EXPLANATION_TEXT),
    ("correction", MAX_EXPLANATION_TEXT),
    ("concept", MAX_EXPLANATION_TEXT),
])
def test_oversized_explanation_text_is_rejected(field, limit):
    with pytest.raises(ValidationError):
        IssueExplanation.model_validate(_explanation(**{field: "x" * (limit + 1)}))


def test_oversized_code_example_and_caveat_are_rejected():
    with pytest.raises(ValidationError):
        IssueCodeExample(before="x" * (MAX_CODE_EXAMPLE_CHARS + 1), after="safe()")
    with pytest.raises(ValidationError):
        IssueExplanation.model_validate(_explanation(caveats=["x" * (MAX_CAVEAT_CHARS + 1)]))


@pytest.mark.parametrize("resource", [
    {"title": "x" * 121, "url": "https://docs.python.org/3/"},
    {"title": "Python docs", "url": "https://docs.python.org/3/", "description": "x" * 301},
    {"title": "Python docs", "url": "https://docs.python.org/" + "x" * 480},
])
def test_oversized_learning_resource_fields_are_rejected(resource):
    with pytest.raises(ValidationError):
        LearningResource.model_validate(resource)


def test_resource_list_and_caveat_counts_are_bounded():
    resource = {"title": "Python docs", "url": "https://docs.python.org/3/"}
    with pytest.raises(ValidationError):
        IssueExplanation.model_validate(_explanation(learning_resources=[resource] * 6))
    with pytest.raises(ValidationError):
        IssueExplanation.model_validate(_explanation(caveats=["uncertain"] * 6))


@pytest.mark.parametrize("url", [
    "http://docs.python.org/3/",
    "https://docs.python.org.evil.example/",
    "https://user@docs.python.org/",
    "https://docs.python.org:444/",
    "https://docs.python.org/?next=https://evil.example/",
    "javascript:alert(1)",
    "https://docs.python.org/with whitespace",
])
def test_learning_resource_rejects_untrusted_or_unsafe_urls(url):
    with pytest.raises(ValidationError):
        LearningResource(title="Reference", url=url)


def test_canonical_issue_rejects_malformed_optional_explanation_instead_of_dropping_it():
    with pytest.raises(ValidationError):
        CanonicalIssue.model_validate(_issue(explanation={"summary": "incomplete"}))


def test_existing_canonical_issue_without_explanation_remains_compatible():
    issue = CanonicalIssue.model_validate(_issue())
    round_trip = CanonicalIssue.model_validate_json(issue.model_dump_json())
    assert round_trip.explanation is None
    assert round_trip.observations == []


def test_explanation_round_trips_through_canonical_issue_serialization():
    issue = CanonicalIssue.model_validate(_issue(explanation=_explanation()))
    serialized = issue.model_dump(mode="json")
    restored = CanonicalIssue.model_validate(json.loads(json.dumps(serialized)))

    assert restored == issue
    assert restored.explanation.learning_resources[0].title == "Python DB-API"


def test_legacy_review_remains_readable_without_fabricated_issues_or_explanations():
    from app.data.sample_reviews import sample_review

    legacy = sample_review("owner", "legacy", 17).model_dump(mode="python")
    legacy.pop("canonical_issues", None)
    legacy.pop("unique_issue_count", None)
    legacy.pop("raw_observation_count", None)
    response = ReviewResponse.model_validate(legacy)

    assert response.canonical_issues == []
    assert response.unique_issue_count is None
    assert len(response.findings) == len(legacy["findings"])
    assert all(issue.explanation is None for issue in response.canonical_issues)


def test_future_explanation_prompt_guidance_is_separate_from_candidate_prompt():
    from app.ai_review.prompts import (
        CANONICAL_ISSUE_EXPLANATION_GUIDANCE,
        SYSTEM_PROMPT,
    )

    assert "CanonicalIssue" not in SYSTEM_PROMPT
    assert "CanonicalIssue" in CANONICAL_ISSUE_EXPLANATION_GUIDANCE
    assert "FindingObservations" in CANONICAL_ISSUE_EXPLANATION_GUIDANCE
    assert "issue_id, explanation" in CANONICAL_ISSUE_EXPLANATION_GUIDANCE
    assert "Omit code_example" in CANONICAL_ISSUE_EXPLANATION_GUIDANCE
