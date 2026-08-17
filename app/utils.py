from __future__ import annotations

import base64
import binascii
import hashlib
import math

from fastapi import HTTPException, status

SUPPORTED_LOCALES = {
    "en",
    "fr",
    "es",
    "de",
    "it",
    "pt-BR",
    "ja",
    "zh-Hans",
    "zh-Hant",
    "ko",
    "ar",
    "hi",
    "id",
    "nl",
    "pl",
    "ru",
    "th",
    "tr",
    "uk",
    "vi",
}
MAX_CURSOR_OFFSET = 1_000_000


def normalize_locale(locale: str | None) -> str:
    if not locale:
        return "en"
    normalized = locale.replace("_", "-")
    if normalized in SUPPORTED_LOCALES:
        return normalized
    language = normalized.split("-", 1)[0].lower()
    if language == "zh":
        return (
            "zh-Hant"
            if any(tag in normalized.lower() for tag in ("hant", "tw", "hk"))
            else "zh-Hans"
        )
    match = next((item for item in SUPPORTED_LOCALES if item.split("-", 1)[0] == language), None)
    return match or "en"


def localized(en: str, ko: str | None, locale: str | None) -> str:
    return ko if normalize_locale(locale) == "ko" and ko else en


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_cursor", "message": "The pagination cursor is invalid."},
        ) from exc
    if value < 0 or value > MAX_CURSOR_OFFSET:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_cursor", "message": "The pagination cursor is invalid."},
        )
    return value


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return round(radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def privacy_hash(value: str | None, secret: str) -> str | None:
    if not value:
        return None
    return hashlib.sha256(f"{secret}:{value}".encode()).hexdigest()
