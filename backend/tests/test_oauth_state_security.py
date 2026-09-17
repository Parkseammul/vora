"""OAuth state tests use database-backed state records and never contact providers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import main
from app.database import engine
from app.models import (
    OAuthAuthorizationState,
    SocialAccountConnection,
    SocialPlatform,
    User,
)
from app.social_providers import OAuthIdentity, SocialProviderError


@pytest.fixture
def oauth_user() -> int:
    with Session(engine) as session:
        user = User(email=f"oauth-state-{uuid4()}@example.test", name="OAuth state test")
        session.add(user)
        session.commit()
        user_id = user.id
    try:
        yield user_id
    finally:
        with Session(engine) as session:
            session.execute(delete(OAuthAuthorizationState).where(OAuthAuthorizationState.user_id == user_id))
            session.execute(delete(SocialAccountConnection).where(SocialAccountConnection.user_id == user_id))
            session.execute(delete(User).where(User.id == user_id))
            session.commit()


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    provider = SimpleNamespace(
        authorization_url=Mock(side_effect=lambda state: f"https://provider.test/authorize?state={state}"),
        exchange_code=Mock(
            return_value=OAuthIdentity(
                "oauth-test-account", "OAuth Test", "test-access", None, None
            )
        ),
    )
    monkeypatch.setattr(main, "_oauth_provider", lambda platform: provider)
    return provider


@pytest.fixture
def authorize_as_test_user(monkeypatch: pytest.MonkeyPatch, oauth_user: int) -> None:
    monkeypatch.setattr(main, "_dev_user_or_404", lambda session: SimpleNamespace(id=oauth_user))


def _issue(client: TestClient, platform: str = "youtube", return_to: str = "/settings/social") -> str:
    response = client.get(f"/social/{platform}/authorize", params={"return_to": return_to})
    assert response.status_code == 200
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]


def _consume_from_other_process(state: str, results: Any) -> None:
    """Importing the app anew models an API worker with no shared Python memory."""
    from app.main import _consume_oauth_state

    with Session(engine) as session:
        try:
            _consume_oauth_state(session, state, SocialPlatform.YOUTUBE)
        except SocialProviderError:
            results.put(False)
        else:
            results.put(True)


def test_state_is_hashed_and_allows_callback_from_new_client(
    fake_provider: SimpleNamespace, authorize_as_test_user: None
) -> None:
    with TestClient(main.app) as issuing_client:
        state = _issue(issuing_client)
    with Session(engine) as session:
        record = session.scalar(select(OAuthAuthorizationState))
        assert record is not None
        assert record.state_hash == main._oauth_state_hash(state)
        assert state not in record.state_hash
        assert record.consumed_at is None

    # A new client/session represents a different API process reading shared PostgreSQL state.
    with TestClient(main.app) as callback_client:
        response = callback_client.get(
            "/social/youtube/callback",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"].endswith("/settings/social?oauth=success&platform=YOUTUBE")
    fake_provider.exchange_code.assert_called_once_with("test-code")
    with Session(engine) as session:
        record = session.scalar(select(OAuthAuthorizationState))
        assert record is not None and record.consumed_at is not None


def test_state_issued_by_one_process_is_consumed_by_another(
    fake_provider: SimpleNamespace, authorize_as_test_user: None
) -> None:
    with TestClient(main.app) as client:
        state = _issue(client)

    context = get_context("spawn")
    results = context.Queue()
    consumer = context.Process(target=_consume_from_other_process, args=(state, results))
    consumer.start()
    consumer.join(timeout=15)

    assert consumer.exitcode == 0
    assert results.get(timeout=1) is True
    with Session(engine) as session, pytest.raises(SocialProviderError):
        main._consume_oauth_state(session, state, SocialPlatform.YOUTUBE)
    fake_provider.exchange_code.assert_not_called()


@pytest.mark.parametrize("state", [None, "forged-state"])
def test_missing_or_forged_state_never_exchanges_code(
    fake_provider: SimpleNamespace, authorize_as_test_user: None, state: str | None
) -> None:
    with TestClient(main.app) as client:
        response = client.get(
            "/social/youtube/callback",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"].endswith("/settings/social?oauth=failed&platform=youtube")
    fake_provider.exchange_code.assert_not_called()


def test_expired_reused_and_platform_mismatched_state_are_rejected(
    fake_provider: SimpleNamespace, authorize_as_test_user: None, oauth_user: int
) -> None:
    expired_state = "expired-state"
    with Session(engine) as session:
        session.add(
            OAuthAuthorizationState(
                state_hash=main._oauth_state_hash(expired_state),
                user_id=oauth_user,
                platform=SocialPlatform.YOUTUBE,
                return_to="/settings/social",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        session.commit()

    with TestClient(main.app) as client:
        expired = client.get(
            "/social/youtube/callback",
            params={"code": "test-code", "state": expired_state},
            follow_redirects=False,
        )
        state = _issue(client, "youtube")
        mismatch = client.get(
            "/social/instagram/callback",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
        success = client.get(
            "/social/youtube/callback",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
        reused = client.get(
            "/social/youtube/callback",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )

    assert expired.headers["location"].endswith("oauth=failed&platform=youtube")
    assert mismatch.headers["location"].endswith("oauth=failed&platform=instagram")
    assert success.headers["location"].endswith("oauth=success&platform=YOUTUBE")
    assert reused.headers["location"].endswith("oauth=failed&platform=youtube")
    fake_provider.exchange_code.assert_called_once_with("test-code")


def test_concurrent_consumers_allow_exactly_one_success(
    fake_provider: SimpleNamespace, authorize_as_test_user: None
) -> None:
    with TestClient(main.app) as client:
        state = _issue(client)

    def consume() -> str:
        with TestClient(main.app) as client:
            response = client.get(
                "/social/youtube/callback",
                params={"code": "test-code", "state": state},
                follow_redirects=False,
            )
        return response.headers["location"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume(), range(2)))

    assert sum("oauth=success" in location for location in results) == 1
    assert sum("oauth=failed" in location for location in results) == 1
    fake_provider.exchange_code.assert_called_once_with("test-code")


@pytest.mark.parametrize(
    "return_to",
    [
        "https://attacker.test",
        "//attacker.test",
        "/\\attacker",
        "/%2f%2fattacker",
        "/../admin",
        "/\x00attacker",
    ],
)
def test_unsafe_return_paths_and_post_callback_bypass_are_rejected(
    fake_provider: SimpleNamespace, authorize_as_test_user: None, return_to: str
) -> None:
    with TestClient(main.app) as client:
        response = client.get("/social/youtube/authorize", params={"return_to": return_to})
        post = client.post("/social/youtube/callback", json={"code": "test-code"})

    assert response.status_code == 422
    assert post.status_code == 405
    fake_provider.authorization_url.assert_not_called()
    fake_provider.exchange_code.assert_not_called()
