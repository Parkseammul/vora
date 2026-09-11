from collections.abc import Sequence
from typing import TypeVar

import pytest
from pydantic import BaseModel

from app.content_planning import ContentPlanningService
from app.llm_provider import LLMMetadata, LLMProviderType, LLMResult
from app.node_executors import (
    NodeExecutionResult,
    NodeExecutorConfigurationError,
    RuleBasedNodeExecutor,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class FakeProvider:
    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        provider: LLMProviderType,
        model: str,
        images: Sequence[str] = (),
    ) -> LLMResult:
        return LLMResult(
            data=response_model.model_validate(
                {
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
                            "duration_seconds": 30,
                            "source_asset_id": None,
                            "visual_direction": "wide",
                            "transition_to_next": None,
                        }
                    ],
                }
            ),
            metadata=LLMMetadata(
                provider=provider,
                model=model,
                input_tokens=10,
                output_tokens=20,
                cost=0.01,
            ),
        )


def test_ai_node_returns_pure_output_and_separate_attempt_metadata() -> None:
    executor = RuleBasedNodeExecutor(
        content_planning_service=ContentPlanningService(
            FakeProvider(), LLMProviderType.OPENAI, "model"
        )
    )
    result = executor.execute(
        "content_planning",
        {
            "content_goal": "GENERAL_SHORTFORM",
            "target_audience": {"age_group": "ALL", "gender": "ALL", "audience_group": "all"},
            "duration_seconds": 30,
            "tone": "bright",
            "source_asset_ids": [],
        },
        workflow_execution_id=123,
    )

    assert isinstance(result, NodeExecutionResult)
    assert "data" not in result.output_data
    assert "metadata" not in result.output_data
    assert result.output_data["scenes"][0]["scene_id"]
    assert result.attempt_metadata == {
        "provider": "OPENAI",
        "model": "model",
        "input_tokens": 10,
        "output_tokens": 20,
        "cost": 0.01,
    }


def test_content_planning_requires_execution_context_before_service_call() -> None:
    executor = RuleBasedNodeExecutor()

    with pytest.raises(NodeExecutorConfigurationError, match="requires workflow_execution_id"):
        executor.execute("content_planning", {})


@pytest.mark.parametrize("node_key", ["content_planning", "script_generation", "video_generation"])
def test_production_nodes_do_not_fall_back_to_fake_results(node_key: str) -> None:
    executor = RuleBasedNodeExecutor()

    with pytest.raises(NodeExecutorConfigurationError, match="not configured|No production executor"):
        executor.execute(node_key, {}, workflow_execution_id=1)
