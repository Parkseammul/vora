"""Direct image-generation provider contracts used by the V3 runtime."""

import base64
import os
from pathlib import Path
from typing import Protocol


class ImageProvider(Protocol):
    """Generates one image using the supplied source image as a reference."""

    def generate(self, *, prompt: str, source_image: Path) -> bytes: ...


class OpenAIImageProvider:
    """OpenAI Images Edit API adapter; deliberately independent of LangChain."""

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for image generation")
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

    def generate(self, *, prompt: str, source_image: Path) -> bytes:
        with source_image.open("rb") as image_file:
            response = self._client.images.edit(
                model=self._model,
                image=image_file,
                prompt=prompt,
                output_format="png",
            )
        if not response.data or not response.data[0].b64_json:
            raise RuntimeError("OpenAI image provider returned no image data")
        return base64.b64decode(response.data[0].b64_json, validate=True)
