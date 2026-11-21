"""Learning exercise request and response schemas."""

from typing import Any

from pydantic import BaseModel, Field


class AnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=1000)


class LearningExercise(BaseModel):
    id: str
    title: str
    description: str
    language: str
    difficulty: str
    category: str
    instructions: str
    code_snippet: str
    hints: list[str]
    options: list[str]
    learning_objectives: list[str]
    real_world_analogy: str | None = None
    problem_explanation: str | None = None
    mission: str | None = None
    example_input: str | None = None
    example_output: str | None = None


class LearningHistory(BaseModel):
    temporary: bool
    attempts: list[dict[str, Any]]
