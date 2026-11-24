# Deployment settings

## Vercel frontend

Set the Vercel project's **Root Directory** to `frontend` and its framework preset to **Vite**. The checked-in `frontend/vercel.json` serves the generated `dist` directory and routes client-side paths to the React app. The build command is the `build` script in `frontend/package.json` (`tsc -b && vite build`).

Set `VITE_API_BASE_URL=https://pr-mentor-3b2w.onrender.com`, `VITE_SUPABASE_URL`, and `VITE_SUPABASE_ANON_KEY` in Vercel's project environment variables for each environment that should use the backend and Supabase. The frontend uses the Render URL as its production-safe fallback if the API variable is omitted. Redeploy the frontend after changing build variables; Vite embeds them at build time. The npm `cache-dir` warning is non-fatal; remove any manually configured `npm_config_cache_dir` / `NPM_CONFIG_CACHE_DIR` variable from the Vercel project if that warning needs to be cleared.

For the custom domain, add both `https://prmentor.in` and `https://www.prmentor.in` to `PR_MENTOR_CORS_ORIGINS` on the Render backend, alongside any existing origins. This variable replaces the backend's defaults when set. Redeploy/restart the backend after changing it. Check `https://pr-mentor-3b2w.onrender.com/health` for API liveness and `/api/health/ready` for configured database readiness.


## Render backend

The backend always allows the production origins `https://prmentor.in` and `https://www.prmentor.in`, even when `PR_MENTOR_CORS_ORIGINS` is set. Keep any other required frontend origins in that variable and redeploy the backend after changing configuration.

Use a verified release commit from `main` for the Render backend. Set **Root Directory** to `backend`, **Build Command** to `pip install -r requirements.txt && python -m compileall -q app`, and **Start Command** to `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Keep the existing MongoDB and Supabase environment settings. Check both health endpoints and submit an authenticated live review after deployment.

Quick live reviews use an 18-second server deadline and a 20-second browser deadline. Full live reviews use a 33-second server deadline and a 35-second browser deadline. Full reviews preserve completed evidence when optional checks exhaust their share of the deadline. The browser deadline includes network and hosting time. Use an always-on backend if the first review after an idle period must meet the same target.

### Learning Lab Persistence & Render Disks
The Learning Lab subsystems (PR learning spaces, concepts, coding exercises, drafts, and submissions) persist to SQLite via `connection()`. To ensure learning progress, drafts, and spaces survive service restarts or redeployments on Render:
1. Attach a **Render Persistent Disk** to the backend service (e.g., Mount Path: `/var/data`, Size: 1 GB+).
2. Set `PR_MENTOR_DATABASE_PATH=/var/data/pr_mentor.sqlite3` in Render's Environment Variables.
Reviews and workspaces continue using MongoDB as configured by `MONGODB_URI`.

## Email OTP with Brevo

Email OTP uses Supabase Auth so verified users receive a normal Supabase session; Brevo delivers the messages through Supabase custom SMTP. In Supabase Dashboard, open **Project Settings → Authentication → SMTP Settings**, enable custom SMTP, and set the Brevo SMTP host, port, login, and SMTP key. Use a sender address/domain verified in Brevo. Configure SMTP credentials only in Supabase (never in `VITE_*` variables or frontend code). In **Authentication → Sign In / Providers → Email**, enable email sign-in and configure OTP delivery with a six-digit code; disable email-link-only delivery if the UI should send numeric codes. The frontend requires the existing `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`; no Brevo API key or backend secret is needed for this Supabase SMTP integration.

For numeric codes on both paths, update **Authentication → Email Templates** in the Supabase Dashboard:

- **Confirm signup** is used for account creation. Set its subject to `{{ .Token }} is your PR Mentor verification code` and make the body display `{{ .Token }}`. Remove `{{ .ConfirmationURL }}` so signup sends a code rather than a confirmation link.
- **Magic Link** is used for existing-user login. Set its subject to `{{ .Token }} is your PR Mentor sign-in code` and make the body display `{{ .Token }}` instead of `{{ .ConfirmationURL }}`.

Use valid, simple HTML in the template body, for example `<p><strong>{{ .Token }}</strong></p>`. Avoid escaped quotes in HTML attributes; malformed attributes cause Supabase Auth to fail the email request before sending it.


The frontend verifies signup codes with OTP type `signup` and login codes with OTP type `email`. The login screen reports Supabase's missing-account OTP error and directs the user to create an account (or choose GitHub if the account uses GitHub sign-in).
