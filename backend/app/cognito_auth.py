"""Offline-testable Cognito Access Token verification and identity mapping."""

import json
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, cast
from urllib.request import Request, urlopen

import jwt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models import User


class CognitoAuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class CognitoIdentity:
    issuer: str
    sub: str
    email: str
    name: str


class CognitoTokenVerifier:
    def __init__(self, configured: Settings = settings) -> None:
        self.settings = configured
        self._jwks: dict[str, dict[str, Any]] | None = None
        self._jwks_loaded_at = 0.0
        self._lock = Lock()

    def clear_cache(self) -> None:
        with self._lock:
            self._jwks = None
            self._jwks_loaded_at = 0.0

    def _configuration(self) -> tuple[str, str, str]:
        issuer, client_id, jwks_url = (
            self.settings.cognito_issuer,
            self.settings.cognito_client_id,
            self.settings.cognito_jwks_url,
        )
        if not issuer or not client_id or not jwks_url:
            raise CognitoAuthenticationError("Cognito authentication is not configured")
        return issuer.rstrip("/"), client_id, jwks_url

    def _load_jwks(self, url: str, *, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            if (
                not force_refresh
                and self._jwks is not None
                and now - self._jwks_loaded_at < self.settings.cognito_jwks_ttl_seconds
            ):
                return self._jwks
            try:
                request = Request(url, headers={"Accept": "application/json"})
                with urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                keys = payload.get("keys")
                if not isinstance(keys, list):
                    raise TypeError("JWKS keys are missing")
                loaded = {item["kid"]: item for item in keys if isinstance(item, dict) and isinstance(item.get("kid"), str)}
                if not loaded:
                    raise ValueError("JWKS contains no usable keys")
            except Exception as exc:
                raise CognitoAuthenticationError("Cognito signing keys are unavailable") from exc
            self._jwks, self._jwks_loaded_at = loaded, now
            return loaded

    def verify(self, token: str) -> CognitoIdentity:
        issuer, client_id, jwks_url = self._configuration()
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError("unsupported token header")
            key_data = self._load_jwks(jwks_url).get(header["kid"])
            if key_data is None:
                # A new Cognito signing key may appear before the TTL expires.
                key_data = self._load_jwks(jwks_url, force_refresh=True).get(header["kid"])
            if key_data is None:
                raise ValueError("unknown signing key")
            key = cast(Any, jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_data)))
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=issuer,
                options={"require": ["exp", "iss", "sub", "token_use", "client_id"]},
            )
            if claims.get("token_use") != "access" or claims.get("client_id") != client_id:
                raise ValueError("token is not an allowed Cognito access token")
            sub = claims.get("sub")
            if not isinstance(sub, str) or not sub.strip():
                raise ValueError("token subject is invalid")
            email, name = self._userinfo(token, issuer)
            return CognitoIdentity(issuer, sub, email, name)
        except CognitoAuthenticationError:
            raise
        except Exception as exc:
            raise CognitoAuthenticationError("Invalid Cognito access token") from exc

    def _userinfo(self, token: str, issuer: str) -> tuple[str, str]:
        url = self.settings.cognito_userinfo_url or f"{issuer}/oauth2/userInfo"
        try:
            request = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            email = payload.get("email")
            if not isinstance(email, str) or "@" not in email or email.strip() != email or len(email) > 255:
                raise ValueError("verified email is missing")
            if payload.get("email_verified") is not True:
                raise ValueError("email is not verified")
            name = payload.get("name") or payload.get("preferred_username") or email.split("@", 1)[0]
            return email, str(name)[:100]
        except Exception as exc:
            raise CognitoAuthenticationError("Cognito user information is unavailable") from exc


verifier = CognitoTokenVerifier()


def get_or_create_user(session: Session, identity: CognitoIdentity) -> User:
    existing = session.scalar(select(User).where(User.cognito_issuer == identity.issuer, User.cognito_sub == identity.sub))
    if existing is not None:
        return existing
    # Email is not an account-linking key; legacy users must not be adopted.
    if session.scalar(select(User).where(User.email == identity.email)) is not None:
        raise CognitoAuthenticationError("A legacy account cannot be linked by email")
    user = User(email=identity.email, name=identity.name, cognito_issuer=identity.issuer, cognito_sub=identity.sub)
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        existing = session.scalar(select(User).where(User.cognito_issuer == identity.issuer, User.cognito_sub == identity.sub))
        if existing is not None:
            return existing
        raise CognitoAuthenticationError("Unable to create internal user") from exc
    return user
