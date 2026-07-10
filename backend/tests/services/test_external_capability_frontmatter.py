from pathlib import Path


def test_shared_frontmatter_parser_preserves_yaml_types_and_trims_body() -> None:
    from app.services.external_capabilities.frontmatter import split_yaml_frontmatter

    metadata, body = split_yaml_frontmatter("---\nname: Demo\nenabled: true\ntags:\n  - one\n---\n\n# Instructions\n")

    assert metadata == {"name": "Demo", "enabled": True, "tags": ["one"]}
    assert body == "# Instructions"


def test_plugin_adapters_do_not_keep_private_frontmatter_copies() -> None:
    root = Path(__file__).resolve().parents[2] / "app" / "services" / "external_capabilities"

    for filename in ("cc_plugin_adapter.py", "codex_plugin_adapter.py"):
        source = (root / filename).read_text(encoding="utf-8")
        assert "def _split_frontmatter" not in source
        assert "split_yaml_frontmatter" in source
