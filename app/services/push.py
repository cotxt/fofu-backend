from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.rate_limit import push_registration_limiter
from app.schemas.push import PushDeviceRegistration, PushDeviceResponse

logger = logging.getLogger(__name__)

_PLATFORM = "ios"
_CANCELABLE_DELIVERY_STATUSES = {"pending", "processing"}
_TERMINAL_DELIVERY_STATUSES = {"sent", "failed", "cancelled"}
_INVALID_DEVICE_REASONS = {
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "ExpiredToken",
    "Unregistered",
}
_RETRYABLE_APNS_REASONS = {
    "ExpiredProviderToken",
    "InternalServerError",
    "ServiceUnavailable",
    "Shutdown",
    "TooManyProviderTokenUpdates",
    "TooManyRequests",
}


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _configured_topic(settings: Settings) -> str:
    if not settings.apns_enabled:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "push_not_configured",
            "Push notifications are not configured for this environment.",
        )
    # Settings' all-or-none validator guarantees this whenever APNs is enabled.
    if not settings.apns_bundle_id:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "push_not_configured",
            "Push notifications are not configured for this environment.",
        )
    return settings.apns_bundle_id


def _cancel_device_deliveries(
    db: Session,
    device_id: str,
    *,
    reason: str,
    excluding_delivery_id: str | None = None,
) -> None:
    filters: list[Any] = [
        models.PushDelivery.device_id == device_id,
        models.PushDelivery.status.in_(_CANCELABLE_DELIVERY_STATUSES),
    ]
    if excluding_delivery_id is not None:
        filters.append(models.PushDelivery.id != excluding_delivery_id)
    now = models.utcnow()
    db.execute(
        update(models.PushDelivery)
        .where(*filters)
        .values(
            status="cancelled",
            lease_id=None,
            lease_expires_at=None,
            last_error_code=reason,
            updated_at=now,
        )
    )


def deactivate_session_devices(
    db: Session,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    reason: str = "session_revoked",
) -> int:
    """Deactivate device bindings inside the caller's session-revocation transaction."""

    if session_id is None and user_id is None:
        return 0
    filters: list[Any] = [models.PushDevice.is_active.is_(True)]
    if session_id is not None:
        filters.append(models.PushDevice.auth_session_id == session_id)
    if user_id is not None:
        filters.append(models.PushDevice.user_id == user_id)
    device_ids = list(db.scalars(select(models.PushDevice.id).where(*filters)).all())
    if not device_ids:
        return 0
    now = models.utcnow()
    db.execute(
        update(models.PushDevice)
        .where(models.PushDevice.id.in_(device_ids))
        .values(
            is_active=False,
            invalidated_reason=reason,
            invalidated_at=now,
            updated_at=now,
        )
    )
    db.execute(
        update(models.PushDelivery)
        .where(
            models.PushDelivery.device_id.in_(device_ids),
            models.PushDelivery.status.in_(_CANCELABLE_DELIVERY_STATUSES),
        )
        .values(
            status="cancelled",
            lease_id=None,
            lease_expires_at=None,
            last_error_code=reason,
            updated_at=now,
        )
    )
    return len(device_ids)


def register_device(
    db: Session,
    *,
    user: models.User,
    auth_session: models.AuthSession,
    installation_id: str,
    payload: PushDeviceRegistration,
    settings: Settings | None = None,
) -> PushDeviceResponse:
    settings = settings or get_settings()
    topic = _configured_topic(settings)
    if (
        auth_session.client_type != "ios"
        or auth_session.scope != "full"
        or user.is_guest
    ):
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "full_ios_push_session_required",
            "A registered full iOS app session is required for push registration.",
        )
    push_registration_limiter.check(user.id)
    if payload.environment != settings.apns_environment:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "push_environment_mismatch",
            "The device token environment does not match this API environment.",
        )

    installation_filters = (
        models.PushDevice.platform == _PLATFORM,
        models.PushDevice.topic == topic,
        models.PushDevice.environment == payload.environment,
        models.PushDevice.installation_id == installation_id,
    )
    token_filters = (
        models.PushDevice.platform == _PLATFORM,
        models.PushDevice.topic == topic,
        models.PushDevice.environment == payload.environment,
        models.PushDevice.device_token == payload.token,
    )
    # Serialize the cap check for this user in PostgreSQL. SQLite serializes the
    # eventual write transaction and remains sufficient for local/test use.
    db.scalar(select(models.User.id).where(models.User.id == user.id).with_for_update())
    now = models.utcnow()
    obsolete_device_ids = list(
        db.scalars(
            select(models.PushDevice.id).where(
                models.PushDevice.user_id == user.id,
                models.PushDevice.platform == _PLATFORM,
                models.PushDevice.is_active.is_(True),
                or_(
                    models.PushDevice.topic != topic,
                    models.PushDevice.environment != payload.environment,
                ),
            )
        ).all()
    )
    if obsolete_device_ids:
        db.execute(
            update(models.PushDevice)
            .where(models.PushDevice.id.in_(obsolete_device_ids))
            .values(
                is_active=False,
                invalidated_reason="configuration_changed",
                invalidated_at=now,
                updated_at=now,
            )
        )
        db.execute(
            update(models.PushDelivery)
            .where(
                models.PushDelivery.device_id.in_(obsolete_device_ids),
                models.PushDelivery.status.in_(_CANCELABLE_DELIVERY_STATUSES),
            )
            .values(
                status="cancelled",
                lease_id=None,
                lease_expires_at=None,
                last_error_code="configuration_changed",
                updated_at=now,
            )
        )
    stale_device_ids = list(
        db.scalars(
            select(models.PushDevice.id)
            .join(
                models.AuthSession,
                models.AuthSession.id == models.PushDevice.auth_session_id,
            )
            .where(
                models.PushDevice.user_id == user.id,
                models.PushDevice.platform == _PLATFORM,
                models.PushDevice.topic == topic,
                models.PushDevice.environment == payload.environment,
                models.PushDevice.is_active.is_(True),
                or_(
                    models.AuthSession.user_id != models.PushDevice.user_id,
                    models.AuthSession.revoked_at.is_not(None),
                    models.AuthSession.expires_at <= now,
                ),
            )
        ).all()
    )
    if stale_device_ids:
        db.execute(
            update(models.PushDevice)
            .where(models.PushDevice.id.in_(stale_device_ids))
            .values(
                is_active=False,
                invalidated_reason="session_inactive",
                invalidated_at=now,
                updated_at=now,
            )
        )
        db.execute(
            update(models.PushDelivery)
            .where(
                models.PushDelivery.device_id.in_(stale_device_ids),
                models.PushDelivery.status.in_(_CANCELABLE_DELIVERY_STATUSES),
            )
            .values(
                status="cancelled",
                lease_id=None,
                lease_expires_at=None,
                last_error_code="session_inactive",
                updated_at=now,
            )
        )
    installation_device = db.scalar(
        select(models.PushDevice).where(*installation_filters).with_for_update()
    )
    token_device = db.scalar(select(models.PushDevice).where(*token_filters).with_for_update())

    replaces_active_device = any(
        device is not None and device.user_id == user.id and device.is_active
        for device in (installation_device, token_device)
    )
    active_device_count = int(
        db.scalar(
            select(func.count(models.PushDevice.id))
            .join(
                models.AuthSession,
                models.AuthSession.id == models.PushDevice.auth_session_id,
            )
            .where(
                models.PushDevice.user_id == user.id,
                models.PushDevice.platform == _PLATFORM,
                models.PushDevice.topic == topic,
                models.PushDevice.environment == payload.environment,
                models.PushDevice.is_active.is_(True),
                models.AuthSession.user_id == models.PushDevice.user_id,
                models.AuthSession.revoked_at.is_(None),
                models.AuthSession.expires_at > now,
            )
        )
        or 0
    )
    if (
        not replaces_active_device
        and active_device_count >= settings.push_max_active_devices_per_user
    ):
        raise _error(
            status.HTTP_409_CONFLICT,
            "push_device_limit_reached",
            "This account has reached its active push-device limit.",
        )

    target = token_device or installation_device
    if token_device is not None and installation_device is not None:
        if token_device.id != installation_device.id:
            _cancel_device_deliveries(
                db,
                installation_device.id,
                reason="installation_rebound",
            )
            db.delete(installation_device)
            db.flush()
    if target is None:
        target = models.PushDevice(
            user_id=user.id,
            auth_session_id=auth_session.id,
            installation_id=installation_id,
            device_token=payload.token,
            platform=_PLATFORM,
            topic=topic,
            environment=payload.environment,
        )
        db.add(target)
    elif target.user_id != user.id or target.auth_session_id != auth_session.id:
        _cancel_device_deliveries(db, target.id, reason="device_rebound")

    target.user_id = user.id
    target.auth_session_id = auth_session.id
    target.installation_id = installation_id
    target.device_token = payload.token
    target.locale = payload.locale or user.locale
    target.is_active = True
    target.invalidated_reason = None
    target.invalidated_at = None
    target.last_registered_at = now
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # A concurrent registration can race either unique identity. The caller can
        # safely retry the idempotent PUT rather than receiving a database-shaped 500.
        raise _error(
            status.HTTP_409_CONFLICT,
            "push_device_registration_conflict",
            "The push device changed concurrently; retry registration.",
        ) from exc
    db.refresh(target)
    return PushDeviceResponse(
        installation_id=target.installation_id,
        environment=target.environment,  # type: ignore[arg-type]
        topic=target.topic,
        locale=target.locale,
        is_active=target.is_active,
        last_registered_at=target.last_registered_at,
    )


def unregister_device(
    db: Session,
    *,
    user: models.User,
    installation_id: str,
) -> None:
    devices = list(
        db.scalars(
            select(models.PushDevice)
            .where(
                models.PushDevice.user_id == user.id,
                models.PushDevice.platform == _PLATFORM,
                models.PushDevice.installation_id == installation_id,
                models.PushDevice.is_active.is_(True),
            )
            .with_for_update()
        ).all()
    )
    if devices:
        now = models.utcnow()
        for device in devices:
            device.is_active = False
            device.invalidated_reason = "user_unregistered"
            device.invalidated_at = now
            _cancel_device_deliveries(db, device.id, reason="user_unregistered")
        db.commit()


def _generic_message_notification_body(locale: str | None) -> str:
    return (
        "새 메시지가 도착했습니다."
        if (locale or "").casefold().startswith("ko")
        else "You have a new message."
    )


def enqueue_message_notifications(
    db: Session,
    *,
    message: models.Message,
    settings: Settings | None = None,
) -> int:
    """Add per-device deliveries to the same transaction as a newly persisted message."""

    settings = settings or get_settings()
    if not settings.apns_enabled or not settings.apns_bundle_id:
        return 0
    now = models.utcnow()
    devices = list(
        db.scalars(
            select(models.PushDevice)
            .join(
                models.ConversationParticipant,
                models.ConversationParticipant.user_id == models.PushDevice.user_id,
            )
            .join(
                models.AuthSession,
                models.AuthSession.id == models.PushDevice.auth_session_id,
            )
            .join(models.User, models.User.id == models.PushDevice.user_id)
            .where(
                models.ConversationParticipant.conversation_id == message.conversation_id,
                models.ConversationParticipant.user_id != message.sender_user_id,
                models.PushDevice.is_active.is_(True),
                models.PushDevice.platform == _PLATFORM,
                models.PushDevice.topic == settings.apns_bundle_id,
                models.PushDevice.environment == settings.apns_environment,
                models.AuthSession.user_id == models.PushDevice.user_id,
                models.AuthSession.revoked_at.is_(None),
                models.AuthSession.expires_at > now,
                models.User.is_active.is_(True),
            )
        ).all()
    )
    event_key = f"message:{message.id}"
    for device in devices:
        db.add(
            models.PushDelivery(
                event_key=event_key,
                device_id=device.id,
                recipient_user_id=device.user_id,
                notification_type="message",
                title="FOFU",
                body=_generic_message_notification_body(device.locale),
                payload={
                    "fofu_type": "message",
                    "conversation_id": message.conversation_id,
                    "message_id": message.id,
                },
                available_at=now,
            )
        )
    return len(devices)


@dataclass(frozen=True)
class ClaimedDelivery:
    id: str
    lease_id: str
    device_id: str
    recipient_user_id: str
    token: str = field(repr=False)
    title: str
    body: str = field(repr=False)
    payload: dict[str, Any] = field(repr=False)
    attempt_count: int
    device_registered_at: datetime


@dataclass(frozen=True)
class APNsResult:
    succeeded: bool
    retryable: bool = False
    invalidate_device: bool = False
    reason: str | None = None
    apns_id: str | None = None
    retry_after_seconds: float | None = None
    token_invalidated_at: datetime | None = None


class APNsSender:
    """Token-authenticated APNs HTTP/2 adapter that never logs device tokens."""

    def __init__(self, settings: Settings) -> None:
        if not settings.apns_enabled:
            raise RuntimeError("APNs sender cannot start while push is disabled")
        if not all(
            (
                settings.apns_team_id,
                settings.apns_key_id,
                settings.apns_bundle_id,
                settings.apns_private_key_path,
            )
        ):
            raise RuntimeError("APNs configuration is incomplete")
        key_path = Path(settings.apns_private_key_path).expanduser()
        try:
            if key_path.stat().st_size > 32 * 1024:
                raise RuntimeError("APNs private key file is unexpectedly large")
            self._private_key = key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("APNs private key file is unavailable") from exc
        self._team_id = settings.apns_team_id
        self._key_id = settings.apns_key_id
        self._topic = settings.apns_bundle_id
        self._base_url = (
            "https://api.push.apple.com"
            if settings.apns_environment == "production"
            else "https://api.sandbox.push.apple.com"
        )
        self._token_lock = threading.Lock()
        self._cached_provider_token: tuple[str, int] | None = None
        self._client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        # Parse/sign once during application startup so an invalid secret fails closed.
        self._provider_token(force=True)

    def close(self) -> None:
        self._client.close()

    def _provider_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            now = int(time.time())
            if (
                not force
                and self._cached_provider_token is not None
                and now - self._cached_provider_token[1] < 45 * 60
            ):
                return self._cached_provider_token[0]
            encoded = jwt.encode(
                {"iss": self._team_id, "iat": now},
                self._private_key,
                algorithm="ES256",
                headers={"kid": self._key_id},
            )
            self._cached_provider_token = (encoded, now)
            return encoded

    def _refresh_rejected_provider_token(self, rejected_token: str) -> bool:
        with self._token_lock:
            cached = self._cached_provider_token
            if cached is None:
                return False
            if cached[0] != rejected_token:
                return True
            now = int(time.time())
            # Apple rejects provider-token updates made more than once within 20
            # minutes. A very young rejected token normally indicates clock/config
            # trouble, so leave it cached and let the bounded delivery retry handle it.
            if now - cached[1] < 20 * 60:
                return False
            encoded = jwt.encode(
                {"iss": self._team_id, "iat": now},
                self._private_key,
                algorithm="ES256",
                headers={"kid": self._key_id},
            )
            self._cached_provider_token = (encoded, now)
            return True

    def send(self, delivery: ClaimedDelivery) -> APNsResult:
        aps: dict[str, Any] = {
            "alert": {"title": delivery.title, "body": delivery.body},
            "sound": "default",
        }
        conversation_id = delivery.payload.get("conversation_id")
        if isinstance(conversation_id, str):
            aps["thread-id"] = conversation_id
        encoded_payload = json.dumps(
            {"aps": aps, **delivery.payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_payload) > 4096:
            return APNsResult(succeeded=False, reason="PayloadTooLarge")

        provider_token = self._provider_token()
        headers = {
            "authorization": f"bearer {provider_token}",
            "apns-id": delivery.id,
            "apns-topic": self._topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": str(int(time.time()) + 24 * 60 * 60),
        }
        try:
            response = self._client.post(
                f"{self._base_url}/3/device/{delivery.token}",
                content=encoded_payload,
                headers=headers,
            )
        except httpx.HTTPError:
            return APNsResult(succeeded=False, retryable=True, reason="NetworkError")

        apns_id = response.headers.get("apns-id")
        if response.status_code == 200:
            return APNsResult(succeeded=True, apns_id=apns_id)
        reason = "UnknownAPNsError"
        response_body: dict[str, Any] = {}
        try:
            body = response.json()
            if isinstance(body, dict):
                response_body = body
                if isinstance(body.get("reason"), str):
                    reason = body["reason"][:100]
        except ValueError:
            pass
        provider_token_refreshed = False
        if reason == "ExpiredProviderToken":
            provider_token_refreshed = self._refresh_rejected_provider_token(provider_token)
        retry_after = _retry_after_seconds(response.headers.get("retry-after"))
        if retry_after is None and 500 <= response.status_code < 600:
            retry_after = 15 * 60
        if retry_after is None and reason == "TooManyProviderTokenUpdates":
            retry_after = 20 * 60
        if (
            retry_after is None
            and reason == "ExpiredProviderToken"
            and not provider_token_refreshed
        ):
            retry_after = 20 * 60
        retryable = (
            response.status_code == 429
            or 500 <= response.status_code < 600
            or reason in _RETRYABLE_APNS_REASONS
        )
        return APNsResult(
            succeeded=False,
            retryable=retryable,
            invalidate_device=reason in _INVALID_DEVICE_REASONS,
            reason=reason,
            apns_id=apns_id,
            retry_after_seconds=retry_after,
            token_invalidated_at=_apns_invalidation_time(response_body.get("timestamp")),
        )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 3600.0))
    except ValueError:
        return None


def _apns_invalidation_time(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        # APNs returns this timestamp in milliseconds since the Unix epoch.
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _cancel_expired_session_deliveries(db: Session, now: datetime) -> None:
    inactive_device_ids = list(
        db.scalars(
            select(models.PushDevice.id)
            .join(
                models.AuthSession,
                models.AuthSession.id == models.PushDevice.auth_session_id,
            )
            .join(models.User, models.User.id == models.PushDevice.user_id)
            .where(
                models.PushDevice.is_active.is_(True),
                or_(
                    models.AuthSession.user_id != models.PushDevice.user_id,
                    models.AuthSession.revoked_at.is_not(None),
                    models.AuthSession.expires_at <= now,
                    models.User.is_active.is_(False),
                ),
            )
        ).all()
    )
    if inactive_device_ids:
        db.execute(
            update(models.PushDevice)
            .where(models.PushDevice.id.in_(inactive_device_ids))
            .values(
                is_active=False,
                invalidated_reason="session_inactive",
                invalidated_at=now,
                updated_at=now,
            )
        )
    valid_device_ids = (
        select(models.PushDevice.id)
        .join(models.AuthSession, models.AuthSession.id == models.PushDevice.auth_session_id)
        .join(models.User, models.User.id == models.PushDevice.user_id)
        .where(
            models.PushDevice.is_active.is_(True),
            models.AuthSession.user_id == models.PushDevice.user_id,
            models.AuthSession.revoked_at.is_(None),
            models.AuthSession.expires_at > now,
            models.User.is_active.is_(True),
        )
    )
    db.execute(
        update(models.PushDelivery)
        .where(
            models.PushDelivery.status.in_(_CANCELABLE_DELIVERY_STATUSES),
            ~models.PushDelivery.device_id.in_(valid_device_ids),
        )
        .values(
            status="cancelled",
            lease_id=None,
            lease_expires_at=None,
            last_error_code="session_inactive",
            updated_at=now,
        )
    )


def claim_push_deliveries(
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[ClaimedDelivery]:
    current = now or models.utcnow()
    lease_until = current + timedelta(seconds=settings.push_delivery_lease_seconds)
    with SessionLocal() as db:
        _cancel_expired_session_deliveries(db, current)
        db.execute(
            update(models.PushDelivery)
            .where(
                or_(
                    (models.PushDelivery.status == "pending")
                    & (
                        models.PushDelivery.attempt_count
                        >= settings.push_delivery_max_attempts
                    ),
                    (models.PushDelivery.status == "processing")
                    & (
                        models.PushDelivery.attempt_count
                        >= settings.push_delivery_max_attempts
                    )
                    & (models.PushDelivery.lease_expires_at <= current),
                )
            )
            .values(
                status="failed",
                lease_id=None,
                lease_expires_at=None,
                last_error_code="max_attempts_exceeded",
                updated_at=current,
            )
        )
        statement = (
            select(models.PushDelivery, models.PushDevice)
            .join(models.PushDevice, models.PushDevice.id == models.PushDelivery.device_id)
            .join(models.AuthSession, models.AuthSession.id == models.PushDevice.auth_session_id)
            .join(models.User, models.User.id == models.PushDevice.user_id)
            .where(
                or_(
                    (
                        models.PushDelivery.status == "pending"
                    ) & (models.PushDelivery.available_at <= current),
                    (
                        models.PushDelivery.status == "processing"
                    ) & (models.PushDelivery.lease_expires_at <= current),
                ),
                models.PushDelivery.recipient_user_id == models.PushDevice.user_id,
                models.PushDelivery.attempt_count
                < settings.push_delivery_max_attempts,
                models.PushDevice.is_active.is_(True),
                models.PushDevice.platform == _PLATFORM,
                models.PushDevice.topic == settings.apns_bundle_id,
                models.PushDevice.environment == settings.apns_environment,
                models.AuthSession.user_id == models.PushDevice.user_id,
                models.AuthSession.revoked_at.is_(None),
                models.AuthSession.expires_at > current,
                models.User.is_active.is_(True),
            )
            .order_by(models.PushDelivery.available_at, models.PushDelivery.created_at)
            .limit(settings.push_worker_batch_size)
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(
                of=models.PushDelivery,
                skip_locked=True,
            )
        rows = db.execute(statement).all()
        claimed: list[ClaimedDelivery] = []
        for delivery, device in rows:
            lease_id = str(uuid.uuid4())
            delivery.status = "processing"
            delivery.attempt_count += 1
            delivery.lease_id = lease_id
            delivery.lease_expires_at = lease_until
            claimed.append(
                ClaimedDelivery(
                    id=delivery.id,
                    lease_id=lease_id,
                    device_id=device.id,
                    recipient_user_id=delivery.recipient_user_id,
                    token=device.device_token,
                    title=delivery.title,
                    body=delivery.body,
                    payload=dict(delivery.payload or {}),
                    attempt_count=delivery.attempt_count,
                    device_registered_at=device.last_registered_at,
                )
            )
        db.commit()
        return claimed


def _claim_is_current(db: Session, delivery: ClaimedDelivery) -> bool:
    row = db.execute(
        select(models.PushDelivery, models.PushDevice, models.AuthSession, models.User)
        .join(models.PushDevice, models.PushDevice.id == models.PushDelivery.device_id)
        .join(models.AuthSession, models.AuthSession.id == models.PushDevice.auth_session_id)
        .join(models.User, models.User.id == models.PushDevice.user_id)
        .where(models.PushDelivery.id == delivery.id)
    ).one_or_none()
    if row is None:
        return False
    queued, device, session, user = row
    return bool(
        queued.status == "processing"
        and queued.lease_id == delivery.lease_id
        and queued.recipient_user_id == device.user_id == delivery.recipient_user_id
        and device.id == delivery.device_id
        and device.device_token == delivery.token
        and device.is_active
        and session.user_id == device.user_id
        and session.revoked_at is None
        and _aware(session.expires_at) > datetime.now(timezone.utc)
        and user.is_active
    )


def _finish_delivery(
    delivery: ClaimedDelivery,
    result: APNsResult,
    settings: Settings,
) -> None:
    with SessionLocal() as db:
        queued = db.scalar(
            select(models.PushDelivery)
            .where(models.PushDelivery.id == delivery.id)
            .with_for_update()
        )
        if (
            queued is None
            or queued.status != "processing"
            or queued.lease_id != delivery.lease_id
        ):
            return
        now = models.utcnow()
        queued.apns_id = result.apns_id
        queued.last_error_code = result.reason
        queued.lease_id = None
        queued.lease_expires_at = None
        if result.succeeded:
            queued.status = "sent"
            queued.sent_at = now
        elif result.invalidate_device:
            queued.status = "failed"
            device = db.get(models.PushDevice, delivery.device_id)
            same_token_binding = bool(
                device is not None
                and device.device_token == delivery.token
                and device.user_id == delivery.recipient_user_id
            )
            if result.token_invalidated_at is not None:
                invalidation_covers_registration = bool(
                    device is not None
                    and _aware(device.last_registered_at) <= result.token_invalidated_at
                )
            else:
                invalidation_covers_registration = bool(
                    device is not None
                    and _aware(device.last_registered_at)
                    <= _aware(delivery.device_registered_at)
                )
            if same_token_binding and invalidation_covers_registration:
                assert device is not None
                device.is_active = False
                device.invalidated_reason = result.reason or "invalid_device_token"
                device.invalidated_at = now
                _cancel_device_deliveries(
                    db,
                    device.id,
                    reason=result.reason or "invalid_device_token",
                    excluding_delivery_id=queued.id,
                )
        elif result.retryable and queued.attempt_count < settings.push_delivery_max_attempts:
            queued.status = "pending"
            queued.available_at = now + timedelta(
                seconds=_retry_delay_seconds(delivery, result)
            )
        else:
            queued.status = "failed"
        db.commit()


def _retry_delay_seconds(delivery: ClaimedDelivery, result: APNsResult) -> float:
    if result.retry_after_seconds is not None:
        return result.retry_after_seconds
    base = min(2 ** min(delivery.attempt_count, 10), 1800)
    deterministic_jitter = (int(delivery.id.replace("-", "")[:8], 16) % 1000) / 1000
    return min(float(base) * (1 + deterministic_jitter * 0.25), 3600.0)


def dispatch_claimed_delivery(
    sender: APNsSender,
    delivery: ClaimedDelivery,
    settings: Settings,
) -> None:
    with SessionLocal() as db:
        current = _claim_is_current(db, delivery)
    if not current:
        _finish_delivery(
            delivery,
            APNsResult(succeeded=False, reason="device_binding_changed"),
            settings,
        )
        return
    result = sender.send(delivery)
    _finish_delivery(delivery, result, settings)


def purge_terminal_push_deliveries(
    settings: Settings,
    *,
    now: datetime | None = None,
    batch_size: int = 1000,
) -> int:
    cutoff = (now or models.utcnow()) - timedelta(
        days=settings.push_delivery_retention_days
    )
    with SessionLocal() as db:
        delivery_ids = list(
            db.scalars(
                select(models.PushDelivery.id)
                .where(
                    models.PushDelivery.status.in_(_TERMINAL_DELIVERY_STATUSES),
                    models.PushDelivery.updated_at < cutoff,
                )
                .order_by(models.PushDelivery.updated_at, models.PushDelivery.id)
                .limit(batch_size)
            ).all()
        )
        if delivery_ids:
            db.execute(
                delete(models.PushDelivery).where(
                    models.PushDelivery.id.in_(delivery_ids)
                )
            )
            db.commit()
        return len(delivery_ids)


async def run_push_worker(
    sender: APNsSender,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    next_cleanup_at = 0.0
    while not stop_event.is_set():
        monotonic_now = time.monotonic()
        if monotonic_now >= next_cleanup_at:
            # Maintenance must never gate delivery. Advance the deadline before
            # attempting cleanup so a persistent cleanup error does not hot-loop.
            next_cleanup_at = monotonic_now + 60 * 60
            try:
                await asyncio.to_thread(purge_terminal_push_deliveries, settings)
            except Exception:
                logger.error("Push delivery retention cleanup failed")
        try:
            deliveries = await asyncio.to_thread(claim_push_deliveries, settings)
            if deliveries:
                await asyncio.gather(
                    *(
                        asyncio.to_thread(
                            dispatch_claimed_delivery,
                            sender,
                            delivery,
                            settings,
                        )
                        for delivery in deliveries
                    )
                )
                continue
        except Exception:
            # The exception is deliberately not interpolated: HTTP client errors can
            # otherwise include a URL containing the device token.
            logger.error("Push delivery worker iteration failed")
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.push_worker_poll_seconds,
            )
        except asyncio.TimeoutError:
            pass
