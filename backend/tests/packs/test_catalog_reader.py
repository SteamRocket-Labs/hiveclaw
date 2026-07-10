from __future__ import annotations

from pathlib import Path

from app.packs.catalog_reader import PackCatalogReader, find_pack_dirs
from app.tools.collector import collect_tools


REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_ROOTS = (REPO_ROOT / "packs", REPO_ROOT / "backend" / "packs")
REQUIRED_CLOUD_PACKS = {"office_pack"}


def test_pack_catalog_reader_loads_manifest_without_runtime_side_effects(tmp_path):
    pack_dir = tmp_path / "sample_pack"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        """
name: sample_pack
version: "1.0.0"
description: Sample data and analysis pack
license: Proprietary
author: Hive Team
tools:
  - name: sample_resolve_entity
    locale: cloud
    governance:
      security_zone: public
  - name: sample_compile_research_packet
skills:
  - skills/sample-deep-dive
data_sources:
  public_default:
    - public_registry
  paid_optional:
    - paid_registry
mcp_servers:
  - name: sample_optional
    enabled_by_default: false
    credential_scope: tenant
credential_requirements:
  - key: registry_identity
    scope: tenant
    storage: encrypted_tool_config
activation:
  required_capabilities: [sample_data_access]
sandbox_requirements:
  pip_packages:
    - samplelib>=1.0
""".strip(),
        encoding="utf-8",
    )

    reader = PackCatalogReader(tmp_path)
    reader.discover()

    manifests = reader.list_packs()
    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest.name == "sample_pack"
    assert manifest.version == "1.0.0"
    assert manifest.description == "Sample data and analysis pack"
    assert manifest.tool_names == ("sample_resolve_entity", "sample_compile_research_packet")
    assert manifest.skills == ("skills/sample-deep-dive",)
    assert manifest.data_sources["public_default"] == ["public_registry"]
    assert manifest.credential_requirements[0]["scope"] == "tenant"
    assert manifest.to_dict()["runtime_source_of_truth"] == "tool_decorator"
    assert reader.get_pack("sample_pack") is manifest


def test_pack_catalog_reader_is_tolerant_of_invalid_manifest(tmp_path):
    bad_dir = tmp_path / "bad_pack"
    bad_dir.mkdir()
    (bad_dir / "pack.yaml").write_text("name: [unterminated\n", encoding="utf-8")

    good_dir = tmp_path / "office_pack"
    good_dir.mkdir()
    (good_dir / "pack.yaml").write_text(
        """
name: office_pack
version: "0.1.0"
description: Office workflows
tools: []
skills: []
""".strip(),
        encoding="utf-8",
    )

    reader = PackCatalogReader(tmp_path)
    reader.discover()

    assert [manifest.name for manifest in reader.list_packs()] == ["office_pack"]
    assert reader.get_pack("bad_pack") is None


def test_repo_pack_manifests_cover_cloud_capability_packs():
    reader = PackCatalogReader(REPO_ROOT / "packs")
    reader.discover()

    manifests = {manifest.name: manifest for manifest in reader.list_packs()}

    assert {"office_pack"}.issubset(manifests)
    assert manifests["office_pack"].skills == ("skills/office-productivity",)


def test_repo_and_deploy_pack_manifests_are_full_skill_packages():
    failures: list[str] = []

    for pack_root in PACK_ROOTS:
        reader = PackCatalogReader(pack_root)
        reader.discover()
        manifests = {manifest.name: manifest for manifest in reader.list_packs()}

        missing_packs = REQUIRED_CLOUD_PACKS - set(manifests)
        if missing_packs:
            failures.append(f"{pack_root.relative_to(REPO_ROOT)} missing packs {sorted(missing_packs)}")
            continue

        # Only cloud skill packages must ship a full skill bundle; tool-only packs
        # (web/plaza/email/mcp_admin) legitimately declare a manifest with no skills.
        for pack_name in sorted(REQUIRED_CLOUD_PACKS):
            manifest = manifests[pack_name]
            if not manifest.skills:
                failures.append(f"{manifest.manifest_path.relative_to(REPO_ROOT)} does not declare skills")
                continue
            pack_dir = manifest.manifest_path.parent
            for skill_ref in manifest.skills:
                skill_dir = pack_dir / skill_ref
                if not (skill_dir / "SKILL.md").is_file():
                    failures.append(
                        f"{manifest.name} skill {skill_ref} missing SKILL.md in {pack_dir.relative_to(REPO_ROOT)}"
                    )
                if not (skill_dir / "references").is_dir():
                    failures.append(
                        f"{manifest.name} skill {skill_ref} missing references/ in {pack_dir.relative_to(REPO_ROOT)}"
                    )
                if not (skill_dir / "templates").is_dir():
                    failures.append(
                        f"{manifest.name} skill {skill_ref} missing templates/ in {pack_dir.relative_to(REPO_ROOT)}"
                    )
                # Decorative evals/eval.yaml retired (eval-system-spec §3.1).
                if (skill_dir / "evals" / "eval.yaml").exists():
                    failures.append(
                        f"{manifest.name} skill {skill_ref} carries retired decorative eval.yaml"
                        f" in {pack_dir.relative_to(REPO_ROOT)}"
                    )

    assert not failures, "\n".join(failures)


def test_pack_manifest_tools_are_registered_backend_tools():
    registered_tools = {tool["function"]["name"] for tool in collect_tools().openai_tools}
    failures: list[str] = []

    for pack_root in PACK_ROOTS:
        reader = PackCatalogReader(pack_root)
        reader.discover()
        for manifest in reader.list_packs():
            for tool_name in manifest.tool_names:
                if tool_name not in registered_tools:
                    failures.append(
                        f"{manifest.manifest_path.relative_to(REPO_ROOT)} references unknown tool {tool_name}"
                    )

    assert not failures, "\n".join(failures)


def test_find_pack_dirs_supports_repo_and_packaged_backend_layouts(tmp_path):
    repo_packs = tmp_path / "packs"
    backend_packs = tmp_path / "backend" / "packs"
    service_file = tmp_path / "backend" / "app" / "services" / "pack_service.py"
    (repo_packs / "office_pack").mkdir(parents=True)
    (backend_packs / "sample_pack").mkdir(parents=True)
    (repo_packs / "office_pack" / "pack.yaml").write_text("name: office_pack\n", encoding="utf-8")
    (backend_packs / "sample_pack" / "pack.yaml").write_text("name: sample_pack\n", encoding="utf-8")
    service_file.parent.mkdir(parents=True, exist_ok=True)
    service_file.write_text("# test anchor\n", encoding="utf-8")

    assert find_pack_dirs(service_file) == (backend_packs, repo_packs)
