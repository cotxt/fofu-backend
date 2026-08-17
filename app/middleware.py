from __future__ import annotations

import uuid

from fastapi import FastAPI, Request


def install_http_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        supplied = request.headers.get("x-request-id", "")
        request.state.request_id = supplied[:100] if supplied else str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(("/admin", "/api/v1/admin")):
            response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
        else:
            response.headers["Permissions-Policy"] = (
                "camera=(self), geolocation=(self), microphone=()"
            )
        private_prefixes = (
            "/admin",
            "/q/",
            "/api/v1/admin",
            "/api/v1/auth",
            "/api/v1/guest-sessions",
            "/api/v1/me",
            "/api/v1/conversations",
            "/api/v1/owner",
            "/api/v1/media",
            "/api/v1/cart",
            "/api/v1/sessions",
        )
        if request.headers.get("authorization") or request.url.path.startswith(private_prefixes):
            response.headers["Cache-Control"] = "no-store"
        return response
