"""Read-only, head-SHA-bound status for the PR Mentor test workflow."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.github_service.client import GitHubAPIError, async_client, request_json

WORKFLOW_NAME = "PR Tests"
WORKFLOW_FILE = "pr-tests.yml"
SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.I)


def _link(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    return value if re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}/actions/runs/[0-9]+(?:/attempts/[0-9]+)?", value) else None


async def get_pr_test_status(owner: str, repo: str, number: int, head_sha: str) -> dict[str, Any]:
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner) or not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo) or not isinstance(number, int) or isinstance(number, bool) or number < 1 or not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        return _unavailable(head_sha, "The reviewed pull request has no valid full head commit SHA.")
    path = f"/repos/{owner}/{repo}/actions/workflows/{WORKFLOW_FILE}/runs"
    try:
        async with async_client() as client:
            data = await request_json(client, path, {"head_sha": head_sha, "per_page": 20}, use_cache=False)
    except GitHubAPIError as exc:
        if exc.status_code == 404:
            return {**_unavailable(head_sha, "The PR Tests workflow is not configured or is not accessible."), "status": "not_configured"}
        state = "permission_error" if exc.status_code in {401, 403} else "rate_limited" if exc.status_code == 429 or exc.kind in {"primary", "secondary"} else "unavailable"
        messages = {"permission_error": "GitHub denied access to workflow status.", "rate_limited": "GitHub API rate limit reached; retry later.", "unavailable": "Could not retrieve the PR Tests workflow status from GitHub."}
        return {**_unavailable(head_sha, messages[state]), "status": state}
    except Exception:
        return _unavailable(head_sha, "Could not retrieve the PR Tests workflow status from GitHub.")
    if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
        return _unavailable(head_sha, "GitHub returned an invalid workflow status response.")
    runs = data["workflow_runs"][:20]
    matches = [r for r in runs if isinstance(r, dict) and isinstance(r.get("head_sha"), str) and r["head_sha"].lower() == head_sha.lower() and r.get("name") == WORKFLOW_NAME and r.get("path") in {".github/workflows/" + WORKFLOW_FILE, "" + WORKFLOW_FILE} and isinstance(r.get("id"), int) and isinstance(r.get("run_number"), int)]
    if not matches:
        return {**_unavailable(head_sha, "No PR Tests workflow run was found for this commit. Add .github/workflows/pr-tests.yml and open or update a pull request."), "status": "not_configured"}
    run = max(matches, key=lambda r: r.get("run_number", 0) if isinstance(r.get("run_number", 0), int) else 0)
    conclusion = run.get("conclusion")
    if run.get("status") == "queued" or run.get("status") == "requested": status = "queued"
    elif run.get("status") == "in_progress": status = "in_progress"
    elif conclusion == "success": status = "passed"
    elif conclusion == "failure": status = "failed"
    elif conclusion == "cancelled": status = "cancelled"
    elif conclusion == "timed_out": status = "timed_out"
    elif conclusion == "skipped": status = "skipped"
    else: status = "infrastructure_error" if conclusion else "unavailable"
    return {"status": status, "executed": conclusion in {"success", "failure", "cancelled", "timed_out"}, "head_sha": head_sha.lower(), "workflow": WORKFLOW_NAME, "conclusion": conclusion if isinstance(conclusion, str) and len(conclusion) <= 40 else None, "summary": f"GitHub Actions: {status.replace('_', ' ')}"[:300], "html_url": _link(run.get("html_url")), "updated_at": run.get("updated_at") if isinstance(run.get("updated_at"), str) and len(run["updated_at"]) <= 40 else None}


def _unavailable(sha: str, summary: str) -> dict[str, Any]:
    return {"status": "unavailable", "executed": False, "head_sha": sha.lower(), "workflow": WORKFLOW_NAME, "conclusion": None, "summary": summary, "html_url": None, "updated_at": None}
