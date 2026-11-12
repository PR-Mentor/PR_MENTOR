"""Stable educational data; never describes or analyzes the submitted PR."""

from datetime import UTC, datetime
from hashlib import sha256

from app.schemas.finding import ReviewFinding
from app.schemas.github import PullRequestMetadata
from app.schemas.review import ReviewResponse


def sample_review(owner: str, repo: str, number: int) -> ReviewResponse:
    url = f"https://github.com/{owner}/{repo}/pull/{number}"
    items = [
        ReviewFinding(id="demo-empty-input", title="Consider the empty-input case", description="A calculation that divides by the collection length needs an explicit empty-input behavior.", file_path="src/example.py", line_start=8, line_end=8, severity="medium", category="bug", confidence=0.75, source="demo", verification_status="needs_review", evidence=["Illustrative teaching example; no submitted file was inspected."], why_it_matters="An empty collection can make the denominator zero.", suggested_fix="Choose and document an empty-input result, then add a focused test.", learning_explanation="Boundary cases are often useful review questions.", limitations=["This finding is sample content and is unrelated to the submitted pull request."]),
        ReviewFinding(id="demo-shell", title="Avoid shell interpretation of untrusted values", description="Passing untrusted text through a shell can allow command injection.", file_path="src/example.py", line_start=None, line_end=None, severity="high", category="security", confidence=0.8, source="demo", verification_status="needs_review", evidence=["Illustrative teaching example; no analyzer ran."], why_it_matters="Shell metacharacters can change the executed command.", suggested_fix="Use an argument list with shell disabled and validate the target.", learning_explanation="Argument vectors separate data from command syntax.", limitations=["This finding is sample content and is unrelated to the submitted pull request."]),
    ]
    return ReviewResponse(
        review_id="demo-" + sha256(url.encode()).hexdigest()[:16], status="completed", analysis_mode="demo", is_demo=True,
        pull_request=PullRequestMetadata(repository_owner=owner, repository_name=repo, repository_url=f"https://github.com/{owner}/{repo}", pull_request_number=number, pull_request_url=url, title="Illustrative demo review", description="Sample PR metadata. No repository was fetched.", author="demo", base_branch="main", head_branch="demo-example", head_sha="demo", state="sample", changed_files=1, additions=1, deletions=0),
        summary="Illustrative demo only. No submitted repository code or external analyzer was inspected.", findings=items,
        verification_summary={"verified": 0, "needs_review": len(items), "unsupported": 0},
        tool_results=[{"tool": "demo_dataset", "status": "sample", "executed": False, "reason": "Educational sample data only."}],
        warnings=["Demo data only. Findings do not describe the submitted pull request."], created_at=datetime.now(UTC), duration_ms=0, diff_files=[],
    )
