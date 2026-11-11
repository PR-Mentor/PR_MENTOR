"""Internal Stage 6 worker; never called by FastAPI request handlers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from app.github_service.pull_requests import (
    GitHubFileSnapshotError,
    git_blob_sha,
    validate_repository_python_path,
)
from app.schemas.generated_test import GeneratedTestCase
from app.schemas.issue import SuggestedFix
from app.services.fix_eligibility import FixEligibilityResult
from app.test_execution.contracts import (
    ExecutionArtifact,
    ExecutionResourceLimits,
    ExecutionStatus,
    SourceExecutionAttempt,
    TestExecutionResult,
    classify_regression,
)
from app.test_execution.runtime import (
    DockerGVisorRuntime,
    ExecutionRuntime,
    RuntimeCompletion,
    RuntimeHandle,
    RuntimeInfrastructureError,
    RuntimeMetadata,
    RuntimeUnavailable,
)
from app.test_execution.settings import load_execution_settings
from app.verification.fix_snapshot import reconstruct_proposed_source

DEFAULT_LIMITS = ExecutionResourceLimits(
    timeout_seconds=30,
    cpu_count=1,
    memory_bytes=536_870_912,
    process_count=32,
    scratch_bytes=67_108_864,
    output_bytes=128_000,
    concurrent_executions=2,
)
MAX_ARTIFACT_BYTES = 120_000
_WORKER_SEMAPHORE_LOCK = threading.Lock()
_WORKER_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}
_ACTIVE_SLOT = threading.local()


class ExecutionInputRejected(ValueError):
    """Identity, source, or path verification failed before sandbox creation."""


def build_execution_artifact(
    fix: SuggestedFix,
    generated_test: GeneratedTestCase,
    eligibility: FixEligibilityResult,
    *,
    owner: str,
    repo: str,
) -> ExecutionArtifact:
    """Verify backend-owned Stage 2/5 records and freeze an exact source input."""
    if eligibility.status != "eligible" or eligibility.snapshot is None:
        raise ExecutionInputRejected(
            "A currently eligible Stage 2 snapshot is required."
        )
    if not isinstance(eligibility.baseline_source, str) or not isinstance(
        eligibility.proposed_source, str
    ):
        raise ExecutionInputRejected(
            "Verified baseline and proposed source are required."
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", repo
    ):
        raise ExecutionInputRejected("Repository identity is invalid.")
    repository_id = f"{owner}/{repo}"
    snapshot = eligibility.snapshot
    try:
        validate_repository_python_path(fix.file_path)
    except GitHubFileSnapshotError as exc:
        raise ExecutionInputRejected("Execution source path is unsafe.") from exc
    if snapshot.file_path != fix.file_path:
        raise ExecutionInputRejected("Snapshot path does not match the fix.")
    if (
        fix.canonical_issue_id != generated_test.canonical_issue_id
        or fix.fix_id != generated_test.suggested_fix_id
        or generated_test.repository_id.casefold() != repository_id.casefold()
        or generated_test.source_file_path != fix.file_path
    ):
        raise ExecutionInputRejected(
            "Generated test, fix, issue, or repository identity mismatch."
        )
    if not any(
        reference.source_version in {"baseline", "proposed"}
        and reference.file_path == fix.file_path
        for reference in generated_test.source_references
    ):
        raise ExecutionInputRejected(
            "Generated test lacks a source reference for the fix file."
        )
    snapshot_data = snapshot.model_dump(mode="json")
    identity = {"repository_id": repository_id, **snapshot_data}
    expected_snapshot_id = (
        "snapshot-"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
    )
    if generated_test.source_snapshot_id != expected_snapshot_id:
        raise ExecutionInputRejected(
            "Generated test belongs to a different source snapshot."
        )
    base_commit_sha = snapshot.base_commit_sha
    checked_head_sha = snapshot.checked_head_sha
    baseline_blob_sha = snapshot.baseline_blob_sha
    proposed_blob_sha = snapshot.proposed_blob_sha
    if (
        not isinstance(base_commit_sha, str)
        or not isinstance(checked_head_sha, str)
        or not isinstance(baseline_blob_sha, str)
        or not isinstance(proposed_blob_sha, str)
    ):
        raise ExecutionInputRejected("Verified snapshot is missing a full source hash.")
    snapshot_hashes: tuple[str, str, str, str] = (
        base_commit_sha,
        checked_head_sha,
        baseline_blob_sha,
        proposed_blob_sha,
    )
    if any(
        re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value) is None
        for value in snapshot_hashes
    ):
        raise ExecutionInputRejected("Verified snapshot is missing a full source hash.")
    for field, generated_value, snapshot_value in zip(
        ("base_commit_sha", "checked_head_sha", "baseline_blob_sha", "proposed_blob_sha"),
        (
            generated_test.base_commit_sha,
            generated_test.checked_head_sha,
            generated_test.baseline_blob_sha,
            generated_test.proposed_blob_sha,
        ),
        snapshot_hashes,
    ):
        if not isinstance(snapshot_value, str) or generated_value.casefold() != snapshot_value.casefold():
            raise ExecutionInputRejected(
                f"Generated test {field} does not match the verified snapshot."
            )

    baseline = eligibility.baseline_source
    proposed = eligibility.proposed_source
    if not isinstance(baseline_blob_sha, str) or not isinstance(proposed_blob_sha, str):
        raise ExecutionInputRejected("Verified source blob hashes are missing.")
    if (
        git_blob_sha(baseline.encode("utf-8")).casefold()
        != baseline_blob_sha.casefold()
    ):
        raise ExecutionInputRejected(
            "Baseline source does not match its Stage 2 blob hash."
        )
    if (
        git_blob_sha(proposed.encode("utf-8")).casefold()
        != proposed_blob_sha.casefold()
    ):
        raise ExecutionInputRejected(
            "Proposed source does not match its Stage 2 blob hash."
        )
    try:
        reconstructed = reconstruct_proposed_source(baseline, fix)
    except (GitHubFileSnapshotError, UnicodeError, ValueError) as exc:
        raise ExecutionInputRejected(
            "Proposed source cannot be reconstructed from the verified fix."
        ) from exc
    if (
        reconstructed.text != proposed
        or reconstructed.blob_sha.casefold() != proposed_blob_sha.casefold()
    ):
        raise ExecutionInputRejected(
            "Proposed source differs from the Stage 2 fix reconstruction."
        )
    source_bytes = baseline.encode("utf-8", errors="strict") + proposed.encode(
        "utf-8", errors="strict"
    )
    test_bytes = generated_test.test_code.encode("utf-8", errors="strict")
    if len(source_bytes) + len(test_bytes) > MAX_ARTIFACT_BYTES:
        raise ExecutionInputRejected(
            "Execution input exceeds the Stage 6 artifact size limit."
        )
    return ExecutionArtifact(
        repository_id=repository_id,
        canonical_issue_id=fix.canonical_issue_id,
        suggested_fix_id=fix.fix_id,
        test_id=generated_test.test_id,
        source_snapshot_id=expected_snapshot_id,
        base_commit_sha=base_commit_sha,
        checked_head_sha=checked_head_sha,
        baseline_blob_sha=baseline_blob_sha,
        proposed_blob_sha=proposed_blob_sha,
        source_file_path=fix.file_path,
        baseline_source=baseline,
        proposed_source=proposed,
        generated_test_code=generated_test.test_code,
    )


def execute_generated_test(
    fix: SuggestedFix,
    generated_test: GeneratedTestCase,
    eligibility: FixEligibilityResult,
    *,
    owner: str,
    repo: str,
    runtime: ExecutionRuntime | None = None,
    limits: ExecutionResourceLimits | None = None,
    cancellation: threading.Event | None = None,
    execution_id: str | None = None,
    review_id: str | None = None,
) -> TestExecutionResult:
    """Run identical tests in separate isolated baseline and proposed containers."""
    identifier = execution_id or uuid4().hex
    baseline_execution_id = (
        f"{identifier[:96]}-baseline-"
        + hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]
    )
    baseline = _execute_single_test(
        fix,
        generated_test,
        eligibility,
        owner=owner,
        repo=repo,
        runtime=runtime,
        limits=limits,
        cancellation=cancellation,
        execution_id=baseline_execution_id,
        review_id=review_id,
        source_variant="baseline",
    )
    if not baseline.cleanup_confirmed:
        proposed = _result(
            identifier,
            fix,
            generated_test,
            "infrastructure_error",
            datetime.now(UTC),
            time.monotonic(),
            limits or DEFAULT_LIMITS,
            reason="Baseline sandbox cleanup could not be confirmed; proposed execution was not started.",
            cleanup_confirmed=False,
            review_id=review_id,
        )
    else:
        proposed = _execute_single_test(
            fix,
            generated_test,
            eligibility,
            owner=owner,
            repo=repo,
            runtime=runtime,
            limits=limits,
            cancellation=cancellation,
            execution_id=identifier,
            review_id=review_id,
            source_variant="proposed",
        )
    same_environment = _attempt_environment_matches(baseline, proposed)
    baseline_attempt = _as_baseline_attempt(baseline)
    payload = proposed.model_dump(mode="python")
    payload.update(
        review_id=review_id,
        baseline_execution=baseline_attempt,
        regression_status=classify_regression(
            baseline.status, proposed.status, same_environment=same_environment
        ),
    )
    return TestExecutionResult.model_validate(payload)


def _execute_single_test(
    fix: SuggestedFix,
    generated_test: GeneratedTestCase,
    eligibility: FixEligibilityResult,
    *,
    owner: str,
    repo: str,
    runtime: ExecutionRuntime | None,
    limits: ExecutionResourceLimits | None,
    cancellation: threading.Event | None,
    execution_id: str,
    review_id: str | None,
    source_variant: str,
) -> TestExecutionResult:
    """Run one source variant in one newly created isolated runtime."""
    started_at = datetime.now(UTC)
    started = time.monotonic()
    if limits is None:
        try:
            settings = load_execution_settings()
            limits = settings.limits
            if runtime is None and settings.image_digest:
                runtime = DockerGVisorRuntime(
                    image_digest=settings.image_digest,
                    docker_executable=settings.docker_executable,
                )
        except ValueError as exc:
            limits = DEFAULT_LIMITS
            return _result(
                execution_id,
                fix,
                generated_test,
                "unavailable",
                started_at,
                started,
                limits,
                reason=f"Unsafe worker configuration: {exc}",
                cleanup_confirmed=True,
                review_id=review_id,
            )
    assert limits is not None
    cancel_event = cancellation or threading.Event()
    artifact: ExecutionArtifact | None = None
    try:
        artifact = build_execution_artifact(
            fix, generated_test, eligibility, owner=owner, repo=repo
        )
    except (ExecutionInputRejected, TypeError, ValueError) as exc:
        return _result(
            execution_id,
            fix,
            generated_test,
            "rejected",
            started_at,
            started,
            limits,
            reason=str(exc),
            cleanup_confirmed=True,
            review_id=review_id,
        )

    if os.environ.get("PR_MENTOR_EXECUTION_WORKER") != "1":
        return _result(
            execution_id,
            fix,
            generated_test,
            "unavailable",
            started_at,
            started,
            limits,
            reason="Execution is permitted only in the dedicated worker process.",
            cleanup_confirmed=True,
            review_id=review_id,
        )
    secret_names = (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "MONGODB_URI",
        "MONGODB_DATABASE",
        "DATABASE_URL",
        "SQLALCHEMY_DATABASE_URI",
        "PR_MENTOR_STORAGE",
        "PR_MENTOR_DATABASE_PATH",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    )
    if any(os.environ.get(name) for name in secret_names):
        return _result(
            execution_id,
            fix,
            generated_test,
            "unavailable",
            started_at,
            started,
            limits,
            reason="Execution worker environment contains application credentials.",
            cleanup_confirmed=True,
            review_id=review_id,
        )

    active = _acquire_worker_slot(limits.concurrent_executions)
    if not active:
        return _result(
            execution_id,
            fix,
            generated_test,
            "unavailable",
            started_at,
            started,
            limits,
            reason="Worker concurrency limit is reached.",
            cleanup_confirmed=True,
            review_id=review_id,
        )

    selected_runtime = runtime
    handle: RuntimeHandle | None = None
    metadata: RuntimeMetadata | None = None
    completion: RuntimeCompletion | None = None
    temporary: tempfile.TemporaryDirectory[str] | None = None
    status: ExecutionStatus = "infrastructure_error"
    error: str | None = None
    cleanup_confirmed = True
    try:
        if selected_runtime is None:
            image = os.environ.get("PR_MENTOR_TEST_EXECUTION_IMAGE", "")
            if not image:
                raise RuntimeUnavailable(
                    "PR_MENTOR_TEST_EXECUTION_IMAGE must pin an immutable image digest."
                )
            selected_runtime = DockerGVisorRuntime(image_digest=image)
        metadata = selected_runtime.preflight()
        if cancel_event.is_set():
            status = "cancelled"
        else:
            temporary = tempfile.TemporaryDirectory(prefix="prmentor-stage6-")
            root = Path(temporary.name).resolve(strict=True)
            source_relative = _canonical_input_path(artifact.source_file_path)
            source_target = root.joinpath(*PurePosixPath(source_relative).parts)
            source_target.parent.mkdir(parents=True, exist_ok=True)
            test_relative = (
                "test_prmentor_"
                + hashlib.sha256(artifact.test_id.encode()).hexdigest()[:16]
                + ".py"
            )
            test_target = root.joinpath(*PurePosixPath(test_relative).parts)
            test_target.parent.mkdir(parents=True, exist_ok=True)
            source_text = (
                artifact.baseline_source
                if source_variant == "baseline"
                else artifact.proposed_source
            )
            _write_exclusive(source_target, source_text.encode("utf-8"))
            _write_exclusive(test_target, artifact.generated_test_code.encode("utf-8"))
            _make_artifact_read_only(root)
            handle = selected_runtime.create(
                root,
                source_relative_path=source_relative,
                test_relative_path=test_relative,
                execution_id=execution_id,
                limits=limits,
            )
            completion = selected_runtime.wait(
                handle,
                timeout_seconds=limits.timeout_seconds,
                cancellation=cancel_event,
            )
            if completion.cancelled or cancel_event.is_set():
                status = "cancelled"
            elif completion.timed_out:
                status = "timed_out"
            elif (
                completion.resource_limit_reason
                or completion.output_truncated
                or _combined_output_size(completion.stdout, completion.stderr)
                > limits.output_bytes
            ):
                status = "infrastructure_error"
                error = (
                    completion.resource_limit_reason
                    or "Runtime output limit was exceeded."
                )
            elif completion.exit_code == 0:
                status = "passed"
            elif completion.exit_code == 1:
                status = "failed"
            elif completion.exit_code is not None:
                status = "infrastructure_error"
                error = f"Pytest runtime exited with infrastructure/usage code {completion.exit_code}."
            else:
                status = "infrastructure_error"
                error = "Runtime ended without a test exit code."
    except RuntimeUnavailable as exc:
        status = "unavailable"
        error = str(exc)
    except RuntimeInfrastructureError as exc:
        status = "infrastructure_error"
        error = str(exc)
        cleanup_confirmed = exc.cleanup_confirmed
        completion = RuntimeCompletion(
            exc.exit_code, "", "", resource_limit_reason=exc.resource_limit_reason
        )
    except Exception as exc:  # noqa: BLE001 - worker faults are never test failures.
        status = "infrastructure_error"
        error = f"Worker infrastructure error: {type(exc).__name__}: {str(exc)[:400]}"
    finally:
        if handle is not None and selected_runtime is not None:
            if status in {"timed_out", "cancelled", "infrastructure_error"}:
                try:
                    selected_runtime.cancel(handle)
                except Exception as exc:  # noqa: BLE001 - cleanup still runs after cancel failure.
                    error = f"Container cancellation failed: {type(exc).__name__}"
            try:
                cleanup_confirmed = selected_runtime.cleanup(handle)
            except Exception:  # noqa: BLE001 - cleanup uncertainty is security relevant.
                cleanup_confirmed = False
            if not cleanup_confirmed:
                status = "infrastructure_error"
                error = "Container cleanup could not be confirmed."
        if temporary is not None:
            try:
                _make_artifact_writable(Path(temporary.name))
                temporary.cleanup()
            except Exception:  # noqa: BLE001 - temporary cleanup uncertainty is infrastructure failure.
                status = "infrastructure_error"
                error = "Temporary execution artifact cleanup could not be confirmed."
                cleanup_confirmed = False
        _release_worker_slot()

    return _result(
        execution_id,
        fix,
        generated_test,
        status,
        started_at,
        started,
        limits,
        metadata=metadata,
        completion=completion,
        reason=error,
        cleanup_confirmed=cleanup_confirmed,
        timeout_enforced=handle is not None,
        review_id=review_id,
    )


def _result(
    execution_id: str,
    fix: SuggestedFix,
    test: GeneratedTestCase,
    status: ExecutionStatus,
    started_at: datetime,
    started: float,
    limits: ExecutionResourceLimits,
    *,
    metadata: RuntimeMetadata | None = None,
    completion: RuntimeCompletion | None = None,
    reason: str | None = None,
    cleanup_confirmed: bool,
    timeout_enforced: bool = False,
    review_id: str | None = None,
) -> TestExecutionResult:
    finished = datetime.now(UTC)
    bounded_stdout, bounded_stderr, streams_truncated = _bound_output_pair(
        completion.stdout if completion else "",
        completion.stderr if completion else "",
        limits.output_bytes,
    )
    if status == "infrastructure_error" and not reason:
        reason = "Execution infrastructure failed."
    return TestExecutionResult(
        execution_id=execution_id,
        review_id=review_id,
        repository_id=test.repository_id,
        canonical_issue_id=test.canonical_issue_id,
        suggested_fix_id=fix.fix_id,
        source_snapshot_id=test.source_snapshot_id,
        base_commit_sha=test.base_commit_sha,
        checked_head_sha=test.checked_head_sha,
        baseline_blob_sha=test.baseline_blob_sha,
        proposed_blob_sha=test.proposed_blob_sha,
        test_id=test.test_id,
        status=status,
        runtime_name=metadata.name if metadata else "unavailable",
        runtime_version=metadata.version if metadata else None,
        execution_image_digest=metadata.image_digest if metadata else None,
        python_version=metadata.python_version if metadata else None,
        pytest_version=metadata.pytest_version if metadata else None,
        started_at=started_at,
        completed_at=finished,
        duration_ms=min(600_000, int(max(0, time.monotonic() - started) * 1000)),
        exit_code=completion.exit_code if completion else None,
        stdout=bounded_stdout,
        stderr=bounded_stderr,
        streams_merged=bool(completion and completion.streams_merged),
        output_truncated=streams_truncated
        or bool(completion and completion.output_truncated),
        resource_limits=limits,
        timeout_enforced=timeout_enforced,
        resource_limit_reason=completion.resource_limit_reason if completion else None,
        infrastructure_error=reason if status == "infrastructure_error" else None,
        reason=reason,
        cleanup_confirmed=cleanup_confirmed,
        output_artifacts=[],
    )


def _as_baseline_attempt(result: TestExecutionResult) -> SourceExecutionAttempt:
    return SourceExecutionAttempt(
        variant="baseline",
        review_id=result.review_id,
        repository_id=result.repository_id,
        canonical_issue_id=result.canonical_issue_id,
        suggested_fix_id=result.suggested_fix_id,
        source_snapshot_id=result.source_snapshot_id,
        base_commit_sha=result.base_commit_sha,
        checked_head_sha=result.checked_head_sha,
        baseline_blob_sha=result.baseline_blob_sha,
        proposed_blob_sha=result.proposed_blob_sha,
        test_id=result.test_id,
        status=result.status,
        runtime_name=result.runtime_name,
        runtime_version=result.runtime_version,
        execution_image_digest=result.execution_image_digest,
        python_version=result.python_version,
        pytest_version=result.pytest_version,
        started_at=result.started_at,
        completed_at=result.completed_at,
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        output_truncated=result.output_truncated,
        resource_limits=result.resource_limits,
        timeout_enforced=result.timeout_enforced,
        resource_limit_reason=result.resource_limit_reason,
        infrastructure_error=result.infrastructure_error,
        reason=result.reason,
        cleanup_confirmed=result.cleanup_confirmed,
    )


def _attempt_environment_matches(
    baseline: TestExecutionResult, proposed: TestExecutionResult
) -> bool:
    return all(
        getattr(baseline, field) == getattr(proposed, field)
        for field in (
            "review_id",
            "repository_id",
            "canonical_issue_id",
            "suggested_fix_id",
            "source_snapshot_id",
            "base_commit_sha",
            "checked_head_sha",
            "baseline_blob_sha",
            "proposed_blob_sha",
            "test_id",
            "runtime_name",
            "runtime_version",
            "execution_image_digest",
            "python_version",
            "pytest_version",
            "resource_limits",
            "timeout_enforced",
        )
    )


def _canonical_input_path(value: str) -> str:
    try:
        canonical = validate_repository_python_path(value)
    except GitHubFileSnapshotError as exc:
        raise ExecutionInputRejected("Artifact path is unsafe.") from exc
    if PurePosixPath(canonical).parts[0].casefold() == ".git":
        raise ExecutionInputRejected(
            "Git metadata paths are not valid execution inputs."
        )
    return canonical


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o444)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
        os.chmod(path, 0o444)
    finally:
        os.close(fd)


def _make_artifact_read_only(root: Path) -> None:
    for directory, _subdirs, _files in os.walk(root, topdown=False):
        os.chmod(directory, 0o555)
    os.chmod(root, 0o555)


def _make_artifact_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, _subdirs, files in os.walk(root, topdown=False):
        for name in files:
            os.chmod(Path(directory, name), 0o600)
        os.chmod(directory, 0o700)


def _bound_output_pair(
    stdout: str, stderr: str, limit_bytes: int
) -> tuple[str, str, bool]:
    stdout_bytes = stdout.encode("utf-8", errors="replace")
    stderr_bytes = stderr.encode("utf-8", errors="replace")
    if len(stdout_bytes) + len(stderr_bytes) <= limit_bytes:
        return stdout, stderr, False
    suffix = b"\n...[output truncated]"
    budget = max(0, limit_bytes - len(suffix))
    stdout_budget = min(len(stdout_bytes), budget // 2)
    stderr_budget = min(len(stderr_bytes), budget - stdout_budget)
    stdout_budget = min(len(stdout_bytes), budget - stderr_budget)

    def clip(data: bytes, maximum: int) -> str:
        return data[:maximum].decode("utf-8", errors="ignore")

    clipped_stdout = clip(stdout_bytes, stdout_budget)
    clipped_stderr = clip(stderr_bytes, stderr_budget)
    if stdout_budget < len(stdout_bytes):
        clipped_stdout += suffix.decode()
    elif stderr_budget < len(stderr_bytes):
        clipped_stderr += suffix.decode()
    return clipped_stdout, clipped_stderr, True


def _combined_output_size(stdout: str, stderr: str) -> int:
    return len(stdout.encode("utf-8", errors="replace")) + len(
        stderr.encode("utf-8", errors="replace")
    )


def _acquire_worker_slot(limit: int) -> bool:
    with _WORKER_SEMAPHORE_LOCK:
        semaphore = _WORKER_SEMAPHORES.setdefault(
            limit, threading.BoundedSemaphore(limit)
        )
    acquired = semaphore.acquire(blocking=False)
    if acquired:
        _ACTIVE_SLOT.limit = limit
    return acquired


def _release_worker_slot() -> None:
    # The caller only releases after a successful acquire. Store the selected
    # semaphore per active thread so varied test configurations remain safe.
    limit = getattr(_ACTIVE_SLOT, "limit", None)
    if limit is not None:
        with _WORKER_SEMAPHORE_LOCK:
            _WORKER_SEMAPHORES[limit].release()
        del _ACTIVE_SLOT.limit
