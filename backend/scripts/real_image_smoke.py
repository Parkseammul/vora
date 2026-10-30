"""One-shot real OpenAI image edit smoke test.

This script deliberately bypasses the workflow and database. It makes one
Images Edit request through the existing OpenAIImageProvider and never retries.
"""

import os
import re
import struct
from collections.abc import Mapping
from pathlib import Path

from openai import APIStatusError, BadRequestError

from app.image_provider import OpenAIImageProvider

MODEL = "gpt-image-2.5-sunburst"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPOSITORY_ROOT / "home.png"
OUTPUT = REPOSITORY_ROOT / "backend" / "uploads" / "real-ai-smoke.png"


_SENSITIVE_PATTERNS = (
    (re.compile(r"(?i)bearer\s+[^\s,;]+"), "Bearer [REDACTED]"),
    (
        re.compile(r"(?i)((?:api[_ -]?key|authorization)\s*[:=])\s*[^\s,;]+"),
        r"\1 [REDACTED]",
    ),
    (re.compile(r"(?i)sk-[A-Za-z0-9_-]+"), "sk-[REDACTED]"),
    (re.compile(r"(?i)data:[^\s,]+,[^\s]+"), "data:[REDACTED]"),
)


def _mask_sensitive(value: object) -> str:
    """Return a bounded, credential-safe representation of an SDK field."""

    text = str(value)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:2000]


def _error_detail(exc: APIStatusError, field: str) -> object:
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, Mapping):
        return error.get(field)
    return getattr(exc, field, None)


def _print_api_error(exc: APIStatusError) -> None:
    """Print bounded, credential-safe response diagnostics; never dump the request."""

    response = getattr(exc, "response", None)
    request_id = getattr(exc, "request_id", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    if not request_id and response is not None:
        request_id = headers.get("x-request-id") if hasattr(headers, "get") else None

    request = getattr(response, "request", None) if response is not None else None
    request_url = getattr(request, "url", None)
    request_host = getattr(request_url, "host", None)
    content_type = headers.get("content-type") if hasattr(headers, "get") else None
    content_length = headers.get("content-length") if hasattr(headers, "get") else None
    raw_body = getattr(response, "text", None) if response is not None else None
    body_format = "json" if isinstance(getattr(exc, "body", None), Mapping) else "text"
    if content_type and "json" in str(content_type).lower():
        body_format = "json"
    elif content_type and "html" in str(content_type).lower():
        body_format = "html"
    elif not raw_body:
        body_format = "empty"

    print("OpenAI image request failed")
    print(f"status_code: {_mask_sensitive(getattr(exc, 'status_code', None))}")
    for field in ("type", "code", "param", "message"):
        print(f"error.{field}: {_mask_sensitive(_error_detail(exc, field))}")
    print(f"request_id: {_mask_sensitive(request_id)}")
    print(f"request_host: {_mask_sensitive(request_host)}")
    print(f"response_content_type: {_mask_sensitive(content_type)}")
    print(f"response_content_length: {_mask_sensitive(content_length)}")
    print(f"response_body_format: {body_format}")
    if raw_body:
        print(f"response_body_excerpt: {_mask_sensitive(raw_body)[:500]}")


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as image_file:
        if image_file.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("output is not a PNG file")
        chunk_length = struct.unpack(">I", image_file.read(4))[0]
        if image_file.read(4) != b"IHDR" or chunk_length < 8:
            raise ValueError("PNG does not start with an IHDR chunk")
        width, height = struct.unpack(">II", image_file.read(8))
    return width, height


def main() -> int:
    if not SOURCE.is_file():
        raise SystemExit(f"source image does not exist: {SOURCE}")
    source_width, source_height = png_dimensions(SOURCE)
    print(f"source: {SOURCE}")
    print(f"source_png_bytes: {SOURCE.stat().st_size}")
    print(f"source_dimensions: {source_width}x{source_height}")
    print(f"model: {MODEL}")
    print("request: images.edit, n=1, quality=low, size=1024x1024, output_format=png")
    print("retry_policy: max_retries=0")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not configured in this PowerShell session")
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite existing output: {OUTPUT}")

    provider = OpenAIImageProvider(api_key, MODEL)
    # The production adapter is intentionally unchanged; pin this one-shot
    # client to zero retries before the only paid request.
    provider._client.max_retries = 0  # type: ignore[attr-defined]
    if provider._client.max_retries != 0:  # type: ignore[attr-defined]
        raise SystemExit("failed to configure OpenAI SDK max_retries=0")

    response_holder: dict[str, object] = {}
    original_edit = provider._client.images.edit

    def edit_once(*args: object, **kwargs: object) -> object:
        kwargs.update(quality="low", n=1, size="1024x1024")
        response = original_edit(*args, **kwargs)
        response_holder["response"] = response
        return response

    provider._client.images.edit = edit_once  # type: ignore[method-assign]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    try:
        image_bytes = provider.generate(
            prompt=(
                "Edit this reference image into one polished square editorial image. "
                "Preserve the main subject and composition; do not add text or logos."
            ),
            source_image=SOURCE,
        )
    except (BadRequestError, APIStatusError) as exc:
        _print_api_error(exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        # Do not print request headers or credential-bearing SDK internals.
        print(f"OpenAI image request failed: {type(exc).__name__}")
        return 1

    OUTPUT.write_bytes(image_bytes)
    width, height = png_dimensions(OUTPUT)
    response = response_holder.get("response")
    response_data = getattr(response, "data", None)
    response_usage = getattr(response, "usage", None)
    print("API authentication/model request: PASS")
    print("images.edit requests: 1")
    print(f"response_image_data: {'present' if response_data else 'missing'}")
    print(f"response_usage: {response_usage if response_usage is not None else 'not returned'}")
    print(f"output: {OUTPUT.resolve()}")
    print(f"png_bytes: {len(image_bytes)}")
    print(f"dimensions: {width}x{height}")
    print("usage/cost: inspect the OpenAI Usage Dashboard; this adapter returns bytes only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
