"""Opt-in persistent compute cache behind the existing original-work lease."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import io
import json
import os
import re
import tarfile
import uuid
from datetime import timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment
from app.services.code_execution.contracts import CodeExecutionResult
from app.services.code_execution.env_policy import sanitize_agent_execution_env
from app.services.execution_environments import EnvironmentError, current_environment_scope, lock_environment

ROOT = "/hive/"
WORK = ROOT + "workspace"
HOME = ROOT + "home"


def _require(condition, code, status=503):
    if not condition:
        raise EnvironmentError(code, status)


def load_profile():
    """Only host policy can pin a profile; a report's verdict grants no authority."""
    path = os.environ.get("HIVE_OPENSANDBOX_PROFILE_PATH")
    approved = os.environ.get("HIVE_OPENSANDBOX_APPROVED_PROFILE_SHA256", "")
    _require(path and re.fullmatch(r"[0-9a-f]{64}", approved), "compute_profile_not_approved")
    try:
        raw = Path(path).read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == approved, "compute_profile_changed")
        profile = json.loads(raw)
        endpoint = urlsplit(profile["server_url"])
        _require(endpoint.scheme in {"http", "https"} and endpoint.hostname and not endpoint.username
            and not endpoint.password and not endpoint.query and not endpoint.fragment
            and endpoint.path in {"", "/"}, "compute_profile_invalid")
        _require(endpoint.scheme == "https" or endpoint.hostname in {"127.0.0.1", "::1", "localhost"},
            "compute_control_transport_unsupported")
        _require(profile["schema"] == "hive.compute-profile.v1" and profile["sdk_version"] == "1.1.1"
            and re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", profile["image"])
            and re.fullmatch(r"[0-9a-f]{64}", profile["conformance_sha256"])
            and isinstance(profile["isolation"], str) and profile["isolation"]
            and type(profile["ttl_seconds"]) is int and profile["ttl_seconds"] > 0
            and set(profile["resource"]) == {"cpu", "memory"}
            and all(isinstance(v, str) and v for v in profile["resource"].values()), "compute_profile_invalid")
        _require(os.environ.get("HIVE_OPENSANDBOX_API_KEY"), "compute_control_credential_unavailable")
        _require(importlib.metadata.version("opensandbox") == "1.1.1", "compute_sdk_version_mismatch")
    except (OSError, ValueError, KeyError, TypeError, importlib.metadata.PackageNotFoundError) as exc:
        raise EnvironmentError("compute_profile_unavailable", 503) from exc
    return profile, approved


def _context(scope):
    return {key: str(getattr(scope, key)) for key in (
        "tenant_id", "agent_id", "user_id", "session_id", "runtime_task_id",
        "machine_credential_id", "external_principal_id")}


async def _binding(scope, *, value=None, write=False):
    async with tenant_scoped_session(scope.tenant_id, require_tenant=True, source="persistent_compute_binding") as db:
        await lock_environment(db, scope.tenant_id, scope.agent_id)
        environment = await db.get(ExecutionEnvironment, scope.environment_id, with_for_update=True)
        lease = await db.get(EnvironmentLease, scope.lease_id, with_for_update=True)
        _require(environment is not None and lease is not None and lease.status == "active"
            and environment.generation == scope.generation and lease.fence_version == scope.fence_version,
            "environment_lease_unavailable", 409)
        if write:
            environment.resource_binding = value
        return environment.resource_binding


async def _dependencies(scope, previous):
    """Revalidate every retained input through the current original-work reader."""
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.content_input_receipts import validate_runtime_input_dependencies
    from app.services.session_model_inputs import _model_dependencies
    from app.services.session_tool_runtime import _sha256

    request = scope.runtime_request
    _require(request is not None and scope.session_id and scope.runtime_task_id,
        "compute_original_work_required", 409)
    context = request.context
    inputs = getattr(context.workspace_authority_scope, "execution_inputs", None)
    snapshot = inputs.snapshot() if inputs is not None else {}
    _require(snapshot.get("observed") and not snapshot.get("unbound"), "compute_input_receipts_unavailable", 409)
    async with tenant_scoped_session(scope.tenant_id, require_tenant=True, source="persistent_compute_inputs") as db:
        await scope._authority(db)
        invocation = (await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.tenant_id == scope.tenant_id,
            SessionToolInvocation.session_id == uuid.UUID(str(scope.session_id)),
            SessionToolInvocation.run_id == scope.runtime_task_id,
            SessionToolInvocation.provider_tool_use_id == scope.invocation_key,
            SessionToolInvocation.tool_name == request.tool_name,
        ))).one_or_none()
        _require(invocation is not None and (invocation.effective_args_hash or invocation.args_hash)
            == _sha256({"tool_name": request.tool_name, "arguments": request.arguments}),
            "compute_original_invocation_unavailable", 409)
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.tenant_id == scope.tenant_id,
            SessionModelResult.session_id == invocation.session_id,
            SessionModelResult.run_id == invocation.run_id,
            SessionModelResult.provider_request_id == invocation.provider_request_id))
        sources, complete = _model_dependencies(model)
        _require(complete, "compute_model_inputs_unavailable", 409)
        sources = [*sources, *snapshot["sources"], *previous]
        unique = sorted({(row["source_kind"], row["source_id"], row["source_version"]) for row in sources})
        sources = [dict(zip(("source_kind", "source_id", "source_version"), row)) for row in unique]
        await validate_runtime_input_dependencies(db, request, sources)
        return sources


def _archive(work_dir):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(work_dir.rglob("*")):
            _require(not path.is_symlink(), "compute_workspace_link_unsupported", 409)
            if path.is_file():
                archive.add(path, arcname=path.relative_to(work_dir).as_posix(), recursive=False)
    return buffer.getvalue()


def _outputs(data):
    # Validate the whole untrusted archive before changing the staged workspace.
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            _require(not path.is_absolute() and ".." not in path.parts and not member.issym()
                and not member.islnk() and not member.isdev(), "compute_output_archive_invalid", 409)
            if member.isdir():
                continue
            _require(member.isfile() and path.parts and path.as_posix() not in files,
                "compute_output_archive_invalid", 409)
            with archive.extractfile(member) as source:
                files[path.as_posix()] = (source.read(), member.mode & 0o777)
    _require(not any(parent.as_posix() in files for name in files
        for parent in PurePosixPath(name).parents), "compute_output_archive_invalid", 409)
    return files


def _merge(files, work_dir):
    # This is the disposable authorized view. Its existing owner/source-aware
    # context manager, not the provider, commits it to the canonical workspace.
    for path in sorted(work_dir.rglob("*"), reverse=True):
        if path.is_file() and path.relative_to(work_dir).as_posix() not in files:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for relative, (data, mode) in files.items():
        target = work_dir / relative
        _require(not target.is_symlink(), "compute_output_archive_invalid", 409)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)


async def _absent(manager, resource_id):
    try:
        await manager.get_sandbox_info(resource_id)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404:
            return True
        raise
    return False


async def _destroy(manager, scope, binding):
    await _binding(scope, value={**binding, "state": "termination_unknown"}, write=True)
    scope.resource_state = "unknown"
    await manager.kill_sandbox(binding["resource_id"])
    _require(await _absent(manager, binding["resource_id"]), "compute_termination_unconfirmed", 409)
    await _binding(scope, value=None, write=True)
    scope.resource_state = "stopped"


async def reconcile_resource(db, environment, lease):
    """Close only the exact known resource after the original worker is absent."""
    from app.services.environment_worker_witness import observe_worker

    binding = environment.resource_binding
    lifecycle = (lease.outcome or {}).get("resource_lifecycle") or {}
    reason, absent = observe_worker(lifecycle.get("worker"))
    _require(absent, reason, 409)
    _require(binding and binding.get("resource_id"), "compute_creation_unconfirmed", 409)
    profile, digest = load_profile()
    _require(binding.get("profile_sha256") == digest, "compute_original_profile_required", 409)
    from opensandbox.config import ConnectionConfig
    from opensandbox.manager import SandboxManager
    from opensandbox.transport import RetryPolicy

    async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
        config = ConnectionConfig(domain=urlsplit(profile["server_url"]).netloc,
            protocol=urlsplit(profile["server_url"]).scheme,
            api_key=os.environ["HIVE_OPENSANDBOX_API_KEY"], use_server_proxy=True,
            request_timeout=timedelta(seconds=10), retry_policy=RetryPolicy.disabled(),
            transport=transport, disable_metrics=True)
        manager = await SandboxManager.create(connection_config=config)
        try:
            if not await _absent(manager, binding["resource_id"]):
                info = await manager.get_sandbox_info(binding["resource_id"])
                _require(info.metadata.get("hive.environment") == str(environment.id)
                    and info.metadata.get("hive.profile") == digest, "compute_resource_identity_unconfirmed", 409)
                await manager.kill_sandbox(binding["resource_id"])
                _require(await _absent(manager, binding["resource_id"]), "compute_termination_unconfirmed", 409)
            environment.resource_binding = None
            return {"reason_code": "original_worker_absent_resource_closed", "resource_id": binding["resource_id"],
                "profile_sha256": digest, "command_outcome": "unknown"}
        except EnvironmentError:
            raise
        except Exception as exc:
            raise EnvironmentError("compute_reconciliation_unconfirmed", 503) from exc
        finally:
            await manager.close()


async def execute_opensandbox_command(command, *, work_dir, env, timeout, network_policy=None, sandbox_spec=None):
    scope = current_environment_scope()
    evidence = {"provider": "opensandbox", "status": "unavailable", "production_adoption": "unproven",
        "network_policy": "deny-all", "credential_egress": "blocked_by_env_allowlist"}
    box = manager = transport = None
    binding = None
    finished = False
    owned = False
    try:
        profile, digest = load_profile()
        _require(scope is not None and scope.lease_id is not None, "compute_original_work_required", 409)
        _require(network_policy in {None, "deny-all"}, "compute_network_policy_unsupported", 409)
        # This candidate accepts only the existing authorized isolated staging
        # path; direct host/canonical-directory callers retain a safe refusal.
        _require(sandbox_spec is not None and scope.runtime_request is not None,
            "compute_authorized_workspace_required", 409)
        evidence.update(profile_sha256=digest, isolation=profile["isolation"], conformance_sha256=profile["conformance_sha256"])
        binding = await _binding(scope)
        if binding:
            if binding.get("state") != "paused":
                scope.resource_state = "unknown"
            _require(binding.get("profile_sha256") == digest, "compute_original_profile_required", 409)
            _require(binding.get("state") == "paused" and binding.get("resource_id"),
                "compute_resource_reconciliation_required", 409)
        prior = binding.get("dependencies", []) if binding and binding.get("context") == _context(scope) else []
        dependencies = await _dependencies(scope, prior)
        from opensandbox import Sandbox
        from opensandbox.config import ConnectionConfig
        from opensandbox.manager import SandboxManager
        from opensandbox.models import NetworkPolicy
        from opensandbox.models.execd import RunCommandOpts
        from opensandbox.transport import RetryPolicy

        transport = httpx.AsyncHTTPTransport(trust_env=False)
        config = ConnectionConfig(domain=urlsplit(profile["server_url"]).netloc,
            protocol=urlsplit(profile["server_url"]).scheme,
            api_key=os.environ["HIVE_OPENSANDBOX_API_KEY"], use_server_proxy=True,
            request_timeout=timedelta(seconds=10), retry_policy=RetryPolicy.disabled(),
            transport=transport, disable_metrics=True)
        manager = await SandboxManager.create(connection_config=config)
        if binding:
            if await _absent(manager, binding["resource_id"]):
                await _binding(scope, value=None, write=True)
                binding = None
            else:
                info = await manager.get_sandbox_info(binding["resource_id"])
                _require(info.metadata.get("hive.environment") == str(scope.environment_id)
                    and info.metadata.get("hive.profile") == digest and info.status.state == "Paused",
                    "compute_resource_identity_unconfirmed", 409)
                owned = True
                if binding["context"] != _context(scope):
                    await _destroy(manager, scope, binding)
                    binding = None
        if binding is None:
            binding = {"schema": "hive.compute-binding.v1", "profile_sha256": digest,
                "context": _context(scope), "dependencies": dependencies, "state": "creation_unknown",
                "resource_id": None, "creation_lease_id": str(scope.lease_id)}
            await scope.record_creation_intent()
            await _binding(scope, value=binding, write=True)
            scope.resource_state = "creation_unknown"
            box = await Sandbox.create(image=profile["image"], resource=profile["resource"],
                timeout=timedelta(seconds=profile["ttl_seconds"]), network_policy=NetworkPolicy(default_action="deny"),
                metadata={"hive.environment": str(scope.environment_id), "hive.profile": digest,
                    "hive.creation_lease": str(scope.lease_id)}, connection_config=config)
            binding = {**binding, "resource_id": box.id, "state": "running"}
            await _binding(scope, value=binding, write=True)
        else:
            binding = {**binding, "state": "resume_unknown", "dependencies": dependencies}
            await _binding(scope, value=binding, write=True)
            scope.resource_state = "unknown"
            box = await Sandbox.resume(binding["resource_id"], connection_config=config)
            binding = {**binding, "state": "running"}
            await _binding(scope, value=binding, write=True)
        scope.resource_state = "running"
        info = await manager.get_sandbox_info(box.id)
        _require(info.id == binding["resource_id"] and info.metadata.get("hive.environment") == str(scope.environment_id)
            and info.metadata.get("hive.profile") == digest
            and info.metadata.get("hive.creation_lease") == binding["creation_lease_id"] and info.status.state == "Running",
            "compute_resource_identity_unconfirmed", 409)
        owned = True
        evidence["resource_id"] = box.id
        safe, env_policy = sanitize_agent_execution_env(env, require_home=False)
        remote_env = {key: value for key, value in safe.items()
            if key not in {"HOME", "PATH", "TMPDIR", "TMP", "TEMP", "SSL_CERT_FILE", "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"}}
        remote_env.update(HOME=HOME, TMPDIR=ROOT + "tmp")
        evidence["env_policy"] = env_policy
        await scope._check()
        await box.commands.run(["mkdir", "-p", WORK, HOME, ROOT + "tmp"])
        await box.files.write_file(ROOT + "input.tar.gz", _archive(work_dir), mode=600)
        preparation = await box.commands.run(["bash", "-c",
            f"rm -rf {WORK} && mkdir -p {WORK} && tar -xzf {ROOT}input.tar.gz -C {WORK}"])
        _require(preparation.complete is not None and preparation.exit_code == 0, "compute_input_sync_failed", 409)
        await scope._check()
        result = await asyncio.wait_for(box.commands.run(command,
            opts=RunCommandOpts(working_directory=WORK, envs=remote_env, timeout=timedelta(seconds=timeout))), timeout)
        _require(result.complete is not None and type(result.exit_code) is int, "compute_command_outcome_unknown", 409)
        await scope._check()
        packed = await box.commands.run(["tar", "-czf", ROOT + "output.tar.gz", "-C", WORK, "."])
        _require(packed.complete is not None and packed.exit_code == 0, "compute_output_sync_failed", 409)
        files = _outputs(await box.files.read_bytes(ROOT + "output.tar.gz"))
        # Match the existing remote provider's skill-review handoff. Export only
        # .agents into the disposable local HOME; the tool's Trust Gate remains
        # the activation authority. Keep unrelated persistent HOME state private.
        packed_home = await box.commands.run(["bash", "-c",
            f"if [ -d {HOME}/.agents ]; then tar -czf {ROOT}home.tar.gz -C {HOME} .agents; "
            f"else tar -czf {ROOT}home.tar.gz -T /dev/null; fi"])
        _require(packed_home.complete is not None and packed_home.exit_code == 0, "compute_home_sync_failed", 409)
        home_files = _outputs(await box.files.read_bytes(ROOT + "home.tar.gz"))
        _require(all(name.startswith(".agents/") for name in home_files), "compute_home_archive_invalid", 409)
        cleared = await box.commands.run(["rm", "-rf", HOME + "/.agents"])
        _require(cleared.complete is not None and cleared.exit_code == 0, "compute_home_sync_failed", 409)
        await box.pause()
        info = await manager.get_sandbox_info(box.id)
        _require(info.status.state == "Paused", "compute_pause_unconfirmed", 409)
        binding = {**binding, "state": "paused"}
        await _binding(scope, value=binding, write=True)
        scope.resource_state = "paused"
        scope.resource_receipt = {"provider": "opensandbox", "resource_id": box.id, "state": "paused",
            "profile_sha256": digest, "creation_lease_id": binding["creation_lease_id"]}
        finished = True
        await scope._check()
        _merge(files, work_dir)
        if home_files:
            _require(safe.get("HOME"), "compute_home_destination_unavailable", 409)
            destination = Path(safe["HOME"]) / ".agents"
            destination.mkdir(parents=True, exist_ok=True)
            _merge({name.removeprefix(".agents/"): value for name, value in home_files.items()}, destination)
        evidence.update(status="completed", resource_state="paused")
        return CodeExecutionResult(stdout="".join(item.text for item in result.logs.stdout),
            stderr="".join(item.text for item in result.logs.stderr), exit_code=result.exit_code,
            error="❌ Sandbox command failed." if result.error else None, evidence=evidence)
    except Exception as exc:
        code = getattr(exc, "code", "compute_execution_unconfirmed")
        evidence.update(status="unknown" if scope and scope.resource_state not in {"not_created", "paused", "stopped"}
            else "denied" if getattr(exc, "status_code", getattr(exc, "status", 503)) == 403 else "unavailable",
            reason_code=code, retryable=False,
            next_action="inspect_original_resource" if binding else "configure_approved_compute_profile")
        return CodeExecutionResult(error=f"❌ {code}. Inspect the original command and compute resource before retrying.",
            timed_out=isinstance(exc, asyncio.TimeoutError), evidence=evidence)
    finally:
        async def close():
            if not finished and owned and binding and binding.get("resource_id") and manager:
                try:
                    await _destroy(manager, scope, binding)
                except Exception:
                    scope.resource_state = "unknown"
            if box:
                await box.close()
            if manager:
                await manager.close()
            if transport:
                await transport.aclose()
        cleanup = asyncio.create_task(close())
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError
