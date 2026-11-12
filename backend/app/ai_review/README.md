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

The default provider uses an OpenAI-compatible Chat Completions endpoint. Set
these environment variables in the process running the backend:

- `AI_API_KEY` — required; keep the value secret.
- `AI_MODEL` — required; the model name for your provider.
- `AI_BASE_URL` — optional; defaults to `https://api.openai.com/v1`.
- `AI_TIMEOUT_SECONDS` — optional; defaults to `30`.

The module reads environment variables directly. It does not load a `.env`
file automatically.

## Tests

From the repository root, run:

```powershell
python -m pytest backend\tests -q
```
