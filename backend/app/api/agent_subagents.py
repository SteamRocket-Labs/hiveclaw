"""Subagent configuration API (cut C2, §12.7) — the human door onto the stores.

Eight endpoints over the C1 resolution chain: four agent-level (the daily
driver, ``check_agent_access`` guarded — reads need access, writes need
manage) and four tenant-level (the company library, org-admin guarded).
Markdown is the configuration format end to end: PUT accepts the full
定义.md text and validates it through the same parser the runtime uses —
no second schema, no drift.

Invariant (§12.9): this surface adds zero runtime power. Tool-face
narrowing, capability gates, recursion guards, and governed memory writes
are enforced at spawn time regardless of how a definition got written.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.subagent import _TYPE_PRESETS, SubagentSpec, builtin_type_description, builtin_type_prompt
from app.agents.subagent_definition import (
    SCOPE_AGENT,
    SCOPE_BUILTIN,
    SCOPE_TENANT,
    SubagentDefinitionStore,
    definition_store_for_agent,
    definition_store_for_tenant,
    list_subagent_definitions,
    parse_subagent_definition,
    render_subagent_definition,
    resolve_subagent_definition,
)
from app.agents.subagent_evolution import apply_proposal, proposal_store_for_agent, reject_proposal
from app.agents.subagent_memory import memory_store_for_agent, memory_store_for_tenant
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.subagent_generator import SubagentGenerationError, generate_subagent_definition

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents/{agent_id}/subagents", tags=["subagents"])
enterprise_router = APIRouter(prefix="/enterprise/subagents", tags=["enterprise"])


class SubagentDefinitionPayload(BaseModel):
    definition: str


class SubagentGeneratePayload(BaseModel):
    description: str


async def _resolve_generation_model_config(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    agent=None,
) -> dict:
    """Model config for the generation LLM: the agent's primary model when
    generating on an agent page, else the tenant's first enabled model."""

    from sqlalchemy import select

    from app.models.llm import LLMModel

    model = None
    if agent is not None and getattr(agent, "primary_model_id", None):
        model = (
            await db.execute(
                select(LLMModel).where(
                    LLMModel.id == agent.primary_model_id,
                    LLMModel.tenant_id == tenant_id,
                    LLMModel.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
    if model is None and tenant_id is not None:
        model = (
            await db.execute(
                select(LLMModel)
                .where(LLMModel.tenant_id == tenant_id, LLMModel.enabled.is_(True))
                .order_by(LLMModel.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=503, detail="No enabled model available for AI generation")
    return {"provider": model.provider, "api_key": model.api_key, "model": model.model, "base_url": model.base_url}


async def _generate_definition_response(
    description: str,
    *,
    model_config: dict,
    agent_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
) -> dict:
    value = description.strip()
    if not value:
        raise HTTPException(status_code=422, detail="description is required")
    existing = [row["name"] for row in list_subagent_definitions(agent_id=agent_id, tenant_id=tenant_id)]
    try:
        definition = await generate_subagent_definition(
            value,
            model_config=model_config,
            existing_names=existing,
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
    except SubagentGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"definition": definition}


def _spec_summary(spec: SubagentSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "type": spec.type,
        "model": spec.model,
        "isolation": spec.isolation,
        "max_tool_rounds": spec.max_tool_rounds,
        "allowed_tools": list(spec.allowed_tools),
        "excluded_tools": list(spec.excluded_tools),
    }


def _memory_summary(store, name: str) -> dict:
    """Existence + entry-count digest of a 记忆.md (§12.7: presence/summary, not body)."""

    try:
        text = store.load(name)
    except ValueError:
        return {"exists": False, "entries": 0}
    if not text:
        return {"exists": False, "entries": 0}
    return {"exists": True, "entries": sum(1 for line in text.splitlines() if line.startswith("- ["))}


def _builtin_detail(name: str) -> dict | None:
    preset = _TYPE_PRESETS.get(name)
    if preset is None:
        return None
    # The template body is the same baseline prompt the runtime injects at
    # spawn time (builtin_type_prompt), and the description is the same
    # whenToUse the spawn surface advertises — what humans read here is
    # exactly what an unspecialized subagent of this type runs with.
    template = SubagentSpec(
        name=name,
        description=builtin_type_description(name),
        type=name,
        system_prompt=builtin_type_prompt(name),
    )
    return {
        "name": name,
        "scope": SCOPE_BUILTIN,
        "definition": render_subagent_definition(template),
        "spec": {**_spec_summary(template), "allowed_tools": list(preset)},
        "memory": {"exists": False, "entries": 0},
    }


def _parse_validated(name: str, payload: SubagentDefinitionPayload) -> SubagentSpec:
    """Parse + validate a PUT body against the URL name; HTTP 422 on any mismatch."""

    try:
        spec = parse_subagent_definition(payload.definition)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if spec.name != name:
        raise HTTPException(
            status_code=422,
            detail=f"frontmatter name {spec.name!r} mismatches URL name {name!r}",
        )
    return spec


def _save_and_respond(store: SubagentDefinitionStore, spec: SubagentSpec, scope: str) -> dict:
    try:
        store.save(spec)
    except ValueError as exc:  # name guard double-checks at the path layer
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"name": spec.name, "scope": scope, "spec": _spec_summary(spec)}


# --- agent-level surface (daily driver, §12.2) --------------------------------


@router.get("")
async def list_agent_subagents(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    rows = list_subagent_definitions(agent_id=agent_id, tenant_id=agent.tenant_id)
    # Evolution loop (§4.3): pending-proposal badge for agent-level rows + the
    # agent's approval-mode switch state for the settings toggle.
    proposal_store = proposal_store_for_agent(agent_id)
    for row in rows:
        row["pending_proposal"] = row["scope"] == SCOPE_AGENT and proposal_store.load_pending(row["name"]) is not None
    return {
        "subagents": rows,
        "evolution_auto_approve": bool(getattr(agent, "subagent_evolution_auto_approve", False)),
    }


@router.get("/{name}")
async def get_agent_subagent(
    agent_id: uuid.UUID,
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    try:
        resolved = resolve_subagent_definition(name, agent_id=agent_id, tenant_id=agent.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if resolved is None:
        builtin = _builtin_detail(name)
        if builtin is None:
            raise HTTPException(status_code=404, detail="Subagent definition not found")
        return builtin

    if resolved.scope == SCOPE_AGENT:
        memory = _memory_summary(memory_store_for_agent(agent_id), name)
    else:
        memory = _memory_summary(memory_store_for_tenant(agent.tenant_id), name)
    payload = {
        "name": resolved.spec.name,
        "scope": resolved.scope,
        "definition": render_subagent_definition(resolved.spec),
        "spec": _spec_summary(resolved.spec),
        "memory": memory,
    }
    if resolved.scope == SCOPE_AGENT:
        pending = proposal_store_for_agent(agent_id).load_pending(name)
        if pending is not None:
            # Diff view: the base body lives in `definition`; this is the revision.
            payload["proposal"] = {
                "proposal_id": pending.proposal_id,
                "rationale": pending.rationale,
                "absorbed_entry_ids": pending.absorbed_entry_ids,
                "created_at": pending.created_at,
                "body": pending.body,
            }
    return payload


@router.post("/generate")
async def generate_agent_subagent(
    agent_id: uuid.UUID,
    payload: SubagentGeneratePayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AI-generate a complete 定义.md from a natural-language description.

    The result prefills the editor — the human stays the final confirmation
    gate, and saving still round-trips the same PUT validation chain.
    """

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Generating subagent definitions requires manage access")
    model_config = await _resolve_generation_model_config(db, tenant_id=agent.tenant_id, agent=agent)
    return await _generate_definition_response(
        payload.description,
        model_config=model_config,
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
    )


@router.put("/{name}")
async def put_agent_subagent(
    agent_id: uuid.UUID,
    name: str,
    payload: SubagentDefinitionPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Editing subagent definitions requires manage access")
    spec = _parse_validated(name, payload)
    return _save_and_respond(definition_store_for_agent(agent_id), spec, SCOPE_AGENT)


class EvolutionModePayload(BaseModel):
    auto_approve: bool


@router.post("/evolution-mode")
async def set_evolution_mode(
    agent_id: uuid.UUID,
    payload: EvolutionModePayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approval-mode switch (§4.3): auto-apply improvement proposals or hold
    them for manual review. Auto mode skips only the human click — validation,
    contract freeze, and ledger audit are identical."""

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Changing evolution mode requires manage access")
    agent.subagent_evolution_auto_approve = payload.auto_approve
    await db.commit()
    return {"evolution_auto_approve": payload.auto_approve}


@router.post("/{name}/proposal/approve")
async def approve_subagent_proposal(
    agent_id: uuid.UUID,
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Approving proposals requires manage access")
    try:
        result = apply_proposal(agent_id, name, approved_by=str(current_user.id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not result.applied:
        if result.error == "no_pending":
            raise HTTPException(status_code=404, detail="No pending proposal for this definition")
        if result.error == "stale_base":
            raise HTTPException(
                status_code=409,
                detail="Definition changed after the proposal was drafted — proposal closed; a new one "
                "will be nominated from current state",
            )
        raise HTTPException(status_code=422, detail=f"Proposal could not be applied: {result.error}")
    return {
        "applied": True,
        "proposal_id": result.proposal_id,
        "absorbed_marked": result.absorbed_marked,
    }


@router.post("/{name}/proposal/reject")
async def reject_subagent_proposal(
    agent_id: uuid.UUID,
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Rejecting proposals requires manage access")
    if not reject_proposal(agent_id, name, rejected_by=str(current_user.id)):
        raise HTTPException(status_code=404, detail="No pending proposal for this definition")
    return {"rejected": True}


@router.delete("/{name}")
async def delete_agent_subagent(
    agent_id: uuid.UUID,
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Deleting subagent definitions requires manage access")
    if current_user.role not in ("org_admin", "platform_admin"):
        raise HTTPException(
            status_code=403,
            detail="Sub-agent definition is an enterprise asset; only an admin can delete it.",
        )
    try:
        deleted = definition_store_for_agent(agent_id).delete(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not deleted:
        # Tenant-level definitions are not deletable through the agent surface;
        # after an agent-level delete the name falls back to tenant scope.
        raise HTTPException(status_code=404, detail="No agent-level definition with this name")
    return {"deleted": name, "scope": SCOPE_AGENT}


# --- tenant-level company library (§12.3, org-admin curated) -------------------


def _require_tenant_admin(current_user: User) -> uuid.UUID:
    """Org-admin guard for the company library; returns the curated tenant id."""

    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Requires admin privileges")
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No company assigned")
    return current_user.tenant_id


@enterprise_router.get("")
async def list_tenant_subagents(current_user: User = Depends(get_current_user)):
    tenant_id = _require_tenant_admin(current_user)
    return {"subagents": list_subagent_definitions(agent_id=None, tenant_id=tenant_id)}


@enterprise_router.get("/{name}")
async def get_tenant_subagent(
    name: str,
    current_user: User = Depends(get_current_user),
):
    """Full definition text for the edit flow — list rows carry only the spec
    summary, and reconstructing frontmatter from them would drop the body."""

    tenant_id = _require_tenant_admin(current_user)
    try:
        spec = definition_store_for_tenant(tenant_id).load(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if spec is None:
        raise HTTPException(status_code=404, detail="No tenant-level definition with this name")
    return {
        "name": spec.name,
        "scope": SCOPE_TENANT,
        "definition": render_subagent_definition(spec),
        "spec": _spec_summary(spec),
        "memory": _memory_summary(memory_store_for_tenant(tenant_id), name),
    }


@enterprise_router.post("/generate")
async def generate_tenant_subagent(
    payload: SubagentGeneratePayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = _require_tenant_admin(current_user)
    model_config = await _resolve_generation_model_config(db, tenant_id=tenant_id)
    return await _generate_definition_response(
        payload.description,
        model_config=model_config,
        agent_id=None,
        tenant_id=tenant_id,
    )


@enterprise_router.put("/{name}")
async def put_tenant_subagent(
    name: str,
    payload: SubagentDefinitionPayload,
    current_user: User = Depends(get_current_user),
):
    tenant_id = _require_tenant_admin(current_user)
    spec = _parse_validated(name, payload)
    return _save_and_respond(definition_store_for_tenant(tenant_id), spec, SCOPE_TENANT)


@enterprise_router.delete("/{name}")
async def delete_tenant_subagent(
    name: str,
    current_user: User = Depends(get_current_user),
):
    tenant_id = _require_tenant_admin(current_user)
    try:
        deleted = definition_store_for_tenant(tenant_id).delete(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="No tenant-level definition with this name")
    return {"deleted": name, "scope": SCOPE_TENANT}
