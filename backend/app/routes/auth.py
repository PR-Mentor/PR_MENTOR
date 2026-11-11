"""First-party account and opaque-cookie session endpoints."""

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import cookie_secure
from app.database import auth as auth_store

router = APIRouter(prefix="/api/auth", tags=["authentication"])
COOKIE_NAME = "prm_session"
SESSION_DAYS = 14


class Credentials(BaseModel):
    email: str = Field(
        min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
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


def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 128:
        raise HTTPException(status_code=401, detail="Authentication required.")
    session = auth_store.get_session(_token_hash(token))
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        expires = session["expires_at"]
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))  # noqa: FURB162 - accept serialized UTC Z timestamps.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            auth_store.delete_session(_token_hash(token))
            raise HTTPException(status_code=401, detail="Session expired.")
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="Invalid session.") from exc
    user = auth_store.get_user_by_id(session["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="Authenticated account is unavailable.")
    return user


@router.post("/register", status_code=201)
def register(request: Request, payload: Credentials, response: Response):
    # Independent IP and account buckets keep an attacker from rotating one identifier.
    _limit_sensitive_attempts(request, payload.email)
    email = str(payload.email).strip().casefold()
    user = {"user_id": uuid4().hex, "email": email}
    if not auth_store.create_user(
        user["user_id"], email, _password_hash(payload.password)
    ):
        raise HTTPException(
            status_code=409, detail="An account with that email already exists."
        )
    return _new_session(response, user)


@router.post("/login")
def login(request: Request, payload: Credentials, response: Response):
    _limit_sensitive_attempts(request, payload.email)
    email = str(payload.email).strip().casefold()
    user = auth_store.get_user_by_email(email)
    if not user or not _password_matches(
        payload.password, user.get("password_hash", "")
    ):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    return _new_session(response, user)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        auth_store.delete_session(_token_hash(token))
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
        secure=cookie_secure(),
    )


@router.get("/me")
def me(user: dict = Depends(current_user)):  # noqa: B008 - FastAPI dependency injection.
    return user


def _limit_sensitive_attempts(request: Request | None, email: str) -> None:
    identity = email.strip().casefold()
    remote = request.client.host if request and request.client else "unknown"
    if not auth_store.allow_attempt(
        "identity", identity
    ) or not auth_store.allow_attempt("remote", remote):
        raise HTTPException(
            status_code=429, detail="Too many authentication attempts. Try again later."
        )
