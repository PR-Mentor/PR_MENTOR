"""Account and opaque-session persistence for the first-party API."""

import hashlib
import re
import time
from datetime import UTC, datetime
from typing import Any

from app.database.connection import connection


def create_user(user_id: str, email: str, password_hash: str) -> bool:
    clean_email = email.strip().casefold()
    now = datetime.now(UTC)
    if _mongo():
        from pymongo.errors import DuplicateKeyError

        users = _mongo_db()["auth_users"]
        try:
            users.insert_one(
                {
                    "user_id": user_id,
                    "email": clean_email,
                    "password_hash": password_hash,
                    "created_at": now,
                }
            )
            identities = _mongo_db()["auth_identities"]
            identities.insert_one(
                {
                    "identity_id": f"local:{user_id}",
                    "provider": "local",
                    "provider_uid": user_id,
                    "email": clean_email,
                    "email_verified": True,
                    "user_id": user_id,
                    "created_at": now,
                    "linked_at": now,
                }
            )
        except DuplicateKeyError:
            return False
        return True
    with connection() as db:
        try:
            db.execute(
                "INSERT INTO auth_users(user_id, email, password_hash) VALUES (?, ?, ?)",
                (user_id, clean_email, password_hash),
            )
            db.execute(
                """
                INSERT OR REPLACE INTO auth_identities(identity_id, provider, provider_uid, email, email_verified, user_id, created_at, linked_at)
                VALUES (?, 'local', ?, ?, 1, ?, ?, ?)
                """,
                (f"local:{user_id}", user_id, clean_email, user_id, now.isoformat(), now.isoformat()),
            )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                return False
            raise
    return True


def get_user_by_email(email: str) -> dict[str, Any] | None:
    if _mongo():
        return _mongo_db()["auth_users"].find_one({"email": email}, {"_id": 0})
    with connection() as db:
        row = db.execute(
            "SELECT user_id, email, password_hash FROM auth_users WHERE email = ?",
            (email,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> dict[str, str] | None:
    """Return public account identity without password or session material."""
    if _mongo():
        item = _mongo_db()["auth_users"].find_one(
            {"user_id": user_id}, {"_id": 0, "user_id": 1, "email": 1}
        )
        return {"user_id": str(item["user_id"]), "email": str(item["email"])} if item else None
    with connection() as db:
        row = db.execute(
            "SELECT user_id, email FROM auth_users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def get_identity(provider: str, provider_uid: str) -> dict[str, Any] | None:
    if _mongo():
        item = _mongo_db()["auth_identities"].find_one(
            {"provider": provider, "provider_uid": provider_uid}, {"_id": 0}
        )
        return dict(item) if item else None
    with connection() as db:
        row = db.execute(
            "SELECT identity_id, provider, provider_uid, email, email_verified, user_id, created_at, linked_at "
            "FROM auth_identities WHERE provider = ? AND provider_uid = ?",
            (provider, provider_uid),
        ).fetchone()
        return dict(row) if row else None


def list_identities_for_user(user_id: str) -> list[dict[str, Any]]:
    if _mongo():
        cursor = _mongo_db()["auth_identities"].find({"user_id": user_id}, {"_id": 0})
        return [dict(doc) for doc in cursor]
    with connection() as db:
        rows = db.execute(
            "SELECT identity_id, provider, provider_uid, email, email_verified, user_id, created_at, linked_at "
            "FROM auth_identities WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_or_create_identity(claims: dict[str, Any]) -> dict[str, Any]:
    """
    Resolve external claims to a canonical internal user record.

    Security & Account Takeover Defense:
    1. If the provider identity was previously mapped, reuses the mapped user_id.
    2. If an existing internal account has this email:
       - If external email is verified: links the external provider identity to the existing account.
       - If external email is NOT verified: DOES NOT LINK (prevents account takeover).
         Assigns an isolated account with a disambiguated email address.
    3. If no existing account has this email: creates a new auth_users record and maps it.
    """
    uid = claims.get("uid")
    if not uid or not isinstance(uid, str):
        raise ValueError("A valid uid is required to resolve identity.")

    raw_email = claims.get("email") or ""
    clean_email = raw_email.strip().casefold()
    email_verified = bool(claims.get("email_verified"))
    provider = str(claims.get("auth_provider") or (claims.get("firebase") or {}).get("sign_in_provider") or "external")
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    # 1. Check if identity is already linked
    existing_ident = get_identity(provider, uid)
    if existing_ident:
        resolved_uid = existing_ident["user_id"]
        # Update email_verified if it transitioned to True
        if email_verified and not existing_ident.get("email_verified"):
            if _mongo():
                _mongo_db()["auth_identities"].update_one(
                    {"provider": provider, "provider_uid": uid},
                    {"$set": {"email_verified": True, "linked_at": now}}
                )
            else:
                with connection() as db:
                    db.execute(
                        "UPDATE auth_identities SET email_verified = 1, linked_at = ? WHERE provider = ? AND provider_uid = ?",
                        (now_iso, provider, uid),
                    )
        user_row = get_user_by_id(resolved_uid)
        return {
            "user_id": resolved_uid,
            "email": user_row["email"] if user_row else (existing_ident.get("email") or clean_email),
            "email_verified": email_verified or bool(existing_ident.get("email_verified")),
        }

    # 2. Identity not yet linked. Check if an account already exists with this email.
    existing_user = get_user_by_email(clean_email) if clean_email else None

    if existing_user:
        if email_verified:
            # Verified email matching existing account: securely link without creating a duplicate
            canonical_uid = existing_user["user_id"]
            ident_id = f"{provider}:{uid}"
            if _mongo():
                _mongo_db()["auth_identities"].insert_one({
                    "identity_id": ident_id,
                    "provider": provider,
                    "provider_uid": uid,
                    "email": clean_email,
                    "email_verified": True,
                    "user_id": canonical_uid,
                    "created_at": now,
                    "linked_at": now,
                })
            else:
                with connection() as db:
                    db.execute(
                        """
                        INSERT OR REPLACE INTO auth_identities
                        (identity_id, provider, provider_uid, email, email_verified, user_id, created_at, linked_at)
                        VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (ident_id, provider, uid, clean_email, canonical_uid, now_iso, now_iso),
                    )
            return {
                "user_id": canonical_uid,
                "email": existing_user["email"],
                "email_verified": True,
            }
        else:
            # UNVERIFIED EMAIL: DO NOT LINK!
            # Prevent account takeover: create an isolated identity with disambiguated email.
            safe_domain = clean_email.split("@")[1] if "@" in clean_email else "unverified.external"
            uid_slug = re.sub(r"[^a-z0-9]+", "-", uid.lower()).strip("-")
            isolated_email = f"unverified+{uid_slug[:12]}@{safe_domain}"
            ident_id = f"{provider}:{uid}"

            if _mongo():
                _mongo_db()["auth_users"].insert_one({
                    "user_id": uid,
                    "email": isolated_email,
                    "password_hash": "unverified_external",
                    "created_at": now,
                })
                _mongo_db()["auth_identities"].insert_one({
                    "identity_id": ident_id,
                    "provider": provider,
                    "provider_uid": uid,
                    "email": clean_email,
                    "email_verified": False,
                    "user_id": uid,
                    "created_at": now,
                    "linked_at": None,
                })
            else:
                with connection() as db:
                    db.execute(
                        "INSERT INTO auth_users(user_id, email, password_hash, created_at) VALUES (?, ?, 'unverified_external', ?)",
                        (uid, isolated_email, now_iso),
                    )
                    db.execute(
                        """
                        INSERT OR REPLACE INTO auth_identities
                        (identity_id, provider, provider_uid, email, email_verified, user_id, created_at, linked_at)
                        VALUES (?, ?, ?, ?, 0, ?, ?, NULL)
                        """,
                        (ident_id, provider, uid, clean_email, uid, now_iso),
                    )
            return {
                "user_id": uid,
                "email": isolated_email,
                "email_verified": False,
            }

    # 3. No existing user with this email: create new auth_users record and map it
    effective_email = clean_email if clean_email else f"{uid[:16]}@placeholder.local"
    ident_id = f"{provider}:{uid}"
    if _mongo():
        _mongo_db()["auth_users"].insert_one({
            "user_id": uid,
            "email": effective_email,
            "password_hash": "external",
            "created_at": now,
        })
        _mongo_db()["auth_identities"].insert_one({
            "identity_id": ident_id,
            "provider": provider,
            "provider_uid": uid,
            "email": effective_email,
            "email_verified": email_verified,
            "user_id": uid,
            "created_at": now,
            "linked_at": now if email_verified else None,
        })
    else:
        with connection() as db:
            db.execute(
                "INSERT INTO auth_users(user_id, email, password_hash, created_at) VALUES (?, ?, 'external', ?) "
                "ON CONFLICT(user_id) DO UPDATE SET email=excluded.email",
                (uid, effective_email, now_iso),
            )
            db.execute(
                """
                INSERT OR REPLACE INTO auth_identities
                (identity_id, provider, provider_uid, email, email_verified, user_id, created_at, linked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ident_id, provider, uid, effective_email, 1 if email_verified else 0, uid, now_iso, now_iso if email_verified else None),
            )
    return {
        "user_id": uid,
        "email": effective_email,
        "email_verified": email_verified,
    }


def upsert_external_auth_user(
    user_id: str, email: str, email_verified: bool = False, provider: str = "external"
) -> None:
    """Ensure an external authenticated user exists with secure identity resolution."""
    if not user_id:
        return
    resolve_or_create_identity({
        "uid": user_id,
        "email": email,
        "email_verified": email_verified,
        "auth_provider": provider,
    })


def save_session(token_hash: str, user_id: str, expires_at: datetime) -> None:
    if _mongo():
        _mongo_db()["auth_sessions"].replace_one(
            {"token_hash": token_hash},
            {"token_hash": token_hash, "user_id": user_id, "expires_at": expires_at},
            upsert=True,
        )
        return
    with connection() as db:
        db.execute(
            "INSERT INTO auth_sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, expires_at.isoformat()),
        )


def get_session(token_hash: str) -> dict[str, Any] | None:
    if _mongo():
        return _mongo_db()["auth_sessions"].find_one(
            {"token_hash": token_hash}, {"_id": 0}
        )
    with connection() as db:
        row = db.execute(
            "SELECT token_hash, user_id, expires_at FROM auth_sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token_hash: str) -> None:
    if _mongo():
        _mongo_db()["auth_sessions"].delete_one({"token_hash": token_hash})
        return
    with connection() as db:
        db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))


def allow_attempt(
    scope: str, key: str, *, limit: int = 10, window_seconds: int = 900
) -> bool:
    """Atomic fixed-window limiter; sensitive identifiers are never stored raw."""
    digest = hashlib.sha256(f"{scope}:{key}".encode()).hexdigest()
    window_start = int(time.time()) // window_seconds * window_seconds
    if _mongo():
        bucket_id = f"{digest}:{window_start}"
        bucket = _mongo_db()["auth_rate_limits"]
        bucket.update_one(
            {"_id": bucket_id},
            {
                "$inc": {"hit_count": 1},
                "$setOnInsert": {
                    "expires_at": datetime.fromtimestamp(
                        window_start + window_seconds, UTC
                    )
                },
            },
            upsert=True,
        )
        return (
            bucket.find_one({"_id": bucket_id}, {"hit_count": 1})["hit_count"] <= limit
        )
    with connection() as db:
        db.execute(
            "DELETE FROM auth_rate_limits WHERE window_start < ?",
            (window_start - window_seconds,),
        )
        db.execute(
            "INSERT INTO auth_rate_limits(bucket_key, window_start, hit_count) VALUES (?, ?, 1) "
            "ON CONFLICT(bucket_key) DO UPDATE SET hit_count=CASE WHEN window_start=excluded.window_start THEN hit_count+1 ELSE 1 END, window_start=excluded.window_start",
            (digest, window_start),
        )
        row = db.execute(
            "SELECT hit_count FROM auth_rate_limits WHERE bucket_key = ?", (digest,)
        ).fetchone()
    return row["hit_count"] <= limit


def _mongo() -> bool:
    from app.config import storage_backend

    return storage_backend() == "mongodb"


def _mongo_db():
    from app.database.mongo_connection import get_mongo_database

    return get_mongo_database()
