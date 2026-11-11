# Deployment settings

## Vercel frontend

Set the Vercel project's **Root Directory** to `frontend` and its framework preset to **Vite**. The checked-in `frontend/vercel.json` serves the generated `dist` directory and routes client-side paths to the React app. The build command is the `build` script in `frontend/package.json` (`tsc -b && vite build`).

Set `VITE_API_BASE_URL=https://pr-mentor.onrender.com`, `VITE_SUPABASE_URL`, and `VITE_SUPABASE_ANON_KEY` in Vercel's project environment variables for each environment that should use the backend and Supabase. The frontend uses the Render URL as its production-safe fallback if the API variable is omitted. Redeploy the frontend after changing build variables; Vite embeds them at build time. The npm `cache-dir` warning is non-fatal; remove any manually configured `npm_config_cache_dir` / `NPM_CONFIG_CACHE_DIR` variable from the Vercel project if that warning needs to be cleared.

For the custom domain, add both `https://prmentor.in` and `https://www.prmentor.in` to `PR_MENTOR_CORS_ORIGINS` on the Render backend, alongside any existing origins. This variable replaces the backend's defaults when set. Redeploy/restart the backend after changing it. Check `https://pr-mentor.onrender.com/health` for API liveness and `/api/health/ready` for configured database readiness.

## Render backend

The FastAPI service must deploy this repository's `codex/fix-supabase-env-deployment` branch to use the repaired backend entry point. The failed deployment log was building `main` at `742c106`; it failed during app import because `backend/app/routes/review.py` contained an indented branch-name line. That stray line is absent from the repaired branch.

In the Render service settings, set **Branch** to `codex/fix-supabase-env-deployment`, **Root Directory** to `backend`, **Build Command** to `pip install -r requirements.txt`, and **Start Command** to `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Configure the service's existing runtime environment variables (including the storage connection and Supabase validation settings) in Render's Environment page; do not commit secrets.

## Email OTP with Brevo

Email OTP uses Supabase Auth so verified users receive a normal Supabase session; Brevo delivers the messages through Supabase custom SMTP. In Supabase Dashboard, open **Project Settings → Authentication → SMTP Settings**, enable custom SMTP, and set the Brevo SMTP host, port, login, and SMTP key. Use a sender address/domain verified in Brevo. Configure SMTP credentials only in Supabase (never in `VITE_*` variables or frontend code). In **Authentication → Sign In / Providers → Email**, enable email sign-in and configure OTP delivery with a six-digit code; disable email-link-only delivery if the UI should send numeric codes. The frontend requires the existing `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`; no Brevo API key or backend secret is needed for this Supabase SMTP integration.

For numeric codes on both paths, update **Authentication → Email Templates** in the Supabase Dashboard:

- **Confirm signup** is used for account creation. Set its subject to `{{ .Token }} is your PR Mentor verification code` and make the body display `{{ .Token }}`. Remove `{{ .ConfirmationURL }}` so signup sends a code rather than a confirmation link.
- **Magic Link** is used for existing-user login. Set its subject to `{{ .Token }} is your PR Mentor sign-in code` and make the body display `{{ .Token }}` instead of `{{ .ConfirmationURL }}`.

 codex/phone-otp-login
Use valid, simple HTML in the template body, for example `<p><strong>{{ .Token }}</strong></p>`. Avoid escaped quotes in HTML attributes; malformed attributes cause Supabase Auth to fail the email request before sending it.


 main
The frontend verifies signup codes with OTP type `signup` and login codes with OTP type `email`. The login screen reports Supabase's missing-account OTP error and directs the user to create an account (or choose GitHub if the account uses GitHub sign-in).
