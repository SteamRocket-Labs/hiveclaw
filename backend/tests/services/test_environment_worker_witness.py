"""A process observation on another boot cannot settle the original worker's lease."""

import os

from app.services import environment_worker_witness as witness


def test_wrong_host_or_legacy_witness_never_probes_a_local_pid(monkeypatch):
    monkeypatch.setattr(witness, "_boot_session", lambda: "current-boot")

    def unexpected(*_args):
        raise AssertionError("A different host's PID is not a local resource identity")

    monkeypatch.setattr(os, "kill", unexpected)
    lifecycle = {"schema": "hive.environment.resource-lifecycle.v1", "phase": "not_created",
                 "worker": {"kind": "darwin_boot_process.v1", "boot_session": "different-boot", "pid": 123}}
    assert witness.observe_prelaunch_worker(lifecycle) == ("original_worker_host_required", False)
    assert witness.observe_prelaunch_worker(None) == ("resource_unobserved", False)


def test_permission_or_platform_failure_is_not_process_absence(monkeypatch):
    monkeypatch.setattr(witness, "_boot_session", lambda: "current-boot")

    def unavailable(*_args):
        raise PermissionError("synthetic-private-process-error")

    monkeypatch.setattr(os, "kill", unavailable)
    lifecycle = {"schema": "hive.environment.resource-lifecycle.v1", "phase": "not_created",
                 "worker": {"kind": "darwin_boot_process.v1", "boot_session": "current-boot", "pid": 123}}
    assert witness.observe_prelaunch_worker(lifecycle) == ("worker_identity_unavailable", False)
