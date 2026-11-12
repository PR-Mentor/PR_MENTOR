"""Re-evaluate a SuggestedFix against verified immutable GitHub snapshots."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal

from app.github_service.client import GitHubAPIError
from app.github_service.pull_requests import (
    GitHubFileSnapshot,
    GitHubFileSnapshotError,
    fetch_current_pull_request_file_change,
    fetch_current_pull_request_head_sha,
    fetch_file_snapshot,
)
from app.schemas.issue import FixValidationSnapshot, SuggestedFix
from app.verification.fix_snapshot import reconstruct_proposed_source

MAX_ELIGIBILITY_SECONDS = 45.0
MAX_GITHUB_OPERATION_SECONDS = 15.0
_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")

EligibilityStatus = Literal["eligible", "stale", "conflicted", "unavailable"]


@dataclass(frozen=True)
class FixEligibilityResult:
    status: EligibilityStatus
    reason: str
    snapshot: FixValidationSnapshot | None = None
    baseline_source: str | None = None
    proposed_source: str | None = None


async def evaluate_fix_eligibility(
    fix: SuggestedFix,
    *,
    owner: str,
    repo: str,
    pull_request_number: int,
) -> FixEligibilityResult:
    """Verify fix origin and target at one freshly retrieved PR head.

    This function is read-only. A newer PR head remains eligible when the
    target file is byte-identical and the expected replacement still matches.
    """
    try:
        return await asyncio.wait_for(
            _evaluate_fix_eligibility(
                fix,
                owner=owner,
                repo=repo,
                pull_request_number=pull_request_number,
            ),
            timeout=MAX_ELIGIBILITY_SECONDS,
        )
    except TimeoutError:
        return _result(
            "unavailable", "Fix eligibility verification exceeded its time limit."
        )


async def _evaluate_fix_eligibility(
    fix: SuggestedFix,
    *,
    owner: str,
    repo: str,
    pull_request_number: int,
) -> FixEligibilityResult:
    if (
        not _valid_repository_component(owner)
        or not _valid_repository_component(repo)
        or not isinstance(pull_request_number, int)
        or isinstance(pull_request_number, bool)
        or pull_request_number < 1
    ):
        return _result("unavailable", "Repository or pull request identity is invalid.")
    file_blob_sha = fix.file_blob_sha
    base_commit_sha = fix.base_commit_sha
    if not isinstance(base_commit_sha, str) or not isinstance(file_blob_sha, str):
        return _result(
            "unavailable",
            "The proposal lacks a valid original commit or file blob SHA.",
        )
    if (
        not _valid_sha(base_commit_sha)
        or not _valid_sha(file_blob_sha)
    ):
        return _result(
            "unavailable",
            "The proposal lacks a valid original commit or file blob SHA.",
        )

    try:
        head_sha = await asyncio.wait_for(
            fetch_current_pull_request_head_sha(owner, repo, pull_request_number),
            timeout=MAX_GITHUB_OPERATION_SECONDS,
        )
        if not isinstance(head_sha, str) or not _valid_sha(head_sha):
            return _result(
                "unavailable", "GitHub did not return a full current PR head SHA."
            )
        try:
            original = await _fetch(owner, repo, fix.file_path, base_commit_sha)
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return _result(
                    "unavailable",
                    "GitHub could not verify access to the proposal's original "
                    "source commit.",
                )
            raise
        if original.blob_sha.casefold() != file_blob_sha.casefold():
            return _result(
                "conflicted",
                "The original source blob does not match the proposal's "
                "recorded blob SHA.",
            )
        try:
            reconstruct_proposed_source(original.text, fix)
        except GitHubFileSnapshotError as exc:
            return _result("conflicted", str(exc))

        if head_sha.casefold() == base_commit_sha.casefold():
            current = original
        else:
            try:
                current = await _fetch(owner, repo, fix.file_path, head_sha)
            except GitHubAPIError as exc:
                if exc.status_code != 404:
                    raise
                return await _classify_missing_current_path(
                    owner, repo, pull_request_number, fix.file_path
                )
            except GitHubFileSnapshotError as exc:
                if "does not exist at the requested commit" not in str(exc):
                    raise
                return await _classify_missing_current_path(
                    owner, repo, pull_request_number, fix.file_path
                )
        try:
            proposed = reconstruct_proposed_source(current.text, fix)
        except GitHubFileSnapshotError as exc:
            return _result(
                "stale",
                f"The current PR source no longer supports this replacement: {exc}",
            )

        snapshot = FixValidationSnapshot(
            file_path=fix.file_path,
            base_commit_sha=fix.base_commit_sha,
            checked_head_sha=head_sha,
            baseline_blob_sha=current.blob_sha,
            proposed_blob_sha=proposed.blob_sha,
        )
        reason = (
            "The fix matches the verified source snapshot at the current PR head."
            if current.blob_sha.casefold() == original.blob_sha.casefold()
            else (
                "The PR head changed, but the original replacement still "
                "matches the current file range."
            )
        )
        return FixEligibilityResult(
            "eligible", reason, snapshot, current.text, proposed.text
        )
    except TimeoutError:
        return _result(
            "unavailable", "Fix eligibility verification exceeded its time limit."
        )
    except asyncio.CancelledError:
        raise
    except GitHubAPIError as exc:
        if exc.status_code in {401, 403, 429}:
            return _result(
                "unavailable",
                "GitHub access or rate limits prevented source verification.",
            )
        return _result(
            "unavailable", "GitHub could not provide the requested source snapshot."
        )
    except (GitHubFileSnapshotError, UnicodeError, ValueError) as exc:
        return _result("unavailable", str(exc))
    except (RuntimeError, OSError, TypeError):
        return _result(
            "unavailable",
            "An unexpected source retrieval failure prevented eligibility "
            "verification.",
        )


async def _fetch(
    owner: str, repo: str, path: str, commit_sha: str
) -> GitHubFileSnapshot:
    return await asyncio.wait_for(
        fetch_file_snapshot(owner, repo, path, commit_sha),
        timeout=MAX_GITHUB_OPERATION_SECONDS,
    )


async def _classify_missing_current_path(
    owner: str, repo: str, pull_request_number: int, path: str
) -> FixEligibilityResult:
    try:
        change = await asyncio.wait_for(
            fetch_current_pull_request_file_change(
                owner, repo, pull_request_number, path
            ),
            timeout=MAX_GITHUB_OPERATION_SECONDS,
        )
    except TimeoutError:
        return _result(
            "unavailable", "GitHub could not confirm why the current source is missing."
        )
    except (GitHubAPIError, OSError, RuntimeError):
        return _result(
            "unavailable", "GitHub could not confirm why the current source is missing."
        )
    if change in {"deleted", "renamed"}:
        return _result(
            "stale", f"The target file was {change} in the current pull request."
        )
    return _result(
        "unavailable",
        "GitHub could not confirm whether the current source was deleted or renamed.",
    )


def _valid_sha(value: str | None) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _valid_repository_component(value: str) -> bool:
    return (
        isinstance(value, str)
        and value not in {".", ".."}
        and re.fullmatch(r"[A-Za-z0-9_.-]+", value) is not None
    )


def _result(status: EligibilityStatus, reason: str) -> FixEligibilityResult:
    return FixEligibilityResult(status, reason)
