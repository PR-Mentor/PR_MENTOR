"""Review access resolution for private owners and explicit workspace shares."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.database import workspaces
from app.database.reviews import get_store

WRITE_ROLES = {"owner", "admin", "editor"}
ALL_ROLES = WRITE_ROLES | {"viewer"}


def resolve_review_access(
    review_id: str, user_id: str, *, write: bool = False
) -> tuple[dict[str, Any] | None, str | None]:
    store = get_store()
    owned = store.get_review(review_id, owner_id=user_id)
    if owned is not None:
        return owned, "owner"
    role = workspaces.review_role(user_id, review_id)
    if role not in ALL_ROLES:
        return None, None
    if write and role not in WRITE_ROLES:
        return None, "forbidden"
    return store.get_review(review_id), role


def require_review_access(review_id: str, user_id: str, *, write: bool = False) -> dict[str, Any]:
    review, role = resolve_review_access(review_id, user_id, write=write)
    if role == "forbidden":
        raise HTTPException(status_code=403, detail="Workspace role is read-only for this action.")
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return review
