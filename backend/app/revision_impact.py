import enum
import json

from pydantic import BaseModel

from app.llm_provider import LLMMetadata, LLMProvider, LLMProviderType


class CoreNodeKey(str, enum.Enum):
    INPUT_ANALYSIS = "input_analysis"
    CONTENT_PLANNING = "content_planning"
    SCRIPT_GENERATION = "script_generation"
    VIDEO_GENERATION = "video_generation"


class RevisionImpactResult(BaseModel):
    target_node: CoreNodeKey


class RevisionImpactOutput(BaseModel):
    data: RevisionImpactResult
    metadata: LLMMetadata


class RevisionImpactService:
    def __init__(
        self, llm_provider: LLMProvider, provider: LLMProviderType, model: str
    ) -> None:
        self._llm_provider = llm_provider
        self._provider = provider
        self._model = model

    def determine(
        self, current_node: CoreNodeKey, revision_request: str
    ) -> RevisionImpactOutput:
        if not revision_request.strip():
            raise ValueError("revision_request must not be blank")
        result = self._llm_provider.generate_structured(
            self._build_prompt(current_node, revision_request),
            RevisionImpactResult,
            self._provider,
            self._model,
        )
        return RevisionImpactOutput(
            data=RevisionImpactResult.model_validate(result.data),
            metadata=result.metadata,
        )

    @staticmethod
    def _build_prompt(current_node: CoreNodeKey, revision_request: str) -> str:
        return (
            "Choose exactly one workflow target_node for this revision request. "
            "Allowed values are input_analysis, content_planning, script_generation, "
            "and video_generation. current_node is only a hint, not a constraint. Input: "
            + json.dumps(
                {
                    "current_node": current_node.value,
                    "revision_request": revision_request,
                },
                ensure_ascii=False,
            )
        )
