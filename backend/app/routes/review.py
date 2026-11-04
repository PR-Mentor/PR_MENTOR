"""Stable review API consumed by the frontend."""

from typing import Annotated, Any
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.database.reviews import get_store
from app.github_service.client import GitHubAPIError
from app.github_service.parser import InvalidPullRequestURL
from app.schemas.review import AnalyzeRequest, DiffResponse, ReviewResponse, SavedPullRequestRequest
from app.services.review_orchestrator import analyze
from app.routes.auth import current_claims

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


@router.post("/analyze", response_model=ReviewResponse)
async def analyze_review(payload: AnalyzeRequest, claims: Annotated[dict[str, Any], Depends(current_claims)], github_token: str | None = Header(default=None, alias="X-GitHub-Token", max_length=512)):
    if github_token and any(ord(char) < 32 or ord(char) == 127 for char in github_token):
        raise HTTPException(status_code=400, detail="The GitHub provider token is malformed.")
    try:
        return await analyze(payload, claims["uid"], github_token=github_token)
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
        # Keep implementation details and local paths out of the public response.
        raise HTTPException(status_code=503, detail="Review could not be completed or persisted.") from exc


@router.get("/history", response_model=list[ReviewResponse])
def history(claims: Annotated[dict[str, Any], Depends(current_claims)], limit: int = Query(default=50, ge=1, le=100)):
    try:
        return get_store().list_reviews(claims["uid"], limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Review history is unavailable.") from exc


@router.get("/saved")
def saved_pull_requests(claims: Annotated[dict[str, Any], Depends(current_claims)]):
    try:
        return get_store().list_saved(claims["uid"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Saved pull requests are unavailable.") from exc


@router.post("/saved", status_code=201)
def save_pull_request(payload: SavedPullRequestRequest, claims: Annotated[dict[str, Any], Depends(current_claims)]):
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
def delete_saved_pull_request(saved_id: str, claims: Annotated[dict[str, Any], Depends(current_claims)]):
    try:
        deleted = get_store().delete_saved(saved_id, claims["uid"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Saved pull requests are unavailable.") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved pull request not found.")
    return {"deleted": True}


@router.get("/{review_id}/diff", response_model=DiffResponse)
def review_diff(review_id: str, claims: Annotated[dict[str, Any], Depends(current_claims)]):
    try:
        review = get_store().get_review(review_id, claims["uid"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Review data is unavailable.") from exc
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return {"review_id": review_id, "files": review.get("diff_files", []), "warnings": review.get("warnings", [])}


@router.get("/{review_id}", response_model=ReviewResponse)
def get_review(review_id: str, claims: Annotated[dict[str, Any], Depends(current_claims)]):
    try:
        review = get_store().get_review(review_id, claims["uid"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Review data is unavailable.") from exc
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return review
