"""Canonical issue, observation, explanation, and suggested-fix contracts.

The legacy ``ReviewResponse.findings`` list remains preserved alongside these
additive issue-level structures.
"""

import math
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import ReviewFinding

IssueSeverity = Literal["critical", "high", "medium", "low", "informational"]
IssueCategory = Literal[
    "bug", "security", "performance", "maintainability", "code_quality",
    "testing", "documentation", "learning",
]
IssueStatus = Literal["open", "resolved", "dismissed"]

MAX_EXPLANATION_TEXT = 2_000
MAX_CODE_EXAMPLE_CHARS = 4_000
MAX_RESOURCE_URL_CHARS = 500
MAX_LEARNING_RESOURCES = 5
MAX_CAVEATS = 5
MAX_CAVEAT_CHARS = 500
MAX_SUGGESTED_FIXES_PER_ISSUE = 5
MAX_FIX_CODE_CHARS = 20_000
MAX_FIX_SUMMARY_CHARS = 500
MAX_FIX_RATIONALE_CHARS = 2_000
MAX_FIX_LIMITATIONS = 5
MAX_FIX_LIMITATION_CHARS = 500
MAX_FIX_VALIDATION_ATTEMPTS = 20
MAX_FIX_VALIDATION_CHECKS = 12
MAX_VALIDATION_EVIDENCE_PER_CHECK = 30
MAX_VALIDATION_LIMITATIONS = 20
MAX_VALIDATION_TEXT_CHARS = 1_000

FixGenerationStatus = Literal["generated", "stale", "conflicted"]
FixDecisionStatus = Literal["pending", "accepted", "rejected"]
FixValidationStatus = Literal[
    "passed", "failed", "partial", "inconclusive", "unavailable", "not_run",
]
FixValidationCheckStatus = Literal[
    "passed", "failed", "partial", "inconclusive", "unavailable", "not_run", "skipped",
]
FixValidationTrigger = Literal["manual", "automatic", "retry"]
FixValidationProvenanceProfile = Literal[
    "source_analysis", "snapshot_integrity", "ai_assessment", "test_execution", "non_source", "legacy_unverified",
]
FixValidationReportFailureStatus = Literal["failed", "unavailable", "inconclusive"]
FixValidationEvidenceKind = Literal[
    "source_snapshot", "tool_result", "finding", "test_result", "syntax_error",
    "diff", "execution_log", "other",
]

_FULL_SHA_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


def _validate_optional_sha(value: str | None) -> str | None:
    if value is not None and not _FULL_SHA_PATTERN.fullmatch(value):
        raise ValueError("snapshot identifiers must be full 40- or 64-character hexadecimal hashes")
    return value


def _validate_aware_timestamp(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("timestamps must include a timezone")
    return value


class FixValidationEvidenceReference(BaseModel):
    """Pointer to bounded validation evidence; raw source and logs are not embedded."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=128)
    kind: FixValidationEvidenceKind
    description: str = Field(min_length=1, max_length=MAX_VALIDATION_TEXT_CHARS)
    file_path: str | None = Field(default=None, max_length=1_000)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    sha256: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("evidence_id", "description")
    @classmethod
    def text_must_be_nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("text must contain non-whitespace characters without NUL characters")
        return value

    @field_validator("sha256")
    @classmethod
    def evidence_digest_must_be_full_sha(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("evidence sha256 must be a full 64-character hexadecimal digest")
        return value

    @model_validator(mode="after")
    def validate_line_range(self):
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class FixValidationSnapshot(BaseModel):
    """Immutable source identifiers used to compare baseline and proposed code."""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(min_length=1, max_length=1_000)
    base_commit_sha: str | None = Field(default=None, min_length=1, max_length=64)
    checked_head_sha: str | None = Field(default=None, min_length=1, max_length=64)
    baseline_blob_sha: str | None = Field(default=None, min_length=1, max_length=64)
    proposed_blob_sha: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("file_path")
    @classmethod
    def snapshot_path_must_be_canonical(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value.strip() or value.startswith(("/", "\\")) or "\\" in value
            or "\x00" in value or any(part in {"", ".", ".."} for part in value.split("/"))
            or str(path) != value
        ):
            raise ValueError("file_path must be a canonical repository-relative POSIX path")
        return value

    @field_validator("base_commit_sha", "checked_head_sha", "baseline_blob_sha", "proposed_blob_sha")
    @classmethod
    def snapshot_identifiers_must_be_full_hashes(cls, value: str | None) -> str | None:
        return _validate_optional_sha(value)


class FixValidationFindingCounts(BaseModel):
    """Differential finding counts for one analyzer and one source file."""

    model_config = ConfigDict(extra="forbid")

    baseline_count: int = Field(ge=0, strict=True)
    proposed_count: int = Field(ge=0, strict=True)
    introduced_count: int = Field(ge=0, strict=True)
    resolved_count: int = Field(ge=0, strict=True)
    unchanged_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def counts_must_reconcile(self):
        if self.baseline_count != self.resolved_count + self.unchanged_count:
            raise ValueError("baseline_count must equal resolved_count plus unchanged_count")
        if self.proposed_count != self.introduced_count + self.unchanged_count:
            raise ValueError("proposed_count must equal introduced_count plus unchanged_count")
        return self


class FixValidationReportFailure(BaseModel):
    """A validation-attempt failure outside an individual check."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=120)
    status: FixValidationReportFailureStatus
    reason: str = Field(min_length=1, max_length=MAX_VALIDATION_TEXT_CHARS)

    @field_validator("code", "reason")
    @classmethod
    def failure_text_must_be_nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("failure code and reason must contain non-whitespace text without NUL characters")
        return value


class FixValidationCheckResult(BaseModel):
    """One check's result, metadata, and references to its supporting evidence."""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1, max_length=128)
    check_name: str = Field(min_length=1, max_length=120)
    # provenance_profile preserves what the report declared. Older v1 data may
    # omit it; effective_provenance_profile records the guarantee supported by
    # the serialized provenance without overwriting historical metadata.
    provenance_profile: FixValidationProvenanceProfile | None = None
    effective_provenance_profile: FixValidationProvenanceProfile | None = None
    status: FixValidationCheckStatus
    required: bool = True
    tool_name: str | None = Field(default=None, max_length=120)
    tool_version: str | None = Field(default=None, max_length=120)
    configuration_sha256: str | None = Field(default=None, min_length=1, max_length=64)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    finding_counts: FixValidationFindingCounts | None = None
    evidence: list[FixValidationEvidenceReference] = Field(
        default_factory=list, max_length=MAX_VALIDATION_EVIDENCE_PER_CHECK,
    )
    reason: str | None = Field(default=None, max_length=MAX_VALIDATION_TEXT_CHARS)
    limitations: list[str] = Field(default_factory=list, max_length=MAX_FIX_LIMITATIONS)

    @field_validator("check_id", "check_name")
    @classmethod
    def identifiers_must_be_nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("value must contain non-whitespace characters without NUL characters")
        return value

    @field_validator("configuration_sha256")
    @classmethod
    def configuration_digest_must_be_full_sha(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("configuration_sha256 must be a full 64-character hexadecimal digest")
        return value

    @field_validator("tool_name", "tool_version")
    @classmethod
    def tool_identity_must_be_nonblank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("tool identity must contain non-whitespace text without NUL characters")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def check_timestamps_must_be_aware(cls, value: datetime | None) -> datetime | None:
        return _validate_aware_timestamp(value)

    @field_validator("reason")
    @classmethod
    def reason_must_be_nonblank_when_present(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("reason must contain non-whitespace characters without NUL characters")
        return value

    @field_validator("limitations")
    @classmethod
    def check_limitations_must_be_bounded(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or "\x00" in item or len(item) > MAX_CAVEAT_CHARS for item in values):
            raise ValueError(f"each limitation must contain non-whitespace text and be at most {MAX_CAVEAT_CHARS} characters")
        return values

    @model_validator(mode="after")
    def validate_check_timing(self):
        if self.started_at and self.completed_at and self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.status == "passed" and self.provenance_profile not in {None, "legacy_unverified"}:
            missing = []
            if not self.started_at or not self.completed_at:
                missing.append("started_at and completed_at")
            if not self.evidence:
                missing.append("evidence")
            if self.provenance_profile in {"source_analysis", "ai_assessment", "test_execution"} and (
                not self.tool_name or not self.tool_version
            ):
                missing.append("tool_name and tool_version")
            if self.provenance_profile == "source_analysis" and not self.configuration_sha256:
                missing.append("configuration_sha256")
            allowed_evidence_kinds = {
                "source_analysis": {"tool_result", "finding"},
                "snapshot_integrity": {"source_snapshot"},
                "ai_assessment": {"tool_result"},
                "test_execution": {"test_result", "execution_log", "source_snapshot"},
            }.get(self.provenance_profile)
            if allowed_evidence_kinds and not any(item.kind in allowed_evidence_kinds for item in self.evidence):
                missing.append(f"evidence kind in {sorted(allowed_evidence_kinds)}")
            if missing:
                raise ValueError("a passed check is missing required provenance: " + ", ".join(missing))
        return self


class FixValidationReport(BaseModel):
    """One immutable validation attempt; it never changes fix or decision state."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1", "2"] = "2"
    validation_id: str = Field(min_length=1, max_length=128)
    fix_id: str = Field(min_length=1, max_length=128)
    attempt_number: int = Field(ge=1, strict=True)
    trigger: FixValidationTrigger = "manual"
    status: FixValidationStatus
    engine_version: str | None = Field(default=None, max_length=120)
    started_at: datetime
    completed_at: datetime | None = None
    snapshot: FixValidationSnapshot | None = None
    checks: list[FixValidationCheckResult] = Field(default_factory=list, max_length=MAX_FIX_VALIDATION_CHECKS)
    report_failure: FixValidationReportFailure | None = None
    limitations: list[str] = Field(default_factory=list, max_length=MAX_VALIDATION_LIMITATIONS)
    reason: str | None = Field(default=None, max_length=MAX_VALIDATION_TEXT_CHARS)

    @field_validator("validation_id", "fix_id")
    @classmethod
    def report_ids_must_be_nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("IDs must contain non-whitespace characters without NUL characters")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def report_timestamps_must_be_aware(cls, value: datetime | None) -> datetime | None:
        return _validate_aware_timestamp(value)

    @field_validator("reason")
    @classmethod
    def report_reason_must_be_nonblank_when_present(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("reason must contain non-whitespace characters without NUL characters")
        return value

    @field_validator("engine_version")
    @classmethod
    def engine_version_must_be_nonblank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("engine_version must contain non-whitespace text without NUL characters")
        return value

    @field_validator("limitations")
    @classmethod
    def report_limitations_must_be_bounded(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or "\x00" in item or len(item) > MAX_CAVEAT_CHARS for item in values):
            raise ValueError(f"each limitation must contain non-whitespace text and be at most {MAX_CAVEAT_CHARS} characters")
        return values

    @model_validator(mode="after")
    def validate_report_integrity(self):
        if self.completed_at and self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        check_ids = [check.check_id for check in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("check IDs must be unique within a validation report")
        if self.schema_version == "2" and any(
            check.provenance_profile in {None, "legacy_unverified"}
            or check.effective_provenance_profile == "legacy_unverified"
            for check in self.checks
        ):
            raise ValueError("schema version 2 requires current declared and effective provenance profiles")
        from app.services.fix_validation import aggregate_validation_status

        expected_status = aggregate_validation_status(self.checks, report_failure=self.report_failure)
        if self.status != expected_status:
            raise ValueError(
                f"report status {self.status!r} contradicts its checks and report failure; "
                f"expected {expected_status!r}"
            )
        if self.schema_version == "2" and self.status == "passed" and self.completed_at is None:
            raise ValueError("a passed report requires completed_at")
        source_based_passes = [
            check for check in self.checks
            if check.status == "passed"
            and check.provenance_profile in {"source_analysis", "snapshot_integrity", "ai_assessment", "test_execution"}
        ]
        if self.schema_version == "2" and source_based_passes:
            if self.snapshot is None:
                raise ValueError("passed checks with a source-based provenance profile require a report snapshot")
            missing_snapshot_ids = [
                name for name in ("checked_head_sha", "baseline_blob_sha", "proposed_blob_sha")
                if getattr(self.snapshot, name) is None
            ]
            if missing_snapshot_ids:
                raise ValueError(
                    "passed source-based reports require complete snapshot identifiers: "
                    + ", ".join(missing_snapshot_ids)
                )
        snapshot_ids_complete = self.snapshot is not None and all(
            getattr(self.snapshot, name) is not None
            for name in ("checked_head_sha", "baseline_blob_sha", "proposed_blob_sha")
        )
        for check in self.checks:
            expected_effective_profile = check.provenance_profile or "legacy_unverified"
            if (
                expected_effective_profile in {"source_analysis", "snapshot_integrity", "ai_assessment", "test_execution"}
                and (self.snapshot is None or not snapshot_ids_complete)
            ):
                expected_effective_profile = "legacy_unverified"
            if self.schema_version == "2":
                expected_effective_profile = check.provenance_profile
            if check.effective_provenance_profile is None:
                check.effective_provenance_profile = expected_effective_profile
            elif check.effective_provenance_profile != expected_effective_profile:
                raise ValueError(
                    "effective provenance profile contradicts the report schema and available provenance"
                )
        return self

# These domains host the documentation/resources currently relevant to PR
# Mentor's Python, GitHub, security, and general web findings. Links are
# validated locally; no request is made to these hosts.
TRUSTED_LEARNING_RESOURCE_HOSTS = frozenset({
    "bandit.readthedocs.io",
    "cheatsheetseries.owasp.org",
    "cwe.mitre.org",
    "developer.mozilla.org",
    "docs.astral.sh",
    "docs.github.com",
    "docs.python.org",
    "fastapi.tiangolo.com",
    "owasp.org",
    "peps.python.org",
})


class IssueCodeExample(BaseModel):
    """Optional before/after example, bounded for safe API/UI handling."""

    model_config = ConfigDict(extra="forbid")

    before: str = Field(min_length=1, max_length=MAX_CODE_EXAMPLE_CHARS)
    after: str = Field(min_length=1, max_length=MAX_CODE_EXAMPLE_CHARS)
    language: str | None = Field(default=None, min_length=1, max_length=40)

    @field_validator("before", "after")
    @classmethod
    def code_must_contain_non_whitespace(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("code examples must contain non-whitespace text without NUL characters")
        return value


class LearningResource(BaseModel):
    """A documentation reference restricted to explicitly trusted hosts."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=MAX_RESOURCE_URL_CHARS)
    description: str | None = Field(default=None, max_length=300)

    @field_validator("title", "description")
    @classmethod
    def text_must_contain_non_whitespace(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("resource text must contain non-whitespace text without NUL characters")
        return value

    @field_validator("url")
    @classmethod
    def url_must_use_trusted_https_host(cls, value: str) -> str:
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("resource URL must not contain whitespace or control characters")
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("resource URL is malformed") from exc
        if (
            parsed.scheme != "https"
            or hostname not in TRUSTED_LEARNING_RESOURCE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.query
            or not parsed.path.startswith("/")
            or parsed.netloc.lower() not in {hostname, f"{hostname}:443"}
        ):
            raise ValueError("resource URL must be HTTPS on an approved documentation host without credentials or query parameters")
        return value


class IssueExplanation(BaseModel):
    """Structured educational explanation attached to one canonical issue."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=MAX_EXPLANATION_TEXT)
    why_it_matters: str = Field(min_length=1, max_length=MAX_EXPLANATION_TEXT)
    root_cause: str = Field(min_length=1, max_length=MAX_EXPLANATION_TEXT)
    correction: str = Field(min_length=1, max_length=MAX_EXPLANATION_TEXT)
    code_example: IssueCodeExample | None = None
    concept: str = Field(min_length=1, max_length=MAX_EXPLANATION_TEXT)
    learning_resources: list[LearningResource] = Field(default_factory=list, max_length=MAX_LEARNING_RESOURCES)
    caveats: list[str] = Field(default_factory=list, max_length=MAX_CAVEATS)

    @field_validator("summary", "why_it_matters", "root_cause", "correction", "concept")
    @classmethod
    def explanation_text_must_contain_non_whitespace(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("explanation text must contain non-whitespace text without NUL characters")
        return value

    @field_validator("caveats")
    @classmethod
    def caveats_must_be_bounded_text(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or "\x00" in value or len(value) > MAX_CAVEAT_CHARS for value in values):
            raise ValueError(f"each caveat must contain non-whitespace text and be at most {MAX_CAVEAT_CHARS} characters")
        return values


class FindingObservation(BaseModel):
    """One source's original diagnostic, retained intact as issue evidence.

    The nested ReviewFinding preserves the source finding ID, source, original
    message/description, evidence, confidence, and original file/line fields.
    Tool and provider names plus rule ID retain producer-specific provenance.
    """

    model_config = ConfigDict(extra="forbid")

    original_finding: ReviewFinding
    tool_name: str | None = None
    provider_name: str | None = None
    rule_id: str | None = None


class SuggestedFix(BaseModel):
    """One bounded, single-range Python replacement proposal.

    A record always represents generated code. Generation failures and
    unavailable outcomes have no proposal and must not be represented by an
    empty SuggestedFix. ``accepted`` records a developer decision in PR
    Mentor only; it never applies code or changes a repository.
    """

    model_config = ConfigDict(extra="forbid")

    fix_id: str = Field(min_length=1, max_length=128)
    canonical_issue_id: str = Field(min_length=1, max_length=128)
    file_path: str = Field(min_length=1, max_length=1_000)
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)
    language: Literal["python"]
    base_commit_sha: str = Field(min_length=1, max_length=64)
    file_blob_sha: str | None = Field(default=None, min_length=1, max_length=64)
    expected_original_code: str = Field(min_length=1, max_length=MAX_FIX_CODE_CHARS)
    proposed_code: str = Field(min_length=1, max_length=MAX_FIX_CODE_CHARS)
    change_summary: str = Field(min_length=1, max_length=MAX_FIX_SUMMARY_CHARS)
    rationale: str = Field(min_length=1, max_length=MAX_FIX_RATIONALE_CHARS)
    limitations: list[str] = Field(default_factory=list, max_length=MAX_FIX_LIMITATIONS)
    confidence: float = Field(ge=0.0, le=1.0)
    generation_status: FixGenerationStatus
    decision_status: FixDecisionStatus = "pending"
    generated_at: datetime
    decided_at: datetime | None = None
    validation_reports: list[FixValidationReport] = Field(
        default_factory=list, max_length=MAX_FIX_VALIDATION_ATTEMPTS,
    )

    @field_validator("fix_id", "canonical_issue_id", "file_path", "change_summary", "rationale", "expected_original_code", "proposed_code")
    @classmethod
    def required_text_must_be_nonblank_and_without_nul(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("value must contain non-whitespace text without NUL characters")
        return value

    @field_validator("file_path")
    @classmethod
    def file_path_must_be_relative_and_canonical(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            value.startswith(("/", "\\"))
            or "\\" in value
            or any(part in {".", ".."} for part in value.split("/"))
            or str(path) != value
        ):
            raise ValueError("file_path must be a canonical repository-relative POSIX path")
        return value

    @field_validator("base_commit_sha", "file_blob_sha")
    @classmethod
    def snapshot_sha_must_be_full_hex_identifier(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value):
            raise ValueError("source snapshot identifiers must be full 40- or 64-character hexadecimal hashes")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def confidence_must_be_a_finite_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("confidence must be a finite number between 0 and 1")
        return value

    @field_validator("limitations")
    @classmethod
    def limitations_must_be_bounded_text(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or "\x00" in value or len(value) > MAX_FIX_LIMITATION_CHARS for value in values):
            raise ValueError(f"each limitation must contain non-whitespace text and be at most {MAX_FIX_LIMITATION_CHARS} characters")
        return values

    @field_validator("generated_at", "decided_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_replacement_range(self):
        if self.start_line > self.end_line:
            raise ValueError("start_line must be less than or equal to end_line")
        if len(self.expected_original_code.splitlines()) != self.end_line - self.start_line + 1:
            raise ValueError("expected_original_code must contain exactly the lines in the replacement range")
        return self


class CanonicalIssue(BaseModel):
    """One underlying PR issue, potentially supported by several observations."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(min_length=1)
    file_path: str
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    category: IssueCategory
    severity: IssueSeverity
    normalized_description: str
    status: IssueStatus = "open"
    observations: list[FindingObservation] = Field(default_factory=list)
    explanation: IssueExplanation | None = None
    suggested_fixes: list[SuggestedFix] = Field(default_factory=list, max_length=MAX_SUGGESTED_FIXES_PER_ISSUE)

    @model_validator(mode="after")
    def line_end_not_before_start(self):
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start.")
        fix_ids = [fix.fix_id for fix in self.suggested_fixes]
        if len(fix_ids) != len(set(fix_ids)):
            raise ValueError("suggested fix IDs must be unique within a canonical issue")
        if any(fix.canonical_issue_id != self.issue_id for fix in self.suggested_fixes):
            raise ValueError("each suggested fix must reference its containing canonical issue")
        for fix in self.suggested_fixes:
            if any(report.fix_id != fix.fix_id for report in fix.validation_reports):
                raise ValueError("each validation report must reference its containing suggested fix")
            if any(
                report.snapshot is not None and report.snapshot.file_path != fix.file_path
                for report in fix.validation_reports
            ):
                raise ValueError("each validation snapshot must reference the suggested fix file")
            report_ids = [report.validation_id for report in fix.validation_reports]
            attempt_numbers = [report.attempt_number for report in fix.validation_reports]
            if len(report_ids) != len(set(report_ids)):
                raise ValueError("validation report IDs must be unique within a suggested fix")
            if len(attempt_numbers) != len(set(attempt_numbers)):
                raise ValueError("validation attempt numbers must be unique within a suggested fix")
        return self
