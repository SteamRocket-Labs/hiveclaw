"""A process observation on another boot cannot settle the original worker's lease."""

import os

import pytest

from app.services import environment_worker_witness as witness


def test_wrong_host_or_legacy_witness_never_probes_a_local_pid(monkeypatch):
    monkeypatch.setattr(witness, "_boot_session", lambda: "current-boot")

    def unexpected(*_args):
        raise AssertionError("A different host's PID is not a local resource identity")

    monkeypatch.setattr(os, "kill", unexpected)
    lifecycle = {
        "schema": "hive.environment.resource-lifecycle.v1",
        "phase": "not_created",
        "worker": {"kind": "darwin_boot_process.v1", "boot_session": "different-boot", "pid": 123},
    }
    assert witness.observe_prelaunch_worker(lifecycle) == ("original_worker_host_required", False)
    assert witness.observe_prelaunch_worker(None) == ("resource_unobserved", False)


def test_permission_or_platform_failure_is_not_process_absence(monkeypatch):
    monkeypatch.setattr(witness, "_boot_session", lambda: "current-boot")

    def unavailable(*_args):
        raise PermissionError("synthetic-private-process-error")

    monkeypatch.setattr(os, "kill", unavailable)
    lifecycle = {
        "schema": "hive.environment.resource-lifecycle.v1",
        "phase": "not_created",
        "worker": {"kind": "darwin_boot_process.v1", "boot_session": "current-boot", "pid": 123},
    }
    assert witness.observe_prelaunch_worker(lifecycle) == ("worker_identity_unavailable", False)


BOOT = "01234567-89ab-4cde-8fab-0123456789ab"
NAMESPACE = {"device": 4, "inode": 4026532000}


def linux_host(monkeypatch, namespace=NAMESPACE):
    monkeypatch.setattr(witness.sys, "platform", "linux")
    monkeypatch.setattr(witness, "_boot_session", lambda: BOOT)
    monkeypatch.setattr(witness, "_linux_pid_namespace", lambda: namespace, raising=False)


def test_linux_capture_preserves_the_boot_and_pid_namespace(monkeypatch):
    linux_host(monkeypatch)
    assert witness.capture_worker_witness() == {
        "kind": "linux_boot_process.v1",
        "boot_session": BOOT,
        "pid": os.getpid(),
        "pid_namespace": NAMESPACE,
    }


@pytest.mark.parametrize("change", ["boot", "namespace", "missing", "unobservable"])
def test_linux_other_namespace_or_missing_identity_never_probes_a_local_pid(monkeypatch, change):
    linux_host(monkeypatch, None if change == "unobservable" else NAMESPACE)
    worker = {"kind": "linux_boot_process.v1", "boot_session": BOOT, "pid": 123, "pid_namespace": NAMESPACE}
    if change == "boot":
        worker["boot_session"] = "different-boot"
    elif change == "namespace":
        worker["pid_namespace"] = {**NAMESPACE, "inode": NAMESPACE["inode"] + 1}
    elif change == "missing":
        worker.pop("pid_namespace")

    def unexpected(*_args):
        raise AssertionError("An unavailable or foreign namespace is not a local PID identity")

    monkeypatch.setattr(os, "kill", unexpected)
    expected = "worker_identity_unavailable" if change == "missing" else "original_worker_host_required"
    assert witness.observe_worker(worker) == (expected, False)


@pytest.mark.parametrize("state", ["alive", "denied", "absent"])
def test_linux_only_exact_local_absence_can_settle_a_prelaunch_lease(monkeypatch, state):
    linux_host(monkeypatch)
    worker = {"kind": "linux_boot_process.v1", "boot_session": BOOT, "pid": 123, "pid_namespace": NAMESPACE}

    def probe(pid, signal):
        assert pid == 123 and signal == 0
        if state == "absent":
            raise ProcessLookupError()
        if state == "denied":
            raise PermissionError()

    monkeypatch.setattr(os, "kill", probe)
    reason = {"alive": "worker_still_present", "denied": "worker_identity_unavailable", "absent": "worker_absent"}[
        state
    ]
    assert witness.observe_worker(worker) == (reason, state == "absent")
    lifecycle = {"schema": "hive.environment.resource-lifecycle.v1", "phase": "not_created", "worker": worker}
    assert witness.observe_prelaunch_worker(lifecycle) == (
        ("worker_terminated_before_resource_creation", True) if state == "absent" else (reason, False)
    )
    assert witness.observe_prelaunch_worker({**lifecycle, "phase": "creation_started"}) == (
        "resource_termination_unconfirmed",
        False,
    )


def test_linux_capture_without_a_namespace_remains_unavailable(monkeypatch):
    linux_host(monkeypatch, None)
    assert witness.capture_worker_witness() is None


@pytest.mark.parametrize("value", [BOOT.upper(), "invalid-boot-id"])
def test_linux_boot_id_comes_from_the_kernel_file_and_rejects_invalid_values(monkeypatch, tmp_path, value):
    boot_file = tmp_path / "boot-id"
    boot_file.write_text(value)

    def kernel_path(path):
        assert path == "/proc/sys/kernel/random/boot_id"
        return boot_file

    monkeypatch.setattr(witness.sys, "platform", "linux")
    monkeypatch.setattr(witness, "Path", kernel_path, raising=False)
    witness._boot_session.cache_clear()
    try:
        assert witness._boot_session() == (BOOT if value != "invalid-boot-id" else None)
    finally:
        witness._boot_session.cache_clear()
