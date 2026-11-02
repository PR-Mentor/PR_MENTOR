# PR Mentor frontend

React + TypeScript application built with Vite.

## Local development

```bash
npm install
npm run dev
```

Copy `.env.example` to `.env.local`, set `VITE_API_BASE_URL` to the FastAPI origin (defaults to `http://127.0.0.1:8000`), and provide the six public Firebase web-app settings. Start the backend as described in [`../backend/README.md`](../backend/README.md).

The Discover page asks whether to use demo or live analysis. Demo findings are explicitly sample content from the backend and do not describe the submitted pull request. History, saved links, diffs, and learning data come from API responses. API errors and unavailable backend stages remain visible; this frontend does not invent fallback review results.

The `/login` page supports Firebase GitHub, Google, and phone OTP providers. The backend verifies ID tokens and stores profiles. Provider sign-in requires Firebase Console setup; see the backend README for local, Firebase, Vercel, and Render configuration. Review history, saved links, and learning progress remain shared records and are not user-scoped.
