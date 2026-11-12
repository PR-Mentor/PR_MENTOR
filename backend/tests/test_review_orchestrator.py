"""Tests for the live review coordinator's AI integration."""

import asyncio

from app.schemas.finding import ReviewFinding
from app.schemas.github import PullRequestBundle, PullRequestMetadata
from app.schemas.review import AnalyzeRequest
from app.services import review_orchestrator as service


def _bundle():
    metadata = PullRequestMetadata(
        repository_owner="acme",
        repository_name="widget",
        repository_url="https://github.com/acme/widget",
        pull_request_number=12,
        pull_request_url="https://github.com/acme/widget/pull/12",
        title="Improve parser",
        description="Handle edge cases",
        author="contributor",
        base_branch="main",
        head_branch="fix/parser",
        head_sha="head-sha",
        state="open",
        changed_files=0,
        additions=0,
        deletions=0,
    )
    return PullRequestBundle(metadata=metadata, files=[])


def test_live_orchestrator_includes_ai_candidates(monkeypatch):
    bundle = _bundle()
    finding = ReviewFinding(
        id="finding-1",
        title="Possible edge case",
        description="A branch may skip validation.",
        file_path="src/parser.py",
        severity="medium",
        category="bug",
        confidence=0.8,
        source="ai_candidate",
        verification_status="needs_review",
        evidence=["The diff adds a conditional."],
        why_it_matters="One input case may not be handled.",
        suggested_fix="Handle the alternate input explicitly.",
        learning_explanation="Check both sides of the condition.",
        limitations=["AI suggestion; human review required."],
    )

    async def fetch_pull_request(_url):
        return bundle

    async def generate_candidates(_metadata, _files, _verification):
        return [finding]

    monkeypatch.setattr(service, "fetch_pull_request", fetch_pull_request)
    monkeypatch.setattr(service, "is_configured", lambda: True)
    monkeypatch.setattr(service, "generate_candidates", generate_candidates)
    monkeypatch.setattr(
        service,
        "run_tests",
        lambda: {
            "status": "unavailable",
            "executed": False,
            "message": "Tests were not run.",
        },
    )
    monkeypatch.setattr(service, "_persist", lambda *_args: None)

    response = asyncio.run(
        service.analyze(
            AnalyzeRequest(
                url="https://github.com/acme/widget/pull/12",
                mode="live",
            ),
            user_id="test-user",
        )
    )

    assert response.analysis_mode == "live"
    assert response.findings == [finding]
    ai_result = next(item for item in response.tool_results if item["tool"] == "ai_review")
    assert ai_result["status"] == "completed"
    assert ai_result["executed"] is True
