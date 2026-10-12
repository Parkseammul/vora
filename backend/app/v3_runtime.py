"""Small V3 fake node runtime used until provider-backed nodes are connected."""

from typing import Any

from langchain_core.runnables import RunnableLambda
from sqlalchemy.orm import Session

from app.models import AssetType, FileAsset
from app.node_executors import NodeExecutionResult


class V3NodeExecutor:
    """Runs the V3 AI nodes through deterministic Runnable pipelines."""

    def __init__(self, session: Session | None = None, fail_node: str | None = None) -> None:
        self._session = session
        self._fail_node = fail_node
        self._runnables = {
            "CONTENT_PLAN": RunnableLambda(self._content_plan),
            "TEXT_GENERATE": RunnableLambda(self._text_generate),
            "TEXT_CHECK": RunnableLambda(self._text_check),
            "IMAGE_CHECK": RunnableLambda(self._image_check),
        }

    def execute(
        self,
        node_key: str,
        input_data: dict[str, Any],
        *,
        workflow_execution_id: int | None = None,
        node_execution_id: int | None = None,
        video_progress_callback: Any = None,
    ) -> NodeExecutionResult | dict[str, Any]:
        del video_progress_callback
        if node_key == "IMAGE_GENERATE":
            return self._image_generate(input_data, workflow_execution_id, node_execution_id)
        if node_key == "HUMAN_APPROVAL":
            return {"status": "WAITING_APPROVAL"}
        runnable = self._runnables.get(node_key)
        if runnable is None:
            raise ValueError(f"Unsupported V3 node: {node_key}")
        if node_key == self._fail_node:
            raise RuntimeError(f"Fake V3 executor failure for {node_key}")
        return NodeExecutionResult(
            output_data=runnable.invoke(input_data),
            attempt_metadata={"runtime": "v3-fake-runnable"},
        )

    @staticmethod
    def _content_plan(_: dict[str, Any]) -> dict[str, Any]:
        return {"slides": [{"slide_no": 1, "purpose": "hook"}, {"slide_no": 2, "purpose": "message"}]}

    @staticmethod
    def _text_generate(input_data: dict[str, Any]) -> dict[str, Any]:
        plan = input_data["slides"]
        return {"slides": [{"slide_no": slide["slide_no"], "text": "Fake headline" if slide["slide_no"] == 1 else "Fake body"} for slide in plan]}

    @staticmethod
    def _text_check(input_data: dict[str, Any]) -> dict[str, Any]:
        return {"slides": input_data["slides"], "passed": True, "violations": [], "feedback": None}

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
