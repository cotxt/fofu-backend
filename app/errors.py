from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status

logger = logging.getLogger(__name__)
_SENSITIVE_VALIDATION_FIELDS = {
    "password",
    "refresh_token",
    "replaced_refresh_token",
    "id_token",
    "code",
    "token",
    "device_token",
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _payload(code: str, message: str, request: Request, details: Any = None) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": details},
        "request_id": _request_id(request),
    }


def _strings_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, dict):
        return {item for child in value.values() for item in _strings_in(child)}
    if isinstance(value, (list, tuple)):
        return {item for child in value for item in _strings_in(child)}
    return set()


def _sensitive_values(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return set()
        if isinstance(decoded, (dict, list)):
            return _sensitive_values(decoded)
        return set()
    if isinstance(value, dict):
        values: set[str] = set()
        for key, child in value.items():
            if str(key).casefold() in _SENSITIVE_VALIDATION_FIELDS:
                values.update(_strings_in(child))
            else:
                values.update(_sensitive_values(child))
        return values
    if isinstance(value, (list, tuple)):
        return {item for child in value for item in _sensitive_values(child)}
    return set()


def _redact_validation_input(value: Any, sensitive_values: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if str(key).casefold() in _SENSITIVE_VALIDATION_FIELDS
                else _redact_validation_input(child, sensitive_values)
            )
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_validation_input(child, sensitive_values) for child in value]
    if isinstance(value, str):
        redacted = value
        for secret in sorted(sensitive_values, key=len, reverse=True):
            redacted = redacted.replace(secret, "[redacted]")
        return redacted
    return value


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    details = jsonable_encoder(exc.errors())
    sensitive_values = _sensitive_values(exc.body)
    for error in details:
        location_parts = tuple(str(part).casefold() for part in error.get("loc", []))
        location = set(location_parts)
        if location.intersection(_SENSITIVE_VALIDATION_FIELDS) and "input" in error:
            error["input"] = "[redacted]"
        elif "input" in error:
            # A model-level type error may contain the entire malformed body.
            # Scalars and arrays have no field key that can identify their secrets,
            # so fail closed at the root. Structured inputs retain useful context
            # while sensitive keys and duplicate secret values are scrubbed deeply.
            if location_parts == ("body",) and not isinstance(error["input"], dict):
                error["input"] = "[redacted]"
            else:
                error["input"] = _redact_validation_input(
                    error["input"], sensitive_values
                )
    return details


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code", "http_error"))
            message = str(detail.get("message", code.replace("_", " ").capitalize()))
            details = detail.get("details")
        else:
            code = "http_error"
            message = str(detail)
            details = None
        headers = exc.headers or {}
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(code, message, request, details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_payload(
                "validation_error",
                "One or more request fields are invalid.",
                request,
                _validation_details(exc),
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled API error request_id=%s", _request_id(request), exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload(
                "internal_error",
                "The server could not complete the request.",
                request,
            ),
        )
