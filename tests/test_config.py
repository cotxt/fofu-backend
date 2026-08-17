from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "jwt_secret",
    [
        "local-development-secret-change-before-deploy",
        "replace-with-at-least-32-random-characters",
        "local-compose-secret-change-before-deploy-123456",
    ],
)
def test_deployed_environment_rejects_known_insecure_secrets(
    environment: str, jwt_secret: str
) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(
            _env_file=None,
            environment=environment,  # type: ignore[arg-type]
            jwt_secret=jwt_secret,
            auto_create_schema=False,
            cors_origins=["https://app.example.com"],
        )


def test_production_rejects_wildcard_cors() -> None:
    with pytest.raises(ValidationError, match="Wildcard CORS"):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret="x" * 48,
            auto_create_schema=False,
            seed_demo_data=False,
            cors_origins=["*"],
        )


def test_production_rejects_known_demo_accounts() -> None:
    with pytest.raises(ValidationError, match="SEED_DEMO_DATA"):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret="x" * 48,
            auto_create_schema=False,
            seed_demo_data=True,
            cors_origins=["https://app.example.com"],
        )


def test_production_rejects_non_postgresql_database() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL DATABASE_URL"):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret="x" * 48,
            database_url="mysql+pymysql://fofu:secret@db/fofu",
            auto_create_schema=False,
            seed_demo_data=False,
            web_app_base_url="https://fofu.example",
            public_api_base_url="https://api.fofu.example",
            cors_origins=["https://fofu.example"],
        )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environment_accepts_explicit_secure_runtime_settings(
    environment: str,
) -> None:
    settings = Settings(
        _env_file=None,
        environment=environment,  # type: ignore[arg-type]
        jwt_secret="x" * 48,
        database_url="postgresql+psycopg://fofu:secret@db/fofu",
        auto_create_schema=False,
        seed_demo_data=False,
        web_app_base_url="https://fofu.example",
        public_api_base_url="https://api.fofu.example",
        cors_origins=["https://fofu.example"],
    )
    assert settings.environment == environment


def test_comma_separated_cors_is_supported() -> None:
    settings = Settings(
        _env_file=None,
        cors_origins="https://one.example, https://two.example",  # type: ignore[arg-type]
    )
    assert settings.cors_origins == ["https://one.example", "https://two.example"]


def test_google_oauth_client_ids_are_trimmed_and_deduplicated() -> None:
    settings = Settings(
        _env_file=None,
        google_oauth_client_ids=[
            " ios-client.apps.googleusercontent.com ",
            "ios-client.apps.googleusercontent.com",
            "web-client.apps.googleusercontent.com",
            " ",
        ],
    )
    assert settings.google_oauth_client_ids == [
        "ios-client.apps.googleusercontent.com",
        "web-client.apps.googleusercontent.com",
    ]


def test_google_oauth_client_ids_load_from_json_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "FOFU_GOOGLE_OAUTH_CLIENT_IDS",
        '["ios-client.apps.googleusercontent.com", "web-client.apps.googleusercontent.com"]',
    )
    settings = Settings(_env_file=None)
    assert settings.google_oauth_client_ids == [
        "ios-client.apps.googleusercontent.com",
        "web-client.apps.googleusercontent.com",
    ]


def test_unsigned_jwt_algorithm_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_algorithm="none")  # type: ignore[arg-type]


def test_apns_enabled_requires_complete_credentials() -> None:
    with pytest.raises(ValidationError, match="FOFU_APNS_ENABLED requires"):
        Settings(_env_file=None, apns_enabled=True)

    settings = Settings(
        _env_file=None,
        apns_enabled=True,
        apns_team_id="TESTTEAM01",
        apns_key_id="TESTKEY001",
        apns_bundle_id="im.fofu.fofu",
        apns_private_key_path=Path("/private/tmp/test-apns-key.p8"),
    )
    assert settings.apns_environment == "sandbox"


def test_production_rejects_sandbox_apns() -> None:
    with pytest.raises(ValidationError, match="APNS_ENVIRONMENT=production"):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret="x" * 48,
            database_url="postgresql+psycopg://fofu:secret@db/fofu",
            auto_create_schema=False,
            seed_demo_data=False,
            web_app_base_url="https://fofu.example",
            public_api_base_url="https://api.fofu.example",
            cors_origins=["https://fofu.example"],
            apns_enabled=True,
            apns_environment="sandbox",
            apns_team_id="TESTTEAM01",
            apns_key_id="TESTKEY001",
            apns_bundle_id="im.fofu.fofu",
            apns_private_key_path=Path("/run/secrets/apns-key.p8"),
        )
