"""Step 5 plugin install service + runtime wiring.

Source-policy fail-closed, manifest validation, install persistence, and the
get_tenant_pack_policies merge that lets an install actually change the runtime
tool surface (completion criterion). DB-touching paths use a queued spy session
(mirrors test_mcp_server_service.py); pure paths run directly.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.packs.catalog_reader import PackManifest
from app.services import plugin_install_service as svc
from app.services.plugin_install_service import PluginInstallError, load_manifest


class _Result:
    def __init__(self, *, scalars=None, scalar=None):
        self._scalars = scalars
        self._scalar = scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars or []))

    def scalar_one_or_none(self):
        return self._scalar


class _SpyDB:
    def __init__(self, results):
        self._queue = list(results)
        self.added: list = []
        self.deleted: list = []

    async def execute(self, _stmt):
        if not self._queue:
            return _Result()
        return self._queue.pop(0)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)


def _patch_session(monkeypatch, spy):
    @contextlib.asynccontextmanager
    async def fake(_tenant_id, **_kw):
        yield spy

    monkeypatch.setattr(svc, "tenant_scoped_session", fake)


# ── pure logic (no DB) ──────────────────────────────────────────────


def test_load_manifest_finds_shipped_pack():
    assert load_manifest("web_pack").name == "web_pack"
    assert load_manifest("does_not_exist") is None


def test_source_policy_fail_closed_on_remote():
    with pytest.raises(PluginInstallError, match="not installable"):
        svc._assert_installable(PackManifest(name="r", source={"kind": "git"}))


def test_assert_installable_rejects_validation_errors():
    with pytest.raises(PluginInstallError, match="invalid"):
        svc._assert_installable(PackManifest(name="b", validation_errors=("boom",)))


def test_assert_installable_accepts_builtin():
    svc._assert_installable(PackManifest(name="ok", source={"kind": "builtin"}))  # no raise


# ── install (spy session) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_plugin_persists_record(monkeypatch):
    spy = _SpyDB([_Result(scalar=None)])  # no existing install
    _patch_session(monkeypatch, spy)
    result = await svc.install_plugin(uuid4(), "web_pack")
    assert result["plugin_key"] == "web_pack"
    assert result["status"] == "enabled"
    assert result["source_kind"] == "builtin"
    assert any(getattr(o, "plugin_key", None) == "web_pack" for o in spy.added)


@pytest.mark.asyncio
async def test_install_plugin_unknown_key_raises(monkeypatch):
    with pytest.raises(PluginInstallError, match="no manifest"):
        await svc.install_plugin(uuid4(), "ghost_pack")


# ── runtime wiring: install changes pack enablement (completion criterion) ──


def _patch_plugin_session(monkeypatch, plugin_db):
    """Patch the dedicated session get_tenant_pack_policies opens to read installs."""
    import app.database

    @contextlib.asynccontextmanager
    async def fake(_tenant_id, **_kw):
        yield plugin_db

    monkeypatch.setattr(app.database, "tenant_scoped_session", fake)


@pytest.mark.asyncio
async def test_installed_plugin_enables_pack_in_policies(monkeypatch):
    """get_tenant_pack_policies merges installed plugins as enabled — this is how
    installing mcp_admin_pack (default_state=inactive) makes its tools visible.
    The install read runs on a dedicated session (does not touch the caller db)."""
    from app.services import pack_policy_service as pp

    caller_db = _SpyDB([_Result(scalar=None)])  # no explicit SystemSetting policy
    _patch_plugin_session(monkeypatch, _SpyDB([_Result(scalars=["mcp_admin_pack"])]))
    policies = await pp.get_tenant_pack_policies(caller_db, uuid4())
    assert policies.get("mcp_admin_pack") is True


@pytest.mark.asyncio
async def test_explicit_policy_overrides_installed(monkeypatch):
    """An explicit pack-policy False wins over an install (tenant opt-out)."""
    from app.services import pack_policy_service as pp

    caller_db = _SpyDB([_Result(scalar=SimpleNamespace(value={"packs": {"mcp_admin_pack": False}}))])
    _patch_plugin_session(monkeypatch, _SpyDB([_Result(scalars=["mcp_admin_pack"])]))
    policies = await pp.get_tenant_pack_policies(caller_db, uuid4())
    assert policies.get("mcp_admin_pack") is False
