from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__, admin_ui
from app.api import health, qr
from app.api.router import api_router
from app.config import get_settings
from app.database import SessionLocal, create_schema
from app.errors import install_error_handlers
from app.logging_filters import install_access_log_redaction
from app.middleware import install_http_middleware
from app.schemas.common import APIErrorResponse
from app.services import push as push_service

settings = get_settings()
install_access_log_redaction()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.push_worker = None
    if settings.auto_create_schema:
        create_schema()
    if settings.seed_demo_data:
        from app.seed import seed_demo_data

        with SessionLocal() as db:
            seed_demo_data(db)
    push_sender: push_service.APNsSender | None = None
    push_worker: asyncio.Task[None] | None = None
    push_stop_event: asyncio.Event | None = None
    if settings.apns_enabled:
        # Key parsing and the initial provider-token signature happen here so an
        # enabled but unusable APNs configuration fails application startup.
        push_sender = push_service.APNsSender(settings)
        push_stop_event = asyncio.Event()
        push_worker = asyncio.create_task(
            push_service.run_push_worker(push_sender, settings, push_stop_event),
            name="fofu-apns-outbox",
        )
        app.state.push_worker = push_worker
    try:
        yield
    finally:
        try:
            if push_stop_event is not None:
                push_stop_event.set()
            if push_worker is not None:
                try:
                    await asyncio.wait_for(push_worker, timeout=15)
                except asyncio.TimeoutError:
                    push_worker.cancel()
                    await asyncio.gather(push_worker, return_exceptions=True)
        finally:
            try:
                if push_sender is not None:
                    push_sender.close()
            finally:
                app.state.push_worker = None


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description=(
        "Platform-neutral API for Fofu discovery, food safety context, Korean order cards, "
        "messaging, merchant tools, and install-free QR web sessions."
    ),
    openapi_url=f"{settings.api_v1_prefix}/openapi.json",
    docs_url=f"{settings.api_v1_prefix}/docs",
    redoc_url=f"{settings.api_v1_prefix}/redoc",
    lifespan=lifespan,
    responses={
        422: {
            "model": APIErrorResponse,
            "description": "The request is invalid or cannot be processed.",
        },
        500: {
            "model": APIErrorResponse,
            "description": "The server could not complete the request.",
        },
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
install_http_middleware(app)
install_error_handlers(app)
app.include_router(admin_ui.router)
app.include_router(health.router)
app.include_router(qr.web_router)
app.include_router(api_router, prefix=settings.api_v1_prefix)

_default_openapi = app.openapi


def openapi_with_optional_auth() -> dict:
    schema = _default_openapi()
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict) and operation.pop("x-fofu-optional-auth", False):
                operation["security"] = [{}, {"BearerAuth": []}]
    return schema


app.openapi = openapi_with_optional_auth


@app.get("/", include_in_schema=False)
def service_info() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "version": __version__,
        "docs": f"{settings.api_v1_prefix}/docs",
    }
