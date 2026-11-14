# AI review module

This module reviews supplied unified diffs and returns validated `Finding`
objects. The shared orchestrator calls it through `run_ai_review`, and the
review API connects GitHub pull request URLs to that orchestrator.

## Interface

Call `review_changed_files(changed_files, provider=None)` from `reviewer.py`.

- `changed_files` is a mapping of repository-relative file paths to unified
  diff text.
- `provider` is optional. Tests can pass a fake provider that implements
  `generate(system_prompt, user_prompt) -> str`.
- The result is a list of the shared `Finding` model.

For the PR ingestion payload, call `review_pull_request_payload(payload, provider=None)`.
It reads the top-level `files` list and each file's `filename` and `patch`.
Files without a patch are skipped because `source_content` alone does not show
which lines changed. The call rejects `files_truncated: true` and fails if no
reviewable patches remain.

The shared integration entry points are `run_ai_review(payload, provider=None)`
and `run_review(payload, provider=None)` in `app.services.review_orchestrator`.
The first returns shared `Finding` objects. The second maps them into the
frontend's existing metadata and finding fields. Neither replaces or relabels
static-analysis findings.

The mounted endpoint is `POST /api/reviews/analyze` with a JSON body containing
`{"url": "https://github.com/owner/repo/pull/123"}`. It fetches the public PR
metadata and changed-file patches, then returns the frontend's existing review
response shape. Set `GITHUB_TOKEN` optionally to increase the GitHub API rate
limit; AI provider settings remain required to complete a live review.

AI findings are marked with `source="ai"` and
`verification_status="unverified"`. A finding must refer to a supplied path
and include evidence found in the supplied diff. A line number is accepted
only when it refers to an added line in that diff.

## Provider configuration

The default OpenAI-compatible provider uses these environment variables in the
backend process:

- `LLM_API_KEY` and `LLM_MODEL` — required for the default provider.
- `LLM_API_BASE_URL` — optional; defaults to the OpenAI-compatible Chat
  Completions endpoint.
- `PR_MENTOR_HTTP_TIMEOUT` — optional HTTP timeout, defaulting to 15 seconds.

To specialize models by task, set `LLM_PROVIDER_<TASK>` to a named provider
and `LLM_MODEL_<TASK>_<PROVIDER>` to its model. For example:

```dotenv
LLM_PROVIDER_CODE_REVIEW=reviewer
LLM_MODEL_CODE_REVIEW_REVIEWER=review-model
LLM_PROVIDER_FIX_GENERATION=coder
LLM_MODEL_FIX_GENERATION_CODER=code-model
LLM_PROVIDER_ISSUE_EXPLANATION=teacher
LLM_MODEL_ISSUE_EXPLANATION_TEACHER=explanation-model
LLM_REVIEWER_API_KEY=...
LLM_REVIEWER_API_BASE_URL=https://provider.example/v1/chat/completions
LLM_CODER_API_KEY=...
LLM_CODER_API_BASE_URL=https://provider.example/v1/chat/completions
LLM_TEACHER_API_KEY=...
LLM_TEACHER_API_BASE_URL=https://provider.example/v1/chat/completions
```

The task suffixes are `CODE_REVIEW`, `FIX_GENERATION`, `TEST_GENERATION`,
`ISSUE_EXPLANATION`, and `SEMANTIC_REVIEW`. Each named provider uses
`LLM_<PROVIDER>_API_KEY` and optionally
`LLM_<PROVIDER>_API_BASE_URL`; its shared model is
`LLM_<PROVIDER>_MODEL`. Task-specific fallbacks can be listed in priority order
with `LLM_FALLBACK_PROVIDERS_<TASK>` (comma-separated provider names). Global
`LLM_PROVIDER`, `LLM_FALLBACK_PROVIDERS`, and `LLM_MODEL` values remain the
fallback configuration. Provider endpoints must use HTTPS, except localhost
development endpoints. Keep API keys in deployment secrets, not committed
environment files.

The module reads environment variables directly. It does not load a `.env`
file automatically.

## Tests

From the repository root, run:

```powershell
python -m pytest backend\tests -q
```
