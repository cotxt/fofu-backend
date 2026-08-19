from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import yaml
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "deploy" / "aws" / "compose.production.yaml"
CADDYFILE_PATH = ROOT / "deploy" / "aws" / "Caddyfile"
WRAPPER_PATH = ROOT / "deploy" / "aws" / "compose-with-ssm.sh"
UNIT_PATH = ROOT / "deploy" / "aws" / "fofu.service"


def load_compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def test_production_proxy_network_and_trusted_ip_stay_aligned() -> None:
    compose = load_compose()
    api = compose["services"]["api"]
    proxy = compose["services"]["proxy"]
    networks = compose["networks"]

    assert "ports" not in api
    assert networks["proxy-internal"]["internal"] is True
    assert networks["proxy-internal"]["ipam"]["config"] == [{"subnet": "172.30.0.0/24"}]
    assert api["networks"]["proxy-internal"]["ipv4_address"] == "172.30.0.3"
    assert proxy["networks"]["proxy-internal"]["ipv4_address"] == "172.30.0.2"
    assert set(api["networks"]) & set(proxy["networks"]) == {"proxy-internal"}
    assert "--forwarded-allow-ips=172.30.0.2" in api["command"]
    assert "--forwarded-allow-ips=*" not in api["command"]


def test_caddy_healthcheck_is_bound_to_loopback() -> None:
    compose = load_compose()
    health_test = compose["services"]["proxy"]["healthcheck"]["test"]
    caddyfile = CADDYFILE_PATH.read_text()

    assert health_test[-1] == "http://127.0.0.1:2015/health"
    assert "http://127.0.0.1:2015 {\n\tbind 127.0.0.1" in caddyfile
    assert "\trespond /health 200" in caddyfile


def test_caddy_access_log_cannot_capture_revocable_qr_paths() -> None:
    caddyfile = CADDYFILE_PATH.read_text()

    # Caddy sees the raw URL before the application's Uvicorn redaction filter.
    # Keep access logging off until a tested path-redaction encoder is configured.
    assert "\n\tlog {" not in caddyfile


def test_runtime_wrapper_enforces_safe_secret_handling() -> None:
    wrapper = WRAPPER_PATH.read_text()

    assert "up | create | run)" in wrapper
    assert "refusing to render Compose configuration" in wrapper
    assert "replace every example.com placeholder" in wrapper
    assert "sslmode=require" in wrapper
    assert "sslmode=verify-full" in wrapper
    assert "for attempt in 1 2 3 4 5" in wrapper


def test_systemd_retries_a_failed_initial_start() -> None:
    unit = UNIT_PATH.read_text()

    assert "Restart=on-failure" in unit
    assert "RestartSec=15s" in unit


async def proxy_scope(client_ip: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured.update(scheme=scope["scheme"], client=scope["client"])
        body = json.dumps(captured).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="172.30.0.2")
    transport = httpx.ASGITransport(app=wrapped, client=(client_ip, 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        response = await client.get(
            "/",
            headers={
                "X-Forwarded-For": "203.0.113.8",
                "X-Forwarded-Proto": "https",
            },
        )
    assert response.status_code == 200
    return response.json()


def test_only_caddy_ip_can_supply_forwarded_headers() -> None:
    trusted = asyncio.run(proxy_scope("172.30.0.2"))
    untrusted = asyncio.run(proxy_scope("172.30.0.99"))

    assert trusted == {"scheme": "https", "client": ["203.0.113.8", 0]}
    assert untrusted == {"scheme": "http", "client": ["172.30.0.99", 12345]}
