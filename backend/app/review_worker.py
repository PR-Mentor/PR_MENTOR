"""Durable background review worker; separate from the sandbox executor."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import threading

from app.ai_review.issue_explanations import MAX_EXPLANATION_SECONDS, generate_issue_explanations
from app.database import issue_explanation_jobs
from app.database.reviews import get_store
from app.database.review_jobs import (
    LEASE_SECONDS,
    claim_next,
    complete,
    fail,
    heartbeat,
)
from app.github_service.client import GitHubAPIError
from app.schemas.issue import CanonicalIssue
from app.schemas.review import AnalyzeRequest
from app.services.review_orchestrator import LIVE_REVIEW_TIMEOUT_SECONDS, QUICK_REVIEW_TIMEOUT_SECONDS, analyze

MAX_JOB_SECONDS = LIVE_REVIEW_TIMEOUT_SECONDS
POLL_SECONDS = 1.0
_stop = threading.Event()
logging.basicConfig(level=logging.INFO, format="%(message)s")
_logger = logging.getLogger("pr_mentor.review_worker")
_prefer_explanation_job = True


def process_one() -> bool:
    """Process review and explanation jobs fairly in the shared worker process."""
    global _prefer_explanation_job

    if _prefer_explanation_job:
        explanation_job = issue_explanation_jobs.claim_next()
        if explanation_job is not None:
            _process_explanation_job(explanation_job)
            _prefer_explanation_job = False
            return True

    job = claim_next()
    if job is not None:
        _process_review_job(job)
        _prefer_explanation_job = True
        return True

    explanation_job = issue_explanation_jobs.claim_next()
    if explanation_job is None:
        return False
    _process_explanation_job(explanation_job)
    _prefer_explanation_job = True
    return True


def _process_review_job(job: dict) -> None:
    job_id = job["job_id"]
    lease_token = job["lease_token"]
    heartbeat_stop = threading.Event()

    def extend_lease():
        while not heartbeat_stop.wait(LEASE_SECONDS / 3):
            if not heartbeat(job_id, lease_token):
                return

    heartbeat_thread = threading.Thread(
        target=extend_lease, name=f"lease-{job_id[:8]}", daemon=True
    )
    heartbeat_thread.start()
    _logger.info(
        json.dumps(
            {
                "event": "review_job_started",
                "job_id": job_id,
                "attempt": job["attempts"],
            }
        )
    )
    try:
        request = AnalyzeRequest.model_validate(job["payload"])
        result = asyncio.run(
            asyncio.wait_for(
                analyze(request, owner_id=job["owner_id"]),
                timeout=MAX_JOB_SECONDS if request.mode == "live" and request.depth == "full" else QUICK_REVIEW_TIMEOUT_SECONDS,
            )
        )
        if not complete(job_id, result.status, result.review_id, lease_token):
            _logger.warning(json.dumps({"event": "review_job_lost_lease", "job_id": job_id}))
        _logger.info(
            json.dumps(
                {
                    "event": "review_job_completed",
                    "job_id": job_id,
                    "status": result.status,
                    "duration_ms": result.duration_ms,
                }
            )
        )
    except Exception as exc:  # noqa: BLE001 - persist safe worker outcome, not raw exception details.
        # Keep exception data out of the persistent record and structured logs.
        next_status = fail(
            job,
            error_code="review_timeout"
            if isinstance(exc, TimeoutError)
            else "review_processing_failed",
            retryable=isinstance(exc, GitHubAPIError) and exc.status_code in {429, 500, 502, 503, 504},
        )
        _logger.warning(
            json.dumps(
                {
                    "event": "review_job_failed",
                    "job_id": job_id,
                    "status": next_status,
                    "error_type": type(exc).__name__,
                }
            )
        )
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)


def _process_explanation_job(job: dict) -> None:
    job_id = str(job["job_id"])
    review_id = str(job["review_id"])
    issue_id = str(job["issue_id"])
    owner_id = str(job["owner_id"])
    store = get_store()
    review = store.get_review(review_id, owner_id=owner_id)
    issues = review.get("canonical_issues") if review else None
    matches = [item for item in issues if isinstance(item, dict) and item.get("issue_id") == issue_id] if isinstance(issues, list) else []
    if len(matches) != 1:
        _finish_explanation_failure(
            store,
            job,
            issue_id,
            owner_id,
            "The saved review or issue could not be found.",
            retryable=False,
        )
        return

    if not store.update_issue_explanation(
        review_id,
        issue_id,
        "running",
        owner_id=owner_id,
        failure_reason=None,
    ):
        _finish_explanation_failure(
            store,
            job,
            issue_id,
            owner_id,
            "The saved issue could not be updated.",
            retryable=False,
        )
        return

    try:
        issue = CanonicalIssue.model_validate(matches[0])
        diff_files = review.get("diff_files")
        result = asyncio.run(
            asyncio.wait_for(
                generate_issue_explanations(
                    [issue],
                    diff_files=diff_files if isinstance(diff_files, list) else [],
                    explanation_level=str(job.get("payload", {}).get("explanation_level", "beginner")),
                ),
                timeout=MAX_EXPLANATION_SECONDS + 5,
            )
        )
        explanation = result.explanations.get(issue_id)
        if explanation is None:
            failure_reason = "Could not build explanation context for this issue." if result.context_failure_count else "The AI provider did not return a valid explanation."
            _finish_explanation_failure(
                store,
                job,
                issue_id,
                owner_id,
                failure_reason,
                retryable=result.context_failure_count == 0,
            )
            return

        if not store.update_issue_explanation(
            review_id,
            issue_id,
            "completed",
            owner_id=owner_id,
            explanation=explanation.model_dump(mode="json"),
            failure_reason=None,
        ):
            raise RuntimeError("Explanation result could not be persisted.")
        if not issue_explanation_jobs.complete(job):
            _logger.warning(json.dumps({"event": "issue_explanation_job_lost_lease", "job_id": job_id}))
            return
        _logger.info(json.dumps({"event": "issue_explanation_job_completed", "job_id": job_id}))
    except Exception as exc:  # noqa: BLE001 - Keep provider/storage details out of persistent records.
        _finish_explanation_failure(
            store,
            job,
            issue_id,
            owner_id,
            "Explanation generation failed and will be retried.",
            retryable=True,
        )
        _logger.warning(
            json.dumps(
                {
                    "event": "issue_explanation_job_error",
                    "job_id": job_id,
                    "error_type": type(exc).__name__,
                }
            )
        )


def _finish_explanation_failure(
    store,
    job: dict,
    issue_id: str,
    owner_id: str,
    failure_reason: str,
    *,
    retryable: bool,
) -> None:
    next_status = issue_explanation_jobs.fail(
        job,
        error_code="context_unavailable" if not retryable else "explanation_generation_failed",
        retryable=retryable,
    )
    if next_status == "lease_lost":
        return
    issue_status = "retrying" if next_status == "queued" else "failed"
    user_reason = "Explanation generation failed and will be retried." if issue_status == "retrying" else failure_reason
    store.update_issue_explanation(
        str(job["review_id"]),
        issue_id,
        issue_status,
        owner_id=owner_id,
        failure_reason=user_reason,
    )


def run_worker(stop_event: threading.Event | None = None) -> None:
    stop = stop_event or _stop
    if stop_event is None:
        stop.clear()
        signal.signal(signal.SIGTERM, lambda *_args: stop.set())
        signal.signal(signal.SIGINT, lambda *_args: stop.set())
    while not stop.is_set():
        try:
            did_work = process_one()
        except Exception as exc:  # noqa: BLE001 - keep the supervised polling loop alive.
            _logger.error(
                json.dumps(
                    {
                        "event": "review_worker_loop_error",
                        "error_type": type(exc).__name__,
                    }
                )
            )
            did_work = False
        if not did_work:
            stop.wait(POLL_SECONDS)


if __name__ == "__main__":
    run_worker()

