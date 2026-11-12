"""Public review request and response contracts consumed by the frontend."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.finding import ReviewFinding
from app.schemas.github import PullRequestMetadata
from app.schemas.issue import CanonicalIssue


class AnalyzeRequest(BaseModel):
    url: str | None = None
    pull_request_url: str | None = None
    mode: Literal["demo", "live"] = "demo"
    include_learning: bool = True
    include_suggested_fixes: bool = False
    workspace_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_url(self):
        if not (self.pull_request_url or self.url):
            raise ValueError("A pull request URL is required.")
        if self.workspace_id and self.mode != "live":
            raise ValueError("Only live reviews can be created in a workspace.")
        return self


class SavedPullRequestRequest(BaseModel):
    url: str | None = None
    pull_request_url: str | None = None

    @model_validator(mode="after")
    def require_url(self):
        if not (self.pull_request_url or self.url):
            raise ValueError("A pull request URL is required.")
        return self


class ReviewResponse(BaseModel):
    """Review contract during the additive findings-to-issues migration.

    ``findings`` remains the original, ungrouped source output for existing
    consumers. ``canonical_issues`` groups those findings; each linked
    observation retains its complete original finding. Counts are nullable for
    legacy records where unique reconciliation cannot be inferred safely.
    """

    model_config = ConfigDict(extra="forbid")

    review_id: str
    workspace_id: str | None = None
    status: Literal["completed", "partial"]
    analysis_mode: Literal["demo", "live"]
    is_demo: bool
    model_provider: str | None = None
    model_name: str | None = None
    model_usage: dict[str, int] | None = None
    model_fallback_used: bool | None = None
    model_latency_ms: int | None = Field(default=None, ge=0)
    model_configuration: dict[str, Any] | None = None
    pull_request: PullRequestMetadata
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    canonical_issues: list[CanonicalIssue] = Field(default_factory=list)
    unique_issue_count: int | None = Field(default=None, ge=0)
    raw_observation_count: int | None = Field(default=None, ge=0)
    observation_counts_by_source: dict[str, int] = Field(default_factory=dict)
    issue_counts_by_severity: dict[str, int] = Field(default_factory=dict)
    issue_counts_by_category: dict[str, int] = Field(default_factory=dict)
    verification_summary: dict[str, int]
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    duration_ms: int = Field(default=0, ge=0)
    diff_files: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def provide_legacy_observation_count(self):
        if self.raw_observation_count is None and not self.canonical_issues:
            # A legacy review's findings are the best available raw diagnostic
            # count. Do not invent canonical issues or observation wrappers.
            self.raw_observation_count = len(self.findings)
        return self


class DiffLine(BaseModel):
    kind: Literal["added", "removed", "context"]
    line: int = Field(ge=0)
    text: str


class DiffHunk(BaseModel):
    header: str
    lines: list[DiffLine] = Field(default_factory=list)


class DiffFile(BaseModel):
    path: str
    previous_path: str | None = None
    status: str
    added: int = Field(default=0, ge=0)
    removed: int = Field(default=0, ge=0)
    hunks: list[DiffHunk] = Field(default_factory=list)
    truncated: bool = False


class DiffResponse(BaseModel):
    review_id: str
    files: list[DiffFile]
    warnings: list[str] = Field(default_factory=list)
