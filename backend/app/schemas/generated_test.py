"""Bounded schemas for generated tests, their quality review, and provenance.

Generated test cases always have execution_status="not_run". Stage 5 never
executes or imports these code strings.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.issue import FixValidationCheckResult

MAX_GENERATED_TESTS = 6
MAX_REJECTED_TESTS = 12
MAX_TEST_CODE_CHARS = 12_000
MAX_TEST_TEXT_CHARS = 1_500
MAX_TEST_REFERENCES = 12
MAX_TEST_LIMITATIONS = 8
MAX_PROVIDER_CALLS = 12  # Four configured providers, at most three attempts each.
MAX_GENERATION_TEXT_CHARS = 500
_SHA1_OR_SHA256 = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_SNAPSHOT_ID = re.compile(r"snapshot-[0-9a-f]{32}\Z")

TestCategory = Literal[
    "issue_reproduction",
    "fix_verification",
    "regression",
    "boundary",
    "error_handling",
]
GenerationStatus = Literal[
    "generated",
    "partial",
    "failed",
    "unavailable",
    "not_attempted",
    "rejected_by_quality",
]
QualityDimensionStatus = Literal["satisfied", "review_required", "rejected"]


class GeneratedTestSourceReference(BaseModel):
    """Exact source or issue range that was present in the bounded prompt."""

    model_config = ConfigDict(extra="forbid")

    source_version: Literal["baseline", "proposed", "issue", "fix", "static_analysis"]
    file_path: str = Field(min_length=1, max_length=1_000)
    line_start: int | None = Field(default=None, ge=1, strict=True)
    line_end: int | None = Field(default=None, ge=1, strict=True)
    description: str = Field(min_length=1, max_length=MAX_GENERATION_TEXT_CHARS)

    @field_validator("file_path")
    @classmethod
    def path_must_be_repository_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value.strip()
            or value.startswith(("/", "\\"))
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or str(path) != value
        ):
            raise ValueError("source reference path must be canonical and repository-relative")
        return value

    @field_validator("description")
    @classmethod
    def description_must_be_nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("source reference description must be nonblank")
        return value

    @model_validator(mode="after")
    def validate_range(self):
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("source references must provide both line bounds or neither")
        if self.line_start is not None and self.line_end < self.line_start:
            raise ValueError("source reference line_end must not precede line_start")
        return self


class GeneratedTestQualityAssessment(BaseModel):
    """Deterministic review flags; no numerical score or AI correctness claim."""

    model_config = ConfigDict(extra="forbid")

    relevance: QualityDimensionStatus
    traceability: QualityDimensionStatus
    specificity: QualityDimensionStatus
    assertion_quality: QualityDimensionStatus
    independence: QualityDimensionStatus
    completeness: QualityDimensionStatus
    determinism: QualityDimensionStatus
    safety: QualityDimensionStatus
    outcome: Literal["ready_for_review", "review_required", "rejected"]
    findings: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("findings")
    @classmethod
    def findings_must_be_bounded(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or "\x00" in item or len(item) > MAX_GENERATION_TEXT_CHARS for item in values):
            raise ValueError("quality findings must be bounded nonblank text")
        return values


class GeneratedTestCase(BaseModel):
    """A generated pytest case. It is never an executed test result."""

    model_config = ConfigDict(extra="forbid")

    test_id: str = Field(min_length=1, max_length=128)
    suggested_fix_id: str = Field(min_length=1, max_length=128)
    canonical_issue_id: str = Field(min_length=1, max_length=128)
    repository_id: str = Field(min_length=3, max_length=300)
    source_snapshot_id: str = Field(min_length=41, max_length=41)
    base_commit_sha: str = Field(min_length=40, max_length=64)
    checked_head_sha: str = Field(min_length=40, max_length=64)
    baseline_blob_sha: str = Field(min_length=40, max_length=64)
    proposed_blob_sha: str = Field(min_length=40, max_length=64)
    source_file_path: str = Field(min_length=1, max_length=1_000)
    test_framework: Literal["pytest"] = "pytest"
    test_language: Literal["python"] = "python"
    test_name: str = Field(min_length=5, max_length=160)
    test_code: str = Field(min_length=1, max_length=MAX_TEST_CODE_CHARS)
    test_category: TestCategory
    test_description: str = Field(min_length=1, max_length=MAX_TEST_TEXT_CHARS)
    rationale: str = Field(min_length=1, max_length=MAX_TEST_TEXT_CHARS)
    expected_behavior: str = Field(min_length=1, max_length=MAX_TEST_TEXT_CHARS)
    source_references: list[GeneratedTestSourceReference] = Field(
        min_length=1, max_length=MAX_TEST_REFERENCES
    )
    generation_model: str = Field(min_length=1, max_length=200)
    generation_provider: str = Field(min_length=1, max_length=120)
    generation_configuration_sha256: str = Field(min_length=64, max_length=64)
    generation_timestamp: datetime
    generation_status: Literal["generated"] = "generated"
    execution_status: Literal["not_run"] = "not_run"
    quality_assessment: GeneratedTestQualityAssessment
    limitations: list[str] = Field(default_factory=list, max_length=MAX_TEST_LIMITATIONS)

    @field_validator("test_id", "suggested_fix_id", "canonical_issue_id", "repository_id", "test_name", "test_code", "test_description", "rationale", "expected_behavior", "generation_model", "generation_provider")
    @classmethod
    def required_text_must_be_nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("required test metadata must be nonblank and contain no NUL characters")
        return value

    @field_validator("source_file_path")
    @classmethod
    def source_path_must_be_canonical(cls, value: str) -> str:
        return GeneratedTestSourceReference(
            source_version="proposed", file_path=value, description="source"
        ).file_path

    @field_validator("source_snapshot_id")
    @classmethod
    def snapshot_id_must_be_digest_derived(cls, value: str) -> str:
        if not _SNAPSHOT_ID.fullmatch(value):
            raise ValueError("source_snapshot_id must use the canonical snapshot digest form")
        return value

    @field_validator("base_commit_sha", "checked_head_sha", "baseline_blob_sha", "proposed_blob_sha")
    @classmethod
    def source_hashes_must_be_full(cls, value: str) -> str:
        if not _SHA1_OR_SHA256.fullmatch(value):
            raise ValueError("source snapshot identifiers must be full SHA-1 or SHA-256 hashes")
        return value

    @field_validator("generation_configuration_sha256")
    @classmethod
    def configuration_digest_must_be_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("generation configuration identity must be a full SHA-256 digest")
        return value

    @field_validator("generation_timestamp")
    @classmethod
    def generation_timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generation timestamps must include a timezone")
        return value

    @field_validator("limitations")
    @classmethod
    def limitations_must_be_bounded(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or "\x00" in item or len(item) > MAX_GENERATION_TEXT_CHARS for item in values):
            raise ValueError("test limitations must be bounded nonblank text")
        return values

    @model_validator(mode="after")
    def validate_candidate_contract(self):
        if self.quality_assessment.outcome == "rejected":
            raise ValueError("rejected quality assessments cannot be stored as generated candidates")
        if not any(ref.source_version in {"baseline", "proposed"} for ref in self.source_references):
            raise ValueError("generated tests require at least one baseline or proposed source reference")
        if any(ref.file_path != self.source_file_path for ref in self.source_references):
            raise ValueError("generated-test references must identify the bound source file")
        return self


class RejectedGeneratedTest(BaseModel):
    """Bounded record of a malformed or low-quality candidate, never executed."""

    model_config = ConfigDict(extra="forbid")

    candidate_index: int = Field(ge=0, strict=True)
    test_name: str | None = Field(default=None, max_length=160)
    test_code: str | None = Field(default=None, max_length=MAX_TEST_CODE_CHARS)
    reason: str = Field(min_length=1, max_length=MAX_GENERATION_TEXT_CHARS)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("reason")
    @classmethod
    def reason_must_be_nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("rejection reason must be nonblank")
        return value

    @field_validator("sha256")
    @classmethod
    def digest_must_be_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("rejected-test digest must be a full SHA-256 hash")
        return value


class TestGenerationResult(BaseModel):
    """Generation outcome with an unexecuted Stage 1 check adapter."""

    model_config = ConfigDict(extra="forbid")
    __test__: ClassVar[bool] = False

    generation_id: str = Field(min_length=1, max_length=128)
    request_fingerprint_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    status: GenerationStatus
    suggested_fix_id: str
    canonical_issue_id: str
    repository_id: str | None = None
    source_snapshot_id: str | None = None
    base_commit_sha: str | None = None
    checked_head_sha: str | None = None
    baseline_blob_sha: str | None = None
    proposed_blob_sha: str | None = None
    source_file_path: str
    generation_model: str | None = None
    generation_provider: str = "openai-compatible"
    generation_configuration_sha256: str = Field(min_length=64, max_length=64)
    generation_timestamp: datetime
    completed_at: datetime
    generation_attempts: int = Field(ge=0, le=1, strict=True)
    provider_call_count: int = Field(ge=0, le=MAX_PROVIDER_CALLS, strict=True)
    token_usage: int | None = Field(default=None, ge=0, strict=True)
    execution_status: Literal["not_run"] = "not_run"
    tests: list[GeneratedTestCase] = Field(default_factory=list, max_length=MAX_GENERATED_TESTS)
    rejected_tests: list[RejectedGeneratedTest] = Field(default_factory=list, max_length=MAX_REJECTED_TESTS)
    ambiguities: list[str] = Field(default_factory=list, max_length=5)
    limitations: list[str] = Field(default_factory=list, max_length=MAX_TEST_LIMITATIONS)
    error_code: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=MAX_TEST_TEXT_CHARS)
    validation_check: FixValidationCheckResult

    @field_validator("suggested_fix_id", "canonical_issue_id", "source_file_path", "generation_id", "generation_provider")
    @classmethod
    def result_identity_must_be_nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("generation result identity must be nonblank")
        return value

    @field_validator("repository_id")
    @classmethod
    def optional_repository_id_must_be_valid(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 300 or "\x00" in value):
            raise ValueError("repository_id must be bounded nonblank text")
        return value

    @field_validator("source_snapshot_id")
    @classmethod
    def optional_snapshot_id_must_be_valid(cls, value: str | None) -> str | None:
        if value is not None and not _SNAPSHOT_ID.fullmatch(value):
            raise ValueError("source_snapshot_id must use the canonical snapshot digest form")
        return value

    @field_validator("base_commit_sha", "checked_head_sha", "baseline_blob_sha", "proposed_blob_sha")
    @classmethod
    def optional_source_hashes_must_be_full(cls, value: str | None) -> str | None:
        if value is not None and not _SHA1_OR_SHA256.fullmatch(value):
            raise ValueError("source snapshot identifiers must be full SHA-1 or SHA-256 hashes")
        return value

    @field_validator("generation_model", "reason")
    @classmethod
    def optional_text_must_be_nonblank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("optional generation metadata must be nonblank when present")
        return value

    @field_validator("generation_configuration_sha256", "request_fingerprint_sha256")
    @classmethod
    def optional_digest_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("generation configuration identity must be a full SHA-256 hash")
        return value

    @field_validator("generation_timestamp", "completed_at")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generation timestamps must include a timezone")
        return value

    @field_validator("ambiguities", "limitations")
    @classmethod
    def result_text_lists_must_be_bounded(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or "\x00" in item or len(item) > MAX_GENERATION_TEXT_CHARS for item in values):
            raise ValueError("generation limitations and ambiguities must be bounded nonblank text")
        return values

    @model_validator(mode="after")
    def validate_generation_result(self):
        if self.completed_at < self.generation_timestamp:
            raise ValueError("completed_at must not precede generation_timestamp")
        if self.status == "generated" and (not self.tests or self.rejected_tests):
            raise ValueError("generated status requires accepted tests and no rejected candidates")
        if self.status == "partial" and (not self.tests or not self.rejected_tests):
            raise ValueError("partial status requires both accepted and rejected candidates")
        if self.status == "rejected_by_quality" and (self.tests or not self.rejected_tests):
            raise ValueError("rejected_by_quality requires rejected candidates and no accepted tests")
        if self.status in {"failed", "unavailable", "not_attempted"} and (self.tests or self.rejected_tests):
            raise ValueError(f"{self.status} results cannot contain accepted or rejected candidate records")
        if self.status in {"generated", "partial", "failed", "rejected_by_quality"} and (
            self.generation_attempts != 1 or self.provider_call_count < 1
        ):
            raise ValueError("provider-backed outcomes require one generation attempt and at least one bounded provider call")
        if self.status == "not_attempted" and (self.generation_attempts or self.provider_call_count):
            raise ValueError("not_attempted results cannot record a provider call")
        if self.execution_status != "not_run" or self.validation_check.status != "not_run":
            raise ValueError("Stage 5 cannot represent generated tests as executed")
        if self.validation_check.required:
            raise ValueError("generated tests must not be a required validation check")
        if (
            self.validation_check.provenance_profile != "ai_assessment"
            or self.validation_check.effective_provenance_profile != "ai_assessment"
            or any(reference.kind != "other" for reference in self.validation_check.evidence)
        ):
            raise ValueError("generated tests must remain non-result AI assessment metadata")
        test_ids = [test.test_id for test in self.tests]
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("generated test IDs must be unique")
        for test in self.tests:
            if (
                test.suggested_fix_id != self.suggested_fix_id
                or test.canonical_issue_id != self.canonical_issue_id
                or test.repository_id != self.repository_id
                or test.source_snapshot_id != self.source_snapshot_id
                or test.base_commit_sha != self.base_commit_sha
                or test.checked_head_sha != self.checked_head_sha
                or test.baseline_blob_sha != self.baseline_blob_sha
                or test.proposed_blob_sha != self.proposed_blob_sha
                or test.source_file_path != self.source_file_path
            ):
                raise ValueError("generated test provenance must match its generation result")
        return self
