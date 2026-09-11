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

    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        provider: LLMProviderType,
        model: str,
        images: Sequence[str] = (),
    ) -> LLMResult:
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
