"""Stable review API consumed by the frontend."""

# FastAPI's dependency injection uses Depends() as a default value by design.
# ruff: noqa: B008

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

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
)
from app.database.test_execution_jobs import (
    cancel as cancel_execution_job,
)
from app.database.test_execution_jobs import (
    enqueue as enqueue_execution_job,
)
from app.database.test_execution_jobs import (
    get_job as get_execution_job,
)
from app.github_service.access import ensure_public_repository
from app.github_service.client import GitHubAPIError
from app.github_service.parser import InvalidPullRequestURL, parse_pull_request_url
from app.routes.auth import current_user
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
    user: dict = Depends(current_user),
):
    review = require_review_access(review_id, user["user_id"])
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
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128),
    user: dict = Depends(current_user),
):
    review = require_review_access(review_id, user["user_id"], write=True)
    issue_data = next((item for item in (review or {}).get("canonical_issues", []) if item.get("issue_id") == issue_id), None)
    if review is None or review.get("is_demo") or issue_data is None:
        raise HTTPException(status_code=404, detail="Live review or suggested fix not found.")
    issue = CanonicalIssue.model_validate(issue_data)
    fix = next((item for item in issue.suggested_fixes if item.fix_id == fix_id), None)
    if fix is None:
        raise HTTPException(status_code=404, detail="Live review or suggested fix not found.")
    pull = review["pull_request"]
    owner, repository = pull["repository_owner"], pull["repository_name"]
    if (
        generated_test.suggested_fix_id != fix.fix_id
        or generated_test.canonical_issue_id != issue.issue_id
        or generated_test.repository_id.casefold() != f"{owner}/{repository}".casefold()
        or generated_test.execution_status != "not_run"
    ):
        raise HTTPException(status_code=422, detail="Generated test identity does not match this review fix.")
    if not allow_attempt("test-execution", user["user_id"], limit=10, window_seconds=60):
        raise HTTPException(status_code=429, detail="Too many test executions. Try again in a minute.")
    try:
        await ensure_public_repository(owner, repository)
        eligibility = await evaluate_fix_eligibility(
            fix, owner=owner, repo=repository, pull_request_number=pull["pull_request_number"]
        )
    except GitHubAPIError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=str(exc)) from exc
    if eligibility.status != "eligible" or eligibility.snapshot is None:
        raise HTTPException(status_code=409, detail=f"Fix is not executable against the current snapshot: {eligibility.reason}")
    snapshot_data = eligibility.snapshot.model_dump(mode="json")
    snapshot_identity = {"repository_id": f"{owner}/{repository}", **snapshot_data}
    expected_snapshot_id = "snapshot-" + hashlib.sha256(
        json.dumps(snapshot_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    source_reference_matches = any(
        reference.file_path == fix.file_path
        and reference.source_version in {"baseline", "proposed"}
        for reference in generated_test.source_references
    )
    snapshot_hash_names = ("base_commit_sha", "checked_head_sha", "baseline_blob_sha", "proposed_blob_sha")
    snapshot_hashes = [getattr(eligibility.snapshot, name) for name in snapshot_hash_names]
    if (
        generated_test.source_snapshot_id != expected_snapshot_id
        or not source_reference_matches
        or generated_test.source_file_path != fix.file_path
        or not all(isinstance(value, str) for value in snapshot_hashes)
        or any(getattr(generated_test, name).casefold() != str(value).casefold()
               for name, value in zip(snapshot_hash_names, snapshot_hashes))
    ):
        raise HTTPException(status_code=409, detail="Generated test does not match the freshly verified fix snapshot.")
    payload = {
        "owner": owner,
        "repo": repository,
        "owner_id": user["user_id"],
        "review_id": review_id,
        "issue_id": issue_id,
        "fix_id": fix_id,
        "fix": fix.model_dump(mode="json"),
        "generated_test": generated_test.model_dump(mode="json"),
        "eligibility": {
            "status": eligibility.status,
            "reason": eligibility.reason,
            "snapshot": eligibility.snapshot.model_dump(mode="json"),
            "baseline_source": eligibility.baseline_source,
            "proposed_source": eligibility.proposed_source,
        },
    }
    require_review_access(review_id, user["user_id"], write=True)
    try:
        return enqueue_execution_job(user["user_id"], idempotency_key, payload)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


@router.get("/execution-jobs/{execution_id}")
def execution_job_status(execution_id: str, user: dict = Depends(current_user)):
    job = get_execution_job(execution_id, user["user_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Execution job not found.")
    job["validation_report"] = None
    review_id, issue_id, fix_id = (
        job.get("review_id"), job.get("issue_id"), job.get("fix_id")
    )
    if (
        isinstance(review_id, str)
        and isinstance(issue_id, str)
        and isinstance(fix_id, str)
    ):
        review = require_review_access(review_id, user["user_id"])
        if review is not None:
            issue = next(
                (item for item in review.get("canonical_issues", []) if item.get("issue_id") == issue_id),
                None,
            )
            fix = next(
                (item for item in (issue or {}).get("suggested_fixes", []) if item.get("fix_id") == fix_id),
                None,
            )
            report = next(
                (item for item in (fix or {}).get("validation_reports", []) if item.get("validation_id") == execution_id),
                None,
            )
            job["validation_report"] = report
    return job


@router.delete("/execution-jobs/{execution_id}")
def cancel_execution(execution_id: str, user: dict = Depends(current_user)):
    result = cancel_execution_job(execution_id, user["user_id"])
    if result is None:
        raise HTTPException(status_code=409, detail="Execution job is not cancellable.")
    return {"execution_id": execution_id, "status": result}


class FixDecisionRequest(BaseModel):
    decision: Literal["accepted", "rejected"]


@router.post("/jobs", status_code=202)
async def create_review_job(
    payload: AnalyzeRequest, user: dict = Depends(current_user)
):
    if payload.workspace_id and workspaces.get_role(
        payload.workspace_id, user["user_id"]
    ) not in {"owner", "admin", "editor"}:
        raise HTTPException(
            status_code=404,
            detail="Workspace not found or membership cannot create reviews.",
        )
    submitted_url = payload.pull_request_url or payload.url or ""
    try:
        owner, repo, _number = parse_pull_request_url(submitted_url)
        if payload.mode == "live":
            await ensure_public_repository(owner, repo)
        if not allow_attempt("review-job", user["user_id"], limit=5, window_seconds=60):
            raise HTTPException(
                status_code=429, detail="Too many review jobs. Try again in a minute."
            )
        return enqueue(user["user_id"], payload.model_dump(mode="json"))
    except InvalidPullRequestURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GitHubAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or 503, detail=str(exc)
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Review job could not be queued."
        ) from exc


@router.get("/jobs/{job_id}")
def review_job_status(job_id: str, user: dict = Depends(current_user)):
    try:
        job = get_job(job_id, user["user_id"])
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Review job status is unavailable."
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Review job not found.")
    return job


@router.delete("/jobs/{job_id}")
def cancel_review_job(job_id: str, user: dict = Depends(current_user)):
    job = get_job(job_id, user["user_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="Review job not found.")
    if job["status"] != "queued" or not cancel_queued(job_id, user["user_id"]):
        raise HTTPException(
            status_code=409, detail="Only a queued review can be cancelled."
        )
    return {"job_id": job_id, "status": "cancelled"}


@router.post("/{review_id}/issues/{issue_id}/fixes/{fix_id}/validate")
async def validate_suggested_fix(
    review_id: str,
    issue_id: str,
    fix_id: str,
    user: dict = Depends(current_user),
):
    """Run bounded deterministic source checks; generated tests stay unexecuted."""
    store = get_store()
    review: dict[str, Any] | None = require_review_access(
        review_id, user["user_id"], write=True
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
                for item in issue_data.get("suggested_fixes", [])
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
        # Preserve an attempt as a report-level failure, never as an artificial check.
        from datetime import datetime
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
        require_review_access(review_id, user["user_id"], write=True)
        store.save_review(review, owner_id=user["user_id"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Validation report could not be persisted."
        ) from exc
    return result_payload


@router.post("/analyze", response_model=ReviewResponse)
async def analyze_review(payload: AnalyzeRequest, user: dict = Depends(current_user)):
    try:
        if payload.mode == "live":
            from app.github_service.parser import parse_pull_request_url

            owner, repo, _number = parse_pull_request_url(
                payload.pull_request_url or payload.url or ""
            )
            await ensure_public_repository(owner, repo)
        return await analyze(payload, owner_id=user["user_id"])
    except InvalidPullRequestURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GitHubAPIError as exc:
        status = exc.status_code
        if status is None:
            status = 503
        elif status == 404:
            status = 404
        elif status == 429:
            status = 429
        elif status == 401:
            status = 401
        elif status == 403:
            status = 403
        elif status >= 500:
            status = 502
        else:
            status = 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail="GitHub returned data that could not be processed."
        ) from exc
    except Exception as exc:
        # Keep implementation details and local paths out of the public response.
        raise HTTPException(
            status_code=503, detail="Review could not be completed or persisted."
        ) from exc


@router.get("/history", response_model=list[ReviewResponse])
def history(
    limit: int = Query(default=50, ge=1, le=100),
    include_demo: bool = Query(default=True),
    user: dict = Depends(current_user),
):
    try:
        return get_store().list_reviews(
            limit, owner_id=user["user_id"], include_demo=include_demo
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Review history is unavailable."
        ) from exc


@router.get("/saved")
def saved_pull_requests(user: dict = Depends(current_user)):
    try:
        return get_store().list_saved(owner_id=user["user_id"])
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Saved pull requests are unavailable."
        ) from exc


@router.post("/saved", status_code=201)
def save_pull_request(
    payload: SavedPullRequestRequest, user: dict = Depends(current_user)
):
    url = payload.pull_request_url or payload.url or ""
    try:
        from app.github_service.parser import parse_pull_request_url

        owner, repo, number = parse_pull_request_url(url)
    except InvalidPullRequestURL as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    canonical_url = f"https://github.com/{owner}/{repo}/pull/{number}"
    try:
        return get_store().save_pull_request(canonical_url, owner_id=user["user_id"])
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Pull request could not be saved."
        ) from exc


@router.delete("/saved/{saved_id}")
def delete_saved_pull_request(saved_id: str, user: dict = Depends(current_user)):
    try:
        deleted = get_store().delete_saved(saved_id, owner_id=user["user_id"])
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Saved pull requests are unavailable."
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved pull request not found.")
    return {"deleted": True}


@router.get("/{review_id}/diff", response_model=DiffResponse)
def review_diff(review_id: str, user: dict = Depends(current_user)):
    try:
        review = require_review_access(review_id, user["user_id"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Review data is unavailable."
        ) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return {
        "review_id": review_id,
        "files": review.get("diff_files", []),
        "warnings": review.get("warnings", []),
    }


@router.get("/{review_id}", response_model=ReviewResponse)
def get_review(review_id: str, user: dict = Depends(current_user)):
    try:
        review = require_review_access(review_id, user["user_id"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Review data is unavailable."
        ) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return review


@router.post("/{review_id}/issues/{issue_id}/fixes/{fix_id}/decision")
def decide_suggested_fix(
    review_id: str,
    issue_id: str,
    fix_id: str,
    payload: FixDecisionRequest,
    user: dict = Depends(current_user),
):
    """Record developer intent only; this endpoint never writes to GitHub."""
    store = get_store()
    review: dict[str, Any] | None = require_review_access(
        review_id, user["user_id"], write=True
    )
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
                for item in issue.get("suggested_fixes", [])
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
        require_review_access(review_id, user["user_id"], write=True)
        store.save_review(review, owner_id=user["user_id"])
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
    user: dict = Depends(current_user),
):
    store = get_store()
    review: dict[str, Any] | None = require_review_access(
        review_id, user["user_id"], write=True
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
                for item in issue_data.get("suggested_fixes", [])
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
        require_review_access(review_id, user["user_id"], write=True)
        store.save_review(review, owner_id=user["user_id"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Semantic assessment could not be saved."
        ) from exc
    return result_record
