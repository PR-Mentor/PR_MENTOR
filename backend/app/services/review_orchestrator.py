"""Coordinate ingestion, static checks, optional AI review, and persistence."""

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from app.ai_review.client import AIReviewUnavailable, is_configured
from app.ai_review.reviewer import generate_candidates
from app.database.reviews import get_store
from app.github_service.files import parse_patch
from app.github_service.parser import InvalidPullRequestURL, parse_pull_request_url
from app.github_service.pull_requests import MAX_SOURCE_BYTES, fetch_pull_request, get_file_content
from app.schemas.finding import Finding, ReviewFinding
from app.schemas.review import AnalyzeRequest, ReviewResponse
from app.verification.bandit_runner import run_bandit
from app.verification.normalizer import normalize_bandit, normalize_ruff
from app.verification.path_validation import PathValidationError
from app.verification.ruff_runner import ToolExecutionError, run_ruff
from app.verification.test_runner import run_tests


async def analyze(request: AnalyzeRequest, user_id: str, github_token: str | None = None) -> ReviewResponse:
    submitted_url = request.pull_request_url or request.url or ""
    owner, repo, number = parse_pull_request_url(submitted_url)
    if request.mode == "demo":
        from app.data.sample_reviews import sample_review

        response = sample_review(owner, repo, number)
        # Keep the teaching content deterministic, while assigning each run a
        # distinct record so repeated demos remain visible in review history.
        response.review_id = uuid4().hex
        response.created_at = datetime.now(timezone.utc)
        if not request.include_learning:
            for finding in response.findings:
                finding.learning_explanation = ""
        await asyncio.to_thread(_persist, response, user_id)
        return response

    started = datetime.now(timezone.utc)
    bundle = await fetch_pull_request(submitted_url, github_token=github_token) if github_token else await fetch_pull_request(submitted_url)
    warnings = list(bundle.warnings)
    diff_files = [parse_patch(item.model_dump()) for item in bundle.files[:100]]
    if len(bundle.files) > 100:
        warnings.append("The displayed diff was capped at 100 files.")

    source_files: list[tuple[str, str]] = []
    for item in bundle.files:
        safe_name = _safe_python_path(item.filename)
        if item.status == "removed" or safe_name is None:
            continue
        if item.size is not None and item.size > MAX_SOURCE_BYTES:
            warnings.append(f"Source analysis skipped oversized file: {safe_name}")
            continue
        if len(source_files) >= 20:
            warnings.append("Static analysis was capped at 20 Python files.")
            break
        try:
            content = await get_file_content(owner, repo, safe_name, bundle.metadata.head_sha, github_token=github_token) if github_token else await get_file_content(owner, repo, safe_name, bundle.metadata.head_sha)
        except Exception:
            content = None
        if content is None:
            warnings.append(f"Source content was unavailable for static analysis: {safe_name}")
            continue
        if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
            warnings.append(f"Source analysis skipped oversized file: {safe_name}")
            continue
        source_files.append((safe_name, content))

    tool_results: list[dict[str, Any]] = [{"tool": "github_rest_api", "status": "completed", "files_received": len(bundle.files), "analyzable_patch_files": sum(bool(file.patch) and file.status != "removed" for file in bundle.files)}]
    normalized_static: list[ReviewFinding] = []
    with tempfile.TemporaryDirectory(prefix="pr-mentor-review-") as temp_name:
        temp_root = Path(temp_name)
        targets = _write_sources(temp_root, source_files)
        for name, runner, normalizer in (("ruff", run_ruff, normalize_ruff), ("bandit", run_bandit, normalize_bandit)):
            if not targets:
                tool_results.append({"tool": name, "status": "skipped", "executed": False, "reason": "No eligible Python source was available."})
                continue
            try:
                raw_results = await asyncio.to_thread(runner, targets, root=temp_root)
                legacy_findings: list[Finding] = normalizer(raw_results)
                rendered = [_static_finding(name, item, temp_root) for item in legacy_findings]
                normalized_static.extend(rendered)
                tool_results.append({"tool": name, "status": "completed", "executed": True, "findings": [item.model_dump(mode="json") for item in rendered]})
            except (ToolExecutionError, PathValidationError, OSError) as exc:
                tool_results.append({"tool": name, "status": "unavailable", "executed": False, "reason": "Analyzer execution failed or was unavailable; local execution details are not exposed."})
                warnings.append(f"{name.title()} was unavailable; no {name.title()} findings are claimed.")

    ai_findings: list[ReviewFinding] = []
    if is_configured():
        try:
            ai_findings = await generate_candidates(bundle.metadata.model_dump(mode="json"), diff_files, [item.model_dump(mode="json") for item in normalized_static])
            tool_results.append({"tool": "ai_review", "status": "completed", "executed": True, "findings": [item.model_dump(mode="json") for item in ai_findings]})
        except AIReviewUnavailable as exc:
            tool_results.append({"tool": "ai_review", "status": "unavailable", "executed": False, "reason": str(exc)})
            warnings.append("AI review was unavailable; no AI findings are claimed.")
    else:
        reason = "LLM_API_KEY and LLM_MODEL are not configured."
        tool_results.append({"tool": "ai_review", "status": "unavailable", "executed": False, "reason": reason})
        warnings.append("AI review is unavailable because no provider is configured.")

    test_result = run_tests()
    tool_results.append({"tool": "tests", **test_result})
    warnings.append(test_result["message"])

    findings = normalized_static + ai_findings
    counts = {status: sum(finding.verification_status == status for finding in findings) for status in ("verified", "needs_review", "unsupported")}
    partial = any(item.get("status") in {"unavailable", "error", "skipped"} for item in tool_results if item.get("tool") != "github_rest_api")
    elapsed = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
    response = ReviewResponse(
        review_id=uuid4().hex, status="partial" if partial else "completed", analysis_mode="live", is_demo=False,
        pull_request=bundle.metadata, summary=f"Retrieved {len(bundle.files)} changed files. Review only the available analysis evidence; submitted code was not executed.",
        findings=findings, verification_summary=counts, tool_results=tool_results, warnings=warnings,
        created_at=datetime.now(timezone.utc), duration_ms=elapsed, diff_files=diff_files,
    )
    if not request.include_learning:
        for finding in response.findings:
            finding.learning_explanation = ""
    await asyncio.to_thread(_persist, response, user_id)
    return response


def _safe_python_path(filename: str) -> str | None:
    normalized = filename.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\x00" in normalized or ":" in path.parts[0] or path.suffix.lower() != ".py":
        return None
    return path.as_posix()


def _write_sources(root: Path, files: list[tuple[str, str]]) -> list[str]:
    targets: list[str] = []
    for filename, content in files:
        safe = _safe_python_path(filename)
        if safe is None:
            continue
        target = root.joinpath(*PurePosixPath(safe).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8", newline="") as handle:
                handle.write(content)
        except FileExistsError:
            continue
        targets.append(safe)
    return targets


def _static_finding(tool: str, item: Finding, root: Path) -> ReviewFinding:
    try:
        candidate = Path(item.file_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        file_path = candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        file_path = ""
    severity = item.severity if item.severity in {"low", "medium", "high", "critical"} else "medium"
    return ReviewFinding(
        id=uuid4().hex, title=f"{tool.title()} {item.rule_id or 'finding'}", description=item.message or "Static analyzer reported a diagnostic.",
        file_path=file_path, line_start=item.line, line_end=item.line, severity=severity,
        category="security" if tool == "bandit" else "code_quality", confidence=0.75,
        source="static_analysis", verification_status="needs_review",
        evidence=[value for value in (f"{tool.title()} {item.rule_id}" if item.rule_id else None, item.evidence, item.message) if value],
        why_it_matters="A static analyzer emitted this diagnostic. It is evidence to review, not proof of exploitability.",
        suggested_fix="Inspect the reported line and confirm the appropriate correction in context.",
        learning_explanation="Static analysis checks source patterns without executing the submitted pull request.",
        limitations=["Analyzer output can include false positives and does not establish runtime impact."],
    )


def _persist(response: ReviewResponse, user_id: str) -> None:
    get_store().save_review(response.model_dump(mode="json"), user_id)
