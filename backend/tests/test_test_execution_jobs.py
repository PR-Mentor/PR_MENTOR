import time

import mongomock
import pytest

from app.database import test_execution_jobs as jobs


def prepare(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", backend)
    database = None
    if backend == "sqlite":
        monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "execution.sqlite3"))
    else:
        database = mongomock.MongoClient(tz_aware=True)["execution"]
        monkeypatch.setattr(jobs, "_db", lambda: database)
    return database


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_execution_job_idempotency_owner_scope_and_result_round_trip(backend, tmp_path, monkeypatch):
    prepare(backend, tmp_path, monkeypatch)
    payload = {"generated_test": {"test_id": "test-a", "test_code": "assert True"}, "source": "bounded"}
    first = jobs.enqueue("owner-a", "request-key-123456", payload)
    duplicate = jobs.enqueue("owner-a", "request-key-123456", payload)
    assert first["execution_id"] == duplicate["execution_id"]
    assert jobs.get_job(first["execution_id"], "owner-b") is None
    claimed = jobs.claim_next()
    assert claimed["execution_id"] == first["execution_id"]
    assert claimed["payload"] == payload
    result = {"execution_id": first["execution_id"], "status": "passed", "stdout": "1 passed"}
    assert jobs.complete(first["execution_id"], "passed", result)
    restored = jobs.get_job(first["execution_id"], "owner-a")
    assert restored["status"] == "passed"
    assert restored["result"] == result
    assert restored["generated_test"] == payload["generated_test"]


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_execution_idempotency_key_cannot_be_reused_for_different_payload(backend, tmp_path, monkeypatch):
    prepare(backend, tmp_path, monkeypatch)
    jobs.enqueue("owner", "same-key-12345678", {"generated_test": {"test_id": "first"}})
    with pytest.raises(jobs.IdempotencyConflict):
        jobs.enqueue("owner", "same-key-12345678", {"generated_test": {"test_id": "different"}})


@pytest.mark.parametrize("backend", ["sqlite", "mongodb"])
def test_execution_jobs_cancel_queued_and_request_running_cancellation(backend, tmp_path, monkeypatch):
    database = prepare(backend, tmp_path, monkeypatch)
    queued = jobs.enqueue("owner", "queued-key-123456", {"generated_test": {"test_id": "t1"}})
    assert jobs.cancel(queued["execution_id"], "owner") == "cancelled"
    assert jobs.get_job(queued["execution_id"], "owner")["status"] == "cancelled"
    running = jobs.enqueue("owner", "running-key-1234", {"generated_test": {"test_id": "t2"}})
    jobs.claim_next()
    assert jobs.cancel(running["execution_id"], "owner") == "cancellation_requested"
    assert jobs.cancellation_requested(running["execution_id"])
    if backend == "mongodb":
        assert database["test_execution_jobs"].find_one({"execution_id": running["execution_id"]})["cancel_requested"] is True


def test_expired_execution_lease_fails_closed_without_rerunning(tmp_path, monkeypatch):
    prepare("sqlite", tmp_path, monkeypatch)
    now = time.time()
    job = jobs.enqueue("owner", "lease-key-123456", {"generated_test": {"test_id": "t"}})
    assert jobs.claim_next(now=now) is not None
    from app.database.connection import connection

    with connection() as db:
        db.execute(
            "UPDATE test_execution_jobs SET lease_until=? WHERE execution_id=?",
            (now - 1, job["execution_id"]),
        )
    assert jobs.heartbeat(job["execution_id"], now=now) is False
    assert jobs.complete(job["execution_id"], "passed", {"status": "passed"}) is False
    assert jobs.fail(job["execution_id"], "late-worker") is False
    assert jobs.claim_next(now=now) is None
    restored = jobs.get_job(job["execution_id"], "owner")
    assert restored["status"] == "infrastructure_error"
    assert restored["error_code"] == "worker_lease_expired"
