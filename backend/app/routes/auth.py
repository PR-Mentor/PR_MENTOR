"""Authentication, session verification, and user profile endpoints."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import cookie_secure
from app.database import auth as auth_store
from app.database.users import delete_user, get_user, update_user, upsert_user

router = APIRouter(prefix="/api", tags=["authentication"])
COOKIE_NAME = "prm_session"
SESSION_DAYS = 14


# ---------------------------------------------------------------------------
# Firebase & Supabase Token Verification (Authoritative Main Foundation)
# ---------------------------------------------------------------------------

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

        claims = auth.verify_id_token(authorization[7:].strip(), app=_firebase_app(), check_revoked=True)
        firebase_claims = claims.get("firebase") or {}
        if firebase_claims.get("sign_in_provider") == "phone":
            raise HTTPException(status_code=403, detail="Phone authentication is disabled.")
        return claims
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="The Firebase ID token is invalid, expired, or revoked.", headers={"WWW-Authenticate": "Bearer"}) from exc


def verify_supabase_token(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="A bearer token is required.", headers={"WWW-Authenticate": "Bearer"})
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    api_key = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("SUPABASE_PUBLIC_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or ""
    ).strip()
    if not url or not api_key:
        raise HTTPException(status_code=503, detail="Supabase token verification is not configured on the API.")
    token = authorization[7:].strip()
    try:
        response = httpx.get(
            f"{url}/auth/v1/user",
            headers={"apikey": api_key, "Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Supabase authentication is temporarily unavailable.") from exc
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=401, detail="The Supabase access token is invalid or expired.", headers={"WWW-Authenticate": "Bearer"})
    if not response.is_success:
        raise HTTPException(status_code=503, detail="Supabase could not verify this account.")
    try:
        user = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Supabase returned an invalid user response.") from exc
    uid = user.get("id") if isinstance(user, dict) else None
    if not isinstance(uid, str) or not uid:
        raise HTTPException(status_code=401, detail="The Supabase access token did not resolve to a user.", headers={"WWW-Authenticate": "Bearer"})

    metadata = user.get("user_metadata") or {}
    app_metadata = user.get("app_metadata") or {}
    provider = app_metadata.get("provider") or "github"
    if provider == "phone":
        raise HTTPException(status_code=403, detail="Phone authentication is disabled.")
    return {
        "uid": f"supabase:{uid}",
        "name": metadata.get("full_name") or metadata.get("name"),
        "preferred_username": metadata.get("user_name") or metadata.get("preferred_username"),
        "email": user.get("email"),
        "email_verified": bool(user.get("email_confirmed_at")),
        "picture": metadata.get("avatar_url") or metadata.get("picture"),
        "auth_provider": "supabase",
        "firebase": {"sign_in_provider": provider},
    }


def current_claims(
    authorization: str | None = Header(default=None),
    prm_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError("Malformed JWT")
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            if not isinstance(decoded, dict):
                raise ValueError("JWT payload must be an object")
            issuer = decoded.get("iss", "")
        except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
            raise HTTPException(status_code=401, detail="The bearer token is invalid.", headers={"WWW-Authenticate": "Bearer"}) from None
        if isinstance(issuer, str) and issuer.startswith("https://securetoken.google.com/"):
            return verify_firebase_token(authorization)
        return verify_supabase_token(authorization)

    # Cookie session fallback
    if prm_session and len(prm_session) <= 128:
        session = auth_store.get_session(_token_hash(prm_session))
        if session:
            try:
                expires = session["expires_at"]
                if isinstance(expires, str):
                    expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if expires > datetime.now(UTC):
                    user_record = auth_store.get_user_by_id(session["user_id"])
                    if user_record:
                        return {
                            "uid": user_record["user_id"],
                            "email": user_record["email"],
                            "name": user_record.get("email", "").split("@")[0],
                            "email_verified": True,
                            "auth_provider": "local",
                            "firebase": {"sign_in_provider": "password"},
                        }
            except Exception:
                pass

    raise HTTPException(
        status_code=401,
        detail="A bearer token is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(claims: Annotated[dict[str, Any], Depends(current_claims)]) -> dict[str, Any]:
    uid = claims["uid"]
    email = claims.get("email") or ""
    return {
        "user_id": uid,
        "firebase_uid": uid,
        "email": email,
        "display_name": claims.get("name") or claims.get("preferred_username") or email or uid,
        "claims": claims,
    }


def safe_user(user: dict[str, Any]) -> dict[str, Any]:
    user.pop("_id", None)
    return user


def delete_firebase_identity(uid: str) -> None:
    from firebase_admin import auth

    auth.delete_user(uid, app=_firebase_app())


# ---------------------------------------------------------------------------
# First-Party Password / Cookie Auth Helpers (Phase 5 Compatibility)
# ---------------------------------------------------------------------------

class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=12, max_length=256)


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return (
        "scrypt$"
        + base64.urlsafe_b64encode(salt).decode()
        + "$"
        + base64.urlsafe_b64encode(digest).decode()
    )


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_text, digest_text = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_session(response: Response, user: dict) -> dict:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=SESSION_DAYS)
    auth_store.save_session(_token_hash(token), user["user_id"], expires)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=cookie_secure(),
        samesite="strict",
        path="/",
    )
    return {"user_id": user["user_id"], "email": user["email"]}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _limit_sensitive_attempts(request: Request, identity: str) -> None:
    ip = _client_ip(request)
    if not auth_store.allow_attempt("auth-ip", ip, limit=20, window_seconds=900):
        raise HTTPException(status_code=429, detail="Too many attempts from this network. Try again later.")
    if not auth_store.allow_attempt("auth-account", identity.casefold().strip(), limit=5, window_seconds=900):
        raise HTTPException(status_code=429, detail="Too many attempts for this account. Try again later.")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/firebase/verify")
def firebase_verify(claims: Annotated[dict[str, Any], Depends(current_claims)]):
    user = upsert_user(claims)
    return {"user": safe_user(user)}


@router.post("/auth/supabase/verify")
def supabase_verify(claims: Annotated[dict[str, Any], Depends(current_claims)]):
    if claims.get("auth_provider") != "supabase":
        raise HTTPException(status_code=401, detail="A Supabase access token is required.")
    user = upsert_user(claims)
    return {"user": safe_user(user)}


@router.get("/auth/me")
def me(claims: Annotated[dict[str, Any], Depends(current_claims)]):
    user = get_user(claims["uid"])
    if not user:
        user = upsert_user(claims)
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="This account is inactive.")
    cleaned = safe_user(user)
    uid = cleaned.get("firebase_uid") or claims["uid"]
    return {
        "user": cleaned,
        "user_id": uid,
        "email": cleaned.get("email"),
    }


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
    if claims.get("auth_provider") == "supabase":
        raise HTTPException(status_code=501, detail="Supabase account deletion is not configured for this project.")
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
def logout(claims: Annotated[dict[str, Any], Depends(current_claims)], response: Response):
    """Frontend signs out locally; also clears cookie session if set."""
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"signed_out": True, "firebase_uid": claims["uid"]}


@router.post("/auth/register", status_code=201)
def register(request: Request, payload: Credentials, response: Response):
    _limit_sensitive_attempts(request, payload.email)
    email = str(payload.email).strip().casefold()
    user_id = f"local:{secrets.token_hex(8)}"
    user = {"user_id": user_id, "email": email}
    if not auth_store.create_user(user["user_id"], email, _password_hash(payload.password)):
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    upsert_user({
        "uid": user_id,
        "email": email,
        "email_verified": True,
        "name": email.split("@")[0],
        "auth_provider": "local",
        "firebase": {"sign_in_provider": "password"},
    })
    return _new_session(response, user)


@router.post("/auth/login")
def login(request: Request, payload: Credentials, response: Response):
    _limit_sensitive_attempts(request, payload.email)
    email = str(payload.email).strip().casefold()
    user = auth_store.get_user_by_email(email)
    if not user or not _password_matches(payload.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    return _new_session(response, user)


# Retain the old contract as a fail-closed response for clients that have not
# migrated. Authentication challenges are handled by Supabase on the client.
legacy_router = APIRouter(prefix="/api/auth", tags=["authentication"])


class LegacyOtpRequest(BaseModel):
    identity: str = Field(min_length=3, max_length=320)
    channel: str = Field(pattern="^email$")


class LegacyOtpVerifyRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=4, max_length=12)


@legacy_router.post("/otp/send")
def legacy_send_otp(_payload: LegacyOtpRequest):
    raise HTTPException(status_code=503, detail="Standalone OTP delivery is not configured; use Supabase email authentication.")


@legacy_router.post("/otp/verify")
def legacy_verify_otp(_payload: LegacyOtpVerifyRequest):
    raise HTTPException(status_code=503, detail="Standalone OTP verification is not configured; use Supabase email authentication.")
