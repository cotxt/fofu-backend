from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main as main_module
from app import models
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.services import push as push_service


def _settings(
    *,
    max_attempts: int = 3,
    key_path: Path = Path("/private/tmp/unused-apns-test-key.p8"),
) -> Settings:
    return Settings(
        _env_file=None,
        apns_enabled=True,
        apns_environment="sandbox",
        apns_team_id="TESTTEAM01",
        apns_key_id="TESTKEY001",
        apns_bundle_id="im.fofu.fofu",
        apns_private_key_path=key_path,
        push_delivery_max_attempts=max_attempts,
    )


class _Response:
    def __init__(
        self,
        status_code: int,
        body: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self) -> dict[str, object]:
        return self._body


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests: list[tuple[str, bytes, dict[str, str]]] = []

    def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> _Response:
        self.requests.append((url, content, headers))
        return self.response


def _claimed(*, registered_at: datetime | None = None) -> push_service.ClaimedDelivery:
    return push_service.ClaimedDelivery(
        id=str(uuid.uuid4()),
        lease_id=str(uuid.uuid4()),
        device_id=str(uuid.uuid4()),
        recipient_user_id=str(uuid.uuid4()),
        token="ab" * 16,
        title="FOFU",
        body="You have a new message.",
        payload={
            "fofu_type": "message",
            "conversation_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
        },
        attempt_count=1,
        device_registered_at=registered_at or datetime.now(timezone.utc),
    )


def _sender_with_response(response: _Response) -> tuple[push_service.APNsSender, _Client]:
    sender = object.__new__(push_service.APNsSender)
    client = _Client(response)
    sender._client = client  # type: ignore[attr-defined]
    sender._topic = "im.fofu.fofu"  # type: ignore[attr-defined]
    sender._base_url = "https://api.sandbox.push.apple.com"  # type: ignore[attr-defined]
    sender._provider_token = lambda **_kwargs: "provider-token"  # type: ignore[method-assign]
    sender._refresh_rejected_provider_token = lambda _token: True  # type: ignore[method-assign]
    return sender, client


def test_apns_sender_validates_and_signs_mounted_p8_key(tmp_path: Path) -> None:
    key_path = tmp_path / "AuthKey_TESTKEY001.p8"
    private_key = ec.generate_private_key(ec.SECP256R1())
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    sender = push_service.APNsSender(_settings(key_path=key_path))
    try:
        provider_token = sender._provider_token()
        assert len(provider_token.split(".")) == 3
    finally:
        sender.close()


def test_apns_sender_uses_private_generic_payload_and_retries_all_5xx() -> None:
    sender, client = _sender_with_response(
        _Response(599, {"reason": "InternalServerError"})
    )
    delivery = _claimed()

    result = sender.send(delivery)

    assert result.retryable is True
    assert result.retry_after_seconds == 15 * 60
    assert len(client.requests) == 1
    _, encoded, headers = client.requests[0]
    payload = json.loads(encoded)
    assert payload == {
        "aps": {
            "alert": {"title": "FOFU", "body": "You have a new message."},
            "sound": "default",
            "thread-id": delivery.payload["conversation_id"],
        },
        **delivery.payload,
    }
    assert headers["apns-push-type"] == "alert"
    assert headers["apns-topic"] == "im.fofu.fofu"


def test_apns_410_timestamp_and_expired_token_are_permanent() -> None:
    invalidated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    sender, _ = _sender_with_response(
        _Response(
            410,
            {
                "reason": "ExpiredToken",
                "timestamp": int(invalidated_at.timestamp() * 1000),
            },
        )
    )

    result = sender.send(_claimed())

    assert result.invalidate_device is True
    assert result.retryable is False
    assert result.token_invalidated_at is not None
    assert abs((result.token_invalidated_at - invalidated_at).total_seconds()) < 0.01


def test_rejected_provider_token_refresh_is_single_flight(monkeypatch) -> None:
    sender = object.__new__(push_service.APNsSender)
    sender._token_lock = threading.Lock()  # type: ignore[attr-defined]
    sender._cached_provider_token = ("rejected", int(time.time()) - 21 * 60)  # type: ignore[attr-defined]
    sender._team_id = "TESTTEAM01"  # type: ignore[attr-defined]
    sender._key_id = "TESTKEY001"  # type: ignore[attr-defined]
    sender._private_key = "private-key"  # type: ignore[attr-defined]
    calls: list[object] = []

    def fake_encode(*args, **kwargs):
        calls.append((args, kwargs))
        return "replacement"

    monkeypatch.setattr(push_service.jwt, "encode", fake_encode)
    assert sender._refresh_rejected_provider_token("rejected") is True
    assert sender._refresh_rejected_provider_token("rejected") is True
    assert len(calls) == 1

    sender._cached_provider_token = ("young", int(time.time()) - 60)
    assert sender._refresh_rejected_provider_token("young") is False
    assert len(calls) == 1


def _persist_delivery(
    *,
    status: str,
    attempt_count: int,
    lease_expires_at: datetime | None,
    registered_at: datetime | None = None,
) -> tuple[str, str, str, datetime]:
    now = datetime.now(timezone.utc)
    user_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())
    delivery_id = str(uuid.uuid4())
    registered = registered_at or now
    with SessionLocal() as db:
        db.add(
            models.User(
                id=user_id,
                email=f"push-worker-{uuid.uuid4().hex}@example.com",
                display_name="Push Worker",
                locale="en",
                is_guest=False,
                is_active=True,
                roles=["customer"],
            )
        )
        db.add(
            models.AuthSession(
                id=session_id,
                user_id=user_id,
                refresh_token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                client_type="ios",
                scope="full",
                is_guest_at_issue=False,
                expires_at=now + timedelta(days=2),
            )
        )
        db.flush()
        db.add(
            models.PushDevice(
                id=device_id,
                user_id=user_id,
                auth_session_id=session_id,
                installation_id=f"install-{uuid.uuid4()}",
                device_token=uuid.uuid4().hex,
                platform="ios",
                topic="im.fofu.fofu",
                environment="sandbox",
                locale="en",
                is_active=True,
                last_registered_at=registered,
            )
        )
        db.flush()
        db.add(
            models.PushDelivery(
                id=delivery_id,
                event_key=f"message:{uuid.uuid4()}",
                device_id=device_id,
                recipient_user_id=user_id,
                notification_type="message",
                title="FOFU",
                body="You have a new message.",
                payload={
                    "fofu_type": "message",
                    "conversation_id": str(uuid.uuid4()),
                    "message_id": str(uuid.uuid4()),
                },
                status=status,
                attempt_count=attempt_count,
                available_at=now - timedelta(minutes=1),
                lease_id="active-lease" if status == "processing" else None,
                lease_expires_at=lease_expires_at,
            )
        )
        db.commit()
    return delivery_id, device_id, user_id, registered


def test_max_attempt_processing_delivery_is_not_stolen_before_lease_expiry(
    client: TestClient,
) -> None:
    del client
    settings = _settings(max_attempts=3)
    now = datetime.now(timezone.utc)
    delivery_id, _, _, _ = _persist_delivery(
        status="processing",
        attempt_count=3,
        lease_expires_at=now + timedelta(minutes=1),
    )

    claimed = push_service.claim_push_deliveries(settings, now=now)
    assert all(item.id != delivery_id for item in claimed)
    with SessionLocal() as db:
        delivery = db.get(models.PushDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == "processing"
        assert delivery.lease_id == "active-lease"

    reclaimed = push_service.claim_push_deliveries(
        settings,
        now=now + timedelta(minutes=2),
    )
    assert all(item.id != delivery_id for item in reclaimed)
    with SessionLocal() as db:
        delivery = db.get(models.PushDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == "failed"
        assert delivery.last_error_code == "max_attempts_exceeded"


def test_stale_410_does_not_invalidate_a_newer_registration(client: TestClient) -> None:
    del client
    registered_at = datetime.now(timezone.utc)
    delivery_id, device_id, user_id, snapshot = _persist_delivery(
        status="processing",
        attempt_count=1,
        lease_expires_at=registered_at + timedelta(minutes=1),
        registered_at=registered_at,
    )
    with SessionLocal() as db:
        delivery = db.get(models.PushDelivery, delivery_id)
        assert delivery is not None
        delivery.lease_id = "finish-lease"
        db.commit()
    claim = push_service.ClaimedDelivery(
        id=delivery_id,
        lease_id="finish-lease",
        device_id=device_id,
        recipient_user_id=user_id,
        token=db_token(device_id),
        title="FOFU",
        body="You have a new message.",
        payload={},
        attempt_count=1,
        device_registered_at=snapshot,
    )
    push_service._finish_delivery(
        claim,
        push_service.APNsResult(
            succeeded=False,
            invalidate_device=True,
            reason="Unregistered",
            token_invalidated_at=registered_at - timedelta(seconds=1),
        ),
        _settings(),
    )

    with SessionLocal() as db:
        device = db.get(models.PushDevice, device_id)
        delivery = db.get(models.PushDelivery, delivery_id)
        assert device is not None and device.is_active is True
        assert delivery is not None and delivery.status == "failed"


def test_terminal_delivery_retention_never_deletes_pending_rows(
    client: TestClient,
) -> None:
    del client
    now = datetime.now(timezone.utc)
    terminal_id, _, _, _ = _persist_delivery(
        status="failed",
        attempt_count=3,
        lease_expires_at=None,
    )
    pending_id, _, _, _ = _persist_delivery(
        status="pending",
        attempt_count=0,
        lease_expires_at=None,
    )
    old = now - timedelta(days=31)
    with SessionLocal() as db:
        terminal = db.get(models.PushDelivery, terminal_id)
        pending = db.get(models.PushDelivery, pending_id)
        assert terminal is not None and pending is not None
        terminal.updated_at = old
        pending.updated_at = old
        db.commit()

    deleted = push_service.purge_terminal_push_deliveries(_settings(), now=now)

    assert deleted >= 1
    with SessionLocal() as db:
        assert db.get(models.PushDelivery, terminal_id) is None
        assert db.get(models.PushDelivery, pending_id) is not None


def test_retention_failure_does_not_starve_delivery_claims(monkeypatch) -> None:
    calls: list[str] = []

    def fail_cleanup(_settings: Settings) -> int:
        calls.append("cleanup")
        raise RuntimeError("simulated cleanup failure")

    async def exercise_worker() -> None:
        stop_event = asyncio.Event()

        def claim_once(_settings: Settings):
            calls.append("claim")
            stop_event.set()
            return []

        monkeypatch.setattr(
            push_service,
            "purge_terminal_push_deliveries",
            fail_cleanup,
        )
        monkeypatch.setattr(push_service, "claim_push_deliveries", claim_once)
        await asyncio.wait_for(
            push_service.run_push_worker(object(), _settings(), stop_event),  # type: ignore[arg-type]
            timeout=1,
        )

    asyncio.run(exercise_worker())

    assert calls == ["cleanup", "claim"]


def test_worker_survives_empty_poll_and_stops_cleanly(monkeypatch) -> None:
    calls: list[str] = []

    def no_cleanup(_settings: Settings) -> int:
        return 0

    def claim_nothing(_settings: Settings) -> list[push_service.ClaimedDelivery]:
        calls.append("claim")
        return []

    monkeypatch.setattr(push_service, "purge_terminal_push_deliveries", no_cleanup)
    monkeypatch.setattr(push_service, "claim_push_deliveries", claim_nothing)

    async def exercise_worker() -> None:
        settings = _settings()
        settings.push_worker_poll_seconds = 0.25
        stop_event = asyncio.Event()
        worker = asyncio.create_task(
            push_service.run_push_worker(
                object(),  # type: ignore[arg-type]
                settings,
                stop_event,
            )
        )
        await asyncio.sleep(0.35)
        assert not worker.done()
        assert len(calls) >= 2
        stop_event.set()
        await asyncio.wait_for(worker, timeout=1)

    asyncio.run(exercise_worker())


def test_lifespan_catches_asyncio_timeout_and_closes_sender(monkeypatch) -> None:
    events: list[str] = []

    class FakeSender:
        def __init__(self, _settings: Settings) -> None:
            events.append("started")

        def close(self) -> None:
            events.append("closed")

    async def stalled_worker(
        _sender: FakeSender,
        _settings: Settings,
        _stop_event: asyncio.Event,
    ) -> None:
        await asyncio.Event().wait()

    async def timeout_immediately(_future, *, timeout: float):  # type: ignore[no-untyped-def]
        assert timeout == 15
        raise asyncio.TimeoutError

    monkeypatch.setattr(main_module, "settings", _settings())
    monkeypatch.setattr(push_service, "APNsSender", FakeSender)
    monkeypatch.setattr(push_service, "run_push_worker", stalled_worker)
    monkeypatch.setattr(main_module.asyncio, "wait_for", timeout_immediately)

    async def exercise_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            worker = main_module.app.state.push_worker
            assert worker is not None and not worker.done()
        assert main_module.app.state.push_worker is None

    asyncio.run(exercise_lifespan())
    assert events == ["started", "closed"]


def test_lifespan_closes_sender_and_clears_worker_after_worker_failure(monkeypatch) -> None:
    events: list[str] = []

    class FakeSender:
        def __init__(self, _settings: Settings) -> None:
            events.append("started")

        def close(self) -> None:
            events.append("closed")

    async def failed_worker(
        _sender: FakeSender,
        _settings: Settings,
        _stop_event: asyncio.Event,
    ) -> None:
        raise RuntimeError("simulated worker failure")

    monkeypatch.setattr(main_module, "settings", _settings())
    monkeypatch.setattr(push_service, "APNsSender", FakeSender)
    monkeypatch.setattr(push_service, "run_push_worker", failed_worker)

    async def exercise_lifespan() -> None:
        with pytest.raises(RuntimeError, match="simulated worker failure"):
            async with main_module.lifespan(main_module.app):
                worker = main_module.app.state.push_worker
                assert worker is not None
                await asyncio.sleep(0)
                assert worker.done()
        assert main_module.app.state.push_worker is None

    asyncio.run(exercise_lifespan())
    assert events == ["started", "closed"]


def test_readiness_fails_when_enabled_push_worker_is_unavailable(
    client: TestClient,
) -> None:
    class WorkerState:
        def __init__(self, *, done: bool) -> None:
            self._done = done

        def done(self) -> bool:
            return self._done

    settings = _settings()
    main_module.app.dependency_overrides[get_settings] = lambda: settings
    try:
        main_module.app.state.push_worker = None
        absent = client.get("/health/ready")
        assert absent.status_code == 503
        assert absent.json()["error"]["code"] == "push_worker_unavailable"

        main_module.app.state.push_worker = WorkerState(done=True)
        stopped = client.get("/health/ready")
        assert stopped.status_code == 503
        assert stopped.json()["error"]["code"] == "push_worker_unavailable"

        main_module.app.state.push_worker = WorkerState(done=False)
        assert client.get("/health/ready").status_code == 200
    finally:
        main_module.app.dependency_overrides.pop(get_settings, None)
        main_module.app.state.push_worker = None


def db_token(device_id: str) -> str:
    with SessionLocal() as db:
        token = db.scalar(
            select(models.PushDevice.device_token).where(models.PushDevice.id == device_id)
        )
    assert token is not None
    return token
