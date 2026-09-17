"""Pytest bootstrap that prevents application imports from using a non-test database."""

import os
import re
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import make_url

_TEST_URL_ENV = "VORA_TEST_POSTGRES_URL"
_TEST_DATABASE_PATTERN = re.compile(r"vora_test(?:_[a-z0-9_]+)?$", re.IGNORECASE)


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


@pytest.fixture(scope="session", autouse=True)
def test_database_schema() -> Iterator[None]:
    """Create only missing tables in the already validated dedicated test database."""
    from app.database import engine
    from app.models import Base

    Base.metadata.create_all(engine)
    yield
