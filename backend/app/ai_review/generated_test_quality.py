"""Static-only screening of generated pytest candidates; never imports or runs them."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from app.ai_review.generated_test_context import TestGenerationContext
from app.schemas.generated_test import (
    GeneratedTestQualityAssessment,
    GeneratedTestSourceReference,
)

MAX_CANDIDATE_CHARS = 12_000
_DANGEROUS_IMPORTS = {
    "subprocess", "socket", "requests", "httpx", "urllib", "multiprocessing",
    "ctypes", "pty", "telnetlib", "ftplib", "pathlib", "shutil", "os", "sys",
    "tempfile", "aiohttp", "builtins", "pickle", "marshal", "importlib", "runpy",
}
_DANGEROUS_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "system", "popen", "remove",
    "unlink", "rmdir", "mkdir", "makedirs", "rename", "replace", "write_text",
    "write_bytes", "read_text", "read_bytes", "chmod", "chown", "rmtree", "startfile",
    "getattr", "setattr", "globals", "locals", "vars",
}
_NONDETERMINISTIC_CALLS = {"time", "sleep", "random", "uuid4", "now", "today"}


@dataclass(frozen=True)
class CandidateReview:
    accepted: bool
    assessment: GeneratedTestQualityAssessment
    references: tuple[GeneratedTestSourceReference, ...]
    rejection_reason: str | None = None
    digest: str = ""


def review_candidate(payload: dict[str, object], context: TestGenerationContext, *, source_path: str) -> CandidateReview:
    expected_keys = {
        "test_name", "test_code", "category", "description", "rationale",
        "expected_behavior", "source_references",
    }
    if set(payload) != expected_keys:
        return _reject("Candidate fields do not match the generation contract.", repr(payload))
    code = payload.get("test_code")
    name = payload.get("test_name")
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CANDIDATE_CHARS:
        return _reject("Candidate code is missing or exceeds the configured limit.", str(code or ""))
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return _reject("Candidate code is not valid Python syntax.", code, digest)
    if not isinstance(name, str) or not re.fullmatch(r"test_[A-Za-z0-9_]+", name):
        return _reject("Candidate must declare a pytest-style test function name.", code, digest)
    allowed_top_level = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef)
    if any(not isinstance(node, allowed_top_level) for node in tree.body):
        return _reject("Candidate contains top-level executable statements.", code, digest)
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(functions) != 1 or functions[0].name != name or isinstance(functions[0], ast.AsyncFunctionDef):
        return _reject("Candidate must contain exactly the named synchronous test function.", code, digest)
    function = functions[0]
    if function.decorator_list or function.args.defaults or function.args.kw_defaults:
        return _reject("Candidate uses decorators or definition-time default expressions.", code, digest)
    if function.returns is not None or any(
        isinstance(node, ast.arg) and node.annotation is not None for node in ast.walk(function.args)
    ):
        return _reject("Candidate uses annotations that could run while a test module is imported.", code, digest)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            root = (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else ""
            import_names = [alias.name.split(".")[0] for alias in node.names] if isinstance(node, ast.Import) else [root]
            if any(item in _DANGEROUS_IMPORTS for item in import_names):
                return _reject("Candidate imports a prohibited system, network, or filesystem module.", code, digest)
        if isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            if call_name.split(".")[-1] in _DANGEROUS_CALLS:
                return _reject("Candidate uses dynamic execution, shell, or direct file operations.", code, digest)
    assertions = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
    meaningful_assertions = [
        node for node in assertions
        if any(isinstance(part, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)) for part in ast.walk(node.test))
    ]
    raises = any(isinstance(node, ast.Call) and _call_name(node.func) == "pytest.raises" for node in ast.walk(function))
    if not meaningful_assertions and not raises:
        return _reject("Candidate has no meaningful assertion or pytest.raises expectation.", code, digest)

    refs_raw = payload.get("source_references")
    if not isinstance(refs_raw, list) or not refs_raw or len(refs_raw) > 12:
        return _reject("Candidate has no source-grounding references.", code, digest)
    refs: list[GeneratedTestSourceReference] = []
    allowed = {"baseline", "proposed"}
    for item in refs_raw[:12]:
        if not isinstance(item, dict):
            return _reject("Candidate source reference is malformed.", code, digest)
        if set(item) != {"source_version", "line_start", "line_end", "description"}:
            return _reject("Candidate source reference fields do not match the contract.", code, digest)
        version, start, end, description = (item.get(k) for k in ("source_version", "line_start", "line_end", "description"))
        if version not in allowed | {"issue", "fix"} or not isinstance(description, str):
            return _reject("Candidate cites an unsupported source reference.", code, digest)
        if version in allowed:
            if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
                return _reject("Source references must include exact source line bounds.", code, digest)
            available = next((rng for rng in context.source_ranges if rng.source_version == version and rng.file_path == source_path), None)
            if (
                available is None or start > end
                or not set(range(start, end + 1)).issubset(set(available.line_numbers))
            ):
                return _reject("Candidate cites source lines outside the verified prompt context.", code, digest)
            path = source_path
        else:
            start = end = None
            path = source_path
        refs.append(GeneratedTestSourceReference(
            source_version=version, file_path=path, line_start=start, line_end=end,
            description=description[:500],
        ))
    if not refs:
        return _reject("Candidate has no usable provenance references.", code, digest)

    names = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names.update(n.attr.lower() for n in ast.walk(tree) if isinstance(n, ast.Attribute))
    source_snapshot = context.prompt_data.get("source_snapshot")
    if not isinstance(source_snapshot, dict):
        return _reject("Verified proposed source is unavailable for relevance screening.", code, digest)
    source_excerpt = source_snapshot.get("proposed_excerpt")
    if not isinstance(source_excerpt, str):
        return _reject("Verified proposed source is unavailable for relevance screening.", code, digest)
    source_symbols = set(re.findall(r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", source_excerpt))
    source_symbols.update(re.findall(r"\bimport\s+([A-Za-z_][A-Za-z0-9_]*)", source_excerpt))
    if not names.intersection(symbol.lower() for symbol in source_symbols):
        return _reject("Candidate does not reference a symbol present in the verified proposed source.", code, digest)
    relevance: Literal["satisfied", "review_required", "rejected"] = "satisfied"
    non_deterministic = any(
        isinstance(node, ast.Call) and _call_name(node.func).split(".")[-1] in _NONDETERMINISTIC_CALLS
        for node in ast.walk(tree)
    )
    flags = []
    if relevance != "satisfied":
        flags.append("Source relevance requires human review.")
    if non_deterministic:
        flags.append("Candidate appears to depend on time, randomness, or generated identifiers.")
    outcome: Literal["ready_for_review", "review_required", "rejected"] = (
        "review_required" if flags else "ready_for_review"
    )
    assessment = GeneratedTestQualityAssessment(
        relevance=relevance, traceability="satisfied", specificity="review_required",
        assertion_quality="satisfied", independence="review_required",
        completeness="review_required", determinism="review_required" if non_deterministic else "satisfied",
        safety="satisfied", outcome=outcome, findings=flags,
    )
    return CandidateReview(True, assessment, tuple(refs), digest=digest)


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def _reject(reason: str, code: str, digest: str | None = None) -> CandidateReview:
    return CandidateReview(
        False,
        GeneratedTestQualityAssessment(
            relevance="rejected", traceability="rejected", specificity="rejected",
            assertion_quality="rejected", independence="rejected", completeness="rejected",
            determinism="review_required", safety="rejected", outcome="rejected", findings=[reason],
        ),
        (), reason, digest or hashlib.sha256(code.encode("utf-8", errors="replace")).hexdigest(),
    )
