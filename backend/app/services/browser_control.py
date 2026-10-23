"""Current source authority and exclusive control for private browser resources."""

import asyncio
import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, FiniteFloat, Field, ValidationError
from sqlalchemy import select

from app.agents.delegation_token import DelegationToken, validate_delegation_token
from app.config import get_settings
from app.core.execution_context import ExecutionPrincipal
from app.database import tenant_scoped_session
from app.models.environment_browser import EnvironmentBrowser
from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment
from app.services.browser_device import BrowserPage, browser_device_spec
from app.services.browser_lifecycle import BrowserLifecycleSpec
from app.services.browser_pipe import BrowserPipeError
from app.services.channel_secret_storage import (
    ChannelSecretDecryptionError,
    channel_secret_key_id,
    decrypt_channel_secret,
    encrypt_channel_secret,
)
from app.services.code_execution.contracts import CodeExecutionResult
from app.services.execution_environments import (
    EnvironmentCommandFrame,
    EnvironmentCommandScope,
    EnvironmentError,
    active_leases,
    environment_command_scope,
    lock_environment,
)


class BrowserControlError(EnvironmentError):
    def __init__(self, code, status=409, *, effect="not_started"):
        super().__init__(code, status)
        self.effect = effect


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Navigate(_NoArguments):
    url: str


class _Point(_NoArguments):
    x: FiniteFloat
    y: FiniteFloat


class _Scroll(_Point):
    deltaX: FiniteFloat
    deltaY: FiniteFloat


class _Text(_NoArguments):
    text: str


class _Key(_NoArguments):
    key: str
    modifiers: int = Field(default=0, ge=0, le=15)


class _Select(_NoArguments):
    target_id: str


_ARGUMENTS = {
    **dict.fromkeys(
        (
            "start",
            "observe",
            "request_control",
            "take_control",
            "return_control",
            "heartbeat",
            "disconnect",
            "stop",
            "reconcile",
        ),
        _NoArguments,
    ),
    "navigate": _Navigate,
    "click": _Point,
    "scroll": _Scroll,
    "type": _Text,
    "key": _Key,
    "select_page": _Select,
}


@dataclass(frozen=True)
class BrowserSource:
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID
    tool_name: str | None = None
    policy_snapshot_hash: str | None = None
    approval_id: uuid.UUID | None = None
    delegation_token: DelegationToken | None = None
    execution_principal: ExecutionPrincipal | None = None

    async def authorize(self, db, scope=None):
        from app.core.permissions import authorize_session_action, require_writable_session
        from app.services.runtime_user_identity import load_scoped_runtime_user

        actor = await load_scoped_runtime_user(db, self.user_id, tenant_id=self.tenant_id)
        if actor is None:
            raise BrowserControlError("browser_authority_revoked", 403)
        if scope is not None and any(
            str(getattr(scope, key)) != str(getattr(self, key))
            for key in ("tenant_id", "agent_id", "user_id", "session_id")
        ):
            raise BrowserControlError("browser_source_changed", 403)
        try:
            decision = await authorize_session_action(
                db,
                actor,
                agent_id=self.agent_id,
                session_id=self.session_id,
                action="browser_control",
                require_execution=True,
                audit=False,
            )
            if decision.session.session_kind == "delegation_run":
                from app.services.browser_delegation import authorize_delegated_browser

                await authorize_delegated_browser(db, self, decision, scope)
            else:
                require_writable_session(decision.session, action="browser_control")
        except HTTPException as exc:
            raise BrowserControlError(
                "browser_authority_unavailable" if exc.status_code >= 500 else "browser_authority_revoked",
                503 if exc.status_code >= 500 else 403,
            ) from exc
        if decision.agent.tenant_id != self.tenant_id or decision.session.tenant_id != self.tenant_id:
            raise BrowserControlError("browser_source_changed", 403)
        if decision.session.audience_kind not in {None, "private"}:
            raise BrowserControlError("browser_private_context_required", 422)
        if self.tool_name is not None or self.policy_snapshot_hash is not None:
            from app.services.approval_ticket import build_live_approval_policy_snapshot, hash_policy_snapshot
            from app.services.capability_gate import _resolve_capability

            if self.tool_name not in {"browser_observe", "browser_manage", "browser_action"}:
                raise BrowserControlError("browser_tool_binding_invalid", 403)
            if self.delegation_token is not None and not validate_delegation_token(
                self.delegation_token, child_agent_id=self.agent_id, capability=_resolve_capability(self.tool_name)
            ).valid:
                raise BrowserControlError("browser_delegation_revoked", 403)
            snapshot = await build_live_approval_policy_snapshot(
                db=db, tenant_id=self.tenant_id, agent_id=self.agent_id, tool_name=self.tool_name
            )
            if snapshot["capability"]["denied"]:
                raise BrowserControlError("browser_capability_revoked", 403)
            if hash_policy_snapshot(snapshot) != self.policy_snapshot_hash:
                raise BrowserControlError("browser_policy_changed", 409)
        if self.approval_id is not None:
            from app.models.audit import ApprovalRequest
            from app.models.runtime_task import RuntimeTask
            from app.services.approval_service import can_resolve_approval
            from app.services.shared_group_authority import approval_execution_matches

            approval = await db.get(ApprovalRequest, self.approval_id, populate_existing=True)
            from app.services.browser_human_approval import authorize, is_human_approval
            if approval is not None and is_human_approval(approval):
                await authorize(db, self, approval)
                return actor
            if (
                approval is None
                or approval.tenant_id != self.tenant_id
                or approval.agent_id != self.agent_id
                or approval.requested_by != self.user_id
                or approval.tool_name != self.tool_name
                or approval.policy_snapshot_hash != self.policy_snapshot_hash
                or approval.status != "approved"
                or approval.execution_status != "executing"
                or approval.consumed_at is None
                or approval.expires_at is None
                or approval.expires_at <= datetime.now(UTC)
                or scope is not None and scope.runtime_task_id != approval.execution_task_id
            ):
                raise BrowserControlError("browser_approval_revoked", 403)
            try:
                original_id = uuid.UUID(str((approval.execution_envelope or {}).get("runtime_task_id")))
            except (TypeError, ValueError) as exc:
                raise BrowserControlError("browser_approval_revoked", 403) from exc
            original = await db.get(RuntimeTask, original_id, populate_existing=True)
            approver = await load_scoped_runtime_user(db, approval.resolved_by, tenant_id=self.tenant_id)
            if (
                not await approval_execution_matches(db, task=original, session=decision.session)
                or approver is None
                or not await can_resolve_approval(db, agent=decision.agent, user=approver, approval=approval)
            ):
                raise BrowserControlError("browser_approval_revoked", 403)
        return actor

    async def revalidate(self, db, scope):
        return await self.authorize(db, scope)

    def receipt(self):
        return {
            "schema": "hive.browser.source.v1",
            **{key: str(getattr(self, key)) for key in ("tenant_id", "agent_id", "user_id", "session_id")},
            **(
                {"tool_policy": {
                    "tool_name": self.tool_name,
                    "policy_snapshot_hash": self.policy_snapshot_hash,
                    **({"approval_id": str(self.approval_id)} if self.approval_id is not None else {}),
                    **({"delegation_token": {
                        "delegation_id": self.delegation_token.delegation_id,
                        "parent_agent_id": str(self.delegation_token.parent_agent_id),
                        "child_agent_id": str(self.delegation_token.child_agent_id),
                        "issued_at": self.delegation_token.issued_at,
                        "expires_at": self.delegation_token.expires_at,
                        "granted_capabilities": sorted(self.delegation_token.granted_capabilities),
                        "inherit_parent_capabilities": self.delegation_token.inherit_parent_capabilities,
                    }} if self.delegation_token is not None else {}),
                    **({"execution_principal": self.execution_principal.to_evidence()}
                       if self.execution_principal is not None else {}),
                }}
                if self.tool_name is not None
                else {}
            ),
        }


def _expire(row, now):
    expired = row.control_expires_at is not None and row.control_expires_at <= now
    disconnected = row.viewer_expires_at is not None and row.viewer_expires_at <= now
    if row.control_mode == "human" and (expired or disconnected):
        row.control_mode = "paused"
        row.control_expires_at = None
        row.viewer_expires_at = None
        row.control_epoch += 1
        row.observed_epoch = 0
        row.revision += 1
        row.resource_error = "browser_human_control_expired" if expired else "browser_viewer_disconnected"


class BrowserControlService:
    def __init__(self):
        self.instance_id = uuid.uuid4().hex
        self._pages = {}
        self._monitors = {}
        self._sources = {}
        self._closing = False
        self._operations = set()
        self._rpc_jobs = {}

    async def _row(self, db, source, *, lock=False):
        statement = (
            select(EnvironmentBrowser)
            .where(
                EnvironmentBrowser.tenant_id == source.tenant_id,
                EnvironmentBrowser.agent_id == source.agent_id,
                EnvironmentBrowser.session_id == source.session_id,
                EnvironmentBrowser.principal_id == source.user_id,
            )
            .order_by(EnvironmentBrowser.created_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return await db.scalar(statement.with_for_update() if lock else statement)

    def _view(self, row):
        if row is None:
            return None
        attached = row.worker_instance == self.instance_id and row.id in self._pages
        return {
            "id": str(row.id),
            "status": row.status,
            "desired_state": row.desired_state,
            "revision": row.revision,
            "control_epoch": row.control_epoch,
            "control_mode": row.control_mode,
            "control_expires_at": row.control_expires_at,
            "viewer_expires_at": row.viewer_expires_at,
            "fresh_observation_required": row.observed_epoch != row.control_epoch,
            "attached_here": attached,
            "device_profile": "trusted_host",
            "isolation": "browser_native_sandbox_only",
            "resource_error": row.resource_error,
            "reconciliation_pending": (row.last_effect or {}).get("reconciliation_requested") is True,
            "last_effect": dict(row.last_effect or {}),
            "next_action": "wait_for_stop"
            if row.desired_state == "stopped" and row.status not in {"stopped", "revoked"}
            else "wait_for_reconciliation"
            if (row.last_effect or {}).get("reconciliation_requested") is True
            else "start"
            if row.status in {"stopped", "revoked"}
            else "inspect_original_worker"
            if not attached
            else "reconcile"
            if row.status == "lost"
            else "human_control"
            if row.control_mode == "human"
            else "return_control"
            if row.control_mode in {"requested", "paused"}
            else "observe"
            if row.observed_epoch != row.control_epoch
            else "continue",
        }

    async def read(self, source):
        async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_resource_read") as db:
            await source.authorize(db)
            await lock_environment(db, source.tenant_id, source.agent_id)
            row = await self._row(db, source, lock=True)
            if row is not None:
                await self._reconcile_closed(db, row)
                _expire(row, datetime.now(UTC))
                if row.status in {"stopped", "revoked"} and (row.last_effect or {}).get("closure_confirmed") is True:
                    await self._settle_browser_leases(db, row, "stopped")
            return self._view(row)

    async def _provider(self, source):
        async with tenant_scoped_session(
            source.tenant_id, require_tenant=True, source="browser_environment_binding"
        ) as db:
            await source.authorize(db)
            await lock_environment(db, source.tenant_id, source.agent_id)
            resources = list(
                await db.scalars(
                    select(EnvironmentBrowser)
                    .where(
                        EnvironmentBrowser.tenant_id == source.tenant_id,
                        EnvironmentBrowser.agent_id == source.agent_id,
                        EnvironmentBrowser.status.in_(["starting", "ready", "lost"]),
                    )
                    .with_for_update()
                )
            )
            for resource in resources:
                await self._reconcile_closed(db, resource)
            row = await self._row(db, source, lock=True)
            if (
                row is not None
                and row.status in {"stopped", "revoked"}
                and (row.last_effect or {}).get("closure_confirmed") is True
            ):
                await self._settle_browser_leases(db, row, "stopped")
            env = await db.scalar(
                select(ExecutionEnvironment).where(
                    ExecutionEnvironment.tenant_id == source.tenant_id, ExecutionEnvironment.agent_id == source.agent_id
                )
            )
            return env.provider_key if env is not None else "local_os_sandbox"

    async def execute(
        self,
        source,
        *,
        action,
        request_id,
        expected_revision,
        expected_epoch,
        actor_kind="agent",
        arguments=None,
        runtime_task_id=None,
    ):
        if self._closing and action not in {"stop", "reconcile"}:
            raise BrowserControlError("browser_worker_stopping", 503)
        if actor_kind not in {"agent", "human"}:
            raise BrowserControlError("browser_actor_kind_invalid", 422)
        try:
            request_id = uuid.UUID(str(request_id))
            arguments = _ARGUMENTS[action].model_validate(arguments or {}).model_dump()
        except (KeyError, ValueError, TypeError, ValidationError) as exc:
            raise BrowserControlError("browser_arguments_invalid", 422) from exc
        if action in {"stop", "reconcile"}:
            return await self._recovery(source, action, expected_revision, expected_epoch)
        if action in {"request_control", "take_control", "return_control", "heartbeat", "disconnect"}:
            return await self._metadata(source, action, expected_revision, expected_epoch, actor_kind, runtime_task_id, request_id)
        invocation_key = f"browser:{source.session_id}:{source.user_id}:{request_id}"
        payload = {}
        frame = EnvironmentCommandFrame(
            source.tenant_id,
            source.agent_id,
            source.user_id,
            runtime_task_id,
            source_binding=source,
            session_id=source.session_id,
        )
        async with environment_command_scope(frame, invocation_key) as scope:
            scope.resource_receipt = {"kind": "browser", "action": action, "effect": "not_started"}

            async def operation():
                nonlocal payload
                from app.services.browser_rpc import ACTIONS, forward

                remote = None
                if action in ACTIONS:
                    async with tenant_scoped_session(
                        source.tenant_id, require_tenant=True, source="browser_rpc_route"
                    ) as db:
                        await source.authorize(db, scope)
                        await self._check_request(db, scope)
                        candidate = await self._row(db, source)
                        if candidate is not None and candidate.worker_instance != self.instance_id:
                            remote = candidate
                if remote is not None:
                    payload = await forward(
                        source,
                        scope,
                        remote,
                        action=action,
                        revision=expected_revision,
                        epoch=expected_epoch,
                        actor_kind=actor_kind,
                        arguments=arguments or {},
                    )
                else:
                    payload = await self._run_owned(
                        source, scope, action, expected_revision, expected_epoch, actor_kind, arguments or {}
                    )
                return CodeExecutionResult(evidence={"provider": "trusted_host_browser_device"})

            result = await scope.execute(operation, provider=await self._provider(source), timeout=30)
            if result.error:
                effect = scope.resource_receipt.get("effect")
                if effect not in {"unknown", "not_started"}:
                    effect = "unknown" if scope.resource_state == "unknown" else "not_started"
                raise BrowserControlError(
                    result.evidence.get("reason_code", "browser_execution_unavailable"),
                    503 if result.evidence.get("status") == "unavailable" else 409,
                    effect=effect,
                )
            return payload

    async def _metadata(self, source, action, revision, epoch, actor_kind, runtime_task_id, request_id):
        async with tenant_scoped_session(
            source.tenant_id, require_tenant=True, source="browser_control_metadata"
        ) as db:
            probe = EnvironmentCommandScope(
                source.tenant_id,
                source.agent_id,
                source.user_id,
                "browser_control_metadata",
                runtime_task_id,
                source_binding=source,
                session_id=source.session_id,
            )
            await probe._authority(db, before_command=True)
            await lock_environment(db, source.tenant_id, source.agent_id)
            row = await self._row(db, source, lock=True)
            if row is None:
                raise BrowserControlError("browser_not_started")
            _expire(row, datetime.now(UTC))
            self._expect(row, revision, epoch)
            from app.services.browser_human_approval import check_effect
            await check_effect(db, source, action, request_id, revision, epoch, {}, row, actor_kind)
            env = await db.get(ExecutionEnvironment, row.environment_id)
            if (
                row.status != "ready"
                or row.desired_state != "running"
                or env is None
                or env.desired_state != "ready"
                or env.generation != row.generation
            ):
                raise BrowserControlError("browser_resource_unavailable")
            if action in {"take_control", "return_control"}:
                for lease in await active_leases(db, row.environment_id):
                    outcome = lease.outcome or {}
                    if (outcome.get("resource_receipt") or {}).get("kind") == "browser" or (
                        outcome.get("browser_rpc") or {}
                    ).get("resource_id") == str(row.id):
                        raise BrowserControlError("browser_commands_active")
            self._control(row, action, actor_kind, datetime.now(UTC))
            row.last_used_at = datetime.now(UTC)
            return {"browser": self._view(row)}

    async def _check_effect(self, source, scope, browser_id, epoch, actor_kind):
        await scope._check()
        async with tenant_scoped_session(
            source.tenant_id, require_tenant=True, source="browser_control_effect_fence"
        ) as db:
            row = await db.get(EnvironmentBrowser, browser_id)
            if row is None:
                raise BrowserControlError("browser_resource_unavailable")
            _expire(row, datetime.now(UTC))
            if (
                row.status != "ready"
                or row.desired_state != "running"
                or row.worker_instance != self.instance_id
                or row.generation != scope.generation
                or row.control_epoch != epoch
                or row.lifecycle_nonce != scope.resource_receipt.get("lifecycle_nonce")
                or row.control_mode != actor_kind
            ):
                raise BrowserControlError("browser_control_changed")

    async def _run_owned(self, source, scope, action, revision, epoch, actor_kind, arguments):
        task = asyncio.current_task()
        self._operations.add(task)
        try:
            if self._closing:
                raise BrowserControlError("browser_worker_stopping", 503)
            return await self._operate(source, scope, action, revision, epoch, actor_kind, arguments)
        except asyncio.CancelledError:
            if self._closing:
                raise BrowserControlError("browser_worker_stopping", 503) from None
            raise
        finally:
            self._operations.discard(task)

    def enqueue_rpc(self, tenant_id, resource_id, lease_id):
        try:
            tenant_id, resource_id, lease_id = (uuid.UUID(str(value)) for value in (tenant_id, resource_id, lease_id))
        except (ValueError, TypeError):
            return False
        source = self._sources.get(resource_id)
        if self._closing or source is None or source.tenant_id != tenant_id or resource_id not in self._pages:
            return False
        if lease_id in self._rpc_jobs:
            return True

        async def receive():
            from app.services.browser_rpc import run

            try:
                await run(self, resource_id, lease_id)
            finally:
                if self._rpc_jobs.get(lease_id) is asyncio.current_task():
                    self._rpc_jobs.pop(lease_id, None)

        self._rpc_jobs[lease_id] = asyncio.create_task(receive())
        return True

    @staticmethod
    async def _check_request(db, scope):
        repeated = await db.scalar(
            select(EnvironmentLease.id)
            .where(
                EnvironmentLease.environment_id == scope.environment_id,
                EnvironmentLease.invocation_key == scope.invocation_key,
                EnvironmentLease.id != scope.lease_id,
            )
            .limit(1)
        )
        if repeated is not None:
            raise BrowserControlError("browser_request_already_recorded")

    async def _operate(self, source, scope, action, revision, epoch, actor_kind, arguments):
        now = datetime.now(UTC)
        async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_effect_intent") as db:
            await source.authorize(db, scope)
            await lock_environment(db, source.tenant_id, source.agent_id)
            await self._check_request(db, scope)
            row = await self._row(db, source, lock=True)
            from app.services.browser_human_approval import check_effect
            await check_effect(db, source, action, scope.invocation_key.rsplit(":", 1)[-1], revision, epoch, arguments, row, actor_kind)
            if action == "start":
                if row is None:
                    if revision != 0 or epoch != 0:
                        raise BrowserControlError("browser_revision_conflict")
                    active = await db.scalar(
                        select(EnvironmentBrowser.id)
                        .where(
                            EnvironmentBrowser.environment_id == scope.environment_id,
                            EnvironmentBrowser.status.in_(["starting", "ready", "lost"]),
                        )
                        .limit(1)
                    )
                    if active is not None:
                        raise BrowserControlError("browser_device_busy")
                    row = EnvironmentBrowser(
                        tenant_id=source.tenant_id,
                        agent_id=source.agent_id,
                        environment_id=scope.environment_id,
                        session_id=source.session_id,
                        principal_id=source.user_id,
                        worker_instance=self.instance_id,
                        generation=scope.generation,
                    )
                    db.add(row)
                    await db.flush()
                else:
                    self._expect(row, revision, epoch)
                    if row.status not in {"stopped", "revoked"}:
                        raise BrowserControlError("browser_resource_already_started")
                    active = await db.scalar(
                        select(EnvironmentBrowser.id)
                        .where(
                            EnvironmentBrowser.environment_id == scope.environment_id,
                            EnvironmentBrowser.status.in_(["starting", "ready", "lost"]),
                        )
                        .limit(1)
                    )
                    if active is not None:
                        raise BrowserControlError("browser_device_busy")
                    row.status, row.desired_state = "starting", "running"
                    row.worker_instance, row.generation = self.instance_id, scope.generation
                    row.lifecycle_nonce, row.lifecycle_secret, row.lifecycle_receipt_path = None, None, None
                    row.control_mode, row.control_expires_at = "agent", None
                    row.viewer_expires_at = None
                    row.control_epoch += 1
                    row.observed_epoch = 0
                    row.revision += 1
            else:
                if row is None:
                    raise BrowserControlError("browser_not_started")
                _expire(row, now)
                self._expect(row, revision, epoch)
                if row.status != "ready" or row.desired_state != "running" or row.generation != scope.generation:
                    raise BrowserControlError("browser_resource_unavailable")
                if row.worker_instance != self.instance_id or row.id not in self._pages:
                    raise BrowserControlError("browser_original_worker_required", 503)
                if actor_kind == "agent":
                    if row.control_mode != "agent":
                        raise BrowserControlError("browser_human_control_active")
                    if action != "observe" and row.observed_epoch != row.control_epoch:
                        raise BrowserControlError("browser_fresh_observation_required")
                elif action != "observe" and row.control_mode != "human":
                    raise BrowserControlError("browser_human_control_required")
            browser_id = row.id
            operation_epoch = row.control_epoch
            scope.resource_receipt = {
                "kind": "browser",
                "browser_resource_id": str(browser_id),
                "action": action,
                "effect": "pending",
                "control_epoch": row.control_epoch,
                "lifecycle_nonce": row.lifecycle_nonce,
            }
            row.last_effect = {**scope.resource_receipt, "lease_id": str(scope.lease_id)}
            row.last_used_at = now

        try:
            if action == "start":
                spec = browser_device_spec(browser_id, source.tenant_id, source.agent_id)
                spec = await self._prepare_lifecycle(source, browser_id, spec)
                scope.resource_receipt = {**scope.resource_receipt, "lifecycle_nonce": spec.lifecycle.nonce}
                await scope.record_creation_intent()
                scope.resource_state = "unknown"
                page = await BrowserPage.start(spec)
                self._pages[browser_id] = page
                identity_source = replace(source, tool_name=None, policy_snapshot_hash=None, approval_id=None, delegation_token=None, execution_principal=None)
                self._sources[browser_id] = identity_source
                self._monitors[browser_id] = asyncio.create_task(self._monitor(identity_source, browser_id, page))
            else:
                page = self._pages[browser_id]
                scope.resource_state = "unknown"
                if action == "observe":
                    observed = await page.observe()
                else:
                    await page.act(
                        action,
                        arguments,
                        before_effect=lambda: self._check_effect(
                            source, scope, browser_id, operation_epoch, actor_kind
                        ),
                    )
            await scope._check()
            async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_effect_ack") as db:
                await lock_environment(db, source.tenant_id, source.agent_id)
                row = await db.get(EnvironmentBrowser, browser_id, with_for_update=True)
                if row is None:
                    raise BrowserControlError("browser_receipt_unavailable", 503, effect="unknown")
                if row.control_epoch != operation_epoch or row.desired_state != "running":
                    scope.resource_state = "command_completed"
                    scope.resource_receipt = {
                        **scope.resource_receipt,
                        "effect": "acknowledged",
                        "business_outcome": "unverified",
                    }
                    raise BrowserControlError("browser_control_changed", effect="unknown")
                row.status, row.resource_error = "ready", None
                if action == "observe" and actor_kind == "agent":
                    row.observed_epoch = row.control_epoch
                scope.resource_receipt = {**scope.resource_receipt, "effect": "acknowledged"}
                row.last_effect = {**row.last_effect, **scope.resource_receipt}
                payload = {"browser": self._view(row), **({"observation": observed} if action == "observe" else {})}
            scope.resource_state = "command_completed"
            return payload
        except BrowserPipeError as exc:
            await self._failure(source, browser_id, scope, exc)
            raise BrowserControlError(exc.code, 503 if exc.effect == "unknown" else 422, effect=exc.effect) from exc

        except asyncio.CancelledError as exc:
            error = BrowserPipeError(
                "browser_operation_cancelled",
                "not_started" if scope.resource_state == "not_created" else "unknown",
                getattr(exc, "request_id", None),
            )
            error.resource_closed = getattr(exc, "resource_closed", False)
            await self._failure(source, browser_id, scope, error)
            raise
        except EnvironmentError as exc:
            effect = "not_started" if scope.resource_state == "not_created" else "unknown"
            await self._failure(source, browser_id, scope, BrowserPipeError(exc.code, effect))
            raise BrowserControlError(exc.code, exc.status, effect=effect) from exc
        except Exception as exc:
            # Device admission has already recorded the command intent. An
            # unexpected executor failure cannot prove a dispatched effect absent.
            effect = "not_started" if scope.resource_state == "not_created" else "unknown"
            error = BrowserPipeError("browser_execution_unconfirmed", effect)
            await self._failure(source, browser_id, scope, error)
            raise BrowserControlError(error.code, 503, effect=effect) from exc

    @staticmethod
    def _expect(row, revision, epoch):
        if row.revision != revision or row.control_epoch != epoch:
            raise BrowserControlError("browser_revision_conflict")

    async def _prepare_lifecycle(self, source, browser_id, spec):
        nonce, key = uuid.uuid4().hex, secrets.token_hex(32)
        directory = spec.cwd.parent / ".control" / str(browser_id)
        try:
            if directory.resolve() != directory:
                raise OSError("aliased private supervisor directory")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            encrypted = encrypt_channel_secret(key)
        except (OSError, RuntimeError) as exc:
            raise BrowserPipeError("browser_lifecycle_storage_unavailable", "not_started") from exc
        path = directory / f"{nonce}.json"
        async with tenant_scoped_session(
            source.tenant_id, require_tenant=True, source="browser_lifecycle_identity"
        ) as db:
            await source.authorize(db)
            await lock_environment(db, source.tenant_id, source.agent_id)
            row = await db.get(EnvironmentBrowser, browser_id, with_for_update=True)
            if row is None or row.desired_state != "running" or row.status != "starting":
                raise BrowserPipeError("browser_start_cancelled", "not_started")
            row.lifecycle_nonce, row.lifecycle_secret, row.lifecycle_receipt_path = nonce, encrypted, str(path)
            row.last_effect = {**row.last_effect, "lifecycle_nonce": nonce}
            identity = BrowserLifecycleSpec(str(row.id), row.generation, nonce, key, path)
        return replace(spec, lifecycle=identity)

    async def _reconcile_closed(self, db, row):
        if row.status in {"stopped", "revoked"}:
            return False
        if not all((row.lifecycle_nonce, row.lifecycle_secret, row.lifecycle_receipt_path)):
            return False
        path = Path(row.lifecycle_receipt_path)
        if (
            path.name != f"{row.lifecycle_nonce}.json"
            or path.parent.name != str(row.id)
            or path.parent.parent.name != ".control"
        ):
            return False
        try:
            if channel_secret_key_id(row.lifecycle_secret) is None:
                return False
            key = decrypt_channel_secret(row.lifecycle_secret)
        except ChannelSecretDecryptionError:
            return False
        identity = BrowserLifecycleSpec(str(row.id), row.generation, row.lifecycle_nonce, key, path)
        if not identity.observes_closed():
            return False
        row.status, row.desired_state, row.control_mode = "stopped", "stopped", "paused"
        row.control_expires_at, row.viewer_expires_at = None, None
        row.control_epoch += 1
        row.observed_epoch = 0
        row.revision += 1
        row.last_effect = {
            **(row.last_effect or {}),
            "effect": "unknown"
            if (row.last_effect or {}).get("effect") == "pending"
            else (row.last_effect or {}).get("effect"),
            "closure_confirmed": True,
            "closure_evidence": "signed_private_supervisor_receipt",
            "business_outcome": "unverified",
            "reconciliation_requested": False,
        }
        await self._settle_browser_leases(db, row, "stopped")
        return True

    @staticmethod
    def _control(row, action, actor_kind, now):
        settings = get_settings()
        if settings.BROWSER_HUMAN_CONTROL_SECONDS <= 0 or settings.BROWSER_CONTROL_DISCONNECT_SECONDS <= 0:
            raise BrowserControlError("browser_control_limits_invalid", 503)
        if action != "request_control" and actor_kind != "human":
            raise BrowserControlError("browser_human_action_required", 403)
        if action == "heartbeat":
            if row.control_mode != "human":
                raise BrowserControlError("browser_human_control_required")
            row.viewer_expires_at = min(
                row.control_expires_at, now + timedelta(seconds=settings.BROWSER_CONTROL_DISCONNECT_SECONDS)
            )
            return
        if action == "request_control":
            if row.control_mode == "human":
                raise BrowserControlError("browser_human_control_active")
            row.control_mode = "requested"
        elif action == "take_control":
            if row.control_mode == "human":
                raise BrowserControlError("browser_human_control_active")
            row.control_mode = "human"
            row.control_expires_at = now + timedelta(seconds=settings.BROWSER_HUMAN_CONTROL_SECONDS)
            row.viewer_expires_at = min(
                row.control_expires_at, now + timedelta(seconds=settings.BROWSER_CONTROL_DISCONNECT_SECONDS)
            )
        elif action == "return_control":
            row.control_mode = "agent"
        elif action == "disconnect":
            row.control_mode = "paused"
        else:
            raise BrowserControlError("browser_action_unsupported", 422)
        if row.control_mode != "human":
            row.control_expires_at = None
            row.viewer_expires_at = None
        row.control_epoch += 1
        row.observed_epoch = 0
        row.revision += 1
        row.resource_error = None

    async def _failure(self, source, browser_id, scope, error):
        scope.resource_state = "unknown" if error.effect == "unknown" else "not_created"
        observation = scope.resource_receipt.get("action") == "observe"
        if observation:
            scope.resource_state = "command_completed"
        if error.resource_closed:
            scope.resource_state = "stopped"
        if error.pipe is not None and browser_id not in self._pages:
            self._pages[browser_id] = BrowserPage(error.pipe)
            identity_source = replace(source, tool_name=None, policy_snapshot_hash=None, approval_id=None, delegation_token=None, execution_principal=None)
            self._sources[browser_id] = identity_source
            self._monitors[browser_id] = asyncio.create_task(
                self._monitor(identity_source, browser_id, self._pages[browser_id])
            )
        scope.resource_receipt = {
            **scope.resource_receipt,
            "effect": error.effect,
            "reason_code": error.code,
            "protocol_request_id": error.request_id,
        }
        async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_effect_failure") as db:
            await lock_environment(db, source.tenant_id, source.agent_id)
            row = await db.get(EnvironmentBrowser, browser_id, with_for_update=True)
            if row is not None:
                row.status = (
                    "lost"
                    if error.effect == "unknown" and not observation
                    else "stopped"
                    if row.status == "starting"
                    else row.status
                )
                row.resource_error = error.code
                row.last_effect = {**row.last_effect, **scope.resource_receipt}
                if error.resource_closed:
                    row.status, row.desired_state, row.control_mode = "stopped", "stopped", "paused"
                    row.last_effect = {**row.last_effect, "closure_confirmed": True}

    async def _recovery(self, source, action, revision, epoch):
        async with tenant_scoped_session(
            source.tenant_id, require_tenant=True, source="browser_resource_recovery"
        ) as db:
            await source.authorize(db)
            await lock_environment(db, source.tenant_id, source.agent_id)
            row = await self._row(db, source, lock=True)
            if row is None:
                raise BrowserControlError("browser_not_started")
            self._expect(row, revision, epoch)
            if await self._reconcile_closed(db, row):
                return {"browser": self._view(row)}
            if action == "stop":
                row.desired_state, row.control_mode = "stopped", "paused"
                row.control_expires_at = None
                row.viewer_expires_at = None
                row.control_epoch += 1
                row.observed_epoch = 0
                row.revision += 1
                row.last_effect = {**row.last_effect, "reconciliation_requested": False}
            elif row.status != "lost":
                raise BrowserControlError("browser_reconciliation_not_required")
            browser_id = row.id
            if row.worker_instance != self.instance_id or browser_id not in self._pages:
                if action == "reconcile":
                    # This is a durable request to inspect the existing ACK,
                    # never authority to dispatch or replay a browser action.
                    row.last_effect = {**row.last_effect, "reconciliation_requested": True}
                return {"browser": self._view(row)}
        page = self._pages.get(browser_id)
        if page is None:
            raise BrowserControlError("browser_original_worker_required", 503, effect="unknown")
        if action == "stop":
            await self._close_resource(source, browser_id)
        else:
            async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_late_ack") as db:
                await lock_environment(db, source.tenant_id, source.agent_id)
                row = await db.get(EnvironmentBrowser, browser_id, with_for_update=True)
                self._expect(row, revision, epoch)
                identity = page.pipe.lifecycle
                if (
                    identity is None
                    or row.worker_instance != self.instance_id
                    or row.lifecycle_nonce != identity.nonce
                    or row.generation != identity.generation
                ):
                    raise BrowserControlError("browser_resource_unavailable")
                response = page.pipe.late_results.get((row.last_effect or {}).get("protocol_request_id"))
                if response is None:
                    raise BrowserControlError("browser_effect_unconfirmed", 409, effect="unknown")
                row.status, row.control_mode, row.resource_error = "ready", "paused", None
                row.control_epoch += 1
                row.observed_epoch = 0
                row.revision += 1
                row.last_effect = {
                    **row.last_effect,
                    "effect": "acknowledged",
                    "business_outcome": "unverified",
                    "reconciliation": "late_protocol_ack",
                    "protocol_error": "error" in response,
                    "reconciliation_requested": False,
                }
                await self._settle_browser_leases(db, row, "command_completed")
        return {"browser": await self.read(source)}

    async def _settle_browser_leases(self, db, row, resource_state):
        await lock_environment(db, row.tenant_id, row.agent_id)
        for lease in await active_leases(db, row.environment_id):
            receipt = (lease.outcome or {}).get("resource_receipt") or {}
            frame_matches = (
                str(lease.id) == str((row.last_effect or {}).get("lease_id"))
                and lease.generation == row.generation
                and lease.principal_id == row.principal_id
                and row.lifecycle_nonce is not None
                and row.last_effect.get("lifecycle_nonce") == row.lifecycle_nonce
            )
            receipt_matches = (
                receipt.get("browser_resource_id") == str(row.id)
                and row.lifecycle_nonce is not None
                and receipt.get("lifecycle_nonce") == row.lifecycle_nonce
                and lease.generation == row.generation
                and lease.principal_id == row.principal_id
            )
            if receipt_matches or frame_matches:
                lease.status, lease.released_at = "released", datetime.now(UTC)
                lease.outcome = {
                    **lease.outcome,
                    "resource_state": resource_state,
                    "browser_reconciliation": {
                        "effect": row.last_effect.get("effect"),
                        "business_outcome": "unverified",
                    },
                }
        env = await db.get(ExecutionEnvironment, row.environment_id, with_for_update=True)
        if env is not None and not await active_leases(db, env.id):
            env.observed_state = "stopped" if env.desired_state == "stopped" else "ready"

    async def _close_resource(self, source, browser_id, *, revoked=False, owned_page=None):
        page = owned_page if owned_page is not None else self._pages.get(browser_id)
        if page is None:
            return
        try:
            await page.close()
        except BrowserPipeError:
            if page.pipe.lifecycle is None or not page.pipe.lifecycle.observes_closed():
                return  # No positive closure receipt: preserve the resource and writer.
        async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_resource_closed") as db:
            await lock_environment(db, source.tenant_id, source.agent_id)
            row = await db.get(EnvironmentBrowser, browser_id, with_for_update=True)
            identity = page.pipe.lifecycle
            if (
                row is not None
                and identity is not None
                and row.worker_instance == self.instance_id
                and row.lifecycle_nonce == identity.nonce
                and row.generation == identity.generation
            ):
                row.status, row.desired_state = "revoked" if revoked else "stopped", "stopped"
                row.control_mode, row.control_expires_at = "paused", None
                row.viewer_expires_at = None
                row.observed_epoch = 0
                row.last_effect = {**row.last_effect, "closure_confirmed": True, "reconciliation_requested": False}
                await self._settle_browser_leases(db, row, "stopped")
        if self._pages.get(browser_id) is page:
            self._pages.pop(browser_id, None)
            self._sources.pop(browser_id, None)

    async def _monitor(self, source, browser_id, owned_page):
        from app.services.runtime_task_fence import clear_runtime_task_fence, reset_runtime_task_fence

        fence_token = clear_runtime_task_fence()
        try:
            while self._pages.get(browser_id) is owned_page:
                await asyncio.sleep(1)
                if self._pages.get(browser_id) is not owned_page:
                    break
                revoked = False
                stop = False
                pending_rpc = []
                reconciliation = None
                async with tenant_scoped_session(
                    source.tenant_id, require_tenant=True, source="browser_resource_supervision"
                ) as db:
                    await lock_environment(db, source.tenant_id, source.agent_id)
                    row = await db.get(EnvironmentBrowser, browser_id, with_for_update=True)
                    identity = owned_page.pipe.lifecycle
                    if (
                        row is None
                        or identity is None
                        or row.worker_instance != self.instance_id
                        or row.lifecycle_nonce != identity.nonce
                        or row.generation != identity.generation
                    ):
                        revoked = True
                    else:
                        try:
                            probe = EnvironmentCommandScope(
                                source.tenant_id,
                                source.agent_id,
                                source.user_id,
                                "browser_supervision",
                                None,
                                source_binding=source,
                                session_id=source.session_id,
                            )
                            await probe._authority(db)
                        except EnvironmentError:
                            revoked = True
                        _expire(row, datetime.now(UTC))
                        env = await db.get(ExecutionEnvironment, row.environment_id)
                        revoked = (
                            revoked or env is None or env.desired_state != "ready" or env.generation != row.generation
                        )
                        idle_seconds = get_settings().BROWSER_DEVICE_IDLE_SECONDS
                        stop = (
                            row.desired_state == "stopped"
                            or (datetime.now(UTC) - row.last_used_at).total_seconds() >= idle_seconds
                        )
                        if row.status == "lost" and (row.last_effect or {}).get("reconciliation_requested") is True:
                            reconciliation = (row.revision, row.control_epoch)
                        for lease in await active_leases(db, row.environment_id):
                            rpc = (lease.outcome or {}).get("browser_rpc") or {}
                            if rpc.get("phase") == "pending" and rpc.get("resource_id") == str(browser_id):
                                pending_rpc.append(lease.id)
                for lease_id in pending_rpc:
                    self.enqueue_rpc(source.tenant_id, browser_id, lease_id)
                if revoked or stop:
                    await self._close_resource(source, browser_id, revoked=revoked, owned_page=owned_page)
                elif reconciliation is not None:
                    try:
                        await self._recovery(source, "reconcile", *reconciliation)
                    except BrowserControlError as exc:
                        if exc.code not in {"browser_effect_unconfirmed", "browser_revision_conflict"}:
                            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            # Failure to revalidate never allows a new browser effect.
            await self._close_resource(source, browser_id, revoked=True, owned_page=owned_page)
        finally:
            reset_runtime_task_fence(fence_token)
            if self._monitors.get(browser_id) is asyncio.current_task():
                self._monitors.pop(browser_id, None)

    async def close(self):
        self._closing = True
        rpc_jobs = list(self._rpc_jobs.values())
        for job in rpc_jobs:
            job.cancel()
        await asyncio.gather(*rpc_jobs, return_exceptions=True)
        operations = list(self._operations)
        for operation in operations:
            operation.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        monitors = list(self._monitors.values())
        for monitor in monitors:
            monitor.cancel()
        await asyncio.gather(*monitors, return_exceptions=True)
        for browser_id in list(self._pages):
            await self._close_resource(self._sources[browser_id], browser_id)
        if self._pages:
            raise BrowserControlError("browser_cleanup_unconfirmed", 503, effect="unknown")


browser_control = BrowserControlService()
