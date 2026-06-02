from pathlib import Path


def test_pack_policy_routes_removed_but_mcp_registry_routes_retained():
    project_root = Path(__file__).resolve().parents[3]
    packs_source = (project_root / "backend/app/api/packs.py").read_text()

    # Pack catalog/policy routes are removed from the user surface (skill+MCP cutover, Part 7).
    assert '@router.get("/packs")' not in packs_source
    assert '@router.get("/agents/{agent_id}/packs")' not in packs_source
    assert '@router.get("/enterprise/packs/policies")' not in packs_source
    assert '@router.put("/enterprise/packs/policies/{pack_name}")' not in packs_source

    # Legacy MCP registry routes remain (frontend uses the new /records route; leaving these is acceptable this pass).
    assert '@router.get("/enterprise/mcp-servers")' in packs_source
    assert '@router.post("/enterprise/mcp-servers/import")' in packs_source
    assert '@router.delete("/enterprise/mcp-servers/{server_key}")' in packs_source

    # Surviving governance/runtime summary routes stay.
    assert '@router.get("/agents/{agent_id}/capability-summary")' in packs_source
    assert '@router.get("/chat/sessions/{session_id}/runtime-summary")' in packs_source
