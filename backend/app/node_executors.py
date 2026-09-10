from collections.abc import Mapping
from typing import Any, Protocol

from app.input_analysis import analyze_input


class NodeExecutor(Protocol):
    def execute(self, node_key: str, input_data: Mapping[str, Any]) -> dict[str, Any]: ...


class FakeNodeExecutor:
    """Deterministic executor used to exercise the engine without external services."""

    def __init__(self, failing_node_key: str | None = None) -> None:
        self.failing_node_key = failing_node_key
        self.executed_node_keys: list[str] = []

    def execute(self, node_key: str, input_data: Mapping[str, Any]) -> dict[str, Any]:
        self.executed_node_keys.append(node_key)
        if node_key == self.failing_node_key:
            raise RuntimeError(f"Fake executor failure for {node_key}")
        return {"result": f"fake {node_key} result", "input": dict(input_data)}


class RuleBasedNodeExecutor:
    """Provides the stable input-analysis contract without calling an AI provider."""

    def execute(self, node_key: str, input_data: Mapping[str, Any]) -> dict[str, Any]:
        if node_key == "input_analysis":
            return analyze_input(dict(input_data)).model_dump(mode="json")
        if node_key == "content_planning":
            return {"result": "fake content planning result"}
        return {"result": f"fake {node_key} result"}
