"""Direct image-generation provider contracts used by the V3 runtime."""

import base64
import os
from pathlib import Path
from typing import Any, Protocol

_SUPPORTED_QUALITIES = frozenset({"low", "medium", "high", "xhigh", "max", "auto"})
_SUPPORTED_SIZES = frozenset({"256x256", "512x512", "1024x1024", "1536x1024", "1024x1536", "auto"})


class ImageProvider(Protocol):
    """Generates one image using the supplied source image as a reference."""

    def generate(self, *, prompt: str, source_image: Path) -> bytes: ...


class OpenAIImageProvider:
    """OpenAI Images Edit API adapter; deliberately independent of LangChain."""

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for image generation")
        self._image_request_settings = self._read_image_request_settings()
        from openai import OpenAI

        max_retries = os.environ.get("OPENAI_MAX_RETRIES")
        if max_retries is None:
            self._client = OpenAI(api_key=api_key)
        else:
            try:
                parsed_max_retries = int(max_retries)
            except ValueError as exc:
                raise ValueError("OPENAI_MAX_RETRIES must be a non-negative integer") from exc
            if parsed_max_retries < 0:
                raise ValueError("OPENAI_MAX_RETRIES must be a non-negative integer")
            self._client = OpenAI(api_key=api_key, max_retries=parsed_max_retries)
        self._model = model

    @property
    def generation_settings(self) -> dict[str, str | int]:
        """Return only request settings that affect generated image output."""

        return {"output_format": "png", **self._image_request_settings}

    @staticmethod
    def _read_image_request_settings() -> dict[str, str | int]:
        settings: dict[str, str | int] = {}
        quality = os.environ.get("OPENAI_IMAGE_QUALITY")
        if quality is not None:
            if quality not in _SUPPORTED_QUALITIES:
                allowed = ", ".join(sorted(_SUPPORTED_QUALITIES))
                raise ValueError(f"OPENAI_IMAGE_QUALITY must be one of: {allowed}")
            settings["quality"] = quality

        size = os.environ.get("OPENAI_IMAGE_SIZE")
        if size is not None:
            if size not in _SUPPORTED_SIZES:
                allowed = ", ".join(sorted(_SUPPORTED_SIZES))
                raise ValueError(f"OPENAI_IMAGE_SIZE must be one of: {allowed}")
            settings["size"] = size

        count = os.environ.get("OPENAI_IMAGE_COUNT")
        if count is not None:
            try:
                parsed_count = int(count)
            except ValueError as exc:
                raise ValueError("OPENAI_IMAGE_COUNT must be exactly 1") from exc
            if parsed_count != 1:
                raise ValueError("OPENAI_IMAGE_COUNT must be exactly 1 for ImageProvider")
            settings["n"] = parsed_count
        return settings

    def generate(self, *, prompt: str, source_image: Path) -> bytes:
        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "image": None,
            "prompt": prompt,
            "output_format": "png",
            **self._image_request_settings,
        }
        with source_image.open("rb") as image_file:
            request_kwargs["image"] = image_file
            response = self._client.images.edit(**request_kwargs)
        if not response.data or not response.data[0].b64_json:
            raise RuntimeError("OpenAI image provider returned no image data")
        return base64.b64decode(response.data[0].b64_json, validate=True)
