from pathlib import Path


def test_pack_api_exposes_pack_policy_and_mcp_registry_routes():
    project_root = Path(__file__).resolve().parents[3]
    packs_source = (project_root / "backend/app/api/packs.py").read_text()

    assert '@router.get("/enterprise/packs/policies")' in packs_source
    assert '@router.put("/enterprise/packs/policies/{pack_name}")' in packs_source
    assert '@router.get("/enterprise/mcp-servers")' in packs_source
    assert '@router.post("/enterprise/mcp-servers/import")' in packs_source
    assert '@router.delete("/enterprise/mcp-servers/{server_key}")' in packs_source
