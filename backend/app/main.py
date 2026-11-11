from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import cors_origins
from app.database.mongo_connection import close_mongo_client
from app.database.reviews import get_store
from app.routes.analytics import router as analytics_router
from app.routes.auth import router as auth_router
from app.routes.benchmarks import router as benchmarks_router
from app.routes.learning import router as learning_router
from app.routes.review import router as review_router
from app.routes.workspaces import router as workspaces_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        close_mongo_client()


app = FastAPI(title="PR Mentor API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def enforce_browser_origin(request, call_next):
    """Reject cross-origin cookie-authenticated state changes, including same-site subdomains."""
    origin = request.headers.get("origin")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin is not None and origin not in cors_origins():
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed."})
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "service": "pr-mentor"}


@app.get("/api/health")
def api_health():
    return health()


@app.get("/api/health/ready")
def readiness():
    try:
        get_store().ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Configured storage is unavailable.") from exc
    return {"status": "ready", "service": "pr-mentor"}


app.include_router(review_router)
app.include_router(learning_router)
app.include_router(auth_router)
app.include_router(analytics_router)
app.include_router(workspaces_router)
app.include_router(benchmarks_router)
