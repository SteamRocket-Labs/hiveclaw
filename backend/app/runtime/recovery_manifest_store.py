"""Authority-bound durable storage for :mod:`app.runtime.recovery_manifest`.

The model-visible manifest remains lossless structured recovery evidence.  This
module owns the mechanical boundary around that evidence: authenticated runtime
identity, a session/root-run storage key, server HMAC integrity, atomic writes,
typed hold/quarantine outcomes, and legacy cutover.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from app.runtime.ccplus_contracts import permission_profile_snapshot_hash
from app.runtime.recovery_manifest import (
    LEGACY_RUNTIME_RECOVERY_MANIFEST_REL_PATH,
    LEGACY_WORKSPACE_RECOVERY_MANIFEST_REL_PATH,
    RecoveryManifest,
    build_recovery_manifest,
    hydrate_session_context_from_recovery_manifest,
    manifest_from_payload,
    merge_session_memory_into_manifest,
)
from app.runtime.recovery_manifest_metrics import record_recovery_manifest_event
from app.services.execution_receipts import canonical_payload_hash


RECOVERY_AUTHORITY_SCHEMA = "hive.recovery_authority.v1"
RECOVERY_ENVELOPE_SCHEMA = "hive.recovery_manifest_envelope.v2"
RECOVERY_MANIFESTS_REL_DIR = Path("runtime_artifacts") / "recovery_manifests"
RECOVERY_QUARANTINE_REL_DIR = RECOVERY_MANIFESTS_REL_DIR / "quarantine"
RECOVERY_SNAPSHOTS_DIR_NAME = "snapshots"
_SAFE_AGENT_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_-]+$")
_RECOVERY_RESOURCE_REF_RE = re.compile(r"^recovery-manifest://([0-9a-f]{64})/([0-9a-f]{64})$")


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _sequence(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _metadata_sequence(metadata: Mapping[str, Any]) -> int | None:
    for key in ("base_transcript_sequence", "initial_user_message_t0_sequence"):
        if key in metadata and metadata.get(key) is not None:
            return _sequence(metadata.get(key))
    return None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RecoveryAuthorityFrame:
    """Complete mechanical authority required to consume recovery state."""

    tenant_id: str
    agent_id: str
    requester_user_id: str | None
    session_id: str
    root_session_id: str
    root_runtime_task_id: str | None
    principal_type: str
    principal_id: str | None
    principal_snapshot_hash: str
    policy_snapshot_hash: str
    config_snapshot_hash: str
    base_transcript_sequence: int | None
    delegation_authority_hash: str | None = None
    schema: str = RECOVERY_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        required = {
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "root_session_id": self.root_session_id,
            "root_runtime_task_id": self.root_runtime_task_id,
            "principal_type": self.principal_type,
            "principal_id": self.principal_id,
            "principal_snapshot_hash": self.principal_snapshot_hash,
            "policy_snapshot_hash": self.policy_snapshot_hash,
            "config_snapshot_hash": self.config_snapshot_hash,
        }
        missing = [key for key, value in required.items() if not _text(value)]
        if self.schema != RECOVERY_AUTHORITY_SCHEMA:
            raise ValueError("unsupported recovery authority schema")
        if missing:
            raise ValueError("recovery authority is incomplete: " + ", ".join(missing))
        if not _SAFE_AGENT_PATH_COMPONENT.fullmatch(self.agent_id):
            raise ValueError("recovery authority agent_id is not a safe path component")
        if self.base_transcript_sequence is not None and self.base_transcript_sequence < 0:
            raise ValueError("base_transcript_sequence must be non-negative")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "requester_user_id": self.requester_user_id,
            "session_id": self.session_id,
            "root_session_id": self.root_session_id,
            "root_runtime_task_id": self.root_runtime_task_id,
            "principal_type": self.principal_type,
            "principal_id": self.principal_id,
            "principal_snapshot_hash": self.principal_snapshot_hash,
            "policy_snapshot_hash": self.policy_snapshot_hash,
            "config_snapshot_hash": self.config_snapshot_hash,
            "base_transcript_sequence": self.base_transcript_sequence,
            "delegation_authority_hash": self.delegation_authority_hash,
        }

    @property
    def digest(self) -> str:
        return canonical_payload_hash(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RecoveryAuthorityFrame":
        return cls(
            schema=str(payload.get("schema") or ""),
            tenant_id=str(payload.get("tenant_id") or ""),
            agent_id=str(payload.get("agent_id") or ""),
            requester_user_id=_text(payload.get("requester_user_id")),
            session_id=str(payload.get("session_id") or ""),
            root_session_id=str(payload.get("root_session_id") or ""),
            root_runtime_task_id=_text(payload.get("root_runtime_task_id")),
            principal_type=str(payload.get("principal_type") or ""),
            principal_id=_text(payload.get("principal_id")),
            principal_snapshot_hash=str(payload.get("principal_snapshot_hash") or ""),
            policy_snapshot_hash=str(payload.get("policy_snapshot_hash") or ""),
            config_snapshot_hash=str(payload.get("config_snapshot_hash") or ""),
            base_transcript_sequence=_sequence(payload.get("base_transcript_sequence")),
            delegation_authority_hash=_text(payload.get("delegation_authority_hash")),
        )


@dataclass(frozen=True, slots=True)
class RecoveryAuthorityResolution:
    status: str
    frame: RecoveryAuthorityFrame | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryManifestPersistResult:
    status: str
    reason: str | None = None
    paths: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryManifestResourceReadResult:
    status: str
    reason: str | None = None
    ref: str | None = None
    sha256: str | None = None
    content: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryManifestLoadResult:
    status: str
    reason: str | None = None
    authority: RecoveryAuthorityFrame | None = None
    manifest: RecoveryManifest | None = None
    path: Path | None = None
    manifest_ref: str | None = None
    envelope_sha256: str | None = None
    envelope_chars: int | None = None
    envelope_bytes: int | None = None
    resource_path: Path | None = None
    resource_agent_root: Path | None = None
    raw_envelope: bytes | None = None
    quarantine_path: Path | None = None
    quarantine_ref: str | None = None

    @property
    def loaded(self) -> bool:
        return self.status in {"loaded", "loaded_migrated"} and self.manifest is not None

    def hydrate(self, session_context: Any) -> bool:
        if not self.loaded or self.authority is None or self.manifest is None:
            return False
        runtime_session_id = _text(getattr(session_context, "session_id", None))
        if runtime_session_id != self.authority.session_id:
            return False
        hydrated = hydrate_session_context_from_recovery_manifest(session_context, self.manifest)
        metadata = getattr(session_context, "metadata", None)
        if hydrated and isinstance(metadata, dict):
            metadata["recovery_manifest_status"] = self.status
            metadata["recovery_manifest_authority_hash"] = self.authority.digest
        return hydrated

    def render_restoration_text(self, *, budget_chars: int = 20_000) -> str:
        if not self.loaded or self.manifest is None:
            return ""
        if self.resource_path is None or self.resource_agent_root is None or self.raw_envelope is None:
            raise RuntimeError("recovery manifest resource snapshot is unavailable")
        _materialize_envelope_snapshot(
            self.resource_path,
            self.raw_envelope,
            agent_root=self.resource_agent_root,
        )
        return self.manifest.to_restoration_text(
            budget_chars=budget_chars,
            manifest_ref=self.manifest_ref,
            manifest_sha256=self.envelope_sha256,
            manifest_chars=self.envelope_chars,
            manifest_bytes=self.envelope_bytes,
            manifest_reader_tool="read_context_resource",
        )

    def status_payload(self) -> dict[str, Any] | None:
        if self.status == "absent":
            return None
        return {
            "schema": "hive.recovery_manifest_status.v1",
            "status": self.status,
            "reason": self.reason,
            "retryable": self.status == "unavailable",
        }


def unavailable_recovery_result(reason: str) -> RecoveryManifestLoadResult:
    return _record_load_result(RecoveryManifestLoadResult(status="unavailable", reason=reason))


def _resolution(
    status: str,
    *,
    frame: RecoveryAuthorityFrame | None = None,
    reason: str | None = None,
) -> RecoveryAuthorityResolution:
    record_recovery_manifest_event(operation="resolve", status=status, reason=reason)
    return RecoveryAuthorityResolution(status, frame=frame, reason=reason)


def _record_load_result(result: RecoveryManifestLoadResult) -> RecoveryManifestLoadResult:
    record_recovery_manifest_event(operation="load", status=result.status, reason=result.reason)
    return result


def _record_persist_result(result: RecoveryManifestPersistResult) -> RecoveryManifestPersistResult:
    record_recovery_manifest_event(operation="persist", status=result.status, reason=result.reason)
    return result


def _session_metadata(request: Any) -> dict[str, Any]:
    session = getattr(request, "session_context", None)
    metadata = getattr(session, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _principal_resolution(request: Any, tenant_id: str) -> tuple[dict[str, Any] | None, str | None]:
    metadata = _session_metadata(request)
    evidence = metadata.get("execution_principal")
    if isinstance(evidence, dict):
        try:
            from app.core.execution_context import ExecutionPrincipal

            principal = ExecutionPrincipal.from_evidence(evidence)
        except (KeyError, TypeError, ValueError) as exc:
            return None, f"execution_principal_invalid:{type(exc).__name__}"
        if principal is None or str(principal.tenant_id) != tenant_id:
            return None, "execution_principal_tenant_mismatch"
        requester = _text(principal.requester_user_id)
        request_user = _text(getattr(request, "user_id", None))
        if requester and request_user and requester != request_user:
            return None, "requester_principal_mismatch"
        payload = principal.to_evidence()
        return {
            "requester_user_id": requester or request_user,
            "root_session_id": _text(principal.root_session_id),
            "root_runtime_task_id": _text(principal.root_runtime_task_id),
            "principal_type": "execution_principal",
            "principal_id": requester or _text(principal.source_agent_id),
            "principal_payload": payload,
        }, None

    request_user = _text(getattr(request, "user_id", None))
    identity = getattr(request, "execution_identity", None)
    principal_type = _text(getattr(identity, "identity_type", None))
    principal_id = _text(getattr(identity, "identity_id", None))
    agent_id = _text(getattr(request, "agent_id", None))
    if not principal_type:
        principal_type = "delegated_user" if request_user else "agent_bot"
    if not principal_id:
        principal_id = request_user or agent_id
    payload = {
        "schema": "hive.recovery_runtime_principal.v1",
        "tenant_id": tenant_id,
        "principal_type": principal_type,
        "principal_id": principal_id,
        "requester_user_id": request_user,
    }
    return {
        "requester_user_id": request_user,
        "root_session_id": None,
        "root_runtime_task_id": None,
        "principal_type": principal_type,
        "principal_id": principal_id,
        "principal_payload": payload,
    }, None


def _runtime_config_snapshot(request: Any, runtime_config: Any) -> dict[str, Any]:
    model = getattr(request, "model", None)
    return {
        "execution_mode": _text(getattr(runtime_config, "execution_mode", None)),
        "max_tool_rounds": getattr(request, "max_tool_rounds", None)
        or getattr(runtime_config, "max_tool_rounds", None),
        "turn_token_budget": getattr(runtime_config, "turn_token_budget", None),
        "core_tools_only": bool(getattr(request, "core_tools_only", True)),
        "allowed_tool_names": sorted(str(item) for item in getattr(request, "allowed_tool_names", ()) or ()),
        "excluded_tool_names": sorted(str(item) for item in getattr(request, "excluded_tool_names", ()) or ()),
        "expand_tools": bool(getattr(request, "expand_tools", True)),
        "disable_tools": bool(getattr(request, "disable_tools", False)),
        "max_output_tokens": getattr(request, "max_output_tokens", None),
        "invocation_scope": _text(getattr(request, "invocation_scope", None)),
        "model_provider": _text(getattr(model, "provider", None)),
        "model_name": _text(getattr(model, "model", None)),
        "model_max_input_tokens": getattr(model, "max_input_tokens", None),
    }


def resolve_recovery_authority(request: Any, runtime_config: Any) -> RecoveryAuthorityResolution:
    """Resolve a complete frame from authenticated runtime facts without guessing."""

    tenant_id = _text(getattr(runtime_config, "tenant_id", None))
    agent_id = _text(getattr(request, "agent_id", None))
    session_context = getattr(request, "session_context", None)
    context_session_id = _text(getattr(session_context, "session_id", None))
    memory_session_id = _text(getattr(request, "memory_session_id", None))
    if not tenant_id:
        return _resolution("unavailable", reason="tenant_id_unavailable")
    if not agent_id:
        return _resolution("unavailable", reason="agent_id_unavailable")
    if context_session_id and memory_session_id and context_session_id != memory_session_id:
        return _resolution("unavailable", reason="session_id_drift")
    session_id = context_session_id or memory_session_id
    if not session_id:
        return _resolution("unavailable", reason="session_id_unavailable")

    principal, principal_error = _principal_resolution(request, tenant_id)
    if principal is None:
        return _resolution("unavailable", reason=principal_error or "principal_unavailable")
    metadata = _session_metadata(request)
    profile = metadata.get("permission_profile") if isinstance(metadata.get("permission_profile"), dict) else None
    policy_hash = permission_profile_snapshot_hash(profile)
    if metadata.get("a2a_authority_required") is True:
        declared_policy_hash = _text(metadata.get("a2a_authority_policy_hash"))
        if not declared_policy_hash:
            return _resolution("unavailable", reason="a2a_policy_snapshot_missing")
        if declared_policy_hash != policy_hash:
            return _resolution("unavailable", reason="a2a_policy_snapshot_drift")

    root_session_id = principal["root_session_id"] or _text(metadata.get("root_session_id")) or session_id
    root_runtime_task_id = (
        principal["root_runtime_task_id"]
        or _text(metadata.get("root_runtime_task_id"))
        or _text(metadata.get("runtime_task_id"))
        or _text(metadata.get("task_id"))
    )
    if not root_runtime_task_id:
        return _resolution("unavailable", reason="root_runtime_task_id_unavailable")
    frame = RecoveryAuthorityFrame(
        tenant_id=tenant_id,
        agent_id=agent_id,
        requester_user_id=principal["requester_user_id"],
        session_id=session_id,
        root_session_id=root_session_id,
        root_runtime_task_id=root_runtime_task_id,
        principal_type=str(principal["principal_type"]),
        principal_id=principal["principal_id"],
        principal_snapshot_hash=canonical_payload_hash(principal["principal_payload"]),
        policy_snapshot_hash=policy_hash,
        config_snapshot_hash=canonical_payload_hash(_runtime_config_snapshot(request, runtime_config)),
        base_transcript_sequence=_metadata_sequence(metadata),
        delegation_authority_hash=_text(metadata.get("a2a_authority_snapshot_hash")),
    )
    return _resolution("bound", frame=frame)


def _data_root(data_root: str | Path | None) -> Path:
    if data_root is not None:
        return Path(data_root)
    from app.config import get_settings

    return Path(get_settings().AGENT_DATA_DIR)


def _agent_root_for_id(agent_id: Any, data_root: str | Path | None) -> Path:
    normalized_agent_id = _text(agent_id)
    if normalized_agent_id is None or not _SAFE_AGENT_PATH_COMPONENT.fullmatch(normalized_agent_id):
        raise ValueError("recovery authority agent_id is not a safe path component")
    root = _data_root(data_root) / normalized_agent_id
    if root.is_symlink():
        raise OSError("recovery manifest agent root is a symlink")
    return root


def _agent_root(authority: RecoveryAuthorityFrame, data_root: str | Path | None) -> Path:
    return _agent_root_for_id(authority.agent_id, data_root)


def _guard_agent_path_for_id(path: Path, agent_id: Any, data_root: str | Path | None) -> Path:
    root = _agent_root_for_id(agent_id, data_root)
    if path.is_symlink():
        raise OSError("recovery manifest path is a symlink")
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise OSError("recovery manifest path escapes the agent root") from exc
    return path


def _guard_agent_path(path: Path, authority: RecoveryAuthorityFrame, data_root: str | Path | None) -> Path:
    return _guard_agent_path_for_id(path, authority.agent_id, data_root)


def _session_directory(authority: RecoveryAuthorityFrame, data_root: str | Path | None) -> Path:
    session_digest = hashlib.sha256(authority.session_id.encode("utf-8")).hexdigest()
    return _agent_root(authority, data_root) / RECOVERY_MANIFESTS_REL_DIR / session_digest


def recovery_manifest_path(
    authority: RecoveryAuthorityFrame,
    *,
    data_root: str | Path | None = None,
) -> Path:
    return _guard_agent_path(
        _session_directory(authority, data_root) / f"{authority.digest}.json",
        authority,
        data_root,
    )


def _resource_snapshot_path(
    authority: RecoveryAuthorityFrame,
    envelope_sha256: str,
    *,
    data_root: str | Path | None,
) -> Path:
    return _guard_agent_path(
        _session_directory(authority, data_root)
        / RECOVERY_SNAPSHOTS_DIR_NAME
        / authority.digest
        / f"{envelope_sha256}.json",
        authority,
        data_root,
    )


def _resource_ref(authority: RecoveryAuthorityFrame, envelope_sha256: str) -> str:
    return f"recovery-manifest://{authority.digest}/{envelope_sha256}"


def _signing_key() -> bytes:
    from app.config import get_settings

    secret = str(get_settings().JWT_SECRET_KEY or "")
    if not secret:
        raise RuntimeError("recovery manifest signing key is unavailable")
    return hashlib.sha256(f"{secret}:hive.recovery-manifest.v2".encode()).digest()


def _envelope_bytes(authority: RecoveryAuthorityFrame, manifest: RecoveryManifest) -> bytes:
    body = {
        "schema": RECOVERY_ENVELOPE_SCHEMA,
        "authority": authority.to_payload(),
        "manifest": manifest.to_payload(),
    }
    signature = hmac.new(_signing_key(), _canonical_bytes(body), hashlib.sha256).hexdigest()
    return json.dumps(
        {
            "body": body,
            "integrity": {
                "schema": "hive.hmac_sha256.v1",
                "purpose": RECOVERY_ENVELOPE_SCHEMA,
                "signature": signature,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


def _materialize_envelope_snapshot(path: Path, payload: bytes, *, agent_root: Path) -> None:
    if agent_root.is_symlink():
        raise OSError("recovery manifest snapshot agent root is a symlink")
    try:
        path.resolve(strict=False).relative_to(agent_root.resolve(strict=False))
    except ValueError as exc:
        raise OSError("recovery manifest snapshot path escapes the agent root") from exc
    if path.is_symlink():
        raise OSError("recovery manifest snapshot path is a symlink")
    expected_sha256 = path.stem
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise OSError("recovery manifest snapshot hash does not match its immutable path")
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise OSError("recovery manifest immutable snapshot collision")
        return
    _atomic_write(path, payload)


def _quarantine(
    path: Path,
    *,
    reason: str,
    authority: RecoveryAuthorityFrame,
    data_root: str | Path | None,
) -> tuple[Path, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    destination = _agent_root(authority, data_root) / RECOVERY_QUARANTINE_REL_DIR / f"{reason}-{digest}.json"
    _guard_agent_path(destination, authority, data_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != raw:
            raise OSError("recovery quarantine digest collision")
        path.unlink(missing_ok=True)
    else:
        os.replace(path, destination)
        os.chmod(destination, 0o600)
    _fsync_directory(destination.parent)
    if path.parent != destination.parent:
        _fsync_directory(path.parent)
    return destination, destination.relative_to(_agent_root(authority, data_root)).as_posix()


def _parse_signed_envelope(raw: bytes) -> tuple[RecoveryAuthorityFrame, RecoveryManifest]:
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("corrupt_json") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("body"), dict):
        raise ValueError("legacy_authority_unverifiable")
    body = envelope["body"]
    integrity = envelope.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("schema") != "hive.hmac_sha256.v1":
        raise ValueError("legacy_authority_unverifiable")
    expected = hmac.new(_signing_key(), _canonical_bytes(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(integrity.get("signature") or ""), expected):
        raise ValueError("integrity_mismatch")
    if body.get("schema") != RECOVERY_ENVELOPE_SCHEMA:
        raise ValueError("unsupported_envelope_schema")
    authority_payload = body.get("authority")
    manifest_payload = body.get("manifest")
    if not isinstance(authority_payload, dict) or not isinstance(manifest_payload, dict):
        raise ValueError("invalid_envelope_shape")
    return RecoveryAuthorityFrame.from_payload(authority_payload), manifest_from_payload(manifest_payload)


_AUTHORITY_FIELDS = (
    "tenant_id",
    "agent_id",
    "requester_user_id",
    "session_id",
    "root_session_id",
    "root_runtime_task_id",
    "principal_type",
    "principal_id",
    "principal_snapshot_hash",
    "policy_snapshot_hash",
    "config_snapshot_hash",
    "base_transcript_sequence",
    "delegation_authority_hash",
)


def _authority_mismatch(stored: RecoveryAuthorityFrame, expected: RecoveryAuthorityFrame) -> str | None:
    for field_name in _AUTHORITY_FIELDS:
        if getattr(stored, field_name) != getattr(expected, field_name):
            return f"{field_name}_mismatch"
    return None


def _normalized_identifier(value: Any) -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    try:
        return UUID(normalized).hex
    except ValueError:
        return normalized


def _same_identifier(left: Any, right: Any) -> bool:
    normalized_left = _normalized_identifier(left)
    normalized_right = _normalized_identifier(right)
    return normalized_left is not None and normalized_left == normalized_right


def _resource_read_result(
    status: str,
    *,
    reason: str | None = None,
    ref: str | None = None,
    sha256: str | None = None,
    content: str | None = None,
) -> RecoveryManifestResourceReadResult:
    metric_status = {
        "ok": "loaded",
        "invalid_arguments": "held",
        "authority_denied": "held",
        "not_found": "unavailable",
        "integrity_mismatch": "quarantined",
    }.get(status, "unavailable")
    record_recovery_manifest_event(
        operation="resource",
        status=metric_status,
        reason=reason,
    )
    return RecoveryManifestResourceReadResult(
        status=status,
        reason=reason,
        ref=ref,
        sha256=sha256,
        content=content,
    )


def read_recovery_manifest_resource(
    ref: str,
    *,
    context: Any,
    data_root: str | Path | None = None,
) -> RecoveryManifestResourceReadResult:
    """Read one immutable envelope through the authenticated tool frame.

    The opaque ref never acts as bearer authority. The signed envelope must
    still match the current tenant, Agent, requester, session, root task,
    permission profile, and (when delegated) principal/delegation snapshots.
    """

    match = _RECOVERY_RESOURCE_REF_RE.fullmatch(str(ref or "").strip())
    if match is None:
        return _resource_read_result("invalid_arguments", reason="invalid_resource_ref")
    authority_digest, envelope_sha256 = match.groups()
    agent_id = _text(getattr(context, "agent_id", None))
    tenant_id = _text(getattr(context, "tenant_id", None))
    requester_user_id = _text(getattr(context, "user_id", None))
    session_id = _text(getattr(context, "session_id", None))
    root_runtime_task_id = _text(getattr(context, "authority_root_runtime_task_id", None)) or _text(
        getattr(context, "runtime_task_id", None)
    )
    if not all((agent_id, tenant_id, requester_user_id, session_id, root_runtime_task_id)):
        return _resource_read_result("authority_denied", reason="resource_authority_incomplete")
    try:
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        path = _guard_agent_path_for_id(
            _agent_root_for_id(agent_id, data_root)
            / RECOVERY_MANIFESTS_REL_DIR
            / session_digest
            / RECOVERY_SNAPSHOTS_DIR_NAME
            / authority_digest
            / f"{envelope_sha256}.json",
            agent_id,
            data_root,
        )
    except (OSError, ValueError):
        return _resource_read_result("authority_denied", reason="resource_path_rejected")
    if not path.is_file():
        return _resource_read_result("not_found", reason="resource_not_found")
    try:
        raw = path.read_bytes()
    except OSError:
        return _resource_read_result("not_found", reason="resource_read_unavailable")
    if hashlib.sha256(raw).hexdigest() != envelope_sha256:
        return _resource_read_result("integrity_mismatch", reason="resource_hash_mismatch")
    try:
        stored_authority, _manifest = _parse_signed_envelope(raw)
    except ValueError as exc:
        return _resource_read_result("integrity_mismatch", reason=str(exc))
    if stored_authority.digest != authority_digest:
        return _resource_read_result("integrity_mismatch", reason="resource_authority_digest_mismatch")

    expected_policy_hash = permission_profile_snapshot_hash(getattr(context, "permission_profile", None))
    authority_checks = (
        (stored_authority.tenant_id == tenant_id, "tenant_id_mismatch"),
        (_same_identifier(stored_authority.agent_id, agent_id), "agent_id_mismatch"),
        (stored_authority.session_id == session_id, "session_id_mismatch"),
        (
            _same_identifier(stored_authority.root_runtime_task_id, root_runtime_task_id),
            "root_runtime_task_id_mismatch",
        ),
        (stored_authority.policy_snapshot_hash == expected_policy_hash, "policy_snapshot_hash_mismatch"),
    )
    for accepted, reason in authority_checks:
        if not accepted:
            return _resource_read_result("authority_denied", reason=reason)
    if stored_authority.requester_user_id is not None:
        if not _same_identifier(stored_authority.requester_user_id, requester_user_id):
            return _resource_read_result("authority_denied", reason="requester_user_id_mismatch")
    elif not (
        stored_authority.principal_type == "agent_bot"
        and _same_identifier(requester_user_id, stored_authority.agent_id)
    ):
        return _resource_read_result("authority_denied", reason="requester_user_id_mismatch")

    execution_principal = getattr(context, "execution_principal", None)
    if stored_authority.principal_type == "execution_principal":
        try:
            principal_hash = canonical_payload_hash(execution_principal.to_evidence())
        except (AttributeError, TypeError, ValueError):
            return _resource_read_result("authority_denied", reason="principal_unavailable")
        if principal_hash != stored_authority.principal_snapshot_hash:
            return _resource_read_result("authority_denied", reason="principal_snapshot_hash_mismatch")
    else:
        identity = getattr(context, "execution_identity", None)
        if identity is None:
            return _resource_read_result("authority_denied", reason="principal_unavailable")
        if _text(getattr(identity, "identity_type", None)) != stored_authority.principal_type or not _same_identifier(
            getattr(identity, "identity_id", None),
            stored_authority.principal_id,
        ):
            return _resource_read_result("authority_denied", reason="principal_snapshot_hash_mismatch")
    if stored_authority.delegation_authority_hash is not None and (
        _text(getattr(context, "authority_snapshot_hash", None))
        != stored_authority.delegation_authority_hash
    ):
        return _resource_read_result("authority_denied", reason="delegation_authority_hash_mismatch")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _resource_read_result("integrity_mismatch", reason="corrupt_json")
    return _resource_read_result(
        "ok",
        ref=ref,
        sha256=envelope_sha256,
        content=content,
    )


def _loaded_result(
    *,
    status: str,
    authority: RecoveryAuthorityFrame,
    manifest: RecoveryManifest,
    path: Path,
    raw: bytes,
    data_root: str | Path | None,
    quarantine_path: Path | None = None,
    quarantine_ref: str | None = None,
) -> RecoveryManifestLoadResult:
    envelope_sha256 = hashlib.sha256(raw).hexdigest()
    return RecoveryManifestLoadResult(
        status=status,
        authority=authority,
        manifest=manifest,
        path=path,
        manifest_ref=_resource_ref(authority, envelope_sha256),
        envelope_sha256=envelope_sha256,
        envelope_chars=len(raw.decode("utf-8")),
        envelope_bytes=len(raw),
        resource_path=_resource_snapshot_path(
            authority,
            envelope_sha256,
            data_root=data_root,
        ),
        resource_agent_root=_agent_root(authority, data_root),
        raw_envelope=raw,
        quarantine_path=quarantine_path,
        quarantine_ref=quarantine_ref,
    )


def _load_exact(
    path: Path,
    authority: RecoveryAuthorityFrame,
    *,
    data_root: str | Path | None,
) -> RecoveryManifestLoadResult:
    raw = path.read_bytes()
    try:
        stored_authority, manifest = _parse_signed_envelope(raw)
    except ValueError as exc:
        reason = str(exc)
        quarantine_path, quarantine_ref = _quarantine(
            path,
            reason=reason,
            authority=authority,
            data_root=data_root,
        )
        return RecoveryManifestLoadResult(
            status="quarantined",
            reason=reason,
            authority=authority,
            quarantine_path=quarantine_path,
            quarantine_ref=quarantine_ref,
        )
    mismatch = _authority_mismatch(stored_authority, authority)
    if mismatch:
        return RecoveryManifestLoadResult(
            status="held",
            reason=mismatch,
            authority=authority,
            path=path,
        )
    return _loaded_result(
        status="loaded",
        authority=authority,
        manifest=manifest,
        path=path,
        raw=raw,
        data_root=data_root,
    )


def _load_alternative_for_same_root(
    authority: RecoveryAuthorityFrame,
    *,
    expected_path: Path,
    data_root: str | Path | None,
) -> tuple[RecoveryManifestLoadResult | None, bool]:
    different_root = False
    session_directory = _session_directory(authority, data_root)
    if not session_directory.exists():
        return None, different_root
    for candidate in sorted(session_directory.glob("*.json")):
        if candidate == expected_path:
            continue
        candidate = _guard_agent_path(candidate, authority, data_root)
        raw = candidate.read_bytes()
        try:
            stored_authority, _manifest = _parse_signed_envelope(raw)
        except ValueError as exc:
            quarantine_path, quarantine_ref = _quarantine(
                candidate,
                reason=str(exc),
                authority=authority,
                data_root=data_root,
            )
            return (
                RecoveryManifestLoadResult(
                    status="quarantined",
                    reason=str(exc),
                    authority=authority,
                    quarantine_path=quarantine_path,
                    quarantine_ref=quarantine_ref,
                ),
                different_root,
            )
        if stored_authority.session_id != authority.session_id:
            continue
        if stored_authority.root_runtime_task_id != authority.root_runtime_task_id:
            different_root = True
            continue
        mismatch = _authority_mismatch(stored_authority, authority)
        if mismatch:
            return (
                RecoveryManifestLoadResult(
                    status="held",
                    reason=mismatch,
                    authority=authority,
                    path=candidate,
                ),
                different_root,
            )
    return None, different_root


def _legacy_paths(authority: RecoveryAuthorityFrame, data_root: str | Path | None) -> tuple[Path, Path]:
    root = _agent_root(authority, data_root)
    return (
        _guard_agent_path(root / LEGACY_RUNTIME_RECOVERY_MANIFEST_REL_PATH, authority, data_root),
        _guard_agent_path(root / LEGACY_WORKSPACE_RECOVERY_MANIFEST_REL_PATH, authority, data_root),
    )


def _load_legacy(
    authority: RecoveryAuthorityFrame,
    *,
    data_root: str | Path | None,
) -> RecoveryManifestLoadResult | None:
    for legacy_path in _legacy_paths(authority, data_root):
        if not legacy_path.exists():
            continue
        raw = legacy_path.read_bytes()
        try:
            stored_authority, manifest = _parse_signed_envelope(raw)
        except ValueError as exc:
            reason = str(exc)
            quarantine_path, quarantine_ref = _quarantine(
                legacy_path,
                reason=reason,
                authority=authority,
                data_root=data_root,
            )
            return RecoveryManifestLoadResult(
                status="quarantined",
                reason=reason,
                authority=authority,
                quarantine_path=quarantine_path,
                quarantine_ref=quarantine_ref,
            )
        mismatch = _authority_mismatch(stored_authority, authority)
        if mismatch:
            quarantine_path, quarantine_ref = _quarantine(
                legacy_path,
                reason="legacy_authority_mismatch",
                authority=authority,
                data_root=data_root,
            )
            return RecoveryManifestLoadResult(
                status="quarantined",
                reason=mismatch,
                authority=authority,
                quarantine_path=quarantine_path,
                quarantine_ref=quarantine_ref,
            )
        canonical = recovery_manifest_path(authority, data_root=data_root)
        _atomic_write(canonical, raw)
        quarantine_path, quarantine_ref = _quarantine(
            legacy_path,
            reason="legacy_migrated",
            authority=authority,
            data_root=data_root,
        )
        return _loaded_result(
            status="loaded_migrated",
            authority=authority,
            manifest=manifest,
            path=canonical,
            raw=raw,
            data_root=data_root,
            quarantine_path=quarantine_path,
            quarantine_ref=quarantine_ref,
        )
    return None


def load_recovery_manifest(
    authority: RecoveryAuthorityFrame,
    *,
    data_root: str | Path | None = None,
) -> RecoveryManifestLoadResult:
    path = recovery_manifest_path(authority, data_root=data_root)
    if path.exists():
        return _record_load_result(_load_exact(path, authority, data_root=data_root))
    alternative, different_root = _load_alternative_for_same_root(
        authority,
        expected_path=path,
        data_root=data_root,
    )
    if alternative is not None:
        return _record_load_result(alternative)
    legacy = _load_legacy(authority, data_root=data_root)
    if legacy is not None:
        return _record_load_result(legacy)
    return _record_load_result(
        RecoveryManifestLoadResult(
            status="absent",
            reason="different_root_runtime_task" if different_root else None,
            authority=authority,
            path=path,
        )
    )


def persist_recovery_manifest(
    authority: RecoveryAuthorityFrame,
    session_context: Any,
    *,
    data_root: str | Path | None = None,
    delete_if_empty: bool = False,
) -> RecoveryManifestPersistResult:
    runtime_session_id = _text(getattr(session_context, "session_id", None))
    if runtime_session_id != authority.session_id:
        return _record_persist_result(RecoveryManifestPersistResult("held", reason="session_id_mismatch"))
    manifest = build_recovery_manifest(session_context)
    manifest = merge_session_memory_into_manifest(
        manifest,
        agent_id=authority.agent_id,
        data_root=data_root,
    )
    path = recovery_manifest_path(authority, data_root=data_root)
    if manifest.is_empty():
        if delete_if_empty:
            existed = path.exists()
            path.unlink(missing_ok=True)
            if existed:
                _fsync_directory(path.parent)
            return _record_persist_result(RecoveryManifestPersistResult("deleted", paths=(path,)))
        return _record_persist_result(RecoveryManifestPersistResult("skipped", reason="empty_manifest"))
    if permission_profile_snapshot_hash(manifest.permission_profile) != authority.policy_snapshot_hash:
        return _record_persist_result(
            RecoveryManifestPersistResult("held", reason="policy_snapshot_changed_before_persist")
        )
    metadata = getattr(session_context, "metadata", None)
    if isinstance(metadata, dict):
        current_sequence = _metadata_sequence(metadata)
        if current_sequence != authority.base_transcript_sequence:
            return _record_persist_result(
                RecoveryManifestPersistResult("held", reason="base_transcript_sequence_changed_before_persist")
            )
    _atomic_write(path, _envelope_bytes(authority, manifest))
    return _record_persist_result(RecoveryManifestPersistResult("written", paths=(path,)))


__all__ = [
    "RECOVERY_AUTHORITY_SCHEMA",
    "RECOVERY_ENVELOPE_SCHEMA",
    "RecoveryAuthorityFrame",
    "RecoveryAuthorityResolution",
    "RecoveryManifestLoadResult",
    "RecoveryManifestPersistResult",
    "RecoveryManifestResourceReadResult",
    "load_recovery_manifest",
    "persist_recovery_manifest",
    "read_recovery_manifest_resource",
    "recovery_manifest_path",
    "resolve_recovery_authority",
    "unavailable_recovery_result",
]
