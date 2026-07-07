from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ExternalComponentType = Literal["slash_command", "skill", "subagent", "hook", "mcp_server"]


@dataclass(frozen=True)
class ExternalCapabilityComponent:
    component_type: ExternalComponentType
    local_name: str
    qualified_name: str
    source_path: str
    content_sha256: str
    runtime_projection: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    ignored_fields: tuple[str, ...] = ()


@dataclass
class NormalizedExternalPluginBundle:
    source_format: str
    source_uri: str
    plugin_name: str
    version: str | None = None
    description: str | None = None
    manifest_sha256: str | None = None
    components: list[ExternalCapabilityComponent] = field(default_factory=list)
    unsupported_components: list[dict[str, Any]] = field(default_factory=list)
    credential_requirements: list[dict[str, Any]] = field(default_factory=list)
    admission_notes: list[dict[str, Any]] = field(default_factory=list)

    def component_by_name(self, qualified_name: str) -> ExternalCapabilityComponent:
        for component in self.components:
            if component.qualified_name == qualified_name:
                return component
        raise KeyError(qualified_name)

