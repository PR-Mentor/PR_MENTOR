"""Evidence-derived personal insights and engineering analytics."""

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException

from app.database import workspaces
from app.database.reviews import get_store
from app.github_service.access import ensure_public_repository
from app.github_service.client import GitHubAPIError
from app.routes.auth import current_user
from app.services.repository_intelligence import analyze_repository

router = APIRouter(prefix="/api", tags=["insights and analytics"])
ANALYSIS_WINDOW = 100


def _issues(review: dict) -> list[dict]:
    canonical = review.get("canonical_issues")
    if isinstance(canonical, list) and canonical:
        return [item for item in canonical if isinstance(item, dict)]
    return [item for item in review.get("findings", []) if isinstance(item, dict)]


def _learning_insights(reviews: list[dict], *, scope: str) -> dict:
    categories: Counter = Counter()
    decisions: Counter = Counter()
    validations: Counter = Counter()
    fix_count = 0
    for review in reviews:
        for issue in _issues(review):
            if isinstance(issue.get("category"), str):
                categories[issue["category"]] += 1
            for fix in issue.get("suggested_fixes", []):
                if not isinstance(fix, dict):
                    continue
                fix_count += 1
                if fix.get("decision_status") in {"accepted", "rejected"}:
                    decisions[fix["decision_status"]] += 1
                for report in fix.get("validation_reports", []):
                    if isinstance(report, dict) and isinstance(report.get("status"), str):
                        validations[report["status"]] += 1
    repeated = [
        {"category": category, "observation_count": count}
        for category, count in sorted(categories.items()) if count >= 2
    ]
    return {
        "scope": scope,
        "source_review_count": len(reviews),
        "window_limit": ANALYSIS_WINDOW,
        "repeated_patterns": repeated,
        "suggested_topics": [item["category"] for item in repeated],
        "suggested_fix_count": fix_count,
        "fix_decisions": dict(sorted(decisions.items())),
        "validation_reports_by_status": dict(sorted(validations.items())),
        "basis": "Derived from stored review findings, fix decisions, and validation reports in the latest bounded review window. Counts are evidence summaries, not a measure of developer skill.",
    }


@router.get("/learning/insights")
def learning_insights(user: dict = Depends(current_user)):  # noqa: B008 - FastAPI dependency injection.
    try:
        reviews = get_store().list_reviews(
            ANALYSIS_WINDOW, owner_id=user["user_id"], include_demo=False
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Personal insights are unavailable."
        ) from exc
    return _learning_insights(reviews, scope="personal")


@router.get("/workspaces/{workspace_id}/learning-insights")
def workspace_learning_insights(workspace_id: str, user: dict = Depends(current_user)):  # noqa: B008 - FastAPI dependency injection.
    if workspaces.get_role(workspace_id, user["user_id"]) is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    try:
        store = get_store()
        reviews = [
            review for review_id in workspaces.list_shared_review_ids(workspace_id, limit=ANALYSIS_WINDOW)
            if (review := store.get_review(review_id)) is not None
        ]
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Workspace insights are unavailable.") from exc
    return _learning_insights(reviews, scope="workspace")


@router.get("/analytics/me")
def personal_analytics(user: dict = Depends(current_user)):  # noqa: B008 - FastAPI dependency injection.
    try:
        store = get_store()
        total_reviews = store.live_review_count(owner_id=user["user_id"])
        live_pull_request_count = store.live_pull_request_count(owner_id=user["user_id"])
        live_finding_count = store.live_finding_count(owner_id=user["user_id"])
        reviews = store.list_reviews(
            ANALYSIS_WINDOW, owner_id=user["user_id"], include_demo=False
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Engineering analytics are unavailable."
        ) from exc
    categories: Counter = Counter()
    severities: Counter = Counter()
    decisions: Counter = Counter()
    validations: Counter = Counter()
    fix_count = duration_total = 0
    for review in reviews:
        duration_total += int(review.get("duration_ms", 0) or 0)
        for issue in _issues(review):
            if isinstance(issue.get("category"), str):
                categories[issue["category"]] += 1
            if isinstance(issue.get("severity"), str):
                severities[issue["severity"]] += 1
            for fix in issue.get("suggested_fixes", []):
                fix_count += 1
                if fix.get("decision_status") in {"accepted", "rejected"}:
                    decisions[fix["decision_status"]] += 1
                for report in fix.get("validation_reports", []):
                    if isinstance(report.get("status"), str):
                        validations[report["status"]] += 1
    return {
        "total_reviews": total_reviews,
        "live_pull_request_count": live_pull_request_count,
        "live_finding_count": live_finding_count,
        "reviews_analyzed": len(reviews),
        "analysis_window_limit": ANALYSIS_WINDOW,
        "findings_by_category": dict(sorted(categories.items())),
        "findings_by_severity": dict(sorted(severities.items())),
        "suggested_fix_count": fix_count,
        "fix_decisions": dict(sorted(decisions.items())),
        "validation_reports_by_status": dict(sorted(validations.items())),
        "average_review_duration_ms": round(duration_total / len(reviews))
        if reviews
        else None,
        "model_latency_ms": None,
        "model_cost": None,
        "accuracy_metrics": None,
        "unavailable_metrics": [
            "model latency is not recorded",
            "provider cost is not available",
            "human-labelled accuracy data is not available",
        ],
    }


@router.get("/repositories/{owner}/{repository}/intelligence")
async def repository_intelligence(
    owner: str,
    repository: str,
    ref: str,
    _user: dict = Depends(current_user),  # noqa: B008 - FastAPI dependency injection.
):
    try:
        await ensure_public_repository(owner, repository)
        return await analyze_repository(owner, repository, ref)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GitHubAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or 503, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Repository intelligence is temporarily unavailable.",
        ) from exc
