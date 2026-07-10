"""Step 4 plugin-manifest invariants.

pack.yaml is the install/composition/governance source of truth (not the tool
schema). This pins the role classification (owns/requires_core/optional_provider),
the fail-closed validator (governed inclusion §6.7), and the audit assertions
(CORE∩owns disjoint, manifest validity + manifest/decorator agreement).
"""

from __future__ import annotations

import pytest

_SHIPPED = {
    "web_pack",
    "feishu_pack",
    "plaza_pack",
    "email_pack",
    "mcp_admin_pack",
    "office_pack",
    "command_pack",
    "personal_knowledge_pack",
}


def test_manifest_role_classification():
    from app.packs.catalog_reader import PackManifest

    m = PackManifest(
        name="t",
        tools=(
            {"name": "a", "role": "owns"},
            {"name": "b", "role": "requires_core"},
            {"name": "c", "role": "optional_provider"},
            {"name": "d"},  # default role = owns
        ),
    )
    assert set(m.owns_names) == {"a", "d"}
    assert m.requires_core_names == ("b",)
    assert m.optional_provider_names == ("c",)
    assert set(m.tool_names) == {"a", "b", "c", "d"}


def test_validator_rejects_unknown_role():
    from app.packs.catalog_reader import validate_manifest

    errors = validate_manifest({"name": "t", "tools": [{"name": "x", "role": "bogus"}]})
    assert any("unknown role" in e for e in errors)


def test_validator_rejects_raw_hook_handler():
    from app.packs.catalog_reader import validate_manifest

    errors = validate_manifest({"name": "t", "hooks": [{"handler": "app.evil:run"}]})
    assert any("raw code" in e or "allowlist" in e for e in errors)


def test_validator_rejects_unpinned_dependency():
    from app.packs.catalog_reader import validate_manifest

    errors = validate_manifest({"name": "t", "dependencies": [{"name": "foo"}]})
    assert any("not pinned" in e for e in errors)


def test_validator_rejects_remote_source_in_v1():
    from app.packs.catalog_reader import validate_manifest

    errors = validate_manifest({"name": "t", "source": {"kind": "git", "url": "x"}})
    assert any("fail-closed" in e or "not installable" in e for e in errors)


def test_validator_accepts_builtin_source():
    from app.packs.catalog_reader import validate_manifest

    errors = validate_manifest({"name": "t", "source": {"kind": "builtin"}, "tools": [{"name": "x", "role": "owns"}]})
    assert errors == ()


def test_all_shipped_manifests_valid():
    from app.tools.audit import _iter_manifests

    names = set()
    for m in _iter_manifests():
        assert m.validation_errors == (), f"{m.name}: {m.validation_errors}"
        names.add(m.name)
    assert names == _SHIPPED


def test_assert_core_pack_disjoint_covers_manifest_owns(monkeypatch):
    """A CORE tool declared role=owns in a manifest must fail the invariant."""
    import app.tools.audit as audit
    from app.packs.catalog_reader import PackManifest

    fake = PackManifest(name="bad", tools=({"name": "read_file", "role": "owns"},))
    monkeypatch.setattr(audit, "_iter_manifests", lambda: iter([fake]))
    with pytest.raises(RuntimeError, match="manifest.owns"):
        audit.assert_core_pack_disjoint()


def test_requires_core_may_reference_core(monkeypatch):
    """A CORE tool as role=requires_core is allowed (pack depends on CORE)."""
    import app.tools.audit as audit
    from app.packs.catalog_reader import PackManifest

    fake = PackManifest(name="ok", tools=({"name": "read_file", "role": "requires_core"},))
    monkeypatch.setattr(audit, "_iter_manifests", lambda: iter([fake]))
    audit.assert_core_pack_disjoint()  # must not raise


def test_assert_manifests_valid_passes_on_shipped():
    from app.tools.audit import assert_manifests_valid

    assert_manifests_valid()  # must not raise


def test_assert_manifests_valid_rejects_unregistered_tool(monkeypatch):
    import app.tools.audit as audit
    from app.packs.catalog_reader import PackManifest

    fake = PackManifest(name="bad", tools=({"name": "nonexistent_tool_xyz", "role": "owns"},))
    monkeypatch.setattr(audit, "_iter_manifests", lambda: iter([fake]))
    with pytest.raises(RuntimeError, match="unregistered tools"):
        audit.assert_manifests_valid()
