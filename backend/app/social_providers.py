"""OAuth2 and publishing HTTP adapters. Secrets are never included in raised messages."""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import httpx

from app.models import SocialPlatform


class SocialProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, code: str = "provider_error") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


@dataclass(frozen=True)
class OAuthIdentity:
    external_account_id: str
    account_name: str | None
    access_token: str
    refresh_token: str | None
    token_expires_at: datetime | None


@dataclass(frozen=True)
class PublishedPost:
    external_post_id: str
    external_post_url: str | None


def _error(response: httpx.Response) -> SocialProviderError:
    if response.status_code >= 500 or response.status_code == 429:
        return SocialProviderError("Social provider is temporarily unavailable", retryable=True)
    if response.status_code in (401, 403):
        return SocialProviderError("Social account authorization was rejected", code="authorization")
    return SocialProviderError("Social provider rejected the publishing request", code="bad_request")


class _MultipartRelatedStream(httpx.SyncByteStream):
    """Streams a Google media upload without buffering the video in memory."""

    def __init__(self, path: Path, metadata: bytes, media_type: str, boundary: str) -> None:
        self._path = path
        boundary_bytes = boundary.encode("ascii")
        self._metadata_part = (
            b"--" + boundary_bytes
            + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + metadata
            + b"\r\n"
        )
        self._media_part = (
            b"--" + boundary_bytes
            + f"\r\nContent-Type: {media_type}\r\n\r\n".encode("ascii")
        )
        self._closing = b"\r\n--" + boundary_bytes + b"--\r\n"

    @property
    def content_length(self) -> int:
        return (
            len(self._metadata_part)
            + len(self._media_part)
            + self._path.stat().st_size
            + len(self._closing)
        )

    def __iter__(self) -> Iterator[bytes]:
        yield self._metadata_part
        yield self._media_part
        with self._path.open("rb") as video:
            while chunk := video.read(1024 * 1024):
                yield chunk
        yield self._closing


def _safe_youtube_reason(response: httpx.Response) -> str | None:
    """Return Google error.reason only; never persist response text or request data."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    errors = error.get("errors") if isinstance(error, dict) else None
    first = errors[0] if isinstance(errors, list) and errors else None
    reason = first.get("reason") if isinstance(first, dict) else None
    return reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", reason) else None


def _youtube_upload_error(response: httpx.Response) -> SocialProviderError:
    reason = _safe_youtube_reason(response)
    details = f"HTTP {response.status_code}" + (f", reason={reason}" if reason else "")
    if response.status_code >= 500 or response.status_code == 429:
        return SocialProviderError(
            f"YouTube upload result is unknown ({details})",
            code="delivery_unknown",
        )
    code = f"youtube_http_{response.status_code}" + (f"_{reason}" if reason else "")
    return SocialProviderError(f"YouTube rejected upload ({details})", code=code)


class YouTubeProvider:
    _AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    _TOKEN_URL = "https://oauth2.googleapis.com/token"
    _CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"

    def __init__(self, client_id: str | None, client_secret: str | None, redirect_uri: str | None) -> None:
        self._client_id, self._client_secret, self._redirect_uri = client_id, client_secret, redirect_uri

    def authorization_url(self, state: str) -> str:
        if not self._client_id or not self._redirect_uri:
            raise SocialProviderError("YouTube OAuth is not configured", code="configuration")
        # return f"{self._AUTH_URL}?{urlencode({'client_id': self._client_id, 'redirect_uri': self._redirect_uri, 'response_type': 'code', 'scope': 'https://www.googleapis.com/auth/youtube.upload', 'access_type': 'offline', 'prompt': 'consent', 'state': state})}"
        return f"{self._AUTH_URL}?{urlencode({'client_id': self._client_id, 'redirect_uri': self._redirect_uri, 'response_type': 'code', 'scope': 'https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly', 'access_type': 'offline', 'prompt': 'consent', 'state': state})}"

    def exchange_code(self, code: str) -> OAuthIdentity:
        if not self._client_id or not self._client_secret or not self._redirect_uri:
            raise SocialProviderError("YouTube OAuth is not configured", code="configuration")
        try:
            response = httpx.post(self._TOKEN_URL, data={"code": code, "client_id": self._client_id, "client_secret": self._client_secret, "redirect_uri": self._redirect_uri, "grant_type": "authorization_code"}, timeout=15.0)
        except httpx.HTTPError as exc:
            raise SocialProviderError("OAuth token exchange failed", retryable=True) from exc
        if response.is_error:
            raise _error(response)
        payload = response.json()
        access_token = payload.get("access_token")
        if not isinstance(access_token, str):
            raise SocialProviderError("OAuth token response was invalid")
        channel = httpx.get(self._CHANNEL_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15.0)
        if channel.is_error:
            raise _error(channel)
        item = channel.json().get("items", [{}])[0]
        return OAuthIdentity(str(item.get("id")), item.get("snippet", {}).get("title"), access_token, payload.get("refresh_token"), datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600))))

    def publish(self, access_token: str, video_path: str, title: str, description: str) -> PublishedPost:
        metadata = {"snippet": {"title": title, "description": description}, "status": {"privacyStatus": "private"}}
        path = Path(video_path)
        boundary = f"vora-{uuid4().hex}"
        stream = _MultipartRelatedStream(
            path,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "video/mp4",
            boundary,
        )
        try:
            response = httpx.post(
                "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=multipart&part=snippet,status",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": f'multipart/related; boundary="{boundary}"',
                    "Content-Length": str(stream.content_length),
                },
                content=stream,
                timeout=120.0,
            )
        except OSError as exc:
            raise SocialProviderError("YouTube video file could not be read", code="file_io") from exc
        except httpx.HTTPError as exc:
            # A transport failure can happen after YouTube accepted the upload. Retrying
            # without a provider id could create a duplicate video.
            raise SocialProviderError(
                f"YouTube upload result is unknown after transport error ({type(exc).__name__})",
                code="delivery_unknown",
            ) from exc
        if response.is_error:
            raise _youtube_upload_error(response)
        payload = response.json()
        post_id = payload.get("id")
        if not isinstance(post_id, str):
            raise SocialProviderError("YouTube upload response was invalid")
        return PublishedPost(post_id, f"https://www.youtube.com/watch?v={post_id}")


class InstagramProvider:
    _AUTH_URL = "https://www.instagram.com/oauth/authorize"
    _TOKEN_URL = "https://api.instagram.com/oauth/access_token"

    def __init__(self, client_id: str | None, client_secret: str | None, redirect_uri: str | None) -> None:
        self._client_id, self._client_secret, self._redirect_uri = client_id, client_secret, redirect_uri

    def authorization_url(self, state: str) -> str:
        if not self._client_id or not self._redirect_uri:
            raise SocialProviderError("Instagram OAuth is not configured", code="configuration")
        return f"{self._AUTH_URL}?{urlencode({'client_id': self._client_id, 'redirect_uri': self._redirect_uri, 'response_type': 'code', 'scope': 'instagram_business_basic,instagram_business_content_publish', 'state': state})}"

    def exchange_code(self, code: str) -> OAuthIdentity:
        if not self._client_id or not self._client_secret or not self._redirect_uri:
            raise SocialProviderError("Instagram OAuth is not configured", code="configuration")
        try:
            response = httpx.post(self._TOKEN_URL, data={"client_id": self._client_id, "client_secret": self._client_secret, "grant_type": "authorization_code", "redirect_uri": self._redirect_uri, "code": code}, timeout=15.0)
        except httpx.HTTPError as exc:
            raise SocialProviderError("OAuth token exchange failed", retryable=True) from exc
        if response.is_error:
            raise _error(response)
        payload = response.json()
        token, account_id = payload.get("access_token"), payload.get("user_id")
        if not isinstance(token, str) or account_id is None:
            raise SocialProviderError("OAuth token response was invalid")
        return OAuthIdentity(str(account_id), None, token, None, None)

    def publish(self, access_token: str, account_id: str, video_url: str, caption: str) -> PublishedPost:
        base = f"https://graph.instagram.com/{account_id}"
        try:
            creation = httpx.post(f"{base}/media", data={"media_type": "REELS", "video_url": video_url, "caption": caption, "access_token": access_token}, timeout=60.0)
            if creation.is_error:
                raise _error(creation)
            container_id = creation.json().get("id")
            publish = httpx.post(f"{base}/media_publish", data={"creation_id": container_id, "access_token": access_token}, timeout=60.0)
        except httpx.HTTPError as exc:
            raise SocialProviderError("Instagram publishing failed", retryable=True) from exc
        if publish.is_error:
            raise _error(publish)
        post_id = publish.json().get("id")
        if not isinstance(post_id, str):
            raise SocialProviderError("Instagram publishing response was invalid")
        return PublishedPost(post_id, None)


def platform_from_string(value: str) -> SocialPlatform:
    try:
        return SocialPlatform(value.upper())
    except ValueError as exc:
        raise SocialProviderError("Unsupported social platform", code="bad_request") from exc
