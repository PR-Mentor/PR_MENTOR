"""Membership-aware workspace management and explicit review sharing."""

# FastAPI dependency defaults are intentional.
# ruff: noqa: B008

from __future__ import annotations

import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.database import auth as auth_store
from app.database import workspaces
from app.database.reviews import get_store
from app.routes.auth import current_user
from app.schemas.review import ReviewResponse

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class WorkspaceInvitationCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Literal["admin", "editor", "viewer"] = "viewer"


class WorkspaceInvitationAccept(BaseModel):
    token: str = Field(min_length=32, max_length=128)


class MemberRoleChange(BaseModel):
    role: Literal["admin", "editor", "viewer"]


@router.post("", status_code=201)
def create_workspace(payload: WorkspaceCreate, user: dict = Depends(current_user)):
    name = payload.name.strip()
    if not name or "\x00" in name:
        raise HTTPException(status_code=422, detail="Workspace name must be nonblank text.")
    if not auth_store.allow_attempt("workspace-create", user["user_id"], limit=10, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many workspaces were created recently.")
    if len(workspaces.list_workspaces(user["user_id"])) >= 20:
        raise HTTPException(status_code=409, detail="Workspace limit reached for this account.")
    return workspaces.create_workspace(user["user_id"], name)


@router.get("")
def list_workspaces(user: dict = Depends(current_user)):
    return workspaces.list_workspaces(user["user_id"])


@router.get("/{workspace_id}")
def get_workspace(workspace_id: str, user: dict = Depends(current_user)):
    role = workspaces.get_role(workspace_id, user["user_id"])
    if role is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    workspace = workspaces.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    return {**workspace, "role": role, "members": workspaces.list_members(workspace_id)}


@router.post("/{workspace_id}/invitations", status_code=201)
def invite_member(workspace_id: str, payload: WorkspaceInvitationCreate, user: dict = Depends(current_user)):
    role = _require_workspace_role(workspace_id, user["user_id"], {"owner", "admin"})
    if payload.role == "admin" and role != "owner":
        raise HTTPException(status_code=403, detail="Only the workspace owner can invite an administrator.")
    if not auth_store.allow_attempt("workspace-invite", user["user_id"], limit=20, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many workspace invitations were created recently.")
    email = str(payload.email).strip().casefold()
    token = secrets.token_urlsafe(32)
    expires = workspaces.create_invitation(workspaces.hash_invitation(token), workspace_id, email, payload.role, user["user_id"])
    return {"workspace_id": workspace_id, "email": email, "role": payload.role, "token": token, "expires_at": expires.isoformat()}


@router.post("/invitations/accept")
def accept_invitation(payload: WorkspaceInvitationAccept, user: dict = Depends(current_user)):
    if not auth_store.allow_attempt("workspace-invite-accept", user["user_id"], limit=10, window_seconds=900):
        raise HTTPException(status_code=429, detail="Too many invitation attempts. Try again later.")
    email = workspaces.user_email(user["user_id"])
    if email is None:
        raise HTTPException(status_code=401, detail="Authenticated account is unavailable.")
    workspace_id = workspaces.accept_invitation(workspaces.hash_invitation(payload.token), user["user_id"], email.casefold())
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="Invitation is invalid, expired, or addressed to another account.")
    return {"workspace_id": workspace_id, "role": workspaces.get_role(workspace_id, user["user_id"])}


@router.patch("/{workspace_id}/members/{member_id}")
def change_member_role(workspace_id: str, member_id: str, payload: MemberRoleChange, user: dict = Depends(current_user)):
    _require_workspace_role(workspace_id, user["user_id"], {"owner"})
    target_role = workspaces.get_role(workspace_id, member_id)
    if target_role is None:
        raise HTTPException(status_code=404, detail="Workspace member not found.")
    if target_role == "owner":
        raise HTTPException(status_code=409, detail="Workspace ownership cannot be changed through member role updates.")
    if not workspaces.change_member_role(workspace_id, member_id, payload.role):
        raise HTTPException(status_code=409, detail="Member role was not changed.")
    return {"workspace_id": workspace_id, "user_id": member_id, "role": payload.role}


@router.delete("/{workspace_id}/members/{member_id}")
def remove_member(workspace_id: str, member_id: str, user: dict = Depends(current_user)):
    _require_workspace_role(workspace_id, user["user_id"], {"owner"})
    target_role = workspaces.get_role(workspace_id, member_id)
    if target_role is None:
        raise HTTPException(status_code=404, detail="Workspace member not found.")
    if target_role == "owner":
        raise HTTPException(status_code=409, detail="The workspace owner cannot be removed.")
    if not workspaces.remove_member(workspace_id, member_id):
        raise HTTPException(status_code=409, detail="Workspace member was not removed.")
    return {"removed": True}


@router.post("/{workspace_id}/reviews/{review_id}", status_code=201)
def share_review(workspace_id: str, review_id: str, user: dict = Depends(current_user)):
    _require_workspace_role(workspace_id, user["user_id"], {"owner", "admin", "editor"})
    store = get_store()
    own_review = store.get_review(review_id, owner_id=user["user_id"])
    if own_review is None and workspaces.review_role(user["user_id"], review_id) not in {"owner", "admin", "editor"}:
        raise HTTPException(status_code=404, detail="Review not found or not shareable by this account.")
    if own_review is None and store.get_review(review_id) is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    workspaces.link_review(workspace_id, review_id, user["user_id"])
    return {"workspace_id": workspace_id, "review_id": review_id, "shared": True}


@router.delete("/{workspace_id}/reviews/{review_id}")
def unshare_review(workspace_id: str, review_id: str, user: dict = Depends(current_user)):
    _require_workspace_role(workspace_id, user["user_id"], {"owner", "admin"})
    if not workspaces.unlink_review(workspace_id, review_id):
        raise HTTPException(status_code=404, detail="Shared review not found.")
    return {"workspace_id": workspace_id, "review_id": review_id, "shared": False}


@router.get("/{workspace_id}/reviews", response_model=list[ReviewResponse])
def list_workspace_reviews(workspace_id: str, user: dict = Depends(current_user)):
    _require_workspace_role(workspace_id, user["user_id"], {"owner", "admin", "editor", "viewer"})
    store = get_store()
    reviews = [store.get_review(review_id) for review_id in workspaces.list_shared_review_ids(workspace_id)]
    return [item for item in reviews if item is not None]


@router.get("/{workspace_id}/reviews/{review_id}", response_model=ReviewResponse)
def get_workspace_review(workspace_id: str, review_id: str, user: dict = Depends(current_user)):
    _require_workspace_role(workspace_id, user["user_id"], {"owner", "admin", "editor", "viewer"})
    if review_id not in workspaces.list_shared_review_ids(workspace_id):
        raise HTTPException(status_code=404, detail="Review not found in this workspace.")
    review = get_store().get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found in this workspace.")
    return review


def _require_workspace_role(workspace_id: str, user_id: str, allowed: set[str]) -> str:
    role = workspaces.get_role(workspace_id, user_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if role not in allowed:
        raise HTTPException(status_code=403, detail="Workspace membership does not allow this action.")
    return role
