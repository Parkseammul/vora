from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.content_planning import ContentPlanningResult, ScenePlan
from app.models import AssetType, FileAsset
from app.script_generation import ScriptGenerationResult, ScriptScene


@dataclass(frozen=True)
class GeneratedSceneVideo:
    path: Path
    metadata: dict[str, object]


@dataclass(frozen=True)
class GeneratedSpeech:
    path: Path
    timestamps: list[dict[str, object]]
    metadata: dict[str, object]


@dataclass(frozen=True)
class CompositionScene:
    scene_id: str
    video_path: Path
    duration_seconds: float
    audio_path: Path | None
    subtitle: str | None
    timestamps: list[dict[str, object]]


class VideoProvider(Protocol):
    def generate_scene(
        self, prompt: str, duration_seconds: float, output_path: Path, reference_image: Path | None
    ) -> GeneratedSceneVideo: ...


class TTSProvider(Protocol):
    def synthesize(self, narration: str, output_path: Path) -> GeneratedSpeech: ...


class VideoComposer(Protocol):
    def compose(
        self, scenes: Sequence[CompositionScene], bgm_path: Path, output_path: Path
    ) -> None: ...


class VideoGenerationResult(BaseModel):
    width: int = 1080
    height: int = 1920
    format: str = "mp4"


@dataclass(frozen=True)
class VideoGenerationOutput:
    data: VideoGenerationResult
    final_path: Path
    metadata: dict[str, object]


class VideoGenerationService:
    """Coordinates media providers; workflow state and FileAsset persistence stay outside it."""

    def __init__(
        self,
        session: Session,
        video_provider: VideoProvider,
        tts_provider: TTSProvider,
        composer: VideoComposer,
        uploads_root: Path,
        fixed_bgm_asset_id: int | None,
    ) -> None:
        self._session = session
        self._video_provider = video_provider
        self._tts_provider = tts_provider
        self._composer = composer
        self._uploads_root = uploads_root
        self._fixed_bgm_asset_id = fixed_bgm_asset_id

    def generate(self, input_data: Mapping[str, object], workflow_execution_id: int) -> VideoGenerationOutput:
        planning = ContentPlanningResult.model_validate(input_data["planning"])
        script = ScriptGenerationResult.model_validate(input_data["script"])
        script_by_scene = self._validate_scene_mapping(planning, script)
        assets = self._source_assets(planning, workflow_execution_id)
        bgm_path = self._bgm_path()
        output_dir = self._uploads_root / "generated" / str(workflow_execution_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        composition_scenes: list[CompositionScene] = []
        scene_metadata: list[dict[str, object]] = []
        for scene in planning.scenes:
            script_scene = script_by_scene[scene.scene_id]
            reference = assets.get(scene.source_asset_id) if scene.source_asset_id is not None else None
            video = self._video_provider.generate_scene(
                self._scene_prompt(scene), scene.duration_seconds, output_dir / f"{scene.scene_id}.mp4", reference
            )
            speech = (
                self._tts_provider.synthesize(script_scene.narration, output_dir / f"{scene.scene_id}.mp3")
                if script_scene.narration is not None
                else None
            )
            composition_scenes.append(
                CompositionScene(
                    scene.scene_id, video.path, scene.duration_seconds, speech.path if speech else None,
                    script_scene.subtitle, speech.timestamps if speech else [],
                )
            )
            scene_metadata.append({"video": video.metadata, "tts": speech.metadata if speech else None})
        final_path = output_dir / "final_video.mp4"
        self._composer.compose(composition_scenes, bgm_path, final_path)
        if not final_path.is_file():
            raise RuntimeError("FFmpeg composition did not produce final_video.mp4")
        return VideoGenerationOutput(VideoGenerationResult(), final_path, {"scenes": scene_metadata})

    @property
    def uploads_root(self) -> Path:
        return self._uploads_root

    def _source_assets(self, planning: ContentPlanningResult, execution_id: int) -> dict[int, Path]:
        ids = [scene.source_asset_id for scene in planning.scenes if scene.source_asset_id is not None]
        assets = self._session.scalars(select(FileAsset).where(FileAsset.id.in_(ids))).all() if ids else []
        found = {asset.id: asset for asset in assets}
        paths: dict[int, Path] = {}
        for asset_id in ids:
            asset = found.get(asset_id)
            if asset is None or asset.workflow_execution_id != execution_id or asset.asset_type is not AssetType.IMAGE:
                raise ValueError(f"Invalid source image asset: {asset_id}")
            paths[asset_id] = self._uploads_root / asset.storage_key
        return paths

    def _bgm_path(self) -> Path:
        if self._fixed_bgm_asset_id is None:
            raise RuntimeError("fixed_bgm_asset_id is not configured")
        asset = self._session.get(FileAsset, self._fixed_bgm_asset_id)
        if asset is None or asset.asset_type is not AssetType.AUDIO:
            raise ValueError("Configured fixed BGM FileAsset is invalid")
        return self._uploads_root / asset.storage_key

    @staticmethod
    def _validate_scene_mapping(
        planning: ContentPlanningResult, script: ScriptGenerationResult
    ) -> dict[str, ScriptScene]:
        if [scene.planning_scene_id for scene in script.scenes] != [scene.scene_id for scene in planning.scenes]:
            raise ValueError("Script scenes must match planning scenes in order")
        return {scene.planning_scene_id: scene for scene in script.scenes}

    @staticmethod
    def _scene_prompt(scene: ScenePlan) -> str:
        return f"{scene.description}\nVisual direction: {scene.visual_direction}"
