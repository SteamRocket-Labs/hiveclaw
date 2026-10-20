"""Current team-memory bodies and source witnesses for employee work."""

from copy import deepcopy
from pathlib import Path
import uuid

from sqlalchemy import or_, select

from app.config import get_settings
from app.core.source_read_purpose import source_recipient_read
from app.database import tenant_scoped_session
from app.models.agent_team import AgentTeam
from app.models.chat_session import ChatSession
from app.models.organization_identity import BusinessTeam, BusinessTeamMembership, ContentInputReceipt
from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor, _validate_input_dependencies
from app.services.session_tool_runtime import _sha256
from app.services.team_memory import TeamMemoryStore, _slugify
from app.services.team_memory_authority import authorize_workspace, parse_workspace


def memory_id(entry):
    return f"team_memory:{entry.workspace_key}:{Path(entry.path).stem}"


def parse_memory_id(value):
    try:
        prefix, kind, raw_id, key = value.split(":", 3)
        workspace = f"{kind}:{raw_id}"
        parse_workspace(workspace)
        if prefix != "team_memory" or not key or key != _slugify(key):
            raise ValueError("noncanonical_team_memory")
        return workspace, key
    except (ValueError, AttributeError, TypeError) as exc:
        raise ContentInputReceiptError("team_memory_identity_invalid", "Use an exact team-memory load reference.", 400) from exc


def source_dependency(receipt):
    return {"source_kind": "team_memory", "source_id": str(receipt.id), "source_version": receipt.content_sha256}


def render_entry(entry):
    return f"[team_memory] id={memory_id(entry)} title={entry.title}\n{entry.content}"


async def _latest(db, tenant_id, carrier_ref):
    return await db.scalar(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == tenant_id,
        ContentInputReceipt.carrier_kind == "team_memory", ContentInputReceipt.carrier_ref == carrier_ref
    ).order_by(ContentInputReceipt.revision.desc()).limit(1).execution_options(populate_existing=True))


def _current_entry(tenant_id, workspace, key, latest):
    entry = TeamMemoryStore().get_entry(str(tenant_id), workspace, key, include_deleted=True)
    if (entry is None or entry.deleted or latest is None or entry.tenant_id != str(tenant_id)
        or entry.workspace_key != workspace or entry.key != key or entry.revision != latest.revision
        or entry.checksum != latest.content_sha256):
        raise ContentInputReceiptError("team_memory_source_unavailable",
            "The current team-memory source is missing, withdrawn or unverified. Preserve it for reconciliation.", 403)
    return entry


async def _workspaces(db, tenant_id, request):
    principal, actor = await _runtime_input_actor(db, tenant_id, request)
    business = await db.scalars(select(BusinessTeam.id).join(BusinessTeamMembership,
        BusinessTeamMembership.business_team_id == BusinessTeam.id).where(
            BusinessTeam.tenant_id == tenant_id, BusinessTeam.status == "active",
            BusinessTeamMembership.tenant_id == tenant_id, BusinessTeamMembership.user_id == actor.id,
            BusinessTeamMembership.status == "active"))
    teams = await db.scalars(select(AgentTeam.id).join(ChatSession, ChatSession.id == AgentTeam.parent_session_id).where(
        AgentTeam.tenant_id == tenant_id, AgentTeam.lead_agent_id == principal.agent_id,
        ChatSession.tenant_id == tenant_id,
        or_(ChatSession.user_id == actor.id, ChatSession.audience_kind == "group")))
    return [*(f"business_team:{value}" for value in business), *(f"agent_team:{value}" for value in teams)]


async def _group_readers(db, tenant_id, request, workspace):
    """A group read proves its actual audience independently of its speaker."""
    from fastapi import HTTPException
    from app.services.shared_group_authority import load_current_group_recipient_users

    principal, _actor = await _runtime_input_actor(db, tenant_id, request)
    session = await db.get(ChatSession, uuid.UUID(principal.session_id), populate_existing=True)
    if session.audience_kind in {None, "private"}:
        return None, ()
    if session.audience_kind != "group":
        raise ContentInputReceiptError("team_memory_audience_unavailable", "The current work audience needs reconciliation.", 503)
    kind, identity = parse_workspace(workspace)
    try:
        _config, recipients = await load_current_group_recipient_users(db, session=session)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise ContentInputReceiptError(detail.get("code", "group_membership_unavailable"),
            "The current group recipients could not be verified.", exc.status_code) from exc
    if kind == "business_team":
        members = set(await db.scalars(select(BusinessTeamMembership.user_id).where(
            BusinessTeamMembership.tenant_id == tenant_id, BusinessTeamMembership.business_team_id == identity,
            BusinessTeamMembership.status == "active", BusinessTeamMembership.user_id.in_([user.id for user in recipients]))))
        if members != {user.id for user in recipients}:
            raise ContentInputReceiptError("team_memory_group_disclosure_denied",
                "The group contains recipients outside this team's current audience.", 403)
    else:
        team = await db.get(AgentTeam, identity, populate_existing=True)
        origin = await db.get(ChatSession, team.parent_session_id, populate_existing=True) if team is not None else None
        if (origin is None or origin.audience_kind != "group" or any(getattr(origin, name) != getattr(session, name)
            for name in ("tenant_id", "source_channel", "group_installation_ref", "group_subject_id"))):
            raise ContentInputReceiptError("team_memory_group_disclosure_denied",
                "Recall this work team's memory within its original audience or authorized private work.", 403)
    return session.id, tuple(recipients)


async def _check_sources(db, tenant_id, request, workspace, receipt, *, active_receipts):
    group_id, recipients = await _group_readers(db, tenant_id, request, workspace)
    await _validate_input_dependencies(db, tenant_id, receipt.dependencies_json, runtime_request=request,
        active_workspace_receipts=active_receipts)
    for recipient in recipients:
        with source_recipient_read(session_id=group_id, user_id=recipient.id):
            await _validate_input_dependencies(db, tenant_id, receipt.dependencies_json, user=recipient,
                active_workspace_receipts=active_receipts)
    current_group, current_recipients = await _group_readers(db, tenant_id, request, workspace)
    if current_group != group_id or {user.id for user in recipients} != {user.id for user in current_recipients}:
        raise ContentInputReceiptError("team_memory_audience_changed", "The current audience changed while reading the source.", 403)


async def authorize_entry(db, tenant_id, request, entry, receipt, *, active_receipts=frozenset()):
    """Recheck one exact observed body after any backend or selector await."""
    workspace, key = entry.workspace_key, Path(entry.path).stem
    await authorize_workspace(db, tenant_id, workspace, runtime_request=request)
    if (receipt is None or receipt.tenant_id != tenant_id or receipt.carrier_kind != "team_memory"
        or receipt.carrier_ref != f"{workspace}\n{key}" or receipt.revision != entry.revision
        or receipt.content_sha256 != entry.checksum or receipt.id in active_receipts):
        raise ContentInputReceiptError("team_memory_source_unverified", "This body has no current team source witness.", 403)
    version = _sha256(deepcopy(receipt.dependencies_json))
    observed = (entry.content, entry.title, entry.revision, entry.checksum)
    latest = await _latest(db, tenant_id, receipt.carrier_ref)
    current = _current_entry(tenant_id, workspace, key, latest)
    if latest.id != receipt.id or observed != (current.content, current.title, current.revision, current.checksum):
        raise ContentInputReceiptError("team_memory_source_changed", "The team-memory version changed while reading.", 403)
    await _check_sources(db, tenant_id, request, workspace, receipt, active_receipts=active_receipts | {receipt.id})
    await authorize_workspace(db, tenant_id, workspace, runtime_request=request)
    latest = await _latest(db, tenant_id, receipt.carrier_ref)
    current = _current_entry(tenant_id, workspace, key, latest)
    if (latest.id != receipt.id or _sha256(receipt.dependencies_json) != version
        or observed != (current.content, current.title, current.revision, current.checksum)):
        raise ContentInputReceiptError("team_memory_source_changed", "The team-memory version changed while reading.", 403)
    return current


async def read_entries(*, tenant_id, request, ids=None, query=None, data_root=None, issues=None, session_factory=None):
    configured = Path(get_settings().AGENT_DATA_DIR).resolve()
    if data_root is not None and Path(data_root).resolve() != configured:
        raise ContentInputReceiptError("team_memory_root_unavailable", "The configured memory root needs reconciliation.", 503)
    store = TeamMemoryStore(data_root=configured)
    pairs = []
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        original, _actor = await _runtime_input_actor(db, tenant_id, request)
        targets = {}
        if ids is not None:
            for value in ids:
                workspace, key = parse_memory_id(value)
                targets.setdefault(workspace, []).append(key)
        else:
            targets = {workspace: None for workspace in await _workspaces(db, tenant_id, request)}
        for workspace, keys in targets.items():
            scope_verified = False
            try:
                await authorize_workspace(db, tenant_id, workspace, runtime_request=request)
                scope_verified = True
                listed = (store.list_entries(str(tenant_id), workspace) if keys is None else
                    [entry for key in keys if (entry := store.get_entry(str(tenant_id), workspace, key)) is not None])
                for listed_entry in listed:
                    try:
                        key = Path(listed_entry.path).stem
                        receipt = await _latest(db, tenant_id, f"{workspace}\n{key}")
                        current = _current_entry(tenant_id, workspace, key, receipt)
                        await authorize_entry(db, tenant_id, request, current, receipt)
                        if query and query.lower() not in f"{current.title}\n{current.content}".lower():
                            continue
                        pairs.append((current, receipt))
                    except ContentInputReceiptError:
                        if issues is not None:
                            issue = {"status": "unavailable", "code": "team_memory_source_unavailable"}
                            if issue not in issues:
                                issues.append(issue)
            except ContentInputReceiptError as exc:
                if issues is not None and (scope_verified or exc.status_code not in {403, 404}):
                    issue = {"status": "unavailable", "code": "team_memory_source_unavailable"}
                    if issue not in issues:
                        issues.append(issue)
        current, _actor = await _runtime_input_actor(db, tenant_id, request)
        if current != original:
            raise ContentInputReceiptError("team_memory_reader_changed", "The current memory reader changed.", 403)
    return pairs


async def validate_dependency(db, tenant_id, dependency, *, user=None, runtime_request=None, active_receipts=frozenset()):
    try:
        receipt = await db.get(ContentInputReceipt, uuid.UUID(dependency["source_id"]), populate_existing=True)
        if (receipt is None or receipt.tenant_id != tenant_id or receipt.carrier_kind != "team_memory"
            or receipt.content_sha256 != dependency["source_version"] or receipt.id in active_receipts):
            raise ValueError("team_memory_receipt_unavailable")
        workspace, key = receipt.carrier_ref.split("\n", 1)
        parse_workspace(workspace)
        if key != _slugify(key):
            raise ValueError("team_memory_receipt_unavailable")
    except (ValueError, KeyError, AttributeError, TypeError) as exc:
        raise ContentInputReceiptError("team_memory_source_unverified", "The team source receipt needs recovery.", 403) from exc
    await authorize_workspace(db, tenant_id, workspace, user=user, runtime_request=runtime_request)
    latest = await _latest(db, tenant_id, receipt.carrier_ref)
    _current_entry(tenant_id, workspace, key, latest)
    if latest.revision < receipt.revision:
        raise ContentInputReceiptError("team_memory_source_changed", "The team source has been rewound.", 403)
    version = _sha256(deepcopy(receipt.dependencies_json))
    if runtime_request is not None:
        await _check_sources(db, tenant_id, runtime_request, workspace, receipt, active_receipts=active_receipts | {receipt.id})
    else:
        await _validate_input_dependencies(db, tenant_id, receipt.dependencies_json, user=user,
            active_workspace_receipts=active_receipts | {receipt.id})
    await authorize_workspace(db, tenant_id, workspace, user=user, runtime_request=runtime_request)
    latest = await _latest(db, tenant_id, receipt.carrier_ref)
    _current_entry(tenant_id, workspace, key, latest)
    if latest.revision < receipt.revision or _sha256(receipt.dependencies_json) != version:
        raise ContentInputReceiptError("team_memory_source_changed", "The team source changed while checking access.", 403)
