# PR Mentor backend

FastAPI service for the frontend review workspace. API schemas and routes are the stable boundary; ingestion, analysis, AI, and storage implementations can be replaced behind their current interfaces.

## Run locally

From this directory, create and activate a virtual environment, install dependencies, and start the service:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Copy `.env.example` to `.env` and export the desired values in the shell (the service intentionally does not load dotenv files implicitly). `PR_MENTOR_STORAGE` selects `sqlite` (default) or `mongodb`; the service does not silently fall back if the selected database is unavailable. SQLite uses `PR_MENTOR_DATABASE_PATH`, defaulting to `backend/data/pr_mentor.sqlite3`. MongoDB requires `MONGODB_URI`; `MONGODB_DATABASE` defaults to `pr_mentor`. Live reviews use the signed-in user's GitHub OAuth provider token when available. `GITHUB_TOKEN` remains an optional server-side fallback for users without a provider token. AI needs both `LLM_API_KEY` and `LLM_MODEL`; the OpenAI-compatible endpoint may be changed with `LLM_API_BASE_URL`. `PR_MENTOR_CORS_ORIGINS` is a comma-separated allowlist. `PR_MENTOR_HTTP_TIMEOUT` is bounded to 1–60 seconds.

Run tests from this directory with `python -m pytest -q`. Tests use temporary SQLite files, `mongomock`, and mocked external services; they need no GitHub token, LLM key, MongoDB server, or production database.

### MongoDB configuration and migration

Set `PR_MENTOR_STORAGE=mongodb`, `MONGODB_URI`, and optionally `MONGODB_DATABASE` to enable MongoDB. For Atlas, use the Atlas-provided `mongodb+srv://` URI and keep TLS certificate validation enabled. Local development can use `mongodb://localhost:27017`. The backend uses PyMongo and `dnspython` for SRV URIs, with bounded connection, server-selection, and socket timeouts. The process-level client is closed during FastAPI shutdown. `/api/health/ready` checks the currently selected storage; liveness remains independent of database availability.

Never commit a real connection URI, print it to logs, or disable TLS certificate validation. Store production credentials in the deployment secret manager and use a least-privilege database user. MongoDB connections and writes are only made by the backend; the browser uses the existing HTTP API.

The migration command is explicit and repeatable. It reads, but never changes or deletes, the SQLite source:

```powershell
$env:PR_MENTOR_STORAGE = "mongodb"
$env:MONGODB_URI = "<your-secret-uri>"
python -m scripts.migrate_sqlite_to_mongodb --source .\data\pr_mentor.sqlite3
```

The default source path is `PR_MENTOR_DATABASE_PATH`. Migration is never run at application startup. Reviews are upserted by `review_id`; saved URLs and learning attempts use stable unique keys so rerunning the command does not duplicate them.

Collections are `reviews`, `saved_pull_requests`, `learning_attempts`, and `users`. User profiles are also available in SQLite for local use. The MongoDB user collection has a unique `firebase_uid` index. Reviews and saved PR links are stored with the verified account ID, and every review read or saved-link operation is restricted to that account. SQLite remains suitable for local development; use a persistent shared MongoDB deployment for review history to follow a user across devices. Records created before account ownership was added have no verified owner and are not shown to signed-in users.

## API contracts

- `GET /api/health` is a liveness check; `GET /api/health/ready` verifies SQLite can open.
- `POST /api/reviews/analyze` accepts `{ "url": "https://github.com/owner/repo/pull/42", "mode": "demo" | "live", "include_learning": true }`. `pull_request_url` is also accepted for compatibility. Responses use `schemas/review.py` and `schemas/finding.py`, including explicit `is_demo`, analysis provenance, tool statuses, warnings, verification counts, and diff files.
- `POST /api/reviews/analyze`, `GET /api/reviews/history?limit=50`, `GET /api/reviews/{review_id}`, and `GET /api/reviews/{review_id}/diff` require the signed-in user's bearer token; completed reviews are automatically saved to that account.
- `GET /api/reviews/saved`, `POST /api/reviews/saved` with `{ "url": "..." }`, and `DELETE /api/reviews/saved/{saved_id}` require the signed-in user's bearer token and manage only that account's saved PR links.
- `GET /api/learning/exercises`, `GET /api/learning/exercises/{exercise_id}`, `POST /api/learning/exercises/{exercise_id}/submit` with `{ "answer": "..." }`, `GET /api/learning/progress`, and `GET /api/learning/history` serve authored exercises and persistent progress. Answers are omitted until submission.
- `POST /api/auth/firebase/verify` verifies a Firebase ID token and creates or refreshes its profile; `GET /api/auth/me`, `PATCH /api/users/me`, `DELETE /api/users/me`, and `POST /api/auth/logout` require a bearer token. Account deletion requires recent authentication (five minutes). Firebase Phone Auth sends and verifies SMS codes in the browser; legacy `/api/auth/otp/*` paths fail closed.

FastAPI validation and HTTP errors use the standard `{ "detail": ... }` shape consumed by the frontend. Fixed paths are declared before dynamic review IDs.

## Module boundaries and team ownership

The public request/response schemas in `app/schemas/` and route paths above are shared contracts. Keep them stable while replacing internals.

- **Member 1 — GitHub ingestion:** `app/github_service/parser.py`, `client.py`, `pull_requests.py`, and `files.py`. `parse_pull_request_url()` validates a GitHub PR URL; `fetch_pull_request()` returns a `PullRequestBundle` with typed metadata, statuses, and patches. Requests go only to the fixed GitHub API host, use timeouts, paginate with a cap, and report upstream failures explicitly.
- **Member 2 — static analysis and verification:** `app/verification/`. `path_validation.py` is a security boundary: retain its checkout containment and symlink checks. Ruff and Bandit consume only Python files materialized into a temporary directory. `test_runner.py` reports unavailable; PR code is never executed on the host.
- **Member 3 — AI review:** `app/ai_review/client.py`, `prompts.py`, and `reviewer.py`. Replace the OpenAI-compatible transport behind `complete_json()` while keeping `generate_candidates()` schema-validating and provider-independent. Credentials are environment-only. Missing credentials and provider errors produce unavailable tool status, never fabricated AI findings.
- **Integration owner:** `app/services/review_orchestrator.py` coordinates modules and partial failures. `app/routes/`, `app/schemas/`, `app/database/`, and `app/main.py` own the API, persistence boundary, and application configuration. Coordinate contract changes here rather than having module owners independently change routes or frontend types.

## Runtime behavior and security

Demo mode returns stable educational findings labelled with source `demo`; it does not fetch or analyze the submitted repository. Live mode fetches GitHub metadata and diffs, retrieves at most 20 bounded Python files, runs available static analyzers in a temporary workspace, and includes AI findings only when configured. Tests are not run; the response explicitly says so. Tool unavailability creates `partial` status and warnings. The submitted repository URL is parsed and restricted to HTTPS `github.com`; server requests use a fixed `api.github.com` base to avoid arbitrary outbound destinations.

SQLite and MongoDB implement the replaceable `ReviewStore` interface. Reviews, saved PR links, and learning attempts survive process restarts. A failed database write returns an API error; the service does not claim success. Before production deployment, configure managed storage, secrets, platform rate limiting, and an isolated runner if test execution is ever introduced. New frontend sign-in uses Supabase Auth and GitHub OAuth. Firebase token verification remains available for older clients. Device sessions are not exposed because reliable device tracking is not implemented.

## Supabase GitHub authentication setup

1. Set `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` (recommended) or legacy `SUPABASE_ANON_KEY` in the backend host environment. The API validates Supabase access tokens against the Supabase Auth user endpoint and syncs the resulting profile. Never use a secret or service-role key for frontend or bearer-token verification.
2. In Supabase **Authentication → Sign In / Providers**, enable GitHub and save the GitHub OAuth app client ID and secret there. Configure the app's local and deployed URLs under **Authentication → URL Configuration**. The GitHub OAuth app's callback URL must match the callback URL Supabase displays for that provider. The frontend requests only `read:user user:email`; live PR reads use the user's OAuth token so each user's API use has its own GitHub quota. The token is sent only with live analysis requests and is not stored by PR Mentor.
3. Set `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` in `frontend/.env.local` for local development and as GitHub Actions repository variables for GitHub Pages builds. The GitHub Pages URLs are `https://pr-mentor.github.io/PR_MENTOR/` and the matching login route. See Supabase's [GitHub sign-in guide](https://supabase.com/docs/guides/auth/social-sign-in/auth-github).

4. Add the exact frontend origins to `PR_MENTOR_CORS_ORIGINS` on the backend host. For GitHub Pages, the origin is `https://pr-mentor.github.io` (no repository path).
5. Locally, copy `frontend/.env.example` to `frontend/.env.local`, then start the frontend and backend using the commands above. On Render, add `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` (or `SUPABASE_ANON_KEY` for legacy projects); do not add a service-role key to the frontend or use it to validate user access tokens.

Supabase account deletion is not enabled through the API; users can sign out from the workspace. Firebase Admin settings below are only needed if an older Firebase client still needs to authenticate.

The orchestration layer has no dependency on the nested backend repository from Ritik's fork. `backend/` is an ordinary tracked directory in this canonical repository.
