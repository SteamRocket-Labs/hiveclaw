"""Registered workflow definition lifecycle (§9 P6).

§3.1: ephemeral and registered share ONE engine; the only difference is
definition lifecycle. This service owns that lifecycle:

* versions are immutable — content changes mint a new version row
  (DB-anchored by ``uq_workflow_definition_version``);
* status machine: ``draft → active → deprecated | revoked``; execution
  resolution refuses draft/revoked, and deprecated only with explicit
  ``allow_deprecated`` (the P8 pinned-trigger continuation path);
* §10 decision 5 — visibility ≠ executability: ``visibility_scope`` governs
  who SEES a definition, ``call_policy`` (allowed_agents / allowed_orgs /
  allowed_roles) governs who may RUN it; leaf capabilities are still checked
  per run on top;
* promoted records are executable only when linked to a matching APPROVED
  immutable run-evidence proposal; legacy direct promotions are quarantined;
* fork: registered version + patch → ephemeral definition DATA for a one-off
  run; the registered record is never mutated.

Every session is tenant-scoped (the table is FORCE-RLS).
"""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import tenant_scoped_session
from app.models.workflow import WorkflowDefinitionRecord, WorkflowPromotionProposal
from app.runtime.workflow_compiler import CompiledWorkflow, WorkflowCompileError, compile_workflow
from app.runtime.workflow_definition import compute_definition_hash


logger = logging.getLogger(__name__)


class WorkflowDefinitionError(ValueError):
    """Lifecycle / visibility / policy violation — the operation must not proceed."""


@dataclass(slots=True)
class ResolvedDefinition:
    record: WorkflowDefinitionRecord
    compiled: CompiledWorkflow
    asset_ref: object | None = None


class WorkflowDefinitionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    def _session(self, tenant_id: uuid.UUID | str | None):
        return tenant_scoped_session(str(tenant_id) if tenant_id else None, session_factory=self._session_factory)

    async def _require_governed_promotion(
        self,
        record: WorkflowDefinitionRecord,
        *,
        tenant_id: uuid.UUID,
    ) -> None:
        if record.promoted_from_run_id is None:
            return
        if record.promotion_proposal_id is None:
            raise WorkflowDefinitionError(
                "legacy direct Workflow promotion has no immutable promotion proposal and is quarantined"
            )
        async with self._session(tenant_id) as session:
            proposal = (
                await session.execute(
                    select(WorkflowPromotionProposal).where(
                        WorkflowPromotionProposal.id == record.promotion_proposal_id,
                        WorkflowPromotionProposal.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        if (
            proposal is None
            or proposal.status != "approved"
            or proposal.run_id != record.promoted_from_run_id
            or proposal.definition_hash != record.definition_hash
            or proposal.definition_json != record.definition_json
            or proposal.agent_id not in {record.owner_id, record.created_by_agent_id}
            or compute_definition_hash(record.definition_json) != record.definition_hash
        ):
            raise WorkflowDefinitionError(
                "promoted Workflow is not bound to a matching approved promotion proposal and is quarantined"
            )

    # ── creation / lifecycle ─────────────────────────────────────

    async def create_draft(
        self,
        *,
        tenant_id: uuid.UUID,
        definition_data: dict,
        created_by_user_id: uuid.UUID | None = None,
        created_by_agent_id: uuid.UUID | None = None,
        visibility_scope: str = "agent",
        owner_type: str = "user",
        owner_id: uuid.UUID | None = None,
        call_policy: dict | None = None,
        promoted_from_run_id: uuid.UUID | None = None,
    ) -> WorkflowDefinitionRecord:
        if visibility_scope == "platform":
            raise WorkflowDefinitionError(
                "platform scope is factory-curated read-only (§10 decision 5); tenants cannot create it"
            )
        compiled = compile_workflow(definition_data)  # structural validation up front
        name = compiled.definition.name

        async with self._session(tenant_id) as session:
            current_max = (
                await session.execute(
                    select(WorkflowDefinitionRecord.definition_version)
                    .where(
                        WorkflowDefinitionRecord.tenant_id == tenant_id,
                        WorkflowDefinitionRecord.name == name,
                    )
                    .order_by(WorkflowDefinitionRecord.definition_version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            record = WorkflowDefinitionRecord(
                tenant_id=tenant_id,
                name=name,
                definition_version=(current_max or 0) + 1,
                definition_hash=compute_definition_hash(definition_data),
                definition_json=definition_data,
                status="draft",
                visibility_scope=visibility_scope,
                owner_type=owner_type,
                owner_id=owner_id,
                call_policy=call_policy,
                created_by_user_id=created_by_user_id,
                created_by_agent_id=created_by_agent_id,
                promoted_from_run_id=promoted_from_run_id,
            )
            session.add(record)
            await session.flush()
            await session.refresh(record)
            from app.services.ai_asset_adapters import project_workflow
            from app.services.ai_assets import register_projection

            await register_projection(
                session,
                project_workflow(record),
                change_source="create",
                actor_user_id=created_by_user_id,
                actor_agent_id=created_by_agent_id,
                change_message="Workflow draft created",
            )
        return record

    async def get_record(self, definition_id: uuid.UUID, *, tenant_id: uuid.UUID) -> WorkflowDefinitionRecord:
        async with self._session(tenant_id) as session:
            record = (
                await session.execute(
                    select(WorkflowDefinitionRecord).where(WorkflowDefinitionRecord.id == definition_id)
                )
            ).scalar_one_or_none()
        if record is None:
            raise WorkflowDefinitionError(f"definition {definition_id} not found")
        return record

    async def _transition(
        self,
        definition_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        to_status: str,
        allowed_from: tuple[str, ...],
        actor_user_id: uuid.UUID | None = None,
    ) -> WorkflowDefinitionRecord:
        async with self._session(tenant_id) as session:
            record = (
                await session.execute(
                    select(WorkflowDefinitionRecord).where(WorkflowDefinitionRecord.id == definition_id)
                )
            ).scalar_one_or_none()
            if record is None:
                raise WorkflowDefinitionError(f"definition {definition_id} not found")
            if record.status not in allowed_from:
                raise WorkflowDefinitionError(f"cannot move definition from {record.status!r} to {to_status!r}")
            record.status = to_status
            await session.flush()
            await session.refresh(record)
            from app.services.ai_asset_adapters import project_workflow
            from app.services.ai_assets import register_projection

            await register_projection(
                session,
                project_workflow(record),
                change_source={"active": "publish", "deprecated": "deprecate", "revoked": "revoke"}[to_status],
                actor_user_id=actor_user_id,
                change_message=f"Workflow moved to {to_status}",
            )
        return record

    async def activate(
        self,
        definition_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        allowed_leaves: set[str] | None = None,
    ) -> WorkflowDefinitionRecord:
        record = await self.get_record(definition_id, tenant_id=tenant_id)
        await self._require_governed_promotion(record, tenant_id=tenant_id)
        try:
            compile_workflow(record.definition_json, known_leaves=allowed_leaves)
        except WorkflowCompileError as exc:
            raise WorkflowDefinitionError(f"activation pre-check failed: {exc}") from exc
        return await self._transition(
            definition_id,
            tenant_id=tenant_id,
            to_status="active",
            allowed_from=("draft",),
            actor_user_id=actor_user_id,
        )

    async def deprecate(
        self, definition_id: uuid.UUID, *, tenant_id: uuid.UUID, actor_user_id: uuid.UUID | None = None
    ) -> WorkflowDefinitionRecord:
        return await self._transition(
            definition_id,
            tenant_id=tenant_id,
            to_status="deprecated",
            allowed_from=("active",),
            actor_user_id=actor_user_id,
        )

    async def revoke(
        self, definition_id: uuid.UUID, *, tenant_id: uuid.UUID, actor_user_id: uuid.UUID | None = None
    ) -> WorkflowDefinitionRecord:
        return await self._transition(
            definition_id,
            tenant_id=tenant_id,
            to_status="revoked",
            allowed_from=("draft", "active", "deprecated"),
            actor_user_id=actor_user_id,
        )

    # ── visibility / execution resolution (§10 decision 5) ───────

    async def list_definitions(
        self, *, tenant_id: uuid.UUID, agent_id: uuid.UUID | None = None
    ) -> list[WorkflowDefinitionRecord]:
        """Everything VISIBLE to this agent: platform templates (tenant NULL),
        tenant/org-scoped records, and the agent's own agent-scoped ones."""
        async with self._session(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        select(WorkflowDefinitionRecord).order_by(
                            WorkflowDefinitionRecord.name, WorkflowDefinitionRecord.definition_version
                        )
                    )
                )
                .scalars()
                .all()
            )
        visible: list[WorkflowDefinitionRecord] = []
        for record in rows:
            if record.visibility_scope == "agent":
                if agent_id is None:
                    continue
                if record.owner_id != agent_id and record.created_by_agent_id != agent_id:
                    continue
            visible.append(record)
        return visible

    @staticmethod
    def _may_execute(record: WorkflowDefinitionRecord, agent_id: uuid.UUID) -> bool:
        policy = record.call_policy or {}
        unsupported_policy_keys = [key for key in ("allowed_roles", "allowed_orgs") if policy.get(key) is not None]
        if unsupported_policy_keys:
            raise WorkflowDefinitionError(
                "call_policy "
                + ", ".join(unsupported_policy_keys)
                + " requires actor role/org context; refusing to execute without it"
            )
        allowed_agents = policy.get("allowed_agents")
        if allowed_agents is not None:
            return str(agent_id) in {str(a) for a in allowed_agents}
        if record.visibility_scope == "agent":
            if record.owner_id is None and record.created_by_agent_id is None:
                # User-created with no agent owner recorded: no owner
                # constraint to enforce (tenant isolation already applied).
                return True
            return record.owner_id == agent_id or record.created_by_agent_id == agent_id
        # tenant / org / platform scopes default to executable when no
        # explicit call_policy restricts them (leaf capabilities still gate
        # per run).
        return True

    async def resolve_for_execution(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        agent_id: uuid.UUID,
        version: int | None = None,
        definition_hash: str | None = None,
        allow_deprecated: bool = False,
    ) -> ResolvedDefinition:
        async with self._session(tenant_id) as session:
            query = select(WorkflowDefinitionRecord).where(WorkflowDefinitionRecord.name == name)
            if version is not None:
                query = query.where(WorkflowDefinitionRecord.definition_version == version)
            else:
                query = query.order_by(WorkflowDefinitionRecord.definition_version.desc())
            records = (await session.execute(query)).scalars().all()
        if not records:
            raise WorkflowDefinitionError(f"definition {name!r} not found for this tenant")

        record = next((r for r in records if r.status == "active"), records[0]) if version is None else records[0]

        await self._require_governed_promotion(record, tenant_id=tenant_id)

        if record.status == "revoked":
            raise WorkflowDefinitionError(f"definition {name!r} v{record.definition_version} is revoked")
        if record.status == "draft":
            raise WorkflowDefinitionError(f"definition {name!r} v{record.definition_version} is a draft")
        if record.status == "deprecated" and not allow_deprecated:
            raise WorkflowDefinitionError(
                f"definition {name!r} v{record.definition_version} is deprecated; only pinned continuations may run it"
            )
        if definition_hash and record.definition_hash != definition_hash:
            raise WorkflowDefinitionError(
                f"definition hash mismatch for {name!r} v{record.definition_version}: "
                f"pinned {definition_hash} != stored {record.definition_hash}"
            )
        if not self._may_execute(record, agent_id):
            raise WorkflowDefinitionError(
                f"agent {agent_id} is not authorized to execute definition {name!r} (call_policy/visibility)"
            )
        compiled = compile_workflow(record.definition_json)
        async with self._session(tenant_id) as asset_session:
            from app.services.ai_asset_adapters import project_workflow
            from app.services.ai_assets import register_projection, resolve_asset_ref

            native_key = f"workflow:{record.name}@{record.definition_version}"
            asset_ref = await resolve_asset_ref(
                asset_session,
                tenant_id=tenant_id,
                asset_type="workflow",
                native_key=native_key,
            )
            if asset_ref is None:
                await register_projection(
                    asset_session,
                    project_workflow(record),
                    change_source="reconcile",
                    actor_agent_id=agent_id,
                    change_message="Execution-bound Workflow asset reconciliation",
                )
                asset_ref = await resolve_asset_ref(
                    asset_session,
                    tenant_id=tenant_id,
                    asset_type="workflow",
                    native_key=native_key,
                )
            if asset_ref is None:
                raise WorkflowDefinitionError(
                    f"definition {name!r} v{record.definition_version} has no immutable AI asset revision"
                )
        return ResolvedDefinition(record=record, compiled=compiled, asset_ref=asset_ref)

    # ── fork ─────────────────────────────────────────────────────

    async def fork_to_ephemeral(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        agent_id: uuid.UUID,
        version: int | None = None,
        patch: dict | None = None,
    ) -> dict:
        """Registered version + shallow patch → ephemeral definition DATA.

        The returned dict feeds the normal ephemeral launch path (preview →
        risk grade → confirm → run); the registered record is never touched."""
        resolved = await self.resolve_for_execution(tenant_id=tenant_id, name=name, agent_id=agent_id, version=version)
        forked = copy.deepcopy(resolved.record.definition_json)
        if patch:
            forked.update(copy.deepcopy(patch))
        compile_workflow(forked)  # the patched result must still be valid data
        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log(
                "workflow_definition_forked",
                details={
                    "tenant_id": str(tenant_id),
                    "definition_id": str(resolved.record.id),
                    "definition_hash": resolved.record.definition_hash,
                    "forked_by_agent_id": str(agent_id),
                },
                agent_id=agent_id,
            )
        except Exception:
            import logging

            logging.getLogger(__name__).warning("fork audit write failed (non-fatal)", exc_info=True)
        return forked

    async def fork_record_to_ephemeral(
        self,
        *,
        tenant_id: uuid.UUID,
        definition_id: uuid.UUID,
        agent_id: uuid.UUID,
        expected_version: int | None = None,
        patch: dict | None = None,
    ) -> dict:
        """Exact registered row + patch → ephemeral definition DATA.

        REST fork is keyed by ``definition_id``. Re-resolving by name would let
        a request for v1 silently fork a newer same-name version, so this path
        validates and forks the row identified by the URL.
        """
        record = await self.get_record(definition_id, tenant_id=tenant_id)
        await self._require_governed_promotion(record, tenant_id=tenant_id)
        if expected_version is not None and record.definition_version != expected_version:
            raise WorkflowDefinitionError(
                f"definition version mismatch for {definition_id}: "
                f"requested {expected_version} != stored {record.definition_version}"
            )
        if record.status == "revoked":
            raise WorkflowDefinitionError(f"definition {record.name!r} v{record.definition_version} is revoked")
        if record.status == "draft":
            raise WorkflowDefinitionError(f"definition {record.name!r} v{record.definition_version} is a draft")
        if record.status == "deprecated":
            raise WorkflowDefinitionError(
                f"definition {record.name!r} v{record.definition_version} is deprecated; fork an active version"
            )
        if not self._may_execute(record, agent_id):
            raise WorkflowDefinitionError(
                f"agent {agent_id} is not authorized to execute definition {record.name!r} (call_policy/visibility)"
            )

        forked = copy.deepcopy(record.definition_json)
        if patch:
            forked.update(copy.deepcopy(patch))
        compile_workflow(forked)
        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log(
                "workflow_definition_forked",
                details={
                    "tenant_id": str(tenant_id),
                    "definition_id": str(record.id),
                    "definition_hash": record.definition_hash,
                    "forked_by_agent_id": str(agent_id),
                },
                agent_id=agent_id,
            )
        except Exception:
            import logging

            logging.getLogger(__name__).warning("fork audit write failed (non-fatal)", exc_info=True)
        return forked
