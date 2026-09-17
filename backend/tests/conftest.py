"""Pytest bootstrap that prevents application imports from using a non-test database."""

import os
import re
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from sqlalchemy.engine import make_url

_TEST_URL_ENV = "VORA_TEST_POSTGRES_URL"
_TEST_DATABASE_PATTERN = re.compile(r"vora_test(?:_[a-z0-9_]+)?$", re.IGNORECASE)
_CELERY_REDIS_E2E_ENV = "VORA_RUN_CELERY_REDIS_E2E"
_TEST_REDIS_URL_ENV = "VORA_TEST_REDIS_URL"
_TEST_CELERY_QUEUE_ENV = "VORA_TEST_CELERY_QUEUE"
_TEST_CELERY_QUEUE_PATTERN = re.compile(r"vora_test_[a-z0-9_-]+$")


def _configure_test_database() -> None:
    value = os.environ.get(_TEST_URL_ENV)
    if not value:
        pytest.exit(f"{_TEST_URL_ENV} is required before pytest can import application modules")
    url = make_url(value)
    if (
        url.get_backend_name() != "postgresql"
        or not _TEST_DATABASE_PATTERN.fullmatch(url.database or "")
        or not url.host
        or not url.username
    ):
        pytest.exit("pytest requires an explicitly allowed dedicated PostgreSQL test database")

    # app.config reads these process variables before its .env fallback.  Set them
    # before any app import so every app.database engine targets the test database.
    os.environ["DB_HOST"] = url.host
    os.environ["DB_PORT"] = str(url.port or 5432)
    os.environ["DB_NAME"] = url.database or ""
    os.environ["DB_USER"] = url.username
    os.environ["DB_PASSWORD"] = url.password or ""


_configure_test_database()


def _configure_celery_redis_e2e() -> None:
    """Apply isolated broker settings before Celery or the FastAPI app is imported."""
    if os.environ.get(_CELERY_REDIS_E2E_ENV, "").lower() != "true":
        return

    redis_url = os.environ.get(_TEST_REDIS_URL_ENV)
    queue = os.environ.get(_TEST_CELERY_QUEUE_ENV)
    parsed = urlparse(redis_url or "")
    if (
        parsed.scheme not in {"redis", "rediss"}
        or not parsed.hostname
        or parsed.path in {"", "/", "/0"}
        or not queue
        or not _TEST_CELERY_QUEUE_PATTERN.fullmatch(queue)
    ):
        pytest.exit(
            "Celery Redis E2E requires a dedicated VORA_TEST_REDIS_URL and "
            "a VORA_TEST_CELERY_QUEUE beginning with vora_test_"
        )
    if os.environ.get("VORA_E2E_FAKE_PROVIDERS", "").lower() != "true":
        pytest.exit("Celery Redis E2E requires VORA_E2E_FAKE_PROVIDERS=true")

    # Only this pytest process and its child worker receive these values.  The
    # worker consumes the one explicit test queue; it cannot consume default queues.
    os.environ["REDIS_URL"] = redis_url
    os.environ["VIDEO_GENERATION_QUEUE"] = queue
    os.environ["PUBLICATION_QUEUE"] = f"{queue}_publication"


_configure_celery_redis_e2e()


@pytest.fixture(scope="session", autouse=True)
def test_database_schema() -> Iterator[None]:
    """Create only missing tables in the already validated dedicated test database."""
    from app.database import engine
    from app.models import Base

    Base.metadata.create_all(engine)
    yield
