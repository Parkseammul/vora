import subprocess
from pathlib import Path

from app.content_planning import ContentPlanningDraft
from app.e2e_providers import (
    DeterministicE2ELLMProvider,
    FFmpegE2ETTSProvider,
    FFmpegE2EVideoProvider,
)
from app.llm_provider import LLMProviderType
from app.media_providers import FFmpegVideoComposer
from app.publication_copy import PublicationCopyDraft
from app.revision_impact import RevisionImpactResult
from app.script_generation import ScriptGenerationResult
from app.video_generation import CompositionScene


def test_fake_planning_preserves_source_order_and_duration() -> None:
    provider = DeterministicE2ELLMProvider()
    result = provider.generate_structured(
        'Create plan. Input: {"duration_seconds": 30, "source_asset_ids": [10, 11]}',
        ContentPlanningDraft,
        LLMProviderType.OPENAI,
        "unused",
        images=["10", "11"],
    )
    plan = ContentPlanningDraft.model_validate(result.data)
    assert [scene.source_asset_id for scene in plan.scenes] == [10, 11]
    assert sum(scene.duration_seconds for scene in plan.scenes) == 30


def test_fake_planning_uses_original_request_meaning() -> None:
    provider = DeterministicE2ELLMProvider()
    plan = ContentPlanningDraft.model_validate(
        provider.generate_structured(
            'Create plan. Input: {"request_text": "러닝화를 홍보하는 감성적인 야간 러닝 광고를 만들어줘", "duration_seconds": 30, "source_asset_ids": []}',
            ContentPlanningDraft,
            LLMProviderType.OPENAI,
            "unused",
        ).data
    )
    assert "러닝화" in plan.concept
    assert "야간 러닝" in plan.visual_style


def test_fake_planning_changes_for_revision_without_breaking_contract() -> None:
    provider = DeterministicE2ELLMProvider()
    prompt = 'Create plan. Input: {"duration_seconds": 30, "source_asset_ids": [10, 11]}'
    initial = ContentPlanningDraft.model_validate(
        provider.generate_structured(
            prompt, ContentPlanningDraft, LLMProviderType.OPENAI, "unused", images=["10", "11"]
        ).data
    )
    revised = ContentPlanningDraft.model_validate(
        provider.generate_structured(
            prompt
            + ' Revision request: 전체 콘셉트를 감성적인 야간 러닝 분위기로 바꾸고 나만의 페이스를 강조해줘.'
            + " Previous result to revise: "
            + initial.model_dump_json(),
            ContentPlanningDraft,
            LLMProviderType.OPENAI,
            "unused",
            images=["10", "11"],
        ).data
    )

    assert (revised.concept, revised.hook, revised.key_message, revised.visual_style) != (
        initial.concept,
        initial.hook,
        initial.key_message,
        initial.visual_style,
    )
    assert "야간 러닝" in revised.concept
    assert "나만의 페이스" in revised.key_message
    assert [scene.source_asset_id for scene in revised.scenes] == [10, 11]
    assert sum(scene.duration_seconds for scene in revised.scenes) == 30
    assert revised.scenes[-1].transition_to_next is None
    assert all(scene.transition_to_next is not None for scene in revised.scenes[:-1])


def test_fake_script_and_revision_impact_are_deterministic() -> None:
    provider = DeterministicE2ELLMProvider()
    script = provider.generate_structured(
        'Write script. Planning: {"scenes": [{"scene_id": "scene-1"}]}',
        ScriptGenerationResult,
        LLMProviderType.OPENAI,
        "unused",
    )
    impact = provider.generate_structured(
        'Choose target. Input: {"revision_request": "기획 콘셉트 자체를 수정해줘"}',
        RevisionImpactResult,
        LLMProviderType.OPENAI,
        "unused",
    )
    assert ScriptGenerationResult.model_validate(script.data).scenes[0].planning_scene_id == "scene-1"
    assert RevisionImpactResult.model_validate(impact.data).target_node.value == "content_planning"

    script_impact = provider.generate_structured(
        'Choose target. Input: {"revision_request": "첫 문장을 더 강하게 바꿔줘"}',
        RevisionImpactResult,
        LLMProviderType.OPENAI,
        "unused",
    )
    assert RevisionImpactResult.model_validate(script_impact.data).target_node.value == "script_generation"


def test_fake_media_providers_write_valid_ffmpeg_inputs(tmp_path: Path) -> None:
    video = FFmpegE2EVideoProvider().generate_scene("prompt", 1, tmp_path / "scene.mp4", None)
    audio = FFmpegE2ETTSProvider().synthesize("narration", tmp_path / "speech.mp3")
    assert video.path.stat().st_size > 0
    assert audio.path.stat().st_size > 0
    for path in (video.path, audio.path):
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)],
            check=True,
            capture_output=True,
        )


def test_composer_renders_korean_subtitle_with_explicit_windows_font(tmp_path: Path) -> None:
    font = Path(r"C:\Windows\Fonts\malgun.ttf")
    scene = FFmpegE2EVideoProvider().generate_scene("prompt", 1, tmp_path / "scene.mp4", None)
    audio = FFmpegE2ETTSProvider().synthesize("narration", tmp_path / "speech.mp3")
    bgm = FFmpegE2ETTSProvider().synthesize("bgm", tmp_path / "bgm.mp3")
    output = tmp_path / "final.mp4"

    FFmpegVideoComposer(font).compose(
        [
            CompositionScene(
                "korean-subtitle", scene.path, 1, audio.path, "VORA와 함께 시작하세요", audio.timestamps
            )
        ],
        bgm.path,
        output,
    )

    assert output.is_file()
    subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-of", "json", str(output)],
        check=True,
        capture_output=True,
    )


def test_fake_publication_copy_uses_planning_content() -> None:
    provider = DeterministicE2ELLMProvider()

    copy = provider.generate_structured(
        'Create publication copy. Input: {"planning": {"concept": "VORA launch", "key_message": "Try VORA"}, "script": {}}',
        PublicationCopyDraft,
        LLMProviderType.OPENAI,
        "unused",
    )

    assert copy.data.youtube_title == "VORA launch"
    assert copy.data.youtube_description == "Try VORA"
