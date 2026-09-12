import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from shutil import rmtree
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.database import engine
from app.media_providers import FFmpegVideoComposer
from app.models import (
    ApprovalDecision,
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    InputType,
    NodeExecution,
    NodeExecutionStatus,
    User,
    UserApproval,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.node_executors import NodeExecutionResult, RuleBasedNodeExecutor
from app.video_generation import (
    CompositionScene,
    GeneratedSceneVideo,
    GeneratedSpeech,
    VideoGenerationService,
)
from app.workflow_definitions import workflow_registry
from app.workflow_engine import WorkflowEngine


class FakeVideoProvider:
    def __init__(self) -> None:
        self.references: list[Path | None] = []

    def generate_scene(
        self, prompt: str, duration_seconds: float, output_path: Path, reference_image: Path | None
    ) -> GeneratedSceneVideo:
        self.references.append(reference_image)
        output_path.write_bytes(b"scene")
        return GeneratedSceneVideo(output_path, {"model": "seedance-2.5"})


class FakeTTSProvider:
    def __init__(self) -> None:
        self.narrations: list[str] = []

    def synthesize(self, narration: str, output_path: Path) -> GeneratedSpeech:
        self.narrations.append(narration)
        output_path.write_bytes(b"audio")
        return GeneratedSpeech(output_path, [{"start": 0, "end": 1}], {"voice": "same"})


class FakeComposer:
    def __init__(self) -> None:
        self.scenes: Sequence[CompositionScene] = ()
        self.bgm_path: Path | None = None

    def compose(self, scenes: Sequence[CompositionScene], bgm_path: Path, output_path: Path) -> None:
        self.scenes = scenes
        self.bgm_path = bgm_path
        output_path.write_bytes(b"final-mp4")


class FixtureVideoProvider(FakeVideoProvider):
    """Creates a valid local MP4 so the real FFmpeg composer is exercised offline."""

    def generate_scene(
        self, prompt: str, duration_seconds: float, output_path: Path, reference_image: Path | None
    ) -> GeneratedSceneVideo:
        self.references.append(reference_image)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x568:d=1",
                "-c:v",
                "libx264",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        return GeneratedSceneVideo(output_path, {"provider": "fake-video"})


class FixtureTTSProvider(FakeTTSProvider):
    def synthesize(self, narration: str, output_path: Path) -> GeneratedSpeech:
        self.narrations.append(narration)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "1",
                "-f",
                "mp3",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        return GeneratedSpeech(output_path, [{"start": 0, "end": 1}], {"provider": "fake-tts"})


def test_scene_failure_keeps_prior_scene_file(uploads_root: Path) -> None:
    class SessionStub:
        def get(self, model: object, asset_id: int) -> object:
            return SimpleNamespace(asset_type=AssetType.AUDIO, storage_key="bgm.mp3")

        def scalars(self, statement: object) -> list[object]:
            return []

    class FailingProvider(FakeVideoProvider):
        def generate_scene(self, prompt: str, duration_seconds: float, output_path: Path, reference_image: Path | None) -> GeneratedSceneVideo:
            if len(self.references) == 1:
                raise RuntimeError("scene failed")
            return super().generate_scene(prompt, duration_seconds, output_path, reference_image)

    (uploads_root / "bgm.mp3").parent.mkdir(parents=True)
    (uploads_root / "bgm.mp3").write_bytes(b"bgm")
    service = VideoGenerationService(SessionStub(), FailingProvider(), FakeTTSProvider(), FakeComposer(), uploads_root, 1)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="scene failed"):
        service.generate({"planning": {"concept": "c", "hook": "h", "key_message": "k", "cta": "c", "visual_style": "v", "bgm_direction": "b", "scenes": [{"scene_id": "one", "purpose": "p", "main_objects": [], "description": "d", "duration_seconds": 5, "source_asset_id": None, "visual_direction": "v", "transition_to_next": "CUT"}, {"scene_id": "two", "purpose": "p", "main_objects": [], "description": "d", "duration_seconds": 5, "source_asset_id": None, "visual_direction": "v", "transition_to_next": None}]}, "script": {"scenes": [{"planning_scene_id": "one"}, {"planning_scene_id": "two"}]}}, 1)
    assert (uploads_root / "generated" / "1" / "one.mp4").is_file()


def test_ffmpeg_composer_creates_small_mp4(uploads_root: Path) -> None:
    scene = uploads_root / "source.mp4"
    bgm = uploads_root / "bgm.wav"
    output = uploads_root / "final.mp4"
    uploads_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x568:d=1", "-c:v", "libx264", str(scene)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", str(bgm)], check=True, capture_output=True)
    FFmpegVideoComposer().compose([CompositionScene("fixture", scene, 1, None, None, [])], bgm, output)
    assert output.is_file() and output.stat().st_size > 0


def test_video_node_generates_media_and_persists_final_video_asset(
    session: Session, uploads_root: Path
) -> None:
    user = User(email=f"{uuid4()}@example.com", name="video tester")
    session.add(user)
    session.flush()
    execution = WorkflowExecution(user_id=user.id, workflow_key="vora_content_creation", workflow_version=1)
    session.add(execution)
    session.flush()
    snapshot = ExecutionInputSnapshot(
        workflow_execution_id=execution.id, input_type=InputType.TEXT_IMAGE, request_text="video"
    )
    session.add(snapshot)
    session.flush()
    image = FileAsset(workflow_execution_id=execution.id, execution_input_snapshot_id=snapshot.id, asset_type=AssetType.IMAGE, storage_key=f"input/{uuid4()}.jpg", file_name="source.jpg", mime_type="image/jpeg", file_size=1, sort_order=1)
    bgm = FileAsset(workflow_execution_id=execution.id, execution_input_snapshot_id=snapshot.id, asset_type=AssetType.AUDIO, storage_key=f"bgm/{uuid4()}.mp3", file_name="bgm.mp3", mime_type="audio/mpeg", file_size=1)
    session.add_all([image, bgm])
    session.flush()
    (uploads_root / image.storage_key).parent.mkdir(parents=True)
    (uploads_root / image.storage_key).write_bytes(b"image")
    (uploads_root / bgm.storage_key).parent.mkdir(parents=True)
    (uploads_root / bgm.storage_key).write_bytes(b"bgm")
    node = NodeExecution(workflow_execution_id=execution.id, node_key="video_generation", input_data={})
    session.add(node)
    session.flush()
    video_provider, tts_provider, composer = FakeVideoProvider(), FakeTTSProvider(), FakeComposer()
    executor = RuleBasedNodeExecutor(
        video_generation_service=VideoGenerationService(session, video_provider, tts_provider, composer, uploads_root, bgm.id),
        session=session,
    )
    result = executor.execute(
        "video_generation",
        {
            "planning": {"concept": "c", "hook": "h", "key_message": "k", "cta": "c", "visual_style": "v", "bgm_direction": "b", "scenes": [{"scene_id": "s1", "purpose": "p", "main_objects": [], "description": "d", "duration_seconds": 15, "source_asset_id": image.id, "visual_direction": "cinematic", "transition_to_next": "CUT"}, {"scene_id": "s2", "purpose": "p", "main_objects": [], "description": "d", "duration_seconds": 15, "source_asset_id": None, "visual_direction": "cinematic", "transition_to_next": None}]},
            "script": {"scenes": [{"planning_scene_id": "s1", "narration": "hello", "subtitle": "shown", "speaking_style": "calm", "emphasis_keywords": []}, {"planning_scene_id": "s2", "narration": None, "subtitle": None, "speaking_style": None, "emphasis_keywords": []}]},
        }, workflow_execution_id=execution.id, node_execution_id=node.id
    )
    assert isinstance(result, NodeExecutionResult)
    asset = session.get(FileAsset, result.output_data["video_asset_id"])
    assert asset is not None and asset.asset_type is AssetType.VIDEO and asset.node_execution_id == node.id
    assert video_provider.references == [uploads_root / image.storage_key, None]
    assert tts_provider.narrations == ["hello"]
    assert composer.bgm_path == uploads_root / bgm.storage_key
    assert composer.scenes[0].subtitle == "shown"
    assert composer.scenes[1].subtitle is None and composer.scenes[1].audio_path is None


def test_invalid_fixed_bgm_asset_fails_before_scene_generation(
    session: Session, uploads_root: Path
) -> None:
    user = User(email=f"{uuid4()}@example.com", name="invalid bgm tester")
    session.add(user)
    session.flush()
    execution = WorkflowExecution(user_id=user.id, workflow_key="vora_content_creation", workflow_version=1)
    session.add(execution)
    session.flush()
    snapshot = ExecutionInputSnapshot(
        workflow_execution_id=execution.id, input_type=InputType.TEXT, request_text="video"
    )
    session.add(snapshot)
    session.flush()
    invalid_bgm = FileAsset(
        workflow_execution_id=execution.id,
        execution_input_snapshot_id=snapshot.id,
        asset_type=AssetType.IMAGE,
        storage_key=f"input/{uuid4()}.jpg",
        file_name="not-bgm.jpg",
        mime_type="image/jpeg",
        file_size=1,
        sort_order=1,
    )
    session.add(invalid_bgm)
    session.flush()
    provider = FakeVideoProvider()
    service = VideoGenerationService(
        session, provider, FakeTTSProvider(), FakeComposer(), uploads_root, invalid_bgm.id
    )

    with pytest.raises(ValueError, match="fixed BGM FileAsset is invalid"):
        service.generate(_text_only_video_input(), execution.id)

    assert provider.references == []


def test_script_approval_generates_final_video_asset_and_waits_for_video_approval(
    session: Session, uploads_root: Path
) -> None:
    user = User(email=f"{uuid4()}@example.com", name="workflow video tester")
    session.add(user)
    session.flush()
    execution = WorkflowExecution(
        user_id=user.id,
        workflow_key="vora_content_creation",
        workflow_version=1,
        status=WorkflowExecutionStatus.WAITING_APPROVAL,
    )
    session.add(execution)
    session.flush()
    snapshot = ExecutionInputSnapshot(
        workflow_execution_id=execution.id, input_type=InputType.TEXT, request_text="offline video"
    )
    session.add(snapshot)
    session.flush()
    bgm = FileAsset(
        workflow_execution_id=execution.id,
        execution_input_snapshot_id=snapshot.id,
        asset_type=AssetType.AUDIO,
        storage_key=f"fixed-bgm/{uuid4()}.wav",
        file_name="fixed.wav",
        mime_type="audio/wav",
        file_size=1,
    )
    session.add(bgm)
    session.flush()
    bgm_path = uploads_root / bgm.storage_key
    bgm_path.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "1",
            str(bgm_path),
        ],
        check=True,
        capture_output=True,
    )
    bgm.file_size = bgm_path.stat().st_size
    planning = NodeExecution(
        workflow_execution_id=execution.id,
        node_key="content_planning",
        status=NodeExecutionStatus.SUCCESS,
        output_data=_text_only_video_input()["planning"],
    )
    script = NodeExecution(
        workflow_execution_id=execution.id,
        node_key="script_generation",
        status=NodeExecutionStatus.WAITING_APPROVAL,
        output_data=_text_only_video_input()["script"],
    )
    session.add_all([planning, script])
    session.flush()
    session.add(
        UserApproval(
            node_execution_id=script.id,
            user_id=user.id,
            decision=ApprovalDecision.APPROVED,
        )
    )
    session.commit()

    video_provider = FixtureVideoProvider()
    tts_provider = FixtureTTSProvider()
    executor = RuleBasedNodeExecutor(
        video_generation_service=VideoGenerationService(
            session,
            video_provider,
            tts_provider,
            FFmpegVideoComposer(),
            uploads_root,
            bgm.id,
        ),
        session=session,
    )
    status = WorkflowEngine(session, workflow_registry, executor).resume_after_approval(execution.id)

    assert status is WorkflowExecutionStatus.WAITING_APPROVAL
    session.refresh(execution)
    assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
    video_node = session.query(NodeExecution).filter_by(
        workflow_execution_id=execution.id, node_key="video_generation"
    ).one()
    assert video_node.status is NodeExecutionStatus.WAITING_APPROVAL
    asset = session.get(FileAsset, video_node.output_data["video_asset_id"])
    assert asset is not None and asset.asset_type is AssetType.VIDEO
    assert asset.node_execution_id == video_node.id
    assert (uploads_root / asset.storage_key).name == "final_video.mp4"
    assert (uploads_root / asset.storage_key).stat().st_size > 0
    assert video_provider.references == [None]
    assert tts_provider.narrations == ["offline narration"]


def _text_only_video_input() -> dict[str, object]:
    return {
        "planning": {
            "concept": "c",
            "hook": "h",
            "key_message": "k",
            "cta": "c",
            "visual_style": "v",
            "bgm_direction": "b",
            "scenes": [
                {
                    "scene_id": "offline-scene",
                    "purpose": "p",
                    "main_objects": [],
                    "description": "d",
                    "duration_seconds": 1,
                    "source_asset_id": None,
                    "visual_direction": "cinematic",
                    "transition_to_next": None,
                }
            ],
        },
        "script": {
            "scenes": [
                {
                    "planning_scene_id": "offline-scene",
                    "narration": "offline narration",
                    "subtitle": None,
                    "speaking_style": "calm",
                    "emphasis_keywords": [],
                }
            ]
        },
    }


@pytest.fixture
def session() -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    db_session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield db_session
    finally:
        db_session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def uploads_root() -> Iterator[Path]:
    root = Path("uploads") / f"test-video-{uuid4()}"
    try:
        yield root
    finally:
        rmtree(root, ignore_errors=True)
