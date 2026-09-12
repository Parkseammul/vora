import json
import math
from typing import Protocol

from pydantic import BaseModel, Field, model_validator

from app.content_planning import ContentPlanningResult
from app.llm_provider import LLMMetadata, LLMProvider, LLMProviderType


class ScriptScene(BaseModel):
    planning_scene_id: str
    narration: str | None = None
    subtitle: str | None = None
    speaking_style: str | None = None
    emphasis_keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def speaking_style_requires_narration(self) -> "ScriptScene":
        if self.narration is None and self.speaking_style is not None:
            raise ValueError("speaking_style must be null when narration is null")
        return self


class ScriptGenerationResult(BaseModel):
    scenes: list[ScriptScene]


class ScriptGenerationOutput(BaseModel):
    data: ScriptGenerationResult
    metadata: LLMMetadata


class TextDurationEstimator(Protocol):
    def estimate_seconds(self, text: str) -> float: ...


class CharacterDurationEstimator:
    def __init__(self, characters_per_second: float) -> None:
        if characters_per_second <= 0:
            raise ValueError("characters_per_second must be positive")
        self._characters_per_second = characters_per_second

    def estimate_seconds(self, text: str) -> float:
        return math.ceil(len(text.strip()) / self._characters_per_second * 10) / 10


class ScriptGenerationService:
    def __init__(
        self,
        llm_provider: LLMProvider,
        provider: LLMProviderType,
        model: str,
        tts_estimator: TextDurationEstimator | None = None,
        subtitle_estimator: TextDurationEstimator | None = None,
    ) -> None:
        self._llm_provider = llm_provider
        self._provider = provider
        self._model = model
        self._tts_estimator = tts_estimator or CharacterDurationEstimator(5.0)
        self._subtitle_estimator = subtitle_estimator or CharacterDurationEstimator(7.0)

    def generate(self, input_data: dict[str, object]) -> ScriptGenerationOutput:
        planning = ContentPlanningResult.model_validate(input_data)
        llm_result = self._llm_provider.generate_structured(
            self._build_prompt(
                planning,
                revision_request=input_data.get("revision_request"),
                previous_output=input_data.get("previous_output"),
            ),
            ScriptGenerationResult,
            self._provider,
            self._model,
        )
        result = ScriptGenerationResult.model_validate(llm_result.data)
        self._validate_against_planning(result, planning)
        return ScriptGenerationOutput(data=result, metadata=llm_result.metadata)

    def _validate_against_planning(
        self, result: ScriptGenerationResult, planning: ContentPlanningResult
    ) -> None:
        expected_ids = [scene.scene_id for scene in planning.scenes]
        actual_ids = [scene.planning_scene_id for scene in result.scenes]
        if actual_ids != expected_ids:
            raise ValueError("Script scenes must reference every planning scene once and in order")
        durations = {scene.scene_id: scene.duration_seconds for scene in planning.scenes}
        for scene in result.scenes:
            limit = durations[scene.planning_scene_id]
            if scene.narration is not None and self._tts_estimator.estimate_seconds(scene.narration) > limit:
                raise ValueError(f"Narration exceeds scene duration: {scene.planning_scene_id}")
            if scene.subtitle is not None and self._subtitle_estimator.estimate_seconds(scene.subtitle) > limit:
                raise ValueError(f"Subtitle exceeds scene duration: {scene.planning_scene_id}")

    @staticmethod
    def _build_prompt(
        planning: ContentPlanningResult,
        revision_request: object | None = None,
        previous_output: object | None = None,
    ) -> str:
        prompt = (
            "Write one script scene for each planning scene, preserving IDs and order. "
            "Narration and subtitles must fit each scene duration. Planning: "
            + json.dumps(planning.model_dump(mode="json"), ensure_ascii=False)
        )
        if revision_request is not None:
            prompt += " Revision request: " + str(revision_request)
        if previous_output is not None:
            prompt += " Previous result to revise: " + json.dumps(
                previous_output, ensure_ascii=False
            )
        return prompt
