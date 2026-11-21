"""Server-side GitHub OAuth exchange and authenticated repository browsing."""

import asyncio
import base64
import hashlib
import os
import secrets
import sqlite3
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import PyMongoError

from app.config import cors_origins
from app.database import github as store
from app.database.auth import allow_attempt
from app.database.mongo_connection import StorageUnavailable
from app.database.users import get_user, upsert_user
from app.github_service import account
from app.github_service.client import GitHubAPIError, async_client, request_json
from app.routes.auth import current_claims, supabase_settings, verify_supabase_token


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


class GitHubRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except (StorageUnavailable, PyMongoError, sqlite3.Error) as exc:
                raise HTTPException(
                    503, "GitHub connection storage is temporarily unavailable."
                ) from exc

        return handle


router = APIRouter(
    prefix="/api/github",
    tags=["github"],
    dependencies=[Depends(no_store)],
    route_class=GitHubRoute,
)
Claims = Annotated[dict[str, Any], Depends(current_claims)]
PAGE_SIZE = 30
TOKEN_PATTERN = r"^[A-Za-z0-9_-]{43}$"
SCOPES = "read:user user:email repo"


class OAuthStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge: str = Field(pattern=TOKEN_PATTERN)
    intent: Literal["login", "connect"]
    return_url: str | None = Field(default=None, max_length=2048)


class OAuthCallback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str = Field(pattern=TOKEN_PATTERN)
    proof: str = Field(pattern=TOKEN_PATTERN)
    code: str = Field(min_length=1, max_length=512)
    intent: Literal["login", "connect"]


def _challenge(value: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest())
        .decode()
        .rstrip("=")
    )


def _owner(intent: str, authorization: str | None) -> str:
    if intent == "login":
        return ""
    claims = current_claims(authorization=authorization, prm_session=None)
    return claims["uid"]


def _return_url(requested: str | None, origin: str | None) -> str:
    allowed = {
        value.strip()
        for value in os.getenv("PR_MENTOR_GITHUB_RETURN_URL", "").split(",")
        if value.strip()
    }
    if not allowed:
        # The exact URL setting is optional when the browser supplies its
        # callback URL and the request origin is already in the API CORS allowlist.
        # This keeps the redirect tied to an operator-approved frontend origin.
        if not requested or not origin:
            raise HTTPException(503, "The GitHub OAuth return URL is not configured.")
        try:
            requested_parts = urlsplit(requested)
        except ValueError as exc:
            raise HTTPException(503, "The GitHub OAuth return URL is invalid.") from exc
        requested_origin = f"{requested_parts.scheme}://{requested_parts.netloc}"
        if origin != requested_origin or origin not in cors_origins():
            raise HTTPException(503, "The GitHub OAuth return URL is not configured.")
        allowed = {requested}
    url = requested or (next(iter(allowed)) if len(allowed) == 1 else "")
    if url not in allowed:
        raise HTTPException(
            400, "This frontend URL is not configured for GitHub sign-in."
        )
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise HTTPException(503, "The GitHub OAuth return URL is invalid.") from exc
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not (parts.scheme == "https" or (parts.scheme == "http" and local))
    ):
        raise HTTPException(503, "The GitHub OAuth return URL is not configured.")
    if origin and origin != f"{parts.scheme}://{parts.netloc}":
        raise HTTPException(
            400, "GitHub sign-in must return to the frontend origin that started it."
        )
    return url


@router.post("/oauth/start")
def start(
    payload: OAuthStart,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
):
    account.cipher()
    url, _ = supabase_settings()
    return_url = _return_url(payload.return_url, request.headers.get("origin"))
    owner = _owner(payload.intent, authorization)
    key = owner or (request.client.host if request.client else "unknown")
    if not allow_attempt("github-oauth", key, limit=30, window_seconds=600):
        raise HTTPException(
            429, "Too many GitHub connection attempts. Try again later."
        )
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    store.save_flow(_challenge(state), payload.challenge, owner, verifier)
    query = urlencode(
        {
            "provider": "github",
            "scopes": SCOPES,
            "redirect_to": f"{return_url}?prm_github_state={state}",
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "s256",
        }
    )
    return {"authorization_url": f"{url}/auth/v1/authorize?{query}"}


async def _exchange(code: str, verifier: str) -> dict:
    url, key = supabase_settings()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{url}/auth/v1/token",
                params={"grant_type": "pkce"},
                headers={"apikey": key},
                json={"auth_code": code, "code_verifier": verifier},
            )
        if not response.is_success:
            raise HTTPException(
                502 if response.status_code >= 500 else 400,
                "GitHub sign-in could not be completed. Start the connection again.",
            )
        data = response.json()
        if not isinstance(data, dict) or any(
            not isinstance(data.get(field), str) or not data[field]
            for field in ("provider_token", "access_token", "refresh_token")
        ):
            raise HTTPException(
                502, "GitHub authorization did not provide a usable session."
            )
        return data
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(503, "GitHub sign-in is temporarily unavailable.") from exc


@router.post("/oauth/callback")
async def callback(
    payload: OAuthCallback, authorization: Annotated[str | None, Header()] = None
):
    owner = await asyncio.to_thread(_owner, payload.intent, authorization)
    verifier = await asyncio.to_thread(
        store.consume_flow, _challenge(payload.state), _challenge(payload.proof), owner
    )
    if verifier is None:
        raise HTTPException(
            400,
            "The GitHub sign-in request expired or could not be verified. Start again.",
        )
    result = await _exchange(payload.code, verifier)
    claims = await asyncio.to_thread(
        verify_supabase_token, f"Bearer {result['access_token']}"
    )
    user_id = owner or claims["uid"]
    existing = await asyncio.to_thread(get_user, user_id)
    if existing and not existing.get("is_active", True):
        raise HTTPException(403, "This account is inactive.")
    try:
        profile = await account.fetch_profile(result["provider_token"])
    except GitHubAPIError as exc:
        raise account.api_error(exc) from exc
    if not owner:
        await asyncio.to_thread(upsert_user, claims)
    await asyncio.to_thread(
        account.save_connection,
        user_id,
        profile,
        result["provider_token"],
        result.get("provider_expires_in"),
    )
    session = (
        {key: result[key] for key in ("access_token", "refresh_token")}
        if not owner
        else None
    )
    return {"session": session}


def _record(claims: Claims) -> dict:
    record = account.connection(claims["uid"])
    if not record:
        raise HTTPException(409, "Connect your GitHub account to access repositories.")
    return record


Connection = Annotated[dict, Depends(_record)]


@router.get("/connection")
def profile(claims: Claims):
    record = account.connection(claims["uid"])
    return {
        "connected": record is not None,
        "profile": record["profile"] if record else None,
    }


@router.delete("/connection")
async def disconnect(claims: Claims):
    record = await asyncio.to_thread(account.connection, claims["uid"])
    if not record:
        return {"connected": False, "revoked": None}
    try:
        token = account.access_token(record)
    except HTTPException:
        token = None
    if not await asyncio.to_thread(account.disconnect, record):
        raise HTTPException(
            409, "Your GitHub connection changed. Reload and try again."
        )
    return {
        "connected": False,
        "revoked": await account.revoke(token) if token else False,
    }


async def _list(record: dict, refresh: bool):
    try:
        return await account.repositories(record, refresh=refresh)
    except GitHubAPIError as exc:
        raise account.api_error(exc, record) from exc
    except (TypeError, KeyError, ValueError) as exc:
        raise HTTPException(502, "GitHub returned invalid repository data.") from exc


@router.get("/repositories")
async def repositories(record: Connection):
    return await _list(record, False)


@router.post("/repositories/refresh")
async def refresh(record: Connection):
    if not await asyncio.to_thread(
        allow_attempt, "github-refresh", record["user_id"], limit=8, window_seconds=60
    ):
        raise HTTPException(
            429, "Too many repository refreshes. Try again in a minute."
        )
    return await _list(record, True)


async def _get(record: dict, path: str, params: dict | None = None):
    try:
        async with async_client(token=account.access_token(record)) as client:
            result = await request_json(client, path, params, use_cache=False)
    except GitHubAPIError as exc:
        raise account.api_error(exc, record) from exc
    await asyncio.to_thread(account.require_current_connection, record)
    return result


@router.get("/repositories/{owner}/{repo}")
async def repository(owner: str, repo: str, record: Connection):
    raw = await _get(record, account.repository_path(owner, repo))
    try:
        return account.repository_metadata(raw)
    except (TypeError, KeyError) as exc:
        raise HTTPException(502, "GitHub returned invalid repository details.") from exc


Page = Annotated[int, Query(ge=1)]


@router.get("/repositories/{owner}/{repo}/branches")
async def branches(owner: str, repo: str, record: Connection, page: Page = 1):
    raw = await _get(
        record,
        f"{account.repository_path(owner, repo)}/branches",
        {"per_page": PAGE_SIZE, "page": page},
    )
    try:
        if not isinstance(raw, list):
            raise TypeError
        return {
            "items": [
                {"name": item["name"], "protected": item["protected"]} for item in raw
            ],
            "has_next": len(raw) == PAGE_SIZE,
        }
    except (KeyError, TypeError) as exc:
        raise HTTPException(502, "GitHub returned invalid branch data.") from exc


@router.get("/repositories/{owner}/{repo}/pulls")
async def pulls(owner: str, repo: str, record: Connection, page: Page = 1):
    raw = await _get(
        record,
        f"{account.repository_path(owner, repo)}/pulls",
        {
            "state": "open",
            "sort": "updated",
            "direction": "desc",
            "per_page": PAGE_SIZE,
            "page": page,
        },
    )
    try:
        if not isinstance(raw, list):
            raise TypeError
        return {
            "items": [account.pull_metadata(item, owner, repo) for item in raw],
            "has_next": len(raw) == PAGE_SIZE,
        }
    except (KeyError, TypeError) as exc:
        raise HTTPException(502, "GitHub returned invalid pull request data.") from exc


@router.get("/repositories/{owner}/{repo}/pulls/{number}")
async def pull(
    owner: str, repo: str, number: Annotated[int, Field(gt=0)], record: Connection
):
    raw = await _get(record, f"{account.repository_path(owner, repo)}/pulls/{number}")
    try:
        return account.pull_metadata(raw, owner, repo)
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            502, "GitHub returned invalid pull request details."
        ) from exc
