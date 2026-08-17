from starlette.requests import Request

from app import rate_limit


def test_qr_limiter_uses_trusted_asgi_client_not_raw_forwarded_header(monkeypatch) -> None:
    keys: list[str] = []
    monkeypatch.setattr(rate_limit.qr_limiter, "check", keys.append)
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"203.0.113.99")],
            "client": ("198.51.100.7", 54321),
        }
    )

    rate_limit.enforce_qr_rate_limit(request)

    assert keys == ["198.51.100.7"]


def test_google_auth_limiter_uses_trusted_asgi_client(monkeypatch) -> None:
    keys: list[str] = []
    monkeypatch.setattr(rate_limit.google_auth_limiter, "check", keys.append)
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"203.0.113.99")],
            "client": ("198.51.100.8", 54321),
        }
    )

    rate_limit.enforce_google_auth_rate_limit(request)

    assert keys == ["198.51.100.8"]
