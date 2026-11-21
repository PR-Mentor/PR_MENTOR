# PR Mentor frontend

React + TypeScript application built with Vite.

## Local development

```bash
npm install
npm run dev
```

Copy `.env.example` to `.env.local`, set `VITE_API_BASE_URL` to the FastAPI origin (defaults to `http://127.0.0.1:8000`), and set `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` from the Supabase project API settings. The anon key is intended for browser use; never put a Supabase service-role key in frontend variables. Start the backend as described in [`../backend/README.md`](../backend/README.md).

The Discover page asks whether to use demo or live analysis. Demo findings are explicitly sample content from the backend and do not describe the submitted pull request. History, saved links, diffs, and learning data come from API responses. API errors and unavailable backend stages remain visible; this frontend does not invent fallback review results.

GitHub sign-in uses Supabase Auth and the API's server-side OAuth exchange. After signing in, **My Repositories** loads the repositories the connected GitHub account can access. See the [backend GitHub configuration](../backend/README.md#supabase-github-authentication-setup) for the return URL and server environment variables.

Copy `.env.example` to `.env.local`, set `VITE_API_BASE_URL` to the FastAPI origin (defaults to `http://127.0.0.1:8000`), and set `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` from the Supabase project **Connect** dialog or API settings. The URL must be the project root (for example `https://your-project-ref.supabase.co`), without `/rest/v1`. Use a publishable (`sb_publishable_…`) or legacy `anon` key in the frontend; never put a secret (`sb_secret_…`) or service-role key in frontend variables. Vite loads environment values when its dev server/build starts, so restart `npm run dev` after changing `.env.local`. Start the backend as described in [`../backend/README.md`](../backend/README.md).

The Discover page asks whether to use demo or live analysis. Demo findings are explicitly sample content from the backend and do not describe the submitted pull request. History, saved links, diffs, and learning data come from API responses. API errors and unavailable backend stages remain visible; this frontend does not invent fallback review results.

GitHub Pages builds read `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` from GitHub Actions repository variables. Use persistent shared backend storage when accounts and review history need to persist across API instances.

codex/phone-otp-login
Phone sign-in uses Supabase SMS OTP, then relies on the existing Supabase session listener to verify the access token with `POST /api/auth/supabase/verify`. To deliver real codes, open Supabase **Authentication → Sign In / Providers → Phone**, enable Phone, select an SMS provider (for example Twilio), enter that provider's server-side credentials and sender or Messaging Service SID, then save. Supabase does not send phone OTPs until an SMS provider is configured; frontend code cannot enable this project setting. Keep `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` configured for the frontend. For a no-cost flow check, configure a test phone number and fixed OTP in Supabase Auth instead of sending SMS. The legacy `/api/auth/otp/*` endpoints are not used.

Phone sign-in uses Supabase SMS OTP, then relies on the existing Supabase session listener to verify the access token with `POST /api/auth/supabase/verify`. Enable the Phone provider and configure an SMS provider in Supabase Auth. Keep the existing `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` variables configured for the frontend. The legacy `/api/auth/otp/*` endpoints are not used.
 main
