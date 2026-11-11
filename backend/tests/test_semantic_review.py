import asyncio
import json
from datetime import UTC, datetime

from app.ai_review import client
from app.ai_review.semantic_review import review_fix_semantics
from app.schemas.issue import CanonicalIssue, SuggestedFix


def records():
    issue = CanonicalIssue(
        issue_id="issue-1",
        file_path="src/a.py",
        category="bug",
        severity="medium",
        normalized_description="Handle empty inputs.",
    )
    fix = SuggestedFix(
        fix_id="fix-1",
        canonical_issue_id="issue-1",
        file_path="src/a.py",
        start_line=1,
        end_line=1,
        language="python",
        base_commit_sha="a" * 40,
        expected_original_code="return items[0]",
        proposed_code="return items[0] if items else None",
        change_summary="Handle empty list",
        rationale="Avoid an index error.",
        confidence=0.8,
        generation_status="generated",
        generated_at=datetime.now(UTC),
    )
    return issue, fix


def test_semantic_review_returns_structured_opinion_without_claiming_verification(
    monkeypatch,
):
    issue, fix = records()
    response = {
        "issue_coverage": "addresses_issue",
        "logical_risks": [],
        "behavior_changes": [],
        "missed_edge_cases": ["Callers may rely on IndexError for empty lists."],
        "generated_test_coverage": "not_available",
        "evidence_conflicts": [],
        "conclusion": "concerns_found",
        "rationale": "The proposal changes empty-input behavior.",
        "limitations": ["Call-site expectations were not reviewed."],
    }

    async def complete(*_args, **_kwargs):
        return json.dumps(response)

    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "complete_json", complete)
    monkeypatch.setattr(
        "app.ai_review.semantic_review.last_call_metadata",
        lambda: {
            "provider": "review-fallback",
            "model": "semantic-v2",
            "fallback_used": True,
            "usage": {"total_tokens": 42},
            "model_configuration": {"provider": "review-fallback", "model": "semantic-v2"},
        },
    )
    result = asyncio.run(
        review_fix_semantics(
            issue, fix, tool_results=[{"tool": "ruff", "status": "completed"}]
        )
    )
    assert result["status"] == "completed"
    assert result["assessment"]["conclusion"] == "concerns_found"
    assert result["assessment"]["missed_edge_cases"]
    assert result["limitations"]
    assert result["provider"] == "review-fallback"
    assert result["model"] == "semantic-v2"
    assert result["fallback_used"] is True
    assert result["usage"]["total_tokens"] == 42


def test_semantic_review_fails_closed_when_provider_unavailable(monkeypatch):
    issue, fix = records()
    monkeypatch.setattr(client, "is_configured", lambda: False)
    result = asyncio.run(review_fix_semantics(issue, fix, tool_results=[]))
    assert result["status"] == "unavailable"
    assert result["assessment"] is None
