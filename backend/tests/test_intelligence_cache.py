"""Focused tests for the repository intelligence SQLite cache (T1) and
the get_or_analyze_repository read-through/write-through wrapper (T2).

All tests use an in-memory SQLite database via monkeypatching so no real
database file is created and no GitHub calls are made.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

OWNER = "acme"
REPO = "widget"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_OTHER_REPO_SHA = "c" * 40

_SAMPLE_RESULT: dict[str, Any] = {
    "repository": f"{OWNER}/{REPO}",
    "snapshot_sha": SHA_A,
    "manifest": [],
    "manifest_count": 3,
    "manifest_truncated": False,
    "python_modules": [{"path": "pkg/a.py", "parse_status": "parsed", "definitions": [], "imports": []}],
    "python_files_considered": 1,
    "internal_import_edges": [],
    "dependency_context": [],
    "circular_dependencies": [],
    "limitations": ["test only"],
}


@pytest.fixture()
def in_memory_connection():
    """Return a factory that always yields the same in-memory SQLite connection.

    ``check_same_thread=False`` is required because the T2 integration tests
    run ``get_cached`` / ``put_cached`` via ``run_in_executor`` on a worker
    thread while the fixture is set up on the main thread.
    """
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row

    @contextmanager
    def _connection():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    return _connection, db


@pytest.fixture()
def index_module(in_memory_connection, monkeypatch):
    """Patch intelligence_index to use an in-memory database."""
    conn_factory, db = in_memory_connection
    import app.database.intelligence_index as idx_mod
    monkeypatch.setattr(idx_mod, "connection", conn_factory)
    return idx_mod, db


# ===========================================================================
# T1 — intelligence_index module (get_cached, put_cached, evict_old)
# ===========================================================================

class TestGetCached:
    def test_returns_none_on_empty_cache(self, index_module):
        idx, _db = index_module
        assert idx.get_cached(OWNER, REPO, SHA_A) is None

    def test_returns_stored_result(self, index_module):
        idx, _db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        result = idx.get_cached(OWNER, REPO, SHA_A)
        assert result is not None
        assert result["snapshot_sha"] == SHA_A
        assert result["manifest_count"] == 3

    def test_different_sha_is_cache_miss(self, index_module):
        idx, _db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        assert idx.get_cached(OWNER, REPO, SHA_B) is None

    def test_different_repository_is_cache_miss(self, index_module):
        idx, _db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        assert idx.get_cached(OWNER, "other-repo", SHA_A) is None

    def test_different_owner_is_cache_miss(self, index_module):
        idx, _db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        assert idx.get_cached("other-owner", REPO, SHA_A) is None

    def test_cache_hit_updates_accessed_at(self, index_module):
        idx, db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        # Manually backdate accessed_at to simulate an old entry.
        old_ts = time.time() - 86_400
        db.execute(
            "UPDATE repository_intelligence_index SET accessed_at = ? WHERE commit_sha = ?",
            (old_ts, SHA_A),
        )
        db.commit()
        idx.get_cached(OWNER, REPO, SHA_A)
        row = db.execute(
            "SELECT accessed_at FROM repository_intelligence_index WHERE commit_sha = ?",
            (SHA_A,),
        ).fetchone()
        assert row["accessed_at"] > old_ts

    def test_storage_error_returns_none_not_exception(self, monkeypatch):
        """get_cached must swallow storage errors."""
        import app.database.intelligence_index as idx_mod

        @contextmanager
        def _bad_connection():
            raise OSError("disk full")
            yield  # noqa: unreachable

        monkeypatch.setattr(idx_mod, "connection", _bad_connection)
        assert idx_mod.get_cached(OWNER, REPO, SHA_A) is None


class TestPutCached:
    def test_stores_and_round_trips_json(self, index_module):
        idx, _db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        result = idx.get_cached(OWNER, REPO, SHA_A)
        assert result["python_modules"][0]["path"] == "pkg/a.py"

    def test_overwrites_existing_entry(self, index_module):
        idx, _db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        updated = {**_SAMPLE_RESULT, "manifest_count": 99}
        idx.put_cached(OWNER, REPO, SHA_A, updated)
        assert idx.get_cached(OWNER, REPO, SHA_A)["manifest_count"] == 99

    def test_multiple_shas_stored_independently(self, index_module):
        idx, _db = index_module
        result_a = {**_SAMPLE_RESULT, "snapshot_sha": SHA_A}
        result_b = {**_SAMPLE_RESULT, "snapshot_sha": SHA_B, "manifest_count": 7}
        idx.put_cached(OWNER, REPO, SHA_A, result_a)
        idx.put_cached(OWNER, REPO, SHA_B, result_b)
        assert idx.get_cached(OWNER, REPO, SHA_A)["snapshot_sha"] == SHA_A
        assert idx.get_cached(OWNER, REPO, SHA_B)["manifest_count"] == 7

    def test_multiple_repositories_stored_independently(self, index_module):
        idx, _db = index_module
        result_w = {**_SAMPLE_RESULT, "repository": f"{OWNER}/widget"}
        result_g = {**_SAMPLE_RESULT, "repository": f"{OWNER}/gadget", "manifest_count": 55}
        idx.put_cached(OWNER, "widget", SHA_A, result_w)
        idx.put_cached(OWNER, "gadget", SHA_A, result_g)
        assert idx.get_cached(OWNER, "widget", SHA_A)["repository"] == f"{OWNER}/widget"
        assert idx.get_cached(OWNER, "gadget", SHA_A)["manifest_count"] == 55

    def test_refuses_empty_dict(self, index_module):
        idx, _db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, {})
        assert idx.get_cached(OWNER, REPO, SHA_A) is None

    def test_refuses_non_dict(self, index_module):
        idx, _db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, None)  # type: ignore[arg-type]
        assert idx.get_cached(OWNER, REPO, SHA_A) is None

    def test_oversized_payload_is_skipped(self, index_module, monkeypatch):
        idx, _db = index_module
        monkeypatch.setattr(idx, "_MAX_PAYLOAD_BYTES", 10)  # tiny limit
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        assert idx.get_cached(OWNER, REPO, SHA_A) is None

    def test_storage_error_does_not_raise(self, monkeypatch):
        """put_cached must swallow storage errors."""
        import app.database.intelligence_index as idx_mod

        @contextmanager
        def _bad_connection():
            raise OSError("disk full")
            yield  # noqa: unreachable

        monkeypatch.setattr(idx_mod, "connection", _bad_connection)
        # Must not raise.
        idx_mod.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)


class TestEvictOld:
    def test_evicts_entries_older_than_ttl(self, index_module):
        idx, db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        # Backdate both timestamps to simulate a stale entry.
        stale_ts = time.time() - 10
        db.execute(
            "UPDATE repository_intelligence_index SET created_at = ?, accessed_at = ?",
            (stale_ts, stale_ts),
        )
        db.commit()
        deleted = idx.evict_old(ttl_seconds=5)
        assert deleted == 1
        assert idx.get_cached(OWNER, REPO, SHA_A) is None

    def test_keeps_recently_accessed_entries(self, index_module):
        idx, _db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        deleted = idx.evict_old(ttl_seconds=3600)
        assert deleted == 0
        assert idx.get_cached(OWNER, REPO, SHA_A) is not None

    def test_evicts_only_stale_not_fresh(self, index_module):
        idx, db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, {**_SAMPLE_RESULT, "snapshot_sha": SHA_A})
        idx.put_cached(OWNER, REPO, SHA_B, {**_SAMPLE_RESULT, "snapshot_sha": SHA_B})
        # Stale only SHA_A.
        stale_ts = time.time() - 100
        db.execute(
            "UPDATE repository_intelligence_index SET accessed_at = ? WHERE commit_sha = ?",
            (stale_ts, SHA_A),
        )
        db.commit()
        deleted = idx.evict_old(ttl_seconds=50)
        assert deleted == 1
        assert idx.get_cached(OWNER, REPO, SHA_A) is None
        assert idx.get_cached(OWNER, REPO, SHA_B) is not None

    def test_evict_all_with_ttl_zero(self, index_module):
        idx, db = index_module
        idx.put_cached(OWNER, REPO, SHA_A, _SAMPLE_RESULT)
        idx.put_cached(OWNER, REPO, SHA_B, _SAMPLE_RESULT)
        # Backdate both entries to ensure they are strictly in the past.
        old_ts = time.time() - 1
        db.execute("UPDATE repository_intelligence_index SET accessed_at = ?", (old_ts,))
        db.commit()
        deleted = idx.evict_old(ttl_seconds=0)
        assert deleted == 2

    def test_eviction_error_returns_zero_not_exception(self, monkeypatch):
        """evict_old must swallow storage errors."""
        import app.database.intelligence_index as idx_mod

        @contextmanager
        def _bad_connection():
            raise OSError("disk full")
            yield  # noqa: unreachable

        monkeypatch.setattr(idx_mod, "connection", _bad_connection)
        assert idx_mod.evict_old() == 0


# ===========================================================================
# T2 — get_or_analyze_repository (read-through / write-through wrapper)
# ===========================================================================

class TestGetOrAnalyzeRepository:
    """Tests for the caching wrapper.

    analyze_repository and cache helpers are both monkeypatched so no
    GitHub API calls or real database writes happen.

    Windows / ProactorEventLoop note
    ---------------------------------
    ``get_or_analyze_repository`` uses ``loop.run_in_executor`` to push
    SQLite I/O off the event loop.  On Windows the default ProactorEventLoop
    creates internal sockets during ``asyncio.run()`` that block in tests
    lacking network access.  We avoid this by running the coroutine on a
    fresh ``SelectorEventLoop`` (the POSIX default, also available on
    Windows) which does not need socket infrastructure.
    """

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_cache(self, stored: dict[str, Any] | None = None):
        """Return (get_fn, put_calls) pair with optional pre-stored result."""
        put_calls: list[tuple] = []

        def get_fn(owner, repo, ref):
            if stored is not None and (owner, repo, ref) == (OWNER, REPO, SHA_A):
                return stored
            return None

        def put_fn(owner, repo, ref, data):
            put_calls.append((owner, repo, ref, data))

        return get_fn, put_fn, put_calls

    def test_cache_hit_returns_cached_without_calling_analyze(self, monkeypatch):
        import app.services.repository_intelligence as svc
        get_fn, put_fn, put_calls = self._make_cache(stored=_SAMPLE_RESULT)
        analyze_calls = []

        async def fake_analyze(*_args, **_kwargs):
            analyze_calls.append(True)
            return _SAMPLE_RESULT

        monkeypatch.setattr(svc, "_cache_get", get_fn)
        monkeypatch.setattr(svc, "_cache_put", put_fn)
        monkeypatch.setattr(svc, "analyze_repository", fake_analyze)

        result = self._run(svc.get_or_analyze_repository(OWNER, REPO, SHA_A))
        assert result["snapshot_sha"] == SHA_A
        assert analyze_calls == [], "analyze_repository must not be called on a cache hit"
        assert put_calls == [], "put_cached must not be called on a cache hit"

    def test_cache_miss_calls_analyze_and_stores_result(self, monkeypatch):
        import app.services.repository_intelligence as svc
        get_fn, put_fn, put_calls = self._make_cache(stored=None)

        async def fake_analyze(*_args, **_kwargs):
            return _SAMPLE_RESULT

        monkeypatch.setattr(svc, "_cache_get", get_fn)
        monkeypatch.setattr(svc, "_cache_put", put_fn)
        monkeypatch.setattr(svc, "analyze_repository", fake_analyze)

        result = self._run(svc.get_or_analyze_repository(OWNER, REPO, SHA_A))
        assert result["manifest_count"] == 3
        assert len(put_calls) == 1
        stored_owner, stored_repo, stored_ref, stored_data = put_calls[0]
        assert (stored_owner, stored_repo, stored_ref) == (OWNER, REPO, SHA_A)
        assert stored_data["manifest_count"] == 3

    def test_invalid_sha_raises_before_cache_or_analyze(self, monkeypatch):
        import app.services.repository_intelligence as svc
        get_calls = []

        def get_fn(*_args):
            get_calls.append(True)
            return None

        monkeypatch.setattr(svc, "_cache_get", get_fn)

        with pytest.raises(ValueError, match="full commit SHA"):
            self._run(svc.get_or_analyze_repository(OWNER, REPO, "main"))
        assert get_calls == [], "Cache must not be touched for an invalid ref"

    def test_cache_read_error_falls_through_to_analyze(self, monkeypatch):
        """A cache read exception must be swallowed and analysis must still run."""
        import app.services.repository_intelligence as svc

        def bad_get(*_args):
            raise OSError("disk full")

        put_calls: list = []

        async def fake_analyze(*_args, **_kwargs):
            return _SAMPLE_RESULT

        monkeypatch.setattr(svc, "_cache_get", bad_get)
        monkeypatch.setattr(svc, "_cache_put", lambda *a: put_calls.append(a))
        monkeypatch.setattr(svc, "analyze_repository", fake_analyze)

        result = self._run(svc.get_or_analyze_repository(OWNER, REPO, SHA_A))
        assert result["manifest_count"] == 3
        assert len(put_calls) == 1

    def test_cache_write_error_does_not_suppress_result(self, monkeypatch):
        """A cache write exception must be swallowed and the result returned."""
        import app.services.repository_intelligence as svc

        def get_fn(*_args):
            return None

        def bad_put(*_args):
            raise OSError("disk full")

        async def fake_analyze(*_args, **_kwargs):
            return _SAMPLE_RESULT

        monkeypatch.setattr(svc, "_cache_get", get_fn)
        monkeypatch.setattr(svc, "_cache_put", bad_put)
        monkeypatch.setattr(svc, "analyze_repository", fake_analyze)

        result = self._run(svc.get_or_analyze_repository(OWNER, REPO, SHA_A))
        assert result["manifest_count"] == 3

    def test_failed_analysis_does_not_write_cache(self, monkeypatch):
        """If analyze_repository raises, nothing should be persisted."""
        import app.services.repository_intelligence as svc

        def get_fn(*_args):
            return None

        put_calls: list = []

        async def failing_analyze(*_args, **_kwargs):
            raise ValueError("GitHub returned an invalid repository tree.")

        monkeypatch.setattr(svc, "_cache_get", get_fn)
        monkeypatch.setattr(svc, "_cache_put", lambda *a: put_calls.append(a))
        monkeypatch.setattr(svc, "analyze_repository", failing_analyze)

        with pytest.raises(ValueError, match="invalid repository tree"):
            self._run(svc.get_or_analyze_repository(OWNER, REPO, SHA_A))
        assert put_calls == [], "Nothing must be cached after a failed analysis"

    def test_different_shas_produce_independent_cache_entries(self, index_module, monkeypatch):
        """End-to-end: two different SHAs are stored and retrieved independently."""
        idx, _db = index_module
        import app.services.repository_intelligence as svc

        # Wire the wrapper to use the in-memory index_module.
        monkeypatch.setattr(svc, "_cache_get", idx.get_cached)
        monkeypatch.setattr(svc, "_cache_put", idx.put_cached)

        call_log: list[str] = []

        async def fake_analyze(owner, repo, ref, **_kw):
            call_log.append(ref)
            return {**_SAMPLE_RESULT, "snapshot_sha": ref, "manifest_count": len(ref)}

        monkeypatch.setattr(svc, "analyze_repository", fake_analyze)

        result_a = self._run(svc.get_or_analyze_repository(OWNER, REPO, SHA_A))
        result_b = self._run(svc.get_or_analyze_repository(OWNER, REPO, SHA_B))
        # Second call for each SHA should be a cache hit.
        result_a2 = self._run(svc.get_or_analyze_repository(OWNER, REPO, SHA_A))
        result_b2 = self._run(svc.get_or_analyze_repository(OWNER, REPO, SHA_B))

        assert result_a["snapshot_sha"] == SHA_A
        assert result_b["snapshot_sha"] == SHA_B
        assert result_a2["snapshot_sha"] == SHA_A
        assert result_b2["snapshot_sha"] == SHA_B
        # analyze_repository should only have been called once per SHA.
        assert call_log == [SHA_A, SHA_B], f"Unexpected call log: {call_log}"

    def test_different_repositories_produce_independent_cache_entries(self, index_module, monkeypatch):
        """Two different repos with the same SHA must not share a cache entry."""
        idx, _db = index_module
        import app.services.repository_intelligence as svc
        monkeypatch.setattr(svc, "_cache_get", idx.get_cached)
        monkeypatch.setattr(svc, "_cache_put", idx.put_cached)

        call_log: list[str] = []

        async def fake_analyze(owner, repo, ref, **_kw):
            call_log.append(repo)
            return {**_SAMPLE_RESULT, "repository": f"{owner}/{repo}"}

        monkeypatch.setattr(svc, "analyze_repository", fake_analyze)

        r1 = self._run(svc.get_or_analyze_repository(OWNER, "alpha", SHA_A))
        r2 = self._run(svc.get_or_analyze_repository(OWNER, "beta", SHA_A))
        assert r1["repository"] == f"{OWNER}/alpha"
        assert r2["repository"] == f"{OWNER}/beta"
        assert set(call_log) == {"alpha", "beta"}
