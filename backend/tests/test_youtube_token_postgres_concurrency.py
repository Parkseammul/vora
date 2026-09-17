"""Real PostgreSQL coverage for the YouTube refresh row lock.

This test runs only after tests.conftest binds all application DB access to the
explicitly configured, separately named test database.
"""

import os
import re
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import (
    Base,
    SocialAccountConnection,
    SocialConnectionStatus,
    SocialPlatform,
    User,
)
from app.social_providers import RefreshedToken
from app.youtube_tokens import YouTubeTokenService

_TEST_URL_ENV = "VORA_TEST_POSTGRES_URL"
_TEST_DATABASE_PATTERN = re.compile(r"vora_test(?:_[a-z0-9_]+)?$", re.IGNORECASE)
_TIMEOUT_SECONDS = 10


def _test_postgres_url() -> str:
    value = os.environ.get(_TEST_URL_ENV)
    if not value:
        pytest.skip(f"{_TEST_URL_ENV} is not configured; PostgreSQL concurrency is not verified")
    url = make_url(value)
    if url.get_backend_name() != "postgresql" or not _TEST_DATABASE_PATTERN.fullmatch(
        url.database or ""
    ):
        pytest.skip("PostgreSQL concurrency test URL is not a dedicated vora_test database")
    # Importing this string does not open a connection.  conftest must have made
    # every application DB engine use the same previously validated test URL.
    from app.database import DATABASE_URL

    if url != make_url(DATABASE_URL):
        pytest.fail("application database URL was not isolated to the test database")
    return value


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    url = _test_postgres_url()
    schema = f"vora_concurrency_{uuid4().hex}"
    bootstrap_engine = create_engine(url, pool_pre_ping=True)
    try:
        with bootstrap_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(
            url,
            connect_args={"options": f"-csearch_path={schema}"},
            pool_pre_ping=True,
        )
        try:
            with engine.begin() as connection:
                Base.metadata.create_all(connection)
            yield engine
        finally:
            engine.dispose()
    finally:
        with bootstrap_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        bootstrap_engine.dispose()


def test_expired_youtube_token_refreshes_once_across_two_postgres_workers(
    postgres_engine: Engine,
) -> None:
    with Session(postgres_engine) as seed_session, seed_session.begin():
        user = User(email=f"concurrency-{uuid4()}@example.test", name="concurrency")
        seed_session.add(user)
        seed_session.flush()
        connection = SocialAccountConnection(
            user_id=user.id,
            platform=SocialPlatform.YOUTUBE,
            external_account_id=f"channel-{uuid4()}",
            access_token="expired-test-access-token",
            refresh_token="test-refresh-token",
            token_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            status=SocialConnectionStatus.CONNECTED,
        )
        seed_session.add(connection)
        seed_session.flush()
        connection_id = connection.id

    start_workers = threading.Barrier(2)
    refresh_entered = threading.Event()
    allow_refresh = threading.Event()
    second_lock_waiting = threading.Event()
    refresh_calls = 0
    refresh_lock = threading.Lock()
    lock_statement_lock = threading.Lock()
    lock_statement_threads: set[int] = set()

    def observe_lock_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if "FOR UPDATE" in statement and "social_account_connections" in statement:
            with lock_statement_lock:
                lock_statement_threads.add(threading.get_ident())
                if len(lock_statement_threads) == 2:
                    second_lock_waiting.set()

    def refresh_access_token(_refresh_token: str) -> RefreshedToken:
        nonlocal refresh_calls
        with refresh_lock:
            refresh_calls += 1
        refresh_entered.set()
        if not allow_refresh.wait(_TIMEOUT_SECONDS):
            raise AssertionError("test did not release the first refresh request")
        return RefreshedToken("new-test-access-token", 3600, None)

    provider = Mock()
    provider.refresh_access_token.side_effect = refresh_access_token

    def worker() -> str:
        start_workers.wait(_TIMEOUT_SECONDS)
        with Session(postgres_engine) as worker_session:
            return YouTubeTokenService(worker_session, provider).valid_access_token(connection_id)  # type: ignore[arg-type]

    event.listen(postgres_engine, "before_cursor_execute", observe_lock_statement)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            try:
                first = executor.submit(worker)
                second = executor.submit(worker)
                assert refresh_entered.wait(_TIMEOUT_SECONDS)
                # The second SELECT was sent while the first transaction holds FOR UPDATE.
                assert second_lock_waiting.wait(_TIMEOUT_SECONDS)
                allow_refresh.set()
                assert {first.result(_TIMEOUT_SECONDS), second.result(_TIMEOUT_SECONDS)} == {
                    "new-test-access-token"
                }
            finally:
                # Do not make an assertion failure wait for the refresh timeout.
                allow_refresh.set()
    finally:
        event.remove(postgres_engine, "before_cursor_execute", observe_lock_statement)

    assert refresh_calls == 1
    assert provider.refresh_access_token.call_count == 1
    with Session(postgres_engine) as verify_session, verify_session.begin():
        stored = verify_session.scalar(
            select(SocialAccountConnection)
            .where(SocialAccountConnection.id == connection_id)
            .with_for_update(nowait=True)
        )
        assert stored is not None
        assert stored.access_token == "new-test-access-token"
        assert stored.token_expires_at is not None
        assert stored.token_expires_at > datetime.now(UTC)
