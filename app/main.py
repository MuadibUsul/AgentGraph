"""AgentGraph FastAPI gateway: REST, admin API, MCP mount, health."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.admin_routes import router as admin_router
from app.api.auth_routes import router as auth_router
from app.api.human_routes import router as human_router
from app.api.stream_routes import admin_router as admin_stream_router
from app.api.stream_routes import router as stream_router
from app.api.v1_routes import router as v1_router
from app.config import get_settings
from app.database import apply_schema, check_database, engine
from app.engine.embeddings import close_embedding_provider, configured_embedding_model
from app.errors import ApiError, error_body
from app.events import event_bus
from app.mcp_server.server import create_http_app, mcp
from app.middleware import RequestContextMiddleware
from app.protocol_middleware import ProtocolMiddleware
from app.services.external_actions import external_action_loop
from app.services.indexer import index_worker_loop
from app.services.reaper import lease_reaper_loop
from app.services.webhooks import webhook_delivery_loop

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agentgraph")

mcp_http_app = create_http_app() if settings.mcp_enabled else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_production:
        token = settings.admin_token or ""
        weak = token in {
            "change-me-in-production",
            "dev-admin-token-change-me",
            "agentgraph-dev-admin",
        }
        if len(token) < 32 or weak:
            raise RuntimeError(
                "production ADMIN_TOKEN must be at least 32 random characters"
            )
    if settings.auto_init_db:
        await apply_schema()
        # One-time, idempotent: constitution + namespace readme stubs (AG-OB-02).
        from app.database import async_session_factory
        from app.services.onboarding import backfill_all_workspaces

        async with async_session_factory() as session:
            scaffolded = await backfill_all_workspaces(session)
        if scaffolded:
            logger.info("scaffolded conventions for %s workspace(s)", scaffolded)
    await event_bus.start()
    reaper_task = asyncio.create_task(
        lease_reaper_loop(), name="agentgraph-lease-reaper"
    )
    webhook_task = asyncio.create_task(
        webhook_delivery_loop(), name="agentgraph-webhook-dispatcher"
    )
    action_task = asyncio.create_task(
        external_action_loop(), name="agentgraph-external-action-dispatcher"
    )
    index_task = asyncio.create_task(index_worker_loop(), name="agentgraph-index-worker")
    try:
        if settings.mcp_enabled:
            async with mcp.session_manager.run():
                logger.info(
                    "AgentGraph %s ready (embedding=%s, mcp=/mcp)",
                    settings.app_version,
                    settings.embedding_provider,
                )
                yield
        else:
            yield
    finally:
        for task in (reaper_task, webhook_task, action_task, index_task):
            task.cancel()
        for task in (reaper_task, webhook_task, action_task, index_task):
            with suppress(asyncio.CancelledError):
                await task
        await event_bus.stop()
        await close_embedding_provider()
        await engine.dispose()


app = FastAPI(
    title="AgentGraph — Human-in-the-Loop Shared Blackboard",
    version=settings.app_version,
    description=(
        "A multi-agent shared workspace with TTL task leases, progressive "
        "L0/L1/L2 handoffs, human sovereignty locks, session-batch undo, and "
        "PostgreSQL LISTEN/NOTIFY realtime updates. REST and MCP share one "
        "capability-scoped service layer."
    ),
    lifespan=lifespan,
    docs_url=(
        "/docs"
        if settings.docs_enabled and not settings.docs_require_auth
        else None
    ),
    redoc_url=(
        "/redoc"
        if settings.docs_enabled and not settings.docs_require_auth
        else None
    ),
    openapi_url=(
        "/openapi.json"
        if settings.docs_enabled and not settings.docs_require_auth
        else None
    ),
)

_CSP = (
    "default-src 'self'; base-uri 'self'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'self'; "
    "script-src 'self' 'unsafe-inline' https://accounts.google.com "
    "https://www.gstatic.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
    "https://fonts.googleapis.com https://accounts.google.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self' https://accounts.google.com "
    "https://oauth2.googleapis.com; "
    "frame-src https://accounts.google.com"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    path = request.url.path
    if path.startswith("/dashboard/_next/static/"):
        response.headers.setdefault(
            "Cache-Control", "public, max-age=31536000, immutable"
        )
    elif path.startswith("/dashboard/") or path == "/dashboard":
        response.headers.setdefault(
            "Cache-Control", "no-store, max-age=0, must-revalidate"
        )
    elif path.startswith("/v1/") or path.startswith("/mcp"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.add_middleware(RequestContextMiddleware)
# Inside CORS so protocol errors (unsupported_api_version) still carry CORS headers.
app.add_middleware(ProtocolMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "AgentGraph-Version", "Idempotent-Replayed", "Retry-After"],
)

app.include_router(v1_router)
app.include_router(auth_router)
app.include_router(human_router)
app.include_router(stream_router)
app.include_router(admin_router)
app.include_router(admin_stream_router)
if mcp_http_app is not None:
    # Trailing slash is the canonical endpoint: /mcp/
    app.mount("/mcp", mcp_http_app, name="mcp")

dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard" / "out"
if dashboard_dir.is_dir():
    app.mount(
        "/dashboard",
        StaticFiles(directory=str(dashboard_dir), html=True),
        name="dashboard",
    )


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.body(),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg", "invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=error_body(
            "validation_error", "request validation failed", 422, details=details
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    message = str(exc.detail) if exc.detail else "HTTP error"
    code = "not_found" if exc.status_code == 404 else "http_error"
    headers = getattr(exc, "headers", None) or {}
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, message, exc.status_code, headers=headers),
        headers=headers,
    )


@app.exception_handler(SQLAlchemyTimeoutError)
async def overload_handler(request: Request, exc: SQLAlchemyTimeoutError) -> JSONResponse:
    """Connection pool exhausted: a distinguishable, retryable 503 (AG-ST-06)."""
    logger.warning("database pool exhausted on %s", request.url.path)
    headers = {"Retry-After": "1"}
    return JSONResponse(
        status_code=503,
        content=error_body(
            "overloaded",
            "the server is at capacity; retry after the indicated delay",
            503,
            details={"retry_after_ms": 1000},
            headers=headers,
        ),
        headers=headers,
    )


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    logger.exception("database error")
    return JSONResponse(
        status_code=503,
        content=error_body(
            "database_unavailable", "database operation failed", 503,
            headers={"Retry-After": "2"},
        ),
        headers={"Retry-After": "2"},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content=error_body("internal_error", "internal server error", 500),
    )


_docs_security = HTTPBasic(auto_error=False)


def _require_docs_auth(
    credentials: HTTPBasicCredentials | None = Depends(_docs_security),
) -> None:
    if not settings.docs_require_auth:
        return
    admin = settings.admin_token
    if (
        not admin
        or credentials is None
        or not hmac.compare_digest(credentials.password, admin)
    ):
        raise HTTPException(
            status_code=401,
            detail="API docs require the admin token (HTTP Basic password)",
            headers={"WWW-Authenticate": 'Basic realm="AgentGraph docs"'},
        )


if settings.docs_enabled and settings.docs_require_auth:

    @app.get("/openapi.json", include_in_schema=False)
    async def protected_openapi(
        _: None = Depends(_require_docs_auth),
    ) -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False)
    async def protected_docs(
        _: None = Depends(_require_docs_auth),
    ) -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{settings.app_name} API docs",
        )


@app.get("/", include_in_schema=False)
async def root(request: Request) -> Any:
    if (
        dashboard_dir.is_dir()
        and "text/html" in request.headers.get("accept", "").lower()
    ):
        return RedirectResponse(url="/dashboard/", status_code=307)
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs" if settings.docs_enabled else None,
        "health": "/healthz",
        "rest": "/v1",
        "mcp": "/mcp/" if settings.mcp_enabled else None,
        "dashboard": "/dashboard/" if dashboard_dir.is_dir() else None,
        "agent_docs": "/v1/agent-docs",
        "first_call": "get_orientation",
        "primitives": [
            "get_orientation",
            "ack_constitution",
            "get_usage",
            "get_index_status",
            "workspace_sync",
            "create_task",
            "review_task",
            "request_human_review",
            "agent_claim_and_read",
            "agent_patch_shared_state",
            "agent_handoff_task",
            "agent_escalate_to_human",
            "upsert_shared_doc",
            "upsert_many",
            "patch_doc_section",
            "query_workspace",
            "list_documents",
            "dereference",
            "dereference_many",
            "archive_document",
            "restore_document",
            "rename_document",
            "register_namespace",
            "list_namespaces",
            "get_token",
            "get_lineage",
            "list_token_tree",
            "explain_write",
            "register_parts",
            "propose_document_change",
            "vote_on_proposal",
            "list_document_proposals",
            "draft_external_action",
            "get_api_docs",
        ],
    }


@app.get("/healthz", tags=["Health"], summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": settings.app_version}


@app.get("/readyz", tags=["Health"], summary="Readiness probe")
async def readyz() -> JSONResponse:
    database_ok = await check_database()
    payload = {
        "status": "ok" if database_ok else "degraded",
        "database": "ok" if database_ok else "unavailable",
        "embedding_provider": settings.embedding_provider,
        "embedding_model": configured_embedding_model(),
        "embedding_dimensions": settings.embedding_dimensions,
        "mcp": "enabled" if settings.mcp_enabled else "disabled",
        "version": settings.app_version,
    }
    return JSONResponse(status_code=200 if database_ok else 503, content=payload)
