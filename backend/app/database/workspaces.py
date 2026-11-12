"""Workspace, membership, invitation, and review-share persistence."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.database.connection import connection

ROLES = {"owner", "admin", "editor", "viewer"}
INVITABLE_ROLES = {"admin", "editor", "viewer"}
INVITATION_DAYS = 7


def create_workspace(user_id: str, name: str) -> dict[str, Any]:
    workspace_id = uuid4().hex
    now = datetime.now(UTC)
    if _mongo():
        db = _db()
        db["workspaces"].insert_one({"workspace_id": workspace_id, "name": name, "created_by": user_id, "created_at": now})
        db["workspace_members"].insert_one({"workspace_id": workspace_id, "user_id": user_id, "role": "owner", "joined_at": now})
    else:
        with connection() as db:
            db.execute("INSERT INTO workspaces(workspace_id, name, created_by, created_at) VALUES (?, ?, ?, ?)", (workspace_id, name, user_id, now.isoformat()))
            db.execute("INSERT INTO workspace_members(workspace_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)", (workspace_id, user_id, now.isoformat()))
    return {"workspace_id": workspace_id, "name": name, "created_by": user_id, "created_at": now.isoformat(), "role": "owner"}


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    if _mongo():
        item = _db()["workspaces"].find_one({"workspace_id": workspace_id}, {"_id": 0})
        if item and isinstance(item.get("created_at"), datetime):
            item["created_at"] = item["created_at"].isoformat()
        return item
    with connection() as db:
        row = db.execute("SELECT workspace_id, name, created_by, created_at FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
    return dict(row) if row else None


def get_role(workspace_id: str, user_id: str) -> str | None:
    if _mongo():
        item = _db()["workspace_members"].find_one({"workspace_id": workspace_id, "user_id": user_id}, {"role": 1})
        return item.get("role") if item else None
    with connection() as db:
        row = db.execute("SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?", (workspace_id, user_id)).fetchone()
    return str(row["role"]) if row else None


def list_workspaces(user_id: str) -> list[dict[str, Any]]:
    if _mongo():
        rows = list(_db()["workspace_members"].find({"user_id": user_id}, {"_id": 0, "workspace_id": 1, "role": 1}))
        results = []
        for membership in rows:
            item = get_workspace(membership["workspace_id"])
            if item:
                results.append({**item, "role": membership["role"]})
        return results
    with connection() as db:
        rows = db.execute("SELECT w.workspace_id, w.name, w.created_by, w.created_at, m.role FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.workspace_id WHERE m.user_id=? ORDER BY w.created_at DESC", (user_id,)).fetchall()
    return [dict(row) for row in rows]


def list_members(workspace_id: str) -> list[dict[str, Any]]:
    if _mongo():
        rows = list(_db()["workspace_members"].find({"workspace_id": workspace_id}, {"_id": 0}))
        users = _db()["auth_users"]
        return [{**row, "email": (users.find_one({"user_id": row["user_id"]}, {"email": 1}) or {}).get("email")} for row in rows]
    with connection() as db:
        rows = db.execute("SELECT m.user_id, u.email, m.role, m.joined_at FROM workspace_members m JOIN auth_users u ON u.user_id=m.user_id WHERE m.workspace_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, m.joined_at", (workspace_id,)).fetchall()
    return [dict(row) for row in rows]


def create_invitation(token_hash: str, workspace_id: str, email: str, role: str, invited_by: str) -> datetime:
    expires = datetime.now(UTC) + timedelta(days=INVITATION_DAYS)
    if _mongo():
        _db()["workspace_invitations"].insert_one({"token_hash": token_hash, "workspace_id": workspace_id, "email": email, "role": role, "invited_by": invited_by, "expires_at": expires, "accepted_at": None})
    else:
        with connection() as db:
            db.execute("INSERT INTO workspace_invitations(token_hash, workspace_id, email, role, invited_by, expires_at) VALUES (?, ?, ?, ?, ?, ?)", (token_hash, workspace_id, email, role, invited_by, expires.isoformat()))
    return expires


def accept_invitation(token_hash: str, user_id: str, email: str) -> str | None:
    now = datetime.now(UTC)
    if _mongo():
        invitations = _db()["workspace_invitations"]
        item = invitations.find_one_and_update({"token_hash": token_hash, "email": email, "accepted_at": None, "expires_at": {"$gt": now}}, {"$set": {"accepted_at": now}})
        if not item:
            return None
        _db()["workspace_members"].update_one({"workspace_id": item["workspace_id"], "user_id": user_id}, {"$setOnInsert": {"workspace_id": item["workspace_id"], "user_id": user_id, "role": item["role"], "joined_at": now}}, upsert=True)
        return str(item["workspace_id"])
    with connection() as db:
        item = db.execute("SELECT workspace_id, role FROM workspace_invitations WHERE token_hash=? AND email=? AND accepted_at IS NULL AND expires_at>?", (token_hash, email, now.isoformat())).fetchone()
        if not item:
            return None
        changed = db.execute("UPDATE workspace_invitations SET accepted_at=? WHERE token_hash=? AND accepted_at IS NULL AND expires_at>?", (now.isoformat(), token_hash, now.isoformat()))
        if changed.rowcount != 1:
            return None
        db.execute("INSERT INTO workspace_members(workspace_id, user_id, role, joined_at) VALUES (?, ?, ?, ?) ON CONFLICT(workspace_id, user_id) DO NOTHING", (item["workspace_id"], user_id, item["role"], now.isoformat()))
        return str(item["workspace_id"])


def change_member_role(workspace_id: str, user_id: str, role: str) -> bool:
    if role not in INVITABLE_ROLES:
        return False
    if _mongo():
        result = _db()["workspace_members"].update_one({"workspace_id": workspace_id, "user_id": user_id, "role": {"$ne": "owner"}}, {"$set": {"role": role}})
        return result.modified_count > 0
    with connection() as db:
        result = db.execute("UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=? AND role!='owner'", (role, workspace_id, user_id))
    return result.rowcount > 0


def remove_member(workspace_id: str, user_id: str) -> bool:
    if _mongo():
        result = _db()["workspace_members"].delete_one({"workspace_id": workspace_id, "user_id": user_id, "role": {"$ne": "owner"}})
        return result.deleted_count > 0
    with connection() as db:
        result = db.execute("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=? AND role!='owner'", (workspace_id, user_id))
    return result.rowcount > 0


def link_review(workspace_id: str, review_id: str, shared_by: str) -> None:
    now = datetime.now(UTC)
    if _mongo():
        _db()["workspace_review_shares"].update_one({"workspace_id": workspace_id, "review_id": review_id}, {"$setOnInsert": {"workspace_id": workspace_id, "review_id": review_id, "shared_by": shared_by, "shared_at": now}}, upsert=True)
    else:
        with connection() as db:
            db.execute("INSERT INTO workspace_review_shares(workspace_id, review_id, shared_by, shared_at) VALUES (?, ?, ?, ?) ON CONFLICT(workspace_id, review_id) DO NOTHING", (workspace_id, review_id, shared_by, now.isoformat()))


def unlink_review(workspace_id: str, review_id: str) -> bool:
    if _mongo():
        return _db()["workspace_review_shares"].delete_one({"workspace_id": workspace_id, "review_id": review_id}).deleted_count > 0
    with connection() as db:
        result = db.execute("DELETE FROM workspace_review_shares WHERE workspace_id=? AND review_id=?", (workspace_id, review_id))
    return result.rowcount > 0


def review_role(user_id: str, review_id: str) -> str | None:
    if _mongo():
        shares = _db()["workspace_review_shares"]
        memberships = _db()["workspace_members"]
        roles = []
        for shared in shares.find({"review_id": review_id}, {"workspace_id": 1}):
            member = memberships.find_one({"workspace_id": shared["workspace_id"], "user_id": user_id}, {"role": 1})
            if member:
                roles.append(str(member["role"]))
        return max(roles, key=lambda role: {"owner": 4, "admin": 3, "editor": 2, "viewer": 1}.get(role, 0), default=None)
    with connection() as db:
        row = db.execute("SELECT m.role FROM workspace_review_shares s JOIN workspace_members m ON m.workspace_id=s.workspace_id WHERE s.review_id=? AND m.user_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'editor' THEN 2 ELSE 3 END LIMIT 1", (review_id, user_id)).fetchone()
    return str(row["role"]) if row else None


def list_shared_review_ids(workspace_id: str, *, limit: int | None = None) -> list[str]:
    if _mongo():
        cursor = _db()["workspace_review_shares"].find({"workspace_id": workspace_id}, {"review_id": 1}).sort("shared_at", -1)
        if limit is not None:
            cursor = cursor.limit(max(0, limit))
        return [str(row["review_id"]) for row in cursor]
    with connection() as db:
        query = "SELECT review_id FROM workspace_review_shares WHERE workspace_id=? ORDER BY shared_at DESC"
        params: tuple[Any, ...] = (workspace_id,)
        if limit is not None:
            query += " LIMIT ?"
            params += (max(0, limit),)
        rows = db.execute(query, params).fetchall()
    return [str(row["review_id"]) for row in rows]


def user_email(user_id: str) -> str | None:
    if _mongo():
        item = _db()["auth_users"].find_one({"user_id": user_id}, {"email": 1})
        return str(item["email"]) if item and isinstance(item.get("email"), str) else None
    with connection() as db:
        row = db.execute("SELECT email FROM auth_users WHERE user_id=?", (user_id,)).fetchone()
    return str(row["email"]) if row else None


def hash_invitation(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mongo() -> bool:
    from app.config import storage_backend
    return storage_backend() == "mongodb"


def _db() -> Any:
    from app.database.mongo_connection import get_mongo_database
    return get_mongo_database()
