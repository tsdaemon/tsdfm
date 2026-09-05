import os

import pytest

APP_URL = os.environ.get("E2E_APP_URL", "http://localhost:8080")
ICECAST_STATUS = os.environ.get("E2E_ICECAST_STATUS", "http://localhost:8000/status-json.xsl")


@pytest.fixture(scope="session")
def app_url() -> str:
    return APP_URL


@pytest.fixture(scope="session")
def icecast_status_url() -> str:
    return ICECAST_STATUS


@pytest.fixture(scope="session")
def invite_token() -> str:
    token = os.environ.get("INVITE_TOKEN")
    if not token:
        pytest.skip("INVITE_TOKEN not set - e2e tests need the real stack's invite secret")
    return token
