"""Private command-resource lifecycle; canonical work remains in the workspace."""

import asyncio
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.services.code_execution.contracts import CodeExecutionResult
from app.services.runtime_task_fence import assert_runtime_task_fence
from app.services.environment_worker_witness import capture_worker_witness, observe_prelaunch_worker


class EnvironmentError(RuntimeError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def provider_key(value):
    if value in {"local", "local_os_sandbox", "os_sandbox", "bwrap", "sandbox-exec"}:
        return "local_os_sandbox"
    if value in {"vercel", "vercel_sandbox"}:
        return "vercel_sandbox"
    return value


def capabilities(provider):
    return {
        "scope": "agent_private",
        "compute_lifetime": "command",
        "persistent_compute": False,
        "canonical_workspace": True,
        "browser": False,
        "provider": provider,
        "stop_scope": "supervised_commands",
        "detached_process_control": False,
    }


async def lock_environment(db, tenant_id, agent_id):
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"environment:{tenant_id}:{agent_id}", 0)))
    )


async def active_leases(db, environment_id):
    return list(
        await db.scalars(
            select(EnvironmentLease).where(
                EnvironmentLease.environment_id == environment_id, EnvironmentLease.status == "active"
            )
        )
    )


async def project_environment(db, *, tenant_id, agent_id, include_recovery=False):
    env = await db.scalar(
        select(ExecutionEnvironment).where(
            ExecutionEnvironment.tenant_id == tenant_id, ExecutionEnvironment.agent_id == agent_id
        )
    )
    if env is None:
        return None
    leases = await active_leases(db, env.id)
    lost = any(lease.expires_at <= datetime.now(UTC) for lease in leases)
    state = "lost" if lost else env.observed_state
    recovery = None
    if lost and include_recovery:
        lease = leases[0]
        reason, allowed = observe_prelaunch_worker((lease.outcome or {}).get("resource_lifecycle"))
        recovery = {"lease_id": str(lease.id), "reason_code": reason, "can_reconcile": allowed}
    return {
        "id": str(env.id),
        "provider_key": env.provider_key,
        "generation": env.generation,
        "revision": env.revision,
        "desired_state": env.desired_state,
        "observed_state": state,
        "active_commands": len(leases),
        "capabilities": capabilities(env.provider_key),
        "last_used_at": env.last_used_at,
        **({"recovery": recovery} if recovery else {}),
        "next_action": "reconcile" if recovery and recovery["can_reconcile"] else "inspect_original_worker"
        if lost
        else "refresh"
        if state == "stopping"
        else "resume"
        if state == "stopped"
        else "continue",
    }


async def control_environment(db, *, tenant_id, agent_id, action, expected_revision, expected_lease_id=None):
    await lock_environment(db, tenant_id, agent_id)
    env = await db.scalar(
        select(ExecutionEnvironment)
        .where(ExecutionEnvironment.tenant_id == tenant_id, ExecutionEnvironment.agent_id == agent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if env is None:
        raise EnvironmentError("environment_not_started")
    if env.revision != expected_revision:
        raise EnvironmentError("environment_revision_conflict")
    leases = await active_leases(db, env.id)
    if action == "stop":
        env.desired_state = "stopped"
        env.observed_state = "stopping" if leases else "stopped"
    elif action == "resume":
        if leases:
            raise EnvironmentError(
                "environment_reconciliation_required"
                if any(row.expires_at <= datetime.now(UTC) for row in leases)
                else "environment_commands_active"
            )
        if env.desired_state != "ready" or env.observed_state != "ready":
            env.generation += 1
        env.desired_state = env.observed_state = "ready"
    elif action == "reconcile":
        if len(leases) != 1 or expected_lease_id is None or leases[0].id != expected_lease_id:
            raise EnvironmentError("environment_lease_conflict")
        lease = leases[0]
        if lease.expires_at > datetime.now(UTC):
            raise EnvironmentError("environment_commands_active")
        reason, allowed = observe_prelaunch_worker((lease.outcome or {}).get("resource_lifecycle"))
        if not allowed:
            raise EnvironmentError(reason)
        lease.status = "released"
        lease.released_at = datetime.now(UTC)
        lease.outcome = {
            **(lease.outcome or {}),
            "status": "interrupted",
            "resource_state": "not_created",
            "reconciliation": {"reason_code": reason, "observed_at": lease.released_at.isoformat()},
        }
        env.desired_state = env.observed_state = "stopped"
    else:
        raise EnvironmentError("environment_action_unsupported", 422)
    env.revision += 1
    await db.flush()
    return await project_environment(db, tenant_id=tenant_id, agent_id=agent_id, include_recovery=True)


@dataclass
class EnvironmentCommandScope:
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    user_id: uuid.UUID
    invocation_key: str
    runtime_task_id: uuid.UUID | None
    lease_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None
    generation: int | None = None
    fence_version: int | None = None
    outcome: dict = field(default_factory=dict)
    resource_state: str = "not_created"
    selected_provider: str | None = None
    source_binding: Any = field(default=None, repr=False)
    worker_witness: dict | None = field(default=None, init=False, repr=False)

    async def _authority(self, db, *, before_command=False):
        from app.models.tenant import Tenant
        from app.services.agent_identity_lifecycle import resolve_live_effect_principal

        agent = await db.get(Agent, self.agent_id, populate_existing=True)
        user = (
            (
                await self.source_binding.authorize(db, self)
                if before_command
                else await self.source_binding.revalidate(db, self)
            )
            if self.source_binding is not None
            else await db.get(User, self.user_id, populate_existing=True)
        )
        tenant = await db.get(Tenant, self.tenant_id, populate_existing=True)
        if (
            agent is None
            or agent.tenant_id != self.tenant_id
            or agent.deleted_at
            or agent.deactivated_at
            or user is None
            or not user.is_active
            or tenant is None
            or not tenant.is_active
            or user.tenant_id != self.tenant_id
            and user.role != "platform_admin"
            or (await resolve_live_effect_principal(db, agent)).block_reason is not None
        ):
            raise EnvironmentError("environment_authority_unavailable", 403)
        if self.runtime_task_id is not None:
            task = await db.get(RuntimeTask, self.runtime_task_id, populate_existing=True)
            if task is None or task.tenant_id != self.tenant_id:
                raise EnvironmentError("environment_runtime_claim_unavailable", 403)
            terminal_owner = getattr(self.source_binding, "owns_terminal_task", None)
            if terminal_owner is None or not terminal_owner(task):
                if task.status != "running":
                    raise EnvironmentError("environment_runtime_claim_unavailable", 403)
                assert_runtime_task_fence(task)

    async def _acquire(self, provider, timeout):
        self.worker_witness = capture_worker_witness()
        async with tenant_scoped_session(
            self.tenant_id, require_tenant=True, source="environment_command_attach"
        ) as db:
            await self._authority(db, before_command=True)
            await lock_environment(db, self.tenant_id, self.agent_id)
            env = await db.scalar(
                select(ExecutionEnvironment)
                .where(ExecutionEnvironment.tenant_id == self.tenant_id, ExecutionEnvironment.agent_id == self.agent_id)
                .with_for_update()
            )
            if env is None:
                env = ExecutionEnvironment(tenant_id=self.tenant_id, agent_id=self.agent_id, provider_key=provider)
                db.add(env)
                await db.flush()
            if env.desired_state != "ready":
                raise EnvironmentError("environment_stopped")
            leases = await active_leases(db, env.id)
            if leases:
                raise EnvironmentError(
                    "environment_reconciliation_required"
                    if any(row.expires_at <= datetime.now(UTC) for row in leases)
                    else "environment_commands_active"
                )
            if env.provider_key != provider:
                env.provider_key = provider
                env.generation += 1
                env.revision += 1
            env.fence_counter += 1
            env.last_used_at = datetime.now(UTC)
            env.observed_state = "ready"
            lease = EnvironmentLease(
                tenant_id=self.tenant_id,
                environment_id=env.id,
                runtime_task_id=self.runtime_task_id,
                principal_id=self.user_id,
                invocation_key=self.invocation_key,
                generation=env.generation,
                fence_version=env.fence_counter,
                expires_at=datetime.now(UTC) + timedelta(seconds=max(timeout, 1) + 60),
                outcome={"resource_lifecycle": {
                    "schema": "hive.environment.resource-lifecycle.v1",
                    "phase": "not_created", "worker": self.worker_witness,
                }},
            )
            db.add(lease)
            await db.flush()
            values = (lease.id, env.id, env.generation, lease.fence_version)
        self.lease_id, self.environment_id, self.generation, self.fence_version = values

    async def record_creation_intent(self):
        # Commit before provider creation; a crash or lost commit acknowledgement stays unknown.
        try:
            async with tenant_scoped_session(
                self.tenant_id, require_tenant=True, source="environment_resource_creation_intent"
            ) as db:
                await self._authority(db)
                await lock_environment(db, self.tenant_id, self.agent_id)
                env = await db.get(ExecutionEnvironment, self.environment_id, with_for_update=True)
                lease = await db.get(EnvironmentLease, self.lease_id, with_for_update=True)
                lifecycle = (lease.outcome or {}).get("resource_lifecycle") if lease else None
                if (
                    env is None or lease is None or lease.status != "active"
                    or lease.environment_id != env.id or env.generation != self.generation
                    or lease.generation != self.generation or lease.fence_version != self.fence_version
                    or env.desired_state != "ready" or lease.expires_at <= datetime.now(UTC)
                    or not isinstance(lifecycle, dict) or lifecycle.get("schema") != "hive.environment.resource-lifecycle.v1"
                    or lifecycle.get("worker") != self.worker_witness
                ):
                    raise EnvironmentError("environment_lease_unavailable")
                lease.outcome = {**lease.outcome, "resource_lifecycle": {**lifecycle, "phase": "creation_started"}}
        except SQLAlchemyError as exc:
            raise EnvironmentError("environment_state_unavailable", 503) from exc

    async def _check(self):
        async with tenant_scoped_session(
            self.tenant_id, require_tenant=True, source="environment_command_supervision"
        ) as db:
            await self._authority(db)
            env = await db.get(ExecutionEnvironment, self.environment_id)
            lease = await db.get(EnvironmentLease, self.lease_id)
            if (
                env is None
                or lease is None
                or lease.status != "active"
                or lease.environment_id != env.id
                or env.generation != self.generation
                or lease.generation != self.generation
                or lease.fence_version != self.fence_version
                or lease.expires_at <= datetime.now(UTC)
            ):
                raise EnvironmentError("environment_lease_unavailable")
            if env.desired_state != "ready":
                raise EnvironmentError("environment_stopped")

    async def execute(self, operation, *, provider, timeout):
        task = None
        try:
            if self.user_id is None and self.source_binding is not None:
                raise EnvironmentError("environment_source_authority_unbound", 403)
            try:
                self.tenant_id, self.agent_id, self.user_id = (
                    uuid.UUID(str(value)) for value in (self.tenant_id, self.agent_id, self.user_id)
                )
                self.runtime_task_id = uuid.UUID(str(self.runtime_task_id)) if self.runtime_task_id else None
            except (ValueError, TypeError):
                raise EnvironmentError("environment_scope_unavailable", 503)
            selected = provider_key(provider)
            try:
                if self.lease_id is None:
                    await self._acquire(selected, timeout)
                    self.selected_provider = selected
                elif selected != self.selected_provider:
                    raise EnvironmentError("environment_provider_changed")
                elif self.resource_state not in {"not_created", "command_completed", "stopped"}:
                    raise EnvironmentError("environment_reconciliation_required")
                elif self.source_binding is not None:
                    async with tenant_scoped_session(
                        self.tenant_id, require_tenant=True, source="environment_command_source"
                    ) as db:
                        await self._authority(db, before_command=True)
                await self._check()
            except EnvironmentError:
                raise
            except Exception as exc:
                raise EnvironmentError("environment_state_unavailable", 503) from exc
            self.resource_state = "not_created"
            scope_token = _COMMAND_SCOPE.set(self)
            try:
                task = asyncio.create_task(operation())
            finally:
                _COMMAND_SCOPE.reset(scope_token)
            while not task.done():
                done, _pending = await asyncio.wait({task}, timeout=0.5)
                if not done:
                    try:
                        await self._check()
                    except EnvironmentError:
                        raise
                    except Exception as exc:
                        raise EnvironmentError("environment_state_unavailable", 503) from exc
            result = task.result()
            if self.source_binding is not None:
                try:
                    await self._check()
                except EnvironmentError:
                    raise
                except Exception as exc:
                    raise EnvironmentError("environment_state_unavailable", 503) from exc
            self.outcome = {
                "status": "failed" if result.error else "completed",
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "provider_evidence": {
                    key: result.evidence[key]
                    for key in ("provider", "isolation", "sandbox_profile", "network_policy", "credential_egress")
                    if key in result.evidence
                },
            }
            return replace(result, evidence={**result.evidence, "environment": self.receipt()})
        except EnvironmentError as exc:
            status = "unavailable" if exc.status == 503 else "cancelled" if task is not None else "denied"
            self.outcome = {"status": status, "reason_code": exc.code}
            return CodeExecutionResult(
                error=f"❌ {exc.code}. Inspect the command environment in employee settings.",
                evidence={"environment": self.receipt(), "status": self.outcome["status"], "reason_code": exc.code},
            )
        finally:
            if task is not None and not task.done():
                task.cancel()
                # The provider owns process reaping; do not return a stopped receipt early.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass

    def receipt(self):
        return {
            "environment_id": str(self.environment_id) if self.environment_id else None,
            "lease_id": str(self.lease_id) if self.lease_id else None,
            "generation": self.generation,
            "fence_version": self.fence_version,
            "outcome": dict(self.outcome),
            **({"source": self.source_binding.receipt()} if self.source_binding is not None else {}),
        }

    async def release(self):
        try:
            await self._release()
        except SQLAlchemyError as exc:
            raise EnvironmentError("environment_release_unknown", 503) from exc

    async def _release(self):
        if self.lease_id is None:
            return
        async with tenant_scoped_session(
            self.tenant_id, require_tenant=True, source="environment_command_release"
        ) as db:
            await lock_environment(db, self.tenant_id, self.agent_id)
            lease = await db.get(EnvironmentLease, self.lease_id, with_for_update=True)
            env = await db.get(ExecutionEnvironment, self.environment_id, with_for_update=True)
            if lease is None or env is None or lease.fence_version != self.fence_version:
                raise EnvironmentError("environment_release_unknown", 503)
            lifecycle = (lease.outcome or {}).get("resource_lifecycle")
            if isinstance(lifecycle, dict) and self.resource_state != "not_created" and lifecycle.get("phase") == "not_created":
                lifecycle = {**lifecycle, "phase": "unconfirmed"}
            lease.outcome = {
                **({"resource_lifecycle": lifecycle} if lifecycle is not None else {}),
                **(self.outcome or {"status": "interrupted"}),
                "resource_state": self.resource_state,
                **({"source": self.source_binding.receipt()} if self.source_binding is not None else {}),
            }
            env.last_used_at = datetime.now(UTC)
            if self.resource_state in {"not_created", "command_completed", "stopped"}:
                lease.status = "released"
                lease.released_at = datetime.now(UTC)
                if env.desired_state == "stopped":
                    env.observed_state = "stopped"
                elif self.resource_state == "not_created" and self.outcome.get("status") == "failed":
                    env.observed_state = "failed"
            else:
                lease.expires_at = datetime.now(UTC)
                env.observed_state = "lost"


_COMMAND_SCOPE: ContextVar[EnvironmentCommandScope | None] = ContextVar("environment_command_scope", default=None)


@dataclass(frozen=True)
class EnvironmentCommandFrame:
    tenant_id: uuid.UUID | str | None
    agent_id: uuid.UUID | str | None
    user_id: uuid.UUID | str | None
    runtime_task_id: uuid.UUID | str | None
    source_binding: Any = field(default=None, repr=False, compare=False)


_COMMAND_FRAME: ContextVar[EnvironmentCommandFrame | None] = ContextVar("environment_command_frame", default=None)


def _command_frame(context):
    binding = getattr(context, "source_binding", None)
    parent = _COMMAND_FRAME.get()
    if (
        binding is None
        and parent is not None
        and all(str(getattr(parent, key)) == str(getattr(context, key)) for key in ("tenant_id", "user_id"))
    ):
        binding = parent.source_binding
    return EnvironmentCommandFrame(
        context.tenant_id, context.agent_id, context.user_id, context.runtime_task_id, binding
    )


@contextmanager
def command_environment_context(context):
    # Only the resolved host context supplies authority; hook payloads remain data.
    frame = _command_frame(context)
    token = _COMMAND_FRAME.set(frame)
    try:
        yield
    finally:
        _COMMAND_FRAME.reset(token)


async def execute_environment_command(operation, *, provider, timeout):
    frame = _COMMAND_FRAME.get()
    scope = current_environment_scope()
    if scope is not None and (
        frame is None
        or scope.source_binding is frame.source_binding
        and all(
            str(getattr(scope, key)) == str(getattr(frame, key))
            for key in ("tenant_id", "agent_id", "user_id", "runtime_task_id")
        )
    ):
        return await scope.execute(operation, provider=provider, timeout=timeout)
    if frame is None:
        return await operation()
    # Pre/governance/post hooks have their own command lifetime. The backend
    # retains its existing scope through canonical workspace settlement.
    async with environment_command_scope(frame, uuid.uuid4().hex) as owned:
        return await owned.execute(operation, provider=provider, timeout=timeout)


def current_environment_scope():
    return _COMMAND_SCOPE.get()


def record_environment_resource_state(state):
    scope = current_environment_scope()
    if scope is not None:
        scope.resource_state = state


async def record_environment_resource_creation(state):
    scope = current_environment_scope()
    if scope is not None:
        await scope.record_creation_intent()
    record_environment_resource_state(state)


@asynccontextmanager
async def environment_command_scope(context, invocation_key):
    # The governed runtime supplies this frame; tool arguments cannot select a resource owner.
    frame = _command_frame(context)
    scope = EnvironmentCommandScope(
        tenant_id=frame.tenant_id,
        agent_id=frame.agent_id,
        user_id=frame.user_id,
        invocation_key=invocation_key,
        runtime_task_id=frame.runtime_task_id,
        source_binding=frame.source_binding,
    )
    token = _COMMAND_SCOPE.set(scope)
    frame_token = _COMMAND_FRAME.set(
        EnvironmentCommandFrame(
            scope.tenant_id, scope.agent_id, scope.user_id, scope.runtime_task_id, scope.source_binding
        )
    )
    try:
        yield scope
    finally:
        _COMMAND_SCOPE.reset(token)
        _COMMAND_FRAME.reset(frame_token)
        if scope.lease_id is not None:
            cleanup = asyncio.create_task(scope.release())
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            cleanup.result()
            if interrupted:
                raise asyncio.CancelledError
