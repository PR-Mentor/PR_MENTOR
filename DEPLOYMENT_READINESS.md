# PR Mentor deployment readiness

Status as of 2026-11-11. This is a repository-based audit, not evidence that a production host was inspected. No production credentials or external systems were used.

Status labels:

- **VERIFIED** means confirmed by current repository code or local test/configuration evidence.
- **UNVERIFIED** means it depends on deployment values, credentials, or infrastructure not available to this audit.
- **BLOCKED** means a repository or deployment mismatch must be resolved before release.

## Non-Phase-6 configuration reviewed locally

- **VERIFIED — Backend environment names:** `PR_MENTOR_STORAGE` (optional, default `sqlite`); `PR_MENTOR_DATABASE_PATH` (optional SQLite path); `MONGODB_URI` (required only when MongoDB is selected); `MONGODB_DATABASE` (optional, default `pr_mentor`); `PR_MENTOR_CORS_ORIGINS` (optional, defaults to local Vite origins); `PR_MENTOR_HTTP_TIMEOUT` (optional, default 15 seconds, clamped to 1–60); and `PR_MENTOR_COOKIE_SECURE` (optional, default `true`; only `true`/`false` accepted). The application does not load `.env` files automatically.
- **VERIFIED — GitHub/AI variables:** `GITHUB_TOKEN` is optional and is a shared server token, not per-user OAuth. Private repository access is refused by the application. The legacy AI configuration is `LLM_API_KEY`, `LLM_MODEL`, and `LLM_API_BASE_URL`; AI is unavailable without credentials/model. Provider routing supports `LLM_PROVIDER`, `LLM_PROVIDER_<TASK>`, `LLM_FALLBACK_PROVIDERS`, `LLM_FALLBACK_PROVIDERS_<TASK>`, `LLM_PROVIDER_RETRIES`, `LLM_<NAME>_API_KEY`, `LLM_<NAME>_API_BASE_URL`, `LLM_<NAME>_MODEL`, `LLM_<NAME>_MAX_CONCURRENCY`, `LLM_MODEL_<TASK>`, and `LLM_MODEL_<TASK>_<NAME>`. The implemented task suffixes are `CODE_REVIEW`, `FIX_GENERATION`, `TEST_GENERATION`, `ISSUE_EXPLANATION`, and `SEMANTIC_REVIEW`.
- **VERIFIED — Frontend variable:** `VITE_API_BASE_URL` is optional and defaults to `http://localhost:8000`; Vite embeds it at build time. `frontend/.env.example` contains a local URL, not a production origin.
- **VERIFIED — Database:** storage defaults to SQLite and does not fall back if selected MongoDB is unavailable. MongoDB code rejects disabled TLS validation and requires TLS for non-local connections. `/api/health/ready` calls the selected store's `ping()`; it does not test GitHub, AI, or worker health.
- **VERIFIED — Security middleware/settings:** CORS has an explicit origin list and credentials enabled; allowed methods and headers are `*`. State-changing requests with a disallowed `Origin` are rejected. Session cookies are Secure by default, HttpOnly, SameSite=Strict. FastAPI is instantiated without `debug=True`, so its default debug mode is off. There is no `TrustedHostMiddleware`, configured host allowlist, production mode switch, custom exception handler, or application logging configuration.
- **UNVERIFIED — Production values/edge:** exact HTTPS CORS origins, reverse-proxy host validation, TLS termination/forwarded-header behavior, production cookie headers, database identity/permissions/backups, secret-manager wiring, and service/log monitoring were not available for inspection.
- **UNVERIFIED — OAuth/provider operations:** no GitHub OAuth implementation exists. GitHub token access is shared and public-repository checks remain in force. No live GitHub or AI provider request was made. Provider credentials, model identifiers, endpoint compatibility, quotas, and provider error behavior in the deployed account remain unverified.
- **VERIFIED — Error/health behavior:** FastAPI HTTP/validation errors use the standard `detail` response. Readiness returns 503 for configured storage errors. **UNVERIFIED:** production exception logging, log redaction/retention, and reverse-proxy error handling are not configured in the inspected application.
- **BLOCKED — Host restriction:** the application has no allowed-host enforcement. Production ingress must enforce the expected `Host` values until an application host policy is provided and verified.
- **BLOCKED — Documentation mismatches:** the backend README's API list says `/api/health/ready` verifies SQLite, but the route pings whichever store `PR_MENTOR_STORAGE` selected. The same README summarizes AI as requiring legacy `LLM_API_KEY` and `LLM_MODEL`, while named providers can be configured with their own key/model variables. The frontend README's local default pairs `localhost:5173` with `localhost:8000`, while `frontend/.env.example` points at `127.0.0.1:8000`; those hosts can cause cookie/same-site behavior to differ. These statements/examples need reconciliation before treating local setup instructions as verified.
- **VERIFIED — Git ignore and tracked credential review:** tracked sensitive-name search found only `backend/.env.example` and `frontend/.env.example`; both are configuration templates. A filename-only tracked-content pattern scan found no private-key block, common GitHub/OpenAI/AWS token formats. This limited scan is not a complete secret-scanner result. The root `.gitignore` ignores exact `.env` names but does not ignore general `.env.*`, `*.pem`, `*.key`, `*.p12`, or `*.pfx` files. `frontend/.gitignore` covers `.env.local` and `.env.*.local`, but not `.env.production`; future secret-bearing files with those names/extensions could be accidentally tracked.
- **VERIFIED — Local checks:** frontend tests and production build passed; the full backend suite passed in a clean Python 3.12 environment with pytest 9.2.0, and `pip check` passed there. The workspace `.venv` itself contains CPython 3.13 wheel metadata under Python 3.12 and was not used for the final suite.
- **VERIFIED / UNVERIFIED — Dependency checks:** an initial pip-audit scan found pytest 8.4.2 affected by PYSEC-2026-1845; `requirements.txt` now requires pytest 9.0.3 or newer. A fresh post-change pip-audit refresh could not reach PyPI because DNS was unavailable. `npm audit --offline --omit=dev` found zero vulnerabilities in the local cache only. A deployment-time advisory scan of resolved packages is **UNVERIFIED**.
- **UNVERIFIED — Deployment/recovery:** the repository documents local `uvicorn --reload`, the review worker command, and health routes, but contains no non-Phase-6 production API/review-worker service definitions, monitoring configuration, or tested backup/restore/rollback runbook. The backup and rollback steps below are preparation only and have not been exercised.
- **BLOCKED — Environment file guidance:** backend documentation says `.env` is not loaded implicitly, but the local example includes `PR_MENTOR_COOKIE_SECURE=false`. Production must inject `true` or omit the variable; copying the example into a deployment without overriding it would disable Secure cookies if an external launcher loads that file. Production must not use the example's localhost CORS or frontend API URLs.
- **VERIFIED — Frontend build-time setting:** `VITE_API_BASE_URL` is compiled into the frontend bundle; changing it only in the web server's runtime environment will not redirect an already-built bundle. The value must be set during the production build and checked in the emitted configuration/runtime behavior.

## Environment variable inventory

| Component | Variable(s) | Status / use |
|---|---|---|
| Backend storage | `PR_MENTOR_STORAGE`, `PR_MENTOR_DATABASE_PATH`, `MONGODB_URI`, `MONGODB_DATABASE` | **VERIFIED** names/defaults; Mongo URI is conditional. Production selection and credentials **UNVERIFIED**. |
| Backend browser/security | `PR_MENTOR_CORS_ORIGINS`, `PR_MENTOR_COOKIE_SECURE` | **VERIFIED** names/defaults; production HTTPS origins and cookie header **UNVERIFIED**. |
| Backend HTTP | `PR_MENTOR_HTTP_TIMEOUT` | **VERIFIED** optional bounded timeout. |
| GitHub | `GITHUB_TOKEN` | **VERIFIED** optional shared token; no OAuth. Production token scope/value **UNVERIFIED**. |
| AI legacy | `LLM_API_KEY`, `LLM_MODEL`, `LLM_API_BASE_URL` | **VERIFIED** optional; AI requires key and model. Production credentials/provider **UNVERIFIED**. |
| AI routing | `LLM_PROVIDER[_<TASK>]`, `LLM_FALLBACK_PROVIDERS[_<TASK>]`, `LLM_PROVIDER_RETRIES`, `LLM_<NAME>_API_KEY`, `LLM_<NAME>_API_BASE_URL`, `LLM_<NAME>_MODEL`, `LLM_<NAME>_MAX_CONCURRENCY`, `LLM_MODEL_<TASK>[_<NAME>]` | **VERIFIED** from provider/client code; `.env.example` documents only a partial sample. |
| Frontend build | `VITE_API_BASE_URL` | **VERIFIED** optional build-time API origin; production value **UNVERIFIED**. |
| Phase 6 execution | `PR_MENTOR_TEST_EXECUTION_IMAGE`, `PR_MENTOR_DOCKER_EXECUTABLE`, `PR_MENTOR_TEST_EXECUTION_TIMEOUT_SECONDS`, `PR_MENTOR_TEST_EXECUTION_CPU_COUNT`, `PR_MENTOR_TEST_EXECUTION_MEMORY_BYTES`, `PR_MENTOR_TEST_EXECUTION_PROCESS_COUNT`, `PR_MENTOR_TEST_EXECUTION_SCRATCH_BYTES`, `PR_MENTOR_TEST_EXECUTION_OUTPUT_BYTES`, `PR_MENTOR_TEST_EXECUTION_CONCURRENCY`, `PR_MENTOR_STAGE6_TEMP_ROOT` | **BLOCKED / UNVERIFIED:** names and defaults are in Phase 6 code; image digest and Linux runtime are not verified. `PR_MENTOR_EXECUTION_WORKER` is an internal process gate set by worker code, not an operator secret. |

## BLOCKED — Phase 6 deployment contract (intentionally untouched)

The production runtime in `backend/app/test_execution/runtime.py` fails closed unless Docker is rootless, cgroups v2 is active, `runsc` is registered, and the approved immutable sandbox image is present. The backend README and `backend/stage6/` describe rootless Docker. In contrast, `backend/stage6_final/` configures rootful Docker, and the Stage 6 systemd files select a user runtime socket. Those materials describe incompatible runtime models. A rootful Docker VM does not meet the documented rootless production contract merely because it has gVisor registered.

The isolated dependency audit also reported `pytest==8.4.2` (the version pinned by the Stage 6 sandbox image) as affected by PYSEC-2026-1845 / CVE-2025-71176 on UNIX; versions before 9.0.3 are affected. This can cause denial of service or potentially privilege escalation via pytest temporary-directory handling. The general backend requirement is raised to pytest 9.0.3 or newer, but the sandbox image and its tests are Phase 6 and remain untouched. Determine whether and how this advisory applies to the sandbox threat model, then update and re-verify that image in a separately authorized Phase 6 change before treating it as cleared. See the [OSV advisory](https://osv.dev/vulnerability/PYSEC-2026-1845) and [pytest release notes](https://docs.pytest.org/en/latest/changelog.html).

No Phase 6 file or host configuration was changed during this audit. Do not use the rootful setup as evidence that production execution is supported. Reconcile the architecture and its documentation under a separately authorized Phase 6 task.

## UNVERIFIED — Required manual Linux host verification

Run these checks on the intended dedicated worker host after the architecture is reconciled:

1. Confirm the worker runs as a non-root dedicated service account and that `DOCKER_HOST` resolves to that account's rootless Docker socket. Confirm no worker or sandbox process can access a rootful Docker socket.
2. Confirm `docker info` reports rootless security options, cgroup version 2, and the `runsc` runtime. Confirm required cgroup controllers are delegated and usable by the service account.
3. Confirm the exact configured sandbox image is immutable (`name@sha256:...`), locally available, and has the expected Python and pytest labels. Record the digest; do not use a mutable tag as release evidence.
4. Run the approved runtime smoke test and behaviorally verify network denial, read-only root filesystem, non-root UID/GID, dropped capabilities, `no-new-privileges`, CPU/memory/PID limits, output/time limits, and cleanup after pass, failure, timeout, and cancellation.
5. Confirm the service unit's environment, socket, state/temp directories, permissions, restart behavior, and reaper schedule match the verified rootless account. Verify that application credentials are absent from the sandbox environment.
6. Preserve command output and host/runtime/image versions as deployment evidence. Unit tests and a successful `runsc` launch alone do not prove isolation or resource enforcement.

## UNVERIFIED — Release operational gates outside Phase 6

- Provide deployment-managed secrets for database, GitHub, and AI integrations; verify access without printing values.
- Verify exact production CORS origins, HTTPS, Secure cookie behavior, auth rate-limit persistence across the selected storage backend, and cross-user authorization on the deployed stack.
- Define and test database backup/restore, migration, and rollback procedures before schema or application rollout.
- Supervise the API and `python -m app.review_worker` separately, verify restart and health behavior, and establish log retention/alerting. Do not interpret the API health endpoint as proof that the review worker is healthy.
- Verify provider routing and error handling with approved test credentials in a non-production environment. No real provider calls or billed requests were made during this audit.

## UNVERIFIED — Backup and rollback preparation

The repository has no deployed database endpoint, service name, or release identifier, so these procedures must be adapted to the target environment and exercised before release:

- **SQLite:** stop the API and review worker for a quiescent backup, or use SQLite's online backup API to copy the effective `PR_MENTOR_DATABASE_PATH` to a separate protected location. Verify the copy by opening it read-only and running `PRAGMA integrity_check`. Keep the original intact. For restoration, stop both processes, preserve the current database as a separate snapshot, restore the verified copy to the configured path, then start the API and check `/api/health/ready` plus authenticated ownership workflows.
- **MongoDB:** use the organization's secret-safe backup tooling to create a consistent database snapshot; do not put a credential-bearing URI in command history or logs. Restore to an isolated database first, verify indexes, record counts, auth ownership, and readiness, then point a non-production deployment at that restored database. A production restore is an operator-controlled recovery action and was not performed here.
- **Application rollback:** roll back only to a known-good build that retains authentication, authorization, and owner-scoped access. The repository's older unauthenticated baseline is not a safe rollback target once account-owned data exists. Preserve a pre-rollout database snapshot; do not restore an older schema over current data unless the tested recovery plan requires it. Verify public liveness, storage readiness, login/logout, cross-user denial, one review read, and worker health after rollback.

None of these backup, restore, or application rollback steps has been exercised against production data or infrastructure.
