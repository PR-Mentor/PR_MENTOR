import asyncio

import pytest

from app.services import repository_intelligence as intelligence


def test_snapshot_manifest_ast_import_graph_and_cycles_are_bounded(monkeypatch):
    tree = {
        "truncated": False,
        "tree": [
            {"path": "pkg", "type": "tree"},
            {"path": "pkg/a.py", "type": "blob", "size": 40},
            {"path": "pkg/b.py", "type": "blob", "size": 30},
            {"path": "node_modules/ignored.py", "type": "blob", "size": 1},
        ],
    }

    async def request_json(*_args, **kwargs):
        assert kwargs["params"] == {"recursive": "1"}
        return tree

    sources = {
        "pkg/a.py": "from pkg import b\nclass A:\n    def run(self):\n        return 1\n",
        "pkg/b.py": "import pkg.a\ndef helper():\n    return 2\n",
    }

    async def get_file_content(_owner, _repo, path, _ref):
        return sources[path]

    monkeypatch.setattr(intelligence, "request_json", request_json)
    monkeypatch.setattr(intelligence, "get_file_content", get_file_content)
    result = asyncio.run(intelligence.analyze_repository("acme", "repo", "a" * 40))
    assert result["snapshot_sha"] == "a" * 40
    assert [item["path"] for item in result["manifest"]] == [
        "pkg",
        "pkg/a.py",
        "pkg/b.py",
    ]
    assert len(result["python_modules"]) == 2
    assert result["internal_import_edges"]
    assert result["circular_dependencies"]
    assert any(
        item["kind"] == "method"
        for module in result["python_modules"]
        for item in module["definitions"]
    )


def test_invalid_snapshot_is_rejected_before_github_access():
    with pytest.raises(ValueError, match="full commit SHA"):
        asyncio.run(intelligence.analyze_repository("acme", "repo", "main"))


def test_changed_source_structure_is_bounded_and_available_to_review_prompt():
    from app.ai_review.prompts import build_user_prompt

    structure = intelligence.summarize_python_sources([
        ("pkg/a.py", "from pkg.b import helper\nclass A:\n    def run(self): pass\n"),
        ("pkg/b.py", "def helper(): return 1\n"),
        ("pkg/broken.py", "def broken(:\n"),
    ])
    assert len(structure["modules"]) == 3
    assert structure["modules"][2]["parse_status"] == "syntax_error"
    assert structure["internal_import_edges"] == [{"from": "pkg/a.py", "to": "pkg/b.py"}]

    prompt = build_user_prompt({}, [], [], structure)
    assert '"repository_structure"' in prompt
    assert '"name": "helper"' in prompt
    assert "only Python files fetched from the changed PR snapshot" in prompt


def test_review_context_reuses_changed_sources_and_extracts_bounded_dependencies(monkeypatch):
    tree = {"truncated": False, "tree": [
        {"path": "lib", "type": "tree"},
        {"path": "lib/other.py", "type": "blob", "size": 20},
        {"path": "src", "type": "tree"},
        {"path": "src/changed.py", "type": "blob", "size": 30},
        {"path": "requirements.txt", "type": "blob", "size": 35},
    ]}
    fetched = []

    async def request_json(*_args, **_kwargs):
        return tree

    async def get_file_content(_owner, _repo, path, _ref):
        fetched.append(path)
        return {"lib/other.py": "def helper(): return 1", "requirements.txt": "httpx>=0.2\n# note\n-r private.txt"}.get(path)

    monkeypatch.setattr(intelligence, "request_json", request_json)
    monkeypatch.setattr(intelligence, "get_file_content", get_file_content)
    result = asyncio.run(intelligence.build_review_repository_context(
        "acme", "repo", "a" * 40, [("src/changed.py", "from lib.other import helper\nclass Changed: pass")],
    ))
    assert fetched == ["lib/other.py", "requirements.txt"]
    assert result["python_modules"][0]["path"] == "src/changed.py"
    assert result["python_modules"][0]["changed"] is True
    assert result["dependency_context"] == [{"manifest": "requirements.txt", "packages": ["httpx"]}]
    assert result["internal_import_edges"] == [{"from": "src/changed.py", "to": "lib/other.py"}]


def test_dependency_extraction_rejects_malformed_and_remote_requirement_lines():
    assert intelligence._parse_dependency_names("pyproject.toml", "invalid = [") == []
    assert intelligence._parse_dependency_names("requirements.txt", "-e git+https://bad\nhttps://example.test/pkg\n") == []
