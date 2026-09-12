"""Register a fixed BGM file as an AUDIO FileAsset without changing the schema."""

import argparse
import mimetypes
import shutil
import sys
from pathlib import Path
from uuid import uuid4

# Allow `python scripts/register_fixed_bgm.py` from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import engine
from app.models import AssetType, ExecutionInputSnapshot, FileAsset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_execution_id", type=int)
    parser.add_argument("audio_file", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    audio_file = args.audio_file.resolve()
    if not audio_file.is_file():
        raise ValueError(f"Audio file does not exist: {audio_file}")
    mime_type = mimetypes.guess_type(audio_file.name)[0]
    if mime_type is None or not mime_type.startswith("audio/"):
        raise ValueError("audio_file must have a recognized audio MIME type")

    with Session(engine) as session:
        snapshot = session.scalar(
            select(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == args.workflow_execution_id
            )
        )
        if snapshot is None:
            raise ValueError("workflow_execution_id must have an ExecutionInputSnapshot")

        storage_key = f"fixed-bgm/{uuid4()}{audio_file.suffix.lower()}"
        destination = settings.uploads_root / storage_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(audio_file, destination)
        asset = FileAsset(
            workflow_execution_id=args.workflow_execution_id,
            execution_input_snapshot_id=snapshot.id,
            asset_type=AssetType.AUDIO,
            storage_key=storage_key,
            file_name=audio_file.name,
            mime_type=mime_type,
            file_size=destination.stat().st_size,
        )
        session.add(asset)
        session.commit()
        print(f"Registered fixed BGM FileAsset ID: {asset.id}")
        print("Set FIXED_BGM_ASSET_ID to this value before starting video generation.")


if __name__ == "__main__":
    main()
