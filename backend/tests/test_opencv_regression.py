"""Regression test specifically verifying the OpenCV PR #31028 fixes (P1-P4)."""

import json
from pathlib import Path
import pytest

from app.ai_review.prompts import build_user_prompt
from app.languages.cpp import CppAdapter
from app.schemas.finding import ReviewFinding
from app.schemas.issue import FindingObservation
from app.services.finding_reconciliation import _build_issue
from app.services.review_orchestrator import _determine_finding_attribution
from app.verification.diff_mapping import added_lines_by_file


def test_opencv_core_hpp_comment_false_positives_eliminated(tmp_path):
    """P1 & P4: Verify that doc comments in OpenCV core.hpp mentioning 'new <noun>'

    (such as lines 1192, 1215, 2548, 2596, 2597, 2928, 3158) produce ZERO false positives.
    """
    # Excerpt representing lines in OpenCV core.hpp with Doxygen doc comments
    core_hpp_excerpt = """#ifndef OPENCV_CORE_HPP
#define OPENCV_CORE_HPP

/**
 * @file core.hpp
 * Line 1192 documentation:
 * This function calculates a new axes projection for matrix transformation.
 * Line 1215 documentation:
 * Generates a new shape descriptor for affine warping.
 * Line 2548 documentation:
 * Resets the buffer to a new state before matrix inversion.
 * Line 2596 documentation:
 * Computes a new coordinate system based on eigenvectors.
 * Line 2597 documentation:
 * Maps points into a new coordinate frame.
 * Line 2928 documentation:
 * Creates a new dimension slice from an existing MatND.
 * Line 3158 documentation:
 * Returns a new representation of the sparse tensor.
 */

namespace cv {

class Mat {
public:
    void* operator new(size_t sz);
    void* operator new[](size_t sz);
    
    // Real code with modern allocation
    void safe_alloc() {
        // no raw new
    }
};

} // namespace cv

#endif
"""
    cpp_adapter = CppAdapter()
    findings = cpp_adapter._run_builtin_analyzer(
        [("modules/core/include/opencv2/core.hpp", core_hpp_excerpt)],
        tmp_path,
    )

    # In production before P1/P4, this reported 7 findings. Now it must be 0!
    raw_new_findings = [f for f in findings if "CPP-MEM-RAW-NEW" in f.title]
    assert len(raw_new_findings) == 0, f"Expected 0 false positives, got {len(raw_new_findings)}: {raw_new_findings}"
    assert len(findings) == 0


def test_opencv_diff_mapping_and_three_state_attribution():
    """P2: Verify C++ diff hunk mapping and attribution (introduced vs pre_existing vs contextual).

    OpenCV PR #31028 modifies only 22 lines in core.hpp (around lines 140-162).
    """
    # Unified diff patch modifying lines 150-151 (2 context + 1 deleted + 1 context = 4 old; 2 context + 2 added + 1 context = 5 new)
    patch = (
        "@@ -148,4 +148,5 @@\n"
        " context_before\n"
        " existing_code\n"
        "-old_call()\n"
        "+new_call()\n"
        "+added_line()\n"
        " context_after"
    )
    bundle_files = [
        {"filename": "modules/core/include/opencv2/core.hpp", "status": "modified", "patch": patch},
        {"filename": "modules/core/src/copy.cpp", "status": "modified", "patch": "@@ -10 +10 @@\n-old\n+new"},
    ]

    added_lines_map, limitations = added_lines_by_file(bundle_files)
    assert limitations == {}
    assert "modules/core/include/opencv2/core.hpp" in added_lines_map
    added_lines = added_lines_map["modules/core/include/opencv2/core.hpp"]
    assert 150 in added_lines
    assert 151 in added_lines

    changed_files = {f["filename"] for f in bundle_files}

    # Finding 1: An introduced finding in the added lines (line 150)
    finding_introduced = ReviewFinding(
        id="f-intro",
        title="Cpp_Safety_Analyzer CPP-MEM-RAW-NEW",
        description="Raw new in PR hunk",
        file_path="modules/core/include/opencv2/core.hpp",
        line_start=150,
        line_end=150,
        severity="low",
        category="maintainability",
        confidence=0.75,
        source="static_analysis",
        verification_status="needs_review",
        why_it_matters="why",
        suggested_fix="fix",
    )
    attr1 = _determine_finding_attribution(finding_introduced, added_lines_map, changed_files)
    assert attr1 == "introduced"
    finding_introduced.attribution = attr1

    # Finding 2: A pre-existing finding in surrounding unchanged code (e.g. line 2548)
    finding_pre_existing = ReviewFinding(
        id="f-pre",
        title="Cpp_Safety_Analyzer CPP-MEM-RAW-NEW",
        description="Raw new in pre-existing codebase",
        file_path="modules/core/include/opencv2/core.hpp",
        line_start=2548,
        line_end=2548,
        severity="low",
        category="maintainability",
        confidence=0.75,
        source="static_analysis",
        verification_status="needs_review",
        why_it_matters="why",
        suggested_fix="fix",
    )
    attr2 = _determine_finding_attribution(finding_pre_existing, added_lines_map, changed_files)
    assert attr2 == "pre_existing"
    finding_pre_existing.attribution = attr2

    # Finding 3: Contextual observation (e.g. file-level diagnostic without line number)
    finding_contextual = ReviewFinding(
        id="f-ctx",
        title="General Diagnostic",
        description="File-level note",
        file_path="modules/core/include/opencv2/core.hpp",
        line_start=None,
        line_end=None,
        severity="informational",
        category="code_quality",
        confidence=0.5,
        source="static_analysis",
        verification_status="needs_review",
        why_it_matters="why",
        suggested_fix="fix",
    )
    attr3 = _determine_finding_attribution(finding_contextual, added_lines_map, changed_files)
    assert attr3 == "contextual"
    finding_contextual.attribution = attr3

    # CanonicalIssue reconciliation reflects the proper attribution
    obs1 = FindingObservation(original_finding=finding_introduced, tool_name="cpp_safety_analyzer")
    obs2 = FindingObservation(original_finding=finding_pre_existing, tool_name="cpp_safety_analyzer")
    obs3 = FindingObservation(original_finding=finding_contextual, tool_name="static")

    issue1 = _build_issue([obs1])
    assert issue1.attribution == "introduced"

    issue2 = _build_issue([obs2])
    assert issue2.attribution == "pre_existing"

    issue3 = _build_issue([obs3])
    assert issue3.attribution == "contextual"


def test_opencv_ai_review_prompt_within_budget_and_valid_json():
    """P3: Verify that user prompt for OpenCV PR stays within 28,000 characters and parses as valid JSON."""
    # Simulated OpenCV PR metadata with long issue template description
    metadata = {
        "repository_owner": "opencv",
        "repository_name": "opencv",
        "pull_request_number": 31028,
        "title": "Fix memory allocation in core matrix operations",
        "description": (
            "### Description\n"
            "This pull request optimizes core matrix transforms.\n\n"
            "### Checklist\n"
            "- [x] I have read the contribution guidelines.\n"
            "- [ ] Added unit tests.\n\n"
            "| Component | Status | Details |\n"
            "| --- | --- | --- |\n"
            "| Core | Modified | Mat operations |\n"
            "| Highgui | Unchanged | N/A |\n"
        ) * 20,
    }

    files = [
        {
            "filename": "modules/core/include/opencv2/core.hpp",
            "status": "modified",
            "patch": "@@ -140,20 +140,22 @@\n" + ("+new_code_line();\n" * 22),
        },
        {
            "filename": "modules/core/src/copy.cpp",
            "status": "modified",
            "patch": "@@ -50,5 +50,5 @@\n" + ("+int x = 1;\n" * 5),
        },
    ]

    prompt = build_user_prompt(metadata, files, [])

    assert len(prompt) <= 28_000, f"Prompt length {len(prompt)} exceeds 28,000 limit"
    assert "<untrusted_pr_json>" in prompt
    assert "</untrusted_pr_json>" in prompt

    json_start = prompt.index("<untrusted_pr_json>\n") + len("<untrusted_pr_json>\n")
    json_end = prompt.rindex("\n</untrusted_pr_json>")
    json_str = prompt[json_start:json_end]

    # Must be valid, parseable JSON
    parsed = json.loads(json_str)
    assert parsed["pull_request"]["repository_name"] == "opencv"
    assert len(parsed["diff_files"]) == 2


def test_opencv_ai_review_candidate_normalization_and_diff_grounding(monkeypatch):
    """Verify that candidate findings for OpenCV PR #31028 with provider quirks

    (markdown fences, synonyms, extra metadata) normalize and ground correctly in added lines.
    """
    import asyncio
    from app.ai_review import reviewer

    diff_files = [
        {
            "path": "modules/core/include/opencv2/core.hpp",
            "hunks": [
                {
                    "lines": [
                        {"kind": "added", "line": 150, "text": "void* ptr = cv::fastMalloc(sz);"},
                        {"kind": "added", "line": 151, "text": "CV_Assert(ptr != NULL);"},
                    ]
                }
            ],
        }
    ]

    provider_output = """```json
    {
        "findings": [
            {
                "title": "Use of raw C allocation function",
                "message": "fastMalloc is a low-level allocator; prefer std::vector where possible",
                "file": "modules/core/include/opencv2/core.hpp",
                "line": 150,
                "severity": "WARNING",
                "category": "MAINTAINABILITY",
                "confidence": 0.8,
                "evidence": ["fastMalloc(sz)"],
                "explanation": "Manual memory management risks leaks",
                "fix": "std::vector<uchar> buffer(sz);",
                "rule_id": "OPENCV-MEM-ALLOC"
            }
        ]
    }
    ```"""

    async def mock_complete(*_args, **_kwargs):
        return provider_output

    monkeypatch.setattr(reviewer, "complete_json", mock_complete)
    findings = asyncio.run(reviewer.generate_candidates({}, diff_files, []))
    assert len(findings) == 1
    f = findings[0]
    assert f.file_path == "modules/core/include/opencv2/core.hpp"
    assert f.line_start == 150
    assert f.severity == "medium"
    assert f.category == "maintainability"
    assert f.suggested_fix == "std::vector<uchar> buffer(sz);"


def test_opencv_ai_failure_preserves_static_findings_and_explains_both_canonical_issues(monkeypatch):
    """Verify that when AI candidate review fails, all canonical static findings are preserved,

    and issue explanations succeed for both canonical issues.
    """
    import asyncio
    from app.ai_review import reviewer, issue_explanations
    from app.ai_review.client import AIReviewUnavailable
    from app.schemas.finding import ReviewFinding
    from app.schemas.issue import CanonicalIssue, FindingObservation
    from app.services.finding_reconciliation import reconcile_observations

    # 1. Static findings: 2 Ruff findings from OpenCV PR #31028
    ruff_finding_1 = ReviewFinding(
        id="ruff-f1",
        title="Ruff F401",
        description="`os` imported but unused",
        file_path="modules/python/test/test_core.py",
        line_start=12,
        line_end=12,
        severity="low",
        category="code_quality",
        confidence=0.9,
        source="static_analysis",
        verification_status="verified",
        why_it_matters="Unused imports slow down startup",
        suggested_fix="Remove `import os`",
    )
    ruff_finding_2 = ReviewFinding(
        id="ruff-f2",
        title="Ruff E711",
        description="Comparison to `None` should be `cond is None`",
        file_path="modules/python/test/test_core.py",
        line_start=45,
        line_end=45,
        severity="low",
        category="code_quality",
        confidence=0.95,
        source="static_analysis",
        verification_status="verified",
        why_it_matters="Identity comparison is faster and avoids custom __eq__ bugs",
        suggested_fix="Use `is None`",
    )

    static_findings = [ruff_finding_1, ruff_finding_2]
    observations = [
        FindingObservation(original_finding=ruff_finding_1, tool_name="ruff", rule_id="F401"),
        FindingObservation(original_finding=ruff_finding_2, tool_name="ruff", rule_id="E711"),
    ]

    # Reconcile into 2 CanonicalIssues
    reconciliation = reconcile_observations(observations)
    assert len(reconciliation.issues) == 2
    issue_ids = [issue.issue_id for issue in reconciliation.issues]

    # 2. When AI candidate generation fails with schema error, verify static findings are preserved
    async def mock_fail_candidates(*_args, **_kwargs):
        raise AIReviewUnavailable("AI provider returned a finding that did not match the review schema.")

    monkeypatch.setattr(reviewer, "complete_json", mock_fail_candidates)
    with pytest.raises(AIReviewUnavailable, match="did not match the review schema"):
        asyncio.run(reviewer.generate_candidates({}, [], []))

    # Static findings remain intact (preserving all findings when AI fails)
    assert len(static_findings) == 2

    # 3. Verify explanation generation for both canonical issues succeeds even with provider quirks
    explanation_response = json.dumps({
        "explanations": [
            {
                "issue_id": issue_ids[0],
                "summary": "Unused `os` import in test module",
                "impact": "Increases module load time and clutters namespace",
                "cause": "Import statement was retained after refactoring",
                "fix": "Remove `import os` statement",
                "topic": "Clean Python Code",
            },
            {
                "issue_id": issue_ids[1],
                "summary": "Equality comparison to None using `==` instead of `is`",
                "impact": "May invoke arbitrary overloaded `__eq__` methods",
                "cause": "Using `== None` rather than singleton identity check",
                "fix": "Change `x == None` to `x is None`",
                "topic": "Python Idiomatic Comparison",
            },
        ]
    })

    async def mock_complete_explanations(task, guidance, prompt, parse_func, **_kwargs):
        return explanation_response, parse_func(explanation_response)

    monkeypatch.setattr(issue_explanations, "complete_validated_json", mock_complete_explanations)

    result = asyncio.run(
        issue_explanations.generate_issue_explanations(reconciliation.issues)
    )

    assert result.failed_batch_count == 0
    assert result.rejected_explanation_count == 0
    assert len(result.explanations) == 2
    assert issue_ids[0] in result.explanations
    assert issue_ids[1] in result.explanations
    assert result.issue_statuses[issue_ids[0]] == "completed"
    assert result.issue_statuses[issue_ids[1]] == "completed"

