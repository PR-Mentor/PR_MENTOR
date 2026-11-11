# Deployment settings

## Vercel frontend

Set the Vercel project's **Root Directory** to `frontend` and its framework preset to **Vite**. The checked-in `frontend/vercel.json` serves the generated `dist` directory and routes client-side paths to the React app. The build command is the `build` script in `frontend/package.json` (`tsc -b && vite build`).

Set `VITE_API_BASE_URL`, `VITE_SUPABASE_URL`, and `VITE_SUPABASE_ANON_KEY` in Vercel's project environment variables for each environment that should use the backend and Supabase. The npm `cache-dir` warning is non-fatal; remove any manually configured `npm_config_cache_dir` / `NPM_CONFIG_CACHE_DIR` variable from the Vercel project if that warning needs to be cleared.

## Render backend

The backend always allows the production origins `https://prmentor.in` and `https://www.prmentor.in`, even when `PR_MENTOR_CORS_ORIGINS` is set. Keep any other required frontend origins in that variable and redeploy the backend after changing configuration.

The FastAPI service must deploy this repository's `codex/fix-supabase-env-deployment` branch to use the repaired backend entry point. The failed deployment log was building `main` at `742c106`; it failed during app import because `backend/app/routes/review.py` contained an indented branch-name line. That stray line is absent from the repaired branch.

In the Render service settings, set **Branch** to `codex/fix-supabase-env-deployment`, **Root Directory** to `backend`, **Build Command** to `pip install -r requirements.txt`, and **Start Command** to `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Set `MONGODB_URI` to the Atlas connection string and optionally set `MONGODB_DATABASE` to the existing database name (`pr_mentor` by default) in Render's Environment page. Keep the database URI secret; do not commit it. Render now selects MongoDB for durable storage and reports the API as not ready if the URI is missing, instead of writing review history to ephemeral SQLite storage.
