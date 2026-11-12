from datetime import UTC, datetime

import pytest

from app.schemas.issue import CanonicalIssue, SuggestedFix
from app.services.fix_analysis import analyze_fix_relationships

BASE_SHA = "a" * 40
OTHER_SHA = "b" * 40
BLOB_SHA = "c" * 40
OTHER_BLOB_SHA = "d" * 40


def make_fix(
    fix_id: str,
    issue_id: str,
    *,
    path: str = "src/a.py",
    start: int = 1,
    end: int = 1,
    expected: str = "value = 1",
    proposed: str = "value = 2",
    base_sha: str = BASE_SHA,
    blob_sha: str | None = BLOB_SHA,
    decision: str = "pending",
) -> SuggestedFix:
    return SuggestedFix(
        fix_id=fix_id,
        canonical_issue_id=issue_id,
        file_path=path,
        start_line=start,
        end_line=end,
        language="python",
        base_commit_sha=base_sha,
        file_blob_sha=blob_sha,
        expected_original_code=expected,
        proposed_code=proposed,
        change_summary="Replace the reported source range.",
        rationale="The replacement addresses the reported issue.",
        limitations=[],
        confidence=0.8,
        generation_status="generated",
        decision_status=decision,
        generated_at=datetime.now(UTC),
    )


def make_issue(issue_id: str, *fixes: SuggestedFix) -> CanonicalIssue:
    return CanonicalIssue(
        issue_id=issue_id,
        file_path="src/a.py",
        line_start=None,
        line_end=None,
        category="code_quality",
        severity="medium",
        normalized_description="A reported issue.",
        suggested_fixes=list(fixes),
    )


def test_identical_proposals_are_reported_as_duplicates_and_preserved():
    accepted = make_fix("fix-a", "issue-a", decision="accepted")
    rejected = make_fix("fix-b", "issue-b", decision="rejected")
    issues = [make_issue("issue-a", accepted), make_issue("issue-b", rejected)]

    result = analyze_fix_relationships(issues)

    assert result["status"] == "completed"
    assert result["relationship_counts"] == {"duplicate": 1}
    relationship = result["relationships"][0]
    assert relationship["relationship_type"] == "duplicate"
    assert relationship["fix_ids"] == ["fix-a", "fix-b"]
    assert relationship["file_path"] == "src/a.py"
    assert [item["start_line"] for item in relationship["line_ranges"]] == [1, 1]
    assert "Both proposals are retained" in relationship["explanation"]
    assert accepted.generation_status == rejected.generation_status == "generated"
    assert accepted.decision_status == "accepted"
    assert rejected.decision_status == "rejected"
    assert len(issues[0].suggested_fixes) == len(issues[1].suggested_fixes) == 1


def test_identical_range_with_different_replacements_is_a_definite_conflict():
    first = make_fix("fix-a", "issue-a", proposed="value = 2", decision="accepted")
    second = make_fix("fix-b", "issue-b", proposed="value = 3", decision="rejected")

    result = analyze_fix_relationships([make_issue("issue-a", first), make_issue("issue-b", second)])

    assert result["relationship_counts"] == {"definite_conflict": 1}
    assert result["relationships"][0]["relationship_type"] == "definite_conflict"
    assert first.generation_status == second.generation_status == "conflicted"
    assert first.decision_status == "accepted"
    assert second.decision_status == "rejected"


def test_partially_overlapping_ranges_are_a_definite_conflict():
    first = make_fix(
        "fix-a", "issue-a", start=1, end=2, expected="a = 1\nb = 2", proposed="a = 3\nb = 4",
    )
    second = make_fix(
        "fix-b", "issue-b", start=2, end=3, expected="b = 2\nc = 3", proposed="b = 5\nc = 6",
    )

    result = analyze_fix_relationships([make_issue("issue-a", first), make_issue("issue-b", second)])

    assert result["relationship_counts"] == {"definite_conflict": 1}
    assert first.generation_status == second.generation_status == "conflicted"


@pytest.mark.parametrize(
    "first_args,second_args",
    [
        ({"start": 1, "end": 2, "expected": "a = 1\nb = 2"}, {"start": 3, "end": 3, "expected": "c = 3"}),
        ({"start": 1, "end": 1}, {"start": 5, "end": 5}),
    ],
)
def test_adjacent_and_disjoint_ranges_have_no_detected_conflict(first_args, second_args):
    first = make_fix("fix-a", "issue-a", **first_args)
    second = make_fix("fix-b", "issue-b", **second_args)

    result = analyze_fix_relationships([make_issue("issue-a", first), make_issue("issue-b", second)])

    assert result["relationship_counts"] == {"no_detected_conflict": 1}
    assert first.generation_status == second.generation_status == "generated"
    assert "not proof of independence" in result["relationships"][0]["explanation"]


def test_different_files_have_no_direct_conflict():
    first = make_fix("fix-a", "issue-a", path="src/a.py")
    second = make_fix("fix-b", "issue-b", path="src/b.py", proposed="value = 3")

    result = analyze_fix_relationships([make_issue("issue-a", first), make_issue("issue-b", second)])

    relationship = result["relationships"][0]
    assert relationship["relationship_type"] == "no_detected_conflict"
    assert relationship["detection_method"] == "different_file_paths"
    assert first.generation_status == second.generation_status == "generated"


@pytest.mark.parametrize(
    "first_snapshot,second_snapshot,expected_type",
    [
        ((BASE_SHA, BLOB_SHA), (OTHER_SHA, BLOB_SHA), "definite_conflict"),
        ((BASE_SHA, BLOB_SHA), (BASE_SHA, BLOB_SHA), "definite_conflict"),
        ((BASE_SHA, BLOB_SHA), (BASE_SHA, OTHER_BLOB_SHA), "incompatible_snapshot"),
        ((BASE_SHA, BLOB_SHA), (OTHER_SHA, OTHER_BLOB_SHA), "incompatible_snapshot"),
        ((BASE_SHA, None), (OTHER_SHA, None), "incompatible_snapshot"),
        ((BASE_SHA, None), (BASE_SHA, BLOB_SHA), "definite_conflict"),
    ],
)
def test_snapshot_compatibility_is_conservative(first_snapshot, second_snapshot, expected_type):
    first = make_fix("fix-a", "issue-a", proposed="value = 2", base_sha=first_snapshot[0], blob_sha=first_snapshot[1])
    second = make_fix("fix-b", "issue-b", proposed="value = 3", base_sha=second_snapshot[0], blob_sha=second_snapshot[1])

    result = analyze_fix_relationships([make_issue("issue-a", first), make_issue("issue-b", second)])

    relationship = result["relationships"][0]
    assert relationship["relationship_type"] == expected_type
    if expected_type == "incompatible_snapshot":
        assert result["status"] == "partial"
        assert first.generation_status == second.generation_status == "generated"
    else:
        assert result["status"] == "completed"
        assert first.generation_status == second.generation_status == "conflicted"


def test_multiple_proposals_on_one_issue_are_compared_without_removal():
    first = make_fix("fix-a", "issue-a", proposed="value = 2")
    second = make_fix("fix-b", "issue-a", proposed="value = 3")
    issue = make_issue("issue-a", first, second)

    result = analyze_fix_relationships([issue])

    assert result["relationship_counts"] == {"definite_conflict": 1}
    assert len(issue.suggested_fixes) == 2
    assert all(fix.generation_status == "conflicted" for fix in issue.suggested_fixes)


def test_no_proposals_are_reported_as_not_needed():
    result = analyze_fix_relationships([make_issue("issue-empty")])

    assert result["status"] == "not_needed"
    assert result["executed"] is False
    assert result["relationships"] == []


def test_analysis_caps_proposals_and_reports_partial_completion():
    fixes = [make_fix(f"fix-{index}", f"issue-{index}", start=index + 1, end=index + 1) for index in range(26)]
    issues = [make_issue(f"issue-{index}", fix) for index, fix in enumerate(fixes)]

    result = analyze_fix_relationships(issues)

    assert result["status"] == "partial"
    assert result["proposal_count"] == 26
    assert result["analyzed_proposal_count"] == 25
    assert result["skipped_proposal_count"] == 1
