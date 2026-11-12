"""Structured but explicitly non-deterministic AI semantic review output."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SemanticReviewAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_coverage: Literal["addresses_issue", "does_not_address", "unclear"]
    logical_risks: list[str] = Field(default_factory=list, max_length=8)
    behavior_changes: list[str] = Field(default_factory=list, max_length=8)
    missed_edge_cases: list[str] = Field(default_factory=list, max_length=8)
    generated_test_coverage: Literal[
        "adequate_for_scope", "gaps_remain", "not_available", "unclear"
    ]
    evidence_conflicts: list[str] = Field(default_factory=list, max_length=8)
    conclusion: Literal["no_obvious_concern", "concerns_found", "unclear"]
    rationale: str = Field(min_length=1, max_length=1_500)
    limitations: list[str] = Field(default_factory=list, max_length=8)

    @field_validator(
        "logical_risks",
        "behavior_changes",
        "missed_edge_cases",
        "evidence_conflicts",
        "limitations",
    )
    @classmethod
    def bounded_nonblank_items(cls, values: list[str]) -> list[str]:
        if any(
            not item.strip() or "\x00" in item or len(item) > 500 for item in values
        ):
            raise ValueError("assessment items must be bounded nonblank text")
        return values

    @field_validator("rationale")
    @classmethod
    def rationale_nonblank(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("rationale must be nonblank text")
        return value
