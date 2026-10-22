"""Offline-only readiness checks for the Instagram Carousel publication path."""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.models import AssetType, SocialPlatform
from app.publication import PublicationService


class SettingsLike(Protocol):
    db_host: str | None
    db_port: str | int | None
    db_name: str | None
    db_user: str | None
    db_password: str | None
    instagram_client_id: str | None
    instagram_client_secret: str | None
    instagram_redirect_uri: str | None
    instagram_graph_api_version: str
    storage_provider: str
    s3_bucket: str | None
    s3_region: str | None
    publication_queue: str


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class OfflineSettings:
    """Safe, partial settings view for a check that must work without a DB."""

    db_host: str | None
    db_port: str | None
    db_name: str | None
    db_user: str | None
    db_password: str | None
    instagram_client_id: str | None
    instagram_client_secret: str | None
    instagram_redirect_uri: str | None
    instagram_graph_api_version: str
    storage_provider: str
    s3_bucket: str | None
    s3_region: str | None
    publication_queue: str

    @classmethod
    def from_current_directory(cls) -> OfflineSettings:
        values = _dotenv_values(Path.cwd() / ".env")
        return cls(
            db_host=values.get("DB_HOST"),
            db_port=values.get("DB_PORT"),
            db_name=values.get("DB_NAME"),
            db_user=values.get("DB_USER"),
            db_password=values.get("DB_PASSWORD"),
            instagram_client_id=values.get("INSTAGRAM_CLIENT_ID"),
            instagram_client_secret=values.get("INSTAGRAM_CLIENT_SECRET"),
            instagram_redirect_uri=values.get("INSTAGRAM_REDIRECT_URI"),
            instagram_graph_api_version=values.get("INSTAGRAM_GRAPH_API_VERSION", "v24.0"),
            storage_provider=values.get("STORAGE_PROVIDER", "local"),
            s3_bucket=values.get("S3_BUCKET"),
            s3_region=values.get("S3_REGION"),
            publication_queue=values.get("PUBLICATION_QUEUE", "publication"),
        )


def validate_carousel_assets(assets: list[object]) -> PreflightCheck:
    """Validate metadata only; never materialize, transform, or upload an object."""
    ordered = sorted(assets, key=lambda asset: getattr(asset, "slide_no", 0) or 0)
    if len(ordered) != 5:
        return PreflightCheck("Carousel Asset Metadata", "FAIL", "Exactly five image assets are required.")
    if [getattr(asset, "slide_no", None) for asset in ordered] != [1, 2, 3, 4, 5]:
        return PreflightCheck("Carousel Asset Metadata", "FAIL", "slide_no must be exactly 1 through 5.")
    if any(
        getattr(asset, "asset_type", None) is not AssetType.IMAGE
        or not str(getattr(asset, "mime_type", "")).startswith("image/")
        for asset in ordered
    ):
        return PreflightCheck("Carousel Asset Metadata", "FAIL", "All five assets must be image FileAssets.")
    return PreflightCheck("Carousel Asset Metadata", "PASS", "Five ordered image FileAssets are suitable for preparation.")


def run_preflight(
    settings: SettingsLike,
    *,
    connection: object | None = None,
    now: datetime | None = None,
    pillow_available: bool | None = None,
) -> list[PreflightCheck]:
    """Run local/static checks only. This function never accesses Meta, AWS, or Redis."""
    clock = now or datetime.now(UTC)
    checks = [_database_configuration_check(settings), _instagram_login_check(settings)]
    checks.append(_account_platform_check(connection))
    checks.append(
        PreflightCheck(
            "OAuth Permissions",
            "NOT_VERIFIED",
            "Configured scopes are static; account consent and publish permission require approved read-only verification.",
        )
    )
    checks.append(_token_expiration_check(connection, clock))
    checks.append(_graph_api_check(settings))
    available = importlib.util.find_spec("PIL") is not None if pillow_available is None else pillow_available
    checks.append(
        PreflightCheck(
            "JPEG Conversion",
            "PASS" if available else "FAIL",
            "Pillow is available for local PNG-to-JPEG conversion."
            if available
            else "Pillow is not installed; install the declared dependency before publishing.",
        )
    )
    checks.extend(_storage_checks(settings))
    checks.append(_wiring_check(settings))
    checks.append(_instagram_only_check())
    return checks


def ready_for_real_smoke_test(checks: list[PreflightCheck]) -> bool:
    """Real smoke testing requires no failure and no unverified external prerequisites."""
    return all(check.status == "PASS" for check in checks)


def _instagram_login_check(settings: SettingsLike) -> PreflightCheck:
    missing = [
        name
        for name, value in (
            ("INSTAGRAM_CLIENT_ID", settings.instagram_client_id),
            ("INSTAGRAM_CLIENT_SECRET", settings.instagram_client_secret),
            ("INSTAGRAM_REDIRECT_URI", settings.instagram_redirect_uri),
        )
        if not value
    ]
    if missing:
        return PreflightCheck("Instagram Login Configuration", "FAIL", f"Missing: {', '.join(missing)}.")
    redirect_uri = settings.instagram_redirect_uri
    if redirect_uri is None or not redirect_uri.startswith(("https://", "http://localhost")):
        return PreflightCheck("Instagram Login Configuration", "FAIL", "Redirect URI must use HTTPS (localhost is allowed for development).")
    return PreflightCheck("Instagram Login Configuration", "PASS", "Instagram Login client and redirect URI are configured.")


def _database_configuration_check(settings: SettingsLike) -> PreflightCheck:
    missing = [
        name
        for name, value in (
            ("DB_HOST", getattr(settings, "db_host", None)),
            ("DB_PORT", getattr(settings, "db_port", None)),
            ("DB_NAME", getattr(settings, "db_name", None)),
            ("DB_USER", getattr(settings, "db_user", None)),
            ("DB_PASSWORD", getattr(settings, "db_password", None)),
        )
        if not value
    ]
    if missing:
        return PreflightCheck("Database Configuration", "FAIL", f"Missing: {', '.join(missing)}.")
    return PreflightCheck("Database Configuration", "PASS", "Database settings are present; no connection was attempted.")


def _account_platform_check(connection: object | None) -> PreflightCheck:
    if connection is None:
        return PreflightCheck("Account Platform", "NOT_VERIFIED", "No connected account was supplied for local inspection.")
    if getattr(connection, "platform", None) is not SocialPlatform.INSTAGRAM:
        return PreflightCheck("Account Platform", "FAIL", "Publication connection is not an Instagram account.")
    if not getattr(connection, "external_account_id", None):
        return PreflightCheck("Account Platform", "FAIL", "Instagram User ID is missing.")
    return PreflightCheck("Account Platform", "PASS", "Connected account is an Instagram User ID.")


def _token_expiration_check(connection: object | None, clock: datetime) -> PreflightCheck:
    if connection is None:
        return PreflightCheck("Token Expiration", "NOT_VERIFIED", "No stored token metadata was supplied.")
    if not getattr(connection, "access_token", None):
        return PreflightCheck("Token Expiration", "FAIL", "Stored Instagram access token is missing.")
    expires_at = getattr(connection, "token_expires_at", None)
    if expires_at is None:
        return PreflightCheck("Token Expiration", "NOT_VERIFIED", "Token expiry is not recorded; no token refresh or network validation was performed.")
    if expires_at <= clock:
        return PreflightCheck("Token Expiration", "FAIL", "Stored Instagram access token is expired.")
    return PreflightCheck("Token Expiration", "NOT_VERIFIED", "Stored expiry is in the future, but token validity was not verified over the network.")


def _graph_api_check(settings: SettingsLike) -> PreflightCheck:
    if not re.fullmatch(r"v\d+\.\d+", settings.instagram_graph_api_version):
        return PreflightCheck("Graph API Version", "FAIL", "Instagram Graph API version must have the form vNN.N.")
    return PreflightCheck("Graph API Version", "PASS", "Pinned Instagram Login Graph API base URL is graph.instagram.com.")


def _storage_checks(settings: SettingsLike) -> list[PreflightCheck]:
    if settings.storage_provider.lower() != "s3":
        return [
            PreflightCheck("Storage Configuration", "FAIL", "Instagram publishing requires STORAGE_PROVIDER=s3."),
            PreflightCheck("S3 Presigned URL Capability", "NOT_VERIFIED", "S3 is not configured; no URL capability check was attempted."),
        ]
    missing = [name for name, value in (("S3_BUCKET", settings.s3_bucket), ("S3_REGION", settings.s3_region)) if not value]
    if missing:
        return [
            PreflightCheck("Storage Configuration", "FAIL", f"Missing: {', '.join(missing)}."),
            PreflightCheck("S3 Presigned URL Capability", "NOT_VERIFIED", "No AWS client or presigned URL was created."),
        ]
    return [
        PreflightCheck("Storage Configuration", "PASS", "Private S3 configuration is present."),
        PreflightCheck("S3 Presigned URL Capability", "NOT_VERIFIED", "AWS object and presigned URL access were not tested."),
    ]


def _wiring_check(settings: SettingsLike) -> PreflightCheck:
    app_dir = Path(__file__).resolve().parent
    celery_source = (app_dir / "celery_app.py").read_text(encoding="utf-8")
    api_source = (app_dir / "main.py").read_text(encoding="utf-8")
    has_route = (
        '"app.publication_tasks.execute_publication": {"queue": settings.publication_queue}'
        in celery_source
        and settings.publication_queue
    )
    route_source = inspect.getsource(PublicationService.request_publish)
    if (
        has_route
        and "self._dispatcher.enqueue(publication.id)" in route_source
        and '@app.post("/workflow-executions/{workflow_execution_id}/publications")' in api_source
    ):
        return PreflightCheck("Publication API/Worker Wiring", "PASS", "Publication intent commits before Celery enqueue and routes to the publication queue.")
    return PreflightCheck("Publication API/Worker Wiring", "FAIL", "Publication enqueue or Celery routing is not configured.")


def _instagram_only_check() -> PreflightCheck:
    source = inspect.getsource(PublicationService._claim)
    if "connection.platform is not SocialPlatform.INSTAGRAM" in source:
        return PreflightCheck("Instagram-only Safety", "PASS", "V3 worker rejects non-Instagram connections before provider execution.")
    return PreflightCheck("Instagram-only Safety", "FAIL", "Worker does not enforce Instagram-only publication.")


def _dotenv_values(path: Path) -> dict[str, str]:
    """Match Settings' CWD `.env` precedence without importing DB-required Settings."""
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            key = key.strip()
            if separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                values[key] = value.strip().strip('"').strip("'")
    for key in tuple(values) + (
        "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
        "INSTAGRAM_CLIENT_ID", "INSTAGRAM_CLIENT_SECRET", "INSTAGRAM_REDIRECT_URI",
        "INSTAGRAM_GRAPH_API_VERSION", "STORAGE_PROVIDER", "S3_BUCKET", "S3_REGION",
        "PUBLICATION_QUEUE",
    ):
        if env_value := os.environ.get(key):
            values[key] = env_value
    return values
