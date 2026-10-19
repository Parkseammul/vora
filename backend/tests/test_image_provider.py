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


def test_openai_image_provider_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIImageProvider("", "gpt-image-2.5-sunburst")
