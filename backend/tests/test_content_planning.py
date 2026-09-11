from collections.abc import Sequence
from typing import TypeVar

import pytest
from pydantic import BaseModel

from app.content_planning import ContentPlanningDraft, ContentPlanningService
from app.llm_provider import LLMMetadata, LLMProviderType, LLMResult

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class FakeProvider:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        provider: LLMProviderType,
        model: str,
        images: Sequence[str] = (),
    ) -> LLMResult:
        return LLMResult(
            data=response_model.model_validate(self.payload),
            metadata=LLMMetadata(provider=provider, model=model),
        )


def payload(*, duration: float = 30, transition: str | None = None) -> dict[str, object]:
    return {
        "concept": "concept",
        "hook": "hook",
        "key_message": "message",
        "cta": "cta",
        "visual_style": "clean",
        "bgm_direction": "upbeat",
        "scenes": [
            {
                "purpose": "intro",
                "main_objects": [],
                "description": "visual",
                "duration_seconds": duration,
                "source_asset_id": None,
                "visual_direction": "wide",
                "transition_to_next": transition,
            }
        ],
    }


def analysis() -> dict[str, object]:
    return {
        "content_goal": "GENERAL_SHORTFORM",
        "target_audience": {"age_group": "ALL", "gender": "ALL", "audience_group": "all"},
        "duration_seconds": 30,
        "tone": "bright",
        "source_asset_ids": [],
    }


def test_text_only_plan_gets_backend_scene_id_and_metadata() -> None:
    service = ContentPlanningService(
        FakeProvider(payload()), LLMProviderType.OPENAI, "planning-model"
    )

    output = service.generate(analysis())

    assert output.data.scenes[0].scene_id
    assert output.data.scenes[0].source_asset_id is None
    assert output.metadata.model == "planning-model"


def test_plan_rejects_duration_sum_and_final_transition() -> None:
    service = ContentPlanningService(
        FakeProvider(payload(duration=29)), LLMProviderType.OPENAI, "model"
    )
    with pytest.raises(ValueError, match="duration sum"):
        service.generate(analysis())

    invalid_draft = payload(transition="CUT")
    service = ContentPlanningService(
        FakeProvider(invalid_draft), LLMProviderType.OPENAI, "model"
    )
    with pytest.raises(ValueError, match="final scene"):
        service.generate(analysis())


def test_duration_allows_at_most_one_decimal_place() -> None:
    with pytest.raises(ValueError, match="decimal place"):
        ContentPlanningDraft.model_validate(payload(duration=29.99))
