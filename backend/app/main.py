from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import cors_origins
from app.database.mongo_connection import close_mongo_client
from app.database.reviews import get_store
from app.routes.auth import legacy_router as legacy_auth_router, router as auth_router
from app.routes.learning import router as learning_router
from app.routes.review import router as review_router


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


@app.get("/", tags=["default"])
def root():
    return {
        "service": "PR Mentor API",
        "status": "ok",
        "docs": "/docs",
        "health": "/health",
        "readiness": "/api/health/ready",
    }


@app.get("/health", tags=["default"])
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
app.include_router(legacy_auth_router)
