from copy import deepcopy

import mongomock
import pytest
from pydantic import ValidationError

from app.data.sample_reviews import sample_review
from app.database.mongo_reviews import MongoReviewStore
from app.database.reviews import SQLiteReviewStore
from app.schemas.issue import CanonicalIssue, SuggestedFix
from app.schemas.review import ReviewResponse

BASE_FIX = {
    "fix_id": "fix-001",
    "canonical_issue_id": "issue-001",
    "file_path": "src/query.py",
    "start_line": 10,
    "end_line": 11,
    "language": "python",
    "base_commit_sha": "a" * 40,
    "file_blob_sha": "b" * 40,
    "expected_original_code": "query = f\"SELECT * FROM t WHERE id={user_id}\"\nrun(query)",
    "proposed_code": "query = \"SELECT * FROM t WHERE id = ?\"\nrun(query, (user_id,))",
    "change_summary": "Parameterize the query value.",
    "rationale": "Separating SQL syntax from user data prevents interpolation.",
    "limitations": ["The database driver's parameter style was not verified."],
    "confidence": 0.87,
    "generation_status": "generated",
    "decision_status": "pending",
    "generated_at": "2026-11-06T12:00:00Z",
}


def _issue(**overrides):
    values = {
        "issue_id": "issue-001",
        "file_path": "src/query.py",
        "line_start": 10,
        "line_end": 11,
        "category": "security",
        "severity": "high",
        "normalized_description": "User input is interpolated into a query.",
    }
    values.update(overrides)
    return CanonicalIssue.model_validate(values)


def test_valid_suggested_fix_has_bounded_python_replacement_contract():
    fix = SuggestedFix.model_validate(BASE_FIX)

    assert fix.fix_id == "fix-001"
    assert fix.canonical_issue_id == "issue-001"
    assert fix.language == "python"
    assert fix.generation_status == "generated"
    assert fix.decision_status == "pending"
    assert fix.generated_at.utcoffset().total_seconds() == 0
    assert fix.decided_at is None


@pytest.mark.parametrize("field", [
    "fix_id", "canonical_issue_id", "file_path", "start_line", "end_line", "language",
    "base_commit_sha", "expected_original_code", "proposed_code", "change_summary",
    "rationale", "confidence", "generation_status", "generated_at",
])
def test_suggested_fix_rejects_missing_required_fields(field):
    values = deepcopy(BASE_FIX)
    values.pop(field)

    with pytest.raises(ValidationError):
        SuggestedFix.model_validate(values)


@pytest.mark.parametrize(("start_line", "end_line", "original"), [
    (0, 1, "line"), (3, 2, "line"), (5, 6, "only one line"), (True, 1, "line"),
])
def test_suggested_fix_rejects_invalid_or_noncontiguous_ranges(start_line, end_line, original):
    values = {**BASE_FIX, "start_line": start_line, "end_line": end_line, "expected_original_code": original}

    with pytest.raises(ValidationError):
        SuggestedFix.model_validate(values)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), float("inf"), True, "0.5"])
def test_suggested_fix_rejects_invalid_confidence(confidence):
    with pytest.raises(ValidationError):
        SuggestedFix.model_validate({**BASE_FIX, "confidence": confidence})


@pytest.mark.parametrize("language", ["javascript", "Python", "python3", ""])
def test_suggested_fix_rejects_unsupported_languages(language):
    with pytest.raises(ValidationError):
        SuggestedFix.model_validate({**BASE_FIX, "language": language})


@pytest.mark.parametrize(("field", "value"), [
    ("expected_original_code", "x" * 20_001),
    ("proposed_code", "x" * 20_001),
    ("change_summary", "x" * 501),
    ("rationale", "x" * 2_001),
    ("limitations", ["ok"] * 6),
    ("limitations", ["x" * 501]),
])
def test_suggested_fix_rejects_oversized_code_and_text(field, value):
    with pytest.raises(ValidationError):
        SuggestedFix.model_validate({**BASE_FIX, field: value})


@pytest.mark.parametrize(("field", "value"), [
    ("generation_status", "pending"), ("generation_status", "unavailable"),
    ("generation_status", "failed"), ("decision_status", "conflicted"),
    ("decision_status", "applied"),
])
def test_suggested_fix_rejects_states_outside_the_proposal_contract(field, value):
    with pytest.raises(ValidationError):
        SuggestedFix.model_validate({**BASE_FIX, field: value})


@pytest.mark.parametrize("generation_status", ["generated", "stale", "conflicted"])
def test_suggested_fix_accepts_proposal_lifecycle_states(generation_status):
    fix = SuggestedFix.model_validate({**BASE_FIX, "generation_status": generation_status})
    assert fix.generation_status == generation_status


def test_source_snapshot_requires_base_commit_but_file_blob_hash_is_optional():
    fix = SuggestedFix.model_validate({**BASE_FIX, "file_blob_sha": None})
    assert fix.base_commit_sha == "a" * 40
    assert fix.file_blob_sha is None

    for field, value in (("base_commit_sha", "short"), ("file_blob_sha", "not-a-hash")):
        with pytest.raises(ValidationError):
            SuggestedFix.model_validate({**BASE_FIX, field: value})


@pytest.mark.parametrize("file_path", ["", "   ", "/tmp/file.py", "../secret.py", "src/../secret.py", "src\\file.py"])
def test_suggested_fix_rejects_invalid_repository_paths(file_path):
    with pytest.raises(ValidationError):
        SuggestedFix.model_validate({**BASE_FIX, "file_path": file_path})


def test_canonical_issue_accepts_multiple_fixes_and_defaults_to_no_fixes():
    first = {**BASE_FIX, "fix_id": "fix-001"}
    second = {**BASE_FIX, "fix_id": "fix-002", "start_line": 20, "end_line": 20, "expected_original_code": "old", "proposed_code": "new"}

    issue = _issue(suggested_fixes=[first, second])
    without_fixes = _issue()

    assert [fix.fix_id for fix in issue.suggested_fixes] == ["fix-001", "fix-002"]
    assert without_fixes.suggested_fixes == []
    with pytest.raises(ValidationError):
        _issue(suggested_fixes=[
            {**BASE_FIX, "fix_id": f"fix-{index}", "start_line": 20 + index, "end_line": 20 + index,
             "expected_original_code": "old", "proposed_code": "new"}
            for index in range(6)
        ])


def test_canonical_issue_rejects_mismatched_or_duplicate_fix_ids():
    with pytest.raises(ValidationError, match="reference its containing"):
        _issue(suggested_fixes=[{**BASE_FIX, "canonical_issue_id": "another-issue"}])
    with pytest.raises(ValidationError, match="unique within"):
        _issue(suggested_fixes=[BASE_FIX, BASE_FIX])


def test_decision_state_is_independent_from_canonical_issue_status():
    accepted = _issue(suggested_fixes=[{**BASE_FIX, "decision_status": "accepted"}])
    dismissed_issue = _issue(status="dismissed", suggested_fixes=[{**BASE_FIX, "decision_status": "pending"}])

    assert accepted.status == "open"
    assert accepted.suggested_fixes[0].decision_status == "accepted"
    assert dismissed_issue.status == "dismissed"
    assert dismissed_issue.suggested_fixes[0].decision_status == "pending"


def test_legacy_review_deserializes_without_fabricating_issues_or_fixes():
    legacy = sample_review("owner", "legacy", 42).model_dump(mode="json")
    legacy.pop("canonical_issues", None)
    legacy.pop("unique_issue_count", None)
    legacy.pop("raw_observation_count", None)

    restored = ReviewResponse.model_validate(legacy)

    assert restored.findings == sample_review("owner", "legacy", 42).findings
    assert restored.canonical_issues == []
    assert restored.raw_observation_count == len(legacy["findings"])


def test_suggested_fix_serialization_round_trip_preserves_decision_and_snapshot():
    issue = _issue(suggested_fixes=[{**BASE_FIX, "decision_status": "rejected", "decided_at": "2026-11-06T13:00:00Z"}])

    restored = CanonicalIssue.model_validate(issue.model_dump(mode="json"))

    assert restored.suggested_fixes[0].model_dump(mode="json") == issue.suggested_fixes[0].model_dump(mode="json")
    assert restored.suggested_fixes[0].file_blob_sha == "b" * 40
    assert restored.suggested_fixes[0].decision_status == "rejected"

    review = sample_review("owner", "roundtrip", 7)
    payload = review.model_dump(mode="json")
    payload["canonical_issues"] = [issue.model_dump(mode="json")]
    payload["unique_issue_count"] = 1
    payload["raw_observation_count"] = 0
    review_restored = ReviewResponse.model_validate(payload)
    assert review_restored.canonical_issues[0].suggested_fixes[0].fix_id == "fix-001"


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_existing_stores_round_trip_nested_suggested_fix(backend, tmp_path, monkeypatch):
    issue = _issue(suggested_fixes=[BASE_FIX])
    record = {
        "review_id": f"fix-contract-{backend}",
        "created_at": "2026-11-06T12:00:00Z",
        "findings": [],
        "canonical_issues": [issue.model_dump(mode="json")],
    }
    if backend == "sqlite":
        monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "fix-contract.sqlite3"))
        store = SQLiteReviewStore()
    else:
        store = MongoReviewStore(mongomock.MongoClient(tz_aware=True)["fix_contract_test"])

    store.save_review(record)
    retrieved = store.get_review(record["review_id"])
    restored = CanonicalIssue.model_validate(retrieved["canonical_issues"][0])

    assert restored.suggested_fixes[0].fix_id == "fix-001"
    assert restored.suggested_fixes[0].expected_original_code == BASE_FIX["expected_original_code"]
