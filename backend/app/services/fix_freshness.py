"""Verify suggested-fix file snapshots against the current PR head.

Freshness is reported separately from conflict relationships. This module only
reads GitHub source; it does not modify or rewrite proposals.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter, defaultdict
from typing import Any

from app.github_service.client import GitHubAPIError
from app.github_service.pull_requests import (
    MAX_SOURCE_BYTES,
    fetch_current_pull_request_file_change,
    fetch_current_pull_request_head_sha,
    get_file_content,
)
from app.schemas.issue import CanonicalIssue, SuggestedFix

MAX_FIXES_TO_VERIFY = 25
MAX_FRESHNESS_SECONDS = 90.0
MAX_GITHUB_OPERATION_SECONDS = 15.0
_SHA_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


async def verify_fix_freshness(
    issues: list[CanonicalIssue],
    *,
    owner: str,
    repo: str,
    pull_request_number: int,
) -> dict[str, Any]:
    """Report whether each bounded proposal matches its file at the latest PR head.

    The current head is fetched without the GitHub client's 60-second cache.
    Source files are fetched once per unique path at that immutable SHA. A head
    change alone is not stale: the recorded file blob hash is the comparison.
    """
    proposals = [
        (issue.issue_id, fix)
        for issue in issues
        for fix in issue.suggested_fixes
    ]
    proposals.sort(key=lambda item: (item[1].file_path, item[0], item[1].fix_id))
    if not proposals:
        return {
            "status": "not_needed", "proposal_count": 0, "current_count": 0,
            "stale_count": 0, "unverifiable_count": 0, "file_fetch_count": 0,
            "checked_head_sha": None, "per_proposal": [],
        }

    outcomes: dict[tuple[str, str], dict[str, Any]] = {}
    eligible_by_path: dict[str, list[tuple[str, SuggestedFix]]] = defaultdict(list)
    for issue_id, fix in proposals:
        reference = (issue_id, fix.fix_id)
        file_blob_sha = fix.file_blob_sha
        if (
            not _is_sha(fix.base_commit_sha)
            or not isinstance(file_blob_sha, str)
            or not _is_sha(file_blob_sha)
        ):
            outcomes[reference] = _outcome(issue_id, fix, "unverifiable", "Required source snapshot metadata is missing or invalid.")
        else:
            eligible_by_path[fix.file_path].append((issue_id, fix))

    proposal_cap = MAX_FIXES_TO_VERIFY
    allowed_paths: set[str] = set()
    allowed_refs: set[tuple[str, str]] = set()
    for issue_id, fix in proposals[:proposal_cap]:
        reference = (issue_id, fix.fix_id)
        if reference in outcomes:
            continue
        allowed_paths.add(fix.file_path)
        allowed_refs.add(reference)
    for issue_id, fix in proposals[proposal_cap:]:
        reference = (issue_id, fix.fix_id)
        if reference not in outcomes:
            outcomes[reference] = _outcome(issue_id, fix, "unverifiable", "Per-review freshness verification limit reached.")

    active_paths = {
        path: [item for item in items if (item[0], item[1].fix_id) in allowed_refs]
        for path, items in eligible_by_path.items()
        if path in allowed_paths
    }
    started = asyncio.get_running_loop().time()
    if not active_paths:
        return _summarize(proposals, outcomes, None, 0)

    head_error: str | None = None
    head_sha: str | None = None
    try:
        head_sha = await asyncio.wait_for(
            fetch_current_pull_request_head_sha(owner, repo, pull_request_number),
            timeout=MAX_GITHUB_OPERATION_SECONDS,
        )
        if not _is_sha(head_sha):
            head_error = "GitHub did not return a valid current PR head snapshot."
    except (GitHubAPIError, TimeoutError, ValueError, OSError) as exc:
        head_error = _safe_github_failure(exc, "The current PR head could not be retrieved.")

    if head_error is not None or head_sha is None:
        reason = head_error or "The current PR head could not be retrieved."
        for items in active_paths.values():
            for issue_id, fix in items:
                outcomes[(issue_id, fix.fix_id)] = _outcome(issue_id, fix, "unverifiable", reason)
        return _summarize(proposals, outcomes, None, 0)

    file_fetch_count = 0
    for path, items in active_paths.items():
        remaining = MAX_FRESHNESS_SECONDS - (asyncio.get_running_loop().time() - started)
        if remaining <= 0:
            reason = "Per-review freshness verification time budget reached."
            for issue_id, fix in items:
                outcomes[(issue_id, fix.fix_id)] = _outcome(issue_id, fix, "unverifiable", reason)
            continue

        file_fetch_count += 1
        try:
            source = await asyncio.wait_for(
                get_file_content(owner, repo, path, head_sha),
                timeout=min(MAX_GITHUB_OPERATION_SECONDS, remaining),
            )
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                # The source read may have consumed most of the shared
                # deadline; recalculate before the confirmation lookup.
                remaining = MAX_FRESHNESS_SECONDS - (asyncio.get_running_loop().time() - started)
                if remaining <= 0:
                    change = None
                else:
                    change = await _current_path_change(owner, repo, pull_request_number, path, remaining)
                if change in {"deleted", "renamed"}:
                    for issue_id, fix in items:
                        outcomes[(issue_id, fix.fix_id)] = _outcome(
                            issue_id, fix, "stale", f"The target file was {change} in the current PR."
                        )
                else:
                    reason = "GitHub could not confirm whether the target file was deleted or renamed."
                    for issue_id, fix in items:
                        outcomes[(issue_id, fix.fix_id)] = _outcome(issue_id, fix, "unverifiable", reason)
            else:
                reason = _safe_github_failure(exc, "GitHub source retrieval failed.")
                for issue_id, fix in items:
                    outcomes[(issue_id, fix.fix_id)] = _outcome(issue_id, fix, "unverifiable", reason)
            continue
        except (TimeoutError, ValueError, OSError) as exc:
            reason = _safe_github_failure(exc, "GitHub source retrieval failed.")
            for issue_id, fix in items:
                outcomes[(issue_id, fix.fix_id)] = _outcome(issue_id, fix, "unverifiable", reason)
            continue

        if not isinstance(source, str) or "\x00" in source:
            reason = "The current source file is inaccessible, oversized, or not valid UTF-8 text."
            for issue_id, fix in items:
                outcomes[(issue_id, fix.fix_id)] = _outcome(issue_id, fix, "unverifiable", reason)
            continue
        source_bytes = source.encode("utf-8")
        if len(source_bytes) > MAX_SOURCE_BYTES:
            reason = "The current source file exceeds the existing retrieval size limit."
            for issue_id, fix in items:
                outcomes[(issue_id, fix.fix_id)] = _outcome(issue_id, fix, "unverifiable", reason)
            continue

        current_blob_sha = _git_blob_sha(source_bytes)
        for issue_id, fix in items:
            recorded_blob_sha = fix.file_blob_sha
            if not isinstance(recorded_blob_sha, str) or not _is_sha(recorded_blob_sha):
                outcomes[(issue_id, fix.fix_id)] = _outcome(
                    issue_id, fix, "unverifiable", "Required source blob SHA is missing or invalid."
                )
                continue
            status = "current" if current_blob_sha.casefold() == recorded_blob_sha.casefold() else "stale"
            reason = (
                "The current file blob matches the proposal's recorded source snapshot."
                if status == "current"
                else "The target file content differs from the proposal's recorded source snapshot."
            )
            outcomes[(issue_id, fix.fix_id)] = _outcome(issue_id, fix, status, reason)

    return _summarize(proposals, outcomes, head_sha, file_fetch_count)


async def _current_path_change(
    owner: str,
    repo: str,
    pull_request_number: int,
    path: str,
    remaining: float,
) -> str | None:
    try:
        return await asyncio.wait_for(
            fetch_current_pull_request_file_change(owner, repo, pull_request_number, path),
            timeout=min(MAX_GITHUB_OPERATION_SECONDS, max(0.001, remaining)),
        )
    except (GitHubAPIError, TimeoutError, ValueError, OSError):
        return None


def _is_sha(value: str | None) -> bool:
    return isinstance(value, str) and _SHA_PATTERN.fullmatch(value) is not None


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def _safe_github_failure(exc: Exception, fallback: str) -> str:
    if isinstance(exc, GitHubAPIError):
        if exc.status_code == 429 or exc.kind in {"primary", "secondary"}:
            return "GitHub rate limits prevented freshness verification."
        if exc.status_code in {401, 403}:
            return "GitHub access was insufficient for freshness verification."
    if isinstance(exc, asyncio.TimeoutError):
        return "GitHub freshness verification timed out."
    return fallback


def _outcome(issue_id: str, fix: SuggestedFix, status: str, reason: str) -> dict[str, Any]:
    return {
        "canonical_issue_id": issue_id,
        "fix_id": fix.fix_id,
        "status": status,
        "reason": reason,
    }


def _summarize(
    proposals: list[tuple[str, SuggestedFix]],
    outcomes: dict[tuple[str, str], dict[str, Any]],
    head_sha: str | None,
    file_fetch_count: int,
) -> dict[str, Any]:
    ordered = [outcomes[(issue_id, fix.fix_id)] for issue_id, fix in proposals]
    counts = Counter(item["status"] for item in ordered)
    unverifiable_count = counts.get("unverifiable", 0)
    determinate_count = counts.get("current", 0) + counts.get("stale", 0)
    if not unverifiable_count:
        status = "completed"
    elif determinate_count:
        status = "partial"
    else:
        status = "unavailable"
    return {
        "status": status,
        "proposal_count": len(proposals),
        "current_count": counts.get("current", 0),
        "stale_count": counts.get("stale", 0),
        "unverifiable_count": unverifiable_count,
        "outcome_counts": dict(counts),
        "checked_head_sha": head_sha,
        "file_fetch_count": file_fetch_count,
        "per_proposal": ordered,
    }
