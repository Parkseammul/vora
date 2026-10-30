"""V3 node runtime; only CONTENT_PLAN currently uses a provider-backed LangChain path."""

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.image_provider import ImageProvider
from app.models import (
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    NodeExecution,
    NodeExecutionStatus,
    PromptVersion,
    RulesVersion,
    WorkflowExecution,
)
from app.node_executors import NodeExecutionResult, NodeExecutorConfigurationError
from app.storage import StorageProvider


class ContentPlanOutput(BaseModel):
    """The structured response required from the CONTENT_PLAN chat model."""

    core_message: str
    slide_structure: list[str] = Field(min_length=5, max_length=5)

    @field_validator("slide_structure")
    @classmethod
    def validate_slide_structure(cls, value: list[str]) -> list[str]:
        if any(not purpose.strip() for purpose in value):
            raise ValueError("slide_structure items must not be blank")
        return value


class SlideText(BaseModel):
    slide_no: int = Field(ge=1, le=5)
    text: str

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("slide text must not be blank")
        return value


class TextGenerateOutput(BaseModel):
    slides: list[SlideText] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_slide_numbers(self) -> "TextGenerateOutput":
        if [slide.slide_no for slide in self.slides] != [1, 2, 3, 4, 5]:
            raise ValueError("slides must contain slide_no 1 through 5 in order")
        return self


class TextCheckOutput(BaseModel):
    passed: bool
    violations: list[str]
    feedback: str | None


class ImageCheckOutput(BaseModel):
    """Structured visual QA result for the generated slide assets."""

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
        image_provider: ImageProvider | None = None,
        image_model_name: str | None = None,
        storage: StorageProvider | None = None,
        image_check_model: BaseChatModel | Runnable[dict[str, Any], ImageCheckOutput] | None = None,
        image_check_model_name: str | None = None,
        image_check_model_factory: Callable[[], BaseChatModel] | None = None,
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
        self._image_provider = image_provider
        self._image_model_name = image_model_name
        self._storage = storage
        self._image_check_model = image_check_model
        self._image_check_model_name = image_check_model_name
        self._image_check_model_factory = image_check_model_factory

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
        if node_key == "IMAGE_CHECK":
            return self._image_check(dict(input_data), workflow_execution_id)
        if node_key == "PUBLICATION":
            return self._publication_prepare(
                dict(input_data), workflow_execution_id, node_execution_id
            )
        if node_key == "HUMAN_APPROVAL":
            return {"status": "WAITING_APPROVAL"}
        raise ValueError(f"Unsupported V3 node: {node_key}")

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
        slides = TextGenerateOutput.model_validate({"slides": input_data.get("slides")}).slides
        if self._session is None or workflow_execution_id is None or node_execution_id is None:
            raise NodeExecutorConfigurationError("IMAGE_GENERATE requires session and workflow execution")
        if self._image_provider is None or self._storage is None:
            raise NodeExecutorConfigurationError("IMAGE_GENERATE provider and storage are not configured")
        session = self._session
        image_provider = self._image_provider
        storage = self._storage
        self._lock_image_generation_scope(workflow_execution_id, node_execution_id)
        slide_payload = [slide.model_dump(mode="json") for slide in slides]
        snapshot = session.scalar(
            select(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id
            )
        )
        if snapshot is None or snapshot.source_asset_id is None:
            raise NodeExecutorConfigurationError("IMAGE_GENERATE input source asset does not exist")
        source_asset = session.get(FileAsset, snapshot.source_asset_id)
        if source_asset is None:
            raise NodeExecutorConfigurationError("IMAGE_GENERATE source asset does not exist")
        requested_slides = input_data.get("image_slide_numbers")
        if requested_slides is not None:
            if not isinstance(requested_slides, list) or sorted(set(requested_slides)) != requested_slides or any(
                not isinstance(slide_no, int) or slide_no not in range(1, 6)
                for slide_no in requested_slides
            ):
                raise ValueError("image_slide_numbers must contain unique values from 1 through 5")
            if not requested_slides:
                raise ValueError("image_slide_numbers must not be empty")
            selected_slides = set(requested_slides)
        else:
            selected_slides = set(range(1, 6))

        prior_assets: dict[int, FileAsset] = {}
        prior_signatures: dict[str, str] = {}
        if requested_slides is not None and selected_slides != set(range(1, 6)):
            current_node = session.get(NodeExecution, node_execution_id)
            prior_node = session.scalar(
                select(NodeExecution)
                .where(
                    NodeExecution.workflow_execution_id == workflow_execution_id,
                    NodeExecution.node_type == "IMAGE_GENERATE",
                    NodeExecution.revision_no < (current_node.revision_no if current_node else 0),
                    NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
                )
                .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
                .limit(1)
            )
            if prior_node is None or not isinstance(prior_node.output_data, dict):
                raise ValueError("Selective IMAGE revision requires a successful prior IMAGE_GENERATE")
            prior_asset_ids = prior_node.output_data.get("asset_ids")
            prior_signature_data = prior_node.output_data.get("image_generation_signatures", {})
            if (
                not isinstance(prior_asset_ids, list)
                or len(prior_asset_ids) != 5
                or not isinstance(prior_signature_data, dict)
            ):
                raise ValueError("Prior IMAGE_GENERATE output is incomplete for selective reuse")
            prior_rows = list(session.scalars(select(FileAsset).where(FileAsset.id.in_(prior_asset_ids))))
            prior_assets = {asset.slide_no: asset for asset in prior_rows if asset.slide_no is not None}
            successful_creator_ids = set(
                session.scalars(
                    select(NodeExecution.id).where(
                        NodeExecution.workflow_execution_id == workflow_execution_id,
                        NodeExecution.node_type == "IMAGE_GENERATE",
                        NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
                        NodeExecution.revision_no <= prior_node.revision_no,
                    )
                )
            )
            if set(prior_assets) != set(range(1, 6)) or any(
                asset.workflow_execution_id != workflow_execution_id
                or asset.created_by_node_execution_id not in successful_creator_ids
                or asset.asset_type is not AssetType.IMAGE
                for asset in prior_assets.values()
            ):
                raise ValueError("Prior IMAGE_GENERATE assets are invalid for selective reuse")
            prior_signatures = {
                str(slide_no): signature
                for slide_no, signature in prior_signature_data.items()
                if isinstance(signature, str)
            }

        asset_ids: list[int] = []
        with tempfile.TemporaryDirectory(prefix="vora-image-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_image = temporary_root / source_asset.file_name
            storage.materialize_private(source_asset.storage_key, source_image)
            source_digest = hashlib.sha256(source_image.read_bytes()).hexdigest()
            previous_output = self._image_generation_progress(node_execution_id)
            previous_asset_ids = previous_output.get("asset_ids", [])
            previous_signatures = previous_output.get("image_generation_signatures", {})
            if not isinstance(previous_asset_ids, list):
                previous_asset_ids = []
            if not isinstance(previous_signatures, dict):
                previous_signatures = {}
            previous_signatures = {**prior_signatures, **previous_signatures}
            for slide in slide_payload:
                slide_no = slide["slide_no"]
                prompt = f"Use the reference image as visual inspiration. Create an image for slide {slide_no}: {slide['text']}"
                feedback = input_data.get("revision_request")
                if slide_no in selected_slides and isinstance(feedback, str) and feedback.strip():
                    prompt += f" Revision feedback for slide {slide_no}: {feedback.strip()}"

                if slide_no not in selected_slides:
                    prior_asset = prior_assets.get(slide_no)
                    if prior_asset is None or not self._valid_stored_image(
                        prior_asset, temporary_root / f"reused-{slide_no}.png"
                    ):
                        raise ValueError(
                            f"Cannot reuse prior IMAGE_GENERATE asset for slide {slide_no}"
                        )
                    asset_ids.append(prior_asset.id)
                    continue

                signature = self._image_generation_signature(
                    prompt=prompt,
                    source_asset=source_asset,
                    source_digest=source_digest,
                )
                file_name = f"slide-{slide_no}.png"
                storage_key = (
                    f"workflows/{workflow_execution_id}/image-generate/"
                    f"node-{node_execution_id}/{file_name}"
                )
                asset = session.scalar(
                    select(FileAsset)
                    .where(
                        FileAsset.storage_key == storage_key,
                        FileAsset.workflow_execution_id == workflow_execution_id,
                        FileAsset.created_by_node_execution_id == node_execution_id,
                        FileAsset.asset_type == AssetType.IMAGE,
                        FileAsset.slide_no == slide_no,
                    )
                    .with_for_update()
                )
                if (
                    str(slide_no) in previous_signatures
                    and previous_signatures[str(slide_no)] == signature
                    and asset is not None
                    and self._valid_stored_image(asset, temporary_root / f"existing-{slide_no}.png")
                ):
                    asset_ids.append(asset.id)
                    continue

                image_bytes = image_provider.generate(prompt=prompt, source_image=source_image)
                if not image_bytes:
                    raise RuntimeError("Image provider returned an empty image")
                output_file = temporary_root / file_name
                output_file.write_bytes(image_bytes)
                storage.upload_private(storage_key, output_file, "image/png")
                if asset is None:
                    asset = FileAsset(
                        workflow_execution_id=workflow_execution_id,
                        created_by_node_execution_id=node_execution_id,
                        asset_type=AssetType.IMAGE,
                        storage_key=storage_key,
                        file_name=file_name,
                        mime_type="image/png",
                        file_size=len(image_bytes),
                        slide_no=slide_no,
                    )
                    session.add(asset)
                else:
                    asset.file_size = len(image_bytes)
                    asset.mime_type = "image/png"
                session.flush()
                asset_ids.append(asset.id)
                previous_signatures[str(slide_no)] = signature
                previous_asset_ids = asset_ids.copy()
                progress_node = session.get(NodeExecution, node_execution_id)
                if progress_node is None:
                    raise ValueError("IMAGE_GENERATE node execution does not exist")
                progress_node.output_data = {
                    "slides": slide_payload,
                    "asset_ids": previous_asset_ids,
                    "image_generation_signatures": previous_signatures,
                }
                session.flush()
        return NodeExecutionResult(
            output_data={
                "slides": slide_payload,
                "asset_ids": asset_ids,
                "image_generation_signatures": previous_signatures,
            },
            attempt_metadata={"runtime": "v3-image-provider"},
            ai_company="OPENAI",
            model=self._image_model_name,
        )

    def _lock_image_generation_scope(self, workflow_execution_id: int, node_execution_id: int) -> None:
        """Serialize duplicate deliveries for one workflow/node before provider calls."""
        if self._session is None:
            raise NodeExecutorConfigurationError("IMAGE_GENERATE requires a database session")
        session = self._session
        workflow = session.scalar(
            select(WorkflowExecution)
            .where(WorkflowExecution.id == workflow_execution_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        node = session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.id == node_execution_id,
                NodeExecution.workflow_execution_id == workflow_execution_id,
                NodeExecution.node_type == "IMAGE_GENERATE",
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if workflow is None or node is None:
            raise ValueError("IMAGE_GENERATE execution scope does not exist")

    def _image_generation_progress(self, node_execution_id: int) -> dict[str, Any]:
        if self._session is None:
            return {}
        node = self._session.get(NodeExecution, node_execution_id)
        if node is None or not isinstance(node.output_data, dict):
            return {}
        return node.output_data

    def _image_generation_signature(
        self,
        *,
        prompt: str,
        source_asset: FileAsset,
        source_digest: str,
    ) -> str:
        provider_settings = getattr(self._image_provider, "generation_settings", None)
        if not isinstance(provider_settings, dict):
            provider_settings = {"output_format": "png"}
        signature_data = {
            "prompt": prompt,
            "source_asset_id": source_asset.id,
            "source_storage_key": source_asset.storage_key,
            "source_sha256": source_digest,
            "model": self._image_model_name,
            "settings": provider_settings,
        }
        serialized = json.dumps(signature_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def _valid_stored_image(self, asset: FileAsset, destination: Path) -> bool:
        if asset.mime_type != "image/png":
            return False
        if self._storage is None:
            return False
        try:
            self._storage.materialize_private(asset.storage_key, destination)
            image_bytes = destination.read_bytes()
        except (FileNotFoundError, OSError, ValueError):
            return False
        return (
            image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            and len(image_bytes) == asset.file_size
            and len(image_bytes) > 8
        )

    def _image_check(
        self, input_data: dict[str, Any], workflow_execution_id: int | None
    ) -> NodeExecutionResult:
        if self._session is None or workflow_execution_id is None:
            raise NodeExecutorConfigurationError("IMAGE_CHECK requires session and workflow execution")
        if self._storage is None:
            raise NodeExecutorConfigurationError("IMAGE_CHECK storage is not configured")
        image_check_model = self._image_check_model
        if image_check_model is None and self._image_check_model_factory is not None:
            image_check_model = self._image_check_model_factory()
        if image_check_model is None:
            raise NodeExecutorConfigurationError("IMAGE_CHECK vision model is not configured")
        prompt_version = self._session.scalar(
            select(PromptVersion)
            .where(PromptVersion.node_type == "IMAGE_CHECK")
            .order_by(PromptVersion.version.desc())
            .limit(1)
        )
        if prompt_version is None:
            raise NodeExecutorConfigurationError("No PromptVersion is configured for IMAGE_CHECK")
        snapshot = self._session.scalar(
            select(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id
            )
        )
        if snapshot is None or snapshot.source_asset_id is None:
            raise NodeExecutorConfigurationError("IMAGE_CHECK input source asset does not exist")
        rules = self._session.get(RulesVersion, snapshot.rules_version_id)
        if rules is None:
            raise NodeExecutorConfigurationError("IMAGE_CHECK rules version does not exist")
        asset_ids = input_data.get("asset_ids", [])
        if (
            not isinstance(asset_ids, list)
            or len(asset_ids) != 5
            or len(set(asset_ids)) != 5
            or not all(isinstance(asset_id, int) for asset_id in asset_ids)
        ):
            raise ValueError("IMAGE_CHECK asset_ids must contain five unique integers")
        assets = list(self._session.scalars(select(FileAsset).where(FileAsset.id.in_(asset_ids))))
        asset_by_id = {asset.id: asset for asset in assets}
        if len(asset_by_id) != len(asset_ids):
            raise ValueError("IMAGE_CHECK generated image asset does not exist")
        generated_assets = [asset_by_id[asset_id] for asset_id in asset_ids]
        if any(
            asset.workflow_execution_id != workflow_execution_id or asset.asset_type is not AssetType.IMAGE
            for asset in generated_assets
        ):
            raise ValueError("IMAGE_CHECK asset does not belong to this workflow execution")
        source_asset = self._session.get(FileAsset, snapshot.source_asset_id)
        if source_asset is None:
            raise NodeExecutorConfigurationError("IMAGE_CHECK source asset does not exist")

        with tempfile.TemporaryDirectory(prefix="vora-image-check-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            reference_data = self._read_asset_data(source_asset, temporary_root / "reference")
            generated_data = [
                self._read_asset_data(asset, temporary_root / f"generated-{asset.id}")
                for asset in generated_assets
            ]
        prompt = PromptTemplate.from_template(prompt_version.prompt_template)
        chain_input = {
            "slides": json.dumps(input_data.get("slides", []), ensure_ascii=False),
            "rules_data": json.dumps(rules.rules_data, ensure_ascii=False),
            "input_data": json.dumps(input_data, ensure_ascii=False),
            "asset_ids": json.dumps(asset_ids),
        }
        rendered_prompt = prompt.format(**chain_input)
        if isinstance(image_check_model, BaseChatModel):
            structured_model = cast(
                Runnable[Any, Any], image_check_model.with_structured_output(ImageCheckOutput)
            )
            content: list[str | dict[Any, Any]] = [{"type": "text", "text": rendered_prompt}]
            content.append(self._vision_image_part(reference_data, source_asset.mime_type))
            content.extend(
                self._vision_image_part(image_data, asset.mime_type)
                for image_data, asset in zip(generated_data, generated_assets, strict=True)
            )
            result = ImageCheckOutput.model_validate(
                structured_model.invoke([HumanMessage(content=content)])
            )
        else:
            result = ImageCheckOutput.model_validate(
                cast(Runnable[Any, Any], image_check_model).invoke(
                    {**chain_input, "reference_image": reference_data, "generated_images": generated_data}
                )
            )
        return NodeExecutionResult(
            output_data={"asset_ids": asset_ids, **result.model_dump(mode="json")},
            attempt_metadata={"runtime": "v3-langchain-vision", "prompt_version": prompt_version.version},
            ai_company="OPENAI",
            model=self._image_check_model_name,
            prompt_version_id=prompt_version.id,
        )

    def _publication_prepare(
        self,
        input_data: dict[str, Any],
        workflow_execution_id: int | None,
        node_execution_id: int | None,
    ) -> NodeExecutionResult:
        """Persist the approved five-card set; external publishing remains separate."""
        if self._session is None or workflow_execution_id is None or node_execution_id is None:
            raise NodeExecutorConfigurationError("PUBLICATION requires session and workflow execution")
        asset_ids = input_data.get("asset_ids")
        if not isinstance(asset_ids, list) or len(asset_ids) != 5 or not all(
            isinstance(asset_id, int) for asset_id in asset_ids
        ):
            raise ValueError("PUBLICATION requires exactly five image asset_ids")
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("PUBLICATION asset_ids must not contain duplicates")
        latest_image_node = self._session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == workflow_execution_id,
                NodeExecution.node_type == "IMAGE_GENERATE",
                NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
            .limit(1)
        )
        if latest_image_node is None or not isinstance(latest_image_node.output_data, dict):
            raise ValueError("PUBLICATION requires a successful IMAGE_GENERATE result")
        generated_ids = latest_image_node.output_data.get("asset_ids")
        if generated_ids != asset_ids:
            raise ValueError("PUBLICATION asset_ids do not match the latest generated images")
        successful_image_node_ids = set(
            self._session.scalars(
                select(NodeExecution.id).where(
                    NodeExecution.workflow_execution_id == workflow_execution_id,
                    NodeExecution.node_type == "IMAGE_GENERATE",
                    NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
                )
            )
        )
        assets = list(self._session.scalars(select(FileAsset).where(FileAsset.id.in_(asset_ids))))
        by_id = {asset.id: asset for asset in assets}
        if len(by_id) != 5 or any(
            asset.workflow_execution_id != workflow_execution_id
            or asset.asset_type is not AssetType.IMAGE
            or asset.created_by_node_execution_id not in successful_image_node_ids
            for asset in by_id.values()
        ):
            raise ValueError("PUBLICATION asset does not belong to a successful image generation")
        ordered_assets = sorted(by_id.values(), key=lambda asset: asset.slide_no or 0)
        if [asset.slide_no for asset in ordered_assets] != [1, 2, 3, 4, 5]:
            raise ValueError("PUBLICATION images must have unique slide_no values 1 through 5")
        return NodeExecutionResult(
            output_data={"asset_ids": [asset.id for asset in ordered_assets]},
            attempt_metadata={"runtime": "v3-publication-prepare"},
        )

    def _read_asset_data(self, asset: FileAsset, destination: Path) -> str:
        if self._storage is None:
            raise NodeExecutorConfigurationError("IMAGE_CHECK storage is not configured")
        self._storage.materialize_private(asset.storage_key, destination)
        if not destination.is_file():
            raise FileNotFoundError("IMAGE_CHECK storage provider did not materialize the image")
        import base64

        return base64.b64encode(destination.read_bytes()).decode("ascii")

    @staticmethod
    def _vision_image_part(image_data: str, mime_type: str) -> dict[str, Any]:
        return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}}
