import logging

from app.logging_filters import RedactAPNsDeviceURLFilter, RedactQRPathFilter


def test_qr_secret_is_redacted_from_uvicorn_access_log() -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", "GET", "/q/super-secret-code", "1.1", 307),
        exc_info=None,
    )
    assert RedactQRPathFilter().filter(record)
    assert record.args[2] == "/q/[redacted]"


def test_apns_token_is_redacted_from_httpx_log() -> None:
    token = "ab" * 32
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: POST %s "HTTP/2 200 OK"',
        args=(f"https://api.push.apple.com/3/device/{token}",),
        exc_info=None,
    )

    assert RedactAPNsDeviceURLFilter().filter(record)
    assert token not in record.getMessage()
    assert "/3/device/[redacted]" in record.getMessage()
