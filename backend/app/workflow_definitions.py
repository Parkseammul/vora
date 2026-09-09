from dataclasses import dataclass


@dataclass(frozen=True)
class NodeDefinition:
    key: str
    requires_approval: bool


@dataclass(frozen=True)
class Transition:
    source_node_key: str
    target_node_key: str


@dataclass(frozen=True)
class WorkflowDefinition:
    key: str
    version: int
    start_node_key: str
    nodes: tuple[NodeDefinition, ...]
    transitions: tuple[Transition, ...]

    def node(self, node_key: str) -> NodeDefinition:
        for node in self.nodes:
            if node.key == node_key:
                return node
        raise KeyError(f"Unknown node: {node_key}")

    def next_node_key(self, node_key: str) -> str | None:
        for transition in self.transitions:
            if transition.source_node_key == node_key:
                return transition.target_node_key
        return None


class WorkflowRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int], WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> None:
        registry_key = (definition.key, definition.version)
        if registry_key in self._definitions:
            raise ValueError(f"Workflow is already registered: {registry_key}")
        self._definitions[registry_key] = definition

    def get(self, workflow_key: str, workflow_version: int) -> WorkflowDefinition:
        try:
            return self._definitions[(workflow_key, workflow_version)]
        except KeyError as exc:
            raise KeyError(
                f"Workflow is not registered: {(workflow_key, workflow_version)}"
            ) from exc


VORA_CONTENT_CREATION_V1 = WorkflowDefinition(
    key="vora_content_creation",
    version=1,
    start_node_key="input_analysis",
    nodes=(
        NodeDefinition(key="input_analysis", requires_approval=False),
        NodeDefinition(key="content_planning", requires_approval=True),
        NodeDefinition(key="script_generation", requires_approval=True),
        NodeDefinition(key="video_generation", requires_approval=True),
    ),
    transitions=(
        Transition("input_analysis", "content_planning"),
        Transition("content_planning", "script_generation"),
        Transition("script_generation", "video_generation"),
    ),
)


workflow_registry = WorkflowRegistry()
workflow_registry.register(VORA_CONTENT_CREATION_V1)
