"""Bounded, snapshot-bound Git tree and Python AST repository intelligence."""

from __future__ import annotations

import ast
import asyncio
import re
from collections import defaultdict
from typing import Any

from app.github_service.client import async_client, request_json
from app.github_service.pull_requests import get_file_content

MAX_TREE_ENTRIES = 5_000
MAX_MANIFEST_OUTPUT = 1_000
MAX_PYTHON_MODULES = 80
MAX_SOURCE_BYTES = 1_000_000
MAX_SYMBOLS_PER_MODULE = 200
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "coverage",
}
_OWNER_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_SHA_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


def _module_name(path: str) -> str:
    name = path.removesuffix(".py")
    name = name.removesuffix("/__init__")
    return name.replace("/", ".")


def _parse_module(path: str, source: str) -> dict[str, Any]:
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return {
            "path": path,
            "parse_status": "syntax_error",
            "error": type(exc).__name__,
            "definitions": [],
            "imports": [],
        }
    definitions: list[dict[str, Any]] = []
    imports: set[str] = set()

    class Visitor(ast.NodeVisitor):
        scope: list[str]

        def __init__(self):
            self.scope = []

        def visit_ClassDef(self, node: ast.ClassDef):
            if len(definitions) < MAX_SYMBOLS_PER_MODULE:
                definitions.append(
                    {
                        "name": node.name,
                        "kind": "class",
                        "line": node.lineno,
                        "scope": ".".join(self.scope) or None,
                    }
                )
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
            if len(definitions) < MAX_SYMBOLS_PER_MODULE:
                definitions.append(
                    {
                        "name": node.name,
                        "kind": "method" if self.scope else "function",
                        "line": node.lineno,
                        "scope": ".".join(self.scope) or None,
                    }
                )
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef):
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
            self._visit_function(node)

        def visit_Import(self, node: ast.Import):
            imports.update(alias.name for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom):
            if node.module:
                base = ("." * node.level) + node.module
                imports.add(base)
                imports.update(
                    base + "." + alias.name for alias in node.names if alias.name != "*"
                )

    Visitor().visit(tree)
    return {
        "path": path,
        "parse_status": "parsed",
        "error": None,
        "definitions": definitions,
        "imports": sorted(imports),
    }


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    visiting: list[str] = []
    active: set[str] = set()
    complete: set[str] = set()
    result: list[list[str]] = []

    def visit(node: str):
        if node in active:
            start = visiting.index(node)
            cycle = visiting[start:] + [node]
            if cycle not in result and len(result) < 50:
                result.append(cycle)
            return
        if node in complete:
            return
        active.add(node)
        visiting.append(node)
        for child in sorted(graph.get(node, set())):
            visit(child)
        visiting.pop()
        active.remove(node)
        complete.add(node)

    for node in sorted(graph):
        visit(node)
    return result


def summarize_python_sources(files: list[tuple[str, str]]) -> dict[str, Any]:
    """Build bounded structural context from already-fetched review snapshots."""
    modules = [_parse_module(path, source) for path, source in files[:20]]
    module_names = {_module_name(item["path"]): item["path"] for item in modules}
    edges: list[dict[str, str]] = []
    for module in modules:
        for imported in module["imports"]:
            target = module_names.get(imported.lstrip("."))
            if target and target != module["path"] and len(edges) < 100:
                edges.append({"from": module["path"], "to": target})
    return {
        "modules": modules,
        "internal_import_edges": edges,
        "limitations": [
            "This context covers only Python files fetched from the changed PR snapshot.",
            "AST relationships do not establish runtime call paths or semantic dependencies.",
        ],
    }


def _parse_dependency_names(path: str, source: str) -> list[str]:
    """Extract bounded package identifiers without forwarding manifest contents."""
    names: set[str] = set()
    if path.endswith("pyproject.toml"):
        try:
            import tomllib

            parsed = tomllib.loads(source[:100_000])
            dependencies = parsed.get("project", {}).get("dependencies", [])
            if isinstance(dependencies, list):
                for entry in dependencies[:100]:
                    if isinstance(entry, str):
                        match = re.match(r"\s*([A-Za-z0-9_.-]+)", entry)
                        if match:
                            names.add(match.group(1).lower())
        except (ValueError, TypeError):
            return []
    elif path.endswith("package.json"):
        import json

        try:
            parsed = json.loads(source[:100_000])
        except (ValueError, TypeError):
            return []
        for section in ("dependencies", "devDependencies"):
            values = parsed.get(section, {}) if isinstance(parsed, dict) else {}
            if isinstance(values, dict):
                names.update(name.lower() for name in list(values)[:100] if isinstance(name, str) and re.fullmatch(r"@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+", name))
    elif path.rsplit("/", 1)[-1].startswith("requirements"):
        for line in source.splitlines()[:200]:
            value = line.split("#", 1)[0].strip()
            match = re.match(r"^([A-Za-z0-9_.-]+)", value)
            if match and not value.startswith(("-", "$")) and not re.match(r"^https?://", value, re.IGNORECASE):
                names.add(match.group(1).lower())
    return sorted(names)[:80]


async def analyze_repository(
    owner: str,
    repository: str,
    ref: str,
    *,
    preloaded_sources: dict[str, str] | None = None,
    priority_paths: list[str] | None = None,
    max_python_modules: int = MAX_PYTHON_MODULES,
    manifest_limit: int = MAX_MANIFEST_OUTPUT,
    include_dependencies: bool = False,
) -> dict[str, Any]:
    if (
        not _OWNER_RE.fullmatch(owner)
        or not _REPO_RE.fullmatch(repository)
        or not _SHA_RE.fullmatch(ref)
    ):
        raise ValueError(
            "Repository identity and snapshot must be canonical, with a full commit SHA."
        )
    async with async_client() as client:
        raw = await request_json(
            client,
            f"/repos/{owner}/{repository}/git/trees/{ref}",
            params={"recursive": "1"},
            use_cache=False,
        )
    if not isinstance(raw, dict) or not isinstance(raw.get("tree"), list):
        raise ValueError("GitHub returned an invalid repository tree.")  # noqa: TRY004 - upstream contract failure.
    entries: list[dict[str, Any]] = []
    python_paths: list[str] = []
    for item in raw["tree"][:MAX_TREE_ENTRIES]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = item["path"]
        parts = path.split("/")
        if (
            len(path) > 1_000
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
            or any(part in EXCLUDED_DIRS for part in parts)
        ):
            continue
        kind = item.get("type")
        size = item.get("size") if isinstance(item.get("size"), int) else None
        if kind not in {"blob", "tree"}:
            continue
        entries.append(
            {
                "path": path,
                "type": "directory" if kind == "tree" else "file",
                "size_bytes": size,
                "extension": ""
                if kind == "tree"
                else (
                    "." + path.rsplit(".", 1)[-1].lower() if "." in parts[-1] else ""
                ),
            }
        )
        if (
            kind == "blob"
            and path.endswith(".py")
            and size is not None
            and size <= MAX_SOURCE_BYTES
        ):
            python_paths.append(path)
    priority = {path: index for index, path in enumerate(priority_paths or [])}
    python_paths.sort(key=lambda path: (path not in priority, priority.get(path, 0), path))
    python_paths = python_paths[: max(1, min(max_python_modules, MAX_PYTHON_MODULES))]
    semaphore = asyncio.Semaphore(8)

    async def fetch(path: str):
        if preloaded_sources and path in preloaded_sources:
            source = preloaded_sources[path]
            if len(source.encode("utf-8", errors="replace")) <= MAX_SOURCE_BYTES:
                return _parse_module(path, source)
            return None
        async with semaphore:
            try:
                fetched_source = await get_file_content(owner, repository, path, ref)
            except Exception:  # noqa: BLE001 - one unavailable source file must not discard other modules.
                return None
        if (
            not isinstance(fetched_source, str)
            or len(fetched_source.encode("utf-8", errors="replace")) > MAX_SOURCE_BYTES
        ):
            return None
        return _parse_module(path, fetched_source)

    modules = [
        result
        for result in await asyncio.gather(*(fetch(path) for path in python_paths))
        if result
    ]
    dependency_context: list[dict[str, Any]] = []
    if include_dependencies:
        dependency_paths = [
            entry["path"] for entry in entries
            if entry["type"] == "file" and entry["path"].rsplit("/", 1)[-1] in {
                "pyproject.toml", "requirements.txt", "package.json",
            }
        ][:3]
        for path in dependency_paths:
            source = (preloaded_sources or {}).get(path)
            if source is None:
                try:
                    source = await get_file_content(owner, repository, path, ref)
                except Exception:  # noqa: BLE001 - optional dependency context must not fail review.
                    source = None
            if isinstance(source, str) and len(source.encode("utf-8", errors="replace")) <= 100_000:
                dependency_context.append({"manifest": path, "packages": _parse_dependency_names(path, source)})
    module_names = {_module_name(item["path"]): item["path"] for item in modules}
    graph: dict[str, set[str]] = defaultdict(set)
    edges: list[dict[str, str]] = []
    for item in modules:
        source_module = item["path"]
        for imported in item["imports"]:
            canonical = imported.lstrip(".")
            target = module_names.get(canonical)
            if target and target != source_module:
                graph[source_module].add(target)
                if len(edges) < 2_000:
                    edges.append({"from": source_module, "to": target})
    truncated = bool(raw.get("truncated")) or len(raw["tree"]) > MAX_TREE_ENTRIES
    return {
        "repository": f"{owner}/{repository}",
        "snapshot_sha": ref.lower(),
        "manifest": entries[:max(1, min(manifest_limit, MAX_MANIFEST_OUTPUT))],
        "manifest_count": len(entries),
        "manifest_truncated": truncated or len(entries) > MAX_MANIFEST_OUTPUT,
        "python_modules": modules,
        "python_files_considered": len(python_paths),
        "internal_import_edges": edges,
        "dependency_context": dependency_context,
        "circular_dependencies": _cycles(graph),
        "limitations": [
            "AST records declarations and import relationships, not runtime call graphs or semantic dependency reachability.",
            "Only bounded Python files from this exact commit snapshot were parsed.",
        ],
    }


async def build_review_repository_context(
    owner: str,
    repository: str,
    ref: str,
    changed_sources: list[tuple[str, str]],
) -> dict[str, Any]:
    """Create bounded full-repository context while reusing PR source fetches."""
    paths = [path for path, _source in changed_sources]
    result = await analyze_repository(
        owner,
        repository,
        ref,
        preloaded_sources=dict(changed_sources),
        priority_paths=paths,
        max_python_modules=12,
        manifest_limit=120,
        include_dependencies=True,
    )
    modules = result["python_modules"]
    compact_modules = [
        {
            "path": item["path"],
            "changed": item["path"] in paths,
            "parse_status": item["parse_status"],
            "definitions": item["definitions"][:15],
            "imports": item["imports"][:15],
        }
        for item in modules[:12]
    ]
    return {
        "repository": result["repository"],
        "snapshot_sha": result["snapshot_sha"],
        "manifest": [item["path"] for item in result["manifest"][:60]],
        "manifest_count": result["manifest_count"],
        "manifest_truncated": result["manifest_truncated"],
        "python_modules": compact_modules[:8],
        "python_files_considered": result["python_files_considered"],
        "internal_import_edges": result["internal_import_edges"][:40],
        "dependency_context": [
            {"manifest": item["manifest"], "packages": item["packages"][:40]}
            for item in result["dependency_context"][:3]
        ],
        "limitations": result["limitations"],
    }
