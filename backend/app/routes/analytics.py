"""Evidence-derived personal insights and engineering analytics."""

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException

from app.database import workspaces
from app.database.reviews import get_store
from app.github_service.access import ensure_public_repository
from app.github_service.client import GitHubAPIError
from app.routes.auth import current_user
from app.services.repository_intelligence import analyze_repository, get_or_analyze_repository, resolve_ref_to_sha

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
            and not review.get("repository_private")
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
    languages: Counter = Counter()
    issue_lifecycle: Counter = Counter()
    decisions: Counter = Counter()
    validations: Counter = Counter()
    fix_count = duration_total = 0
    total_tokens = prompt_tokens = completion_tokens = 0
    estimated_cost_usd = 0.0
    models_used: set[str] = set()
    providers_used: set[str] = set()
    fallback_count = 0

    for review in reviews:
        duration_total += int(review.get("duration_ms", 0) or 0)
        meta = review.get("ai_execution_metadata") or review.get("ai_metadata")
        if isinstance(meta, dict):
            prompt_tokens += int(meta.get("prompt_tokens", 0) or 0)
            completion_tokens += int(meta.get("completion_tokens", 0) or 0)
            total_tokens += int(meta.get("total_tokens", 0) or 0)
            estimated_cost_usd += float(meta.get("estimated_cost_usd", 0.0) or 0.0)
            if meta.get("model"):
                models_used.add(str(meta["model"]))
            if meta.get("provider"):
                providers_used.add(str(meta["provider"]))
            if meta.get("fallback_used"):
                fallback_count += 1

        for issue in _issues(review):
            if isinstance(issue.get("category"), str):
                categories[issue["category"]] += 1
            if isinstance(issue.get("severity"), str):
                severities[issue["severity"]] += 1

            fp = (issue.get("file_path") or "").lower()
            if fp.endswith(".py"):
                languages["python"] += 1
            elif fp.endswith(".c") or fp.endswith(".h"):
                languages["c"] += 1
            elif fp.endswith(".cpp") or fp.endswith(".hpp") or fp.endswith(".cc"):
                languages["cpp"] += 1
            elif fp.endswith(".java"):
                languages["java"] += 1
            elif fp:
                languages["other"] += 1

            status = issue.get("status")
            if status in ("resolved", "open", "dismissed"):
                issue_lifecycle[status] += 1
            else:
                issue_lifecycle["unclassified"] += 1

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
        "findings_by_language": dict(sorted(languages.items())),
        "issue_lifecycle": dict(sorted(issue_lifecycle.items())),
        "ai_orchestration": {
            "total_tokens": total_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_cost_usd": round(estimated_cost_usd, 4),
            "models_used": sorted(models_used),
            "providers_used": sorted(providers_used),
            "fallback_count": fallback_count,
        },
        "suggested_fix_count": fix_count,
        "fix_decisions": dict(sorted(decisions.items())),
        "validation_reports_by_status": dict(sorted(validations.items())),
        "average_review_duration_ms": round(duration_total / len(reviews))
        if reviews
        else None,
        "model_latency_ms": None,
        "model_cost": round(estimated_cost_usd, 4) if estimated_cost_usd > 0 else None,
        "accuracy_metrics": None,
        "unavailable_metrics": [
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
        # Resolve branch/tag refs to a full commit SHA before cache lookup.
        # This prevents stale cache results when a branch advances.
        full_sha = await resolve_ref_to_sha(owner, repository, ref)
        return await get_or_analyze_repository(owner, repository, full_sha)
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


@router.get("/analytics/ai-orchestration")
def ai_orchestration_analytics(_user: dict = Depends(current_user)):
    """Safe, non-secret diagnostic telemetry on AI models, health, and routing."""
    from app.ai_orchestration.health import get_health_tracker
    from app.ai_orchestration.models import get_model_registry
    from app.ai_orchestration.router import get_hybrid_router
    from app.ai_orchestration.tasks import get_task_registry

    task_reg = get_task_registry()
    model_reg = get_model_registry()
    health = get_health_tracker()
    router_inst = get_hybrid_router()

    tasks_data = [t.to_dict() for t in task_reg.list_tasks()]
    models_data = [m.to_dict() for m in model_reg.list_models()]

    routing_explanations = {}
    for t in task_reg.list_tasks():
        routing_explanations[t.task_id] = router_inst.explain_routing(t.task_id)

    return {
        "tasks": tasks_data,
        "models": models_data,
        "routing": routing_explanations,
        "status": "operational",
    }
