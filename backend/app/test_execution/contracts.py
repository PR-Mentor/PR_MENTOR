"""Bounded Stage 6 input and output contracts, separate from generation status."""

from __future__ import annotations

import re
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExecutionStatus = Literal[
    "passed",
    "failed",
    "timed_out",
    "cancelled",
    "unavailable",
    "rejected",
    "infrastructure_error",
    "not_run",
]
RegressionStatus = Literal[
    "detected",
    "not_fixed",
    "not_reproduced",
    "regression_introduced",
    "unavailable",
    "inconclusive",
]

_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_SNAPSHOT = re.compile(r"snapshot-[0-9a-f]{32}\Z")


class ExecutionResourceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: int = Field(ge=1, le=120, strict=True)
    cpu_count: int = Field(ge=1, le=2, strict=True)
    memory_bytes: int = Field(ge=67_108_864, le=2_147_483_648, strict=True)
    process_count: int = Field(ge=8, le=128, strict=True)
    scratch_bytes: int = Field(ge=8_388_608, le=536_870_912, strict=True)
    output_bytes: int = Field(ge=1_024, le=1_000_000, strict=True)
    concurrent_executions: int = Field(ge=1, le=8, strict=True)


class ExecutionArtifact(BaseModel):
    """Explicit bounded input. Source and test are strings, never host paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: str = Field(min_length=3, max_length=300)
    canonical_issue_id: str = Field(min_length=1, max_length=128)
    suggested_fix_id: str = Field(min_length=1, max_length=128)
    test_id: str = Field(min_length=1, max_length=128)
    source_snapshot_id: str = Field(min_length=41, max_length=41)
    base_commit_sha: str = Field(min_length=40, max_length=64)
    checked_head_sha: str = Field(min_length=40, max_length=64)
    baseline_blob_sha: str = Field(min_length=40, max_length=64)
    proposed_blob_sha: str = Field(min_length=40, max_length=64)
    source_file_path: str = Field(min_length=1, max_length=1_000)
    baseline_source: str = Field(min_length=1, max_length=100_000)
    proposed_source: str = Field(min_length=1, max_length=100_000)
    generated_test_code: str = Field(min_length=1, max_length=12_000)

    @field_validator(
        "repository_id", "canonical_issue_id", "suggested_fix_id", "test_id"
    )
    @classmethod
    def identity_text_is_safe(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("execution identities must be nonblank and contain no NUL")
        return value

    @field_validator("source_snapshot_id")
    @classmethod
    def snapshot_id_is_canonical(cls, value: str) -> str:
        if not _SNAPSHOT.fullmatch(value):
            raise ValueError("source_snapshot_id is not canonical")
        return value

    @field_validator(
        "base_commit_sha", "checked_head_sha", "baseline_blob_sha", "proposed_blob_sha"
    )
    @classmethod
    def hashes_are_full(cls, value: str) -> str:
        if not _SHA.fullmatch(value):
            raise ValueError("source identifiers must be full hexadecimal Git hashes")
        return value

    @field_validator("source_file_path")
    @classmethod
    def path_is_bounded_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("source path must be nonblank and contain no NUL")
        return value


class SourceExecutionAttempt(BaseModel):
    """Persisted result for one source version in a paired regression run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variant: Literal["baseline"]
    review_id: str | None = Field(default=None, min_length=1, max_length=128)
    repository_id: str = Field(min_length=3, max_length=300)
    canonical_issue_id: str = Field(min_length=1, max_length=128)
    suggested_fix_id: str = Field(min_length=1, max_length=128)
    source_snapshot_id: str = Field(min_length=41, max_length=41)
    base_commit_sha: str = Field(min_length=40, max_length=64)
    checked_head_sha: str = Field(min_length=40, max_length=64)
    baseline_blob_sha: str = Field(min_length=40, max_length=64)
    proposed_blob_sha: str = Field(min_length=40, max_length=64)
    test_id: str = Field(min_length=1, max_length=128)
    status: ExecutionStatus
    runtime_name: str = Field(min_length=1, max_length=80)
    runtime_version: str | None = Field(default=None, max_length=200)
    execution_image_digest: str | None = None
    python_version: str | None = Field(default=None, max_length=120)
    pytest_version: str | None = Field(default=None, max_length=120)
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0, le=600_000, strict=True)
    exit_code: int | None = Field(default=None, ge=0, le=255, strict=True)
    stdout: str = Field(default="", max_length=1_000_000)
    stderr: str = Field(default="", max_length=1_000_000)
    output_truncated: bool = Field(default=False, strict=True)
    resource_limits: ExecutionResourceLimits
    timeout_enforced: bool = Field(strict=True)
    resource_limit_reason: str | None = Field(default=None, max_length=500)
    infrastructure_error: str | None = Field(default=None, max_length=1_000)
    reason: str | None = Field(default=None, max_length=1_000)
    cleanup_confirmed: bool = Field(strict=True)

    @field_validator("started_at", "completed_at")
    @classmethod
    def attempt_timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution timestamps must be timezone-aware")
        return value

    @field_validator("execution_image_digest")
    @classmethod
    def attempt_image_is_immutable(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r".+@sha256:[0-9a-f]{64}", value):
            raise ValueError("baseline execution image must be pinned by SHA-256")
        return value

    @field_validator("stdout", "stderr")
    @classmethod
    def attempt_output_has_no_nuls(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("captured output must not contain NUL characters")
        return value

    @model_validator(mode="after")
    def attempt_state_is_consistent(self):
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.status in {"passed", "failed"} and (
            self.exit_code is None
            or not self.cleanup_confirmed
            or not self.timeout_enforced
            or not self.execution_image_digest
            or not self.python_version
            or not self.pytest_version
            or self.output_truncated
            or self.resource_limit_reason
            or self.infrastructure_error
        ):
            raise ValueError("normal baseline outcomes require complete clean runtime evidence")
        if self.status == "passed" and self.exit_code != 0:
            raise ValueError("passed baseline execution requires exit code zero")
        if self.status == "failed" and self.exit_code != 1:
            raise ValueError("failed baseline execution requires pytest exit code one")
        if self.status in {
            "rejected", "unavailable", "not_run", "timed_out", "cancelled"
        } and self.exit_code is not None:
            raise ValueError(f"{self.status} baseline execution cannot claim a pytest exit code")
        if self.status == "timed_out" and (
            not self.timeout_enforced or not self.resource_limit_reason
        ):
            raise ValueError("timed_out baseline execution requires timeout evidence")
        if not self.cleanup_confirmed and self.status != "infrastructure_error":
            raise ValueError("unconfirmed baseline cleanup must be infrastructure_error")
        if self.status == "infrastructure_error" and not self.infrastructure_error:
            raise ValueError("baseline infrastructure_error requires an explicit reason")
        return self


class TestExecutionResult(BaseModel):
    """One bounded execution attempt; never implies that a fix is correct."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    __test__: ClassVar[bool] = False

    execution_id: str = Field(min_length=1, max_length=128)
    review_id: str | None = Field(default=None, min_length=1, max_length=128)
    repository_id: str = Field(min_length=3, max_length=300)
    canonical_issue_id: str = Field(min_length=1, max_length=128)
    suggested_fix_id: str = Field(min_length=1, max_length=128)
    source_snapshot_id: str = Field(min_length=41, max_length=41)
    base_commit_sha: str = Field(min_length=40, max_length=64)
    checked_head_sha: str = Field(min_length=40, max_length=64)
    baseline_blob_sha: str = Field(min_length=40, max_length=64)
    proposed_blob_sha: str = Field(min_length=40, max_length=64)
    test_id: str = Field(min_length=1, max_length=128)
    status: ExecutionStatus
    baseline_execution: SourceExecutionAttempt | None = None
    regression_status: RegressionStatus = "inconclusive"
    runtime_name: str = Field(min_length=1, max_length=80)
    runtime_version: str | None = Field(default=None, max_length=200)
    execution_image_digest: str | None = None
    python_version: str | None = Field(default=None, max_length=120)
    pytest_version: str | None = Field(default=None, max_length=120)
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0, le=600_000, strict=True)
    exit_code: int | None = Field(default=None, ge=0, le=255, strict=True)
    stdout: str = Field(default="", max_length=1_000_000)
    stderr: str = Field(default="", max_length=1_000_000)
    streams_merged: bool = Field(default=False, strict=True)
    output_truncated: bool = Field(default=False, strict=True)
    resource_limits: ExecutionResourceLimits
    timeout_enforced: bool = Field(strict=True)
    resource_limit_reason: str | None = Field(default=None, max_length=500)
    infrastructure_error: str | None = Field(default=None, max_length=1_000)
    reason: str | None = Field(default=None, max_length=1_000)
    cleanup_confirmed: bool = Field(strict=True)
    output_artifacts: list[str] = Field(default_factory=list, max_length=0)

    @field_validator("execution_image_digest")
    @classmethod
    def image_digest_is_immutable(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r".+@sha256:[0-9a-f]{64}", value):
            raise ValueError(
                "execution image must be pinned by an immutable SHA-256 digest"
            )
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution timestamps must be timezone-aware")
        return value

    @field_validator("source_snapshot_id")
    @classmethod
    def result_snapshot_id_is_canonical(cls, value: str) -> str:
        if not _SNAPSHOT.fullmatch(value):
            raise ValueError("source_snapshot_id is not canonical")
        return value

    @field_validator(
        "base_commit_sha", "checked_head_sha", "baseline_blob_sha", "proposed_blob_sha"
    )
    @classmethod
    def result_hashes_are_full(cls, value: str) -> str:
        if not _SHA.fullmatch(value):
            raise ValueError("source identifiers must be full hexadecimal Git hashes")
        return value

    @field_validator("stdout", "stderr")
    @classmethod
    def output_has_no_nuls(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("captured output must not contain NUL characters")
        return value

    @field_validator("infrastructure_error", "resource_limit_reason")
    @classmethod
    def optional_reasons_are_bounded_text(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("optional execution reasons must be nonblank text")
        return value

    @field_validator("runtime_name")
    @classmethod
    def runtime_name_not_blank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("runtime name must be nonblank")
        return value

    @field_validator(
        "test_id",
        "execution_id",
        "repository_id",
        "canonical_issue_id",
        "suggested_fix_id",
    )
    @classmethod
    def result_ids_not_blank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("execution identifiers must be nonblank")
        return value

    @field_validator("python_version", "pytest_version", "runtime_version")
    @classmethod
    def optional_versions_not_blank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("runtime version metadata must be nonblank when present")
        return value

    @model_validator(mode="after")
    def result_state_is_consistent(self):
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.status in {"passed", "failed"} and (
            self.exit_code is None or not self.cleanup_confirmed
        ):
            raise ValueError(
                "normal test outcomes require an exit code and confirmed cleanup"
            )
        if self.status == "passed" and self.exit_code != 0:
            raise ValueError("passed execution requires exit code zero")
        if self.status in {"passed", "failed"} and (
            not self.timeout_enforced
            or not self.execution_image_digest
            or not self.python_version
            or not self.pytest_version
        ):
            raise ValueError(
                "test outcomes require verified runtime, image, tool, and timeout metadata"
            )
        if self.status == "failed" and self.exit_code in {None, 0}:
            raise ValueError("failed execution requires pytest failure exit code one")
        if self.status in {"passed", "failed"} and (
            self.resource_limit_reason
            or self.infrastructure_error
            or self.output_truncated
        ):
            raise ValueError(
                "normal test outcomes cannot contain infrastructure/resource failures"
            )
        if (
            self.status
            in {"rejected", "unavailable", "not_run", "timed_out", "cancelled"}
            and self.exit_code is not None
        ):
            raise ValueError(f"{self.status} execution cannot claim a pytest exit code")
        if self.status == "timed_out" and (
            not self.timeout_enforced or not self.resource_limit_reason
        ):
            raise ValueError("timed_out execution requires enforced timeout metadata")
        if not self.cleanup_confirmed and self.status != "infrastructure_error":
            raise ValueError(
                "unconfirmed cleanup must be classified as infrastructure_error"
            )
        if self.status == "infrastructure_error" and not self.infrastructure_error:
            raise ValueError("infrastructure_error requires an explicit reason")
        if self.execution_image_digest is not None:
            match = re.fullmatch(
                r"(.+)@sha256:([0-9a-f]{64})", self.execution_image_digest
            )
            if not match:
                raise ValueError(
                    "execution image must be pinned by an immutable SHA-256 digest"
                )
        baseline = self.baseline_execution
        if baseline is None:
            if self.regression_status != "inconclusive":
                raise ValueError("regression status requires a persisted baseline result")
            return self
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
        ):
            if getattr(baseline, field) != getattr(self, field):
                raise ValueError(f"baseline execution {field} does not match proposed result")
        same_environment = (
            baseline.runtime_name == self.runtime_name
            and baseline.runtime_version == self.runtime_version
            and baseline.execution_image_digest == self.execution_image_digest
            and baseline.python_version == self.python_version
            and baseline.pytest_version == self.pytest_version
            and baseline.resource_limits == self.resource_limits
            and baseline.timeout_enforced == self.timeout_enforced
        )
        expected_regression_status = classify_regression(
            baseline.status, self.status, same_environment=same_environment
        )
        if self.regression_status != expected_regression_status:
            raise ValueError("regression status contradicts the paired execution evidence")
        return self


def classify_regression(
    baseline_status: ExecutionStatus,
    proposed_status: ExecutionStatus,
    *,
    same_environment: bool,
) -> RegressionStatus:
    """Classify only comparable pytest outcomes; infrastructure never detects a fix."""
    if not same_environment:
        return "inconclusive"
    if "unavailable" in {baseline_status, proposed_status} or "rejected" in {
        baseline_status,
        proposed_status,
    } or "not_run" in {baseline_status, proposed_status}:
        return "unavailable"
    if baseline_status == "cancelled" or proposed_status == "cancelled":
        return "unavailable"
    if baseline_status not in {"passed", "failed"} or proposed_status not in {
        "passed",
        "failed",
    }:
        return "inconclusive"
    outcomes: dict[tuple[ExecutionStatus, ExecutionStatus], RegressionStatus] = {
        ("failed", "passed"): "detected",
        ("failed", "failed"): "not_fixed",
        ("passed", "passed"): "not_reproduced",
        ("passed", "failed"): "regression_introduced",
    }
    return outcomes[(baseline_status, proposed_status)]
