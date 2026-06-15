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


def test_dependency_closure_orders_dependencies_and_pins_provenance(monkeypatch, tmp_path):
    """Install truth requires a real dependency closure, not a direct manifest echo."""
    dep_path = tmp_path / "dep_pack.yaml"
    dep_path.write_text("name: dep_pack\nversion: 1.0.0\n", encoding="utf-8")
    parent_path = tmp_path / "parent_pack.yaml"
    parent_path.write_text("name: parent_pack\nversion: 2.0.0\n", encoding="utf-8")
    manifests = {
        "dep_pack": PackManifest(name="dep_pack", version="1.0.0", manifest_path=dep_path),
        "parent_pack": PackManifest(
            name="parent_pack",
            version="2.0.0",
            dependencies=({"name": "dep_pack", "version": "1.0.0"},),
            manifest_path=parent_path,
        ),
    }
    monkeypatch.setattr(svc, "load_manifest", lambda key: manifests.get(key))

    closure = svc.resolve_plugin_dependency_closure("parent_pack")

    assert [item.manifest.name for item in closure] == ["dep_pack", "parent_pack"]
    parent_lock = closure[-1].lockfile
    assert parent_lock["plugin_key"] == "parent_pack"
    assert parent_lock["dependencies"][0]["plugin_key"] == "dep_pack"
    assert parent_lock["dependencies"][0]["content_sha256"]


def test_dependency_closure_rejects_cycles(monkeypatch):
    manifests = {
        "a": PackManifest(name="a", version="1.0.0", dependencies=({"name": "b", "version": "1.0.0"},)),
        "b": PackManifest(name="b", version="1.0.0", dependencies=({"name": "a", "version": "1.0.0"},)),
    }
    monkeypatch.setattr(svc, "load_manifest", lambda key: manifests.get(key))

    with pytest.raises(PluginInstallError, match="cycle"):
        svc.resolve_plugin_dependency_closure("a")


def test_pre_tool_enforce_hook_requires_explicit_admin_approval():
    manifest = PackManifest(
        name="guard_pack",
        hooks=({"event": "pre_tool_use", "handler": "plugin.block", "mode": "enforce"},),
    )

    with pytest.raises(PluginInstallError, match="approved_enforce_hooks"):
        svc.validate_hook_install_approval(manifest, config={})

    svc.validate_hook_install_approval(
        manifest,
        config={"approved_enforce_hooks": ["guard_pack:pre_tool_use:plugin.block"]},
    )


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


@pytest.mark.asyncio
async def test_dependency_install_inherits_agent_assignment_scope(monkeypatch):
    agent_id = uuid4()
    manifests = {
        "dep_pack": PackManifest(name="dep_pack", version="1.0.0"),
        "parent_pack": PackManifest(
            name="parent_pack",
            version="2.0.0",
            dependencies=({"name": "dep_pack", "version": "1.0.0"},),
        ),
    }
    monkeypatch.setattr(svc, "load_manifest", lambda key: manifests.get(key))
    spy = _SpyDB([_Result(scalar=None), _Result(scalar=None)])
    _patch_session(monkeypatch, spy)
    calls: list[tuple[str, list[str] | None]] = []

    async def capture_assignment(_db, _tenant_id, plugin, *, agent_ids):
        calls.append((plugin.plugin_key, [str(item) for item in agent_ids] if agent_ids is not None else None))

    monkeypatch.setattr(svc, "_sync_agent_assignments", capture_assignment)

    await svc.install_plugin(uuid4(), "parent_pack", agent_ids=[agent_id])

    assert calls == [
        ("dep_pack", [str(agent_id)]),
        ("parent_pack", [str(agent_id)]),
    ]


@pytest.mark.asyncio
async def test_dependency_enforce_hook_uses_install_approval_closure(monkeypatch):
    manifests = {
        "dep_pack": PackManifest(
            name="dep_pack",
            version="1.0.0",
            hooks=({"event": "pre_tool_use", "handler": "plugin.block", "mode": "enforce"},),
        ),
        "parent_pack": PackManifest(
            name="parent_pack",
            version="2.0.0",
            dependencies=({"name": "dep_pack", "version": "1.0.0"},),
        ),
    }
    monkeypatch.setattr(svc, "load_manifest", lambda key: manifests.get(key))
    spy = _SpyDB([_Result(scalar=None), _Result(scalar=None)])
    _patch_session(monkeypatch, spy)

    await svc.install_plugin(
        uuid4(),
        "parent_pack",
        config={"approved_enforce_hooks": ["dep_pack:pre_tool_use:plugin.block"]},
    )


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


@pytest.mark.asyncio
async def test_agent_plugin_assignment_controls_pack_visibility(monkeypatch):
    """Runtime tool visibility must be agent-scoped; tenant install alone is not enough."""
    from app.services import pack_policy_service as pp

    agent_id = uuid4()
    tenant_id = uuid4()
    caller_db = _SpyDB(
        [
            _Result(scalar=SimpleNamespace(value={"packs": {"web_pack": True}})),
        ]
    )
    _patch_plugin_session(monkeypatch, _SpyDB([_Result(scalars=["web_pack"])]))

    policies = await pp.get_agent_pack_policies(caller_db, tenant_id, agent_id)

    assert policies["web_pack"] is True


@pytest.mark.asyncio
async def test_unassigned_installed_plugin_is_not_visible_to_agent(monkeypatch):
    from app.services import pack_policy_service as pp

    agent_id = uuid4()
    tenant_id = uuid4()
    caller_db = _SpyDB(
        [
            _Result(scalar=None),
        ]
    )
    _patch_plugin_session(monkeypatch, _SpyDB([_Result(scalars=[])]))

    policies = await pp.get_agent_pack_policies(caller_db, tenant_id, agent_id)

    assert "mcp_admin_pack" not in policies
