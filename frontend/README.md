# PR Mentor frontend

React + TypeScript application built with Vite.

## Local development

```bash
npm install
npm run dev
```

Set `VITE_API_BASE_URL` in `.env.local` to the FastAPI origin (defaults to `http://localhost:8000`). Start the backend as described in [`../backend/README.md`](../backend/README.md). The default host matches the usual `http://localhost:5173` frontend origin so SameSite session cookies work in local development.

The Discover page asks whether to use demo or live analysis. Demo findings are explicitly sample content from the backend and do not describe the submitted pull request. History, saved links, diffs, and learning data come from API responses. API errors and unavailable backend stages remain visible; this frontend does not invent fallback review results.

Stored reviews open at `/reviews/:reviewId`, which retrieves review data from the existing API and supports direct navigation and refresh. The workspace shows canonical issues and their source observations when present, and falls back to the original findings for legacy reviews. Its full diff action uses the existing review diff view.

The application has a local account gate with registration, sign-in and sign-out. The API stores the session in an HttpOnly cookie; browser requests include credentials. GitHub OAuth and private repository access are not implemented. Personal review history, saved PRs, learning attempts and insight/analytics screens use account-scoped API results. Suggested-fix decisions record user intent only and never write changes to GitHub.

The Developer Profile combines the signed-in account identity, exact live review-record, distinct pull-request, and finding counts, the latest bounded non-demo review window, fix decisions, validation reports, and persisted Learning Lab progress. Demo reviews are excluded from personal analytics; legacy findings without issue lifecycle state are counted as unclassified. The page labels partial history windows and keeps workspace analytics separate. Selecting a recent review opens its full stored findings, explanations, suggested fixes, decisions, and validation-attempt history.

The Benchmarks screen manages account-owned human-labelled datasets and compares labels with linked saved live review results. Runs are immutable replays, not model reruns; demo reviews are excluded. Precision/recall use exact `category|severity|file_path` signatures. Missing labels/predictions are shown as incomplete. Provider-reported token usage and measured request latency appear only when available; cost is never inferred.

Discover supports synchronous review and durable background review jobs. Queued jobs are polled until completion or a terminal status; a completed job opens its stored review. Background processing requires the separately supervised backend worker (`python -m app.review_worker`).
