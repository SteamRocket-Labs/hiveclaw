"""Full-Agent process graphs, separate from the axis-1 governed-leaf IR.

Version 1 supports dependency-ordered Agent handoffs, explicit human gates and
UTF-8 artifact packets. Unsupported graph features fail at admission rather
than silently becoming temporary subagents or unverified prose completion.
"""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from pathlib import PurePosixPath
import re
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.runtime.workflow_definition import ArgSpec, BudgetSpec, compute_definition_hash


class A2AParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: UUID
    role: str = ""


class A2AOutputArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")
    path: str
    kind: Literal["markdown_report", "structured_json", "text"] = "markdown_report"
    required: bool = True
    schema_ref: None = None

    @field_validator("path")
    @classmethod
    def workspace_file(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or len(path.parts) < 2
            or path.parts[0] != "workspace"
        ):
            raise ValueError("artifact path must name an exact workspace/ file")
        return value


class A2AOutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts: list[A2AOutputArtifact] = Field(min_length=1)


class A2AInputArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    source: str = Field(alias="from", pattern=r"^[A-Za-z][A-Za-z0-9_-]*\.[A-Za-z][A-Za-z0-9_-]*$")
    name: str = Field(alias="as", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")


class A2AHandoffNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")
    type: Literal["agent_handoff_step"] = "agent_handoff_step"
    agent_ref: str
    task: str = Field(min_length=1)
    input_artifacts: list[A2AInputArtifact] = Field(default_factory=list)
    output_contract: A2AOutputContract
    timeout_seconds: int = Field(default=1800, ge=1, le=3600)


class A2AGateNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")
    type: Literal["gate_step"]
    reason: str = Field(min_length=1)


class A2AEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    source: str = Field(alias="from")
    target: str = Field(alias="to")


class A2AWorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    kind: Literal["a2a_workflow"] = "a2a_workflow"
    schema_version: Literal["a2a_workflow.v1"] = "a2a_workflow.v1"
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    participants: dict[str, A2AParticipant] = Field(min_length=1)
    args_schema: dict[str, ArgSpec] = Field(default_factory=dict)
    default_budget: BudgetSpec = Field(default_factory=BudgetSpec)
    max_artifact_bytes: int = Field(default=262144, ge=1)
    nodes: list[Annotated[A2AHandoffNode | A2AGateNode, Field(discriminator="type")]] = Field(min_length=1)
    edges: list[A2AEdge] = Field(default_factory=list)

    def predecessors(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {node.id: set() for node in self.nodes}
        for edge in self.edges:
            result[edge.target].add(edge.source)
        return result

    def ordered_nodes(self) -> list[A2AHandoffNode | A2AGateNode]:
        by_id = {node.id: node for node in self.nodes}
        return [by_id[node_id] for node_id in TopologicalSorter(self.predecessors()).static_order()]

    @model_validator(mode="after")
    def validate_graph(self) -> A2AWorkflowDefinition:
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("node ids must be unique")
        if not any(isinstance(node, A2AHandoffNode) for node in self.nodes):
            raise ValueError("at least one Agent handoff node is required")
        if any(edge.source not in by_id or edge.target not in by_id for edge in self.edges):
            raise ValueError("every edge must bind existing nodes")
        if len({(edge.source, edge.target) for edge in self.edges}) != len(self.edges):
            raise ValueError("duplicate edges are not allowed")
        try:
            ordered = self.ordered_nodes()
        except CycleError as exc:
            raise ValueError("A2A process graph must be acyclic") from exc
        ancestors: dict[str, set[str]] = {}
        predecessors = self.predecessors()
        for node in ordered:
            ancestors[node.id] = set(predecessors[node.id])
            for predecessor in predecessors[node.id]:
                ancestors[node.id].update(ancestors[predecessor])
            if not isinstance(node, A2AHandoffNode):
                continue
            if node.agent_ref not in self.participants:
                raise ValueError(f"unknown Agent participant: {node.agent_ref}")
            if any(not ref.strip().startswith("args.") for ref in re.findall(r"\{\{([^{}]+)\}\}", node.task)):
                raise ValueError("node tasks may reference args only; transfer upstream data through input_artifacts")
            outputs = node.output_contract.artifacts
            if len({item.name for item in outputs}) != len(outputs) or len({item.path for item in outputs}) != len(
                outputs
            ):
                raise ValueError("each node's output names and paths must be unique")
            if len({item.name for item in node.input_artifacts}) != len(node.input_artifacts):
                raise ValueError("input artifact aliases must be unique")
            for item in node.input_artifacts:
                producer, artifact_name = item.source.split(".", 1)
                source = by_id.get(producer)
                if producer not in ancestors[node.id] or not isinstance(source, A2AHandoffNode):
                    raise ValueError("input artifacts must come from a dependency ancestor")
                if artifact_name not in {output.name for output in source.output_contract.artifacts}:
                    raise ValueError(f"unknown output artifact: {item.source}")
        return self

    @property
    def definition_hash(self) -> str:
        return compute_definition_hash(self.model_dump(mode="json", by_alias=True))


def normalize_a2a_args(definition: A2AWorkflowDefinition, args: dict[str, Any]) -> dict[str, Any]:
    """Use the existing primitive argument type contract, without compiling leaf IR."""
    expected_types = {"string": str, "number": (int, float), "boolean": bool, "array": list, "object": dict}
    if set(args) - definition.args_schema.keys():
        raise ValueError("unknown workflow argument")
    for name, spec in definition.args_schema.items():
        if name not in args:
            if spec.required:
                raise ValueError(f"missing workflow argument: {name}")
            continue
        if not isinstance(args[name], expected_types[spec.type]) or (
            spec.type == "number" and isinstance(args[name], bool)
        ):
            raise ValueError(f"invalid workflow argument type: {name}")
    return dict(args)


class A2AArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: UUID
    workflow_run_id: UUID
    node_id: str
    name: str
    producer_agent_id: UUID
    producer_session_id: UUID
    producer_runtime_task_id: UUID
    path: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str
    schema_ref: str | None = None


class A2AHandoffEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["a2a_handoff"] = "a2a_handoff"
    workflow_run_id: UUID
    node_id: str
    from_agent_id: UUID
    to_agent_id: UUID
    from_session_id: UUID
    to_session_id: UUID
    runtime_task_id: UUID
    requester_user_id: UUID
    definition_hash: str
    input_artifacts: list[dict[str, Any]]
    expected_outputs: list[A2AOutputArtifact]
    artifact_access: Literal["read_only"] = "read_only"
    allow_target_workspace_read: Literal[False] = False
