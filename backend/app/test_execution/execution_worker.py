"""Durable trusted coordinator for isolated test-execution jobs."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from app.database import test_execution_jobs as jobs
from app.test_execution.contracts import TestExecutionResult
from app.verification.bounded_process import BoundedProcessError, run_bounded_process

# Two sequential, independently cleaned sandbox attempts at a 120s cap each.
MAX_CHILD_SECONDS = 340
POLL_SECONDS = 1.0
MAX_CHILD_STDOUT = 4_500_000
_stop = threading.Event()
logging.basicConfig(level=logging.INFO, format="%(message)s")
_logger = logging.getLogger("pr_mentor.test_execution_worker")


def process_one() -> bool:
    job = jobs.claim_next()
    if job is None:
        return False
    execution_id = job["execution_id"]
    heartbeat_stop = threading.Event()

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(jobs.LEASE_SECONDS / 3):
            if not jobs.heartbeat(execution_id):
                return

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop, name=f"execution-lease-{execution_id[:8]}", daemon=True
    )
    heartbeat_thread.start()
    try:
        payload: dict[str, Any] = dict(job["payload"])
        payload["execution_id"] = execution_id
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(raw) > jobs.MAX_JOB_PAYLOAD_BYTES:
            jobs.fail(execution_id, "execution_payload_invalid")
            return True
        with tempfile.TemporaryDirectory(prefix="prmentor-stage6-") as temporary:
            root = Path(temporary).resolve(strict=True)
            input_path = root / "job.json"
            descriptor = os.open(input_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            result = run_bounded_process(
                [sys.executable, "-m", "app.test_execution.execution_process", str(input_path)],
                cwd=Path(__file__).resolve().parents[2],
                env=_child_environment(),
                timeout=MAX_CHILD_SECONDS,
                max_stdout_bytes=MAX_CHILD_STDOUT,
                max_stderr_bytes=8_192,
                cancellation_check=lambda: jobs.cancellation_requested(execution_id),
            )
            if result.returncode != 0:
                jobs.fail(execution_id, "execution_child_failed")
                return True
            execution = TestExecutionResult.model_validate_json(result.stdout)
            test = payload["generated_test"]
            if (
                execution.execution_id != execution_id
                or execution.review_id != payload.get("review_id")
                or execution.test_id != test["test_id"]
                or execution.repository_id != test["repository_id"]
                or execution.canonical_issue_id != test["canonical_issue_id"]
                or execution.suggested_fix_id != test["suggested_fix_id"]
                or execution.source_snapshot_id != test["source_snapshot_id"]
                or execution.base_commit_sha.casefold() != test["base_commit_sha"].casefold()
                or execution.checked_head_sha.casefold() != test["checked_head_sha"].casefold()
                or execution.baseline_blob_sha.casefold() != test["baseline_blob_sha"].casefold()
                or execution.proposed_blob_sha.casefold() != test["proposed_blob_sha"].casefold()
                or execution.baseline_execution is None
                or execution.baseline_execution.test_id != test["test_id"]
                or execution.baseline_execution.source_snapshot_id != test["source_snapshot_id"]
            ):
                jobs.fail(execution_id, "execution_result_identity_mismatch")
                return True
            if result.cancelled and execution.status != "cancelled":
                jobs.fail(execution_id, "execution_cancelled_without_cleanup_result")
                return True
            if not jobs.complete(execution_id, execution.status, execution.model_dump(mode="json")):
                _logger.warning(json.dumps({"event": "execution_job_lost_lease", "execution_id": execution_id}))
            else:
                try:
                    from app.test_execution.execution_reports import (
                        persist_execution_report,
                    )

                    persist_execution_report(payload, execution)
                except Exception as exc:  # noqa: BLE001 - result remains durable in its job record.
                    _logger.error(json.dumps({
                        "event": "execution_report_persistence_failed",
                        "execution_id": execution_id,
                        "error_type": type(exc).__name__,
                    }))
    except BoundedProcessError:
        jobs.fail(execution_id, "execution_child_timeout_or_output_limit")
    except Exception as exc:  # noqa: BLE001 - durable failure with safe type-only diagnostics.
        jobs.fail(execution_id, "execution_worker_failed")
        _logger.error(json.dumps({"event": "execution_worker_failure", "execution_id": execution_id, "error_type": type(exc).__name__}))
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
    return True


def _child_environment() -> dict[str, str]:
    allowed = {
        "PATH", "LANG", "TMPDIR", "DOCKER_HOST", "XDG_RUNTIME_DIR", "PR_MENTOR_DOCKER_EXECUTABLE",
        "PR_MENTOR_TEST_EXECUTION_IMAGE", "PR_MENTOR_TEST_EXECUTION_TIMEOUT_SECONDS",
        "PR_MENTOR_TEST_EXECUTION_CPU_COUNT", "PR_MENTOR_TEST_EXECUTION_MEMORY_BYTES",
        "PR_MENTOR_TEST_EXECUTION_PROCESS_COUNT", "PR_MENTOR_TEST_EXECUTION_SCRATCH_BYTES",
        "PR_MENTOR_TEST_EXECUTION_OUTPUT_BYTES", "PR_MENTOR_TEST_EXECUTION_CONCURRENCY",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["PR_MENTOR_EXECUTION_WORKER"] = "1"
    return env


def run_worker() -> None:
    signal.signal(signal.SIGTERM, lambda *_args: _stop.set())
    signal.signal(signal.SIGINT, lambda *_args: _stop.set())
    while not _stop.is_set():
        try:
            worked = process_one()
        except Exception as exc:  # noqa: BLE001 - keep supervised polling loop alive.
            _logger.error(json.dumps({"event": "execution_worker_loop_error", "error_type": type(exc).__name__}))
            worked = False
        if not worked:
            _stop.wait(POLL_SECONDS)


if __name__ == "__main__":
    run_worker()
