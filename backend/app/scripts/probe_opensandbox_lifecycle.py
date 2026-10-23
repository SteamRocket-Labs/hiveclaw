"""Explicit local-lab lifecycle conformance; never selects a production provider."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

SDK_VERSION = "1.1.1"
KEY_ENV = "HIVE_OPENSANDBOX_PROBE_API_KEY"
LABEL = "hive.conformance"
CONTENT = "CANONICAL_PROVIDER_FILE"
HEARTBEAT = (
    "import os,time; open('/tmp/probe-pid','w').write(str(os.getpid())); "
    "f=open('/tmp/probe-heartbeat','ab',buffering=0)\n"
    "while True: f.write(b'x'); time.sleep(.05)"
)


class ProbeFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def validate_target(server_url, image):
    parsed = urlsplit(server_url)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        port = parsed.port
    except ValueError:
        loopback, port = False, None
    require(
        parsed.scheme == "http"
        and loopback
        and port is not None
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"},
        "explicit_loopback_server_required",
    )
    require(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image) is not None, "immutable_image_digest_required")
    return parsed.netloc


def inspect_resource_runtime(resource_id, *, lab_id, context, expected_runtime):
    """Read the exact owned container through the trusted operator's Docker context."""
    command = ["docker", f"--context={context}"]
    try:
        found = subprocess.run([*command, "ps", "--all", "--quiet", "--no-trunc",
            f"--filter=label=opensandbox.io/id={resource_id}", f"--filter=label={LABEL}={lab_id}"],
            capture_output=True, text=True, timeout=10, check=False)
        require(found.returncode == 0, "docker_inspection_unavailable")
        identifiers = found.stdout.splitlines()
        require(len(identifiers) == 1 and re.fullmatch(r"[0-9a-f]{64}", identifiers[0]) is not None,
            "docker_resource_unavailable")
        template = ('{"runtime":{{json .HostConfig.Runtime}},'
            '"resource_id":{{json (index .Config.Labels "opensandbox.io/id")}},'
            '"lab_id":{{json (index .Config.Labels "hive.conformance")}},'
            '"privileged":{{json .HostConfig.Privileged}},"pid_mode":{{json .HostConfig.PidMode}}}')
        inspected = subprocess.run([*command, "inspect", identifiers[0], "--format", template],
            capture_output=True, text=True, timeout=10, check=False)
        require(inspected.returncode == 0, "docker_inspection_unavailable")
        facts = json.loads(inspected.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ProbeFailure("docker_inspection_unavailable") from exc
    require(isinstance(facts, dict), "docker_inspection_unavailable")
    require(facts.get("resource_id") == resource_id and facts.get("lab_id") == lab_id, "resource_ownership_changed")
    require(facts.get("runtime") == expected_runtime, "docker_runtime_changed")
    require(facts.get("privileged") is False and facts.get("pid_mode") in ("", "private"),
        "docker_workload_namespace_unsupported")
    return {"resource_id": resource_id, "container_id": identifiers[0], "runtime": expected_runtime,
        "privileged": False, "private_pid_namespace": True}


def record_resource_runtime(args, report, resource_id):
    if getattr(args, "docker_context", None) or getattr(args, "docker_runtime", None):
        require(bool(args.docker_context) and bool(args.docker_runtime), "runtime_inspection_options_required")
        report.setdefault("runtime_inspections", []).append(inspect_resource_runtime(resource_id,
            lab_id=report["lab_id"], context=args.docker_context, expected_runtime=args.docker_runtime))
        save(args.output_dir, report)


async def check_control_credential(box, secret):
    """Compare exact environment bytes outside the workload; never send its key."""
    expected = hashlib.sha256(secret.encode()).hexdigest()
    # Only the byte length crosses this boundary. Workload-side hashes contain
    # no control-key verifier unless the key was already present in its env.
    program = ("import hashlib,json,os\nn=" + str(len(secret.encode())) + "\nhashes=set()\n"
        "for value in os.environ.values():\n data=value.encode()\n"
        " for offset in range(len(data)-n+1):\n"
        "  hashes.add(hashlib.sha256(data[offset:offset+n]).hexdigest())\n"
        "print(json.dumps(sorted(hashes)))")
    execution = await box.commands.run(["python3", "-c", program])
    require(execution.error is None and execution.exit_code == 0, "control_credential_inspection_unavailable")
    try:
        digests = json.loads("".join(log.text for log in execution.logs.stdout))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProbeFailure("control_credential_inspection_unavailable") from exc
    require(isinstance(digests, list) and all(isinstance(value, str) and
        re.fullmatch(r"[0-9a-f]{64}", value) is not None for value in digests), "control_credential_inspection_unavailable")
    require(expected not in digests, "control_credential_in_workload_environment")


def save(directory, report):
    temporary = directory / "report.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(directory / "report.json")


def connection(args):
    from opensandbox.config import ConnectionConfig
    from opensandbox.transport.retry import RetryPolicy

    return ConnectionConfig(
        domain=validate_target(args.server_url, args.image),
        api_key=os.environ[KEY_ENV],
        use_server_proxy=args.server_proxy,
        retry_policy=RetryPolicy.disabled(),
        request_timeout=timedelta(seconds=10),
    )


async def server_version(server_url, api_key):
    import httpx

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        response = await client.get(server_url.rstrip("/") + "/version", headers={"OPEN-SANDBOX-API-KEY": api_key})
        require(response.status_code == 200, "server_version_unavailable")
        return response.json()["version"]


async def worker(args):
    from opensandbox import Sandbox
    from opensandbox.models.execd import RunCommandOpts

    box = await Sandbox.connect(args.worker_id, connection_config=connection(args))
    try:
        require(((await box.get_info()).metadata or {}).get(LABEL) == args.lab_id, "resource_ownership_changed")
        execution = await box.commands.run(
            ["python3", "-c", HEARTBEAT], opts=RunCommandOpts(background=True, timeout=timedelta(seconds=120))
        )
        require(execution.error is None, "background_command_failed")
        ready = await box.commands.run(
            [
                "python3",
                "-c",
                "import pathlib,time; p=pathlib.Path('/tmp/probe-heartbeat'); "
                "\nfor _ in range(100):\n if p.exists() and p.stat().st_size: break\n time.sleep(.02)"
                "\nassert p.exists() and p.stat().st_size",
            ]
        )
        require(ready.error is None and ready.exit_code == 0, "background_command_not_ready")
        print(json.dumps({"resource_id": box.id, "worker_pid": os.getpid()}), flush=True)
        await asyncio.Event().wait()
    finally:
        await box.close()


async def cleanup(manager, report):
    from opensandbox.models import SandboxFilter
    from opensandbox.exceptions.sandbox import SandboxApiException

    owned = set(report["resource_ids"])
    page = 1
    while True:
        discovered = await manager.list_sandbox_infos(SandboxFilter(metadata={LABEL: report["lab_id"]}, page=page))
        for info in discovered.sandbox_infos:
            require((info.metadata or {}).get(LABEL) == report["lab_id"], "resource_ownership_changed")
            owned.add(info.id)
        if not discovered.pagination.has_next_page:
            break
        page += 1
    report["resource_ids"] = sorted(owned)
    for resource_id in sorted(owned):
        try:
            info = await manager.get_sandbox_info(resource_id)
        except SandboxApiException as exc:
            require(exc.status_code == 404, "resource_inspection_unconfirmed")
        else:
            require((info.metadata or {}).get(LABEL) == report["lab_id"], "resource_ownership_changed")
            await manager.kill_sandbox(resource_id)
            try:
                await manager.get_sandbox_info(resource_id)
            except SandboxApiException as exc:
                require(exc.status_code == 404, "resource_termination_unconfirmed")
            else:
                raise ProbeFailure("resource_termination_unconfirmed")
    report["cleanup_confirmed"] = True


async def create_resource(args, report, conn):
    from opensandbox import Sandbox
    from opensandbox.models import NetworkPolicy

    report["creation_unconfirmed"] = True
    report["cleanup_confirmed"] = False
    save(args.output_dir, report)
    box = await Sandbox.create(
        args.image,
        timeout=timedelta(seconds=120),
        resource={"cpu": "500m", "memory": "256Mi"},
        network_policy=NetworkPolicy(default_action="deny"),
        metadata={LABEL: report["lab_id"]},
        connection_config=conn,
    )
    report["resource_ids"].append(box.id)
    report["creation_unconfirmed"] = False
    save(args.output_dir, report)
    return box


async def probe(args, report):
    from opensandbox import Sandbox, SandboxManager
    from opensandbox.models import WriteEntry

    conn = connection(args)
    manager = await SandboxManager.create(connection_config=conn)
    box, process = None, None
    try:
        report["server_version"] = await server_version(args.server_url, os.environ[KEY_ENV])
        require(report["server_version"] == SDK_VERSION, "server_version_mismatch")
        box = await create_resource(args, report, conn)
        require(((await box.get_info()).metadata or {}).get(LABEL) == report["lab_id"], "resource_ownership_changed")
        record_resource_runtime(args, report, box.id)
        if getattr(args, "check_control_credential", False):
            await check_control_credential(box, os.environ[KEY_ENV])
            report["checks"].append("workload_environment_excludes_control_credential")
        await box.files.write_files([WriteEntry(path="/tmp/probe-file", data=CONTENT, mode=600)])
        child_args = [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--server-url",
            args.server_url,
            "--image",
            args.image,
            "--output-dir",
            str(args.output_dir),
            "--worker-id",
            box.id,
            "--lab-id",
            report["lab_id"],
        ]
        if args.server_proxy:
            child_args.append("--server-proxy")
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", KEY_ENV) if key in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with (args.output_dir / "worker.log").open("w") as log:
            process = await asyncio.create_subprocess_exec(
                *child_args, env=env, stdout=asyncio.subprocess.PIPE, stderr=log
            )
            receipt = json.loads(await asyncio.wait_for(process.stdout.readline(), 20))
        require(receipt == {"resource_id": box.id, "worker_pid": process.pid}, "worker_receipt_mismatch")
        before = len(await box.files.read_file("/tmp/probe-heartbeat"))
        process.kill()
        require(await asyncio.wait_for(process.wait(), 10) == -9, "worker_termination_unconfirmed")
        report["worker_pid"] = process.pid
        await box.close()
        box = await Sandbox.connect(report["resource_ids"][0], connection_config=conn)
        await asyncio.sleep(0.15)
        require(len(await box.files.read_file("/tmp/probe-heartbeat")) > before, "worker_loss_stopped_resource")
        report["checks"].append("worker_loss_attach_and_live_background_command")
        pid = await box.files.read_file("/tmp/probe-pid")
        await box.pause()
        await box.close()
        box = await Sandbox.resume(report["resource_ids"][0], connection_config=conn)
        require(await box.files.read_file("/tmp/probe-pid") == pid, "pause_resume_process_changed")
        saved = await box.files.read_file("/tmp/probe-file")
        require(saved == CONTENT, "saved_file_changed")
        report["checks"].append("pause_resume_process_and_files")
        await box.destroy()
        box = None
        await cleanup(manager, report)
        require(report["cleanup_confirmed"], "resource_termination_unconfirmed")
        report["checks"].append("destroy_and_authoritative_not_found")
        box = await create_resource(args, report, conn)
        record_resource_runtime(args, report, box.id)
        if getattr(args, "check_control_credential", False):
            await check_control_credential(box, os.environ[KEY_ENV])
            report["checks"].append("workload_environment_excludes_control_credential")
        await box.files.write_files([WriteEntry(path="/tmp/probe-file", data=saved, mode=600)])
        execution = await box.commands.run(
            [
                "python3",
                "-c",
                "from pathlib import Path; p=Path('/tmp/probe-file'); "
                "assert p.read_text()=='CANONICAL_PROVIDER_FILE'; p.write_text(p.read_text()+'_CONTINUED')",
            ]
        )
        require(execution.error is None and execution.exit_code == 0, "restored_command_failed")
        require(await box.files.read_file("/tmp/probe-file") == CONTENT + "_CONTINUED", "restored_file_not_consumed")
        report["saved_sha256"] = hashlib.sha256(saved.encode()).hexdigest()
        report["checks"].append("new_resource_reads_and_continues_saved_bytes")
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["reason_code"] = str(exc) if isinstance(exc, ProbeFailure) else type(exc).__name__
    finally:
        local_cleanup = True
        try:
            if process is not None and process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), 10)
            if box is not None:
                await box.close()
        except Exception as exc:
            local_cleanup = False
            report["status"] = "unknown"
            report["local_cleanup_reason_code"] = type(exc).__name__
        try:
            await cleanup(manager, report)
            report["cleanup_confirmed"] = report["cleanup_confirmed"] and local_cleanup
        except Exception as exc:
            report["status"] = "unknown"
            report["cleanup_confirmed"] = False
            report["cleanup_reason_code"] = str(exc) if isinstance(exc, ProbeFailure) else type(exc).__name__
        finally:
            try:
                await manager.close()
            except Exception as exc:
                report["status"] = "unknown"
                report["manager_close_reason_code"] = type(exc).__name__
        if report.get("creation_unconfirmed"):
            # An empty listing can race a server-side create whose acknowledgement was lost.
            report["status"] = "unknown"
            report["cleanup_confirmed"] = False
            report["cleanup_reason_code"] = "creation_receipt_unconfirmed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--image", required=True, help="Python 3 workload image pinned by digest.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New evidence directory; never overwritten.")
    parser.add_argument("--server-proxy", action="store_true")
    parser.add_argument("--docker-context", help="Explicit context for optional exact-resource runtime inspection.")
    parser.add_argument("--docker-runtime", help="Expected OCI runtime in that trusted Docker context.")
    parser.add_argument("--check-control-credential", action="store_true", help="Check each workload's process environment without sending its control key.")
    parser.add_argument("--worker-id", help=argparse.SUPPRESS)
    parser.add_argument("--lab-id", help=argparse.SUPPRESS)
    args = parser.parse_args()
    validate_target(args.server_url, args.image)
    require(bool(args.docker_context) == bool(args.docker_runtime), "runtime_inspection_options_required")
    require(os.name == "posix", "posix_host_required")
    require(bool(os.environ.get(KEY_ENV)), "explicit_probe_credential_required")
    require(importlib.metadata.version("opensandbox") == SDK_VERSION, "sdk_version_mismatch")
    if args.worker_id:
        asyncio.run(worker(args))
        return
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "schema": "hive.opensandbox.lifecycle-probe.v1",
        "lab_id": str(uuid4()),
        "sdk_version": SDK_VERSION,
        "image": args.image,
        "server_proxy": args.server_proxy,
        "resource_ids": [],
        "checks": [],
        "status": "running",
        "cleanup_confirmed": False,
        "production_adoption": "unproven",
    }
    try:
        asyncio.run(probe(args, report))
    finally:
        save(args.output_dir, report)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["status"] == "passed" and report["cleanup_confirmed"] else 1)


if __name__ == "__main__":
    main()
