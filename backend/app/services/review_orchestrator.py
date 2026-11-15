"""Coordinate ingestion, static checks, optional AI review, and persistence."""

import asyncio
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from app.ai_review.client import AIReviewUnavailable, is_configured, last_call_metadata
from app.ai_review.fix_generation import (
    FixGenerationResult,
    IssueFixOutcome,
    generate_suggested_fixes,
)
from app.ai_review.issue_explanations import (
    ExplanationGenerationResult,
    generate_issue_explanations,
)
from app.ai_review.reviewer import generate_candidates
from app.database.reviews import get_store
from app.github_service.files import parse_patch
from app.github_service.parser import parse_pull_request_url
from app.github_service.pull_requests import (
    MAX_SOURCE_BYTES,
    fetch_pull_request,
    get_file_content,
)
from app.schemas.finding import Finding, ReviewFinding
from app.schemas.issue import CanonicalIssue, FindingObservation
from app.schemas.review import AnalyzeRequest, ReviewResponse
from app.services.finding_reconciliation import reconcile_observations
from app.services.fix_analysis import analyze_fix_relationships
from app.services.fix_freshness import verify_fix_freshness
from app.services.repository_intelligence import (
    build_review_repository_context,
    summarize_python_sources,
)
from app.verification.bandit_runner import run_bandit
from app.verification.normalizer import normalize_bandit, normalize_ruff
from app.verification.path_validation import PathValidationError
from app.verification.ruff_runner import ToolExecutionError, run_ruff
from app.verification.test_runner import run_tests


MAX_SOURCE_FETCH_ATTEMPTS = 20
SOURCE_FETCH_CONCURRENCY = 5


async def analyze(
    request: AnalyzeRequest,
    user_id: str | None = None,
    github_token: str | None = None,
    *,
    owner_id: str | None = None,
) -> ReviewResponse:
    effective_id = user_id or owner_id
    if request.workspace_id:
        from app.database import workspaces

        if effective_id is None or workspaces.get_role(request.workspace_id, effective_id) not in {
            "owner", "admin", "editor"
        }:
            raise ValueError("Workspace membership no longer permits this review job.")
    submitted_url = request.pull_request_url or request.url or ""
    owner, repo, number = parse_pull_request_url(submitted_url)
    if request.mode == "demo":
        from app.data.sample_reviews import sample_review

        response = sample_review(owner, repo, number)
        # Keep the teaching content deterministic, while assigning each run a
        # distinct record so repeated demos remain visible in review history.
        response.review_id = uuid4().hex
        response.created_at = datetime.now(UTC)
        if not request.include_learning:
            for finding in response.findings:
                finding.learning_explanation = ""
        _populate_reconciliation(response, [
            FindingObservation(original_finding=finding, tool_name="demo_dataset")
            for finding in response.findings
        ])
        response.tool_results.append({
            "tool": "issue_explanations", "status": "not_requested", "executed": False,
            "reason": "Demo reviews use curated educational content; canonical issue explanations are generated for live reviews.",
        })
        response.tool_results.append({
            "tool": "fix_generation",
            "status": "unavailable" if request.include_suggested_fixes else "not_requested",
            "executed": False,
            "requested": request.include_suggested_fixes,
            "issue_count": len(response.canonical_issues), "generated_issue_count": 0,
            "outcome_counts": {}, "per_issue_outcomes": [],
            "reason": (
                "Fix generation is available only for live reviews with a captured PR source snapshot."
                if request.include_suggested_fixes else None
            ),
        })
        await asyncio.to_thread(_persist, response, effective_id)
        return response

    started = datetime.now(UTC)
    bundle = (
        await fetch_pull_request(submitted_url, github_token=github_token)
        if github_token
        else await fetch_pull_request(submitted_url)
    )
    warnings = list(bundle.warnings)
    diff_files = [parse_patch(item.model_dump()) for item in bundle.files[:100]]
    if len(bundle.files) > 100:
        warnings.append("The displayed diff was capped at 100 files.")

    source_files: list[tuple[str, str]] = []
    head_sha = bundle.metadata.head_sha
    if not isinstance(head_sha, str) or re.fullmatch(
        r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", head_sha
    ) is None:
        warnings.append("Source analysis was skipped because the pull request head snapshot identity was unavailable or invalid.")
    else:
        source_candidates: list[str] = []
        for item in bundle.files:
            safe_name = _safe_python_path(item.filename)
            if item.status == "removed" or safe_name is None:
                continue
            if item.size is not None and item.size > MAX_SOURCE_BYTES:
                warnings.append(f"Source analysis skipped oversized file: {safe_name}")
                continue
            if len(source_candidates) >= MAX_SOURCE_FETCH_ATTEMPTS:
                warnings.append(
                    f"Source retrieval was capped at {MAX_SOURCE_FETCH_ATTEMPTS} Python files."
                )
                break
            source_candidates.append(safe_name)

        semaphore = asyncio.Semaphore(SOURCE_FETCH_CONCURRENCY)

        async def fetch_source(safe_name: str) -> tuple[str, str | None]:
            async with semaphore:
                try:
                    if github_token:
                        content = await get_file_content(
                            owner, repo, safe_name, head_sha, github_token=github_token
                        )
                    else:
                        content = await get_file_content(owner, repo, safe_name, head_sha)
                except Exception:  # noqa: BLE001 - Missing source context is isolated to that file.
                    content = None
                return safe_name, content

        # Gather preserves candidate order while the semaphore bounds in-flight
        # GitHub requests. A failed fetch no longer triggers serial retries over
        # every remaining changed file.
        fetched_sources = await asyncio.gather(
            *(fetch_source(name) for name in source_candidates)
        )
        for safe_name, content in fetched_sources:
            if content is None:
                warnings.append(f"Source content was unavailable for static analysis: {safe_name}")
                continue
            if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
                warnings.append(f"Source analysis skipped oversized file: {safe_name}")
                continue
            source_files.append((safe_name, content))

    tool_results: list[dict[str, Any]] = [{"tool": "github_rest_api", "status": "completed", "files_received": len(bundle.files), "analyzable_patch_files": sum(bool(file.patch) and file.status != "removed" for file in bundle.files)}]
    normalized_static: list[ReviewFinding] = []
    static_observations: list[FindingObservation] = []
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
                static_observations.extend(
                    FindingObservation(original_finding=finding, tool_name=name, rule_id=raw.rule_id)
                    for raw, finding in zip(legacy_findings, rendered)
                )
                tool_results.append({"tool": name, "status": "completed", "executed": True, "findings": [item.model_dump(mode="json") for item in rendered]})
            except (ToolExecutionError, PathValidationError, OSError):
                tool_results.append({"tool": name, "status": "unavailable", "executed": False, "reason": "Analyzer execution failed or was unavailable; local execution details are not exposed."})
                warnings.append(f"{name.title()} was unavailable; no {name.title()} findings are claimed.")

    ai_findings: list[ReviewFinding] = []
    if is_configured():
        try:
            try:
                if not isinstance(head_sha, str):
                    raise TypeError("Pull request source identity is unavailable.")
                repository_structure = await build_review_repository_context(owner, repo, head_sha, source_files)
            except Exception:  # noqa: BLE001 - full-repository context is optional; changed-file context remains available.
                repository_structure = summarize_python_sources(source_files)
                warnings.append("Full repository context was unavailable; AI review used changed-file structure only.")
            prompt_metadata = bundle.metadata.model_dump(mode="json")
            prompt_metadata["repository_structure"] = repository_structure
            ai_findings = await generate_candidates(
                prompt_metadata,
                diff_files,
                [item.model_dump(mode="json") for item in normalized_static],
            )
            ai_findings, ai_rejections = _ground_ai_findings(ai_findings, diff_files)
            provider_metadata = last_call_metadata() or {}
            tool_results.append({
                "tool": "ai_review", "status": "completed", "executed": True,
                "findings": [item.model_dump(mode="json") for item in ai_findings],
                "returned_finding_count": len(ai_findings) + sum(ai_rejections.values()),
                "rejected_finding_count": sum(ai_rejections.values()),
                "rejected_finding_reasons": ai_rejections,
                "provider_name": provider_metadata.get("provider"),
                "model_name": provider_metadata.get("model"),
                "fallback_used": provider_metadata.get("fallback_used", False),
                "attempt_count": provider_metadata.get("attempt_count"),
                "latency_ms": provider_metadata.get("latency_ms"),
                "usage": provider_metadata.get("usage") or None,
                "model_configuration": {
                    **(provider_metadata.get("model_configuration") or {}),
                    "fallback_used": provider_metadata.get("fallback_used", False),
                    "providers_attempted": provider_metadata.get("providers_attempted", []),
                    "prompt_version": provider_metadata.get("prompt_version"),
                    "system_prompt_sha256": provider_metadata.get("system_prompt_sha256"),
                    "user_prompt_sha256": provider_metadata.get("user_prompt_sha256"),
                },
                "prompt_version": provider_metadata.get("prompt_version"),
                "system_prompt_sha256": provider_metadata.get("system_prompt_sha256"),
                "user_prompt_sha256": provider_metadata.get("user_prompt_sha256"),
            })
            if ai_rejections:
                warnings.append(
                    "AI findings that could not be grounded in changed PR code were omitted."
                )
        except AIReviewUnavailable as exc:
            provider_metadata = last_call_metadata() or {}
            tool_results.append({
                "tool": "ai_review", "status": "unavailable", "executed": False,
                "reason": str(exc), "providers_attempted": provider_metadata.get("providers_attempted", []),
                "attempt_count": provider_metadata.get("attempt_count", 0),
            })
            warnings.append("AI review was unavailable; no AI findings are claimed.")
    else:
        reason = "LLM_API_KEY and LLM_MODEL are not configured."
        tool_results.append({"tool": "ai_review", "status": "unavailable", "executed": False, "reason": reason})
        warnings.append("AI review is unavailable because no provider is configured.")

    test_result = run_tests()
    tool_results.append({"tool": "tests", **test_result})
    warnings.append(test_result["message"])

    findings = normalized_static + ai_findings
    if not request.include_learning:
        for finding in findings:
            finding.learning_explanation = ""
    observations = static_observations + [
        FindingObservation(original_finding=finding, tool_name="ai_review", provider_name="openai_compatible")
        for finding in ai_findings
    ]
    reconciliation = reconcile_observations(observations)
    explanation_result = ExplanationGenerationResult()
    explanation_tool: dict[str, Any]
    if not reconciliation.issues:
        explanation_tool = {
            "tool": "issue_explanations", "status": "not_needed", "executed": False,
            "issue_count": 0, "explained_issue_count": 0,
        }
    elif not request.include_learning:
        explanation_tool = {
            "tool": "issue_explanations", "status": "not_requested", "executed": False,
            "issue_count": len(reconciliation.issues), "explained_issue_count": 0,
            "reason": "Issue explanations were not requested for this review.",
        }
    elif not is_configured():
        explanation_tool = {
            "tool": "issue_explanations", "status": "unavailable", "executed": False,
            "issue_count": len(reconciliation.issues), "explained_issue_count": 0,
            "reason": "LLM_API_KEY and LLM_MODEL are not configured.",
        }
        warnings.append("AI issue explanations were unavailable; canonical issues are preserved without explanations.")
    else:
        try:
            explanation_result = await generate_issue_explanations(
                reconciliation.issues,
                source_files=source_files,
                diff_files=diff_files,
                explanation_level=_user_explanation_level(effective_id),
            )
            for issue in reconciliation.issues:
                explanation = explanation_result.explanations.get(issue.issue_id)
                if explanation is not None:
                    issue.explanation = explanation
            explained_count = len(explanation_result.explanations)
            has_failures = bool(
                explanation_result.failed_batch_count
                or explanation_result.rejected_explanation_count
                or explanation_result.context_failure_count
                or explained_count < len(reconciliation.issues)
            )
            explanation_status = "completed" if not has_failures else "partial" if explained_count else "unavailable"
            explanation_tool = {
                "tool": "issue_explanations", "status": explanation_status, "executed": True,
                "issue_count": len(reconciliation.issues), "explained_issue_count": explained_count,
                "batch_count": explanation_result.batch_count,
                "failed_batch_count": explanation_result.failed_batch_count,
                "rejected_explanation_count": explanation_result.rejected_explanation_count,
                "context_failure_count": explanation_result.context_failure_count,
                "provider_calls": explanation_result.provider_calls,
            }
            if has_failures:
                explanation_tool["reason"] = (
                    "Some canonical issues did not receive a valid explanation."
                    if explained_count
                    else "No valid explanations were returned for this review."
                )
                warnings.append("AI issue explanations were unavailable for some canonical issues; valid explanations and all findings were preserved.")
        except Exception:  # noqa: BLE001 - Optional explanations must not discard completed findings.
            # Explanation generation is optional and must not discard a valid
            # review. Keep details out of the response and retain every issue.
            explanation_tool = {
                "tool": "issue_explanations", "status": "unavailable", "executed": True,
                "issue_count": len(reconciliation.issues), "explained_issue_count": 0,
                "reason": "The explanation service failed; canonical issues remain available.",
            }
            warnings.append("AI issue explanations were unavailable; canonical issues are preserved without explanations.")
    tool_results.append(explanation_tool)
    fix_generation_tool = await _run_fix_generation(
        request,
        bundle.metadata.repository_owner,
        bundle.metadata.repository_name,
        bundle.metadata.head_sha,
        reconciliation.issues,
        diff_files,
    )
    tool_results.append(fix_generation_tool)
    if request.include_suggested_fixes:
        fix_analysis_tool = analyze_fix_relationships(reconciliation.issues)
        conflict_status = fix_analysis_tool["status"]
        freshness_result = await _run_fix_freshness(
            bundle.metadata.repository_owner,
            bundle.metadata.repository_name,
            bundle.metadata.pull_request_number,
            reconciliation.issues,
        )
        fix_analysis_tool["conflict_status"] = conflict_status
        fix_analysis_tool["freshness_status"] = freshness_result["status"]
        fix_analysis_tool["freshness"] = freshness_result
        component_statuses = (conflict_status, freshness_result["status"])
        if all(status == "not_needed" for status in component_statuses):
            fix_analysis_tool["status"] = "not_needed"
        elif any(status in {"partial", "unavailable", "failed"} for status in component_statuses):
            fix_analysis_tool["status"] = "partial"
        else:
            fix_analysis_tool["status"] = "completed"
    else:
        fix_analysis_tool = {
            "tool": "fix_analysis", "status": "not_requested", "executed": False,
            "proposal_count": 0, "analyzed_proposal_count": 0, "skipped_proposal_count": 0,
            "pair_count": 0, "relationship_counts": {}, "relationships": [],
            "conflict_status": "not_requested", "freshness_status": "not_requested",
            "freshness": {"status": "not_requested", "proposal_count": 0, "per_proposal": []},
        }
    tool_results.append(fix_analysis_tool)
    counts = {status: sum(finding.verification_status == status for finding in findings) for status in ("verified", "needs_review", "unsupported")}
    partial = any(item.get("status") in {"unavailable", "error", "skipped", "partial"} for item in tool_results if item.get("tool") not in {"github_rest_api", "issue_explanations", "fix_generation", "fix_analysis"})
    partial = partial or explanation_tool.get("status") in {"unavailable", "partial"}
    elapsed = max(0, int((datetime.now(UTC) - started).total_seconds() * 1000))
    ai_review_result = next((item for item in tool_results if item.get("tool") == "ai_review" and item.get("status") == "completed" and item.get("executed") is True), None)
    response = ReviewResponse(
        review_id=uuid4().hex, workspace_id=request.workspace_id,
        status="partial" if partial else "completed", analysis_mode="live", is_demo=False,
        model_provider=ai_review_result.get("provider_name") if ai_review_result else None,
        model_name=ai_review_result.get("model_name") if ai_review_result else None,
        model_usage=ai_review_result.get("usage") if ai_review_result else None,
        model_fallback_used=ai_review_result.get("fallback_used") if ai_review_result else None,
        model_latency_ms=ai_review_result.get("latency_ms") if ai_review_result else None,
        model_configuration=ai_review_result.get("model_configuration") if ai_review_result else None,
        pull_request=bundle.metadata, summary=f"Retrieved {len(bundle.files)} changed files. Review only the available analysis evidence; submitted code was not executed.",
        findings=findings, canonical_issues=reconciliation.issues,
        unique_issue_count=reconciliation.unique_issue_count,
        raw_observation_count=reconciliation.observation_count,
        observation_counts_by_source=_observation_source_counts(observations),
        issue_counts_by_severity=dict(Counter(issue.severity for issue in reconciliation.issues)),
        issue_counts_by_category=dict(Counter(issue.category for issue in reconciliation.issues)),
        verification_summary=counts, tool_results=tool_results, warnings=warnings,
        created_at=datetime.now(UTC), duration_ms=elapsed, diff_files=diff_files,
    )
    await asyncio.to_thread(_persist, response, effective_id)
    return response


def _safe_python_path(filename: str) -> str | None:
    normalized = filename.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.parts or "\x00" in normalized or ":" in path.parts[0] or path.suffix.lower() != ".py":
        return None
    return path.as_posix()


def _ground_ai_findings(
    findings: list[ReviewFinding],
    diff_files: list[dict[str, Any]],
) -> tuple[list[ReviewFinding], dict[str, int]]:
    """Keep AI findings only when their path, location, and code evidence map to the PR diff."""
    added_lines_by_path: dict[str, dict[int, str]] = {}
    changed_lines_by_path: dict[str, set[int]] = {}
    for diff_file in diff_files:
        raw_path = diff_file.get("path")
        if not isinstance(raw_path, str):
            continue
        path = _normalize_review_path(raw_path)
        if path is None:
            continue
        added_lines: dict[int, str] = {}
        changed_lines: set[int] = set()
        for hunk in diff_file.get("hunks", []):
            if not isinstance(hunk, dict):
                continue
            for line in hunk.get("lines", []):
                if not isinstance(line, dict):
                    continue
                number = line.get("line")
                if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                    continue
                kind = line.get("kind")
                if kind in {"added", "context"}:
                    changed_lines.add(number)
                if kind == "added" and isinstance(line.get("text"), str):
                    added_lines[number] = line["text"]
        added_lines_by_path[path] = added_lines
        changed_lines_by_path[path] = changed_lines

    accepted: list[ReviewFinding] = []
    rejected: Counter[str] = Counter()
    for finding in findings:
        path = _normalize_review_path(finding.file_path)
        if path is None or path not in added_lines_by_path:
            rejected["path_not_in_diff"] += 1
            continue

        added_lines = added_lines_by_path[path]
        changed_lines = changed_lines_by_path[path]
        if finding.line_start is None and finding.line_end is None:
            candidate_lines = list(added_lines.values())
        else:
            start = finding.line_start if finding.line_start is not None else finding.line_end
            end = finding.line_end if finding.line_end is not None else finding.line_start
            if (
                not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or start < 1 or end < start
                or start not in changed_lines or end not in changed_lines
            ):
                rejected["line_not_in_diff"] += 1
                continue
            candidate_lines = [text for number, text in added_lines.items() if start <= number <= end]

        evidence_matches = any(
            _code_evidence_matches(evidence, candidate_lines)
            for evidence in finding.evidence
            if isinstance(evidence, str)
        )
        if not evidence_matches:
            rejected["code_evidence_not_in_changed_lines"] += 1
            continue
        accepted.append(finding)

    return accepted, dict(rejected)


def _normalize_review_path(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized.strip() or path.is_absolute() or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
        or ":" in path.parts[0] or path.as_posix() != normalized
    ):
        return None
    return path.as_posix()


def _code_evidence_matches(evidence: str, changed_lines: list[str]) -> bool:
    """Match a short literal code excerpt after ignoring whitespace and quote wrappers."""
    excerpt = "".join(evidence.strip().strip("`\"'").split())
    if len(excerpt) < 4:
        return False
    return any(excerpt in "".join(line.split()) for line in changed_lines)


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
    is_security_rule = tool == "bandit" or bool(item.rule_id and item.rule_id.upper().startswith("S"))
    return ReviewFinding(
        id=uuid4().hex, title=f"{tool.title()} {item.rule_id or 'finding'}", description=item.message or "Static analyzer reported a diagnostic.",
        file_path=file_path, line_start=item.line, line_end=item.line, severity=severity,
        category="security" if is_security_rule else "code_quality", confidence=0.75,
        source="static_analysis", verification_status="needs_review",
        evidence=[value for value in (f"{tool.title()} {item.rule_id}" if item.rule_id else None, item.evidence, item.message) if value],
        why_it_matters="A static analyzer emitted this diagnostic. It is evidence to review, not proof of exploitability.",
        suggested_fix="Inspect the reported line and confirm the appropriate correction in context.",
        learning_explanation="Static analysis checks source patterns without executing the submitted pull request.",
        limitations=["Analyzer output can include false positives and does not establish runtime impact."],
    )


def _persist(response: ReviewResponse, user_id: str | None = None, *, owner_id: str | None = None) -> None:
    effective_id = user_id or owner_id
    get_store().save_review(response.model_dump(mode="json"), effective_id)
    if response.workspace_id and effective_id:
        from app.database import workspaces

        if workspaces.get_role(response.workspace_id, effective_id) in {"owner", "admin", "editor"}:
            workspaces.link_review(response.workspace_id, response.review_id, effective_id)


def _populate_reconciliation(response: ReviewResponse, observations: list[FindingObservation]) -> None:
    """Attach canonical issues while leaving the legacy findings list intact."""
    result = reconcile_observations(observations)
    response.canonical_issues = result.issues
    response.unique_issue_count = result.unique_issue_count
    response.raw_observation_count = result.observation_count
    response.observation_counts_by_source = _observation_source_counts(observations)
    response.issue_counts_by_severity = dict(Counter(issue.severity for issue in result.issues))
    response.issue_counts_by_category = dict(Counter(issue.category for issue in result.issues))


def _observation_source_counts(observations: list[FindingObservation]) -> dict[str, int]:
    return dict(Counter(item.original_finding.source for item in observations))


def _user_explanation_level(user_id: str | None) -> str:
    """Read the user's saved explanation preference, defaulting safely to beginner."""
    if not user_id:
        return "beginner"
    try:
        from app.database.users import get_user

        user = get_user(user_id)
    except Exception:  # noqa: BLE001 - Personalization must not block a review.
        return "beginner"
    preferences = user.get("preferences") if isinstance(user, dict) else None
    level = preferences.get("explanation_level") if isinstance(preferences, dict) else None
    return level if level in {"beginner", "intermediate", "advanced"} else "beginner"


async def _run_fix_freshness(
    owner: str,
    repo: str,
    pull_request_number: int,
    issues: list[CanonicalIssue],
) -> dict[str, Any]:
    """Keep freshness verification best effort and preserve all proposals."""
    try:
        return await verify_fix_freshness(
            issues,
            owner=owner,
            repo=repo,
            pull_request_number=pull_request_number,
        )
    except Exception:  # noqa: BLE001 - Stale/unavailable fixes must not invalidate the fetched review.
        # Unexpected verification failures are reported without provider
        # details and never invalidate the review or mutate proposal content.
        outcomes = [
            {
                "canonical_issue_id": issue.issue_id,
                "fix_id": fix.fix_id,
                "status": "unverifiable",
                "reason": "Freshness verification failed; the proposal was preserved.",
            }
            for issue in issues
            for fix in issue.suggested_fixes
        ]
        return {
            "status": "unavailable" if outcomes else "not_needed",
            "proposal_count": len(outcomes),
            "current_count": 0,
            "stale_count": 0,
            "unverifiable_count": len(outcomes),
            "outcome_counts": {"unverifiable": len(outcomes)} if outcomes else {},
            "checked_head_sha": None,
            "file_fetch_count": 0,
            "per_proposal": outcomes,
        }


async def _run_fix_generation(
    request: AnalyzeRequest,
    owner: str,
    repo: str,
    head_sha: str | None,
    issues: list[CanonicalIssue],
    diff_files: list[dict[str, Any]],
) -> dict[str, Any]:
    """Best-effort, sequential fix generation for canonical issues only.

    Cancellation intentionally propagates: ``CancelledError`` is not an
    ``Exception`` on supported Python versions, so it is never converted into
    a successful-looking review outcome.
    """
    if not request.include_suggested_fixes:
        return {
            "tool": "fix_generation", "status": "not_requested", "executed": False,
            "requested": False, "issue_count": len(issues), "generated_issue_count": 0,
            "outcome_counts": {}, "per_issue_outcomes": [],
        }
    if not issues:
        return {
            "tool": "fix_generation", "status": "not_needed", "executed": False,
            "requested": True, "issue_count": 0, "generated_issue_count": 0,
            "outcome_counts": {}, "per_issue_outcomes": [],
        }

    try:
        result = await generate_suggested_fixes(
            issues,
            owner=owner,
            repo=repo,
            head_sha=head_sha,
            diff_files=diff_files,
        )
    except Exception:  # noqa: BLE001 - Optional fix generation must not discard the completed review.
        # Keep internals and provider details out of the response, and retain
        # the completed review if the optional service fails as a whole.
        result = FixGenerationResult(outcomes=[
            IssueFixOutcome(issue.issue_id, "failed", "Fix generation failed; the review remains available.")
            for issue in issues
        ])

    issue_counts = Counter(issue.issue_id for issue in issues)
    issues_by_id = {issue.issue_id: issue for issue in issues if issue_counts[issue.issue_id] == 1}
    outcomes: list[dict[str, Any]] = []
    seen_outcome_ids: set[str] = set()
    for outcome in result.outcomes:
        status = outcome.status
        reason = outcome.reason
        if status == "generated":
            issue = issues_by_id.get(outcome.issue_id)
            fix = outcome.fix
            if (
                outcome.issue_id in seen_outcome_ids
                or issue is None
                or fix is None
                or fix.canonical_issue_id != outcome.issue_id
                or fix.generation_status != "generated"
                or any(existing.fix_id == fix.fix_id for existing in issue.suggested_fixes)
            ):
                status = "failed"
                reason = "The generated proposal could not be safely associated with its canonical issue."
            else:
                issue.suggested_fixes.append(fix)
        public_status = "skipped" if status == "skipped_limit" else status
        outcomes.append({"issue_id": outcome.issue_id, "status": public_status, "reason": reason})
        seen_outcome_ids.add(outcome.issue_id)

    # A service result should report each canonical issue. If it does not,
    # expose that omission as a safe failure rather than implying success.
    for issue in issues:
        if issue.issue_id not in seen_outcome_ids:
            outcomes.append({
                "issue_id": issue.issue_id,
                "status": "failed",
                "reason": "Fix generation returned no outcome for this canonical issue.",
            })

    outcome_counts = dict(Counter(item["status"] for item in outcomes))
    generated_count = outcome_counts.get("generated", 0)
    has_operational_failures = any(
        outcome_counts.get(status, 0)
        for status in ("unavailable", "failed", "skipped")
    )
    if has_operational_failures:
        aggregate_status = "partial" if generated_count else "unavailable"
    else:
        aggregate_status = "completed"
    return {
        "tool": "fix_generation",
        "status": aggregate_status,
        "executed": True,
        "requested": True,
        "issue_count": len(issues),
        "processed_issue_count": len(result.outcomes),
        "generated_issue_count": generated_count,
        "outcome_counts": outcome_counts,
        "per_issue_outcomes": outcomes,
        "source_fetch_count": result.source_fetch_count,
        "llm_call_count": result.llm_call_count,
        "provider_calls": result.provider_calls,
    }
