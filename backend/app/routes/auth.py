"""Firebase ID-token verification and authenticated user profile endpoints."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.database.users import delete_user, get_user, update_user, upsert_user

router = APIRouter(prefix="/api", tags=["authentication"])


@lru_cache(maxsize=1)
def _firebase_app():
    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Firebase Admin SDK is not installed.") from exc
    try:
        return firebase_admin.get_app()
    except ValueError:
        pass
    project_id = os.getenv("FIREBASE_PROJECT_ID")
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    path = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE")
    try:
        if raw:
            credential = credentials.Certificate(json.loads(raw))
        elif path:
            credential = credentials.Certificate(path)
        else:
            credential = credentials.ApplicationDefault()
        return firebase_admin.initialize_app(credential, {"projectId": project_id} if project_id else None)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Firebase Admin credentials are not configured correctly.") from exc


def verify_firebase_token(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="A Firebase bearer token is required.", headers={"WWW-Authenticate": "Bearer"})
    try:
        from firebase_admin import auth

        return auth.verify_id_token(authorization[7:].strip(), app=_firebase_app(), check_revoked=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="The Firebase ID token is invalid, expired, or revoked.", headers={"WWW-Authenticate": "Bearer"}) from exc


def current_claims(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return verify_firebase_token(authorization)


def safe_user(user: dict[str, Any]) -> dict[str, Any]:
    user.pop("_id", None)
    return user


def delete_firebase_identity(uid: str) -> None:
    from firebase_admin import auth

    auth.delete_user(uid, app=_firebase_app())


@router.post("/auth/firebase/verify")
def firebase_verify(claims: Annotated[dict[str, Any], Depends(current_claims)]):
    user = upsert_user(claims)
    return {"user": safe_user(user)}


@router.get("/auth/me")
def me(claims: Annotated[dict[str, Any], Depends(current_claims)]):
    user = get_user(claims["uid"])
    if not user:
        user = upsert_user(claims)
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="This account is inactive.")
    return {"user": safe_user(user)}


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    username: str | None = Field(default=None, min_length=2, max_length=39, pattern=r"^[A-Za-z0-9_.-]+$")
    preferences: dict[str, Any] | None = None


@router.patch("/users/me")
def patch_me(payload: ProfileUpdate, claims: Annotated[dict[str, Any], Depends(current_claims)]):
    try:
        user = update_user(claims["uid"], payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if user is None:
        raise HTTPException(status_code=404, detail="User profile not found.")
    return {"user": safe_user(user)}


@router.delete("/users/me")
def delete_me(claims: Annotated[dict[str, Any], Depends(current_claims)]):
    auth_time = int(claims.get("auth_time", 0))
    if not auth_time or (datetime.now(UTC).timestamp() - auth_time) > 300:
        raise HTTPException(status_code=403, detail="Please reauthenticate and try again.")
    try:
        delete_firebase_identity(claims["uid"])
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Firebase Admin SDK is not installed.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Firebase account deletion failed; profile data was retained.") from exc
    delete_user(claims["uid"])
    return {"deleted": True}


@router.post("/auth/logout")
def logout(claims: Annotated[dict[str, Any], Depends(current_claims)]):
    """Frontend signs out locally; this endpoint confirms identity without revoking other devices."""
    return {"signed_out": True, "firebase_uid": claims["uid"]}


# Retain the old contract as a fail-closed response for clients that have not
# migrated. Authentication challenges are handled by Firebase on the client.
legacy_router = APIRouter(prefix="/api/auth", tags=["authentication"])


class LegacyOtpRequest(BaseModel):
    identity: str = Field(min_length=3, max_length=320)
    channel: str = Field(pattern="^(email|phone)$")


class LegacyOtpVerifyRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=4, max_length=12)


@legacy_router.post("/otp/send")
def legacy_send_otp(_payload: LegacyOtpRequest):
    raise HTTPException(status_code=503, detail="Standalone OTP delivery is not configured; use Firebase Phone Authentication.")


@legacy_router.post("/otp/verify")
def legacy_verify_otp(_payload: LegacyOtpVerifyRequest):
    raise HTTPException(status_code=503, detail="Standalone OTP verification is not configured; use Firebase Phone Authentication.")
