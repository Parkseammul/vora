"""Preparation of immutable, derived JPEGs for Instagram Carousel publishing."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models import FileAsset
from app.storage import StorageProvider


class CarouselImagePreparer:
    """Convert approved source images without ever modifying their FileAsset or key."""

    def __init__(self, storage: StorageProvider, max_bytes: int = 8 * 1024 * 1024) -> None:
        self._storage = storage
        self._max_bytes = max_bytes

    @staticmethod
    def storage_key(publication_id: int, asset_id: int) -> str:
        return f"publications/{publication_id}/instagram-carousel/{asset_id}.jpg"

    def prepare(self, publication_id: int, assets: Sequence[FileAsset]) -> list[str]:
        urls: list[str] = []
        with TemporaryDirectory(prefix="vora-carousel-") as temp_dir:
            root = Path(temp_dir)
            for asset in assets:
                key = self.storage_key(publication_id, asset.id)
                if not self._storage.exists(key):
                    source, derived = root / f"{asset.id}-source", root / f"{asset.id}.jpg"
                    self._storage.materialize_private(asset.storage_key, source)
                    self._convert_to_jpeg(source, derived)
                    self._storage.upload_private(key, derived, "image/jpeg")
                # URLs are intentionally transient: never place them in metadata or logs.
                url = self._storage.presigned_get_url(key, expires_seconds=6 * 60 * 60)
                if not url.startswith("https://"):
                    raise ValueError("Instagram requires an HTTPS image URL")
                urls.append(url)
        return urls

    def _convert_to_jpeg(self, source: Path, destination: Path) -> None:
        try:
            from PIL import (  # type: ignore[import-not-found]
                Image,
                UnidentifiedImageError,
            )
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError("Pillow is required for Instagram Carousel publishing") from exc
        try:
            with Image.open(source) as image:
                image.load()
                if image.width < 320 or image.height < 320:
                    raise ValueError("Instagram Carousel image dimensions are too small")
                ratio = image.width / image.height
                if not 0.8 <= ratio <= 1.91:
                    raise ValueError("Instagram Carousel image aspect ratio is unsupported")
                image.convert("RGB").save(destination, format="JPEG", quality=90, optimize=True)
        except (OSError, UnidentifiedImageError) as exc:
            raise ValueError("Approved asset is not a readable image") from exc
        if destination.stat().st_size > self._max_bytes:
            raise ValueError("Instagram Carousel JPEG exceeds the configured size limit")
