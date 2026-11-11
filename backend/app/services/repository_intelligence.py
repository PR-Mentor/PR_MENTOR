"""Bounded, snapshot-bound Git tree and Python AST repository intelligence.

Capabilities
------------
* Repository file/language inventory from the recursive Git tree.
* Python AST analysis: class, function, and method symbol indexing.
* Import/dependency graph generation and circular dependency detection.
* Dependency manifest extraction (pyproject.toml, requirements*.txt, package.json).
* **Ref resolution** — branch/tag references are resolved to a full commit SHA
  before cache lookup so the cache is never stale when a branch advances.
* **Impact analysis** — given a set of changed paths, the reverse import graph
  identifies all repository modules that import those files.
* **Symbol retrieval** — lexical search over indexed definitions lets callers
  retrieve the most relevant functions/classes for a set of changed files.
* **Repository conventions** — evidence-based naming and structural conventions
  derived from the actual module index; never invented from thin samples.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from collections import Counter, defaultdict
from typing import Any

from app.github_service.client import async_client, request_json
from app.github_service.pull_requests import get_file_content

logger = logging.getLogger(__name__)

# Minimum number of modules required before claiming a naming convention.
_MIN_CONVENTION_SAMPLE = 3

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


# ---------------------------------------------------------------------------
# Ref resolution
# ---------------------------------------------------------------------------

_FULL_SHA_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


async def resolve_ref_to_sha(owner: str, repository: str, ref: str) -> str:
    """Resolve a branch name, tag, or abbreviated SHA to a full 40-char commit SHA.

    If ``ref`` is already a full commit SHA it is returned as-is (lowercased)
    without a GitHub API call.  Branch/tag names trigger a lookup against the
    GitHub commits API.  The result is validated against the full-SHA pattern
    so callers can trust that the returned value is usable as a cache key.

    Raises ``ValueError`` for invalid refs or non-SHA responses.
    Raises ``GitHubAPIError`` for GitHub API failures.
    """
    normalised = ref.strip().lower()
    if _FULL_SHA_RE.fullmatch(normalised):
        return normalised  # already a full SHA — no network call needed.

    if not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repository):
        raise ValueError(
            "Repository identity and snapshot must be canonical, with a full commit SHA."
        )
    async with async_client() as client:
        # GitHub's commits endpoint accepts any ref (branch, tag, short SHA).
        result = await request_json(
            client,
            f"/repos/{owner}/{repository}/commits/{ref}",
            use_cache=False,
        )
    if not isinstance(result, dict):
        raise ValueError(f"GitHub returned an unexpected response for ref {ref!r}.")
    sha = result.get("sha")
    if not isinstance(sha, str) or not _FULL_SHA_RE.fullmatch(sha):
        raise ValueError(
            f"GitHub did not return a full commit SHA for ref {ref!r}; got {sha!r}."
        )
    return sha.lower()


# ---------------------------------------------------------------------------
# Impact analysis (reverse dependency graph)
# ---------------------------------------------------------------------------

def compute_impact(
    modules: list[dict[str, Any]],
    edges: list[dict[str, str]],
    changed_paths: list[str],
) -> dict[str, Any]:
    """Return a bounded impact summary for a set of changed file paths.

    The function builds a reverse import graph from ``edges`` (produced by
    ``analyze_repository``) and walks one level to find direct importers of
    each changed file.  Transitive impact is noted in ``limitations`` rather
    than fabricated without reliable evidence.

    Returns a dict with:

    * ``changed_paths`` — the input paths (filtered to those in the index).
    * ``direct_importers`` — {changed_path: [importer_path, …]} (bounded).
    * ``affected_module_count`` — total unique importer count.
    * ``affected_modules`` — sorted list of unique importer paths.
    * ``unchanged_dependents_note`` — human-readable summary.
    * ``limitations`` — caveats about analysis scope.
    """
    if not changed_paths or not edges:
        return _empty_impact(changed_paths, reason="No import edges were available.")

    # Build reverse graph: target → set of files that import it.
    reverse: dict[str, set[str]] = defaultdict(set)
    for edge in edges[:2_000]:
        src = edge.get("from", "")
        tgt = edge.get("to", "")
        if src and tgt:
            reverse[tgt].add(src)

    indexed_paths = {m["path"] for m in modules}
    direct_importers: dict[str, list[str]] = {}
    all_importers: set[str] = set()

    for path in changed_paths[:20]:
        importers = sorted(reverse.get(path, set()) - {path})[:20]
        # Only report importers that are in the analysed module set.
        importers = [p for p in importers if p in indexed_paths]
        if importers:
            direct_importers[path] = importers
            all_importers.update(importers)

    if not direct_importers:
        return _empty_impact(
            changed_paths,
            reason="No indexed modules were found importing the changed files.",
        )

    affected = sorted(all_importers)[:40]
    note = (
        f"{len(affected)} module(s) directly import one or more changed files. "
        "Transitive impact beyond direct importers requires runtime analysis."
    )
    return {
        "changed_paths": changed_paths[:20],
        "direct_importers": direct_importers,
        "affected_module_count": len(affected),
        "affected_modules": affected,
        "unchanged_dependents_note": note,
        "limitations": [
            "Impact covers only modules present in the bounded AST index for this snapshot.",
            "Transitive and runtime dependency reachability is not analysed.",
            "Dynamic imports, decorators, and plugin hooks are not detected.",
        ],
    }


def _empty_impact(changed_paths: list[str], *, reason: str) -> dict[str, Any]:
    return {
        "changed_paths": changed_paths[:20],
        "direct_importers": {},
        "affected_module_count": 0,
        "affected_modules": [],
        "unchanged_dependents_note": reason,
        "limitations": [
            "Impact analysis requires at least one indexed Python module that imports a changed file.",
        ],
    }


# ---------------------------------------------------------------------------
# Symbol / code retrieval
# ---------------------------------------------------------------------------

def retrieve_relevant_symbols(
    modules: list[dict[str, Any]],
    query_paths: list[str],
    *,
    max_symbols: int = 30,
) -> list[dict[str, Any]]:
    """Return a bounded list of symbol definitions relevant to ``query_paths``.

    Priority order:
    1. Symbols defined in the changed files themselves.
    2. Symbols in modules that directly import a changed file.
    3. Symbols in modules imported by a changed file.

    Each returned symbol includes ``path``, ``name``, ``kind``, ``line``,
    and optionally ``scope``.

    Avoids returning entire modules or files; respects ``max_symbols``.
    """
    if not modules or not query_paths:
        return []

    path_set = set(query_paths[:20])
    module_map = {m["path"]: m for m in modules}
    import_map: dict[str, set[str]] = defaultdict(set)
    reverse_map: dict[str, set[str]] = defaultdict(set)

    for m in modules:
        src = m["path"]
        for imp_name in m.get("imports", []):
            # Resolve import name to a module path in the index.
            # Both "pkg.mod" → "pkg/mod.py" and plain name lookups.
            candidate = imp_name.lstrip(".").replace(".", "/") + ".py"
            if candidate in module_map:
                import_map[src].add(candidate)
                reverse_map[candidate].add(src)

    collected: list[dict[str, Any]] = []
    seen_paths: list[str] = []

    def _collect(path: str) -> None:
        if path in seen_paths or path not in module_map:
            return
        seen_paths.append(path)
        m = module_map[path]
        for sym in m.get("definitions", []):
            if len(collected) >= max_symbols:
                return
            collected.append({
                "path": path,
                "name": sym.get("name", ""),
                "kind": sym.get("kind", ""),
                "line": sym.get("line"),
                "scope": sym.get("scope"),
            })

    # Tier 1: changed files.
    for p in query_paths[:20]:
        _collect(p)

    # Tier 2: reverse importers (things that use the changed files).
    for p in query_paths[:20]:
        for importer in sorted(reverse_map.get(p, set())):
            if len(collected) >= max_symbols:
                break
            _collect(importer)

    # Tier 3: direct imports from changed files.
    for p in query_paths[:20]:
        for imported in sorted(import_map.get(p, set())):
            if len(collected) >= max_symbols:
                break
            _collect(imported)

    return collected[:max_symbols]


# ---------------------------------------------------------------------------
# Repository conventions
# ---------------------------------------------------------------------------

def infer_conventions(modules: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive evidence-based naming and structural conventions from the module index.

    Returns only conventions with sufficient supporting evidence
    (>= _MIN_CONVENTION_SAMPLE modules).  Never invents conventions from
    a thin sample.

    Returns a dict with:

    * ``naming_conventions`` — list of observed naming patterns with counts.
    * ``structural_conventions`` — layout observations (package depth, init files, etc.).
    * ``sample_size`` — number of parsed modules analysed.
    * ``evidence_based`` — always True; indicates claims are derived from data.
    * ``limitations`` — caveats about scope.
    """
    parsed = [m for m in modules if m.get("parse_status") == "parsed"]
    if len(parsed) < _MIN_CONVENTION_SAMPLE:
        return {
            "naming_conventions": [],
            "structural_conventions": [],
            "sample_size": len(parsed),
            "evidence_based": True,
            "limitations": [
                f"Fewer than {_MIN_CONVENTION_SAMPLE} parseable modules were available; "
                "conventions cannot be inferred reliably from this sample.",
            ],
        }

    # --- Function/method naming style ---
    func_styles: Counter = Counter()
    class_styles: Counter = Counter()
    func_names: list[str] = []
    class_names: list[str] = []

    for m in parsed:
        for sym in m.get("definitions", []):
            name = sym.get("name", "")
            if not name or name.startswith("_"):
                continue
            kind = sym.get("kind", "")
            if kind in ("function", "method"):
                func_names.append(name)
                if re.match(r"^[a-z][a-z0-9_]*$", name):
                    func_styles["snake_case"] += 1
                elif re.match(r"^[a-z][a-zA-Z0-9]*$", name):
                    func_styles["camelCase"] += 1
            elif kind == "class":
                class_names.append(name)
                if re.match(r"^[A-Z][a-zA-Z0-9]*$", name):
                    class_styles["PascalCase"] += 1
                elif re.match(r"^[A-Z][A-Z0-9_]*$", name):
                    class_styles["UPPER_SNAKE"] += 1

    # --- Module layout ---
    has_init = any(m["path"].endswith("/__init__.py") for m in parsed)
    depths = [len(m["path"].split("/")) - 1 for m in parsed]
    avg_depth = sum(depths) / len(depths) if depths else 0

    naming_conventions: list[dict[str, Any]] = []
    if func_styles and sum(func_styles.values()) >= _MIN_CONVENTION_SAMPLE:
        dominant, count = func_styles.most_common(1)[0]
        naming_conventions.append({
            "scope": "function_naming",
            "pattern": dominant,
            "observation_count": count,
            "total_sampled": len(func_names),
        })
    if class_styles and sum(class_styles.values()) >= _MIN_CONVENTION_SAMPLE:
        dominant, count = class_styles.most_common(1)[0]
        naming_conventions.append({
            "scope": "class_naming",
            "pattern": dominant,
            "observation_count": count,
            "total_sampled": len(class_names),
        })

    structural_conventions: list[dict[str, Any]] = [
        {
            "observation": "uses_package_init_files",
            "value": has_init,
            "detail": "At least one __init__.py was found in the indexed modules.",
        },
        {
            "observation": "average_module_nesting_depth",
            "value": round(avg_depth, 1),
            "detail": f"Average directory depth across {len(parsed)} parsed modules.",
        },
    ]

    return {
        "naming_conventions": naming_conventions,
        "structural_conventions": structural_conventions,
        "sample_size": len(parsed),
        "evidence_based": True,
        "limitations": [
            "Conventions are derived from the bounded AST index for this snapshot only.",
            "Private (underscore-prefixed) symbols are excluded from naming analysis.",
            "Only Python modules are analysed; JS/TS, YAML, and other files are not covered.",
        ],
    }


async def build_review_repository_context(
    owner: str,
    repository: str,
    ref: str,
    changed_sources: list[tuple[str, str]],
) -> dict[str, Any]:
    """Create bounded full-repository context while reusing PR source fetches.

    Extends the base analysis with:
    * Impact analysis (reverse import graph for changed files).
    * Relevant symbol retrieval (bounded definitions for changed + related files).
    * Evidence-based repository conventions.
    """
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
    edges = result["internal_import_edges"]

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

    # Impact analysis — which indexed modules import the changed files.
    impact = compute_impact(modules, edges, paths)

    # Relevant symbols — bounded definitions for changed + directly related modules.
    relevant_symbols = retrieve_relevant_symbols(modules, paths, max_symbols=25)

    # Conventions — evidence-based naming and structural patterns.
    conventions = infer_conventions(modules)

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
        "impact_analysis": impact,
        "relevant_symbols": relevant_symbols,
        "conventions": conventions,
        "limitations": result["limitations"],
    }


async def get_or_analyze_repository(
    owner: str,
    repository: str,
    ref: str,
) -> dict[str, Any]:
    """Return a commit-keyed cached intelligence result, or run ``analyze_repository``.

    Read-through / write-through contract
    --------------------------------------
    1. Check the SQLite intelligence index for a hit on ``(owner, repository, ref)``.
    2. On a cache hit return the stored result immediately — no GitHub calls.
    3. On a cache miss, run ``analyze_repository`` with the full parameter set.
    4. If analysis succeeds, persist the result asynchronously (best-effort
       in a thread so the event loop is not blocked by SQLite I/O).
    5. If the cache layer itself fails at any step the error is logged and
       suppressed; the caller always receives a valid result or a propagated
       ``analyze_repository`` exception.

    The function validates the same constraints as ``analyze_repository``
    (owner/repo names and full SHA) before touching the cache, so callers can
    rely on ``ValueError`` for bad identifiers regardless of cache state.
    """
    # Validate identity before any I/O — consistent with analyze_repository.
    if (
        not _OWNER_RE.fullmatch(owner)
        or not _REPO_RE.fullmatch(repository)
        or not _SHA_RE.fullmatch(ref)
    ):
        raise ValueError(
            "Repository identity and snapshot must be canonical, with a full commit SHA."
        )

    # --- Cache read (runs in a thread to avoid blocking the event loop) -------
    try:
        loop = asyncio.get_running_loop()
        cached = await loop.run_in_executor(
            None,
            lambda: _cache_get(owner, repository, ref),
        )
        if cached is not None:
            logger.debug(
                "repository_intelligence_index: cache hit for %s/%s@%s",
                owner,
                repository,
                ref,
            )
            return cached
    except Exception:  # noqa: BLE001 — cache read failure must not block analysis.
        logger.exception(
            "repository_intelligence_index: cache read raised unexpectedly for %s/%s@%s",
            owner,
            repository,
            ref,
        )

    logger.debug(
        "repository_intelligence_index: cache miss for %s/%s@%s — running analysis",
        owner,
        repository,
        ref,
    )

    # --- Live analysis --------------------------------------------------------
    result = await analyze_repository(owner, repository, ref)

    # --- Cache write (best-effort, failure must not raise) --------------------
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _cache_put(owner, repository, ref, result),
        )
    except Exception:  # noqa: BLE001 — cache write failure must not suppress result.
        logger.exception(
            "repository_intelligence_index: cache write raised unexpectedly for %s/%s@%s",
            owner,
            repository,
            ref,
        )

    return result


# ---------------------------------------------------------------------------
# Private helpers that isolate the import of the database module so that the
# import-time side-effects (table DDL) only run when the cache is actually
# used.  This keeps unit tests that do not need a database fast and clean.
# ---------------------------------------------------------------------------

def _cache_get(owner: str, repository: str, ref: str) -> dict[str, Any] | None:
    from app.database.intelligence_index import get_cached  # local import — avoid circular deps
    return get_cached(owner, repository, ref)


def _cache_put(owner: str, repository: str, ref: str, data: dict[str, Any]) -> None:
    from app.database.intelligence_index import put_cached  # local import — avoid circular deps
    put_cached(owner, repository, ref, data)
