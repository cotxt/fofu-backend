from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_SECRETS = {
    "local-development-secret-change-before-deploy",
    "replace-with-at-least-32-random-characters",
    "local-compose-secret-change-before-deploy-123456",
}


class Settings(BaseSettings):
    """Runtime configuration shared by the API and migration environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FOFU_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Fofu API"
    environment: Literal["local", "test", "staging", "production"] = "local"
    api_v1_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./fofu.db"
    jwt_secret: str = "local-development-secret-change-before-deploy"
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "fofu-api"
    jwt_audience: str = "fofu-clients"
    google_oauth_client_ids: list[str] = Field(default_factory=list)
    apns_enabled: bool = False
    apns_environment: Literal["sandbox", "production"] = "sandbox"
    apns_team_id: str | None = None
    apns_key_id: str | None = None
    apns_bundle_id: str | None = None
    apns_private_key_path: Path | None = None
    push_worker_poll_seconds: float = Field(default=2.0, ge=0.25, le=60)
    push_worker_batch_size: int = Field(default=20, ge=1, le=100)
    push_delivery_max_attempts: int = Field(default=8, ge=1, le=20)
    push_delivery_lease_seconds: int = Field(default=60, ge=15, le=300)
    push_delivery_retention_days: int = Field(default=30, ge=1, le=365)
    push_max_active_devices_per_user: int = Field(default=10, ge=1, le=50)
    access_token_minutes: int = Field(default=15, ge=5, le=60)
    refresh_token_days: int = Field(default=30, ge=1, le=180)
    qr_guest_token_minutes: int = Field(default=30, ge=5, le=120)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    web_app_base_url: str = "http://localhost:3000"
    public_api_base_url: str = "http://localhost:8000"
    upload_dir: Path = Path("./var/uploads")
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024)
    auto_create_schema: bool = True
    seed_demo_data: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.lstrip().startswith("["):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("google_oauth_client_ids")
    @classmethod
    def normalize_google_oauth_client_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(client_id.strip() for client_id in value if client_id.strip()))

    @field_validator("apns_team_id", "apns_key_id", "apns_bundle_id")
    @classmethod
    def normalize_apns_identifiers(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("apns_private_key_path", mode="before")
    @classmethod
    def normalize_apns_private_key_path(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("web_app_base_url", "public_api_base_url")
    @classmethod
    def trim_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_deployment_safety(self) -> Settings:
        apns_fields = {
            "FOFU_APNS_TEAM_ID": self.apns_team_id,
            "FOFU_APNS_KEY_ID": self.apns_key_id,
            "FOFU_APNS_BUNDLE_ID": self.apns_bundle_id,
            "FOFU_APNS_PRIVATE_KEY_PATH": self.apns_private_key_path,
        }
        if self.apns_enabled:
            missing = [name for name, value in apns_fields.items() if not value]
            if missing:
                raise ValueError(
                    "FOFU_APNS_ENABLED requires " + ", ".join(sorted(missing))
                )
            if self.environment == "production" and self.apns_environment != "production":
                raise ValueError("Production requires FOFU_APNS_ENVIRONMENT=production")
            if self.environment in {"staging", "production"}:
                assert self.apns_private_key_path is not None
                if not self.apns_private_key_path.is_absolute():
                    raise ValueError(
                        "Staging/production FOFU_APNS_PRIVATE_KEY_PATH must be absolute"
                    )
        if self.environment in {"staging", "production"}:
            if self.jwt_secret in INSECURE_JWT_SECRETS:
                raise ValueError("FOFU_JWT_SECRET must be changed in staging/production")
            if len(self.jwt_secret.encode()) < 32:
                raise ValueError("FOFU_JWT_SECRET must contain at least 32 bytes")
            if "*" in self.cors_origins:
                raise ValueError("Wildcard CORS is not allowed in staging/production")
            if self.auto_create_schema:
                raise ValueError(
                    "Use Alembic migrations instead of AUTO_CREATE_SCHEMA in staging/production"
                )
            if self.seed_demo_data:
                raise ValueError("FOFU_SEED_DEMO_DATA must be disabled in staging/production")
            if not self.database_url.startswith(("postgresql://", "postgresql+")):
                raise ValueError("Staging/production requires a PostgreSQL DATABASE_URL")
            if not self.web_app_base_url.startswith("https://"):
                raise ValueError("Staging/production WEB_APP_BASE_URL must use HTTPS")
            if not self.public_api_base_url.startswith("https://"):
                raise ValueError("Staging/production PUBLIC_API_BASE_URL must use HTTPS")
            if any(not origin.startswith("https://") for origin in self.cors_origins):
                raise ValueError("Staging/production CORS origins must use HTTPS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
