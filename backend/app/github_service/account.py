"""GitHub account credentials and repository requests scoped to one PR Mentor user."""

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from app.database import github as store
from app.github_service.client import GitHubAPIError, async_client, request_json

REPOSITORY_CACHE_SECONDS = 60
_repositories = OrderedDict()
_inflight = {}
_cache_lock = Lock()


def cipher() -> Fernet:
    try:
        return Fernet(os.environ["PR_MENTOR_GITHUB_TOKEN_KEY"].encode())
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            503, "GitHub account encryption is not configured."
        ) from exc


def save_connection(
    user_id: str, profile: dict, token: str, expires_in: object
) -> None:
    now = datetime.now(UTC)
    expires_at = None
    if isinstance(expires_in, int) and 0 <= expires_in <= 31536000:
        expires_at = (now + timedelta(seconds=expires_in)).isoformat()
    store.save_connection(
        {
            "user_id": user_id,
            "version": uuid4().hex,
            "profile": profile,
            "connected": True,
            "encrypted_token": cipher()
            .encrypt(json.dumps([user_id, token]).encode())
            .decode(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": expires_at,
        }
    )
    invalidate(user_id)


def connection(user_id: str) -> dict | None:
    record = store.get_connection(user_id)
    if not record or not record["connected"]:
        return None
    expires = record.get("expires_at")
    if expires and datetime.fromisoformat(expires) <= datetime.now(UTC):
        disconnect(record)
        return None
    return record


def access_token(record: dict) -> str:
    try:
        user_id, token = json.loads(
            cipher().decrypt(record["encrypted_token"].encode())
        )
        if user_id != record["user_id"] or not isinstance(token, str) or not token:
            raise ValueError
        return token
    except (InvalidToken, ValueError, TypeError) as exc:
        raise HTTPException(
            503, "The GitHub connection could not be decrypted."
        ) from exc


def disconnect(record: dict) -> bool:
    removed = store.disconnect(record)
    if removed:
        invalidate(record["user_id"])
    return removed


def invalidate(user_id: str) -> None:
    with _cache_lock:
        _repositories.pop(user_id, None)


def require_current_connection(record: dict) -> None:
    active = connection(record["user_id"])
    if active is None or active["version"] != record["version"]:
        raise HTTPException(
            409, "Your GitHub connection changed. Reload and try again."
        )


def api_error(exc: GitHubAPIError, record: dict | None = None) -> HTTPException:
    if exc.status_code == 401 and record:
        disconnect(record)
        return HTTPException(
            409, "GitHub access expired or was revoked. Reconnect your GitHub account."
        )
    status = exc.status_code if exc.status_code in {403, 404, 429, 503, 504} else 502
    headers = (
        {"Retry-After": exc.rate_limit["retry-after"]}
        if exc.rate_limit.get("retry-after")
        else None
    )
    return HTTPException(status, str(exc), headers=headers)


async def fetch_profile(token: str) -> dict:
    async with async_client(token=token) as client:
        raw = await request_json(client, "/user", use_cache=False)
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("id"), int)
            or not isinstance(raw.get("login"), str)
        ):
            raise GitHubAPIError("GitHub returned invalid account details.", 502)
        try:
            emails = await request_json(client, "/user/emails", use_cache=False)
        except GitHubAPIError as exc:
            if exc.status_code not in {403, 404}:
                raise
            emails = []
        verified = (
            [
                item
                for item in emails
                if isinstance(item, dict) and item.get("verified") and item.get("email")
            ]
            if isinstance(emails, list)
            else []
        )
        email = next((item["email"] for item in verified if item.get("primary")), None)
        if email is None and verified:
            email = verified[0]["email"]
        return {
            "id": raw["id"],
            "login": raw["login"],
            "avatar_url": raw.get("avatar_url"),
            "html_url": f"https://github.com/{raw['login']}",
            "email": email,
        }


def repository_path(owner: str, repo: str) -> str:
    if any(
        part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", part)
        for part in (owner, repo)
    ):
        raise HTTPException(422, "Enter a valid GitHub repository owner and name.")
    return f"/repos/{owner}/{repo}"


def repository_metadata(raw: dict) -> dict:
    owner = raw["owner"]
    fields = (
        "id",
        "name",
        "full_name",
        "description",
        "language",
        "stargazers_count",
        "forks_count",
        "default_branch",
        "updated_at",
        "open_issues_count",
    )
    return {
        **{key: raw[key] for key in fields},
        "html_url": f"https://github.com/{raw['full_name']}",
        "owner": {"login": owner["login"], "avatar_url": owner.get("avatar_url")},
        "visibility": raw.get("visibility")
        or ("private" if raw["private"] else "public"),
    }


def pull_metadata(raw: dict, owner: str, repo: str) -> dict:
    return {
        **{
            key: raw[key]
            for key in ("number", "title", "body", "created_at", "updated_at", "state")
        },
        "draft": raw.get("draft", False),
        "author": (raw.get("user") or {}).get("login"),
        "head": raw["head"]["ref"],
        "base": raw["base"]["ref"],
        "html_url": f"https://github.com/{owner}/{repo}/pull/{raw['number']}",
        **{key: raw.get(key) for key in ("changed_files", "additions", "deletions")},
    }


async def _fetch_repositories(record: dict) -> list[dict]:
    repos = {}
    async with async_client(token=access_token(record)) as client:
        page = 1
        while True:
            raw = await request_json(
                client,
                "/user/repos",
                {
                    "visibility": "all",
                    "affiliation": "owner,collaborator,organization_member",
                    "per_page": 100,
                    "page": page,
                    "sort": "full_name",
                    "direction": "asc",
                },
                use_cache=False,
            )
            if not isinstance(raw, list):
                raise GitHubAPIError("GitHub returned invalid repository data.", 502)
            for item in raw:
                metadata = repository_metadata(item)
                repos[metadata["id"]] = metadata
            if len(raw) < 100:
                break
            page += 1
    await asyncio.to_thread(require_current_connection, record)
    user_id = record["user_id"]
    data = list(repos.values())
    with _cache_lock:
        _repositories[user_id] = (
            record["version"],
            time.monotonic() + REPOSITORY_CACHE_SECONDS,
            data,
        )
        _repositories.move_to_end(user_id)
        if len(_repositories) > 64:
            _repositories.popitem(last=False)
    return data


async def repositories(record: dict, *, refresh: bool = False) -> list[dict]:
    user_id, version = record["user_id"], record["version"]
    key = (user_id, version)
    with _cache_lock:
        cached = _repositories.get(user_id)
    if not refresh and cached and cached[0] == version and cached[1] > time.monotonic():
        return cached[2]
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_fetch_repositories(record))
        _inflight[key] = task

        def remove(completed):
            _inflight.pop(key, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(remove)
    return await asyncio.shield(task)


async def revoke(token: str) -> bool:
    client_id = os.getenv("PR_MENTOR_GITHUB_CLIENT_ID", "")
    secret = os.getenv("PR_MENTOR_GITHUB_CLIENT_SECRET", "")
    if not client_id or not secret or not re.fullmatch(r"[A-Za-z0-9_-]+", client_id):
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.request(
                "DELETE",
                f"https://api.github.com/applications/{client_id}/token",
                auth=(client_id, secret),
                json={"access_token": token},
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        return response.status_code == 204
    except httpx.RequestError:
        return False
