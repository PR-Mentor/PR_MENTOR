"""Durable background review worker; separate from the sandbox executor."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import threading

from app.database.review_jobs import (
    LEASE_SECONDS,
    claim_next,
    complete,
    fail,
    heartbeat,
)
from app.github_service.client import GitHubAPIError
from app.schemas.review import AnalyzeRequest
from app.services.review_orchestrator import analyze

MAX_JOB_SECONDS = 600
POLL_SECONDS = 1.0
_stop = threading.Event()
logging.basicConfig(level=logging.INFO, format="%(message)s")
_logger = logging.getLogger("pr_mentor.review_worker")


def process_one() -> bool:
    job = claim_next()
    if job is None:
        return False
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
                analyze(request, owner_id=job["owner_id"]), timeout=MAX_JOB_SECONDS
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
    return True


def run_worker() -> None:
    signal.signal(signal.SIGTERM, lambda *_args: _stop.set())
    signal.signal(signal.SIGINT, lambda *_args: _stop.set())
    while not _stop.is_set():
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
            _stop.wait(POLL_SECONDS)


if __name__ == "__main__":
    run_worker()
