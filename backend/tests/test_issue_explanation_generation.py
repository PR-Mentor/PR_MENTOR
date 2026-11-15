import asyncio
import json

import pytest

from app.ai_review import issue_explanations as service
from app.ai_review.client import AIReviewUnavailable
from app.schemas.finding import ReviewFinding
from app.schemas.issue import CanonicalIssue, FindingObservation


def _finding(*, finding_id="finding-1", source="static_analysis", description="Unsafe use of a shell command.", evidence=None, line=4):
    return ReviewFinding(
        id=finding_id, title="Unsafe command", description=description,
        file_path="src/runner.py", line_start=line, line_end=line,
        severity="high", category="security", confidence=0.9, source=source,
        verification_status="needs_review", evidence=evidence or ["subprocess.run(command, shell=True)"],
        why_it_matters="Original note", suggested_fix="Original suggested fix.",
        learning_explanation="Original learning note.", limitations=[],
    )


def _issue(issue_id="issue-1", observations=None, line=4):
    return CanonicalIssue(
        issue_id=issue_id, file_path="src/runner.py", line_start=line, line_end=line,
        category="security", severity="high", normalized_description="Untrusted input reaches shell execution.",
        observations=observations if observations is not None else [FindingObservation(
            original_finding=_finding(), tool_name="ruff", rule_id="S602",
        )],
    )


def _explanation(**overrides):
    result = {
        "summary": "Untrusted data may be interpreted as shell syntax.",
        "why_it_matters": "An attacker may cause a command to run with the application's permissions.",
        "root_cause": "The command is passed through a shell instead of as separate arguments.",
        "correction": "Pass the executable and each argument separately, and validate expected values.",
        "concept": "Argument vectors keep command structure separate from data.",
        "learning_resources": [],
        "caveats": ["The surrounding command construction is not fully shown."],
    }
    result.update(overrides)
    return result


def _response(*issues):
    return json.dumps({"explanations": [
        {"issue_id": issue.issue_id, "explanation": _explanation()}
        for issue in issues
    ]})


def _decode_prompt(prompt):
    return json.loads(prompt.split("\n", 1)[1])


def _stub_validated_completion(monkeypatch, completion):
    """Stub provider text while retaining the production response validator."""
    async def complete_validated_json(task, system_prompt, user_prompt, validator, **kwargs):
        raw = await completion(system_prompt, user_prompt)
        return raw, validator(raw)

    monkeypatch.setattr(service, "complete_validated_json", complete_validated_json)


def test_generates_one_explanation_for_issue_with_multiple_observations(monkeypatch):
    issue = _issue(observations=[
        FindingObservation(original_finding=_finding(finding_id="ruff-1"), tool_name="ruff", rule_id="S602"),
        FindingObservation(original_finding=_finding(finding_id="ai-1", source="ai_candidate", description="Untrusted input reaches a shell.", evidence=["shell=True"]), tool_name="ai_review", provider_name="openai_compatible"),
    ])
    calls = []

    async def complete(system_prompt, user_prompt):
        calls.append((system_prompt, user_prompt))
        return _response(issue)

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue]))

    assert list(result.explanations) == [issue.issue_id]
    assert result.batch_count == 1
    assert len(calls) == 1
    assert "CanonicalIssue" in calls[0][0]
    context = _decode_prompt(calls[0][1])["issues"]
    assert len(context) == 1
    assert len(context[0]["observations"]) == 2
    assert context[0]["observations"][0]["tool_name"] == "ruff"
    assert context[0]["observations"][0]["rule_id"] == "S602"
    assert context[0]["observations"][1]["source"] == "ai_candidate"
    assert context[0]["observations"][1]["provider_name"] == "openai_compatible"
    assert context[0]["observations"][0]["evidence"] == ["subprocess.run(command, shell=True)"]


def test_multiple_issues_share_a_bounded_batch_and_map_by_id(monkeypatch):
    issues = [_issue(f"issue-{index}") for index in range(2)]
    calls = []

    async def complete(_system, prompt):
        calls.append(prompt)
        context = _decode_prompt(prompt)["issues"]
        return json.dumps({"explanations": [
            {"issue_id": item["issue_id"], "explanation": _explanation(summary=f"Summary for {item['issue_id']}.")}
            for item in reversed(context)
        ]})

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations(issues))

    assert len(calls) == 1
    assert len(_decode_prompt(calls[0])["issues"]) == 2
    assert result.explanations["issue-0"].summary == "Summary for issue-0."
    assert result.explanations["issue-1"].summary == "Summary for issue-1."


def test_empty_issues_do_not_make_an_llm_request(monkeypatch):
    async def unexpected(*_args):
        raise AssertionError("no request should be made")

    _stub_validated_completion(monkeypatch, unexpected)
    result = asyncio.run(service.generate_issue_explanations([]))
    assert result.batch_count == 0
    assert result.explanations == {}


def test_ambiguous_canonical_issue_ids_are_not_sent_or_mapped(monkeypatch):
    async def unexpected(*_args):
        raise AssertionError("ambiguous issue IDs must not be sent to the model")

    _stub_validated_completion(monkeypatch, unexpected)
    result = asyncio.run(service.generate_issue_explanations([_issue("duplicate"), _issue("duplicate")]))

    assert result.explanations == {}
    assert result.batch_count == 0
    assert result.context_failure_count == 2


def test_relevant_code_context_is_selected_and_prompted(monkeypatch):
    issue = _issue(line=3)
    source = "line one\nline two\ncommand = build_command(user_input)\nline four\nline five\n"
    prompts = []

    async def complete(_system, prompt):
        prompts.append(prompt)
        return json.dumps({"explanations": [{
            "issue_id": issue.issue_id,
            "explanation": _explanation(code_example={"before": "run(command)", "after": "run([program, arg])", "language": "python"}),
        }]})

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue], source_files=[("src/runner.py", source)]))

    context = _decode_prompt(prompts[0])["issues"][0]
    assert context["code_context_source"] == "current_source"
    assert "command = build_command(user_input)" in context["code_context"]
    assert result.explanations[issue.issue_id].code_example.after == "run([program, arg])"


def test_matching_diff_hunk_supplies_context_when_full_source_is_unavailable(monkeypatch):
    issue = _issue(line=4)
    diff_files = [{
        "path": "src/runner.py",
        "hunks": [{"lines": [
            {"kind": "context", "line": 3, "text": "args = []"},
            {"kind": "added", "line": 4, "text": "subprocess.run(command, shell=True)"},
        ]}],
    }]
    prompts = []

    async def complete(_system, prompt):
        prompts.append(prompt)
        return _response(issue)

    _stub_validated_completion(monkeypatch, complete)
    asyncio.run(service.generate_issue_explanations([issue], diff_files=diff_files))

    context = _decode_prompt(prompts[0])["issues"][0]
    assert context["code_context_source"] == "pull_request_diff"
    assert "4: +subprocess.run(command, shell=True)" in context["code_context"]


def test_missing_code_context_drops_unfounded_code_example_while_preserving_explanation(monkeypatch):
    issue = _issue(line=400)

    async def complete(_system, _prompt):
        return json.dumps({"explanations": [{
            "issue_id": issue.issue_id,
            "explanation": _explanation(code_example={"before": "invented()", "after": "fixed()"}),
        }]})

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue]))

    assert issue.issue_id in result.explanations
    assert result.explanations[issue.issue_id].code_example is None
    assert result.rejected_explanation_count == 0


def test_invalid_learning_resource_is_dropped_while_preserving_explanation(monkeypatch):
    issue = _issue()

    async def complete(_system, _prompt):
        return json.dumps({"explanations": [{
            "issue_id": issue.issue_id,
            "explanation": _explanation(learning_resources=[
                {"title": "Untrusted", "url": "https://evil.example/malicious"},
                {"title": "Valid Docs", "url": "https://docs.python.org/3/library/subprocess.html"},
            ]),
        }]})

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue]))

    assert issue.issue_id in result.explanations
    resources = result.explanations[issue.issue_id].learning_resources
    assert len(resources) == 1
    assert resources[0].title == "Valid Docs"
    assert resources[0].url == "https://docs.python.org/3/library/subprocess.html"
    assert result.rejected_explanation_count == 0


def test_invalid_optional_code_example_is_dropped_while_preserving_explanation(monkeypatch):
    issue = _issue(line=4)
    diff_files = [{
        "path": "src/runner.py",
        "hunks": [{"lines": [{"kind": "context", "line": 4, "text": "subprocess.run(cmd)"}]}],
    }]

    async def complete(_system, _prompt):
        return json.dumps({"explanations": [{
            "issue_id": issue.issue_id,
            "explanation": _explanation(code_example={"before": "only before side"}),  # Missing 'after'
        }]})

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue], diff_files=diff_files))

    assert issue.issue_id in result.explanations
    assert result.explanations[issue.issue_id].code_example is None
    assert result.rejected_explanation_count == 0


def test_harmless_extra_fields_at_all_levels_are_tolerated(monkeypatch):
    issue = _issue()

    async def complete(_system, _prompt):
        return json.dumps({
            "status": "success",
            "model_version": "gpt-4o",
            "notes": "Generated with high confidence.",
            "explanations": [{
                "issue_id": issue.issue_id,
                "category": "security",
                "file_path": "src/runner.py",
                "explanation": _explanation(
                    title="Shell Injection vulnerability",
                    confidence="high",
                    tags=["security", "cwe-78"],
                ),
            }],
        })

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue]))

    assert issue.issue_id in result.explanations
    assert result.explanations[issue.issue_id].summary == "Untrusted data may be interpreted as shell syntax."
    assert result.rejected_explanation_count == 0


def test_single_issue_response_unwrapped_formats(monkeypatch):
    issue = _issue()

    # Format A: {"issue_id": "...", "explanation": {...}}
    async def complete_with_id(_system, _prompt):
        return json.dumps({
            "issue_id": issue.issue_id,
            "explanation": _explanation(summary="Unwrapped single issue with ID."),
        })

    _stub_validated_completion(monkeypatch, complete_with_id)
    result_a = asyncio.run(service.generate_issue_explanations([issue]))
    assert issue.issue_id in result_a.explanations
    assert result_a.explanations[issue.issue_id].summary == "Unwrapped single issue with ID."

    # Format B: {"explanation": {...}} with implicit single expected ID
    async def complete_implicit(_system, _prompt):
        return json.dumps({
            "explanation": _explanation(summary="Unwrapped single issue implicit ID."),
        })

    _stub_validated_completion(monkeypatch, complete_implicit)
    result_b = asyncio.run(service.generate_issue_explanations([issue]))
    assert issue.issue_id in result_b.explanations
    assert result_b.explanations[issue.issue_id].summary == "Unwrapped single issue implicit ID."


def test_markdown_code_fence_wrapper_is_tolerated(monkeypatch):
    issue = _issue()

    async def complete(_system, _prompt):
        inner = json.dumps({"explanations": [{"issue_id": issue.issue_id, "explanation": _explanation()}]})
        return f"```json\n{inner}\n```"

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue]))

    assert issue.issue_id in result.explanations
    assert result.rejected_explanation_count == 0


def test_one_invalid_explanation_preserves_valid_explanations_in_same_batch(monkeypatch):
    issue_a = _issue("issue-a")
    issue_b = _issue("issue-b")
    issue_c = _issue("issue-c")

    async def complete(_system, _prompt):
        return json.dumps({"explanations": [
            {"issue_id": "issue-a", "explanation": _explanation(summary="Valid summary A.")},
            {"issue_id": "issue-b", "explanation": {"summary": "missing other required fields"}},
            {"issue_id": "issue-unknown", "explanation": _explanation(summary="Unknown issue.")},
            {"issue_id": "issue-c", "explanation": _explanation(summary="Valid summary C.")},
        ]})

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue_a, issue_b, issue_c]))

    # Valid explanations for known issues are preserved
    assert set(result.explanations) == {"issue-a", "issue-c"}
    assert result.explanations["issue-a"].summary == "Valid summary A."
    assert result.explanations["issue-c"].summary == "Valid summary C."
    # issue-b had missing required fields (rejected=1), issue-unknown was unknown (rejected=1)
    assert result.rejected_explanation_count == 2
    assert result.failed_batch_count == 0


def test_partial_response_keeps_valid_explanations_and_omits_missing_ones(monkeypatch):
    issues = [_issue("issue-a"), _issue("issue-b")]

    async def complete(_system, _prompt):
        return json.dumps({"explanations": [{"issue_id": "issue-a", "explanation": _explanation()}]})

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations(issues))

    assert set(result.explanations) == {"issue-a"}
    assert result.failed_batch_count == 0


def test_unknown_duplicate_and_malformed_issue_entries_are_rejected(monkeypatch):
    issue = _issue("issue-known")
    valid = {"issue_id": "issue-known", "explanation": _explanation()}
    duplicate = {"issue_id": "issue-known", "explanation": _explanation()}
    entries = [valid, {"issue_id": "unknown", "explanation": _explanation()}, duplicate, duplicate,
               {"issue_id": "issue-known", "explanation": {"summary": "incomplete"}}]

    async def complete(_system, _prompt):
        return json.dumps({"explanations": entries})

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue]))

    assert result.explanations == {}  # Duplicate known IDs invalidate that issue's result.
    assert result.rejected_explanation_count == 5


@pytest.mark.parametrize("payload", [
    "not-json",
    json.dumps({"other": []}),
    json.dumps({"explanations": "wrong type"}),
    json.dumps({"explanations": [{"issue_id": "issue-1", "explanation": {"summary": "missing fields"}}]}),
    json.dumps({"explanations": [{"issue_id": "issue-1", "explanation": "not a dict"}]}),
    json.dumps({"explanations": [{"issue_id": "issue-1", "explanation": _explanation(summary="x" * 2_001)}]}),
])
def test_malformed_or_invalid_batch_is_rejected_without_raising(monkeypatch, payload):
    async def complete(_system, _prompt):
        return payload

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([_issue()]))

    assert result.explanations == {}
    assert result.failed_batch_count == 1 or result.rejected_explanation_count == 1


@pytest.mark.parametrize("message", [
    "AI provider is not configured.",
    "AI provider returned HTTP 503.",
    "AI provider did not respond in time.",
    "AI provider is temporarily unavailable.",
])
def test_provider_failures_are_isolated_to_the_failed_batch(monkeypatch, message):
    async def complete(_system, _prompt):
        raise AIReviewUnavailable(message)

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([_issue()]))

    assert result.explanations == {}
    assert result.failed_batch_count == 1
    assert result.failure_reasons == [message]


def test_batching_caps_issues_per_request_and_continues_after_a_failed_batch(monkeypatch):
    issues = [_issue(f"issue-{index}") for index in range(service.MAX_ISSUES_PER_BATCH + 1)]
    calls = []

    async def complete(_system, prompt):
        current = _decode_prompt(prompt)["issues"]
        calls.append(current)
        if len(calls) == 1:
            raise AIReviewUnavailable("AI provider returned HTTP 503.")
        return json.dumps({"explanations": [
            {"issue_id": item["issue_id"], "explanation": _explanation()}
            for item in current
        ]})

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations(issues))

    assert [len(batch) for batch in calls] == [service.MAX_ISSUES_PER_BATCH, 1]
    assert result.batch_count == 2
    assert result.failed_batch_count == 1
    assert len(result.explanations) == 1


def test_prompt_and_response_limits_are_bounded_without_json_truncation():
    assert service.MAX_EXPLANATION_REQUEST_CHARS == 32_000
    assert service.MAX_EXPLANATION_RESPONSE_CHARS == 96_000
    with pytest.raises(ValueError):
        service.build_issue_explanation_prompt([{"issue_id": "issue-1", "context": "x" * 32_000}])


def test_oversized_provider_response_is_rejected(monkeypatch):
    async def complete(_system, _prompt):
        return " " * (service.MAX_EXPLANATION_RESPONSE_CHARS + 1)

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([_issue()]))

    assert result.explanations == {}
    assert result.failed_batch_count == 1
    assert "oversized" in result.failure_reasons[0]


def test_prompt_requires_explicit_uncertainty_and_rejects_unfounded_claims():
    from app.ai_review.prompts import CANONICAL_ISSUE_EXPLANATION_GUIDANCE

    assert "Distinguish confirmed evidence, reasonable interpretation, and unknowns." in CANONICAL_ISSUE_EXPLANATION_GUIDANCE
    assert "If evidence is insufficient, say what is uncertain in caveats." in CANONICAL_ISSUE_EXPLANATION_GUIDANCE
    assert "Do not state a root cause" in CANONICAL_ISSUE_EXPLANATION_GUIDANCE


def test_realistic_frontier_provider_response_with_markdown_and_mixed_resources(monkeypatch):
    issue = _issue(line=4)
    source = "line 1\nline 2\nline 3\nsubprocess.run(command, shell=True)\nline 5\n"

    # Realistic payload returned by frontier models (markdown fenced, usage stats, extra keys, mixed URLs)
    realistic_payload = """```json
{
  "id": "chatcmpl-9A8B7C",
  "object": "chat.completion",
  "created": 1731512400,
  "model": "gpt-4o",
  "notes": "Reviewed security implications of shell=True.",
  "explanations": [
    {
      "issue_id": "issue-1",
      "title": "OS Command Injection Risk",
      "category": "security",
      "severity": "high",
      "explanation": {
        "summary": "Passing untrusted input with shell=True allows arbitrary shell execution.",
        "why_it_matters": "An attacker who controls user_input can run arbitrary commands with host privileges.",
        "root_cause": "The subprocess.run call enables the shell parser on untrusted command strings.",
        "correction": "Pass arguments as a sequence without shell=True, and validate arguments strictly.",
        "concept": "Separating arguments from shell syntax prevents injection attacks.",
        "severity_level": "high",
        "learning_resources": [
          {"title": "Python subprocess security", "url": "https://docs.python.org/3/library/subprocess.html", "description": "Security considerations for subprocess."},
          {"title": "Random Blog Post", "url": "https://random-blog.example/shell-injection"},
          {"title": "StackOverflow Thread", "url": "https://stackoverflow.com/questions/12345?ref=tracker"}
        ],
        "caveats": [
          "The surrounding input validation could not be determined from the available diff.",
          "   "
        ],
        "code_example": {
          "before": "subprocess.run(cmd, shell=True)",
          "after": "subprocess.run(['prog', arg], check=True)",
          "language": "python"
        }
      }
    }
  ]
}
```"""

    async def complete(_system, _prompt):
        return realistic_payload

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue], source_files=[("src/runner.py", source)]))

    assert issue.issue_id in result.explanations
    explanation = result.explanations[issue.issue_id]
    assert explanation.summary.startswith("Passing untrusted input")
    assert explanation.code_example is not None
    assert explanation.code_example.after == "subprocess.run(['prog', arg], check=True)"
    # Only the approved python docs URL was preserved; untrusted / query-string URLs were safely dropped
    assert len(explanation.learning_resources) == 1
    assert explanation.learning_resources[0].url == "https://docs.python.org/3/library/subprocess.html"
    # Blank caveat was safely stripped
    assert len(explanation.caveats) == 1
    assert result.rejected_explanation_count == 0
    assert result.failed_batch_count == 0


def test_realistic_frontier_provider_response_without_source_context_drops_code_example(monkeypatch):
    issue = _issue(line=999)  # Line not in source context

    realistic_payload = """```json
{
  "explanations": [
    {
      "issue_id": "issue-1",
      "explanation": {
        "summary": "Shell injection is possible when executing commands with shell=True.",
        "why_it_matters": "Arbitrary command execution.",
        "root_cause": "Shell expansion is active.",
        "correction": "Use argument list.",
        "concept": "Command separation.",
        "learning_resources": [],
        "caveats": [],
        "code_example": {
          "before": "subprocess.run(cmd, shell=True)",
          "after": "subprocess.run(['sh', cmd])"
        }
      }
    }
  ]
}
```"""

    async def complete(_system, _prompt):
        return realistic_payload

    _stub_validated_completion(monkeypatch, complete)
    result = asyncio.run(service.generate_issue_explanations([issue]))

    # Valid explanation preserved, ungrounded code example safely stripped to None
    assert issue.issue_id in result.explanations
    explanation = result.explanations[issue.issue_id]
    assert explanation.code_example is None
    assert result.rejected_explanation_count == 0
