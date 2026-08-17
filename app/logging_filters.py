from __future__ import annotations

import logging
import re

_QR_PATH = re.compile(r"(/(?:api/v1/)?q(?:r)?/)[^/?#\s]+")
_APNS_DEVICE_URL = re.compile(
    r"(https://api(?:\.sandbox)?\.push\.apple\.com/3/device/)[0-9A-Fa-f]+"
)


class RedactQRPathFilter(logging.Filter):
    """Prevent raw, revocable QR locators from appearing in Uvicorn access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access" or not isinstance(record.args, tuple):
            return True
        values = list(record.args)
        if len(values) >= 3 and isinstance(values[2], str):
            values[2] = _QR_PATH.sub(r"\1[redacted]", values[2])
            record.args = tuple(values)
        return True


class RedactAPNsDeviceURLFilter(logging.Filter):
    """Keep APNs device tokens out of optional httpx/httpcore request logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name not in {"httpx", "httpcore"}:
            return True
        rendered = record.getMessage()
        redacted = _APNS_DEVICE_URL.sub(r"\1[redacted]", rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def install_access_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, RedactQRPathFilter) for item in logger.filters):
        logger.addFilter(RedactQRPathFilter())
    for logger_name in ("httpx", "httpcore"):
        outbound_logger = logging.getLogger(logger_name)
        if not any(
            isinstance(item, RedactAPNsDeviceURLFilter)
            for item in outbound_logger.filters
        ):
            outbound_logger.addFilter(RedactAPNsDeviceURLFilter())
