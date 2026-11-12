"""Stage 5 orchestration: generate bounded candidate tests, never execute them."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from app.ai_review import client
from app.ai_review.client import AIReviewUnavailable
from app.ai_review.generated_test_context import (
    InsufficientTestContext,
    build_test_generation_context,
)
from app.ai_review.generated_test_prompts import (
    MAX_PROMPT_CHARS,
    SYSTEM_PROMPT,
    build_test_generation_prompt,
)
from app.ai_review.generated_test_quality import review_candidate
from app.schemas.generated_test import (
    MAX_GENERATED_TESTS,
    MAX_PROVIDER_CALLS,
    MAX_REJECTED_TESTS,
    GeneratedTestCase,
    RejectedGeneratedTest,
    TestGenerationResult,
)
from app.schemas.issue import (
    CanonicalIssue,
    FixValidationCheckResult,
    FixValidationEvidenceReference,
    SuggestedFix,
)
from app.services.fix_eligibility import FixEligibilityResult, evaluate_fix_eligibility

MAX_GENERATION_SECONDS = 60.0
MAX_PROVIDER_SECONDS = 35.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_AMBIGUITIES = 5
MAX_GENERATION_TEXT = 500
GENERATION_CONFIGURATION = {
    "contract": "stage5-testgen-v1",
    "max_generation_seconds": MAX_GENERATION_SECONDS,
    "max_provider_seconds": MAX_PROVIDER_SECONDS,
    "max_response_bytes": MAX_RESPONSE_BYTES,
    "max_tests": MAX_GENERATED_TESTS,
    "max_rejected_tests": MAX_REJECTED_TESTS,
    "max_prompt_chars": MAX_PROMPT_CHARS,
    "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
}


async def generate_tests_for_fix(
    fix: SuggestedFix,
    issue: CanonicalIssue,
    *,
    owner: str,
    repo: str,
    pull_request_number: int,
) -> TestGenerationResult:
    """Generate static-reviewed candidates for one fix against a fresh snapshot."""
    started = datetime.now(UTC)
    started_monotonic = asyncio.get_running_loop().time()
    generation_id = uuid4().hex
    model = client.configured_model("test_generation")
    configuration_digest = hashlib.sha256(
        json.dumps(GENERATION_CONFIGURATION, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    fingerprint = _request_fingerprint(fix, owner, repo, model, configuration_digest)
    eligibility: FixEligibilityResult | None = None
    try:
        eligibility = await asyncio.wait_for(
            evaluate_fix_eligibility(
                fix, owner=owner, repo=repo, pull_request_number=pull_request_number
            ),
            timeout=MAX_GENERATION_SECONDS - 1,
        )
    except asyncio.CancelledError:
        raise
    except (TimeoutError, RuntimeError, OSError, ValueError, TypeError):
        return _result(
            generation_id, fingerprint, "unavailable", fix, issue, started,
            configuration_digest, model, reason="Source snapshot verification was unavailable.",
            error_code="snapshot_unavailable",
        )
    if eligibility.status != "eligible":
        status = "unavailable" if eligibility.status == "unavailable" else "not_attempted"
        return _result(
            generation_id, fingerprint, status, fix, issue, started,
            configuration_digest, model, reason=eligibility.reason,
            error_code=f"snapshot_{eligibility.status}",
        )
    try:
        context = build_test_generation_context(
            fix, issue, owner=owner, repo=repo, eligibility=eligibility
        )
    except (InsufficientTestContext, ValueError, TypeError):
        return _result(
            generation_id, fingerprint, "not_attempted", fix, issue, started,
            configuration_digest, model,
            reason="Verified source context was insufficient to ground test generation.",
            error_code="insufficient_context",
            eligibility=eligibility,
        )
    fingerprint = _request_fingerprint(fix, owner, repo, model, configuration_digest, context.snapshot)
    if not client.is_configured():
        return _result(
            generation_id, fingerprint, "unavailable", fix, issue, started,
            configuration_digest, model, reason="The configured AI provider is unavailable.",
            error_code="provider_unavailable", eligibility=eligibility,
        )
    try:
        prompt = build_test_generation_prompt(context.prompt_data)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError("Prompt exceeds configured bound.")
    except ValueError:
        return _result(
            generation_id, fingerprint, "not_attempted", fix, issue, started,
            configuration_digest, model, reason="Verified context exceeded the prompt limit.",
            error_code="prompt_too_large", eligibility=eligibility, context=context,
        )

    provider_timeout = min(
        MAX_PROVIDER_SECONDS,
        MAX_GENERATION_SECONDS - (asyncio.get_running_loop().time() - started_monotonic),
    )
    if provider_timeout <= 0:
        return _result(
            generation_id, fingerprint, "unavailable", fix, issue, started,
            configuration_digest, model,
            reason="The generation time budget was exhausted before provider execution.",
            error_code="time_budget_exhausted", eligibility=eligibility, context=context,
        )
    try:
        _raw, response = await asyncio.wait_for(
            client.complete_validated_json(
                "test_generation", SYSTEM_PROMPT, prompt, _parse_response, timeout=provider_timeout,
                max_response_bytes=MAX_RESPONSE_BYTES,
            ),
            timeout=provider_timeout,
        )
    except asyncio.CancelledError:
        raise
    except (AIReviewUnavailable, TimeoutError, ValueError, TypeError):
        failed_call = client.last_call_metadata() or {}
        return _result(
            generation_id, fingerprint, "unavailable", fix, issue, started,
            configuration_digest, model, reason="The AI provider did not return a usable response.",
            error_code="provider_failure", eligibility=eligibility, context=context,
            attempts=1 if failed_call.get("attempt_count", 0) else 0,
            calls=min(MAX_PROVIDER_CALLS, failed_call.get("attempt_count", 0)),
        )
    provider_metadata = client.last_call_metadata() or {}
    model = provider_metadata.get("model") or model
    provider = provider_metadata.get("provider") or "unknown"
    provider_call_count = min(MAX_PROVIDER_CALLS, provider_metadata.get("attempt_count", 1))
    accepted: list[GeneratedTestCase] = []
    rejected: list[RejectedGeneratedTest] = []
    seen: set[str] = set()
    for index, candidate in enumerate(response["tests"]):
        if index >= MAX_GENERATED_TESTS:
            rejected.append(_rejected(index, candidate, "Candidate count exceeds the six-test limit."))
            continue
        if not isinstance(candidate, dict):
            rejected.append(_rejected(index, candidate, "Candidate must be a JSON object."))
            continue
        review = review_candidate(candidate, context, source_path=fix.file_path)
        if review.digest in seen:
            rejected.append(_rejected(index, candidate, "Duplicate generated test candidate.", review.digest))
            continue
        seen.add(review.digest)
        try:
            case = _build_case(candidate, review, fix, issue, context, model, provider, configuration_digest, started, index)
        except (ValidationError, ValueError, TypeError, KeyError):
            case = None
        if case is None or not review.accepted:
            reason = review.rejection_reason or "Candidate fields did not satisfy the generated-test schema."
            rejected.append(_rejected(index, candidate, reason, review.digest))
        else:
            accepted.append(case)
    rejected = rejected[:MAX_REJECTED_TESTS]
    if accepted and rejected:
        status = "partial"
    elif accepted:
        status = "generated"
    elif rejected:
        status = "rejected_by_quality"
    else:
        status = "failed"
    ambiguities = [value[:MAX_GENERATION_TEXT] for value in response["ambiguities"][:MAX_AMBIGUITIES]]
    return _result(
        generation_id, fingerprint, status, fix, issue, started,
        configuration_digest, model, tests=accepted, rejected=rejected,
        ambiguities=ambiguities,
        reason="Generated candidates are static-screened suggestions; none were executed.",
        error_code=None if accepted else "no_acceptable_candidates",
        eligibility=eligibility, context=context, attempts=1, calls=provider_call_count, provider=provider,
    )


def _parse_response(raw: str) -> dict[str, list]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("response size exceeds limit")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"ambiguities", "tests"}:
        raise ValueError("response must have exactly ambiguities and tests")
    if not isinstance(value["tests"], list) or len(value["tests"]) > MAX_REJECTED_TESTS:
        raise ValueError("candidate list exceeds hard response bound")
    if not isinstance(value["ambiguities"], list) or len(value["ambiguities"]) > 20:
        raise ValueError("ambiguity list exceeds hard response bound")
    if any(not isinstance(item, str) or not item.strip() for item in value["ambiguities"]):
        raise ValueError("ambiguities must be nonblank strings")
    return value


def _build_case(candidate, review, fix, issue, context, model, provider, config_digest, started, index):
    refs = list(review.references)
    test_name = candidate.get("test_name")
    code = candidate.get("test_code")
    if not isinstance(test_name, str) or not isinstance(code, str):
        return None
    case_id = hashlib.sha256(f"{fix.fix_id}:{context.source_snapshot_id}:{index}:{review.digest}".encode()).hexdigest()[:32]
    return GeneratedTestCase(
        test_id=case_id, suggested_fix_id=fix.fix_id, canonical_issue_id=issue.issue_id,
        repository_id=context.repository_id, source_snapshot_id=context.source_snapshot_id,
        base_commit_sha=context.snapshot["base_commit_sha"],
        checked_head_sha=context.snapshot["checked_head_sha"],
        baseline_blob_sha=context.snapshot["baseline_blob_sha"],
        proposed_blob_sha=context.snapshot["proposed_blob_sha"],
        source_file_path=fix.file_path, test_name=test_name, test_code=code,
        test_category=candidate.get("category"), test_description=candidate.get("description"),
        rationale=candidate.get("rationale"), expected_behavior=candidate.get("expected_behavior"),
        source_references=refs, generation_model=model or "unavailable",
        generation_provider=provider, generation_configuration_sha256=config_digest,
        generation_timestamp=started, quality_assessment=review.assessment,
        limitations=["Candidate not executed; human review and Stage 6 isolation are required."],
    )


def _rejected(index: int, candidate: object, reason: str, digest: str | None = None) -> RejectedGeneratedTest:
    safe_code = candidate.get("test_code") if isinstance(candidate, dict) else None
    safe_name = candidate.get("test_name") if isinstance(candidate, dict) else None
    code = safe_code[:12_000] if isinstance(safe_code, str) else None
    name = safe_name[:160] if isinstance(safe_name, str) else None
    digest = digest or hashlib.sha256((code or repr(candidate)[:2_000]).encode("utf-8", errors="replace")).hexdigest()
    return RejectedGeneratedTest(candidate_index=index, test_name=name, test_code=code, reason=reason[:MAX_GENERATION_TEXT], sha256=digest)


def _result(
    generation_id, fingerprint, status, fix, issue, started, config_digest, model, *,
    tests=None, rejected=None, ambiguities=None, reason=None, error_code=None,
    eligibility=None, context=None, attempts=0, calls=0,
    provider="unknown",
):
    completed = datetime.now(UTC)
    snapshot = eligibility.snapshot if eligibility and eligibility.status == "eligible" else None
    snapshot_data = context.snapshot if context else (
        snapshot.model_dump() if snapshot else {}
    )
    evidence = [
        FixValidationEvidenceReference(
            evidence_id=f"generated-test-{test.test_id}", kind="other",
            description="Unexecuted AI-generated test candidate; not a test result.",
            file_path=test.source_file_path,
            sha256=hashlib.sha256(test.test_code.encode()).hexdigest(),
        ) for test in tests or []
    ]
    check = FixValidationCheckResult(
        check_id="stage5-generated-tests", check_name="AI-generated tests (not executed)",
        provenance_profile="ai_assessment", effective_provenance_profile="ai_assessment",
        status="not_run", required=False, tool_name=provider, tool_version=model,
        configuration_sha256=config_digest, started_at=started, completed_at=completed,
        evidence=evidence, reason="Generation and static screening only; test execution status is not_run.",
    )
    return TestGenerationResult(
        generation_id=generation_id, request_fingerprint_sha256=fingerprint,
        status=status, suggested_fix_id=fix.fix_id, canonical_issue_id=issue.issue_id,
        repository_id=context.repository_id if context else None,
        source_snapshot_id=context.source_snapshot_id if context else None,
        base_commit_sha=snapshot_data.get("base_commit_sha"),
        checked_head_sha=snapshot_data.get("checked_head_sha"),
        baseline_blob_sha=snapshot_data.get("baseline_blob_sha"),
        proposed_blob_sha=snapshot_data.get("proposed_blob_sha"),
        source_file_path=fix.file_path, generation_model=model, generation_provider=provider,
        generation_configuration_sha256=config_digest, generation_timestamp=started,
        completed_at=completed, generation_attempts=attempts, provider_call_count=calls,
        tests=tests or [], rejected_tests=rejected or [], ambiguities=ambiguities or [],
        limitations=["Generated test cases are suggestions and have not been executed."],
        error_code=error_code, reason=reason, validation_check=check,
    )


def _request_fingerprint(fix, owner, repo, model, config_digest, snapshot=None):
    payload = json.dumps({
        "fix_id": fix.fix_id, "owner": owner, "repo": repo, "base": fix.base_commit_sha,
        "blob": fix.file_blob_sha, "model": model, "configuration": config_digest,
        "verified_snapshot": snapshot,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
