import json

import pytest
from conftest import API_V1
from fastapi.testclient import TestClient

from app import rate_limit


def test_validation_errors_redact_auth_secrets_and_qr_locator(client: TestClient) -> None:
    password = "secret7"
    invalid_registration = client.post(
        f"{API_V1}/auth/register",
        json={
            "email": "validation@example.com",
            "password": password,
            "display_name": "Validation Test",
            "client_type": "ios",
        },
    )
    assert invalid_registration.status_code == 422
    registration_body = invalid_registration.json()
    assert password not in json.dumps(registration_body)
    assert registration_body["error"]["details"][0]["input"] == "[redacted]"

    google_id_token = "sensitive-google-token" * 500
    replaced_refresh_token = "sensitive-backup-token"
    invalid_google_login = client.post(
        f"{API_V1}/auth/google",
        json={
            "id_token": google_id_token,
            "replaced_refresh_token": replaced_refresh_token,
            "client_type": "ios",
        },
    )
    assert invalid_google_login.status_code == 422
    google_body = invalid_google_login.json()
    assert google_id_token not in json.dumps(google_body)
    assert replaced_refresh_token not in json.dumps(google_body)
    google_errors = {
        error["loc"][-1]: error["input"] for error in google_body["error"]["details"]
    }
    assert google_errors["id_token"] == "[redacted]"
    assert google_errors["replaced_refresh_token"] == "[redacted]"

    qr_locator = "too-short"
    invalid_qr = client.post(
        f"{API_V1}/guest-sessions/qr",
        json={"code": qr_locator, "client_type": "web"},
    )
    assert invalid_qr.status_code == 422
    qr_body = invalid_qr.json()
    assert qr_locator not in json.dumps(qr_body)
    assert qr_body["error"]["details"][0]["input"] == "[redacted]"


@pytest.mark.parametrize(
    "malformed_body",
    [
        json.dumps(
            {
                "id_token": "malformed-google-id-token-secret",
                "replaced_refresh_token": "malformed-refresh-token-secret",
            }
        ),
        [
            {
                "id_token": "malformed-google-id-token-secret",
                "replaced_refresh_token": "malformed-refresh-token-secret",
            }
        ],
        {
            "payload": {
                "id_token": "malformed-google-id-token-secret",
                "replaced_refresh_token": "malformed-refresh-token-secret",
                "duplicate": "malformed-google-id-token-secret",
            }
        },
    ],
)
def test_google_validation_recursively_redacts_secrets_from_malformed_body(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    malformed_body: object,
) -> None:
    monkeypatch.setattr(rate_limit.google_auth_limiter, "check", lambda _key: None)

    response = client.post(f"{API_V1}/auth/google", json=malformed_body)

    assert response.status_code == 422
    serialized = json.dumps(response.json())
    assert "malformed-google-id-token-secret" not in serialized
    assert "malformed-refresh-token-secret" not in serialized
    assert "[redacted]" in serialized
