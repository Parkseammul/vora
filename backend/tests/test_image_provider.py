import base64
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.image_provider import OpenAIImageProvider

VALID_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0dIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99\x1d\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_openai_image_provider_edits_source_image_and_decodes_png(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = Mock()
    client.images.edit.return_value = SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(VALID_PNG).decode())]
    )
    openai_class = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai_class))
    monkeypatch.delenv("OPENAI_MAX_RETRIES", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_QUALITY", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_SIZE", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_COUNT", raising=False)
    source = tmp_path / "source.png"
    source.write_bytes(b"reference")

    provider = OpenAIImageProvider("test-key", "gpt-image-2.5-sunburst")
    result = provider.generate(prompt="Make a slide image", source_image=source)

    assert result == VALID_PNG
    assert result[:8] == b"\x89PNG\r\n\x1a\n"
    assert result[12:16] == b"IHDR"
    openai_class.assert_called_once_with(api_key="test-key")
    assert client.images.edit.call_args.kwargs["model"] == "gpt-image-2.5-sunburst"
    assert client.images.edit.call_args.kwargs["prompt"] == "Make a slide image"
    assert client.images.edit.call_args.kwargs["output_format"] == "png"
    assert "quality" not in client.images.edit.call_args.kwargs
    assert "size" not in client.images.edit.call_args.kwargs
    assert "n" not in client.images.edit.call_args.kwargs


def test_openai_image_provider_can_disable_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai_class = Mock()
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai_class))
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "0")

    OpenAIImageProvider("test-key", "gpt-image-2.5-sunburst")

    openai_class.assert_called_once_with(api_key="test-key", max_retries=0)


def test_openai_image_provider_passes_configured_image_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = Mock()
    client.images.edit.return_value = SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(VALID_PNG).decode())]
    )
    openai_class = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai_class))
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "0")
    monkeypatch.setenv("OPENAI_IMAGE_QUALITY", "low")
    monkeypatch.setenv("OPENAI_IMAGE_SIZE", "1024x1024")
    monkeypatch.setenv("OPENAI_IMAGE_COUNT", "1")
    source = tmp_path / "source.png"
    source.write_bytes(b"reference")

    provider = OpenAIImageProvider("test-key", "gpt-image-2.5-sunburst")
    provider.generate(prompt="Make a slide image", source_image=source)

    openai_class.assert_called_once_with(api_key="test-key", max_retries=0)
    assert provider.generation_settings == {
        "output_format": "png",
        "quality": "low",
        "size": "1024x1024",
        "n": 1,
    }
    assert client.images.edit.call_args.kwargs["quality"] == "low"
    assert client.images.edit.call_args.kwargs["size"] == "1024x1024"
    assert client.images.edit.call_args.kwargs["n"] == 1


@pytest.mark.parametrize("value", ["-1", "not-an-integer"])
def test_openai_image_provider_rejects_invalid_sdk_retry_setting(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    openai_class = Mock()
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai_class))
    monkeypatch.setenv("OPENAI_MAX_RETRIES", value)

    with pytest.raises(ValueError, match="OPENAI_MAX_RETRIES"):
        OpenAIImageProvider("test-key", "gpt-image-2.5-sunburst")

    openai_class.assert_not_called()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("OPENAI_IMAGE_QUALITY", "ultra"),
        ("OPENAI_IMAGE_SIZE", "999x999"),
        ("OPENAI_IMAGE_COUNT", "2"),
    ],
)
def test_openai_image_provider_rejects_invalid_image_settings(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    openai_class = Mock()
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai_class))
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match=variable):
        OpenAIImageProvider("test-key", "gpt-image-2.5-sunburst")

    openai_class.assert_not_called()


def test_openai_image_provider_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIImageProvider("", "gpt-image-2.5-sunburst")
