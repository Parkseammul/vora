"""V3 node runtime; only CONTENT_PLAN currently uses a provider-backed LangChain path."""

import json
from collections.abc import Callable, Mapping
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    PromptVersion,
    RulesVersion,
)
from app.node_executors import NodeExecutionResult, NodeExecutorConfigurationError


class ContentPlanOutput(BaseModel):
    """The structured response required from the CONTENT_PLAN chat model."""

    core_message: str
    slide_structure: list[str] = Field(min_length=1)


class SlideText(BaseModel):
    slide_no: int
    text: str


class TextGenerateOutput(BaseModel):
    slides: list[SlideText] = Field(min_length=1)


class TextCheckOutput(BaseModel):
    passed: bool
    violations: list[str]
    feedback: str | None


class V3NodeExecutor:
    """Runs V3 nodes, with injected real ChatModels for connected AI nodes."""

    def __init__(
        self,
        session: Session | None = None,
        fail_node: str | None = None,
        content_plan_model: BaseChatModel | Runnable[dict[str, Any], ContentPlanOutput] | None = None,
        content_plan_model_name: str | None = None,
        content_plan_model_factory: Callable[[], BaseChatModel] | None = None,
        text_generate_model: BaseChatModel | Runnable[dict[str, Any], TextGenerateOutput] | None = None,
        text_generate_model_name: str | None = None,
        text_generate_model_factory: Callable[[], BaseChatModel] | None = None,
        text_check_model: BaseChatModel | Runnable[dict[str, Any], TextCheckOutput] | None = None,
        text_check_model_name: str | None = None,
        text_check_model_factory: Callable[[], BaseChatModel] | None = None,
    ) -> None:
        self._session = session
        self._fail_node = fail_node
        self._content_plan_model = content_plan_model
        self._content_plan_model_name = content_plan_model_name
        self._content_plan_model_factory = content_plan_model_factory
        self._text_generate_model = text_generate_model
        self._text_generate_model_name = text_generate_model_name
        self._text_generate_model_factory = text_generate_model_factory
        self._text_check_model = text_check_model
        self._text_check_model_name = text_check_model_name
        self._text_check_model_factory = text_check_model_factory
        self._runnables = {
            "IMAGE_CHECK": RunnableLambda(self._image_check),
        }

    def execute(
        self,
        node_key: str,
        input_data: Mapping[str, Any],
        *,
        workflow_execution_id: int | None = None,
        node_execution_id: int | None = None,
        video_progress_callback: Callable[[str, str], None] | None = None,
    ) -> NodeExecutionResult | dict[str, Any]:
        del video_progress_callback
        if node_key == self._fail_node:
            raise RuntimeError(f"Fake V3 executor failure for {node_key}")
        if node_key == "CONTENT_PLAN":
            return self._content_plan(dict(input_data), workflow_execution_id)
        if node_key == "TEXT_GENERATE":
            return self._text_generate(dict(input_data), workflow_execution_id)
        if node_key == "TEXT_CHECK":
            return self._text_check(dict(input_data), workflow_execution_id)
        if node_key == "IMAGE_GENERATE":
            return self._image_generate(dict(input_data), workflow_execution_id, node_execution_id)
        if node_key == "HUMAN_APPROVAL":
            return {"status": "WAITING_APPROVAL"}
        runnable = self._runnables.get(node_key)
        if runnable is None:
            raise ValueError(f"Unsupported V3 node: {node_key}")
        return NodeExecutionResult(
            output_data=runnable.invoke(dict(input_data)),
            attempt_metadata={"runtime": "v3-fake-runnable"},
        )

    def _content_plan(
        self, input_data: dict[str, Any], workflow_execution_id: int | None
    ) -> NodeExecutionResult:
        if self._session is None or workflow_execution_id is None:
            raise NodeExecutorConfigurationError("CONTENT_PLAN requires session and workflow execution")
        content_plan_model = self._content_plan_model
        if content_plan_model is None and self._content_plan_model_factory is not None:
            # Defer provider construction until the CONTENT_PLAN execution boundary.
            content_plan_model = self._content_plan_model_factory()
        if content_plan_model is None:
            raise NodeExecutorConfigurationError("CONTENT_PLAN chat model is not configured")

        prompt_version = self._session.scalar(
            select(PromptVersion)
            .where(PromptVersion.node_type == "CONTENT_PLAN")
            .order_by(PromptVersion.version.desc())
            .limit(1)
        )
        if prompt_version is None:
            raise NodeExecutorConfigurationError("No PromptVersion is configured for CONTENT_PLAN")
        snapshot = self._session.scalar(
            select(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id
            )
        )
        if snapshot is None:
            raise NodeExecutorConfigurationError("CONTENT_PLAN input snapshot does not exist")
        rules = self._session.get(RulesVersion, snapshot.rules_version_id)
        if rules is None:
            raise NodeExecutorConfigurationError("CONTENT_PLAN rules version does not exist")

        prompt = PromptTemplate.from_template(prompt_version.prompt_template)
        chain_input = {
            "request_text": snapshot.request_text,
            "marketing_info": snapshot.marketing_info,
            "theme": snapshot.theme or "",
            "rules_data": json.dumps(rules.rules_data, ensure_ascii=False),
            "input_data": json.dumps(input_data, ensure_ascii=False),
        }
        structured_model: Runnable[Any, Any]
        if isinstance(content_plan_model, BaseChatModel):
            structured_model = cast(
                Runnable[Any, Any],
                content_plan_model.with_structured_output(ContentPlanOutput),
            )
        else:
            structured_model = cast(Runnable[Any, Any], content_plan_model)
        result = ContentPlanOutput.model_validate((prompt | structured_model).invoke(chain_input))
        output_data = result.model_dump(mode="json")
        # TEXT_GENERATE already consumes slides; retain that downstream contract.
        output_data["slides"] = [
            {"slide_no": index, "purpose": purpose}
            for index, purpose in enumerate(result.slide_structure, start=1)
        ]
        return NodeExecutionResult(
            output_data=output_data,
            attempt_metadata={"runtime": "v3-langchain", "prompt_version": prompt_version.version},
            ai_company="OPENAI",
            model=self._content_plan_model_name,
            prompt_version_id=prompt_version.id,
        )

    def _text_generate(
        self, input_data: dict[str, Any], workflow_execution_id: int | None
    ) -> NodeExecutionResult:
        if self._session is None or workflow_execution_id is None:
            raise NodeExecutorConfigurationError("TEXT_GENERATE requires session and workflow execution")
        text_generate_model = self._text_generate_model
        if text_generate_model is None and self._text_generate_model_factory is not None:
            # Match CONTENT_PLAN: provider setup belongs at the AI execution boundary.
            text_generate_model = self._text_generate_model_factory()
        if text_generate_model is None:
            raise NodeExecutorConfigurationError("TEXT_GENERATE chat model is not configured")

        prompt_version = self._session.scalar(
            select(PromptVersion)
            .where(PromptVersion.node_type == "TEXT_GENERATE")
            .order_by(PromptVersion.version.desc())
            .limit(1)
        )
        if prompt_version is None:
            raise NodeExecutorConfigurationError("No PromptVersion is configured for TEXT_GENERATE")
        snapshot = self._session.scalar(
            select(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id
            )
        )
        if snapshot is None:
            raise NodeExecutorConfigurationError("TEXT_GENERATE input snapshot does not exist")
        rules = self._session.get(RulesVersion, snapshot.rules_version_id)
        if rules is None:
            raise NodeExecutorConfigurationError("TEXT_GENERATE rules version does not exist")

        prompt = PromptTemplate.from_template(prompt_version.prompt_template)
        chain_input = {
            "core_message": input_data.get("core_message", ""),
            "slide_structure": json.dumps(input_data.get("slide_structure", []), ensure_ascii=False),
            "rules_data": json.dumps(rules.rules_data, ensure_ascii=False),
            "input_data": json.dumps(input_data, ensure_ascii=False),
        }
        structured_model: Runnable[Any, Any]
        if isinstance(text_generate_model, BaseChatModel):
            structured_model = cast(
                Runnable[Any, Any],
                text_generate_model.with_structured_output(TextGenerateOutput),
            )
        else:
            structured_model = cast(Runnable[Any, Any], text_generate_model)
        result = TextGenerateOutput.model_validate((prompt | structured_model).invoke(chain_input))
        return NodeExecutionResult(
            output_data=result.model_dump(mode="json"),
            attempt_metadata={"runtime": "v3-langchain", "prompt_version": prompt_version.version},
            ai_company="OPENAI",
            model=self._text_generate_model_name,
            prompt_version_id=prompt_version.id,
        )

    def _text_check(
        self, input_data: dict[str, Any], workflow_execution_id: int | None
    ) -> NodeExecutionResult:
        if self._session is None or workflow_execution_id is None:
            raise NodeExecutorConfigurationError("TEXT_CHECK requires session and workflow execution")
        text_check_model = self._text_check_model
        if text_check_model is None and self._text_check_model_factory is not None:
            # Model construction is intentionally deferred until the AI node executes.
            text_check_model = self._text_check_model_factory()
        if text_check_model is None:
            raise NodeExecutorConfigurationError("TEXT_CHECK chat model is not configured")

        prompt_version = self._session.scalar(
            select(PromptVersion)
            .where(PromptVersion.node_type == "TEXT_CHECK")
            .order_by(PromptVersion.version.desc())
            .limit(1)
        )
        if prompt_version is None:
            raise NodeExecutorConfigurationError("No PromptVersion is configured for TEXT_CHECK")
        snapshot = self._session.scalar(
            select(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id
            )
        )
        if snapshot is None:
            raise NodeExecutorConfigurationError("TEXT_CHECK input snapshot does not exist")
        rules = self._session.get(RulesVersion, snapshot.rules_version_id)
        if rules is None:
            raise NodeExecutorConfigurationError("TEXT_CHECK rules version does not exist")

        prompt = PromptTemplate.from_template(prompt_version.prompt_template)
        chain_input = {
            "slides": json.dumps(input_data.get("slides", []), ensure_ascii=False),
            "rules_data": json.dumps(rules.rules_data, ensure_ascii=False),
            "input_data": json.dumps(input_data, ensure_ascii=False),
        }
        structured_model: Runnable[Any, Any]
        if isinstance(text_check_model, BaseChatModel):
            structured_model = cast(
                Runnable[Any, Any], text_check_model.with_structured_output(TextCheckOutput)
            )
        else:
            structured_model = cast(Runnable[Any, Any], text_check_model)
        result = TextCheckOutput.model_validate((prompt | structured_model).invoke(chain_input))
        return NodeExecutionResult(
            output_data={"slides": input_data["slides"], **result.model_dump(mode="json")},
            attempt_metadata={"runtime": "v3-langchain", "prompt_version": prompt_version.version},
            ai_company="OPENAI",
            model=self._text_check_model_name,
            prompt_version_id=prompt_version.id,
        )

    def _image_generate(
        self,
        input_data: dict[str, Any],
        workflow_execution_id: int | None,
        node_execution_id: int | None,
    ) -> NodeExecutionResult:
        if self._fail_node == "IMAGE_GENERATE":
            raise RuntimeError("Fake V3 executor failure for IMAGE_GENERATE")
        slides = input_data["slides"]
        asset_ids: list[int] = []
        if self._session is not None and workflow_execution_id is not None and node_execution_id is not None:
            for slide in slides:
                asset = FileAsset(
                    workflow_execution_id=workflow_execution_id,
                    created_by_node_execution_id=node_execution_id,
                    asset_type=AssetType.IMAGE,
                    storage_key=f"fake/{workflow_execution_id}/slide-{slide['slide_no']}.png",
                    file_name=f"slide-{slide['slide_no']}.png",
                    mime_type="image/png",
                    file_size=0,
                    slide_no=slide["slide_no"],
                )
                self._session.add(asset)
                self._session.flush()
                asset_ids.append(asset.id)
        return NodeExecutionResult(
            output_data={"slides": slides, "asset_ids": asset_ids},
            attempt_metadata={"runtime": "v3-fake-provider"},
        )

    @staticmethod
    def _image_check(input_data: dict[str, Any]) -> dict[str, Any]:
        return {"asset_ids": input_data.get("asset_ids", []), "passed": True, "violations": [], "feedback": None}
