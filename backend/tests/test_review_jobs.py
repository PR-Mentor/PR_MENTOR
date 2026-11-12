import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import mongomock
import pytest

from app.database import review_jobs


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_durable_review_job_lifecycle_is_owner_scoped_and_retries_are_bounded(
    backend, tmp_path, monkeypatch
):
    monkeypatch.setenv("PR_MENTOR_STORAGE", backend)
    if backend == "sqlite":
        monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    else:
        database = mongomock.MongoClient(tz_aware=True)["jobs"]
        monkeypatch.setattr(review_jobs, "_db", lambda: database)

    created = review_jobs.enqueue(
        "user-a", {"url": "https://github.com/a/b/pull/1", "mode": "demo"}
    )
    assert created["status"] == "queued"
    assert review_jobs.get_job(created["job_id"], "user-b") is None
    claimed = review_jobs.claim_next()
    assert claimed["job_id"] == created["job_id"]
    assert claimed["attempts"] == 1
    assert claimed["payload"]["mode"] == "demo"

    now = time.time()
    assert review_jobs.fail(claimed, retryable=True, now=now) == "queued"
    assert review_jobs.get_job(created["job_id"], "user-a")["status"] == "queued"
    # Delay is bounded and attempt count caps retry claims at three.
    retry = review_jobs.claim_next(now=now + 100)
    assert retry["attempts"] == 2
    assert review_jobs.fail(retry, retryable=True, now=now + 100) == "queued"
    final_attempt = review_jobs.claim_next(now=now + 200)
    assert final_attempt["attempts"] == 3
    assert review_jobs.fail(final_attempt, retryable=True, now=now + 200) == "failed"
    assert review_jobs.get_job(created["job_id"], "user-a")["status"] == "failed"


def test_queued_job_can_be_cancelled_but_claimed_job_cannot(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "cancel.sqlite3"))
    queued = review_jobs.enqueue("user-a", {"mode": "demo"})
    assert review_jobs.cancel_queued(queued["job_id"], "user-a") is True
    assert review_jobs.get_job(queued["job_id"], "user-a")["status"] == "cancelled"
    assert review_jobs.cancel_queued(queued["job_id"], "user-a") is False


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_expired_final_worker_lease_is_recovered_as_failed(
    backend, tmp_path, monkeypatch
):
    monkeypatch.setenv("PR_MENTOR_STORAGE", backend)
    if backend == "sqlite":
        monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "expired.sqlite3"))
    else:
        database = mongomock.MongoClient(tz_aware=True)["expired"]
        monkeypatch.setattr(review_jobs, "_db", lambda: database)
    job = review_jobs.enqueue("user-a", {"mode": "demo"})
    current = time.time()
    assert review_jobs.claim_next(now=current, lease_seconds=60) is not None
    if backend == "mongodb":
        database["review_jobs"].update_one(
            {"job_id": job["job_id"]},
            {
                "$set": {
                    "attempts": 3,
                    "status": "running",
                    "lease_until": datetime.fromtimestamp(current + 60, UTC),
                }
            },
        )
    else:
        from app.database.connection import connection

        with connection() as db:
            db.execute(
                "UPDATE review_jobs SET attempts=3, status='running', lease_until=? WHERE job_id=?",
                (current + 60, job["job_id"]),
            )
    assert review_jobs.claim_next(now=current + 61) is None
    assert review_jobs.get_job(job["job_id"], "user-a")["status"] == "failed"


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_review_job_crash_lease_fails_closed_and_stale_worker_is_fenced(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", backend)
    if backend == "sqlite":
        monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "fenced.sqlite3"))
    else:
        database = mongomock.MongoClient(tz_aware=True)["fenced"]
        monkeypatch.setattr(review_jobs, "_db", lambda: database)
    queued = review_jobs.enqueue("owner", {"mode": "live"})
    now = time.time()
    old_worker = review_jobs.claim_next(now=now, lease_seconds=5)
    assert old_worker and old_worker["lease_token"]
    assert review_jobs.claim_next(now=now + 1) is None
    assert review_jobs.claim_next(now=now + 6) is None
    assert review_jobs.get_job(queued["job_id"], "owner")["status"] == "failed"
    assert review_jobs.heartbeat(queued["job_id"], old_worker["lease_token"], now=now + 6) is False
    assert review_jobs.complete(queued["job_id"], "completed", "review", old_worker["lease_token"]) is False
    assert review_jobs.fail(old_worker, retryable=True, now=now + 6) == "lease_lost"


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_only_explicitly_safe_review_failures_are_retried(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", backend)
    if backend == "sqlite":
        monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "retry-policy.sqlite3"))
    else:
        database = mongomock.MongoClient(tz_aware=True)["retry-policy"]
        monkeypatch.setattr(review_jobs, "_db", lambda: database)
    queued = review_jobs.enqueue("owner", {"mode": "live"})
    claimed = review_jobs.claim_next()
    assert claimed
    assert review_jobs.fail(claimed, error_code="provider_side_effect_unknown") == "failed"
    assert review_jobs.get_job(queued["job_id"], "owner")["attempts"] == 1


def test_concurrent_review_workers_claim_a_job_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "concurrent.sqlite3"))
    review_jobs.enqueue("owner", {"mode": "live"})
    with ThreadPoolExecutor(max_workers=2) as workers:
        claimed = list(workers.map(lambda _: review_jobs.claim_next(), range(2)))
    assert sum(item is not None for item in claimed) == 1
