# PR Mentor frontend

React + TypeScript application built with Vite.

## Local development

```bash
npm install
npm run dev
```

Copy `.env.example` to `.env.local`, set `VITE_API_BASE_URL` to the FastAPI origin (defaults to `http://127.0.0.1:8000`), and set `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` from the Supabase project **Connect** dialog or API settings. The URL must be the project root (for example `https://your-project-ref.supabase.co`), without `/rest/v1`. Use a publishable (`sb_publishable_…`) or legacy `anon` key in the frontend; never put a secret (`sb_secret_…`) or service-role key in frontend variables. Vite loads environment values when its dev server/build starts, so restart `npm run dev` after changing `.env.local`. Start the backend as described in [`../backend/README.md`](../backend/README.md).

The Discover page asks whether to use demo or live analysis. Demo findings are explicitly sample content from the backend and do not describe the submitted pull request. History, saved links, diffs, and learning data come from API responses. API errors and unavailable backend stages remain visible; this frontend does not invent fallback review results.

The `/login` page signs in with GitHub through Supabase Auth. In Supabase, enable the GitHub provider and add the app URLs under Authentication → URL Configuration: `http://localhost:5173/PR_MENTOR/` for local development and `https://pr-mentor.github.io/PR_MENTOR/` for GitHub Pages. The GitHub OAuth app callback URL must be the callback shown in Supabase's GitHub provider settings. GitHub Pages builds use the `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` repository variables under Settings → Secrets and variables → Actions → Variables; adding `.env` to the repository does not configure those builds. Configure the Supabase project URL and publishable key on the backend host for API token validation. Review history and saved pull requests are private to the signed-in account. Configure persistent shared MongoDB storage on the backend host for history to appear across devices; local SQLite files are not shared between backend instances.
