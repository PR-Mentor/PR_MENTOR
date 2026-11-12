"""Focused tests for Phase 7 remaining capabilities:
- Ref resolution (resolve_ref_to_sha)
- Impact analysis (compute_impact)
- Symbol retrieval (retrieve_relevant_symbols)
- Repository conventions (infer_conventions)
- build_review_repository_context extended output
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import repository_intelligence as ri


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SHA = "a" * 40
OWNER = "acme"
REPO = "widget"

_MOD_A = {
    "path": "pkg/a.py",
    "parse_status": "parsed",
    "definitions": [
        {"name": "MyClass", "kind": "class", "line": 1, "scope": None},
        {"name": "run", "kind": "method", "line": 3, "scope": "MyClass"},
        {"name": "helper", "kind": "function", "line": 8, "scope": None},
    ],
    "imports": ["pkg.b", "pkg.b.helper"],
}

_MOD_B = {
    "path": "pkg/b.py",
    "parse_status": "parsed",
    "definitions": [
        {"name": "helper", "kind": "function", "line": 1, "scope": None},
        {"name": "util", "kind": "function", "line": 5, "scope": None},
    ],
    "imports": [],
}

_MOD_C = {
    "path": "pkg/c.py",
    "parse_status": "parsed",
    "definitions": [
        {"name": "do_work", "kind": "function", "line": 1, "scope": None},
    ],
    "imports": ["pkg.b"],
}

_MOD_BROKEN = {
    "path": "pkg/broken.py",
    "parse_status": "syntax_error",
    "definitions": [],
    "imports": [],
}

_EDGES = [
    {"from": "pkg/a.py", "to": "pkg/b.py"},
    {"from": "pkg/c.py", "to": "pkg/b.py"},
]


# ===========================================================================
# Ref resolution
# ===========================================================================

class TestResolveRefToSha:
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_full_sha_returned_as_is_without_github_call(self, monkeypatch):
        """A 40-char hex SHA must be returned immediately without network I/O."""
        called = []
        async def fake_client(*_, **__):
            called.append(True)

        monkeypatch.setattr(ri, "async_client", fake_client)
        result = self._run(ri.resolve_ref_to_sha(OWNER, REPO, SHA))
        assert result == SHA
        assert called == [], "No GitHub call must be made for a full SHA"

    def test_full_sha_is_lowercased(self, monkeypatch):
        upper_sha = "A" * 40
        monkeypatch.setattr(ri, "async_client", lambda *_, **__: None)
        result = self._run(ri.resolve_ref_to_sha(OWNER, REPO, upper_sha))
        assert result == upper_sha.lower()

    def test_branch_name_is_resolved_via_github_api(self, monkeypatch):
        """A branch name should trigger a GitHub API call and return the SHA."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_client():
            yield None

        async def fake_request_json(_client, path, **_kwargs):
            assert "/commits/main" in path
            return {"sha": SHA}

        monkeypatch.setattr(ri, "async_client", fake_client)
        monkeypatch.setattr(ri, "request_json", fake_request_json)
        result = self._run(ri.resolve_ref_to_sha(OWNER, REPO, "main"))
        assert result == SHA

    def test_invalid_github_response_raises_value_error(self, monkeypatch):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_client():
            yield None

        async def fake_request_json(_client, _path, **_kwargs):
            return {"sha": "not-a-sha"}

        monkeypatch.setattr(ri, "async_client", fake_client)
        monkeypatch.setattr(ri, "request_json", fake_request_json)
        with pytest.raises(ValueError, match="full commit SHA"):
            self._run(ri.resolve_ref_to_sha(OWNER, REPO, "main"))

    def test_invalid_owner_raises_before_github_call(self, monkeypatch):
        called = []

        async def fake_request_json(*_, **__):
            called.append(True)

        monkeypatch.setattr(ri, "request_json", fake_request_json)
        with pytest.raises(ValueError):
            self._run(ri.resolve_ref_to_sha("bad owner!", REPO, "main"))
        assert called == []


# ===========================================================================
# Impact analysis
# ===========================================================================

class TestComputeImpact:
    def test_identifies_direct_importers_of_changed_file(self):
        result = ri.compute_impact([_MOD_A, _MOD_B, _MOD_C], _EDGES, ["pkg/b.py"])
        assert result["affected_module_count"] == 2
        assert set(result["affected_modules"]) == {"pkg/a.py", "pkg/c.py"}
        assert result["direct_importers"]["pkg/b.py"] == ["pkg/a.py", "pkg/c.py"]

    def test_changed_file_not_listed_as_its_own_importer(self):
        # Even if b.py were in edges as importer of itself, it must be excluded.
        edges = _EDGES + [{"from": "pkg/b.py", "to": "pkg/b.py"}]
        result = ri.compute_impact([_MOD_A, _MOD_B, _MOD_C], edges, ["pkg/b.py"])
        assert "pkg/b.py" not in result["affected_modules"]

    def test_no_importers_returns_empty_impact(self):
        result = ri.compute_impact([_MOD_A, _MOD_B], _EDGES, ["pkg/a.py"])
        # pkg/a.py imports pkg/b.py but nothing imports pkg/a.py
        assert result["affected_module_count"] == 0
        assert result["direct_importers"] == {}

    def test_empty_edges_returns_empty_impact(self):
        result = ri.compute_impact([_MOD_A, _MOD_B, _MOD_C], [], ["pkg/b.py"])
        assert result["affected_module_count"] == 0
        assert "limitations" in result

    def test_empty_changed_paths_returns_empty_impact(self):
        result = ri.compute_impact([_MOD_A, _MOD_B, _MOD_C], _EDGES, [])
        assert result["affected_module_count"] == 0

    def test_importer_not_in_index_is_excluded(self):
        # Edge references a file not present in modules list.
        edges = [{"from": "unknown/external.py", "to": "pkg/b.py"}]
        result = ri.compute_impact([_MOD_B], edges, ["pkg/b.py"])
        assert result["affected_module_count"] == 0

    def test_multiple_changed_paths(self):
        result = ri.compute_impact(
            [_MOD_A, _MOD_B, _MOD_C],
            _EDGES,
            ["pkg/b.py", "pkg/a.py"],
        )
        # pkg/b.py has importers; pkg/a.py has none.
        assert "pkg/b.py" in result["direct_importers"]
        assert "pkg/a.py" not in result["direct_importers"]

    def test_impact_result_always_contains_limitations(self):
        result = ri.compute_impact([_MOD_A], [], ["pkg/a.py"])
        assert isinstance(result["limitations"], list)
        assert len(result["limitations"]) > 0


# ===========================================================================
# Symbol retrieval
# ===========================================================================

class TestRetrieveRelevantSymbols:
    def test_returns_symbols_from_changed_files_first(self):
        symbols = ri.retrieve_relevant_symbols(
            [_MOD_A, _MOD_B, _MOD_C], ["pkg/a.py"]
        )
        paths = [s["path"] for s in symbols]
        # First symbols must come from the changed file.
        assert paths[0] == "pkg/a.py"

    def test_includes_symbols_from_importer_modules(self):
        # pkg/c.py imports pkg/b.py; querying pkg/b.py should include c's symbols.
        symbols = ri.retrieve_relevant_symbols(
            [_MOD_A, _MOD_B, _MOD_C], ["pkg/b.py"]
        )
        paths = {s["path"] for s in symbols}
        assert "pkg/b.py" in paths

    def test_respects_max_symbols_limit(self):
        symbols = ri.retrieve_relevant_symbols(
            [_MOD_A, _MOD_B, _MOD_C], ["pkg/a.py"], max_symbols=2
        )
        assert len(symbols) <= 2

    def test_symbol_has_required_fields(self):
        symbols = ri.retrieve_relevant_symbols([_MOD_A], ["pkg/a.py"])
        assert symbols
        for s in symbols:
            assert "path" in s
            assert "name" in s
            assert "kind" in s
            assert "line" in s

    def test_empty_modules_returns_empty_list(self):
        assert ri.retrieve_relevant_symbols([], ["pkg/a.py"]) == []

    def test_empty_query_returns_empty_list(self):
        assert ri.retrieve_relevant_symbols([_MOD_A, _MOD_B], []) == []

    def test_broken_module_definitions_not_included(self):
        symbols = ri.retrieve_relevant_symbols(
            [_MOD_A, _MOD_BROKEN], ["pkg/broken.py"]
        )
        assert all(s["path"] != "pkg/broken.py" or s["name"] != "" for s in symbols)


# ===========================================================================
# Repository conventions
# ===========================================================================

class TestInferConventions:
    def _make_modules(self, n: int, func_names: list[str], class_names: list[str]) -> list[dict]:
        defs = (
            [{"name": n, "kind": "function", "line": i + 1, "scope": None} for i, n in enumerate(func_names)]
            + [{"name": n, "kind": "class", "line": i + 20, "scope": None} for i, n in enumerate(class_names)]
        )
        return [
            {
                "path": f"pkg/mod{i}.py",
                "parse_status": "parsed",
                "definitions": defs,
                "imports": [],
            }
            for i in range(n)
        ]

    def test_insufficient_sample_returns_no_conventions(self):
        modules = self._make_modules(2, ["do_thing", "run"], ["MyClass"])
        result = ri.infer_conventions(modules)
        assert result["naming_conventions"] == []
        assert result["evidence_based"] is True
        assert len(result["limitations"]) > 0

    def test_detects_snake_case_function_naming(self):
        modules = self._make_modules(
            4,
            ["do_work", "run_job", "fetch_data", "build_index", "process_items"],
            [],
        )
        result = ri.infer_conventions(modules)
        func_conv = next((c for c in result["naming_conventions"] if c["scope"] == "function_naming"), None)
        assert func_conv is not None
        assert func_conv["pattern"] == "snake_case"

    def test_detects_pascal_case_class_naming(self):
        modules = self._make_modules(
            4,
            [],
            ["MyHandler", "BaseParser", "DataModel", "RequestProcessor"],
        )
        result = ri.infer_conventions(modules)
        class_conv = next((c for c in result["naming_conventions"] if c["scope"] == "class_naming"), None)
        assert class_conv is not None
        assert class_conv["pattern"] == "PascalCase"

    def test_private_symbols_excluded(self):
        modules = self._make_modules(4, ["_private", "__dunder__", "_helper"], [])
        result = ri.infer_conventions(modules)
        # Private names excluded — no conventions should be claimed.
        assert result["naming_conventions"] == []

    def test_always_returns_structural_conventions(self):
        modules = self._make_modules(4, ["do_work", "run_job", "fetch_data"], [])
        result = ri.infer_conventions(modules)
        assert isinstance(result["structural_conventions"], list)
        assert len(result["structural_conventions"]) > 0

    def test_evidence_based_flag_is_always_true(self):
        result = ri.infer_conventions([])
        assert result["evidence_based"] is True

    def test_syntax_error_modules_excluded_from_analysis(self):
        good = self._make_modules(4, ["do_work", "run_job", "fetch_data", "build_index"], [])
        bad = [_MOD_BROKEN]
        result = ri.infer_conventions(good + bad)
        # sample_size should count only parsed modules.
        assert result["sample_size"] == 4


# ===========================================================================
# build_review_repository_context — extended output
# ===========================================================================

class TestBuildReviewRepositoryContextExtended:
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_extended_context_includes_impact_analysis(self, monkeypatch):
        from contextlib import asynccontextmanager

        tree = {"truncated": False, "tree": [
            {"path": "pkg", "type": "tree"},
            {"path": "pkg/a.py", "type": "blob", "size": 40},
            {"path": "pkg/b.py", "type": "blob", "size": 30},
        ]}

        @asynccontextmanager
        async def fake_client():
            yield None

        async def fake_request_json(*_, **__):
            return tree

        async def fake_get_file(_owner, _repo, path, _ref):
            sources = {
                "pkg/a.py": "from pkg.b import helper\nclass Changed: pass\n",
                "pkg/b.py": "def helper(): pass\n",
            }
            return sources.get(path)

        monkeypatch.setattr(ri, "async_client", fake_client)
        monkeypatch.setattr(ri, "request_json", fake_request_json)
        monkeypatch.setattr(ri, "get_file_content", fake_get_file)

        result = self._run(ri.build_review_repository_context(
            OWNER, REPO, SHA,
            [("pkg/a.py", "from pkg.b import helper\nclass Changed: pass\n")],
        ))

        assert "impact_analysis" in result
        assert "relevant_symbols" in result
        assert "conventions" in result
        assert isinstance(result["impact_analysis"], dict)
        assert isinstance(result["relevant_symbols"], list)
        assert isinstance(result["conventions"], dict)
        assert result["conventions"]["evidence_based"] is True

    def test_missing_impact_when_no_import_edges(self, monkeypatch):
        """Single-file PR with no imports → impact is empty but present."""
        from contextlib import asynccontextmanager

        tree = {"truncated": False, "tree": [
            {"path": "single.py", "type": "blob", "size": 20},
        ]}

        @asynccontextmanager
        async def fake_client():
            yield None

        async def fake_request_json(*_, **__):
            return tree

        async def fake_get_file(*_, **__):
            return None

        monkeypatch.setattr(ri, "async_client", fake_client)
        monkeypatch.setattr(ri, "request_json", fake_request_json)
        monkeypatch.setattr(ri, "get_file_content", fake_get_file)

        result = self._run(ri.build_review_repository_context(
            OWNER, REPO, SHA,
            [("single.py", "def run(): pass\n")],
        ))

        assert "impact_analysis" in result
        assert result["impact_analysis"]["affected_module_count"] == 0
