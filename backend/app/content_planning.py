import enum
import json
from decimal import Decimal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.input_analysis import InputAnalysisResult
from app.llm_provider import LLMMetadata, LLMProvider, LLMProviderType
from app.models import FileAsset


class TransitionType(str, enum.Enum):
    CUT = "CUT"
    FADE = "FADE"
    ZOOM = "ZOOM"


def _validate_duration(value: float) -> float:
    decimal = Decimal(str(value))
    if decimal <= 0 or decimal != decimal.quantize(Decimal("0.1")):
        raise ValueError("duration_seconds must be positive with at most one decimal place")
    return value


class ScenePlanDraft(BaseModel):
    purpose: str
    main_objects: list[str]
    description: str
    duration_seconds: float
    source_asset_id: int | None = None
    visual_direction: str
    transition_to_next: TransitionType | None = None

    _duration = field_validator("duration_seconds")(_validate_duration)


class ContentPlanningDraft(BaseModel):
    concept: str
    hook: str
    key_message: str
    cta: str
    visual_style: str
    bgm_direction: str
    scenes: list[ScenePlanDraft] = Field(min_length=1)


class ScenePlan(ScenePlanDraft):
    scene_id: str


class ContentPlanningResult(BaseModel):
    concept: str
    hook: str
    key_message: str
    cta: str
    visual_style: str
    bgm_direction: str
    scenes: list[ScenePlan] = Field(min_length=1)

    @model_validator(mode="after")
    def transitions_are_valid(self) -> "ContentPlanningResult":
        if any(scene.transition_to_next is None for scene in self.scenes[:-1]):
            raise ValueError("Only the final scene may omit transition_to_next")
        if self.scenes[-1].transition_to_next is not None:
            raise ValueError("The final scene transition_to_next must be null")
        return self


class ContentPlanningOutput(BaseModel):
    data: ContentPlanningResult
    metadata: LLMMetadata


class ContentPlanningService:
    def __init__(
        self,
        llm_provider: LLMProvider,
        provider: LLMProviderType,
        model: str,
        session: Session | None = None,
    ) -> None:
        self._llm_provider = llm_provider
        self._provider = provider
        self._model = model
        self._session = session

    def generate(
        self, input_data: dict[str, object], workflow_execution_id: int | None = None
    ) -> ContentPlanningOutput:
        analysis = InputAnalysisResult.model_validate(input_data)
        self._validate_assets(analysis.source_asset_ids, workflow_execution_id)
        prompt = self._build_prompt(analysis)
        llm_result = self._llm_provider.generate_structured(
            prompt,
            ContentPlanningDraft,
            self._provider,
            self._model,
            images=[str(asset_id) for asset_id in analysis.source_asset_ids],
        )
        draft = ContentPlanningDraft.model_validate(llm_result.data)
        result = ContentPlanningResult(
            **draft.model_dump(exclude={"scenes"}),
            scenes=[
                ScenePlan(scene_id=str(uuid4()), **scene.model_dump()) for scene in draft.scenes
            ],
        )
        self._validate_business_rules(result, analysis)
        return ContentPlanningOutput(data=result, metadata=llm_result.metadata)

    def _validate_assets(self, asset_ids: list[int], workflow_execution_id: int | None) -> None:
        if not asset_ids:
            return
        if self._session is None or workflow_execution_id is None:
            raise ValueError("Asset validation requires a session and workflow execution id")
        assets = list(self._session.scalars(select(FileAsset).where(FileAsset.id.in_(asset_ids))))
        by_id = {asset.id: asset for asset in assets}
        for asset_id in asset_ids:
            asset = by_id.get(asset_id)
            if asset is None:
                raise ValueError(f"FileAsset does not exist: {asset_id}")
            if asset.workflow_execution_id != workflow_execution_id:
                raise ValueError(f"FileAsset does not belong to workflow execution: {asset_id}")

    @staticmethod
    def _validate_business_rules(
        result: ContentPlanningResult, analysis: InputAnalysisResult
    ) -> None:
        used_ids = [scene.source_asset_id for scene in result.scenes if scene.source_asset_id is not None]
        if used_ids != analysis.source_asset_ids:
            raise ValueError("Planning scenes must use every input asset exactly once and in order")
        if len(result.scenes) < len(analysis.source_asset_ids):
            raise ValueError("Scene count must be at least the input image count")
        total = sum(Decimal(str(scene.duration_seconds)) for scene in result.scenes)
        if total != Decimal(str(analysis.duration_seconds)):
            raise ValueError("Scene duration sum must equal the analyzed duration")

    @staticmethod
    def _build_prompt(analysis: InputAnalysisResult) -> str:
        return (
            "Create a short-form content plan. Preserve source_asset_ids in their given order, "
            "use each exactly once, and make scene durations sum to duration_seconds. "
            "Do not provide scene_id; the backend creates it. Input: "
            + json.dumps(analysis.model_dump(mode="json"), ensure_ascii=False)
        )
