"""Persistent, server-authored learning exercises and progress endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.data.learning_exercises import EXERCISES
from app.database.reviews import get_store
from app.routes.auth import current_user
from app.schemas.learning import AnswerRequest, LearningExercise

router = APIRouter(prefix="/api/learning", tags=["learning"])


def _exercise(item: dict) -> dict:
    return LearningExercise.model_validate({key: value for key, value in item.items() if key not in {"correct_answer", "explanation"}}).model_dump()


@router.get("/exercises", response_model=list[LearningExercise])
def list_exercises(difficulty: str | None = None):
    return [_exercise(item) for item in EXERCISES if difficulty is None or item["difficulty"] == difficulty]


@router.get("/exercises/{exercise_id}", response_model=LearningExercise)
def get_exercise(exercise_id: str):
    item = next((item for item in EXERCISES if item["id"] == exercise_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Learning exercise not found.")
    return _exercise(item)


@router.post("/exercises/{exercise_id}/submit")
def submit_exercise(exercise_id: str, payload: AnswerRequest, user: Annotated[dict, Depends(current_user)]):
    item = next((item for item in EXERCISES if item["id"] == exercise_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Learning exercise not found.")
    correct_answer = item.get("correct_answer")
    if not isinstance(correct_answer, str):
        raise HTTPException(status_code=500, detail="Learning exercise is invalid.")
    correct = payload.answer.strip().casefold() == correct_answer.casefold()
    exercise = LearningExercise.model_validate(
        {
            key: value
            for key, value in item.items()
            if key not in {"correct_answer", "explanation"}
        }
    )
    try:
        store = get_store()
        store.record_learning_attempt(
            exercise_id, exercise.category, correct, owner_id=user["user_id"]
        )
        progress = store.learning_progress(owner_id=user["user_id"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Learning progress could not be saved.") from exc
    return {"correct": correct, "explanation": item["explanation"], "feedback": "Correct — well reasoned." if correct else "Not quite. Review the hint and try again.", "points": 10 if correct else 0, "progress": progress}


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
