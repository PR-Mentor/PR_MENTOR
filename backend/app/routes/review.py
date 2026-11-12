"""Stable review API consumed by the frontend."""

# FastAPI's dependency injection uses Depends() as a default value by design.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from app.ai_review.generated_tests import generate_tests_for_fix
from app.ai_review.semantic_review import review_fix_semantics
from app.database import workspaces
from app.database.auth import allow_attempt
from app.database.review_jobs import cancel_queued, enqueue, get_job
from app.database.reviews import get_store
from app.database.test_execution_jobs import (
    IdempotencyConflict,
    cancel as cancel_execution_job,
    enqueue as enqueue_execution_job,
    get_job as get_execution_job,
)
from app.github_service.access import ensure_public_repository
from app.github_service.client import GitHubAPIError
from app.github_service.parser import InvalidPullRequestURL, parse_pull_request_url
from app.routes.auth import current_claims
from app.schemas.generated_test import GeneratedTestCase
from app.schemas.issue import (
    CanonicalIssue,
    FixValidationReport,
    FixValidationReportFailure,
    SuggestedFix,
)
from app.schemas.review import (
    AnalyzeRequest,
    DiffResponse,
    ReviewResponse,
    SavedPullRequestRequest,
)
from app.services.fix_eligibility import evaluate_fix_eligibility
from app.services.fix_freshness import verify_fix_freshness
from app.services.review_access import require_review_access
from app.services.review_orchestrator import analyze
from app.verification.source_analysis import validate_static_fix

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


@router.post("/{review_id}/issues/{issue_id}/fixes/{fix_id}/generate-tests")
async def generate_fix_tests(
    review_id: str,
    issue_id: str,
    fix_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    uid = claims["uid"]
    review = require_review_access(review_id, uid)
    issue_data = next((item for item in (review or {}).get("canonical_issues", []) if item.get("issue_id") == issue_id), None)
    if review is None or review.get("is_demo") or issue_data is None:
        raise HTTPException(status_code=404, detail="Live review or suggested fix not found.")
    issue = CanonicalIssue.model_validate(issue_data)
    fix = next((item for item in issue.suggested_fixes if item.fix_id == fix_id), None)
    if fix is None:
        raise HTTPException(status_code=404, detail="Live review or suggested fix not found.")
    pull = review["pull_request"]
    try:
        await ensure_public_repository(pull["repository_owner"], pull["repository_name"])
        result = await generate_tests_for_fix(
            fix,
            issue,
            owner=pull["repository_owner"],
            repo=pull["repository_name"],
            pull_request_number=pull["pull_request_number"],
        )
    except GitHubAPIError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=str(exc)) from exc
    return result.model_dump(mode="json")


@router.post("/{review_id}/issues/{issue_id}/fixes/{fix_id}/tests/execute", status_code=202)
async def enqueue_generated_test_execution(
    review_id: str,
    issue_id: str,
    fix_id: str,
    generated_test: GeneratedTestCase,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128),
):
    uid = claims["uid"]
    review = require_review_access(review_id, uid, write=True)
    issue_data = next((item for item in (review or {}).get("canonical_issues", []) if item.get("issue_id") == issue_id), None)
    if review is None or review.get("is_demo") or issue_data is None:
        raise HTTPException(status_code=404, detail="Live review or suggested fix not found.")
    issue = CanonicalIssue.model_validate(issue_data)
    fix = next((item for item in issue.suggested_fixes if item.fix_id == fix_id), None)
    if fix is None:
        raise HTTPException(status_code=404, detail="Live review or suggested fix not found.")
    if generated_test.target_fix_id != fix.fix_id or generated_test.target_issue_id != issue.issue_id:
        raise HTTPException(status_code=422, detail="Generated test case does not target this suggested fix.")
    pull = review["pull_request"]
    try:
        await ensure_public_repository(pull["repository_owner"], pull["repository_name"])
        freshness = await verify_fix_freshness(
            fix,
            owner=pull["repository_owner"],
            repo=pull["repository_name"],
            pull_request_number=pull["pull_request_number"],
        )
    except GitHubAPIError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=str(exc)) from exc
    if not freshness.is_fresh:
        raise HTTPException(status_code=409, detail=f"Target source has changed: {freshness.reason}")
    if freshness.snapshot is None:
        raise HTTPException(status_code=409, detail="Verified target file snapshot could not be established.")
    execution_payload = {
        "review_id": review_id,
        "issue_id": issue_id,
        "fix_id": fix_id,
        "owner": pull["repository_owner"],
        "repo": pull["repository_name"],
        "pull_request_number": pull["pull_request_number"],
        "head_sha": pull["head_sha"],
        "base_sha": pull["base_sha"],
        "target_file": fix.target_file,
        "snapshot_blob_sha": freshness.snapshot.blob_sha,
        "replacement_code": fix.replacement_code,
        "generated_test": generated_test.model_dump(mode="json"),
    }
    try:
        job = enqueue_execution_job(
            owner_id=uid,
            idempotency_key=idempotency_key,
            payload=execution_payload,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job


@router.get("/execution-jobs/{execution_id}")
def get_generated_test_execution_job(
    execution_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    uid = claims["uid"]
    job = get_execution_job(execution_id, owner_id=uid)
    if job is None:
        raise HTTPException(status_code=404, detail="Test execution job not found.")
    job["validation_report"] = None
    review_id, issue_id, fix_id = (
        job.get("review_id"), job.get("issue_id"), job.get("fix_id")
    )
    if (
        isinstance(review_id, str)
        and isinstance(issue_id, str)
        and isinstance(fix_id, str)
    ):
        try:
            review = require_review_access(review_id, uid)
        except Exception:
            review = None
        if review is not None:
            issue = next(
                (item for item in review.get("canonical_issues", []) if item.get("issue_id") == issue_id),
                None,
            )
            fix = next(
                (item for item in (issue or {}).get("suggested_fixes", []) if item.get("fix_id") == fix_id),
                None,
            )
            for report in (fix or {}).get("validation_reports", []):
                if report.get("validation_id") == execution_id:
                    job["validation_report"] = report
                    break
    return job


@router.delete("/execution-jobs/{execution_id}")
def cancel_generated_test_execution_job(
    execution_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    cancelled = cancel_execution_job(execution_id, owner_id=claims["uid"])
    if not cancelled:
        raise HTTPException(status_code=404, detail="Active test execution job not found.")
    return {"status": "cancelled", "execution_id": execution_id}


@router.post("/jobs", status_code=202)
async def enqueue_review(
    payload: AnalyzeRequest,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    uid = claims["uid"]
    if payload.workspace_id and workspaces.get_role(
        payload.workspace_id, uid
    ) not in {"owner", "admin", "editor"}:
        raise HTTPException(
            status_code=404,
            detail="Workspace not found or membership cannot create reviews.",
        )
    if not allow_attempt("review-job", uid, limit=5, window_seconds=60):
        raise HTTPException(status_code=429, detail="Too many reviews queued recently.")
    submitted_url = payload.pull_request_url or payload.url or ""
    try:
        owner, repo, _number = parse_pull_request_url(submitted_url)
        if payload.mode == "live":
            await ensure_public_repository(owner, repo)
    except InvalidPullRequestURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GitHubAPIError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=str(exc)) from exc
    job_payload = payload.model_dump(mode="json")
    job = enqueue(uid, job_payload)
    return job


@router.get("/jobs/{job_id}")
def read_review_job(
    job_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    job = get_job(job_id, claims["uid"])
    if not job:
        raise HTTPException(status_code=404, detail="Review job not found.")
    return job


@router.delete("/jobs/{job_id}")
def cancel_review_job(
    job_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    cancelled = cancel_queued(job_id, claims["uid"])
    if not cancelled:
        raise HTTPException(status_code=404, detail="Review job not found or cannot be cancelled.")
    return {"cancelled": True, "job_id": job_id}


@router.post("/{review_id}/issues/{issue_id}/fixes/{fix_id}/validate")
async def validate_suggested_fix(
    review_id: str,
    issue_id: str,
    fix_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    """Run bounded deterministic source checks; generated tests stay unexecuted."""
    uid = claims["uid"]
    store = get_store()
    review: dict[str, Any] | None = require_review_access(
        review_id, uid, write=True
    )
    if review is None or review.get("is_demo"):
        raise HTTPException(
            status_code=404, detail="Live review or suggested fix not found."
        )
    issue_data = next(
        (
            item
            for item in review.get("canonical_issues", [])
            if item.get("issue_id") == issue_id
        ),
        None,
    )
    fix_data = (
        next(
            (
                item
                for item in (issue_data.get("suggested_fixes", []) if issue_data else [])
                if item.get("fix_id") == fix_id
            ),
            None,
        )
        if issue_data
        else None
    )
    if fix_data is None:
        raise HTTPException(
            status_code=404, detail="Live review or suggested fix not found."
        )
    issue = CanonicalIssue.model_validate(issue_data)
    fix = next((item for item in issue.suggested_fixes if item.fix_id == fix_id), None)
    if fix is None:
        raise HTTPException(
            status_code=404, detail="Live review or suggested fix not found."
        )
    if len(fix.validation_reports) >= 20:
        raise HTTPException(
            status_code=409,
            detail="The validation-attempt history limit has been reached for this fix.",
        )
    pr = review["pull_request"]
    try:
        await ensure_public_repository(pr["repository_owner"], pr["repository_name"])
        eligibility = await evaluate_fix_eligibility(
            fix,
            owner=pr["repository_owner"],
            repo=pr["repository_name"],
            pull_request_number=pr["pull_request_number"],
        )
    except GitHubAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or 503, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Current fix snapshot could not be checked."
        ) from exc

    if eligibility.status != "eligible":
        from uuid import uuid4

        previous = fix.validation_reports
        attempt = max((item.attempt_number for item in previous), default=0) + 1
        report = FixValidationReport(
            validation_id=uuid4().hex,
            fix_id=fix.fix_id,
            attempt_number=attempt,
            status="unavailable",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            snapshot=eligibility.snapshot,
            report_failure=FixValidationReportFailure(
                code="snapshot_not_eligible",
                status="unavailable",
                reason=eligibility.reason,
            ),
            reason=eligibility.reason,
        )
        result_payload = {
            "report": report.model_dump(mode="json"),
            "tool_results": {},
            "generation": None,
            "execution": "not_run",
        }
    else:
        outcome = await asyncio.to_thread(validate_static_fix, fix, eligibility)
        report = outcome.report
        result_payload = {
            "report": report.model_dump(mode="json"),
            "tool_results": outcome.tool_results,
            "generation": None,
            "execution": "not_run",
        }

    fix.validation_reports.append(report)
    issue_data = next(
        item for item in review["canonical_issues"] if item.get("issue_id") == issue_id
    )
    for item in issue_data["suggested_fixes"]:
        if item.get("fix_id") == fix_id:
            item["validation_reports"] = [
                entry.model_dump(mode="json") for entry in fix.validation_reports
            ]
    try:
        require_review_access(review_id, uid, write=True)
        store.save_review(review, owner_id=uid)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Validation report could not be persisted."
        ) from exc
    return result_payload


@router.post("/analyze", response_model=ReviewResponse)
async def analyze_review(
    payload: AnalyzeRequest,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
    github_token: str | None = Header(default=None, alias="X-GitHub-Token", max_length=512),
):
    if github_token and any(ord(char) < 32 or ord(char) == 127 for char in github_token):
        raise HTTPException(status_code=400, detail="The GitHub provider token is malformed.")
    uid = claims["uid"]
    try:
        if payload.mode == "live":
            owner, repo, _number = parse_pull_request_url(
                payload.pull_request_url or payload.url or ""
            )
            await ensure_public_repository(owner, repo)
        return await analyze(payload, user_id=uid, github_token=github_token)
    except InvalidPullRequestURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GitHubAPIError as exc:
        status = exc.status_code
        detail = str(exc)
        if status is None:
            status = 503
        elif status == 404:
            status = 404
        elif status == 429:
            status = 429
        elif status == 401:
            status = 502
            detail = "GitHub rejected its access token. Sign out and back in to reconnect GitHub, or update the backend GITHUB_TOKEN."
        elif status == 403:
            status = 403
        elif status >= 500:
            status = 502
        else:
            status = 502
        raise HTTPException(status_code=status, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="GitHub returned data that could not be processed.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Review could not be completed or persisted.") from exc


@router.get("/history", response_model=list[ReviewResponse])
def history(
    claims: Annotated[dict[str, Any], Depends(current_claims)],
    limit: int = Query(default=50, ge=1, le=100),
    include_demo: bool = Query(default=True),
):
    try:
        return get_store().list_reviews(user_id=claims["uid"], limit=limit, include_demo=include_demo)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Review history is unavailable.") from exc


@router.get("/saved")
def saved_pull_requests(claims: Annotated[dict[str, Any], Depends(current_claims)]):
    try:
        return get_store().list_saved(claims["uid"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Saved pull requests are unavailable.") from exc


@router.post("/saved", status_code=201)
def save_pull_request(
    payload: SavedPullRequestRequest,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    url = payload.pull_request_url or payload.url or ""
    try:
        from app.github_service.parser import parse_pull_request_url

        owner, repo, number = parse_pull_request_url(url)
    except InvalidPullRequestURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    canonical_url = f"https://github.com/{owner}/{repo}/pull/{number}"
    try:
        return get_store().save_pull_request(canonical_url, claims["uid"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Pull request could not be saved.") from exc


@router.delete("/saved/{saved_id}")
def delete_saved_pull_request(
    saved_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    try:
        deleted = get_store().delete_saved(saved_id, claims["uid"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Saved pull requests are unavailable.") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved pull request not found.")
    return {"deleted": True}


@router.get("/{review_id}/diff", response_model=DiffResponse)
def review_diff(
    review_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    try:
        review = require_review_access(review_id, claims["uid"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Review data is unavailable.") from exc
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return {
        "review_id": review_id,
        "files": review.get("diff_files", []),
        "warnings": review.get("warnings", []),
    }


@router.get("/{review_id}", response_model=ReviewResponse)
def get_review(
    review_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    try:
        review = require_review_access(review_id, claims["uid"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Review data is unavailable.") from exc
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return review


class FixDecisionRequest(BaseModel):
    decision: Literal["accepted", "rejected"]


@router.post("/{review_id}/issues/{issue_id}/fixes/{fix_id}/decision")
def decide_suggested_fix(
    review_id: str,
    issue_id: str,
    fix_id: str,
    payload: FixDecisionRequest,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    uid = claims["uid"]
    store = get_store()
    review = require_review_access(review_id, uid, write=True)
    if review is None:
        raise HTTPException(
            status_code=404, detail="Review or suggested fix not found."
        )
    issue = next(
        (
            item
            for item in review.get("canonical_issues", [])
            if item.get("issue_id") == issue_id
        ),
        None,
    )
    fix = (
        next(
            (
                item
                for item in (issue.get("suggested_fixes", []) if issue else [])
                if item.get("fix_id") == fix_id
            ),
            None,
        )
        if issue
        else None
    )
    if fix is None:
        raise HTTPException(
            status_code=404, detail="Review or suggested fix not found."
        )
    if payload.decision == "accepted":
        fix_analysis = next(
            (
                item
                for item in review.get("tool_results", [])
                if item.get("tool") == "fix_analysis"
            ),
            None,
        )
        freshness = fix_analysis.get("freshness", {}) if fix_analysis else {}
        current = any(
            item.get("canonical_issue_id") == issue_id
            and item.get("fix_id") == fix_id
            and item.get("status") == "current"
            for item in freshness.get("per_proposal", [])
        )
        if not current:
            raise HTTPException(
                status_code=409,
                detail="Only a fix verified as current in this review can be accepted.",
            )
    fix["decision_status"] = payload.decision
    fix["decided_at"] = datetime.now(UTC).isoformat()
    try:
        require_review_access(review_id, uid, write=True)
        store.save_review(review, owner_id=uid)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Fix decision could not be saved."
        ) from exc
    return fix


@router.post("/{review_id}/issues/{issue_id}/fixes/{fix_id}/semantic-review")
async def semantic_review(
    review_id: str,
    issue_id: str,
    fix_id: str,
    claims: Annotated[dict[str, Any], Depends(current_claims)],
):
    uid = claims["uid"]
    store = get_store()
    review: dict[str, Any] | None = require_review_access(
        review_id, uid, write=True
    )
    if review is None or review.get("is_demo"):
        raise HTTPException(
            status_code=404, detail="Live review or suggested fix not found."
        )
    issue_data = next(
        (
            item
            for item in review.get("canonical_issues", [])
            if item.get("issue_id") == issue_id
        ),
        None,
    )
    fix_data = (
        next(
            (
                item
                for item in (issue_data.get("suggested_fixes", []) if issue_data else [])
                if item.get("fix_id") == fix_id
            ),
            None,
        )
        if issue_data
        else None
    )
    if fix_data is None:
        raise HTTPException(
            status_code=404, detail="Live review or suggested fix not found."
        )
    metadata = review.get("pull_request", {})
    owner, repo = metadata.get("repository_owner"), metadata.get("repository_name")
    number = metadata.get("pull_request_number")
    try:
        await ensure_public_repository(owner, repo)
        current = await verify_fix_freshness(
            [CanonicalIssue.model_validate(issue_data)],
            owner=owner,
            repo=repo,
            pull_request_number=number,
        )
    except GitHubAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or 503, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Current fix snapshot could not be verified."
        ) from exc
    if not any(
        item.get("fix_id") == fix_id and item.get("status") == "current"
        for item in current.get("per_proposal", [])
    ):
        raise HTTPException(
            status_code=409,
            detail="The fix is stale or its current snapshot could not be verified.",
        )
    try:
        result = await review_fix_semantics(
            CanonicalIssue.model_validate(issue_data),
            SuggestedFix.model_validate(fix_data),
            tool_results=review.get("tool_results", []),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Semantic assessment could not be completed."
        ) from exc
    records = review.setdefault("tool_results", [])
    records[:] = [
        item
        for item in records
        if not (
            item.get("tool") == "semantic_review"
            and item.get("issue_id") == issue_id
            and item.get("fix_id") == fix_id
        )
    ]
    result_record = {
        "tool": "semantic_review",
        "issue_id": issue_id,
        "fix_id": fix_id,
        **result,
    }
    records.append(result_record)
    try:
        require_review_access(review_id, uid, write=True)
        store.save_review(review, owner_id=uid)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Semantic assessment could not be saved."
        ) from exc
    return result_record
