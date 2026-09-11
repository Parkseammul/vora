from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.content_planning import ContentPlanningService
from app.input_analysis import analyze_input
from app.script_generation import ScriptGenerationService


@dataclass(frozen=True)
class NodeExecutionResult:
    """Keeps external-call metadata out of the persisted node business result."""

    output_data: dict[str, Any]
    attempt_metadata: dict[str, Any]


class NodeExecutorConfigurationError(RuntimeError):
    """Raised when a production node has no configured implementation."""


class NodeExecutor(Protocol):
    def execute(
        self,
        node_key: str,
        input_data: Mapping[str, Any],
        *,
        workflow_execution_id: int | None = None,
    ) -> dict[str, Any] | NodeExecutionResult: ...


class FakeNodeExecutor:
    """Deterministic executor used to exercise the engine without external services."""

    def __init__(self, failing_node_key: str | None = None) -> None:
        self.failing_node_key = failing_node_key
        self.executed_node_keys: list[str] = []

    def execute(
        self,
        node_key: str,
        input_data: Mapping[str, Any],
        *,
        workflow_execution_id: int | None = None,
    ) -> dict[str, Any]:
        self.executed_node_keys.append(node_key)
        if node_key == self.failing_node_key:
            raise RuntimeError(f"Fake executor failure for {node_key}")
        return {"result": f"fake {node_key} result", "input": dict(input_data)}


class RuleBasedNodeExecutor:
    """Executes stable analysis and delegates AI nodes to their business services."""

    def __init__(
        self,
        content_planning_service: ContentPlanningService | None = None,
        script_generation_service: ScriptGenerationService | None = None,
    ) -> None:
        self._content_planning_service = content_planning_service
        self._script_generation_service = script_generation_service

    def execute(
        self,
        node_key: str,
        input_data: Mapping[str, Any],
        *,
        workflow_execution_id: int | None = None,
    ) -> dict[str, Any] | NodeExecutionResult:
        if node_key == "input_analysis":
            return analyze_input(dict(input_data)).model_dump(mode="json")
        if node_key == "content_planning":
            if workflow_execution_id is None:
                raise NodeExecutorConfigurationError(
                    "content_planning requires workflow_execution_id for asset validation"
                )
            if self._content_planning_service is None:
                raise NodeExecutorConfigurationError(
                    "content_planning service is not configured"
                )
            generated = self._content_planning_service.generate(
                dict(input_data), workflow_execution_id
            )
            return NodeExecutionResult(
                output_data=generated.data.model_dump(mode="json"),
                attempt_metadata=generated.metadata.model_dump(mode="json"),
            )
        if node_key == "script_generation":
            if self._script_generation_service is None:
                raise NodeExecutorConfigurationError(
                    "script_generation service is not configured"
                )
            script_generated = self._script_generation_service.generate(dict(input_data))
            return NodeExecutionResult(
                output_data=script_generated.data.model_dump(mode="json"),
                attempt_metadata=script_generated.metadata.model_dump(mode="json"),
            )
        raise NodeExecutorConfigurationError(f"No production executor configured for {node_key}")
