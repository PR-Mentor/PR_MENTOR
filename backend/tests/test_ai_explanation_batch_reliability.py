"""Tests for AI issue explanation batch reliability, JSON parsing, issue ID association, and partial success isolation."""

import asyncio
import json
import httpx
import pytest

from app.ai_review import client, issue_explanations as service
from app.ai_review.client import AIReviewUnavailable
from app.ai_review.providers import ProviderConfig
from app.schemas.finding import ReviewFinding
from app.schemas.issue import CanonicalIssue, FindingObservation


def _finding(*, finding_id="finding-1", source="static_analysis", description="Unsafe command.", evidence=None, line=4):
    return ReviewFinding(
        id=finding_id,
        title="Unsafe command",
        description=description,
        file_path="src/runner.py",
        line_start=line,
        line_end=line,
        severity="high",
        category="security",
        confidence=0.9,
        source=source,
        verification_status="needs_review",
        evidence=evidence or ["subprocess.run(command, shell=True)"],
        why_it_matters="Command execution risk",
        suggested_fix="Avoid shell=True",
        learning_explanation="Keep arguments in argv vector",
        limitations=[],
    )


def _issue(issue_id="issue-1", observations=None, line=4, file_path="src/runner.py"):
    return CanonicalIssue(
        issue_id=issue_id,
        file_path=file_path,
        line_start=line,
        line_end=line,
        category="security",
        severity="high",
        normalized_description="Untrusted input reaches shell execution.",
        observations=observations
        if observations is not None
        else [
            FindingObservation(
                original_finding=_finding(finding_id=f"f-{issue_id}"),
                tool_name="ruff",
                rule_id="S602",
            )
        ],
    )


def _explanation(**overrides):
    base = {
        "summary": "Untrusted input reaches a shell command.",
        "why_it_matters": "Arbitrary command execution.",
        "root_cause": "shell=True was passed to subprocess.",
        "correction": "Pass argv list without shell=True.",
        "concept": "Command injection prevention.",
        "learning_resources": [],
        "caveats": ["Surrounding command logic not shown."],
    }
    base.update(overrides)
    return base


def _stub_validated_completion(monkeypatch, completion):
    async def complete_validated_json(task, system_prompt, user_prompt, validator, **kwargs):
        raw = await completion(system_prompt, user_prompt)
        return raw, validator(raw)

    monkeypatch.setattr(service, "complete_validated_json", complete_validated_json)


# ---------------------------------------------------------------------------
# 1. JSON Parsing Tests
# ---------------------------------------------------------------------------


def test_json_parsing_top_level_array(monkeypatch):
    """Support top-level array of explanation objects in explanation workflow."""
    issue = _issue("issue-uuid-1")
    payload = json.dumps([
        {"issue_id": "issue-uuid-1", "explanation": _explanation(summary="Array explanation 1")}
    ])

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations([issue]))
    assert "issue-uuid-1" in res.explanations
    assert res.explanations["issue-uuid-1"].summary == "Array explanation 1"
    assert res.issue_statuses["issue-uuid-1"] == "completed"


def test_json_parsing_object_with_explanations_array(monkeypatch):
    """Support object containing an explanations array."""
    issue = _issue("issue-uuid-1")
    payload = json.dumps({
        "explanations": [
            {"issue_id": "issue-uuid-1", "explanation": _explanation(summary="Object array explanation 1")}
        ]
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations([issue]))
    assert "issue-uuid-1" in res.explanations
    assert res.explanations["issue-uuid-1"].summary == "Object array explanation 1"
    assert res.issue_statuses["issue-uuid-1"] == "completed"


def test_json_parsing_malformed_json(monkeypatch):
    """Malformed JSON fails gracefully and records failure."""
    issue = _issue("issue-uuid-1")

    async def complete(_system, _prompt):
        return "not valid json { missing brace"

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations([issue]))
    assert "issue-uuid-1" not in res.explanations
    assert res.issue_statuses["issue-uuid-1"] == "failed"
    assert res.failed_batch_count == 1


def test_json_parsing_unsupported_primitive_types():
    """Unsupported primitive JSON values (int, string, bool) are rejected."""
    for primitive in ("123", '"plain text string"', "true", "null"):
        with pytest.raises(AIReviewUnavailable):
            service._validate_batch_response(primitive, {"issue-1"}, set())


def test_complete_json_unrelated_callers_reject_top_level_array(monkeypatch):
    """Unrelated callers of complete_json() without allow_top_level_array reject top-level arrays."""
    provider = ProviderConfig("test-p", "secret", "model", "https://example.com/chat", True, 2)
    monkeypatch.setattr(client, "provider_registry", lambda _task=None: [provider])
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, content=b'{"choices":[{"message":{"content":"[{\\"k\\":1}]"}}]}'))

    class CustomAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(client.httpx, "AsyncClient", CustomAsyncClient)

    # Calling for code_review or default task must reject top-level array
    with pytest.raises(AIReviewUnavailable, match="must contain one JSON object"):
        asyncio.run(client.complete_json("sys", "user", task="code_review"))

    # Calling with allow_top_level_array=True or for issue_explanation allows it
    res = asyncio.run(client.complete_json("sys", "user", task="issue_explanation"))
    assert res == '[{"k":1}]'


# ---------------------------------------------------------------------------
# 2. Issue Association Tests
# ---------------------------------------------------------------------------


def test_association_exact_canonical_ids(monkeypatch):
    """Explanations matching exact canonical IDs are accepted."""
    issues = [_issue("can-id-1"), _issue("can-id-2")]
    payload = json.dumps({
        "explanations": [
            {"issue_id": "can-id-1", "explanation": _explanation(summary="Sum 1")},
            {"issue_id": "can-id-2", "explanation": _explanation(summary="Sum 2")},
        ]
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations(issues))
    assert set(res.explanations) == {"can-id-1", "can-id-2"}
    assert res.issue_statuses["can-id-1"] == "completed"
    assert res.issue_statuses["can-id-2"] == "completed"


def test_association_ordinal_ids_rejected(monkeypatch):
    """Ordinal IDs like '0', '1', 'issue_1' are rejected and never mapped positionally."""
    issues = [_issue("can-id-1"), _issue("can-id-2")]
    payload = json.dumps({
        "explanations": [
            {"issue_id": "0", "explanation": _explanation(summary="Sum 0")},
            {"issue_id": "1", "explanation": _explanation(summary="Sum 1")},
        ]
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations(issues))
    assert "can-id-1" not in res.explanations
    assert "can-id-2" not in res.explanations
    assert res.issue_statuses["can-id-1"] == "failed"
    assert res.issue_statuses["can-id-2"] == "failed"
    assert res.rejected_explanation_count == 2


def test_association_unknown_ids_rejected(monkeypatch):
    """Unknown IDs are rejected."""
    issues = [_issue("can-id-1")]
    payload = json.dumps({
        "explanations": [
            {"issue_id": "completely-unknown-id", "explanation": _explanation(summary="Unknown")}
        ]
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations(issues))
    assert "can-id-1" not in res.explanations
    assert res.issue_statuses["can-id-1"] == "failed"
    assert res.rejected_explanation_count == 1


def test_association_missing_ids_in_multi_issue_batch_rejected(monkeypatch):
    """In multi-issue batch, entries missing issue_id are rejected."""
    issues = [_issue("can-id-1"), _issue("can-id-2")]
    payload = json.dumps({
        "explanations": [
            {"explanation": _explanation(summary="Missing ID 1")},
            {"explanation": _explanation(summary="Missing ID 2")},
        ]
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations(issues))
    assert len(res.explanations) == 0
    assert res.issue_statuses["can-id-1"] == "failed"
    assert res.issue_statuses["can-id-2"] == "failed"
    assert res.rejected_explanation_count == 2


def test_association_duplicate_ids_rejected(monkeypatch):
    """When the provider returns duplicate entries for the same ID, both are rejected."""
    issues = [_issue("can-id-1")]
    payload = json.dumps({
        "explanations": [
            {"issue_id": "can-id-1", "explanation": _explanation(summary="Duplicate A")},
            {"issue_id": "can-id-1", "explanation": _explanation(summary="Duplicate B")},
        ]
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations(issues))
    assert "can-id-1" not in res.explanations
    assert res.issue_statuses["can-id-1"] == "failed"
    assert res.rejected_explanation_count == 2


def test_association_conflicting_dict_key_and_inner_id_rejected(monkeypatch):
    """Conflicting dictionary key and inner explanation issue_id must be rejected."""
    issues = [_issue("can-id-1"), _issue("can-id-2")]
    payload = json.dumps({
        "explanations": {
            "can-id-1": {
                "issue_id": "can-id-2",  # Conflict! Outer key is can-id-1, inner claims can-id-2
                **_explanation(summary="Conflicting"),
            }
        }
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations(issues))
    assert "can-id-1" not in res.explanations
    assert "can-id-2" not in res.explanations
    assert res.rejected_explanation_count == 1


def test_association_single_issue_strict_fallback(monkeypatch):
    """When exactly 1 issue is expected and 1 entry is returned without ID, fallback is permitted."""
    issue = _issue("can-id-single")
    payload = json.dumps({
        "explanation": _explanation(summary="Single fallback explanation")
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations([issue]))
    assert "can-id-single" in res.explanations
    assert res.explanations["can-id-single"].summary == "Single fallback explanation"
    assert res.issue_statuses["can-id-single"] == "completed"


def test_association_ambiguous_issue_ids_in_context():
    """If the input issues list contains duplicated canonical IDs, mark unavailable without querying provider."""
    issue1 = _issue("ambig-id")
    issue2 = _issue("ambig-id")

    res = asyncio.run(service.generate_issue_explanations([issue1, issue2]))
    assert "ambig-id" not in res.explanations
    assert res.issue_statuses["ambig-id"] == "unavailable"
    assert res.context_failure_count == 2


# ---------------------------------------------------------------------------
# 3. Partial Success & Isolation Tests
# ---------------------------------------------------------------------------


def test_partial_success_one_valid_one_invalid(monkeypatch):
    """One valid and one invalid entry in the same batch preserves valid and marks invalid failed."""
    issue_a = _issue("issue-valid")
    issue_b = _issue("issue-invalid")

    payload = json.dumps({
        "explanations": [
            {"issue_id": "issue-valid", "explanation": _explanation(summary="Valid explanation")},
            {"issue_id": "issue-invalid", "explanation": {"summary": "invalid incomplete payload"}},
        ]
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations([issue_a, issue_b]))

    assert "issue-valid" in res.explanations
    assert res.explanations["issue-valid"].summary == "Valid explanation"
    assert res.issue_statuses["issue-valid"] == "completed"

    assert "issue-invalid" not in res.explanations
    assert res.issue_statuses["issue-invalid"] == "failed"
    assert res.rejected_explanation_count == 1


def test_all_entries_invalid_in_batch(monkeypatch):
    """When all entries in a batch are invalid, all issues are marked failed."""
    issues = [_issue("iss-1"), _issue("iss-2")]
    payload = json.dumps({
        "explanations": [
            {"issue_id": "iss-1", "explanation": {"summary": "bad"}},
            {"issue_id": "iss-2", "explanation": {"summary": "bad"}},
        ]
    })

    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations(issues))
    assert len(res.explanations) == 0
    assert res.issue_statuses["iss-1"] == "failed"
    assert res.issue_statuses["iss-2"] == "failed"
    assert res.rejected_explanation_count == 2


def test_connectivity_error_does_not_retry_indefinitely(monkeypatch):
    """Connectivity errors (HTTP 503, timeout) abort immediately without hammering provider."""
    issues = [_issue("iss-1"), _issue("iss-2")]
    calls = 0

    async def complete(_system, _prompt):
        nonlocal calls
        calls += 1
        raise AIReviewUnavailable("AI provider returned HTTP 503.")

    _stub_validated_completion(monkeypatch, complete)
    res = asyncio.run(service.generate_issue_explanations(issues))
    # Only 1 batch call, no sub-calls hammering the 503 provider
    assert calls == 1
    assert len(res.explanations) == 0
    assert res.issue_statuses["iss-1"] == "failed"
    assert res.issue_statuses["iss-2"] == "failed"


def test_original_findings_and_evidence_preserved_on_failure(monkeypatch):
    """Canonical issues, findings, and evidence are never mutated or dropped on explanation failure."""
    finding_obj = _finding(finding_id="orig-f1", description="Original finding description", evidence=["secret_evidence()"])
    obs = FindingObservation(original_finding=finding_obj, tool_name="bandit", rule_id="B101")
    issue = _issue("issue-preserve", observations=[obs])

    async def complete_fail(_system, _prompt):
        raise AIReviewUnavailable("Provider internal failure")

    _stub_validated_completion(monkeypatch, complete_fail)
    res = asyncio.run(service.generate_issue_explanations([issue]))

    assert res.issue_statuses["issue-preserve"] == "failed"
    # Verify original objects remain intact
    assert len(issue.observations) == 1
    assert issue.observations[0].original_finding.id == "orig-f1"
    assert issue.observations[0].original_finding.description == "Original finding description"
    assert issue.observations[0].original_finding.evidence == ["secret_evidence()"]


# ---------------------------------------------------------------------------
# 4. Exact Reproduction Test for OpenCV Review c70382ec2d85403fa455fbbe476f393e
# ---------------------------------------------------------------------------


def test_opencv_review_3_issue_top_level_array_reproduction(monkeypatch):
    """Reproduce the OpenCV PR #31028 production failure where the provider returns a 3-issue top-level array.

    Prior to this fix, complete_json() raised AIReviewUnavailable('must contain one JSON object').
    With our fix, all 3 issues are explained and marked completed.
    """
    id_ruff_1 = "c70382ec2d85403fa455fbbe476f393e-ruff-1"
    id_ruff_2 = "c70382ec2d85403fa455fbbe476f393e-ruff-2"
    id_ai_cpp = "c70382ec2d85403fa455fbbe476f393e-cpp-ai"

    issues = [
        _issue(id_ruff_1, file_path="modules/python/test/test_copy_make_border.py", line=3),
        _issue(id_ruff_2, file_path="modules/python/test/test_copy_make_border.py", line=12),
        _issue(id_ai_cpp, file_path="modules/core/src/copy.cpp", line=1344),
    ]

    # Representative provider response returning top-level array of explanations
    top_level_array_response = json.dumps([
        {
            "issue_id": id_ruff_1,
            "explanation": _explanation(
                summary="Unsorted imports in Python test script.",
                root_cause="Imports do not follow Ruff isort sorting rules.",
                correction="Sort imports alphabetically.",
                concept="Clean Python code and PEP 8 import styling.",
            ),
        },
        {
            "issue_id": id_ruff_2,
            "explanation": _explanation(
                summary="Unused local variable or import.",
                root_cause="Import was defined but never referenced.",
                correction="Remove unused import.",
                concept="Dead code elimination.",
            ),
        },
        {
            "issue_id": id_ai_cpp,
            "explanation": _explanation(
                summary="Memory copy byte width in copyMakeConstBorder_8u.",
                root_cause="C++ kernel operates on uchar* raw bytes.",
                correction="Ensure total bytes per pixel (elemSize) are passed.",
                concept="Low-level memory replication kernels.",
            ),
        },
    ])

    async def mock_provider_response(_system, _prompt):
        return top_level_array_response

    _stub_validated_completion(monkeypatch, mock_provider_response)

    res = asyncio.run(service.generate_issue_explanations(issues))

    assert res.failed_batch_count == 0
    assert res.rejected_explanation_count == 0
    assert len(res.explanations) == 3
    assert set(res.explanations) == {id_ruff_1, id_ruff_2, id_ai_cpp}
    assert res.issue_statuses[id_ruff_1] == "completed"
    assert res.issue_statuses[id_ruff_2] == "completed"
    assert res.issue_statuses[id_ai_cpp] == "completed"
    assert "Unsorted imports" in res.explanations[id_ruff_1].summary
    assert "copyMakeConstBorder_8u" in res.explanations[id_ai_cpp].summary


# ---------------------------------------------------------------------------
# 5. Finding Pipeline, De-duplication, & False-Positive Regression Tests
# ---------------------------------------------------------------------------


def test_generate_candidates_deduplicates_identical_findings(monkeypatch):
    """When the AI provider returns duplicate candidate findings with the same signature, only one is kept."""
    from app.ai_review import reviewer

    diff_files = [{
        "path": "modules/core/src/copy.cpp",
        "hunks": [{
            "lines": [
                {"kind": "added", "line": 1344, "text": "copyMakeConstBorder_8u(src.ptr(), src.step, src.size());"},
                {"kind": "added", "line": 1345, "text": "return dst;"},
            ]
        }],
    }]

    dup_finding = {
        "title": "Suspicious Memory Call",
        "description": "Alleged type mismatch in kernel call.",
        "file_path": "modules/core/src/copy.cpp",
        "line_start": 1344,
        "line_end": 1344,
        "severity": "medium",
        "category": "bug",
        "confidence": 0.85,
        "evidence": ["copyMakeConstBorder_8u"],
        "why_it_matters": "Potential buffer misuse",
        "suggested_fix": "Pass explicit depth conversion",
    }

    # Provider emits 3 copies of the exact same finding
    payload = json.dumps({"findings": [dup_finding, dup_finding, dup_finding]})

    async def mock_complete(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(reviewer, "complete_json", mock_complete)
    findings = asyncio.run(reviewer.generate_candidates({}, diff_files, []))
    assert len(findings) == 1
    assert findings[0].line_start == 1344


def test_ai_finding_grounding_rejects_hallucinated_unchanged_lines():
    """Verify that AI findings pointing to unchanged or missing lines in C++ code are rejected by grounding."""
    from app.services.review_orchestrator import _ground_ai_findings

    diff_files = [{
        "path": "modules/core/src/copy.cpp",
        "hunks": [{
            "lines": [
                {"kind": "added", "line": 1344, "text": "copyMakeBorderWithBorderValues();"},
            ]
        }],
    }]

    # 1. Finding points to line 1032 (which is in the unchanged base file, not in the PR added lines)
    unsupported_finding = ReviewFinding(
        id="f-hallucinated",
        title="Hallucinated kernel bug",
        description="False positive on legacy copyMakeConstBorder_8u kernel.",
        file_path="modules/core/src/copy.cpp",
        line_start=1032,
        line_end=1032,
        severity="medium",
        category="bug",
        confidence=0.8,
        source="ai_candidate",
        verification_status="needs_review",
        evidence=["copyMakeConstBorder_8u"],
        why_it_matters="Hallucination",
        suggested_fix="None",
        limitations=[],
    )

    accepted, rejections = _ground_ai_findings([unsupported_finding], diff_files)
    assert len(accepted) == 0
    assert rejections.get("line_not_in_diff") == 1


# ---------------------------------------------------------------------------
# 6. Provider Failure Classification & Reliability Tests
# ---------------------------------------------------------------------------


def test_provider_health_tracker_classifies_http_413_as_non_retryable():
    """HTTP 413 (Payload Too Large) is classified as SIZE_LIMIT_EXCEEDED and is non-retryable."""
    from app.ai_orchestration.health import FailureCategory, ProviderHealthTracker

    tracker = ProviderHealthTracker()
    exc = AIReviewUnavailable("AI provider request payload exceeded size limit (HTTP 413).")
    category, retryable, retry_after = tracker.classify_exception(exc)

    assert category == FailureCategory.SIZE_LIMIT_EXCEEDED
    assert retryable is False
    assert retry_after is None


def test_provider_health_tracker_classifies_auth_failure_as_non_retryable():
    """HTTP 401/403 are classified as AUTHENTICATION_ERROR and are non-retryable."""
    from app.ai_orchestration.health import FailureCategory, ProviderHealthTracker

    tracker = ProviderHealthTracker()
    exc = AIReviewUnavailable("AI provider returned HTTP 401.")
    category, retryable, retry_after = tracker.classify_exception(exc)

    assert category == FailureCategory.AUTHENTICATION_ERROR
    assert retryable is False


def test_provider_health_tracker_classifies_timeout_as_retryable():
    """Timeout exceptions are classified as TRANSIENT_NETWORK and are retryable."""
    from app.ai_orchestration.health import FailureCategory, ProviderHealthTracker

    tracker = ProviderHealthTracker()
    exc = AIReviewUnavailable("AI provider did not respond in time (timeout).")
    category, retryable, _ = tracker.classify_exception(exc)

    assert category == FailureCategory.TRANSIENT_NETWORK
    assert retryable is True


# ---------------------------------------------------------------------------
# 7. Multi-Language Context & Candidate Normalization (C, C++, Java, Python)
# ---------------------------------------------------------------------------


def test_multi_language_ai_candidate_normalization_all_languages(monkeypatch):
    """Verify AI candidate generation and normalization works consistently across C, C++, Java, and Python."""
    from app.ai_review import reviewer

    diff_files = [
        {"path": "src/driver.c", "hunks": [{"lines": [{"kind": "added", "line": 10, "text": "char* p = malloc(32);"}]}]},
        {"path": "src/matrix.cpp", "hunks": [{"lines": [{"kind": "added", "line": 42, "text": "auto ptr = std::make_shared<Mat>();"}]}]},
        {"path": "src/Server.java", "hunks": [{"lines": [{"kind": "added", "line": 88, "text": "ServerSocket s = new ServerSocket(8080);"}]}]},
        {"path": "src/app.py", "hunks": [{"lines": [{"kind": "added", "line": 15, "text": "subprocess.run(shlex.split(cmd))"}]}]},
    ]

    raw_candidates = [
        {"file": "src/driver.c", "line": 10, "title": "C Memory Leak", "severity": "warning", "category": "security", "confidence": "90%", "evidence": ["malloc(32)"], "description": "Pointer not freed"},
        {"file": "src/matrix.cpp", "line": 42, "title": "C++ Modern Pointer", "severity": "info", "category": "best_practice", "confidence": 0.95, "evidence": ["std::make_shared<Mat>()"], "description": "Good use of smart pointers"},
        {"file": "src/Server.java", "line": 88, "title": "Java Socket Binding", "severity": "error", "category": "bug", "confidence": 0.8, "evidence": ["ServerSocket s = new ServerSocket(8080)"], "description": "Hardcoded port"},
        {"file": "src/app.py", "line": 15, "title": "Python Safe Subprocess", "severity": "low", "category": "code_quality", "confidence": "0.75", "evidence": ["shlex.split(cmd)"], "description": "Avoids shell=True"},
    ]

    async def mock_complete(*_args, **_kwargs):
        return json.dumps({"findings": raw_candidates})

    monkeypatch.setattr(reviewer, "complete_json", mock_complete)

    findings = asyncio.run(reviewer.generate_candidates({}, diff_files, []))
    assert len(findings) == 4

    findings_by_file = {f.file_path: f for f in findings}
    # C finding: severity warning -> medium
    assert findings_by_file["src/driver.c"].severity == "medium"
    assert findings_by_file["src/driver.c"].confidence == 0.90

    # C++ finding: severity info -> informational, category best_practice -> code_quality
    assert findings_by_file["src/matrix.cpp"].severity == "informational"
    assert findings_by_file["src/matrix.cpp"].category == "code_quality"

    # Java finding: severity error -> high
    assert findings_by_file["src/Server.java"].severity == "high"

    # Python finding
    assert findings_by_file["src/app.py"].severity == "low"
    assert findings_by_file["src/app.py"].confidence == 0.75

