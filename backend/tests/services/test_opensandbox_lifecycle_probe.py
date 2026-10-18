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
