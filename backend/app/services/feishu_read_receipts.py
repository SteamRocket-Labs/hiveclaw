"""Bind successful OpenAPI reads to their actual connection and private reader.

These are data-source receipts, never tool grants. The governed executor still
authorizes every action, and the original session remains the source audience.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import uuid

from sqlalchemy import select

from app.models.channel_config import ChannelConfig
from app.models.tenant_channel_config import TenantChannelConfig

logger = logging.getLogger(__name__)
FEISHU_READ_TOOLS = frozenset({"feishu_doc_read", "feishu_approval_query"})
_active_read: ContextVar = ContextVar("feishu_source_read", default=None)


def _connection_snapshot(config):
    return {"id": str(config.id), "tenant_id": str(config.tenant_id),
        "scope": "company" if isinstance(config, TenantChannelConfig) else "employee",
        "agent_id": str(config.agent_id) if isinstance(config, ChannelConfig) else None,
        "app_id": config.app_id, "updated_at": config.updated_at.astimezone(timezone.utc).isoformat()}


@dataclass
class FeishuReadCapture:
    context: object
    tool_name: str
    arguments: dict
    connections: list[dict] = field(default_factory=list)
    result_hashes: set[str] = field(default_factory=set)


@contextmanager
def capture_feishu_read(context, tool_name, arguments):
    capture = FeishuReadCapture(context, tool_name, dict(arguments))
    token = _active_read.set(capture)
    try:
        yield capture
    finally:
        _active_read.reset(token)


def observe_feishu_connection(config):
    capture = _active_read.get()
    if capture is not None and config is not None:
        snapshot = _connection_snapshot(config)
        if snapshot not in capture.connections:
            capture.connections.append(snapshot)


def attest_feishu_api_read(result):
    """Witness only a successful result at the concrete OpenAPI read boundary."""
    from app.services.session_model_inputs import _hash

    capture = _active_read.get()
    if capture is not None:
        capture.result_hashes.add(_hash(str(result)))
    return result


async def settle_feishu_read(capture, result):
    """Retain exact executor evidence without changing the model's tool result."""
    from app.database import tenant_scoped_session
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.connector_acl import extract_connector_source_items
    from app.services.session_model_inputs import _hash

    context = capture.context
    if context is None or capture.tool_name not in FEISHU_READ_TOOLS or not all((
        context.user_id, context.tenant_id, context.session_id, context.runtime_task_id, context.tool_call_id,
    )) or len(capture.connections) != 1 or _hash(str(result)) not in capture.result_hashes:
        return result
    sources = extract_connector_source_items(result)
    if not sources or any(
        item.get("metadata", {}).get("acl_authority") != "connector_verified"
        or item.get("metadata", {}).get("connector") != "feishu"
        or item.get("metadata", {}).get("accessing_agent_id") != str(context.agent_id)
        or item.get("acl", {}).get("user_ids") != [str(context.user_id)]
        or item.get("acl", {}).get("tenant_ids") != [str(context.tenant_id)]
        for item in sources
    ):
        return result
    try:
        tenant, session, run = (uuid.UUID(str(v)) for v in (context.tenant_id, context.session_id, context.runtime_task_id))
        async with tenant_scoped_session(tenant) as db:
            invocation = await db.scalar(select(SessionToolInvocation).where(
                SessionToolInvocation.tenant_id == tenant, SessionToolInvocation.session_id == session,
                SessionToolInvocation.run_id == run, SessionToolInvocation.provider_tool_use_id == context.tool_call_id,
                SessionToolInvocation.tool_name == capture.tool_name))
            model = await db.scalar(select(SessionModelResult).where(
                SessionModelResult.tenant_id == tenant, SessionModelResult.session_id == session,
                SessionModelResult.run_id == run,
                SessionModelResult.provider_request_id == invocation.provider_request_id)) if invocation else None
            args_hash = _hash({"tool_name": capture.tool_name, "arguments": capture.arguments})
            if model is None or invocation.result_event_id is not None or (
                invocation.effective_args_hash or invocation.args_hash) != args_hash:
                return result
            proof = {"schema": "hive.feishu_read.v1", "invocation_id": str(invocation.id),
                "model_result_id": str(model.id), "model_request_hash": model.model_request_hash,
                "authority_snapshot_hash": invocation.authority_snapshot_hash,
                "effective_args_hash": args_hash, "content_hash": _hash(str(result)),
                "connection": capture.connections[0], "reader_id": str(context.user_id), "sources": sources}
            proof["receipt_hash"] = _hash(proof)
            evidence_id = uuid.uuid5(invocation.id, "feishu-read-evidence")
            existing = await db.get(InvocationSpan, evidence_id)
            if existing is not None:
                if existing.metadata_json != {"feishu_read": proof}:
                    return result
            else:
                db.add(InvocationSpan(id=evidence_id, tenant_id=tenant, agent_id=context.agent_id,
                    user_id=context.user_id, runtime_task_id=run, session_id=str(session),
                    trace_id=f"session-tool:{invocation.id}", span_id=f"feishu-read:{invocation.id}",
                    span_type="feishu_read", name=capture.tool_name, status="captured", duration_ms=0,
                    started_at=datetime.now(timezone.utc), input_hash=args_hash, metadata_json={"feishu_read": proof}))
            await db.commit()
    except Exception as exc:
        logger.warning("Feishu read evidence unavailable (%s)", type(exc).__name__)
    return result


async def load_feishu_read(db, *, tenant_id, agent_id, invocation, content_hash, current=False):
    """Load an exact sealed read; optionally recheck its actual installation."""
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult
    from app.services.session_model_inputs import _hash

    if invocation.tool_name not in FEISHU_READ_TOOLS or invocation.effect_state != "effect_committed":
        return None
    row = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "feishu-read-evidence"), populate_existing=True)
    proof = (row.metadata_json or {}).get("feishu_read") if row else None
    model = await db.scalar(select(SessionModelResult).where(SessionModelResult.tenant_id == tenant_id,
        SessionModelResult.session_id == invocation.session_id, SessionModelResult.run_id == invocation.run_id,
        SessionModelResult.provider_request_id == invocation.provider_request_id))
    if not isinstance(proof, dict) or model is None or (
        row.tenant_id, row.agent_id, row.session_id, row.runtime_task_id, row.span_type, row.name, row.status,
    ) != (tenant_id, agent_id, str(invocation.session_id), invocation.run_id, "feishu_read", invocation.tool_name, "captured"):
        return None
    if (proof.get("schema") != "hive.feishu_read.v1" or proof.get("reader_id") != str(row.user_id)
        or proof.get("invocation_id") != str(invocation.id) or proof.get("model_result_id") != str(model.id)
        or proof.get("model_request_hash") != model.model_request_hash
        or _hash(model.model_request_snapshot_json) != model.model_request_hash
        or proof.get("authority_snapshot_hash") != invocation.authority_snapshot_hash
        or proof.get("effective_args_hash") != _hash({"tool_name": invocation.tool_name,
            "arguments": invocation.effective_arguments_json or invocation.provider_arguments_json})
        or proof.get("content_hash") != content_hash
        or proof.get("receipt_hash") != _hash({k: v for k, v in proof.items() if k != "receipt_hash"})):
        return None
    connection = proof.get("connection")
    sources = proof.get("sources")
    if (not isinstance(connection, dict) or connection.get("tenant_id") != str(tenant_id)
        or connection.get("scope") not in {"company", "employee"} or not isinstance(sources, list) or not sources):
        return None
    if any(not isinstance(item, dict) or item.get("metadata", {}).get("connector") != "feishu"
        or item.get("metadata", {}).get("acl_authority") != "connector_verified"
        or item.get("metadata", {}).get("accessing_agent_id") != str(agent_id)
        or item.get("acl", {}).get("user_ids") != [proof["reader_id"]]
        or item.get("acl", {}).get("tenant_ids") != [str(tenant_id)] for item in sources):
        return None
    if current:
        try:
            config_id = uuid.UUID(connection["id"])
        except (ValueError, KeyError, TypeError):
            return None
        config_type = TenantChannelConfig if connection.get("scope") == "company" else ChannelConfig
        config = await db.get(config_type, config_id, populate_existing=True)
        if (config is None or config.channel_type != "feishu" or not config.is_configured
            or not config.app_id or not config.app_secret or config.tenant_id != tenant_id
            or isinstance(config, ChannelConfig) and config.agent_id != agent_id
            or _connection_snapshot(config) != connection):
            return None
    return proof
