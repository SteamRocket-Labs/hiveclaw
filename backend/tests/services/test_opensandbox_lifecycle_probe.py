"""The destructive conformance probe rejects external targets before opening a client."""

import pytest

from app.scripts.probe_opensandbox_lifecycle import ProbeFailure, validate_target, server_version

IMAGE = "python@sha256:" + "a" * 64


@pytest.mark.parametrize(
    "target",
    [
        "http://example.com:8080",
        "http://127.0.0.1:8080?token=x",
        "http://user:secret@127.0.0.1:8080",
        "http://127.0.0.1:8080/v1",
    ],
)
def test_external_or_ambiguous_target_is_rejected_before_resource_creation(target):
    with pytest.raises(ProbeFailure, match="^explicit_loopback_server_required$"):
        validate_target(target, IMAGE)


def test_floating_workload_image_cannot_claim_a_reproducible_profile():
    with pytest.raises(ProbeFailure, match="^immutable_image_digest_required$"):
        validate_target("http://127.0.0.1:8080", "python:3.12-alpine")
    assert validate_target("http://127.0.0.1:8080", IMAGE) == "127.0.0.1:8080"


async def test_version_redirect_cannot_forward_the_control_credential(monkeypatch):
    import httpx

    requests = []
    original = httpx.AsyncClient

    def redirect(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.com/collect"})

    def local_client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(redirect))

    monkeypatch.setattr(httpx, "AsyncClient", local_client)
    with pytest.raises(ProbeFailure, match="^server_version_unavailable$"):
        await server_version("http://127.0.0.1:8080", "synthetic-control-credential")
    assert requests == ["http://127.0.0.1:8080/version"]


async def test_lost_create_ack_cannot_claim_cleanup_from_one_empty_listing(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from app.scripts import probe_opensandbox_lifecycle as module

    async def version(*_args):
        return module.SDK_VERSION

    async def ambiguous_create(*_args, **_kwargs):
        raise TimeoutError("The server may still be creating the resource")

    class Manager:
        @classmethod
        async def create(cls, **_kwargs):
            return cls()

        async def list_sandbox_infos(self, _filter):
            return SimpleNamespace(sandbox_infos=[], pagination=SimpleNamespace(has_next_page=False))

        async def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "opensandbox",
        SimpleNamespace(Sandbox=SimpleNamespace(create=ambiguous_create), SandboxManager=Manager),
    )
    monkeypatch.setitem(
        sys.modules,
        "opensandbox.models",
        SimpleNamespace(NetworkPolicy=lambda **_kwargs: None, WriteEntry=None, SandboxFilter=lambda **_kwargs: None),
    )
    monkeypatch.setitem(sys.modules, "opensandbox.exceptions", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "opensandbox.exceptions.sandbox",
        SimpleNamespace(SandboxApiException=type("SDKError", (Exception,), {})),
    )
    monkeypatch.setattr(module, "connection", lambda _args: None)
    monkeypatch.setattr(module, "server_version", version)
    monkeypatch.setenv(module.KEY_ENV, "synthetic-only")
    report = {"resource_ids": [], "lab_id": "owned-probe", "checks": []}
    await module.probe(SimpleNamespace(server_url="http://127.0.0.1:8080", image=IMAGE, output_dir=tmp_path), report)
    assert report["status"] == "unknown"
    assert report["cleanup_confirmed"] is False
    assert report["cleanup_reason_code"] == "creation_receipt_unconfirmed"


@pytest.mark.parametrize("change,code", [
    ("same", None), ("runtime", "docker_runtime_changed"), ("resource", "resource_ownership_changed"),
    ("lab", "resource_ownership_changed"), ("privileged", "docker_workload_namespace_unsupported"),
    ("host_pid", "docker_workload_namespace_unsupported"), ("multiple", "docker_resource_unavailable"),
    ("missing", "docker_resource_unavailable"), ("invalid_json", "docker_inspection_unavailable"),
])
def test_runtime_inspection_uses_only_the_exact_owned_container(monkeypatch, change, code):
    import json
    from types import SimpleNamespace
    from uuid import uuid4
    from app.scripts import probe_opensandbox_lifecycle as module

    resource_id, lab_id, container_id = str(uuid4()), str(uuid4()), "a" * 64
    facts = {"resource_id": resource_id, "lab_id": lab_id, "runtime": "kata-runtime", "privileged": False,
        "pid_mode": "", "unrelated_secret": "DO_NOT_EXPORT_CONTAINER_SECRET"}
    if change == "runtime":
        facts["runtime"] = "runc"
    if change in {"resource", "lab"}:
        facts[change + "_id"] = str(uuid4())
    if change == "privileged":
        facts["privileged"] = True
    if change == "host_pid":
        facts["pid_mode"] = "host"
    ids = "" if change == "missing" else container_id + ("\n" + "b" * 64 if change == "multiple" else "")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[:2] == ["docker", "--context=approved-context"]
        assert kwargs["capture_output"] and kwargs["timeout"] == 10
        return SimpleNamespace(returncode=0, stdout=ids if len(calls) == 1 else
            "invalid" if change == "invalid_json" else json.dumps(facts))

    monkeypatch.setattr(module.subprocess, "run", run)
    if code:
        with pytest.raises(ProbeFailure, match="^" + code + "$"):
            module.inspect_resource_runtime(resource_id, lab_id=lab_id, context="approved-context", expected_runtime="kata-runtime")
    else:
        actual = module.inspect_resource_runtime(resource_id, lab_id=lab_id, context="approved-context", expected_runtime="kata-runtime")
        assert actual == {"resource_id": resource_id, "container_id": container_id, "runtime": "kata-runtime",
            "privileged": False, "private_pid_namespace": True}
        assert "DO_NOT_EXPORT" not in json.dumps(actual)
    assert calls[0][-2:] == [f"--filter=label=opensandbox.io/id={resource_id}", f"--filter=label={module.LABEL}={lab_id}"]
    assert len(calls) == (1 if change in {"multiple", "missing"} else 2)
    if len(calls) == 2:
        assert calls[1][2:5] == ["inspect", container_id, "--format"]


@pytest.mark.parametrize("flag", ["--docker-context", "--docker-runtime"])
def test_partial_runtime_inspection_options_fail_before_sdk_or_resource_use(monkeypatch, tmp_path, flag):
    import sys
    from app.scripts import probe_opensandbox_lifecycle as module

    monkeypatch.setattr(sys, "argv", ["probe", "--server-url", "http://127.0.0.1:8080", "--image", IMAGE,
        "--output-dir", str(tmp_path / "evidence"), flag, "selected"])
    with pytest.raises(ProbeFailure, match="^runtime_inspection_options_required$"):
        module.main()
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("gate", ["runtime", "credential"])
async def test_runtime_mismatch_stops_before_work_and_cleans_its_exact_resource(monkeypatch, tmp_path, gate):
    import sys
    from types import SimpleNamespace
    from uuid import uuid4
    from app.scripts import probe_opensandbox_lifecycle as module

    resource_id, lab_id = str(uuid4()), str(uuid4())
    calls = []

    class SDKError(Exception):
        status_code = 404

    class Box:
        id = resource_id

        async def get_info(self):
            return SimpleNamespace(metadata={module.LABEL: lab_id})

        async def close(self):
            calls.append("box_closed")

    class Manager:
        alive = True

        @classmethod
        async def create(cls, **_kwargs):
            return cls()

        async def list_sandbox_infos(self, _filter):
            return SimpleNamespace(sandbox_infos=[], pagination=SimpleNamespace(has_next_page=False))

        async def get_sandbox_info(self, identifier):
            assert identifier == resource_id
            if not self.alive:
                raise SDKError()
            return SimpleNamespace(metadata={module.LABEL: lab_id})

        async def kill_sandbox(self, identifier):
            assert identifier == resource_id
            self.alive = False
            calls.append("resource_killed")

        async def close(self):
            calls.append("manager_closed")

    async def create(*_args, **_kwargs):
        return Box()

    async def version(*_args):
        return module.SDK_VERSION

    def reject(identifier, **kwargs):
        assert identifier == resource_id and kwargs["lab_id"] == lab_id
        calls.append("runtime_checked")
        raise ProbeFailure("docker_runtime_changed")

    async def reject_credential(box, secret):
        assert box.id == resource_id and secret == "synthetic-only"
        calls.append("credential_checked")
        raise ProbeFailure("control_credential_in_workload_environment")

    monkeypatch.setitem(sys.modules, "opensandbox", SimpleNamespace(Sandbox=SimpleNamespace(create=create), SandboxManager=Manager))
    monkeypatch.setitem(sys.modules, "opensandbox.models", SimpleNamespace(NetworkPolicy=lambda **_kwargs: None,
        WriteEntry=None, SandboxFilter=lambda **_kwargs: None))
    monkeypatch.setitem(sys.modules, "opensandbox.exceptions", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "opensandbox.exceptions.sandbox", SimpleNamespace(SandboxApiException=SDKError))
    monkeypatch.setattr(module, "connection", lambda _args: None)
    monkeypatch.setattr(module, "server_version", version)
    monkeypatch.setattr(module, "inspect_resource_runtime", reject)
    monkeypatch.setattr(module, "check_control_credential", reject_credential)
    monkeypatch.setenv(module.KEY_ENV, "synthetic-only")
    report = {"resource_ids": [], "lab_id": lab_id, "checks": []}
    await module.probe(SimpleNamespace(server_url="http://127.0.0.1:8080", image=IMAGE, output_dir=tmp_path,
        docker_context="approved-context" if gate == "runtime" else None,
        docker_runtime="kata-runtime" if gate == "runtime" else None, check_control_credential=gate == "credential"), report)
    expected_code = "docker_runtime_changed" if gate == "runtime" else "control_credential_in_workload_environment"
    assert report["status"] == "failed" and report["reason_code"] == expected_code
    assert report["cleanup_confirmed"] is True and report["creation_unconfirmed"] is False
    assert calls == ["runtime_checked" if gate == "runtime" else "credential_checked",
        "box_closed", "resource_killed", "manager_closed"]


@pytest.mark.parametrize("placement", ["absent", "exact", "embedded"])
async def test_credential_canary_program_never_receives_or_prints_its_control_key(placement):
    import json
    import subprocess
    import sys
    from types import SimpleNamespace
    from app.scripts import probe_opensandbox_lifecycle as module

    secret = "synthetic-control-credential-12345"
    executions = []

    async def run(command):
        assert secret not in json.dumps(command)
        value = "safe-public-value" if placement == "absent" else secret if placement == "exact" else "prefix:" + secret + ":suffix"
        result = subprocess.run([sys.executable, *command[1:]], env={"EXPLICIT_SYNTHETIC_CANARY": value},
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0 and secret not in result.stdout + result.stderr
        executions.append(result)
        return SimpleNamespace(error=None, exit_code=result.returncode, logs=SimpleNamespace(stdout=[SimpleNamespace(text=result.stdout)]))

    box = SimpleNamespace(commands=SimpleNamespace(run=run))
    if placement == "absent":
        await module.check_control_credential(box, secret)
    else:
        with pytest.raises(ProbeFailure, match="^control_credential_in_workload_environment$"):
            await module.check_control_credential(box, secret)
    assert len(executions) == 1
