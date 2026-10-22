from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.instagram_carousel import CarouselImagePreparer
from app.instagram_publication_preflight import (
    OfflineSettings,
    ready_for_real_smoke_test,
    run_preflight,
    validate_carousel_assets,
)
from app.models import AssetType, SocialPlatform


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "db_host": "localhost",
        "db_port": "5432",
        "db_name": "vora",
        "db_user": "vora",
        "db_password": "secret",
        "instagram_client_id": "client",
        "instagram_client_secret": "secret",
        "instagram_redirect_uri": "https://app.example/oauth/instagram",
        "instagram_graph_api_version": "v24.0",
        "storage_provider": "s3",
        "s3_bucket": "private-bucket",
        "s3_region": "ap-northeast-2",
        "publication_queue": "publication",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _connection(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "platform": SocialPlatform.INSTAGRAM,
        "external_account_id": "instagram-user",
        "access_token": "secret",
        "token_expires_at": datetime.now(UTC) + timedelta(days=1),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _status(checks: list[object], name: str) -> str:
    return next(check.status for check in checks if check.name == name)  # type: ignore[union-attr]


def test_normal_mock_preflight_is_locally_ready_but_external_state_is_not_verified() -> None:
    checks = run_preflight(_settings(), connection=_connection(), pillow_available=True)

    assert _status(checks, "Instagram Login Configuration") == "PASS"
    assert _status(checks, "Account Platform") == "PASS"
    assert _status(checks, "OAuth Permissions") == "NOT_VERIFIED"
    assert _status(checks, "Token Expiration") == "NOT_VERIFIED"
    assert _status(checks, "JPEG Conversion") == "PASS"
    assert _status(checks, "Publication API/Worker Wiring") == "PASS"
    assert _status(checks, "Instagram-only Safety") == "PASS"
    assert ready_for_real_smoke_test(checks) is False


def test_missing_required_instagram_setting_fails() -> None:
    checks = run_preflight(_settings(instagram_client_secret=None), pillow_available=True)
    assert _status(checks, "Instagram Login Configuration") == "FAIL"


def test_wrong_platform_and_expired_token_fail() -> None:
    checks = run_preflight(
        _settings(),
        connection=_connection(platform=SocialPlatform.YOUTUBE, token_expires_at=datetime.now(UTC)),
        pillow_available=True,
    )
    assert _status(checks, "Account Platform") == "FAIL"
    assert _status(checks, "Token Expiration") == "FAIL"


def test_storage_misconfiguration_and_missing_pillow_fail() -> None:
    checks = run_preflight(_settings(storage_provider="local"), pillow_available=False)
    assert _status(checks, "Storage Configuration") == "FAIL"
    assert _status(checks, "JPEG Conversion") == "FAIL"


def test_asset_metadata_requires_five_ordered_images() -> None:
    good = [SimpleNamespace(slide_no=index, asset_type=AssetType.IMAGE, mime_type="image/png") for index in range(1, 6)]
    assert validate_carousel_assets(good).status == "PASS"
    assert validate_carousel_assets(good[:4]).status == "FAIL"
    good[-1].slide_no = 7
    assert validate_carousel_assets(good).status == "FAIL"


def test_preparer_rejects_non_https_url_without_storage_write() -> None:
    storage = Mock()
    storage.exists.return_value = True
    storage.presigned_get_url.return_value = "http://invalid.example/image.jpg"
    assets = [SimpleNamespace(id=index, storage_key=f"source/{index}.png") for index in range(1, 6)]

    with pytest.raises(ValueError, match="HTTPS"):
        CarouselImagePreparer(storage).prepare(1, assets)

    storage.upload_private.assert_not_called()
    storage.materialize_private.assert_not_called()


def test_preflight_never_calls_external_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    http_call = Mock(side_effect=AssertionError("network call"))
    monkeypatch.setattr("httpx.get", http_call)
    monkeypatch.setattr("httpx.post", http_call)

    run_preflight(_settings(), connection=_connection(), pillow_available=True)

    http_call.assert_not_called()


def test_offline_settings_matches_cwd_dotenv_and_environment_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    (tmp_path / ".env").write_text("DB_HOST=dotenv-host\nDB_PORT=5432\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_HOST", "environment-host")
    monkeypatch.delenv("DB_PORT", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    settings = OfflineSettings.from_current_directory()

    assert settings.db_host == "environment-host"
    assert settings.db_port == "5432"
    assert settings.db_name is None
