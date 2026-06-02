from pathlib import Path


def test_pack_policy_and_legacy_mcp_routes_removed_from_packs_module():
    project_root = Path(__file__).resolve().parents[3]
    packs_source = (project_root / "backend/app/api/packs.py").read_text()

    # Pack catalog/policy routes are removed from the user surface (skill+MCP cutover, Part 7).
    assert '@router.get("/packs")' not in packs_source
    assert '@router.get("/agents/{agent_id}/packs")' not in packs_source
    assert '@router.get("/enterprise/packs/policies")' not in packs_source
    assert '@router.put("/enterprise/packs/policies/{pack_name}")' not in packs_source

    # Legacy Tool-grouped MCP routes are gone — reconciled to the server-first
    # api/mcp_servers.py canonical path (pack-derived identity retired).
    assert '@router.get("/enterprise/mcp-servers")' not in packs_source
    assert '@router.post("/enterprise/mcp-servers/import")' not in packs_source
    assert '@router.delete("/enterprise/mcp-servers/{server_key}")' not in packs_source

    # Surviving governance/runtime summary routes stay.
    assert '@router.get("/agents/{agent_id}/capability-summary")' in packs_source
    assert '@router.get("/chat/sessions/{session_id}/runtime-summary")' in packs_source


def test_canonical_mcp_server_routes_live_in_mcp_servers_module():
    project_root = Path(__file__).resolve().parents[3]
    mcp_source = (project_root / "backend/app/api/mcp_servers.py").read_text()

    # Canonical server-first MCP routes (no /records, stable {server_id} delete).
    assert '@router.get("/enterprise/mcp-servers")' in mcp_source
    assert '@router.post("/enterprise/mcp-servers/import")' in mcp_source
    assert '@router.delete("/enterprise/mcp-servers/{server_id}")' in mcp_source
    # The pack-derived /records path is gone.
    assert "/enterprise/mcp-servers/records" not in mcp_source
