"""Tests for the asynchronous, LLM_API_KEY-based AI review path."""

import asyncio
import json

import pytest

from app.ai_review import client, reviewer
from app.ai_review.client import AIReviewUnavailable
from app.ai_review.prompts import SYSTEM_PROMPT, build_user_prompt
from app.ai_review.providers import ProviderConfig


def test_ai_is_unavailable_without_credentials(monkeypatch):
    monkeypatch.setattr(
        client,
        "llm_configuration",
        lambda: (None, None, "https://api.openai.com/v1/chat/completions"),
    )

    with pytest.raises(AIReviewUnavailable, match="LLM_API_KEY and LLM_MODEL"):
        asyncio.run(client.complete_json("system", "user"))


def test_prompt_wraps_context_as_untrusted_json():
    prompt = build_user_prompt(
        {"title": "Example"},
        [{"filename": "src/example.py", "patch": "+return 1"}],
        [],
    )

    assert "<untrusted_pr_json>" in prompt
    assert '"title": "Example"' in prompt
    assert "untrusted pr context" in prompt.lower()
    assert "Do not claim tests were run" in SYSTEM_PROMPT


def _candidate(**overrides):
    finding = {
        "title": "Possible bug",
        "description": "Check this branch.",
        "file_path": "src/example.py",
        "line_start": None,
        "line_end": None,
        "severity": "medium",
        "category": "bug",
        "confidence": 0.62,
        "evidence": ["if value is None: raise ValueError"],
        "why_it_matters": "The alternate case may be skipped.",
        "suggested_fix": "Add a case-specific test.",
        "learning_explanation": "Review both paths.",
        "limitations": ["Candidate requires human review."],
    }
    finding.update(overrides)
    return finding


def test_reviewer_validates_candidates_and_marks_them_unverified(monkeypatch):
    async def complete_json(*_args):
        return json.dumps({"findings": [_candidate()]})

    monkeypatch.setattr(reviewer, "complete_json", complete_json)
    files = [{"path": "src/example.py", "hunks": [{"lines": [
        {"kind": "added", "line": 3, "text": "if value is None: raise ValueError"},
    ]}]}]
    findings = asyncio.run(
        reviewer.generate_candidates({"title": "Example"}, files, [])
    )

    assert len(findings) == 1
    assert findings[0].source == "ai_candidate"
    assert findings[0].verification_status == "needs_review"
    assert findings[0].line_start is None


def test_reviewer_rejects_malformed_provider_json(monkeypatch):
    async def complete_json(*_args):
        return "not JSON"

    monkeypatch.setattr(reviewer, "complete_json", complete_json)

    with pytest.raises(AIReviewUnavailable, match="invalid JSON"):
        asyncio.run(reviewer.generate_candidates({}, [], []))


def test_reviewer_rejects_findings_that_do_not_match_the_schema(monkeypatch):
    async def complete_json(*_args):
        return json.dumps({"findings": [_candidate(severity="impossible")]})

    monkeypatch.setattr(reviewer, "complete_json", complete_json)

    with pytest.raises(AIReviewUnavailable, match="did not match the review schema"):
        asyncio.run(reviewer.generate_candidates({}, [], []))


def test_reviewer_schema_failure_falls_back_and_records_actual_provider(monkeypatch):
    import httpx

    from app.ai_review import client

    providers = [
        ProviderConfig("primary", "secret-1", "model-1", "https://primary.example/chat", True, 1),
        ProviderConfig("secondary", "secret-2", "model-2", "https://secondary.example/chat", True, 1),
    ]
    monkeypatch.setattr(client, "provider_registry", lambda _task=None: providers)
    monkeypatch.setattr(reviewer, "provider_registry", lambda _task=None: providers)
    monkeypatch.setattr(client, "llm_configuration", lambda: ("secret-1", "model-1", "https://primary.example/chat"))
    monkeypatch.setenv("LLM_PROVIDER_RETRIES", "0")

    def respond(request):
        content = "{}" if "primary.example" in str(request.url) else '{"findings":[]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    transport = httpx.MockTransport(respond)
    real_client = httpx.AsyncClient

    class RoutedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(client.httpx, "AsyncClient", RoutedClient)

    async def call():
        result = await reviewer.generate_candidates({}, [], [])
        return result, client.last_call_metadata()

    findings, metadata = asyncio.run(call())
    assert findings == []
    assert metadata["provider"] == "secondary"
    assert metadata["fallback_used"] is True
    assert metadata["prompt_version"] == "code_review_v1"
    assert len(metadata["system_prompt_sha256"]) == 64
    assert len(metadata["user_prompt_sha256"]) == 64


def test_user_prompt_strictly_bounded_to_28k_chars_and_produces_valid_json():
    # Simulate a huge PR payload similar to OpenCV PR #31028
    massive_description = "### Checklist\n- [x] Tested\n- [ ] Unfinished\n" + ("| Col 1 | Col 2 |\n| --- | --- |\n| val | val |\n" * 50) + ("Long text paragraph.\n" * 200)
    massive_diff_files = [
        {
            "path": f"src/file_{i}.cpp",
            "status": "modified",
            "hunks": [
                {
                    "header": "@@ -1,50 +1,50 @@",
                    "lines": [{"kind": "added", "line": j, "text": f"line {j} content " + ("x" * 100)} for j in range(1, 51)],
                }
            ],
        }
        for i in range(50)
    ]
    massive_static_findings = [
        {
            "file_path": f"src/file_{i}.cpp",
            "line_start": i,
            "title": f"RULE-{i}",
            "description": f"Diagnostic description for issue {i}",
            "severity": "medium",
            "why_it_matters": "Extremely verbose explanation string " * 20,
            "suggested_fix": "Extremely verbose suggested fix string " * 20,
            "evidence": ["line of evidence " * 10],
            "limitations": ["analyzer limitation " * 10],
        }
        for i in range(100)
    ]

    prompt = build_user_prompt(
        {"title": "Massive PR", "description": massive_description},
        massive_diff_files,
        massive_static_findings,
    )

    assert len(prompt) <= 28_000
    # Must contain valid, uncorrupted JSON inside <untrusted_pr_json>
    start_tag = "<untrusted_pr_json>\n"
    end_tag = "\n</untrusted_pr_json>"
    assert start_tag in prompt
    assert end_tag in prompt
    json_text = prompt[prompt.index(start_tag) + len(start_tag) : prompt.rindex(end_tag)]
    parsed = json.loads(json_text)
    assert isinstance(parsed, dict)
    assert "pull_request" in parsed
    assert "diff_files" in parsed
    assert "static_findings" in parsed
    assert len(parsed["diff_files"]) > 0
    # Confirm markdown table boilerplate was stripped
    desc = parsed["pull_request"].get("description", "")
    assert "| Col 1 |" not in desc
    assert "- [x]" not in desc


def test_user_prompt_guarantees_byte_budget_for_single_oversized_diff():
    from app.ai_review.prompts import estimate_request_body_bytes, MAX_REQUEST_BODY_BYTES

    # 1 file with 1 massive hunk of 500 lines
    massive_file = [
        {
            "path": "src/massive_single_file.cpp",
            "status": "modified",
            "hunks": [
                {
                    "header": "@@ -1,500 +1,500 @@",
                    "lines": [
                        {"kind": "added" if i % 2 == 0 else "context", "line": i, "text": f"code statement {i} " + ("x" * 150)}
                        for i in range(1, 501)
                    ],
                }
            ],
        }
    ]

    prompt = build_user_prompt({"title": "Single Huge Diff"}, massive_file, [])
    body_bytes = estimate_request_body_bytes(prompt)
    assert body_bytes <= MAX_REQUEST_BODY_BYTES, f"Body bytes {body_bytes} exceeds {MAX_REQUEST_BODY_BYTES}"

    # Verify JSON parsed properly and added lines survived compaction
    start_tag = "<untrusted_pr_json>\n"
    end_tag = "\n</untrusted_pr_json>"
    parsed = json.loads(prompt[prompt.index(start_tag) + len(start_tag) : prompt.rindex(end_tag)])
    assert len(parsed["diff_files"]) == 1
    hunk = parsed["diff_files"][0]["hunks"][0]
    # Check that added lines were prioritized
    kinds = [l["kind"] for l in hunk["lines"]]
    assert "added" in kinds


def test_user_prompt_multibyte_utf8_budget_enforced():
    from app.ai_review.prompts import estimate_request_body_bytes, MAX_REQUEST_BODY_BYTES

    # Multi-byte UTF-8 string: emojis and CJK characters (3-4 bytes each)
    multibyte_description = "### 説明\n" + ("✨ 特徴: 行列変換の修正 🚀\n" * 500)
    files = [
        {
            "path": "src/intl.cpp",
            "status": "modified",
            "hunks": [
                {
                    "header": "@@ -1,10 +1,10 @@",
                    "lines": [{"kind": "added", "line": i, "text": "/* 多言語サポート */ auto x = 1;"} for i in range(1, 11)],
                }
            ],
        }
    ]

    prompt = build_user_prompt({"title": "Multibyte UTF-8 PR", "description": multibyte_description}, files, [])
    body_bytes = estimate_request_body_bytes(prompt)
    assert body_bytes <= MAX_REQUEST_BODY_BYTES, f"Multibyte UTF-8 body bytes {body_bytes} exceeds {MAX_REQUEST_BODY_BYTES}"
    parsed = json.loads(prompt[prompt.index("<untrusted_pr_json>\n") + len("<untrusted_pr_json>\n") : prompt.rindex("\n</untrusted_pr_json>")])
    assert parsed["pull_request"]["title"] == "Multibyte UTF-8 PR"


def test_user_prompt_unachievable_budget_raises_explicit_value_error():
    # Setting an impossibly low budget must raise an explicit ValueError rather than returning an oversized body
    with pytest.raises(ValueError, match="exceeds the configured AI request byte budget"):
        build_user_prompt({"title": "Test"}, [{"path": "a.py", "status": "added"}], [], max_request_bytes=500)


def test_reviewer_normalizes_representative_valid_provider_responses(monkeypatch):
    """Verify that common provider quirks (field synonyms, extra metadata, markdown fences,

    severity/category casing, wrapper keys) are normalized while preserving strict schema validation.
    """
    diff_files = [
        {
            "path": "modules/core/include/opencv2/core.hpp",
            "hunks": [
                {
                    "lines": [
                        {"kind": "added", "line": 150, "text": "void* p = new_call();"},
                        {"kind": "added", "line": 151, "text": "int count = 42;"},
                    ]
                }
            ],
        }
    ]

    # Markdown-wrapped response with field synonyms and extra metadata (rule_id, code, etc.)
    provider_raw = """```json
    {
        "findings": [
            {
                "title": "Use of raw pointer allocation",
                "message": "Prefer modern smart pointers over manual memory management",
                "file": "modules/core/include/opencv2/core.hpp",
                "line": "150",
                "rule_id": "CPP-RAW-NEW",
                "severity": "WARNING",
                "category": "STYLE",
                "confidence": "0.85",
                "evidence": ["new_call()"],
                "explanation": "Raw pointer allocation risks leaks if exceptions occur",
                "fix": "std::make_unique<T>()",
                "code": "void* p = new_call();"
            }
        ]
    }
    ```"""

    async def complete_json(*_args, **_kwargs):
        return provider_raw

    monkeypatch.setattr(reviewer, "complete_json", complete_json)

    findings = asyncio.run(reviewer.generate_candidates({}, diff_files, []))
    assert len(findings) == 1
    f = findings[0]
    assert f.file_path == "modules/core/include/opencv2/core.hpp"
    assert f.line_start == 150
    assert f.line_end == 150
    assert f.severity == "medium"  # WARNING mapped to medium
    assert f.category == "code_quality"  # STYLE mapped to code_quality
    assert f.confidence == 0.85
    assert f.suggested_fix == "std::make_unique<T>()"
    assert f.why_it_matters == "Raw pointer allocation risks leaks if exceptions occur"
    assert f.source == "ai_candidate"
    assert f.verification_status == "needs_review"


def test_reviewer_unwraps_top_level_list_and_candidate_keys(monkeypatch):
    """Verify that provider outputs wrapped in a top-level list or 'candidates' key are unwrapped."""
    diff_files = [
        {
            "path": "src/algo.cpp",
            "hunks": [
                {
                    "lines": [
                        {"kind": "added", "line": 20, "text": "int* buffer = (int*)malloc(100);"}
                    ]
                }
            ],
        }
    ]

    # 1. Wrapped in 'candidates'
    async def complete_candidates(*_args, **_kwargs):
        return json.dumps({
            "candidates": [
                {
                    "title": "C-style malloc",
                    "description": "Use new or vector",
                    "file_path": "src/algo.cpp",
                    "line_start": 20,
                    "line_end": 20,
                    "severity": "info",  # mapped to informational
                    "category": "performance",
                    "confidence": 90,  # 90% mapped to 0.9
                    "evidence": ["malloc(100)"],
                    "why_it_matters": "Type safety",
                    "suggested_fix": "std::vector<int>",
                }
            ]
        })

    monkeypatch.setattr(reviewer, "complete_json", complete_candidates)
    findings = asyncio.run(reviewer.generate_candidates({}, diff_files, []))
    assert len(findings) == 1
    assert findings[0].severity == "informational"
    assert findings[0].confidence == 0.9

    # 2. Top-level list
    async def complete_list(*_args, **_kwargs):
        return json.dumps([
            {
                "title": "C-style malloc",
                "description": "Use new or vector",
                "file_path": "src/algo.cpp",
                "line_start": 20,
                "line_end": 20,
                "severity": "low",
                "category": "performance",
                "confidence": 0.9,
                "evidence": ["malloc(100)"],
                "why_it_matters": "Type safety",
                "suggested_fix": "std::vector<int>",
            }
        ])

    monkeypatch.setattr(reviewer, "complete_json", complete_list)
    findings2 = asyncio.run(reviewer.generate_candidates({}, diff_files, []))
    assert len(findings2) == 1
    assert findings2[0].title == "C-style malloc"



