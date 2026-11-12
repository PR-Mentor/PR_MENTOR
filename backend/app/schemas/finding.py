from typing import Literal

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """Shared representation of an AI or tool-reported code finding."""

    file_path: str
    line: int | None = None
    rule_id: str | None = None
    severity: Literal["info", "low", "medium", "high", "critical"]
    message: str
    source: Literal["ruff", "bandit", "test", "ai"]
    evidence: str | None = None
    verification_status: Literal["tool_flagged", "reproduced", "unverified"] = "tool_flagged"
    explanation: str | None = None
    suggested_fix: str | None = None


class ReviewFinding(BaseModel):
    """Stable frontend finding shape, independent of any analyzer provider."""

    id: str
    title: str
    description: str
    file_path: str
    line_start: int | None = None
    line_end: int | None = None
    severity: Literal["critical", "high", "medium", "low", "informational"]
    category: Literal["bug", "security", "performance", "maintainability", "code_quality", "testing", "documentation", "learning"]
    confidence: float
    source: Literal["static_analysis", "ai_candidate", "test_evidence", "combined", "demo"]
    verification_status: Literal["verified", "needs_review", "unsupported"]
    evidence: list[str] = Field(default_factory=list)
    why_it_matters: str
    suggested_fix: str
    learning_explanation: str = ""
    limitations: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}
