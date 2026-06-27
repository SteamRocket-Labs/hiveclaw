"""Capability pack manifest catalog — install / composition / governance source.

`pack.yaml` is the install / composition / credential / governance / distribution
source of truth (NOT the executable tool schema — `@tool` decorators remain that).
Manifests are read and validated **fail-closed** here; runtime tool *visibility*
is decided by CORE + `TenantInstalledPlugin`/`AgentPluginAssignment` (Step 5), and
`@tool(ToolMeta.pack=)` is validated against — not duplicated by — these manifests
(see `app/tools/audit.py`).

Tool classification (governed inclusion §0.x correction 4): each entry in `tools`
carries a ``role`` — ``owns`` (this pack defines it), ``requires_core`` (depends on
a CORE tool, may reference CORE), or ``optional_provider`` (unlocked only with a
provider credential). ``CORE ∩ owns = ∅`` is asserted in audit; ``requires_core``
may reference CORE.

Governed inclusion (§6 decision 7): ``hooks`` may only bind a platform-allowlisted
handler or governed external hook type (never raw shell/import/webhook);
``dependencies`` must be pinned; ``source`` is recognized for builtin/local
(installable in v1) and structurally recognized-but-fail-closed for remote kinds
until signature + sandbox infra lands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Valid tool roles within a manifest (§0.x correction 4).
_TOOL_ROLES = frozenset({"owns", "requires_core", "optional_provider"})

# Platform allowlist for declarative hook handlers (governed inclusion §6.7). A
# manifest may only bind a hook to one of these platform-owned names. ``plugin.*``
# maps to in-process Python handlers; ``hook.*`` maps to GovernedHookRunner
# specs. Manifests never provide raw Python/import paths or direct webhooks.
HOOK_HANDLER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "hook.agent",
        "hook.command",
        "hook.http",
        "hook.prompt",
        "plugin.audit",
        "plugin.block",
        "plugin.args_overlay",
    }
)

# Source kinds. builtin/local are installable in v1; remote kinds are recognized
# but fail closed until content-hash/signature verification + sandbox
# materialization infra lands (§6 decision 3).
_INSTALLABLE_SOURCE_KINDS = frozenset({"builtin", "local"})
_REMOTE_SOURCE_KINDS = frozenset({"git", "url", "npm", "pip"})
_RECOGNIZED_SOURCE_KINDS = _INSTALLABLE_SOURCE_KINDS | _REMOTE_SOURCE_KINDS


def find_pack_dirs(anchor: Path | None = None) -> tuple[Path, ...]:
    """Find pack manifest roots for both repo and packaged backend layouts.

    Local development keeps pack manifests at `<repo>/packs`. Railway backend
    deployments use `backend` as the build root, so deployable manifests may
    live at `<backend>/packs` inside the container. Return nearest roots first
    and de-duplicate paths.
    """
    start = (anchor or Path(__file__).resolve()).resolve()
    ancestors = (start, *start.parents) if start.is_dir() else start.parents
    seen: set[Path] = set()
    pack_dirs: list[Path] = []

    for ancestor in ancestors:
        candidate = ancestor / "packs"
        has_manifest = candidate.is_dir() and any(pack.joinpath("pack.yaml").is_file() for pack in candidate.iterdir())
        if has_manifest and candidate not in seen:
            seen.add(candidate)
            pack_dirs.append(candidate)

    return tuple(pack_dirs)


@dataclass(frozen=True, slots=True)
class PackManifest:
    name: str
    version: str = "0.0.0"
    description: str = ""
    license: str | None = None
    author: str | None = None
    tools: tuple[dict[str, Any], ...] = ()
    skills: tuple[str, ...] = ()
    agents: tuple[dict[str, Any], ...] = ()
    hooks: tuple[dict[str, Any], ...] = ()
    dependencies: tuple[dict[str, Any], ...] = ()
    source: dict[str, Any] = field(default_factory=dict)
    data_sources: dict[str, Any] = field(default_factory=dict)
    mcp_servers: tuple[dict[str, Any], ...] = ()
    credential_requirements: tuple[dict[str, Any], ...] = ()
    activation: dict[str, Any] = field(default_factory=dict)
    sandbox_requirements: dict[str, Any] = field(default_factory=dict)
    manifest_path: Path | None = None
    validation_errors: tuple[str, ...] = ()

    def _names_for_role(self, role: str) -> tuple[str, ...]:
        names: list[str] = []
        for tool in self.tools:
            name = tool.get("name")
            tool_role = str(tool.get("role") or "owns").strip().lower()
            if isinstance(name, str) and name and tool_role == role:
                names.append(name)
        return tuple(names)

    @property
    def owns_names(self) -> tuple[str, ...]:
        """Tools this pack defines (CORE ∩ owns must be ∅ — asserted in audit)."""
        return self._names_for_role("owns")

    @property
    def requires_core_names(self) -> tuple[str, ...]:
        """CORE / cross-pack tools this pack depends on (may reference CORE)."""
        return self._names_for_role("requires_core")

    @property
    def optional_provider_names(self) -> tuple[str, ...]:
        """Tools unlocked only with a provider credential (e.g. firecrawl)."""
        return self._names_for_role("optional_provider")

    @property
    def tool_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for tool in self.tools:
            name = tool.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(names)

    @property
    def is_valid(self) -> bool:
        return not self.validation_errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "license": self.license,
            "author": self.author,
            "tools": list(self.tools),
            "tool_names": list(self.tool_names),
            "owns": list(self.owns_names),
            "requires_core": list(self.requires_core_names),
            "optional_providers": list(self.optional_provider_names),
            "skills": list(self.skills),
            "agents": list(self.agents),
            "hooks": list(self.hooks),
            "dependencies": list(self.dependencies),
            "source": self.source,
            "data_sources": self.data_sources,
            "mcp_servers": list(self.mcp_servers),
            "credential_requirements": list(self.credential_requirements),
            "activation": self.activation,
            "sandbox_requirements": self.sandbox_requirements,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "validation_errors": list(self.validation_errors),
            "is_valid": self.is_valid,
            "runtime_source_of_truth": "tool_decorator",
        }


def validate_manifest(data: dict[str, Any]) -> tuple[str, ...]:
    """Fail-closed structural validation of a manifest (governed inclusion §6.7).

    Catches the dangerous shapes BEFORE a manifest can drive any install:
    invalid tool roles, hook handlers outside the platform allowlist (or raw
    shell/import/webhook), unpinned dependencies, and disallowed / remote source
    kinds whose enablement infra is not yet ready. Returns a tuple of human
    errors; an empty tuple means the manifest is structurally safe.
    """
    errors: list[str] = []

    # Tool roles must be known.
    for tool in _tool_entries(data.get("tools")):
        role = str(tool.get("role") or "owns").strip().lower()
        if role not in _TOOL_ROLES:
            errors.append(f"tool {tool.get('name')!r} has unknown role {role!r} (allowed: {sorted(_TOOL_ROLES)})")

    # Hooks: declarative, platform-allowlisted handlers only — no raw code.
    for hook in _dict_tuple(data.get("hooks")):
        handler = _string(hook.get("handler"))
        if not handler:
            errors.append("hook entry is missing a 'handler'")
            continue
        mode = _string(hook.get("mode") or "observe").lower()
        if mode not in {"observe", "enforce"}:
            errors.append(f"hook handler {handler!r} has invalid mode {mode!r} (allowed: observe/enforce)")
        if any(token in handler for token in ("/", "\\", ".py", "import ", "http://", "https://", "$(", "`")):
            errors.append(f"hook handler {handler!r} looks like raw code/shell/webhook — forbidden")
        elif handler not in HOOK_HANDLER_ALLOWLIST:
            errors.append(f"hook handler {handler!r} is not in the platform allowlist")

    # Dependencies must be pinned (name + exact version).
    for dep in _dict_tuple(data.get("dependencies")):
        dep_name = _string(dep.get("name"))
        version = _string(dep.get("version"))
        if not dep_name:
            errors.append("dependency entry is missing a 'name'")
        if not version:
            errors.append(f"dependency {dep_name!r} is not pinned (missing exact 'version')")

    # Source provenance: recognized kinds only; remote kinds fail closed in v1.
    source = data.get("source")
    if source is not None:
        if not isinstance(source, dict):
            errors.append("source must be a mapping")
        else:
            kind = _string(source.get("kind") or "builtin").lower()
            if kind not in _RECOGNIZED_SOURCE_KINDS:
                errors.append(f"source kind {kind!r} is not recognized (allowed: {sorted(_RECOGNIZED_SOURCE_KINDS)})")
            elif kind in _REMOTE_SOURCE_KINDS:
                errors.append(
                    f"source kind {kind!r} is recognized but not installable in v1 "
                    "(needs signature verification + sandbox materialization) — fail-closed"
                )

    return tuple(errors)


class PackCatalogReader:
    """Discover `packs/*/pack.yaml` manifests for install/catalog/UI use."""

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

        errors = validate_manifest(data)
        if errors:
            logger.warning("Pack manifest %s has %d validation error(s): %s", manifest_path, len(errors), errors)

        return PackManifest(
            name=name,
            version=_string(data.get("version")) or "0.0.0",
            description=_string(data.get("description")),
            license=_optional_string(data.get("license")),
            author=_optional_string(data.get("author")),
            tools=_tool_entries(data.get("tools")),
            skills=_string_tuple(data.get("skills")),
            agents=_dict_tuple(data.get("agents")),
            hooks=_dict_tuple(data.get("hooks")),
            dependencies=_dict_tuple(data.get("dependencies")),
            source=_dict_value(data.get("source")),
            data_sources=_dict_value(data.get("data_sources")),
            mcp_servers=_dict_tuple(data.get("mcp_servers")),
            credential_requirements=_dict_tuple(data.get("credential_requirements")),
            activation=_dict_value(data.get("activation")),
            sandbox_requirements=_dict_value(data.get("sandbox_requirements")),
            manifest_path=manifest_path,
            validation_errors=errors,
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
