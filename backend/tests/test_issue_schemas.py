import pytest
from pydantic import ValidationError

from app.schemas.finding import ReviewFinding
from app.schemas.issue import CanonicalIssue, FindingObservation


def _finding(**overrides):
    values = {
        "id": "finding-1",
        "title": "Avoid unsafe query construction",
        "description": "User input is interpolated into a query.",
        "file_path": "src/data.py",
        "line_start": 12,
        "line_end": 13,
        "severity": "high",
        "category": "security",
        "confidence": 0.91,
        "source": "ai_candidate",
        "verification_status": "needs_review",
        "evidence": ["query = f\"SELECT * FROM t WHERE id={user_id}\""],
        "why_it_matters": "Untrusted input can change query meaning.",
        "suggested_fix": "Use a parameterized query.",
        "learning_explanation": "Keep query syntax separate from data.",
        "limitations": ["The database driver is not shown."],
    }
    values.update(overrides)
    return ReviewFinding.model_validate(values)


def test_finding_observation_preserves_original_finding_and_provenance():
    original = _finding()
    observation = FindingObservation(
        original_finding=original,
        tool_name="review-model",
        provider_name="configured-llm-provider",
        rule_id="SEC-QUERY-01",
    )

    dumped = observation.model_dump(mode="json")
    assert dumped["original_finding"]["id"] == "finding-1"
    assert dumped["original_finding"]["source"] == "ai_candidate"
    assert dumped["original_finding"]["description"] == original.description
    assert dumped["original_finding"]["evidence"] == original.evidence
    assert dumped["original_finding"]["confidence"] == original.confidence
    assert dumped["original_finding"]["line_start"] == 12
    assert dumped["original_finding"]["line_end"] == 13
    assert dumped["tool_name"] == "review-model"
    assert dumped["provider_name"] == "configured-llm-provider"
    assert dumped["rule_id"] == "SEC-QUERY-01"


def test_canonical_issue_links_multiple_observations_and_defaults_open():
    first = FindingObservation(original_finding=_finding())
    second = FindingObservation(
        original_finding=_finding(id="finding-2", source="static_analysis", title="Bandit B608"),
        tool_name="bandit",
        rule_id="B608",
    )

    issue = CanonicalIssue(
        issue_id="issue-1",
        file_path="src/data.py",
        line_start=12,
        line_end=13,
        category="security",
        severity="high",
        normalized_description="Untrusted input is interpolated into a database query.",
        observations=[first, second],
    )

    assert issue.status == "open"
    assert len(issue.observations) == 2
    assert issue.observations[0].original_finding.id == "finding-1"
    assert issue.observations[1].original_finding.source == "static_analysis"


@pytest.mark.parametrize("line_start,line_end", [(0, 1), (4, 3)])
def test_canonical_issue_rejects_invalid_line_ranges(line_start, line_end):
    with pytest.raises(ValidationError):
        CanonicalIssue(
            issue_id="issue-invalid-line",
            file_path="src/data.py",
            line_start=line_start,
            line_end=line_end,
            category="bug",
            severity="medium",
            normalized_description="An issue.",
        )


def test_canonical_issue_rejects_unknown_status_and_extra_fields():
    base = {
        "issue_id": "issue-invalid-status",
        "file_path": "src/data.py",
        "category": "bug",
        "severity": "medium",
        "normalized_description": "An issue.",
    }
    with pytest.raises(ValidationError):
        CanonicalIssue(**base, status="accepted")
    with pytest.raises(ValidationError):
        CanonicalIssue(**base, unexpected="value")


def test_existing_review_finding_contract_remains_unchanged():
    finding = _finding()
    assert finding.model_dump()["id"] == "finding-1"
    assert finding.source == "ai_candidate"
