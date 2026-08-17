from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="fofu-backend-pytest-", dir="/private/tmp"))
_DATABASE_PATH = _TEST_ROOT / "integration.db"
_UPLOAD_PATH = _TEST_ROOT / "uploads"
_TEST_ENV = {
    "FOFU_ENVIRONMENT": "test",
    "FOFU_DATABASE_URL": f"sqlite:///{_DATABASE_PATH}",
    "FOFU_JWT_SECRET": "integration-test-secret-with-more-than-thirty-two-bytes",
    "FOFU_AUTO_CREATE_SCHEMA": "true",
    "FOFU_SEED_DEMO_DATA": "true",
    "FOFU_WEB_APP_BASE_URL": "http://web.test",
    "FOFU_PUBLIC_API_BASE_URL": "http://api.test",
    "FOFU_UPLOAD_DIR": str(_UPLOAD_PATH),
}

# The application constructs its engine and cached Settings at import time. Apply the
# isolated runtime only for those imports, then restore the process environment so the
# Settings unit tests continue to exercise real defaults.
_PREVIOUS_ENV = {key: os.environ.get(key) for key in _TEST_ENV}
os.environ.update(_TEST_ENV)

from app.database import engine  # noqa: E402
from app.main import app  # noqa: E402

for _key, _value in _PREVIOUS_ENV.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


API_V1 = "/api/v1"
DEMO_PASSWORD = "fofu-demo-password"
DEMO_QR_CODE = "halmoni-table-demo"


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Run requests through the real ASGI lifespan against the isolated test DB."""

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def upload_root() -> Path:
    return _UPLOAD_PATH


def pytest_sessionfinish() -> None:
    engine.dispose()
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
