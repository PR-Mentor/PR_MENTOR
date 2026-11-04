"""Request and response models for the review API."""

from typing import Literal

from pydantic import BaseModel, Field


class ReviewAnalyzeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class ReviewFindingResponse(BaseModel):
    severity: Literal["Critical", "High", "Medium", "Low", "Informational"]
    title: str
    file: str
    line: int | str
    text: str
    proof: str | None = None
    status: str | None = None


class ReviewResponse(BaseModel):
    repository: str
    pull_request_title: str
    pull_request_number: int
    url: str | None = None
    review_id: str | None = None
    findings: list[ReviewFindingResponse]
    status: str | None = None

"""Public review request and response contracts consumed by the frontend."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.finding import ReviewFinding
from app.schemas.github import PullRequestMetadata


class AnalyzeRequest(BaseModel):
    url: str | None = None
    pull_request_url: str | None = None
    mode: Literal["demo", "live"] = "demo"
    include_learning: bool = True

    @model_validator(mode="after")
    def require_url(self):
        if not (self.pull_request_url or self.url):
            raise ValueError("A pull request URL is required.")
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
    model_config = ConfigDict(extra="forbid")

    review_id: str
    status: Literal["completed", "partial"]
    analysis_mode: Literal["demo", "live"]
    is_demo: bool
    pull_request: PullRequestMetadata
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    verification_summary: dict[str, int]
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    duration_ms: int = Field(default=0, ge=0)
    diff_files: list[dict[str, Any]] = Field(default_factory=list)


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
