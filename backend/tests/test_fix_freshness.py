import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest

from app.ai_review.fix_generation import _git_blob_sha
from app.github_service import client as github_client
from app.github_service import pull_requests
from app.github_service.client import GitHubAPIError, request_json
from app.schemas.issue import CanonicalIssue, SuggestedFix
from app.services import fix_freshness

BASE_SHA = "a" * 40
CURRENT_HEAD = "b" * 40
OTHER_HEAD = "c" * 40
SOURCE = "value = 1\n"


def make_fix(
    fix_id="fix-1",
    *,
    issue_id="issue-1",
    path="src/a.py",
    source=SOURCE,
    blob_sha=None,
    base_sha=BASE_SHA,
    generation_status="generated",
    decision_status="accepted",
):
    return SuggestedFix(
        fix_id=fix_id,
        canonical_issue_id=issue_id,
        file_path=path,
        start_line=1,
        end_line=1,
        language="python",
        base_commit_sha=base_sha,
        file_blob_sha=blob_sha or _git_blob_sha(source.encode("utf-8")),
        expected_original_code="value = 1",
        proposed_code="value = 2",
        change_summary="Update the value.",
        rationale="The replacement resolves the reported condition.",
        limitations=[],
        confidence=0.8,
        generation_status=generation_status,
        decision_status=decision_status,
        generated_at=datetime.now(UTC),
    )


def make_issue(*fixes, issue_id="issue-1"):
    return CanonicalIssue(
        issue_id=issue_id,
        file_path="src/a.py",
        category="code_quality",
        severity="medium",
        normalized_description="A source issue.",
        suggested_fixes=list(fixes),
    )


def setup_freshness(monkeypatch, *, head=CURRENT_HEAD, files=None, change=None, head_error=None):
    files = files or {}
    source_calls = []
    head_calls = []
    change_calls = []

    async def get_head(owner, repo, pull_number):
        head_calls.append((owner, repo, pull_number))
        if head_error:
            raise head_error
        return head

    async def get_source(owner, repo, path, ref):
        source_calls.append((owner, repo, path, ref))
        item = files.get(path)
        if isinstance(item, BaseException):
            raise item
        return item

    async def get_change(owner, repo, pull_number, path):
        change_calls.append((owner, repo, pull_number, path))
        return change

    monkeypatch.setattr(fix_freshness, "fetch_current_pull_request_head_sha", get_head)
    monkeypatch.setattr(fix_freshness, "get_file_content", get_source)
    monkeypatch.setattr(fix_freshness, "fetch_current_pull_request_file_change", get_change)
    return head_calls, source_calls, change_calls


def test_current_file_matches_recorded_snapshot(monkeypatch):
    fix = make_fix()
    before = fix.model_dump(mode="json")
    setup_freshness(monkeypatch, files={fix.file_path: SOURCE})

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=5,
    ))

    assert result["status"] == "completed"
    assert result["current_count"] == 1
    assert result["per_proposal"][0]["status"] == "current"
    assert fix.model_dump(mode="json") == before


def test_changed_pr_head_with_unchanged_target_file_remains_current(monkeypatch):
    fix = make_fix()
    head_calls, source_calls, _ = setup_freshness(monkeypatch, head=OTHER_HEAD, files={fix.file_path: SOURCE})

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=6,
    ))

    assert result["per_proposal"][0]["status"] == "current"
    assert result["checked_head_sha"] == OTHER_HEAD
    assert source_calls == [("owner", "repo", fix.file_path, OTHER_HEAD)]
    assert head_calls == [("owner", "repo", 6)]
    assert fix.base_commit_sha == BASE_SHA


@pytest.mark.parametrize("current_source", [
    "value = 1\nother = 1\n",  # Changed outside the proposal range.
    "value = 9\n",  # Changed target line.
])
def test_any_target_file_change_is_stale_even_if_target_line_is_unchanged(monkeypatch, current_source):
    fix = make_fix(source="value = 1\nother = 0\n")
    setup_freshness(monkeypatch, files={fix.file_path: current_source})

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=7,
    ))

    assert result["stale_count"] == 1
    assert result["per_proposal"][0]["status"] == "stale"
    assert fix.expected_original_code == "value = 1"
    assert fix.proposed_code == "value = 2"


@pytest.mark.parametrize("change", ["deleted", "renamed"])
def test_confirmed_deleted_or_renamed_file_is_stale(monkeypatch, change):
    fix = make_fix()
    _, source_calls, change_calls = setup_freshness(
        monkeypatch,
        files={fix.file_path: GitHubAPIError("GitHub rejected the request.", 404)},
        change=change,
    )

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=8,
    ))

    assert result["per_proposal"][0]["status"] == "stale"
    assert change_calls == [("owner", "repo", 8, fix.file_path)]
    assert source_calls == [("owner", "repo", fix.file_path, CURRENT_HEAD)]


def test_unconfirmed_missing_file_is_unverifiable(monkeypatch):
    fix = make_fix()
    setup_freshness(
        monkeypatch,
        files={fix.file_path: GitHubAPIError("GitHub rejected the request.", 404)},
        change=None,
    )

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=9,
    ))

    assert result["per_proposal"][0]["status"] == "unverifiable"


def test_missing_blob_snapshot_metadata_is_unverifiable_without_source_fetch(monkeypatch):
    fix = make_fix()
    fix.file_blob_sha = None
    head_calls, source_calls, _ = setup_freshness(monkeypatch)

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=10,
    ))

    assert result["per_proposal"][0]["status"] == "unverifiable"
    assert "snapshot metadata" in result["per_proposal"][0]["reason"]
    assert head_calls == [] and source_calls == []


def test_head_lookup_rate_limit_makes_proposals_unverifiable(monkeypatch):
    fix = make_fix()
    head_calls, source_calls, _ = setup_freshness(
        monkeypatch,
        head_error=GitHubAPIError("GitHub primary rate limit reached.", 429, kind="primary"),
    )

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=11,
    ))

    assert result["status"] == "unavailable"
    assert result["per_proposal"][0]["status"] == "unverifiable"
    assert "rate limits" in result["per_proposal"][0]["reason"]
    assert head_calls and source_calls == []


def test_file_rate_limit_and_other_api_failures_are_unverifiable(monkeypatch):
    fix = make_fix()
    _, source_calls, _ = setup_freshness(
        monkeypatch,
        files={fix.file_path: GitHubAPIError("GitHub secondary rate limit reached.", 429, kind="secondary")},
    )

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=12,
    ))

    assert result["per_proposal"][0]["status"] == "unverifiable"
    assert "rate limits" in result["per_proposal"][0]["reason"]
    assert len(source_calls) == 1


def test_unavailable_or_oversized_source_is_unverifiable(monkeypatch):
    fix = make_fix()
    setup_freshness(monkeypatch, files={fix.file_path: None})
    unavailable = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=13,
    ))
    assert unavailable["per_proposal"][0]["status"] == "unverifiable"

    oversized = "x" * (fix_freshness.MAX_SOURCE_BYTES + 1)
    setup_freshness(monkeypatch, files={fix.file_path: oversized})
    too_large = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(fix)], owner="owner", repo="repo", pull_request_number=14,
    ))
    assert too_large["per_proposal"][0]["status"] == "unverifiable"


def test_multiple_proposals_for_one_file_fetch_source_once(monkeypatch):
    fixes = [make_fix(f"fix-{index}") for index in range(3)]
    _, source_calls, _ = setup_freshness(monkeypatch, files={"src/a.py": SOURCE})

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(*fixes)], owner="owner", repo="repo", pull_request_number=15,
    ))

    assert result["current_count"] == 3
    assert result["file_fetch_count"] == 1
    assert source_calls == [("owner", "repo", "src/a.py", CURRENT_HEAD)]


def test_same_file_proposals_compare_each_recorded_blob_independently(monkeypatch):
    matching = make_fix("matches-current")
    stale = make_fix("stale-snapshot", source="value = 0\n")
    setup_freshness(monkeypatch, files={"src/a.py": SOURCE})

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue(matching, stale)], owner="owner", repo="repo", pull_request_number=17,
    ))

    statuses = {item["fix_id"]: item["status"] for item in result["per_proposal"]}
    assert statuses == {"matches-current": "current", "stale-snapshot": "stale"}


def test_legacy_issues_without_fixes_do_not_contact_github(monkeypatch):
    head_calls, source_calls, _ = setup_freshness(monkeypatch)

    result = asyncio.run(fix_freshness.verify_fix_freshness(
        [make_issue()], owner="owner", repo="repo", pull_request_number=16,
    ))

    assert result["status"] == "not_needed"
    assert result["per_proposal"] == []
    assert head_calls == [] and source_calls == []


def test_request_json_cache_bypass_reads_current_value_and_does_not_cache_it():
    github_client._response_cache.clear()
    path = "/repos/freshness/pulls/1"
    key = (path, ())
    old = {"head": {"sha": BASE_SHA}}
    github_client._response_cache[key] = (time.monotonic() + 60, old)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"head": {"sha": CURRENT_HEAD}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await request_json(client, path, use_cache=False)

    fresh = asyncio.run(run())

    assert fresh == {"head": {"sha": CURRENT_HEAD}}
    assert len(calls) == 1
    assert github_client._response_cache[key][1] == old
    github_client._response_cache.clear()


def test_current_head_and_rename_lookups_use_cache_bypass(monkeypatch):
    calls = []

    class AsyncClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def request(_client, path, *, params=None, use_cache=True):
        calls.append((path, params, use_cache))
        if path.endswith("/pulls/1"):
            return {"head": {"sha": CURRENT_HEAD}}
        return [{"filename": "src/new.py", "previous_filename": "src/old.py", "status": "renamed"}]

    monkeypatch.setattr(pull_requests, "async_client", AsyncClientContext)
    monkeypatch.setattr(pull_requests, "request_json", request)

    async def run():
        head = await pull_requests.fetch_current_pull_request_head_sha("owner", "repo", 1)
        change = await pull_requests.fetch_current_pull_request_file_change("owner", "repo", 1, "src/old.py")
        return head, change

    head, change = asyncio.run(run())

    assert head == CURRENT_HEAD and change == "renamed"
    assert calls[0][2] is False and calls[1][2] is False
