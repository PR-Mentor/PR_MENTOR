import json

import httpx
import pytest
from app.ai_review.client import (
    AIConfigurationError,
    AIProviderConfig,
    AIProviderError,
    OpenAICompatibleProvider,
)
from app.ai_review.prompts import SYSTEM_PROMPT, build_user_prompt
from app.ai_review.reviewer import review_changed_files, review_pull_request_payload


def make_config():
    return AIProviderConfig(
        api_key="test-key",
        model="test-model",
        base_url="https://example.test/v1",
        timeout_seconds=5,
    )


def test_missing_api_key_is_reported():
    with pytest.raises(AIConfigurationError, match="AI_API_KEY"):
        AIProviderConfig.from_env({})


def test_missing_model_is_reported():
    with pytest.raises(AIConfigurationError, match="AI_MODEL"):
        AIProviderConfig.from_env({"AI_API_KEY": "test-key"})


def test_generate_sends_request_and_returns_text():
    def handler(request):
        assert request.url == "https://example.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"

        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["temperature"] == 0
        assert payload["messages"] == [
            {"role": "system", "content": "You review code."},
            {"role": "user", "content": "Review this function."},
        ]

        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "  No findings.  "}}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(make_config(), http_client=client)
        result = provider.generate("You review code.", "Review this function.")

    assert result == "No findings."


def test_timeout_becomes_provider_error():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(make_config(), http_client=client)

        with pytest.raises(AIProviderError, match="timed out"):
            provider.generate("System prompt.", "User prompt.")


def test_http_error_becomes_provider_error():
    def handler(request):
        return httpx.Response(503, json={"error": "unavailable"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(make_config(), http_client=client)

        with pytest.raises(AIProviderError, match="HTTP 503"):
            provider.generate("System prompt.", "User prompt.")


def test_malformed_json_becomes_provider_error():
    def handler(request):
        return httpx.Response(200, content=b"not JSON")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(make_config(), http_client=client)

        with pytest.raises(AIProviderError, match="invalid JSON"):
            provider.generate("System prompt.", "User prompt.")


def test_unexpected_response_shape_becomes_provider_error():
    def handler(request):
        return httpx.Response(200, json={"choices": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(make_config(), http_client=client)

        with pytest.raises(AIProviderError, match="did not contain a chat message"):
            provider.generate("System prompt.", "User prompt.")


def test_empty_response_becomes_provider_error():
    def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "  "}}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(make_config(), http_client=client)

        with pytest.raises(AIProviderError, match="empty response"):
            provider.generate("System prompt.", "User prompt.")

def test_build_user_prompt_includes_supplied_path_and_text():
    prompt = build_user_prompt({"src/example.py": "return 10 / count"})

    payload = json.loads(prompt.split("\n\n", 1)[1])
    assert payload == {
        "files": [
            {
                "file_path": "src/example.py",
                "text": "return 10 / count",
            }
        ]
    }


def test_build_user_prompt_rejects_empty_files():
    with pytest.raises(ValueError, match="At least one"):
        build_user_prompt({})


def test_build_user_prompt_rejects_empty_path():
    with pytest.raises(ValueError, match="non-empty path"):
        build_user_prompt({"": "some code"})


def test_system_prompt_requires_supported_references_and_json():
    assert "Do not invent" in SYSTEM_PROMPT
    assert "exactly matches one supplied" in SYSTEM_PROMPT
    assert "valid JSON object" in SYSTEM_PROMPT

class FakeProvider:
    def __init__(self, response):
        self.response = response

    def generate(self, system_prompt, user_prompt):
        return self.response


def sample_diff():
    return """\
diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -1,0 +1,1 @@
+return 10 / count
"""


def sample_finding(**overrides):
    finding = {
        "file_path": "src/example.py",
        "line": 1,
        "rule_id": None,
        "severity": "high",
        "message": "The divisor could be zero",
        "evidence": "return 10 / count",
        "explanation": "If count is zero, this division raises an error.",
        "suggested_fix": "Check that count is not zero before dividing.",
    }
    finding.update(overrides)
    return finding


def test_reviewer_returns_validated_unverified_ai_finding():
    provider = FakeProvider(
        json.dumps({"findings": [sample_finding()]})
    )

    findings = review_changed_files(
        {"src/example.py": sample_diff()},
        provider=provider,
    )

    assert len(findings) == 1
    assert findings[0].file_path == "src/example.py"
    assert findings[0].line == 1
    assert findings[0].source == "ai"
    assert findings[0].verification_status == "unverified"
    assert findings[0].evidence == "return 10 / count"


def test_reviewer_accepts_empty_findings():
    provider = FakeProvider(json.dumps({"findings": []}))

    findings = review_changed_files(
        {"src/example.py": sample_diff()},
        provider=provider,
    )

    assert findings == []


def test_payload_adapter_reviews_ingestion_patch():
    provider = FakeProvider(json.dumps({"findings": [sample_finding()]}))
    payload = {
        "pull_request": {"owner": "owner", "repo": "repo", "number": 42, "title": "Example"},
        "files": [{"filename": "src/example.py", "patch": sample_diff()}],
        "files_truncated": False,
    }

    findings = review_pull_request_payload(payload, provider=provider)

    assert len(findings) == 1
    assert findings[0].file_path == "src/example.py"
    assert findings[0].source == "ai"


def test_payload_adapter_rejects_source_without_a_patch():
    payload = {
        "pull_request": {"owner": "owner", "repo": "repo", "number": 42},
        "files": [
            {
                "filename": "src/example.py",
                "patch": None,
                "source_content": "return 10 / count",
                "content_unavailable_reason": None,
            }
        ],
        "files_truncated": False,
    }

    with pytest.raises(ValueError, match="no reviewable file patches"):
        review_pull_request_payload(payload, provider=FakeProvider(json.dumps({"findings": []})))


def test_payload_adapter_rejects_files_without_text_diffs():
    with pytest.raises(ValueError, match="no reviewable file patches"):
        review_pull_request_payload(
            {
                "pull_request": {"owner": "owner", "repo": "repo", "number": 42},
                "files": [{"filename": "image.png", "patch": None}],
                "files_truncated": False,
            },
            provider=FakeProvider(json.dumps({"findings": []})),
        )


def test_payload_adapter_rejects_duplicate_paths():
    with pytest.raises(ValueError, match="duplicate filename"):
        review_pull_request_payload(
            {
                "pull_request": {"owner": "owner", "repo": "repo", "number": 42},
                "files": [
                    {"filename": "src/example.py", "patch": sample_diff()},
                    {"filename": "src/example.py", "patch": sample_diff()},
                ],
                "files_truncated": False,
            },
            provider=FakeProvider(json.dumps({"findings": []})),
        )


def test_payload_adapter_rejects_truncated_file_list():
    with pytest.raises(ValueError, match="file list is truncated"):
        review_pull_request_payload(
            {
                "pull_request": {"owner": "owner", "repo": "repo", "number": 42},
                "files": [{"filename": "src/example.py", "patch": sample_diff()}],
                "files_truncated": True,
            },
            provider=FakeProvider(json.dumps({"findings": []})),
        )


def test_reviewer_rejects_unsupported_file_path():
    from app.ai_review.client import AIProviderError

    provider = FakeProvider(
        json.dumps({"findings": [sample_finding(file_path="other.py")]})
    )

    with pytest.raises(AIProviderError, match="unsupported file path"):
        review_changed_files(
            {"src/example.py": sample_diff()},
            provider=provider,
        )


def test_reviewer_rejects_line_not_added_in_diff():
    from app.ai_review.client import AIProviderError

    provider = FakeProvider(
        json.dumps({"findings": [sample_finding(line=2)]})
    )

    with pytest.raises(AIProviderError, match="not shown as added"):
        review_changed_files(
            {"src/example.py": sample_diff()},
            provider=provider,
        )


def test_reviewer_rejects_evidence_not_in_supplied_diff():
    from app.ai_review.client import AIProviderError

    provider = FakeProvider(
        json.dumps({"findings": [sample_finding(evidence="invented evidence")]})
    )

    with pytest.raises(AIProviderError, match="not present"):
        review_changed_files(
            {"src/example.py": sample_diff()},
            provider=provider,
        )


def test_reviewer_rejects_invalid_json():
    from app.ai_review.client import AIProviderError

    provider = FakeProvider("not JSON")

    with pytest.raises(AIProviderError, match="not valid JSON"):
        review_changed_files(
            {"src/example.py": sample_diff()},
            provider=provider,
        )
 main


import asyncio
import json

import pytest

from app.ai_review import reviewer
from app.ai_review.client import AIReviewUnavailable


def test_ai_is_honestly_unavailable_without_credentials(monkeypatch):
    from app.ai_review import client

    monkeypatch.setattr(client, "llm_configuration", lambda: (None, None, "https://api.openai.com/v1/chat/completions"))
    with pytest.raises(AIReviewUnavailable, match="not configured"):
        asyncio.run(client.complete_json("system", "user"))


def test_reviewer_validates_provider_findings_and_marks_them_unverified(monkeypatch):
    async def complete(*_args):
        return json.dumps({"findings": [{"title": "Possible bug", "description": "Check this branch.", "file_path": "src/a.py", "line_start": None, "line_end": None, "severity": "medium", "category": "bug", "confidence": 0.62, "evidence": ["The diff adds a conditional."], "why_it_matters": "The alternate case may be skipped.", "suggested_fix": "Add a case-specific test.", "learning_explanation": "Review both paths.", "limitations": ["Candidate requires human review."]}]})

    monkeypatch.setattr(reviewer, "complete_json", complete)
    findings = asyncio.run(reviewer.generate_candidates({"title": "T"}, [], []))
    assert findings[0].source == "ai_candidate"
    assert findings[0].verification_status == "needs_review"
    assert findings[0].line_start is None


def test_reviewer_rejects_malformed_provider_json(monkeypatch):
    async def complete(*_args):
        return "not json"

    monkeypatch.setattr(reviewer, "complete_json", complete)
    with pytest.raises(AIReviewUnavailable, match="invalid JSON"):
        asyncio.run(reviewer.generate_candidates({}, [], []))
 main
