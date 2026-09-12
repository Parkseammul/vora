from collections.abc import Sequence
from typing import TypeVar

import pytest
from pydantic import BaseModel

from app.llm_provider import LLMMetadata, LLMProviderType, LLMResult
from app.revision_impact import CoreNodeKey, RevisionImpactService

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class FakeProvider:
    def __init__(self, target_node: str) -> None:
        self.target_node = target_node
        self.prompt = ""

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
            data=response_model.model_validate({"target_node": self.target_node}),
            metadata=LLMMetadata(provider=provider, model=model),
        )


def test_revision_impact_selects_core_node_without_forcing_current_node() -> None:
    provider = FakeProvider("script_generation")
    service = RevisionImpactService(provider, LLMProviderType.OPENAI, "impact-model")

    output = service.determine(CoreNodeKey.CONTENT_PLANNING, "첫 문장을 더 강하게 바꿔줘")

    assert output.data.target_node is CoreNodeKey.SCRIPT_GENERATION
    assert '"current_node": "content_planning"' in provider.prompt


def test_revision_impact_rejects_invalid_target_and_blank_request() -> None:
    invalid = RevisionImpactService(
        FakeProvider("invalid_node"), LLMProviderType.OPENAI, "impact-model"
    )
    with pytest.raises(ValueError):
        invalid.determine(CoreNodeKey.SCRIPT_GENERATION, "수정해줘")

    valid = RevisionImpactService(
        FakeProvider("script_generation"), LLMProviderType.OPENAI, "impact-model"
    )
    with pytest.raises(ValueError, match="must not be blank"):
        valid.determine(CoreNodeKey.SCRIPT_GENERATION, "  ")
