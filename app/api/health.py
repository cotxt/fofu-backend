from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import __version__
from app.config import Settings, get_settings
from app.database import get_db

router = APIRouter(tags=["operations"])


@router.get("/health/live", include_in_schema=False)
def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/health/ready", include_in_schema=False)
def ready(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "database_unavailable", "message": "Database is not ready."},
        ) from exc
    push_worker = getattr(request.app.state, "push_worker", None)
    if settings.apns_enabled and (push_worker is None or push_worker.done()):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "push_worker_unavailable",
                "message": "The push notification worker is not ready.",
            },
        )
    return {"status": "ready", "environment": settings.environment}
