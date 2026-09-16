"""Upload a file through VORA storage and verify private presigned access."""

import argparse
import mimetypes
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

# Allow `python scripts/verify_s3_storage.py` from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.publication_runtime import get_storage_provider

DEFAULT_URL_EXPIRY_SECONDS = 3600


def build_verification_key(source: Path) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"verification/s3/{timestamp}-{uuid4().hex}{source.suffix.lower()}"


def public_object_url(bucket: str, region: str, storage_key: str) -> str:
    encoded_key = quote(storage_key, safe="/")
    return f"https://{bucket}.s3.{region}.amazonaws.com/{encoded_key}"


def verify_http_get(url: str) -> int:
    request = Request(url, headers={"Range": "bytes=0-0"})
    with urlopen(request, timeout=30) as response:
        response.read(1)
        return response.status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="A small MP4 or other file to upload")
    parser.add_argument(
        "--expires-seconds",
        type=int,
        default=DEFAULT_URL_EXPIRY_SECONDS,
        help="Presigned GET URL lifetime (default: 3600)",
    )
    args = parser.parse_args()

    source = args.file.resolve()
    if not source.is_file():
        parser.error(f"File does not exist: {source}")
    if settings.storage_provider.lower() != "s3":
        parser.error("STORAGE_PROVIDER must be s3")
    if not settings.s3_bucket or not settings.s3_region:
        parser.error("S3_BUCKET and S3_REGION must be configured")
    if args.expires_seconds <= 0:
        parser.error("--expires-seconds must be greater than zero")

    storage_key = build_verification_key(source)
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    storage = get_storage_provider()

    print(f"Uploading: {source}")
    storage.upload_private(storage_key, source, content_type)
    print(f"Object key: {storage_key}")

    presigned_url = storage.presigned_get_url(storage_key, args.expires_seconds)
    print(f"Presigned URL ({args.expires_seconds}s): {presigned_url}")
    status = verify_http_get(presigned_url)
    print(f"Presigned GET: HTTP {status}")

    unsigned_url = public_object_url(settings.s3_bucket, settings.s3_region, storage_key)
    try:
        unsigned_status = verify_http_get(unsigned_url)
    except HTTPError as exc:
        print(f"Unsigned public GET: HTTP {exc.code} (private access confirmed)")
    else:
        raise RuntimeError(
            f"Unsigned public GET unexpectedly succeeded with HTTP {unsigned_status}: {unsigned_url}"
        )

    print("Object was intentionally left in S3 for console verification.")


if __name__ == "__main__":
    main()
