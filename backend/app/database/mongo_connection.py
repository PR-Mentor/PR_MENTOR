"""Process-wide, bounded MongoDB connection management."""

from __future__ import annotations

from threading import RLock
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pymongo import MongoClient
from pymongo.errors import ConfigurationError, PyMongoError

from app.config import mongodb_database, mongodb_uri


class StorageUnavailable(RuntimeError):
    """Configured persistence is unavailable or invalid."""


_lock = RLock()
_client: MongoClient[dict[str, Any]] | None = None
_client_uri: str | None = None
_indexed_databases: set[tuple[str, str]] = set()


def _validate_uri(uri: str) -> None:
    try:
        parts = urlsplit(uri)
        options = {key.lower(): values[-1].lower() for key, values in parse_qs(parts.query).items() if values}
        if parts.scheme not in {"mongodb", "mongodb+srv"} or not parts.netloc:
            raise ValueError
        if options.get("tlsallowinvalidcertificates") == "true" or options.get("tlsinsecure") == "true":
            raise ValueError
        hostname = (parts.hostname or "").lower()
        local_host = hostname in {"localhost", "127.0.0.1", "::1"}
        tls_option = options.get("tls", options.get("ssl"))
        if (parts.scheme == "mongodb+srv" or not local_host) and tls_option == "false":
            raise ValueError
        if not local_host and tls_option != "true" and parts.scheme != "mongodb+srv":
            raise ValueError
    except ValueError as exc:
        raise StorageUnavailable("MONGODB_URI is invalid or disables TLS certificate validation.") from exc


def get_mongo_client() -> MongoClient[dict[str, Any]]:
    global _client, _client_uri
    uri = mongodb_uri()
    if not uri:
        raise StorageUnavailable("PR_MENTOR_STORAGE=mongodb requires MONGODB_URI to be configured.")
    _validate_uri(uri)
    with _lock:
        if _client is None or _client_uri != uri:
            if _client is not None:
                _client.close()
            try:
                _client = MongoClient(
                    uri,
                    tz_aware=True,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=10000,
                )
            except (ConfigurationError, ValueError) as exc:
                _client = None
                _client_uri = None
                raise StorageUnavailable("MongoDB connection configuration is invalid.") from exc
            _client_uri = uri
            _indexed_databases.clear()
        return _client


def get_mongo_database():
    client = get_mongo_client()
    name = mongodb_database()
    database = client[name]
    key = (_client_uri or "", name)
    with _lock:
        if key not in _indexed_databases:
            from app.database.mongo_reviews import create_indexes

            create_indexes(database)
            _indexed_databases.add(key)
    return database


def ping_mongo() -> None:
    try:
        get_mongo_database().command("ping")
    except PyMongoError as exc:
        raise StorageUnavailable("MongoDB readiness check failed.") from exc


def close_mongo_client() -> None:
    global _client, _client_uri
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
        _client_uri = None
        _indexed_databases.clear()
