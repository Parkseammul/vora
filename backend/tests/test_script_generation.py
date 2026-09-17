from collections.abc import Sequence
from typing import TypeVar

import pytest
from pydantic import BaseModel

from app.content_planning import ContentPlanningResult
from app.llm_provider import LLMMetadata, LLMProviderType, LLMResult
from app.script_generation import ScriptGenerationService, ScriptScene

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class FakeProvider:
    def __init__(self, scenes: list[dict[str, object]]) -> None:
        self.scenes = scenes
        self.prompt: str | None = None

    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        provider: LLMProviderType,
        model: str,
        images: Sequence[str] = (),
    ) -> LLMResult:
        self.prompt = prompt
        return LLMResult(
            data=response_model.model_validate({"scenes": self.scenes}),
            metadata=LLMMetadata(provider=provider, model=model),
        )


class FixedEstimator:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def estimate_seconds(self, text: str) -> float:
        return self.seconds


def planning() -> dict[str, object]:
    return ContentPlanningResult.model_validate(
        {
            "concept": "concept",
            "hook": "hook",
            "key_message": "message",
            "cta": "cta",
            "visual_style": "clean",
            "bgm_direction": "upbeat",
            "scenes": [
                {
                    "scene_id": "scene-1",
                    "purpose": "intro",
                    "main_objects": [],
                    "description": "visual",
                    "duration_seconds": 5,
                    "source_asset_id": None,
                    "visual_direction": "wide",
                    "transition_to_next": None,
                }
            ],
        }
    ).model_dump(mode="json")


def test_script_preserves_planning_scene_and_allows_pure_visual_scene() -> None:
    provider = FakeProvider(
        [{"planning_scene_id": "scene-1", "narration": None, "subtitle": None}]
    )
    service = ScriptGenerationService(provider, LLMProviderType.CLAUDE, "script-model")

    output = service.generate(planning())

    assert output.data.scenes[0].planning_scene_id == "scene-1"
    assert output.data.scenes[0].emphasis_keywords == []


def test_script_rejects_wrong_scene_reference_and_duration_overflow() -> None:
    wrong = ScriptGenerationService(
        FakeProvider([{"planning_scene_id": "other"}]),
        LLMProviderType.GEMINI,
        "model",
    )
    with pytest.raises(ValueError, match="every planning scene"):
        wrong.generate(planning())

    too_long = ScriptGenerationService(
        FakeProvider([{"planning_scene_id": "scene-1", "narration": "hello"}]),
        LLMProviderType.GEMINI,
        "model",
        tts_estimator=FixedEstimator(5.1),
    )
    with pytest.raises(ValueError, match="Narration exceeds"):
        too_long.generate(planning())


def test_speaking_style_requires_narration() -> None:
    with pytest.raises(ValueError, match="speaking_style"):
        ScriptScene(planning_scene_id="scene-1", speaking_style="energetic")


def test_korean_planning_instructs_korean_script_output() -> None:
    provider = FakeProvider([{"planning_scene_id": "scene-1", "narration": "한국어 대본"}])

    ScriptGenerationService(provider, LLMProviderType.OPENAI, "model").generate(planning())

    assert provider.prompt is not None
    assert "write narration, subtitles, speaking_style, and emphasis_keywords in Korean" in provider.prompt


def test_korean_three_scene_script_preserves_order_and_duration_limits() -> None:
    korean_plan = ContentPlanningResult.model_validate(
        {
            "concept": "텀블러 소개",
            "hook": "출근길 필수품",
            "key_message": "가볍고 편리합니다",
            "cta": "지금 골라보세요",
            "visual_style": "밝은 제품 영상",
            "bgm_direction": "경쾌한 리듬",
            "scenes": [
                {
                    "scene_id": "scene-1",
                    "purpose": "hook",
                    "main_objects": ["텀블러"],
                    "description": "텀블러를 드는 장면",
                    "duration_seconds": 5,
                    "visual_direction": "제품 클로즈업",
                    "transition_to_next": "CUT",
                },
                {
                    "scene_id": "scene-2",
                    "purpose": "benefit",
                    "main_objects": ["텀블러"],
                    "description": "가방에 넣는 장면",
                    "duration_seconds": 5,
                    "visual_direction": "사용 장면",
                    "transition_to_next": "CUT",
                },
                {
                    "scene_id": "scene-3",
                    "purpose": "cta",
                    "main_objects": ["텀블러"],
                    "description": "출근하는 장면",
                    "duration_seconds": 5,
                    "visual_direction": "밝은 마무리",
                    "transition_to_next": None,
                },
            ],
        }
    ).model_dump(mode="json")
    provider = FakeProvider(
        [
            {"planning_scene_id": "scene-1", "narration": "출근길을 가볍게", "subtitle": "가볍게 시작"},
            {"planning_scene_id": "scene-2", "narration": "가방에도 쏙 들어가요", "subtitle": "간편한 휴대"},
            {"planning_scene_id": "scene-3", "narration": "오늘의 텀블러를 골라보세요", "subtitle": "나만의 선택"},
        ]
    )

    output = ScriptGenerationService(provider, LLMProviderType.OPENAI, "model").generate(korean_plan)

    assert [scene.planning_scene_id for scene in output.data.scenes] == [
        "scene-1",
        "scene-2",
        "scene-3",
    ]
    assert all(scene.narration and scene.subtitle for scene in output.data.scenes)
