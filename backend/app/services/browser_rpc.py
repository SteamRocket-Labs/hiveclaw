"""Lease-bound browser commands; Redis carries encrypted transient data only."""

import asyncio
import hashlib
import json
import math
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from redis.exceptions import RedisError

from app.agents.delegation_token import DelegationToken
from app.core.events import get_redis
from app.core.execution_context import ExecutionPrincipal
from app.database import tenant_scoped_session
from app.models.environment_browser import EnvironmentBrowser
from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment
from app.services.channel_secret_storage import decrypt_channel_secret, encrypt_channel_secret
from app.services.code_execution.contracts import CodeExecutionResult
from app.services.execution_environments import EnvironmentCommandScope, EnvironmentError, lock_environment
from app.services.runtime_task_fence import current_runtime_task_fence, reset_runtime_task_fence, set_runtime_task_fence

SCHEMA = "hive.browser.rpc.v1"
ACTIONS = frozenset({"observe", "navigate", "click", "scroll", "type", "key", "select_page"})


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _key(tenant_id, lease_id, suffix):
    return f"hive:browser:rpc:{tenant_id}:{lease_id}:{suffix}"


async def forward(source, scope, row, *, action, revision, epoch, actor_kind, arguments):
    from app.services.browser_control import BrowserControlError
    from app.services.runtime_control_bus import publish_runtime_control_event

    fence = current_runtime_task_fence()
    if scope.runtime_task_id is not None and (fence is None or str(fence.task_id) != str(scope.runtime_task_id)):
        raise BrowserControlError("browser_rpc_runtime_fence_unavailable", 503)
    encrypted = encrypt_channel_secret(json.dumps({"arguments": arguments}, ensure_ascii=False))
    async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_rpc_admission") as db:
        await scope._authority(db, before_command=True)
        await lock_environment(db, source.tenant_id, source.agent_id)
        lease = await db.get(EnvironmentLease, scope.lease_id, with_for_update=True)
        current = await db.get(EnvironmentBrowser, row.id, with_for_update=True)
        if (
            lease is None
            or current is None
            or lease.status != "active"
            or lease.environment_id != row.environment_id
            or current.worker_instance != row.worker_instance
            or current.lifecycle_nonce != row.lifecycle_nonce
            or current.principal_id != source.user_id
            or current.session_id != source.session_id
        ):
            raise BrowserControlError("browser_rpc_frame_changed")
        remaining = (lease.expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise BrowserControlError("browser_rpc_expired")
        try:
            redis = await get_redis()
            stored = await redis.set(
                _key(source.tenant_id, lease.id, "request"), encrypted, ex=math.ceil(remaining), nx=True
            )
            if not stored:
                raise BrowserControlError("browser_rpc_request_conflict")
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            raise BrowserControlError("browser_original_worker_required", 503) from exc
        scope.resource_state = "unknown"
        scope.resource_receipt = {
            "kind": "browser",
            "transport": "browser_rpc",
            "browser_resource_id": str(row.id),
            "lifecycle_nonce": row.lifecycle_nonce,
            "action": action,
            "effect": "pending",
        }
        lease.outcome = {
            **lease.outcome,
            "browser_rpc": {
                "schema": SCHEMA,
                "phase": "pending",
                "resource_id": str(row.id),
                "owner": row.worker_instance,
                "lifecycle_nonce": row.lifecycle_nonce,
                "request_digest": _digest(encrypted),
                "action": action,
                "revision": revision,
                "epoch": epoch,
                "actor_kind": actor_kind,
                "tool_policy": source.receipt().get("tool_policy"),
                "task_fence": {
                    "task_id": str(fence.task_id),
                    "claim_version": fence.claim_version,
                    "worker_id": fence.worker_id,
                }
                if fence is not None
                else None,
            },
        }
    # An advisory wakeup cannot grant authority or recreate an expired request.
    try:
        await publish_runtime_control_event(
            {
                "type": "browser_control_request",
                "tenant_id": str(source.tenant_id),
                "resource_id": str(row.id),
                "lease_id": str(scope.lease_id),
            }
        )
    except (RedisError, OSError, asyncio.TimeoutError):
        pass  # The resource's supervisor also discovers exact pending leases.
    try:
        response = await redis.blpop(_key(source.tenant_id, scope.lease_id, "response"), timeout=math.ceil(remaining))
    except (RedisError, OSError, asyncio.TimeoutError) as exc:
        raise BrowserControlError("browser_rpc_response_unconfirmed", 503, effect="unknown") from exc
    if response is None:
        raise BrowserControlError("browser_rpc_response_unconfirmed", 503, effect="unknown")
    encoded = response[1].decode() if isinstance(response[1], bytes) else response[1]
    async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_rpc_response") as db:
        await scope._authority(db)
        lease = await db.get(EnvironmentLease, scope.lease_id)
        rpc = (lease.outcome or {}).get("browser_rpc") if lease is not None else None
        if not rpc or rpc.get("phase") != "finished" or rpc.get("response_digest") != _digest(encoded):
            raise BrowserControlError("browser_rpc_response_unconfirmed", 503, effect="unknown")
        scope.resource_state = rpc["resource_state"]
        scope.resource_receipt = dict(rpc["resource_receipt"])
    await scope._check()
    payload = json.loads(decrypt_channel_secret(encoded))
    if payload.get("error"):
        error = payload["error"]
        raise BrowserControlError(error["code"], error["status"], effect=error["effect"])
    result = payload["result"]
    if isinstance(result.get("browser"), dict):
        result["browser"] = {**result["browser"], "attached_here": False, "control_transport": "remote_rpc"}
    return result


async def run(service, resource_id, lease_id):
    from app.services.browser_control import BrowserControlError

    source = service._sources.get(resource_id)
    page = service._pages.get(resource_id)
    if source is None or page is None or service._closing:
        return
    token = None
    scope = None
    try:
        async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_rpc_claim") as db:
            await lock_environment(db, source.tenant_id, source.agent_id)
            lease = await db.get(EnvironmentLease, lease_id, with_for_update=True)
            row = await db.get(EnvironmentBrowser, resource_id, with_for_update=True)
            rpc = dict((lease.outcome or {}).get("browser_rpc") or {}) if lease is not None else {}
            if (
                row is None
                or lease is None
                or lease.status != "active"
                or lease.expires_at <= datetime.now(UTC)
                or rpc.get("schema") != SCHEMA
                or rpc.get("phase") != "pending"
                or rpc.get("owner") != service.instance_id
                or rpc.get("resource_id") != str(resource_id)
                or rpc.get("lifecycle_nonce") != row.lifecycle_nonce
                or row.worker_instance != service.instance_id
                or page.pipe.lifecycle.nonce != row.lifecycle_nonce
                or lease.environment_id != row.environment_id
                or lease.generation != row.generation
                or lease.principal_id != source.user_id
                or row.principal_id != source.user_id
                or row.session_id != source.session_id
                or rpc.get("action") not in ACTIONS
            ):
                return
            env = await db.get(ExecutionEnvironment, row.environment_id)
            binding = rpc.get("tool_policy")
            if binding is not None:
                required = {"tool_name", "policy_snapshot_hash"}
                if not isinstance(binding, dict) or not required <= set(binding) <= required | {
                    "approval_id", "delegation_token", "execution_principal"
                }:
                    return
                if "approval_id" in binding:
                    binding = {**binding, "approval_id": UUID(binding["approval_id"])}
                if "delegation_token" in binding:
                    token = dict(binding["delegation_token"])
                    token["parent_agent_id"] = UUID(token["parent_agent_id"])
                    token["child_agent_id"] = UUID(token["child_agent_id"])
                    token["granted_capabilities"] = frozenset(token["granted_capabilities"])
                    binding = {**binding, "delegation_token": DelegationToken(**token)}
                if "execution_principal" in binding:
                    binding = {**binding, "execution_principal": ExecutionPrincipal.from_evidence(binding["execution_principal"])}
                source = replace(source, **binding)
            scope = EnvironmentCommandScope(
                source.tenant_id,
                source.agent_id,
                source.user_id,
                lease.invocation_key,
                lease.runtime_task_id,
                lease_id=lease.id,
                environment_id=env.id,
                generation=lease.generation,
                fence_version=lease.fence_version,
                selected_provider=env.provider_key,
                source_binding=source,
                session_id=source.session_id,
            )
            captured = rpc.get("task_fence")
            if scope.runtime_task_id is not None:
                if not captured or captured.get("task_id") != str(scope.runtime_task_id):
                    return
                token = set_runtime_task_fence(**captured)
            await scope._authority(db, before_command=True)
            rpc["phase"] = "executing"
            lease.outcome = {**lease.outcome, "browser_rpc": rpc}
        redis = await get_redis()
        encrypted = await redis.get(_key(source.tenant_id, lease_id, "request"))
        if isinstance(encrypted, bytes):
            encrypted = encrypted.decode()
        if not encrypted or _digest(encrypted) != rpc["request_digest"]:
            raise BrowserControlError("browser_rpc_payload_unavailable", 503)
        arguments = json.loads(decrypt_channel_secret(encrypted))["arguments"]
        payload = {}

        async def operation():
            nonlocal payload
            payload = await service._run_owned(
                source, scope, rpc["action"], rpc["revision"], rpc["epoch"], rpc["actor_kind"], arguments
            )
            return CodeExecutionResult(evidence={"provider": "trusted_host_browser_device"})

        result = await scope.execute(operation, provider=scope.selected_provider, timeout=30)
        if result.error:
            raise BrowserControlError(
                result.evidence.get("reason_code", "browser_rpc_failed"),
                503,
                effect=scope.resource_receipt.get("effect", "unknown"),
            )
        response = {"result": payload}
    except EnvironmentError as exc:
        response = {"error": {"code": exc.code, "status": exc.status, "effect": getattr(exc, "effect", "unknown")}}
    except asyncio.CancelledError:
        response = {"error": {"code": "browser_worker_stopping", "status": 503, "effect": "unknown"}}
    except Exception:
        response = {"error": {"code": "browser_rpc_failed", "status": 503, "effect": "unknown"}}
    else:
        pass
    finally:
        if token is not None:
            reset_runtime_task_fence(token)
    if scope is None:
        return
    encoded = encrypt_channel_secret(json.dumps(response, ensure_ascii=False, default=str))
    async with tenant_scoped_session(source.tenant_id, require_tenant=True, source="browser_rpc_result") as db:
        await lock_environment(db, source.tenant_id, source.agent_id)
        lease = await db.get(EnvironmentLease, lease_id, with_for_update=True)
        latest = dict((lease.outcome or {}).get("browser_rpc") or {}) if lease else {}
        if latest.get("phase") != "executing" or latest.get("owner") != service.instance_id:
            return
        latest.update(
            {
                "phase": "finished",
                "response_digest": _digest(encoded),
                "resource_state": scope.resource_state,
                "resource_receipt": scope.resource_receipt,
            }
        )
        lease.outcome = {**lease.outcome, "browser_rpc": latest}
    redis = await get_redis()
    key = _key(source.tenant_id, lease_id, "response")
    await redis.rpush(key, encoded)
    await redis.expire(key, 90)
    await redis.delete(_key(source.tenant_id, lease_id, "request"))
