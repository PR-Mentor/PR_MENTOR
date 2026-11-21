"""Pydantic contracts for PR-connected Learning Lab spaces, concepts, and coding exercises."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

ConceptStatus = Literal["not_started", "in_progress", "mastered"]
SubmissionStatus = Literal[
    "draft_saved",
    "submitted",
    "static_verified",
    "test_execution_unavailable",
    "completed",
    "failed",
]
ExerciseDifficulty = Literal["beginner", "intermediate", "advanced"]
ExerciseCategory = Literal["bug", "security", "performance", "maintainability", "code_quality"]


class PRLearningSpace(BaseModel):
    """Canonical learning space associated with a pull request across all its review snapshots."""

    model_config = ConfigDict(extra="ignore")

    space_id: str = Field(default_factory=lambda: uuid4().hex)
    canonical_repo: str  # owner/repo
    pr_number: int
    pr_title: str
    pr_url: str
    latest_review_id: str
    review_count: int = 1
    review_snapshot_ids: list[str] = Field(default_factory=list)
    primary_language: str = "python"
    languages: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LearningConcept(BaseModel):
    """Educational concept derived from actual canonical issues in a PR review."""

    model_config = ConfigDict(extra="ignore")

    concept_id: str = Field(default_factory=lambda: uuid4().hex)
    space_id: str
    title: str
    summary: str
    why_it_matters: str
    category: str
    language: str
    canonical_issue_ids: list[str] = Field(default_factory=list)
    exercise_ids: list[str] = Field(default_factory=list)
    order_index: int = 0
    status: ConceptStatus = "not_started"
    evidence_snippets: list[str] = Field(default_factory=list)


class CodingExercise(BaseModel):
    """Interactive practical coding exercise grounded in PR findings."""

    model_config = ConfigDict(extra="ignore")

    exercise_id: str = Field(default_factory=lambda: uuid4().hex)
    space_id: str
    concept_id: str
    canonical_issue_id: str | None = None
    title: str
    description: str
    instructions: str
    language: Literal["python", "c", "cpp", "java"]
    difficulty: ExerciseDifficulty = "beginner"
    category: ExerciseCategory = "code_quality"
    starter_code: str
    buggy_code: str | None = None
    expected_behavior: str
    hints: list[str] = Field(default_factory=list)
    explanation: str = ""
    completion_criteria: list[str] = Field(default_factory=list)
    validation_rules: dict[str, Any] = Field(default_factory=dict)
    points: int = 10
    real_world_analogy: str | None = None
    problem_explanation: str | None = None
    mission: str | None = None
    example_input: str | None = None
    example_output: str | None = None

    def to_public(self) -> CodingExercisePublic:
        data = self.model_dump(exclude={"validation_rules"})
        return CodingExercisePublic.model_validate(data)


class CodingExercisePublic(BaseModel):
    """Public interactive practical coding exercise view with internal validation rules protected."""

    model_config = ConfigDict(extra="ignore")

    exercise_id: str
    space_id: str
    concept_id: str
    canonical_issue_id: str | None = None
    title: str
    description: str
    instructions: str
    language: Literal["python", "c", "cpp", "java"]
    difficulty: ExerciseDifficulty = "beginner"
    category: ExerciseCategory = "code_quality"
    starter_code: str
    buggy_code: str | None = None
    expected_behavior: str
    hints: list[str] = Field(default_factory=list)
    explanation: str = ""
    completion_criteria: list[str] = Field(default_factory=list)
    points: int = 10
    real_world_analogy: str | None = None
    problem_explanation: str | None = None
    mission: str | None = None
    example_input: str | None = None
    example_output: str | None = None


class ExerciseDraft(BaseModel):
    """User in-progress code draft for an exercise."""

    model_config = ConfigDict(extra="ignore")

    exercise_id: str
    user_id: str
    code: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExerciseSubmission(BaseModel):
    """User submission record with truthful validation status."""

    model_config = ConfigDict(extra="ignore")

    submission_id: str = Field(default_factory=lambda: uuid4().hex)
    exercise_id: str
    user_id: str
    code: str
    status: SubmissionStatus
    passed: bool
    feedback: str
    eval_details: dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LearningLabDashboard(BaseModel):
    """Dashboard summarizing PR learning spaces and user educational progress."""

    model_config = ConfigDict(extra="ignore")

    total_spaces: int = 0
    total_concepts: int = 0
    mastered_concepts: int = 0
    total_exercises: int = 0
    completed_exercises: int = 0
    active_spaces: list[PRLearningSpace] = Field(default_factory=list)
    recent_submissions: list[ExerciseSubmission] = Field(default_factory=list)
    recommended_exercises: list[CodingExercise] = Field(default_factory=list)
