"""Read-only capability pack manifest catalog.

This module intentionally does not participate in runtime tool collection.
Runtime pack membership remains owned by @tool(ToolMeta(... pack=...)).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PackManifest:
    name: str
    version: str = "0.0.0"
    description: str = ""
    license: str | None = None
    author: str | None = None
    tools: tuple[dict[str, Any], ...] = ()
    skills: tuple[str, ...] = ()
    data_sources: dict[str, Any] = field(default_factory=dict)
    mcp_servers: tuple[dict[str, Any], ...] = ()
    credential_requirements: tuple[dict[str, Any], ...] = ()
    activation: dict[str, Any] = field(default_factory=dict)
    sandbox_requirements: dict[str, Any] = field(default_factory=dict)
    manifest_path: Path | None = None

    @property
    def tool_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for tool in self.tools:
            name = tool.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(names)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "license": self.license,
            "author": self.author,
            "tools": list(self.tools),
            "tool_names": list(self.tool_names),
            "skills": list(self.skills),
            "data_sources": self.data_sources,
            "mcp_servers": list(self.mcp_servers),
            "credential_requirements": list(self.credential_requirements),
            "activation": self.activation,
            "sandbox_requirements": self.sandbox_requirements,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "runtime_source_of_truth": "tool_decorator",
        }


class PackCatalogReader:
    """Discover `packs/*/pack.yaml` manifests for catalog/UI use."""

    def __init__(self, packs_dir: Path) -> None:
        self.packs_dir = packs_dir
        self._manifests: dict[str, PackManifest] = {}

    def discover(self) -> None:
        self._manifests = {}
        if not self.packs_dir.exists():
            return

        for pack_dir in sorted(path for path in self.packs_dir.iterdir() if path.is_dir()):
            manifest_path = pack_dir / "pack.yaml"
            if not manifest_path.exists():
                continue
            try:
                loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                logger.warning("Skipping invalid pack manifest %s: %s", manifest_path, exc)
                continue
            except OSError as exc:
                logger.warning("Skipping unreadable pack manifest %s: %s", manifest_path, exc)
                continue

            if not isinstance(loaded, dict):
                logger.warning("Skipping pack manifest %s because root is not a mapping", manifest_path)
                continue

            manifest = self._build_manifest(loaded, manifest_path)
            if not manifest:
                continue
            self._manifests[manifest.name] = manifest

    def list_packs(self) -> tuple[PackManifest, ...]:
        return tuple(self._manifests.values())

    def get_pack(self, name: str) -> PackManifest | None:
        return self._manifests.get(name)

    def _build_manifest(self, data: dict[str, Any], manifest_path: Path) -> PackManifest | None:
        name = _string(data.get("name"))
        if not name:
            logger.warning("Skipping pack manifest %s because name is missing", manifest_path)
            return None

        return PackManifest(
            name=name,
            version=_string(data.get("version")) or "0.0.0",
            description=_string(data.get("description")),
            license=_optional_string(data.get("license")),
            author=_optional_string(data.get("author")),
            tools=_tool_entries(data.get("tools")),
            skills=_string_tuple(data.get("skills")),
            data_sources=_dict_value(data.get("data_sources")),
            mcp_servers=_dict_tuple(data.get("mcp_servers")),
            credential_requirements=_dict_tuple(data.get("credential_requirements")),
            activation=_dict_value(data.get("activation")),
            sandbox_requirements=_dict_value(data.get("sandbox_requirements")),
            manifest_path=manifest_path,
        )


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _optional_string(value: Any) -> str | None:
    text = _string(value)
    return text or None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(item for item in (_string(item) for item in value) if item)
    text = _string(value)
    return (text,) if text else ()


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def _tool_entries(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    entries: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str) and item:
            entries.append({"name": item})
        elif isinstance(item, dict) and _string(item.get("name")):
            entries.append(dict(item))
    return tuple(entries)
