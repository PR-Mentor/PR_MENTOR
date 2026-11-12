"""Credential-free child process for one sandboxed Stage 6 test attempt."""

from __future__ import annotations

import json
import signal
import sys
import threading
from pathlib import Path

from app.schemas.generated_test import GeneratedTestCase
from app.schemas.issue import FixValidationSnapshot, SuggestedFix
from app.services.fix_eligibility import FixEligibilityResult
from app.test_execution.worker import execute_generated_test

MAX_INPUT_BYTES = 200_000


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    payload_path = Path(sys.argv[1]).resolve(strict=True)
    if payload_path.name != "job.json" or not payload_path.parent.name.startswith("prmentor-stage6-"):
        return 2
    raw = payload_path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        return 2
    payload = json.loads(raw)
    fix = SuggestedFix.model_validate(payload["fix"])
    test = GeneratedTestCase.model_validate(payload["generated_test"])
    eligibility_data = payload["eligibility"]
    eligibility = FixEligibilityResult(
        status=eligibility_data["status"],
        reason=eligibility_data["reason"],
        snapshot=FixValidationSnapshot.model_validate(eligibility_data["snapshot"]),
        baseline_source=eligibility_data["baseline_source"],
        proposed_source=eligibility_data["proposed_source"],
    )
    cancellation = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: cancellation.set())
    result = execute_generated_test(
        fix,
        test,
        eligibility,
        owner=payload["owner"],
        repo=payload["repo"],
        review_id=payload["review_id"],
        execution_id=payload["execution_id"],
        cancellation=cancellation,
    )
    sys.stdout.write(result.model_dump_json())
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, AttributeError):
        # Do not send provider, credential, or source details to worker logs.
        exit_code = 1
    raise SystemExit(exit_code)
