"""Pinned SDK serialization with in-memory HTTP; no server or resource is used."""

import hashlib
import io
import json
import tarfile
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("opensandbox")

from app.services.code_execution import opensandbox_provider as provider


class UnexpectedSDKRequest(BaseException):
    """Stop immediately; a readiness retry cannot hide an unsafe request."""


async def test_pinned_sdk_preserves_https_through_proxy_resume_and_file_handoff(tmp_path, monkeypatch):
    resource_id = str(uuid4())
    calls, state = [], {"binding": None, "status": "Running", "metadata": {}}
    secret = "synthetic-sdk-control-only"
    profile = {"schema": "hive.compute-profile.v1", "server_url": "https://sdk.example.invalid",
        "image": "synthetic/image@sha256:" + "c" * 64, "sdk_version": "1.1.1",
        "isolation": "synthetic-protocol-only", "conformance_sha256": "d" * 64,
        "resource": {"cpu": "500m", "memory": "256Mi"}, "ttl_seconds": 120}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    monkeypatch.setenv("HIVE_OPENSANDBOX_PROFILE_PATH", str(path))
    monkeypatch.setenv("HIVE_OPENSANDBOX_APPROVED_PROFILE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setenv("HIVE_OPENSANDBOX_API_KEY", secret)

    def archive(files):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as output:
            for name, data in files.items():
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(data), 0o644
                output.addfile(item, io.BytesIO(data))
        return buffer.getvalue()

    def info():
        return {"id": resource_id, "status": {"state": state["status"]}, "metadata": state["metadata"],
            "entrypoint": ["tail", "-f", "/dev/null"], "createdAt": "2026-01-01T00:00:00Z",
            "expiresAt": "2026-01-01T00:02:00Z"}

    async def handle(request):
        body = await request.aread()
        calls.append((request.method, str(request.url)))
        if request.url.scheme != "https" or request.url.host != "sdk.example.invalid":
            raise UnexpectedSDKRequest(f"SDK changed the approved control origin: {request.url}")
        assert secret.encode() not in body and secret not in str(request.url)
        url = request.url.path
        if request.method == "POST" and url == "/v1/sandboxes":
            payload = json.loads(body)
            assert payload["networkPolicy"]["defaultAction"] == "deny"
            assert not payload.get("env") and not payload.get("volumes")
            state["metadata"] = payload["metadata"]
            return httpx.Response(202, json=info())
        if "/endpoints/" in url:
            return httpx.Response(200, json={"endpoint": "sdk.example.invalid/v1/proxy/" + url.rsplit("/", 1)[1]})
        if url.endswith("/ping"):
            return httpx.Response(200)
        if url == f"/v1/sandboxes/{resource_id}":
            return httpx.Response(200, json=info())
        if url.endswith("/pause"):
            state["status"] = "Paused"
            return httpx.Response(204)
        if url.endswith("/resume"):
            state["status"] = "Running"
            return httpx.Response(204)
        if url.endswith("/command"):
            payload = json.loads(body)
            assert secret not in json.dumps(payload)
            events = [{"type": "init", "timestamp": 1, "id": "protocol-command"},
                {"type": "stdout", "timestamp": 2, "text": "SDK_PROTOCOL_OUTPUT"},
                {"type": "execution_complete", "timestamp": 3, "execution_time_in_millis": 1}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content="".join("data: " + json.dumps(event) + "\n\n" for event in events))
        if url.endswith("/files/upload"):
            return httpx.Response(200)
        if url.endswith("/files/download"):
            files = {} if request.url.params.get("path") == "/hive/home.tar.gz" else {
                "input.txt": b"AUTHORIZED_INPUT", "output.txt": b"SDK_PROTOCOL_FILE"}
            return httpx.Response(200, content=archive(files))
        raise UnexpectedSDKRequest(f"Unmodeled SDK request: {request.method} {request.url}")

    monkeypatch.setattr(provider.httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(handle))
    scope = SimpleNamespace(tenant_id=uuid4(), agent_id=uuid4(), user_id=uuid4(), session_id=uuid4(),
        runtime_task_id=uuid4(), machine_credential_id=None, external_principal_id=None,
        environment_id=uuid4(), lease_id=uuid4(), resource_state="not_created", runtime_request=object())

    async def check():
        return None

    async def binding(_scope, *, value=None, write=False):
        if write:
            state["binding"] = value
        return state["binding"]

    async def dependencies(*_args):
        return []  # Native authority and origin validation have separate real-DB tests.

    scope._check = scope.record_creation_intent = check
    monkeypatch.setattr(provider, "current_environment_scope", lambda: scope)
    monkeypatch.setattr(provider, "_binding", binding)
    monkeypatch.setattr(provider, "_dependencies", dependencies)
    work = tmp_path / "work"
    work.mkdir()
    (work / "input.txt").write_bytes(b"AUTHORIZED_INPUT")
    for _ in range(2):
        result = await provider.execute_opensandbox_command(["echo", "SDK_PROTOCOL_OUTPUT"], work_dir=work,
            env={"HOME": str(tmp_path / "home")}, timeout=5, sandbox_spec=object())
        assert result.error is None, result
        assert result.stdout == "SDK_PROTOCOL_OUTPUT" and result.exit_code == 0
        assert scope.resource_state == "paused" and state["binding"]["resource_id"] == resource_id
        assert (work / "output.txt").read_bytes() == b"SDK_PROTOCOL_FILE"
    assert calls.count(("POST", "https://sdk.example.invalid/v1/sandboxes")) == 1
    assert sum(url.endswith("/resume") for _, url in calls) == 1
    assert sum(url.endswith("/pause") for _, url in calls) == 2
