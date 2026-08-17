import pytest

from app.config import Settings
from app.security import (
    TokenError,
    create_access_token,
    decode_access_token,
    digest_refresh_token,
    hash_password,
    new_refresh_token,
    verify_password,
)


def test_password_hash_round_trip() -> None:
    encoded = hash_password("a sufficiently long password")
    assert "sufficiently long" not in encoded
    assert verify_password("a sufficiently long password", encoded)
    assert not verify_password("wrong password", encoded)


def test_access_token_claims_and_signature() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        jwt_secret="test-secret-that-is-longer-than-thirty-two-bytes",
    )
    token, expires_in = create_access_token(
        user_id="user-1",
        session_id="session-1",
        roles=["customer"],
        is_guest=False,
        settings=settings,
    )
    claims = decode_access_token(token, settings)
    assert claims.user_id == "user-1"
    assert claims.session_id == "session-1"
    assert claims.roles == ["customer"]
    assert 0 < expires_in <= 15 * 60

    with pytest.raises(TokenError):
        decode_access_token(f"{token}corrupted", settings)


def test_refresh_tokens_are_random_and_only_digest_is_stored() -> None:
    first = new_refresh_token()
    second = new_refresh_token()
    assert first != second
    assert len(digest_refresh_token(first)) == 64
    assert digest_refresh_token(first) != digest_refresh_token(second)

