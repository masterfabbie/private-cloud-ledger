import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import ensure_admin
from app.config import get_settings
from app.version import APP_NAME, app_commit, app_version
from app import db as db_module
from app.db import SessionLocal
from app.services import banksync
from app.routers import (
    accounts,
    auth,
    bank,
    budgets,
    categories,
    export,
    imports,
    recurring,
    rules,
    stats,
    transactions,
    users,
)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with SessionLocal() as db:
        ensure_admin(db)
    stop_scheduler = banksync.start_scheduler(lambda: db_module.SessionLocal())
    yield
    if stop_scheduler:
        stop_scheduler.set()


app = FastAPI(title=APP_NAME, version=app_version(), lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

_settings = get_settings()
# Holds only the OIDC state/nonce/PKCE verifier during the login round trip (10 minutes).
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.secret_key or secrets.token_urlsafe(32),
    session_cookie="ll_sso",
    max_age=600,
    same_site="lax",
    https_only=_settings.cookie_secure,
)

for r in (auth, users, accounts, bank, categories, transactions, imports, rules, budgets, recurring, stats, export):
    app.include_router(r.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/version")
def version_info():
    return {"name": APP_NAME, "version": app_version(), "commit": app_commit()}


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/js/") or path.startswith("/static/css/"):
        # Always revalidate the app's code (cheap thanks to ETag/304), so browsers never keep
        # running old JavaScript after an update.
        response.headers["Cache-Control"] = "no-cache"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/api/docs"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'",
        )
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
