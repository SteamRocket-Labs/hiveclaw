"""Conservative local process observations, separate from resource termination."""

import os
import subprocess
import sys
import uuid
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _boot_session():
    try:
        if sys.platform == "linux":
            return str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
        if sys.platform != "darwin":
            return None
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            capture_output=True,
            text=True,
            check=True,
            timeout=1,
        )
        return str(uuid.UUID(result.stdout.strip()))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _linux_pid_namespace():
    try:
        namespace = Path("/proc/self/ns/pid").stat()
        return {"device": namespace.st_dev, "inode": namespace.st_ino}
    except OSError:
        return None


def capture_worker_witness():
    boot = _boot_session()
    if not boot:
        return None
    if sys.platform == "darwin":
        return {"kind": "darwin_boot_process.v1", "boot_session": boot, "pid": os.getpid()}
    if sys.platform == "linux":
        namespace = _linux_pid_namespace()
        if namespace is not None:
            return {
                "kind": "linux_boot_process.v1",
                "boot_session": boot,
                "pid": os.getpid(),
                "pid_namespace": namespace,
            }
    return None


def observe_prelaunch_worker(lifecycle):
    if not isinstance(lifecycle, dict) or lifecycle.get("schema") != "hive.environment.resource-lifecycle.v1":
        return "resource_unobserved", False
    if lifecycle.get("phase") != "not_created":
        return "resource_termination_unconfirmed", False
    reason, absent = observe_worker(lifecycle.get("worker"))
    return ("worker_terminated_before_resource_creation", True) if absent else (reason, False)


def observe_worker(worker):
    """Only same-host ESRCH proves worker absence; its resources may still exist."""
    if not isinstance(worker, dict) or worker.get("kind") not in {"darwin_boot_process.v1", "linux_boot_process.v1"}:
        return "worker_identity_unavailable", False
    boot = _boot_session()
    if boot is None or worker.get("boot_session") != boot:
        return "original_worker_host_required", False
    if worker["kind"] == "linux_boot_process.v1":
        if sys.platform != "linux":
            return "worker_identity_unavailable", False
        namespace = worker.get("pid_namespace")
        if (
            not isinstance(namespace, dict)
            or set(namespace) != {"device", "inode"}
            or any(type(value) is not int or value < 0 for value in namespace.values())
        ):
            return "worker_identity_unavailable", False
        # One boot can contain several containers with independent PID spaces.
        current = _linux_pid_namespace()
        if current is None or namespace != current:
            return "original_worker_host_required", False
    elif sys.platform != "darwin":
        return "worker_identity_unavailable", False
    pid = worker.get("pid")
    if type(pid) is not int or not 1 < pid < 2**31:
        return "worker_identity_unavailable", False
    try:
        # Even a reused PID blocks recovery. Only ESRCH on the exact boot proves absence.
        os.kill(pid, 0)
    except ProcessLookupError:
        return "worker_absent", True
    except OSError:
        return "worker_identity_unavailable", False
    return "worker_still_present", False
