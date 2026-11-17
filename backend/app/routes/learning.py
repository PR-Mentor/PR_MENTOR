"""Persistent, server-authored learning exercises, PR learning spaces, and progress endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.data.learning_exercises import EXERCISES
from app.database.reviews import get_store
from app.routes.auth import current_user
from app.schemas.learning import AnswerRequest, LearningExercise
from app.schemas.learning_lab import (
    CodingExercise,
    CodingExercisePublic,
    ExerciseDraft,
    ExerciseSubmission,
    LearningConcept,
    LearningLabDashboard,
    PRLearningSpace,
)
from app.services.learning_spaces import get_learning_service

router = APIRouter(prefix="/api/learning", tags=["learning"])


class ResolveSpaceRequest(BaseModel):
    review_id: str = Field(min_length=1)


class SaveDraftRequest(BaseModel):
    code: str = Field(min_length=1)


class SubmitCodeRequest(BaseModel):
    code: str | None = None
    answer: str | None = None


def _legacy_exercise(item: dict) -> dict:
    return LearningExercise.model_validate(
        {key: value for key, value in item.items() if key not in {"correct_answer", "explanation"}}
    ).model_dump()


# --- PR Learning Spaces & Dashboard ---


@router.get("/dashboard", response_model=LearningLabDashboard)
def get_dashboard(user: Annotated[dict, Depends(current_user)]):
    service = get_learning_service()
    return service.get_dashboard(user.get("user_id"))


@router.get("/spaces", response_model=list[PRLearningSpace])
def list_learning_spaces(user: Annotated[dict, Depends(current_user)]):
    service = get_learning_service()
    return service.list_spaces(user.get("user_id"))


@router.post("/spaces/resolve", response_model=PRLearningSpace)
def resolve_space_from_review(payload: ResolveSpaceRequest, user: Annotated[dict, Depends(current_user)]):
    store = get_store()
    review = store.get_review(payload.review_id, owner_id=user.get("user_id"))
    if not review:
        raise HTTPException(status_code=404, detail="Review record not found.")
    service = get_learning_service()
    return service.resolve_or_create_space(review, owner_id=user.get("user_id"))


@router.get("/spaces/{space_id}", response_model=PRLearningSpace)
def get_learning_space(space_id: str, user: Annotated[dict, Depends(current_user)]):
    service = get_learning_service()
    space = service.get_space(space_id, user.get("user_id"))
    if not space:
        raise HTTPException(status_code=404, detail="Learning space not found.")
    return space


@router.get("/spaces/{space_id}/concepts", response_model=list[LearningConcept])
def list_space_concepts(space_id: str, user: Annotated[dict, Depends(current_user)]):
    service = get_learning_service()
    return service.list_concepts(space_id, user.get("user_id"))


@router.get("/spaces/{space_id}/concepts/{concept_id}", response_model=LearningConcept)
def get_space_concept(space_id: str, concept_id: str, user: Annotated[dict, Depends(current_user)]):
    service = get_learning_service()
    concept = service.get_concept(concept_id, user.get("user_id"))
    if not concept:
        raise HTTPException(status_code=404, detail="Learning concept not found.")
    return concept


@router.get("/spaces/{space_id}/exercises", response_model=list[CodingExercisePublic])
def list_space_exercises(
    space_id: str,
    concept_id: str | None = None,
    user: Annotated[dict, Depends(current_user)] = None,
):
    service = get_learning_service()
    exercises = service.list_exercises(space_id, concept_id)
    return [e.to_public() for e in exercises]


# --- Coding Exercises, Drafts & Submissions ---


@router.get("/coding-exercises/{exercise_id}", response_model=CodingExercisePublic)
def get_coding_exercise(exercise_id: str, user: Annotated[dict, Depends(current_user)] = None):
    service = get_learning_service()
    exercise = service.get_exercise(exercise_id)
    if not exercise:
        raise HTTPException(status_code=404, detail="Coding exercise not found.")
    return exercise.to_public()


@router.get("/coding-exercises/{exercise_id}/draft", response_model=ExerciseDraft | None)
def get_exercise_draft(exercise_id: str, user: Annotated[dict, Depends(current_user)]):
    service = get_learning_service()
    return service.get_draft(exercise_id, user["user_id"])


@router.post("/coding-exercises/{exercise_id}/draft", response_model=ExerciseDraft)
def save_exercise_draft(exercise_id: str, payload: SaveDraftRequest, user: Annotated[dict, Depends(current_user)]):
    service = get_learning_service()
    return service.save_draft(exercise_id, user["user_id"], payload.code)


@router.post("/coding-exercises/{exercise_id}/submit", response_model=ExerciseSubmission)
def submit_coding_exercise(
    exercise_id: str, payload: SubmitCodeRequest, user: Annotated[dict, Depends(current_user)]
):
    code = payload.code or payload.answer or ""
    if not code.strip():
        raise HTTPException(status_code=422, detail="Submission code must not be empty.")
    service = get_learning_service()
    try:
        return service.submit_exercise(exercise_id, user["user_id"], code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/coding-exercises/{exercise_id}/submissions", response_model=list[ExerciseSubmission])
def list_exercise_submissions(exercise_id: str, user: Annotated[dict, Depends(current_user)]):
    service = get_learning_service()
    return service.list_submissions(exercise_id, user["user_id"])


# --- Legacy MCQ Endpoints (Maintained for Backward Compatibility) ---


@router.get("/exercises", response_model=list[LearningExercise])
def list_exercises(difficulty: str | None = None):
    return [_legacy_exercise(item) for item in EXERCISES if difficulty is None or item["difficulty"] == difficulty]


@router.get("/exercises/{exercise_id}", response_model=LearningExercise)
def get_exercise(exercise_id: str):
    # If it's a coding exercise, adapt it to LearningExercise shape for legacy consumers
    service = get_learning_service()
    coding_ex = service.get_exercise(exercise_id)
    if coding_ex:
        return LearningExercise(
            id=coding_ex.exercise_id,
            title=coding_ex.title,
            description=coding_ex.description,
            language=coding_ex.language,
            difficulty=coding_ex.difficulty,
            category=coding_ex.category,
            instructions=coding_ex.instructions,
            code_snippet=coding_ex.starter_code,
            hints=coding_ex.hints,
            options=[],
            learning_objectives=coding_ex.completion_criteria,
        )

    item = next((item for item in EXERCISES if item["id"] == exercise_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Learning exercise not found.")
    return _legacy_exercise(item)


@router.post("/exercises/{exercise_id}/submit")
def submit_exercise(exercise_id: str, payload: SubmitCodeRequest, user: Annotated[dict, Depends(current_user)]):
    # Support both coding exercises and legacy MCQs
    service = get_learning_service()
    coding_ex = service.get_exercise(exercise_id)
    if coding_ex:
        code = payload.code or payload.answer or ""
        sub = service.submit_exercise(exercise_id, user["user_id"], code)
        return {
            "correct": sub.passed,
            "explanation": coding_ex.explanation,
            "feedback": sub.feedback,
            "points": coding_ex.points if sub.passed else 0,
            "progress": get_store().learning_progress(owner_id=user["user_id"]),
            "submission": sub.model_dump(mode="json"),
        }

    item = next((item for item in EXERCISES if item["id"] == exercise_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Learning exercise not found.")
    correct_answer = item.get("correct_answer")
    if not isinstance(correct_answer, str):
        raise HTTPException(status_code=500, detail="Learning exercise is invalid.")
    raw_answer = payload.answer if payload.answer is not None else (payload.code or "")
    if not raw_answer.strip():
        raise HTTPException(status_code=422, detail="Answer must not be empty.")
    correct = raw_answer.strip().casefold() == correct_answer.casefold()
    exercise = LearningExercise.model_validate(
        {key: value for key, value in item.items() if key not in {"correct_answer", "explanation"}}
    )
    try:
        store = get_store()
        store.record_learning_attempt(
            exercise_id, exercise.category, correct, owner_id=user["user_id"]
        )
        progress = store.learning_progress(owner_id=user["user_id"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Learning progress could not be saved.") from exc
    return {
        "correct": correct,
        "explanation": item["explanation"],
        "feedback": "Correct — well reasoned." if correct else "Not quite. Review the hint and try again.",
        "points": 10 if correct else 0,
        "progress": progress,
    }


@router.get("/progress")
def learning_progress(user: Annotated[dict, Depends(current_user)]):
    try:
        return get_store().learning_progress(owner_id=user["user_id"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Learning progress is unavailable.") from exc


@router.get("/history")
def learning_history(user: Annotated[dict, Depends(current_user)]):
    try:
        return get_store().learning_history(owner_id=user["user_id"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Learning history is unavailable.") from exc
