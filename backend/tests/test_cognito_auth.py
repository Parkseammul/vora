import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Self, cast
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import main
from app.cognito_auth import (
    CognitoAuthenticationError,
    CognitoIdentity,
    get_or_create_user,
    verifier,
)
from app.database import engine
from app.models import User, WorkflowExecution


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


@pytest.fixture
def cognito_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, object]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    issuer = "https://cognito-idp.ap-northeast-2.amazonaws.com/test_pool"
    client_id = "test-client"
    jwks_url = "https://fixture.test/.well-known/jwks.json"
    userinfo_url = "https://fixture.test/oauth2/userInfo"
    userinfo: dict[str, object] = {
        "sub": "cognito-sub",
        "email": "new@example.test",
        "email_verified": True,
        "name": "New User",
    }

    def fake_urlopen(request: object, timeout: int = 5) -> _Response:
        del timeout
        url = getattr(request, "full_url", "")
        if url == jwks_url:
            return _Response({"keys": [{**public_jwk, "kid": "test-key", "alg": "RS256", "use": "sig"}]})
        return _Response(userinfo)

    monkeypatch.setattr("app.cognito_auth.urlopen", fake_urlopen)
    monkeypatch.setattr(main.settings, "cognito_issuer", issuer)
    monkeypatch.setattr(main.settings, "cognito_client_id", client_id)
    monkeypatch.setattr(main.settings, "cognito_jwks_url", jwks_url)
    monkeypatch.setattr(main.settings, "cognito_userinfo_url", userinfo_url)
    verifier.clear_cache()

    def token(**overrides: object) -> str:
        kid = str(overrides.pop("kid", "test-key"))
        claims: dict[str, object] = {
            "iss": issuer,
            "sub": "cognito-sub",
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "token_use": "access",
            "client_id": client_id,
        }
        claims.update(overrides)
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid})

    yield {"token": token, "private": private, "issuer": issuer, "userinfo": userinfo}
    verifier.clear_cache()


def test_cognito_access_token_is_verified_and_identity_is_mapped(cognito_fixture: dict[str, object]) -> None:
    token = cognito_fixture["token"]
    assert callable(token)
    identity = verifier.verify(token())
    assert identity == CognitoIdentity(
        "https://cognito-idp.ap-northeast-2.amazonaws.com/test_pool",
        "cognito-sub",
        "new@example.test",
        "New User",
    )
    with Session(engine) as session:
        user = get_or_create_user(session, identity)
        session.commit()
        same = get_or_create_user(session, identity)
        assert same.id == user.id
        session.rollback()
        session.delete(user)
        session.commit()


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_use": "id"},
        {"client_id": "wrong-client"},
        {"iss": "https://wrong.example"},
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
    ],
)
def test_invalid_access_tokens_are_rejected(
    cognito_fixture: dict[str, object], overrides: dict[str, object]
) -> None:
    token = cognito_fixture["token"]
    with pytest.raises(CognitoAuthenticationError):
        verifier.verify(token(**overrides))


def test_forged_signature_and_unknown_kid_are_rejected(cognito_fixture: dict[str, object]) -> None:
    token = cognito_fixture["token"]
    with pytest.raises(CognitoAuthenticationError):
        verifier.verify(token(kid="unknown"))
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {"iss": cognito_fixture["issuer"], "sub": "cognito-sub", "exp": datetime.now(UTC) + timedelta(minutes=5), "token_use": "access", "client_id": "test-client"},
        other,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    with pytest.raises(CognitoAuthenticationError):
        verifier.verify(forged)


def test_protected_api_requires_bearer_token() -> None:
    main.app.dependency_overrides.pop(main.get_current_user, None)
    try:
        with TestClient(main.app) as client:
            assert client.get("/workflow-executions/1").status_code == 401
    finally:
        main.app.dependency_overrides.clear()


def test_authenticated_user_cannot_read_another_users_workflow(
    cognito_fixture: dict[str, object],
) -> None:
    main.app.dependency_overrides.clear()
    identity = verifier.verify(cognito_fixture["token"]())
    with Session(engine) as session:
        owner = get_or_create_user(session, identity)
        other = User(email=f"other-{uuid4()}@example.test", name="Other")
        session.add(other)
        session.flush()
        execution = WorkflowExecution(
            user_id=other.id, workflow_key="vora_content_creation", workflow_version=3
        )
        session.add(execution)
        session.commit()
        execution_id = execution.id
        with TestClient(main.app) as client:
            response = client.get(
                f"/workflow-executions/{execution_id}",
                headers={"Authorization": f"Bearer {cognito_fixture['token']()}"},
            )
            publications = client.get(
                f"/workflow-executions/{execution_id}/publications",
                headers={"Authorization": f"Bearer {cognito_fixture['token']()}"},
            )
            asset = client.get(
                f"/workflow-executions/{execution_id}/slides/1/image",
                headers={"Authorization": f"Bearer {cognito_fixture['token']()}"},
            )
        assert response.status_code == 404
        assert publications.status_code == 404
        assert asset.status_code == 404
        session.delete(execution)
        session.flush()
        session.delete(other)
        session.delete(owner)
        session.commit()


def test_same_email_does_not_auto_link_cognito_identity() -> None:
    identity = CognitoIdentity("https://issuer.test", "sub-" + str(uuid4()), f"legacy-{uuid4()}@example.test", "Dev")
    with Session(engine) as session:
        legacy = User(email=identity.email, name="Legacy")
        session.add(legacy)
        session.commit()
        mapped = get_or_create_user(session, identity)
        session.commit()
        assert mapped.id != legacy.id
        assert mapped.email == legacy.email
        session.delete(mapped)
        session.delete(legacy)
        session.commit()


def test_cognito_identity_without_email_is_allowed() -> None:
    identity = CognitoIdentity("https://issuer.test", "sub-" + str(uuid4()), None, "VORA User")
    with Session(engine) as session:
        user = get_or_create_user(session, identity)
        session.commit()
        assert user.email is None
        same = get_or_create_user(session, identity)
        assert same.id == user.id
        session.rollback()
        session.delete(user)
        session.commit()


def test_unverified_email_is_not_used(cognito_fixture: dict[str, object]) -> None:
    userinfo = cognito_fixture["userinfo"]
    assert isinstance(userinfo, dict)
    userinfo["email_verified"] = False
    token = cognito_fixture["token"]
    assert callable(token)
    identity = verifier.verify(token())
    assert identity.email is None


def test_userinfo_subject_mismatch_is_rejected(cognito_fixture: dict[str, object]) -> None:
    userinfo = cognito_fixture["userinfo"]
    assert isinstance(userinfo, dict)
    userinfo["sub"] = "different-sub"
    token = cognito_fixture["token"]
    assert callable(token)
    with pytest.raises(CognitoAuthenticationError):
        verifier.verify(token())


def test_different_cognito_subjects_with_same_email_are_separate_users() -> None:
    issuer = "https://issuer.test"
    email = f"shared-{uuid4()}@example.test"
    with Session(engine) as session:
        first = get_or_create_user(session, CognitoIdentity(issuer, "sub-1-" + str(uuid4()), email, "First"))
        second = get_or_create_user(session, CognitoIdentity(issuer, "sub-2-" + str(uuid4()), email, "Second"))
        session.commit()
        assert first.id != second.id
        session.delete(second)
        session.delete(first)
        session.commit()


def test_cognito_user_creation_race_returns_winning_identity() -> None:
    identity = CognitoIdentity("https://issuer.test", "race-sub", None, "Race User")
    winner = User(
        id=987654321,
        email=None,
        name="Race User",
        cognito_issuer=identity.issuer,
        cognito_sub=identity.sub,
    )

    class RaceSession:
        scalar_calls = 0
        rolled_back = False

        def scalar(self, _statement: object) -> User | None:
            self.scalar_calls += 1
            return None if self.scalar_calls == 1 else winner

        def add(self, _user: User) -> None:
            return None

        def flush(self) -> None:
            raise IntegrityError("INSERT", {}, Exception("identity race"))

        def rollback(self) -> None:
            self.rolled_back = True

    race_session = RaceSession()
    assert get_or_create_user(cast(Session, race_session), identity) is winner
    assert race_session.rolled_back is True


def test_cognito_identity_pair_constraint_and_unique_key() -> None:
    issuer = "https://issuer-" + str(uuid4())
    first = User(
        email=f"mapped-{uuid4()}@example.test",
        name="Mapped",
        cognito_issuer=issuer,
        cognito_sub="sub-1",
    )
    with Session(engine) as session:
        session.add(first)
        session.commit()
        duplicate = User(
            email=f"mapped-{uuid4()}@example.test",
            name="Duplicate",
            cognito_issuer=issuer,
            cognito_sub="sub-1",
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        partial = User(
            email=f"partial-{uuid4()}@example.test",
            name="Partial",
            cognito_issuer=issuer,
        )
        session.add(partial)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.delete(first)
        session.commit()
