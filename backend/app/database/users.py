"""Persistence for Firebase-authenticated user profiles."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.config import storage_backend
from app.database.connection import connection
from app.database.mongo_connection import get_mongo_database

DEFAULT_PREFERENCES = {"theme": "dark", "explanation_level": "beginner", "notifications": True}


def _now() -> datetime:
    return datetime.now(UTC)


def _profile(claims: dict[str, Any]) -> dict[str, Any]:
    uid = claims.get("uid")
    if not isinstance(uid, str) or not uid:
        raise ValueError("Firebase token has no user identifier.")
    providers = [item.get("providerId") for item in claims.get("firebase", {}).get("sign_in_provider", [])] if isinstance(claims.get("firebase", {}).get("sign_in_provider"), list) else []
    provider = claims.get("firebase", {}).get("sign_in_provider")
    provider_ids = claims.get("firebase", {}).get("identities", {})
    providers = sorted(set([provider] if isinstance(provider, str) else providers) | set(provider_ids.keys()))
    name = claims.get("name")
    email = claims.get("email") if claims.get("email_verified") else None
    avatar = claims.get("picture")
    username = (claims.get("preferred_username") or claims.get("nickname") or (email.split("@", 1)[0] if email else uid[:16]))
    return {
        "firebase_uid": uid,
        "username": str(username)[:39],
        "display_name": str(name)[:100] if name else str(username)[:100],
        "email": email,
        "email_verified": bool(claims.get("email_verified", False)),
        "phone_number": claims.get("phone_number"),
        "phone_verified": bool(claims.get("phone_number")),
        "avatar_url": avatar,
        "providers": providers,
        "role": "user",
        "is_active": True,
        "preferences": DEFAULT_PREFERENCES.copy(),
        "updated_at": _now(),
        "last_login": _now(),
    }


def upsert_user(claims: dict[str, Any]) -> dict[str, Any]:
    incoming = _profile(claims)
    uid = incoming["firebase_uid"]
    if storage_backend() == "mongodb":
        collection = get_mongo_database()["users"]
        collection.create_index("firebase_uid", unique=True)
        existing = collection.find_one({"firebase_uid": uid})
        updates = {
            key: value for key, value in incoming.items()
            if key not in {"username", "display_name", "avatar_url", "email", "phone_number", "email_verified", "phone_verified", "providers", "preferences", "role", "created_at"}
        }
        for key in ("email", "phone_number", "avatar_url"):
            if incoming.get(key):
                updates[key] = incoming[key]
        if incoming.get("display_name") and (not existing or not existing.get("display_name") or existing.get("display_name") == existing.get("username")):
            updates["display_name"] = incoming["display_name"]
        for key in ("email_verified", "phone_verified"):
            updates[key] = bool(incoming[key] or (existing and existing.get(key)))
        existing_providers = list((existing or {}).get("providers", []))
        for p in incoming["providers"]:
            if p not in existing_providers:
                existing_providers.append(p)
        updates["providers"] = existing_providers
        collection.update_one({"firebase_uid": uid}, {"$set": updates, "$setOnInsert": {"created_at": _now(), "preferences": incoming["preferences"], "role": "user"}}, upsert=True)
        return collection.find_one({"firebase_uid": uid}, {"_id": 0}) or {}
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS users (firebase_uid TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        row = db.execute("SELECT payload FROM users WHERE firebase_uid = ?", (uid,)).fetchone()
        old = json.loads(row[0]) if row else {}
        merged = {**incoming, **old}
        for key in ("email", "phone_number"):
            if incoming.get(key):
                merged[key] = incoming[key]
        for key in ("email_verified", "phone_verified"):
            merged[key] = bool(incoming[key] or old.get(key, False))
        existing_providers = list(old.get("providers", []))
        for p in incoming["providers"]:
            if p not in existing_providers:
                existing_providers.append(p)
        merged["providers"] = existing_providers
        for key in ("updated_at", "last_login", "is_active"):
            merged[key] = incoming[key]
        for key in ("display_name", "avatar_url"):
            if incoming.get(key) and (key == "avatar_url" or not old.get(key) or old[key] == old.get("username")):
                merged[key] = incoming[key]
        merged.setdefault("created_at", _now())
        db.execute("INSERT INTO users(firebase_uid,payload) VALUES (?,?) ON CONFLICT(firebase_uid) DO UPDATE SET payload=excluded.payload", (uid, json.dumps(merged, default=lambda value: value.isoformat())))
        return merged


def _decode(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    for key in ("created_at", "updated_at", "last_login"):
        if isinstance(value.get(key), str):
            try:
                value[key] = datetime.fromisoformat(value[key])
            except ValueError:
                pass
    return value


def get_user(uid: str) -> dict[str, Any] | None:
    if storage_backend() == "mongodb":
        return get_mongo_database()["users"].find_one({"firebase_uid": uid}, {"_id": 0})
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS users (firebase_uid TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        row = db.execute("SELECT payload FROM users WHERE firebase_uid = ?", (uid,)).fetchone()
    return _decode(json.loads(row[0])) if row else None


def update_user(uid: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"display_name", "username", "preferences"}
    if set(changes) - allowed:
        raise ValueError("Only display name, username, and preferences can be changed.")
    if "preferences" in changes:
        prefs = changes["preferences"]
        if not isinstance(prefs, dict) or set(prefs) - set(DEFAULT_PREFERENCES):
            raise ValueError("Unsupported preference fields.")
        if "theme" in prefs and prefs["theme"] not in {"dark", "light", "system"}:
            raise ValueError("Theme must be dark, light, or system.")
        if "explanation_level" in prefs and prefs["explanation_level"] not in {"beginner", "intermediate", "advanced"}:
            raise ValueError("Invalid explanation level.")
        if "notifications" in prefs and not isinstance(prefs["notifications"], bool):
            raise ValueError("Notifications must be a boolean.")
    user = get_user(uid)
    if not user:
        return None
    if "preferences" in changes:
        changes = {**changes, "preferences": {**user.get("preferences", DEFAULT_PREFERENCES), **changes["preferences"]}}
    user.update(changes)
    user["updated_at"] = _now()
    if storage_backend() == "mongodb":
        get_mongo_database()["users"].update_one({"firebase_uid": uid}, {"$set": {key: value for key, value in changes.items()}, "$currentDate": {"updated_at": True}})
    else:
        with connection() as db:
            db.execute("UPDATE users SET payload=? WHERE firebase_uid=?", (json.dumps(user, default=lambda value: value.isoformat()), uid))
    return user


def delete_user(uid: str) -> bool:
    if storage_backend() == "mongodb":
        return get_mongo_database()["users"].delete_one({"firebase_uid": uid}).deleted_count > 0
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS users (firebase_uid TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        return db.execute("DELETE FROM users WHERE firebase_uid=?", (uid,)).rowcount > 0
