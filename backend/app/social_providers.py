"""OAuth2 and publishing HTTP adapters. Secrets are never included in raised messages."""

import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse
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


@dataclass(frozen=True)
class RefreshedToken:
    access_token: str
    expires_in: int
    refresh_token: str | None


def _error(response: httpx.Response) -> SocialProviderError:
    if response.status_code >= 500 or response.status_code == 429:
        return SocialProviderError("Social provider is temporarily unavailable", retryable=True)
    if response.status_code in (401, 403):
        return SocialProviderError("Social account authorization was rejected", code="authorization")
    return SocialProviderError("Social provider rejected the publishing request", code="bad_request")


def _instagram_error(response: httpx.Response, operation: str) -> SocialProviderError:
    """Classify a Meta response without retaining its body or credentials."""
    if response.status_code == 429 or response.status_code >= 500:
        # Meta may have accepted a POST before this response became unavailable.
        return SocialProviderError(
            f"Instagram {operation} delivery is unknown (HTTP {response.status_code})",
            code="delivery_unknown",
        )
    if response.status_code in (401, 403):
        return SocialProviderError("Instagram account authorization was rejected", code="authorization")
    return SocialProviderError(
        f"Instagram {operation} was rejected (HTTP {response.status_code})",
        code=f"instagram_http_{response.status_code}",
    )


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


def _safe_google_oauth_error(response: httpx.Response) -> str | None:
    """Return only a known OAuth error code; never retain Google's response body."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    # The allowlist intentionally distinguishes invalid credentials from a
    # malformed request or transient provider-side failure without exposing text.
    return error if error in {"invalid_grant", "invalid_client", "invalid_request"} else None


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

    def refresh_access_token(self, refresh_token: str) -> RefreshedToken:
        if not self._client_id or not self._client_secret:
            raise SocialProviderError("YouTube OAuth is not configured", code="configuration")
        try:
            response = httpx.post(
                self._TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise SocialProviderError("YouTube token refresh network failure", code="token_refresh_network") from exc
        if response.is_error:
            if _safe_google_oauth_error(response) == "invalid_grant":
                raise SocialProviderError("YouTube reconnection is required", code="reauth_required")
            raise SocialProviderError("YouTube token refresh failed", code="token_refresh_failed")
        try:
            payload = response.json()
            access_token = payload.get("access_token")
            expires_in = int(payload.get("expires_in"))
        except (json.JSONDecodeError, TypeError, ValueError):
            access_token, expires_in = None, 0
        if not isinstance(access_token, str) or expires_in <= 0:
            raise SocialProviderError("YouTube token refresh response was invalid", code="token_refresh_failed")
        returned_refresh = payload.get("refresh_token") if isinstance(payload, dict) else None
        return RefreshedToken(
            access_token, expires_in, returned_refresh if isinstance(returned_refresh, str) else None
        )

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

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str | None,
        graph_api_version: str = "v24.0",
        poll_interval_seconds: float = 5.0,
        poll_max_attempts: int = 24,
    ) -> None:
        self._client_id, self._client_secret, self._redirect_uri = client_id, client_secret, redirect_uri
        self._graph_api_version = graph_api_version
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_max_attempts = poll_max_attempts

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

    def publish(
        self,
        access_token: str,
        account_id: str,
        video_url: str,
        caption: str,
        metadata: dict[str, object],
        persist_progress: Callable[[], None],
    ) -> PublishedPost:
        """Publish one Reel while durably recording every non-idempotent boundary.

        ``persist_progress`` commits the owning attempt. It is deliberately called
        immediately after Meta returns an identifier and before another API call.
        Neither the access token nor the presigned URL is placed in metadata.
        """
        base = f"https://graph.instagram.com/{self._graph_api_version}"
        headers = {"Authorization": f"Bearer {access_token}"}
        container_id = metadata.get("instagram_container_id")
        if not isinstance(container_id, str):
            try:
                creation = httpx.post(
                    f"{base}/{account_id}/media",
                    data={"media_type": "REELS", "video_url": video_url, "caption": caption},
                    headers=headers,
                    timeout=60.0,
                )
            except httpx.HTTPError as exc:
                metadata["instagram_reconciliation_required"] = True
                persist_progress()
                raise SocialProviderError(
                    f"Instagram container creation delivery is unknown ({type(exc).__name__})",
                    code="delivery_unknown",
                ) from exc
            if creation.is_error:
                if creation.status_code == 429 or creation.status_code >= 500:
                    metadata["instagram_reconciliation_required"] = True
                    persist_progress()
                raise _instagram_error(creation, "container creation")
            try:
                container_id = creation.json().get("id")
            except (json.JSONDecodeError, ValueError):
                container_id = None
            if not isinstance(container_id, str) or not container_id:
                metadata["instagram_reconciliation_required"] = True
                persist_progress()
                raise SocialProviderError(
                    "Instagram container creation delivery is unknown (invalid response)",
                    code="delivery_unknown",
                )
            metadata["instagram_container_id"] = container_id
            metadata["instagram_container_status"] = "CREATED"
            persist_progress()

        media_id = metadata.get("instagram_media_id")
        status = self._wait_for_container(access_token, base, container_id, metadata, persist_progress)
        if status in {"ERROR", "EXPIRED"}:
            raise SocialProviderError(
                f"Instagram container processing ended with {status}",
                code="instagram_container_failed",
            )
        if status == "PUBLISHED":
            # Meta has already published this container. Do not issue another POST.
            if isinstance(media_id, str):
                return self._verify_media(access_token, base, media_id, metadata, persist_progress)
            metadata["instagram_reconciliation_required"] = True
            persist_progress()
            raise SocialProviderError(
                "Instagram container is already published; reconciliation is required",
                code="delivery_unknown",
            )

        if not isinstance(media_id, str) and isinstance(
            metadata.get("instagram_publish_requested_at"), str
        ):
            # A worker can die after Meta accepts this POST but before its response
            # is committed. Never send the same non-idempotent request again.
            metadata["instagram_reconciliation_required"] = True
            persist_progress()
            raise SocialProviderError(
                "Instagram media publish delivery is unknown; reconciliation is required",
                code="delivery_unknown",
            )

        if not isinstance(media_id, str):
            metadata["instagram_publish_requested_at"] = datetime.now(UTC).isoformat()
            persist_progress()
            try:
                publication = httpx.post(
                    f"{base}/{account_id}/media_publish",
                    data={"creation_id": container_id},
                    headers=headers,
                    timeout=60.0,
                )
            except httpx.HTTPError as exc:
                metadata["instagram_reconciliation_required"] = True
                persist_progress()
                raise SocialProviderError(
                    f"Instagram media publish delivery is unknown ({type(exc).__name__})",
                    code="delivery_unknown",
                ) from exc
            if publication.is_error:
                if publication.status_code == 429 or publication.status_code >= 500:
                    metadata["instagram_reconciliation_required"] = True
                    persist_progress()
                raise _instagram_error(publication, "media publish")
            try:
                media_id = publication.json().get("id")
            except (json.JSONDecodeError, ValueError):
                media_id = None
            if not isinstance(media_id, str) or not media_id:
                metadata["instagram_reconciliation_required"] = True
                persist_progress()
                raise SocialProviderError(
                    "Instagram media publish delivery is unknown (invalid response)",
                    code="delivery_unknown",
                )
            metadata["instagram_media_id"] = media_id
            persist_progress()

        return self._verify_media(access_token, base, media_id, metadata, persist_progress)

    def reconcile(
        self,
        access_token: str,
        account_id: str,
        metadata: dict[str, object],
        persist_progress: Callable[[], None],
    ) -> PublishedPost | None:
        """Read existing external state only; this method never creates or publishes."""
        base = f"https://graph.instagram.com/{self._graph_api_version}"
        media_id = metadata.get("instagram_media_id")
        if isinstance(media_id, str):
            try:
                return self._verify_media(access_token, base, media_id, metadata, persist_progress)
            except SocialProviderError:
                return None

        container_id = metadata.get("instagram_container_id")
        if not isinstance(container_id, str):
            return None
        try:
            status = self._wait_for_container(
                access_token, base, container_id, metadata, persist_progress
            )
        except SocialProviderError:
            return None
        # A container alone cannot be mapped safely to a published Media ID.
        # In every outcome, a caller must keep this delivery_unknown record blocked.
        if status == "PUBLISHED":
            metadata["instagram_reconciliation_required"] = True
            persist_progress()
        return None

    def _wait_for_container(
        self,
        access_token: str,
        base: str,
        container_id: str,
        metadata: dict[str, object],
        persist_progress: Callable[[], None],
    ) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        for poll_no in range(self._poll_max_attempts):
            try:
                response = httpx.get(
                    f"{base}/{container_id}",
                    params={"fields": "status_code,status"},
                    headers=headers,
                    timeout=30.0,
                )
            except httpx.HTTPError:
                # Retrying this GET is safe; it never creates or publishes media.
                if poll_no + 1 < self._poll_max_attempts:
                    time.sleep(self._poll_interval_seconds)
                    continue
                raise SocialProviderError(
                    "Instagram container status could not be read", code="instagram_container_status_unavailable"
                ) from None
            if response.is_error:
                if response.status_code == 429 or response.status_code >= 500:
                    if poll_no + 1 < self._poll_max_attempts:
                        time.sleep(self._poll_interval_seconds)
                        continue
                    raise SocialProviderError(
                        "Instagram container status could not be read",
                        code="instagram_container_status_unavailable",
                    )
                raise _instagram_error(response, "container status lookup")
            try:
                status = response.json().get("status_code")
            except (json.JSONDecodeError, ValueError):
                status = None
            if not isinstance(status, str):
                raise SocialProviderError("Instagram container status response was invalid", code="bad_response")
            metadata["instagram_container_status"] = status
            persist_progress()
            if status == "FINISHED":
                return status
            if status in {"ERROR", "EXPIRED", "PUBLISHED"}:
                return status
            if status != "IN_PROGRESS":
                raise SocialProviderError(
                    "Instagram container returned an unsupported status", code="bad_response"
                )
            if poll_no + 1 < self._poll_max_attempts:
                time.sleep(self._poll_interval_seconds)
        raise SocialProviderError("Instagram container processing timed out", code="instagram_container_timeout")

    def _verify_media(
        self,
        access_token: str,
        base: str,
        media_id: str,
        metadata: dict[str, object],
        persist_progress: Callable[[], None],
    ) -> PublishedPost:
        try:
            response = httpx.get(
                f"{base}/{media_id}",
                params={"fields": "id,permalink"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            metadata["instagram_reconciliation_required"] = True
            persist_progress()
            raise SocialProviderError(
                "Instagram published media could not be verified", code="instagram_media_verification_pending"
            ) from exc
        if response.is_error:
            metadata["instagram_reconciliation_required"] = True
            persist_progress()
            raise SocialProviderError(
                "Instagram published media could not be verified",
                code="instagram_media_verification_pending",
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            metadata["instagram_reconciliation_required"] = True
            persist_progress()
            raise SocialProviderError(
                "Instagram published media verification response was invalid",
                code="instagram_media_verification_pending",
            ) from exc
        permalink = payload.get("permalink") if isinstance(payload, dict) else None
        parsed_permalink = urlparse(permalink) if isinstance(permalink, str) else None
        container_id = metadata.get("instagram_container_id")
        if (
            not isinstance(payload, dict)
            or payload.get("id") != media_id
            or not isinstance(container_id, str)
            or parsed_permalink is None
            or parsed_permalink.scheme != "https"
            or not parsed_permalink.hostname
            or (
                parsed_permalink.hostname != "instagram.com"
                and not parsed_permalink.hostname.endswith(".instagram.com")
            )
        ):
            metadata["instagram_reconciliation_required"] = True
            persist_progress()
            raise SocialProviderError(
                "Instagram published media verification was incomplete",
                code="instagram_media_verification_pending",
            )
        metadata["instagram_reconciliation_required"] = False
        persist_progress()
        return PublishedPost(media_id, permalink)


def platform_from_string(value: str) -> SocialPlatform:
    try:
        return SocialPlatform(value.upper())
    except ValueError as exc:
        raise SocialProviderError("Unsupported social platform", code="bad_request") from exc
