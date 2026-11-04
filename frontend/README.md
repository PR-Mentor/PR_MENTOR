# PR Mentor frontend

React + TypeScript application built with Vite.

## Local development

```bash
npm install
npm run dev
```

Copy `.env.example` to `.env.local`, set `VITE_API_BASE_URL` to the FastAPI origin (defaults to `http://127.0.0.1:8000`), and set `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` from the Supabase project API settings. The anon key is intended for browser use; never put a Supabase service-role key in frontend variables. Start the backend as described in [`../backend/README.md`](../backend/README.md).

The Discover page asks whether to use demo or live analysis. Demo findings are explicitly sample content from the backend and do not describe the submitted pull request. History, saved links, diffs, and learning data come from API responses. API errors and unavailable backend stages remain visible; this frontend does not invent fallback review results.

The `/login` page signs in with GitHub through Supabase Auth. In Supabase, enable the GitHub provider and add the app URLs under Authentication → URL Configuration: `http://localhost:5173/PR_MENTOR/` for local development and `https://pr-mentor.github.io/PR_MENTOR/` for GitHub Pages. The GitHub OAuth app callback URL must be the callback shown in Supabase's GitHub provider settings. Add the two `VITE_SUPABASE_*` values as GitHub Actions repository variables for Pages builds. Configure `SUPABASE_URL` and `SUPABASE_ANON_KEY` on the backend host so the API can validate sessions and sync user profiles. Review history and saved pull requests are private to the signed-in account. Configure persistent shared MongoDB storage on the backend host for history to appear across devices; local SQLite files are not shared between backend instances.
