import pytest
from fastapi import HTTPException

from app.utils import (
    MAX_CURSOR_OFFSET,
    decode_cursor,
    encode_cursor,
    haversine_meters,
    normalize_locale,
)


def test_cursor_round_trip_and_rejects_garbage() -> None:
    assert decode_cursor(encode_cursor(37)) == 37
    assert decode_cursor(None) == 0
    with pytest.raises(HTTPException) as exc_info:
        decode_cursor("not-a-cursor")
    assert exc_info.value.status_code == 422
    with pytest.raises(HTTPException) as oversized:
        decode_cursor(encode_cursor(MAX_CURSOR_OFFSET + 1))
    assert oversized.value.status_code == 422


def test_locale_normalization_matches_ios_language_options() -> None:
    assert normalize_locale("pt_BR") == "pt-BR"
    assert normalize_locale("zh-TW") == "zh-Hant"
    assert normalize_locale("fr-CA") == "fr"
    assert normalize_locale("unsupported") == "en"


def test_haversine_distance_is_in_meters() -> None:
    # Roughly Hongdae station to a nearby restaurant block.
    distance = haversine_meters(37.5572, 126.9245, 37.5547, 126.9221)
    assert 250 <= distance <= 450
