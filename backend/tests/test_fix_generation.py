import asyncio
import hashlib
import json

import pytest

from app.ai_review import fix_generation
from app.ai_review.client import AIReviewUnavailable
from app.schemas.finding import ReviewFinding
from app.schemas.issue import CanonicalIssue, FindingObservation, IssueExplanation

HEAD_SHA = "a" * 40
SOURCE = "\n".join([*(f"# source line {number}" for number in range(1, 10)), "run(command, shell=True)", "print('done')"]) + "\n"
ORIGINAL = "run(command, shell=True)"
REPLACEMENT = "run([\"tool\", command], shell=False)"


def make_issue(issue_id="issue-1", line=10, *, path="src/runner.py", with_explanation=True):
    finding = ReviewFinding(
        id=f"finding-{issue_id}", title="Unsafe shell execution", description="A command uses shell execution.",
        file_path=path, line_start=line, line_end=line, severity="high", category="security",
        confidence=0.91, source="static_analysis", verification_status="needs_review",
        evidence=["run(command, shell=True)"], why_it_matters="The shell may interpret untrusted text.",
        suggested_fix="Pass an argument list.", learning_explanation="Shell parsing interprets metacharacters.", limitations=[],
    )
    return CanonicalIssue(
        issue_id=issue_id, file_path=path, line_start=line, line_end=line, category="security", severity="high",
        normalized_description="Avoid unsafe shell execution.",
        observations=[FindingObservation(original_finding=finding, tool_name="bandit", provider_name="test", rule_id="B602")],
        explanation=IssueExplanation(
            summary="A shell call receives a command string.", why_it_matters="Untrusted text may alter the command.",
            root_cause="The shell parses string input.", correction="Prefer an argument vector.",
            concept="Keep data separate from command syntax.", caveats=["The caller context is limited."],
        ) if with_explanation else None,
    )


def fix_payload(issue, *, start=10, end=10, expected=ORIGINAL, **overrides):
    value = {
        "canonical_issue_id": issue.issue_id,
        "file_path": issue.file_path,
        "start_line": start,
        "end_line": end,
        "expected_original_code": expected,
        "proposed_code": REPLACEMENT,
        "change_summary": "Avoid invoking the shell for this command.",
        "rationale": "An argument list preserves the distinction between executable and data.",
        "limitations": ["Caller input validation still needs review."],
        "confidence": 0.84,
    }
    value.update(overrides)
    return {"fix": value}


def configure(monkeypatch, *, source=SOURCE, response=None, configured=True):
    source_calls = []
    llm_prompts = []

    async def get_source(owner, repo, path, ref):
        source_calls.append((owner, repo, path, ref))
        return source

    async def complete(system_prompt, user_prompt):
        llm_prompts.append((system_prompt, user_prompt))
        result = response() if callable(response) else response
        return json.dumps(result) if isinstance(result, dict) else result

    monkeypatch.setattr(fix_generation, "get_file_content", get_source)
    monkeypatch.setattr(fix_generation, "is_configured", lambda: configured)
    monkeypatch.setattr(fix_generation, "complete_json", complete)
    return source_calls, llm_prompts


def test_generates_source_verified_fix_with_backend_snapshot_and_issue_association(monkeypatch):
    issue = make_issue()
    before = issue.model_dump(mode="json")
    source_calls, prompts = configure(monkeypatch, response=lambda: fix_payload(issue))
    diff = [{
        "path": issue.file_path, "status": "modified", "hunks": [{"header": "@@ -10 +10 @@", "lines": [
            {"kind": "removed", "line": 10, "text": "run(command, shell=True)"},
            {"kind": "added", "line": 10, "text": REPLACEMENT},
        ]}],
    }]

    result = asyncio.run(fix_generation.generate_suggested_fixes(
        [issue], owner="acme", repo="widget", head_sha=HEAD_SHA, diff_files=diff,
    ))

    assert len(result.fixes) == 1
    fix = result.fixes[0]
    expected_blob = hashlib.sha1(f"blob {len(SOURCE.encode())}\0".encode() + SOURCE.encode()).hexdigest()
    assert fix.canonical_issue_id == issue.issue_id
    assert fix.file_path == issue.file_path
    assert fix.start_line == 10 and fix.end_line == 10
    assert fix.expected_original_code == ORIGINAL
    assert fix.proposed_code == REPLACEMENT
    assert fix.base_commit_sha == HEAD_SHA
    assert fix.file_blob_sha == expected_blob
    assert fix.generation_status == "generated" and fix.decision_status == "pending"
    assert result.outcomes[0].status == "generated"
    assert result.source_fetch_count == 1 and result.llm_call_count == 1
    assert source_calls == [("acme", "widget", issue.file_path, HEAD_SHA)]
    assert "B602" in prompts[0][1] and "The caller context is limited." in prompts[0][1]
    assert '"kind":"added"' in prompts[0][1]
    assert "@@ -10 +10 @@" in prompts[0][1]
    assert '"numbered_lines":"6: # source line 6' in prompts[0][1]
    assert '"numbered_lines":"1: # source line 1' not in prompts[0][1]
    assert issue.model_dump(mode="json") == before


@pytest.mark.parametrize("overrides", [
    {"expected_original_code": "invented original"},
    {"canonical_issue_id": "another-issue"},
    {"file_path": "src/other.py"},
    {"start_line": 9},
    {"end_line": 11},
    {"start_line": 10, "end_line": 9},
    {"start_line": 10, "end_line": 10, "proposed_code": ORIGINAL},
])
def test_rejects_unverifiable_or_wrong_range_replacements(monkeypatch, overrides):
    issue = make_issue()
    configure(monkeypatch, response=lambda: fix_payload(issue, **overrides))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.fixes == []
    assert result.outcomes[0].status == "failed"


def test_no_source_context_means_no_llm_call(monkeypatch):
    issue = make_issue()
    _, prompts = configure(monkeypatch, source=None, response=lambda: fix_payload(issue))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "insufficient_context"
    assert result.fixes == []
    assert prompts == []


def test_missing_issue_lines_means_no_llm_call(monkeypatch):
    issue = make_issue(line=10)
    issue.line_start = None
    issue.line_end = None
    issue.observations[0].original_finding.line_start = None
    issue.observations[0].original_finding.line_end = None
    _, prompts = configure(monkeypatch, response=lambda: fix_payload(issue))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "insufficient_context"
    assert prompts == []


def test_invalid_head_snapshot_means_no_source_or_llm_call(monkeypatch):
    issue = make_issue()
    source_calls, prompts = configure(monkeypatch, response=lambda: fix_payload(issue))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha="not-a-sha"))

    assert result.outcomes[0].status == "insufficient_context"
    assert source_calls == [] and prompts == []


@pytest.mark.parametrize("path", ["src/client.js", "../escape.py", "/absolute/file.py", "src\\runner.py"])
def test_unsupported_or_unsafe_path_is_rejected_without_retrieval(monkeypatch, path):
    issue = make_issue(path=path)
    source_calls, prompts = configure(monkeypatch, response=lambda: fix_payload(issue))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "insufficient_context"
    assert source_calls == [] and prompts == []


def test_removed_file_is_not_retrieved_or_sent_to_llm(monkeypatch):
    issue = make_issue()
    source_calls, prompts = configure(monkeypatch, response=lambda: fix_payload(issue))

    result = asyncio.run(fix_generation.generate_suggested_fixes(
        [issue], owner="a", repo="b", head_sha=HEAD_SHA,
        diff_files=[{"path": issue.file_path, "status": "removed", "hunks": []}],
    ))

    assert result.outcomes[0].status == "insufficient_context"
    assert source_calls == [] and prompts == []


def test_oversized_source_context_is_rejected_before_llm_call(monkeypatch):
    issue = make_issue()
    oversized_source = SOURCE + ("# x\n" * (fix_generation.MAX_SOURCE_BYTES // 4 + 1))
    _, prompts = configure(monkeypatch, source=oversized_source, response=lambda: fix_payload(issue))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "insufficient_context"
    assert prompts == []


def test_oversized_prompt_context_is_rejected_before_llm_call(monkeypatch):
    issue = make_issue()
    _, prompts = configure(monkeypatch, response=lambda: fix_payload(issue))
    monkeypatch.setattr("app.ai_review.fix_prompts.MAX_FIX_PROMPT_CHARS", 10)

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "insufficient_context"
    assert prompts == []


@pytest.mark.parametrize("raw", ["not json", json.dumps({"wrong": []}), json.dumps({"fix": {"unexpected": 1}})])
def test_malformed_json_or_shape_is_a_failed_outcome(monkeypatch, raw):
    issue = make_issue()
    configure(monkeypatch, response=raw)

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "failed"
    assert result.fixes == []


@pytest.mark.parametrize("bad", [
    {"confidence": 1.2}, {"language": "javascript"}, {"file_blob_sha": "fake"},
    {"limitations": ["x" * 501]}, {"proposed_code": "x" * 20_001},
])
def test_invalid_suggested_fix_fields_are_rejected(monkeypatch, bad):
    issue = make_issue()
    configure(monkeypatch, response=lambda: fix_payload(issue, **bad))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "failed"
    assert result.fixes == []


def test_provider_failure_is_reported_without_exposing_details(monkeypatch):
    issue = make_issue()
    configure(monkeypatch, response=lambda: (_ for _ in ()).throw(AIReviewUnavailable("private provider detail")))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "failed"
    assert "private provider detail" not in result.outcomes[0].reason


def test_provider_timeout_is_bounded_and_reported(monkeypatch):
    issue = make_issue()
    source_calls, _ = configure(monkeypatch, response=None)
    monkeypatch.setattr(fix_generation, "MAX_PROVIDER_CALL_SECONDS", 0.01)

    async def wait_forever(_system, _prompt):
        await asyncio.Event().wait()

    monkeypatch.setattr(fix_generation, "complete_json", wait_forever)
    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "failed"
    assert result.llm_call_count == 1
    assert len(source_calls) == 1


def test_source_fetch_timeout_is_bounded_without_llm_call(monkeypatch):
    issue = make_issue()
    _, prompts = configure(monkeypatch, response=lambda: fix_payload(issue))
    monkeypatch.setattr(fix_generation, "MAX_SOURCE_FETCH_SECONDS", 0.01)

    async def wait_forever(_owner, _repo, _path, _ref):
        await asyncio.Event().wait()

    monkeypatch.setattr(fix_generation, "get_file_content", wait_forever)
    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "insufficient_context"
    assert result.llm_call_count == 0
    assert prompts == []


def test_model_declining_to_propose_is_not_an_empty_fix(monkeypatch):
    issue = make_issue()
    configure(monkeypatch, response={"fix": None})

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "no_proposal"
    assert result.outcomes[0].fix is None
    assert result.fixes == []


def test_unconfigured_provider_is_unavailable_without_source_or_llm_calls(monkeypatch):
    issue = make_issue()
    source_calls, prompts = configure(monkeypatch, configured=False, response=lambda: fix_payload(issue))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue], owner="a", repo="b", head_sha=HEAD_SHA))

    assert result.outcomes[0].status == "unavailable"
    assert source_calls == [] and prompts == []


def test_partial_success_preserves_valid_proposal_when_another_issue_fails(monkeypatch):
    first = make_issue("issue-first", line=10)
    second = make_issue("issue-second", line=10)
    responses = iter([fix_payload(first), AIReviewUnavailable("secret")])
    source_calls, _ = configure(monkeypatch, response=lambda: next(responses))

    result = asyncio.run(fix_generation.generate_suggested_fixes(
        [first, second], owner="a", repo="b", head_sha=HEAD_SHA,
    ))

    assert [outcome.status for outcome in result.outcomes] == ["generated", "failed"]
    assert [fix.canonical_issue_id for fix in result.fixes] == ["issue-first"]
    assert result.source_fetch_count == 1
    assert len(source_calls) == 1


def test_issue_limit_bounds_source_and_llm_calls(monkeypatch):
    issues = [make_issue(f"issue-{index}", line=10) for index in range(fix_generation.MAX_FIXES_PER_REQUEST + 2)]
    source_calls, prompts = configure(monkeypatch, response=lambda: fix_payload(issues[0]))

    result = asyncio.run(fix_generation.generate_suggested_fixes(issues, owner="a", repo="b", head_sha=HEAD_SHA))

    assert len(result.outcomes) == len(issues)
    assert sum(outcome.status == "skipped_limit" for outcome in result.outcomes) == 2
    assert result.llm_call_count == fix_generation.MAX_FIX_LLM_CALLS
    assert len(prompts) == fix_generation.MAX_FIX_LLM_CALLS
    assert result.source_fetch_count == 1 and len(source_calls) == 1


def test_duplicate_issue_ids_are_not_ambiguously_associated(monkeypatch):
    issue = make_issue()
    _, prompts = configure(monkeypatch, response=lambda: fix_payload(issue))

    result = asyncio.run(fix_generation.generate_suggested_fixes([issue, issue.model_copy(deep=True)], owner="a", repo="b", head_sha=HEAD_SHA))

    assert [outcome.status for outcome in result.outcomes] == ["failed", "failed"]
    assert prompts == []
