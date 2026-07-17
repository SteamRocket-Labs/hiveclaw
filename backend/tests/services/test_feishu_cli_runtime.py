from __future__ import annotations

import json

import pytest


def _extract_tool_error_payload(result: str) -> dict:
    marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(marker) + len(marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


@pytest.mark.asyncio
async def test_feishu_doc_read_prefers_cli_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_docs

    async def fake_cli_available() -> bool:
        return True

    async def fake_cli_api_request(method: str, path: str, *, params=None, body=None):
        assert method == "GET"
        assert path == "/open-apis/docx/v1/documents/doc-token/raw_content"
        assert params == {"lang": 0}
        assert body is None
        return {"code": 0, "data": {"content": "CLI content"}}

    monkeypatch.setattr(feishu_docs, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_docs, "_feishu_cli_api_request", fake_cli_api_request)

    result = await feishu_docs._feishu_doc_read("agent-1", {"document_token": "doc-token"})

    assert "CLI content" in result
    assert "<tool_error>" not in result
    source_item = result.metadata["connector_source_items"][0]
    assert source_item["source"] == "feishu://doc/doc-token"
    assert source_item["acl"] == {"deny_by_default": True}
    assert source_item["metadata"] == {
        "connector": "feishu",
        "resource_type": "doc",
        "acl_authority": "connector_unverified",
        "accessing_agent_id": "agent-1",
    }
    assert source_item["content_digest"]


@pytest.mark.asyncio
async def test_feishu_doc_read_binds_successful_content_to_authenticated_runtime_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_docs
    from app.services.connector_acl import extract_connector_source_items, filter_connector_payload_for_prompt

    async def fake_cli_available() -> bool:
        return True

    async def fake_cli_api_request(method: str, path: str, *, params=None, body=None):
        assert method == "GET"
        assert path == "/open-apis/docx/v1/documents/doc-token/raw_content"
        assert params == {"lang": 0}
        assert body is None
        return {"code": 0, "data": {"content": "authenticated Feishu content"}}

    monkeypatch.setattr(feishu_docs, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_docs, "_feishu_cli_api_request", fake_cli_api_request)

    result = await feishu_docs._feishu_doc_read(
        "agent-1",
        {"document_token": "doc-token"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    source_item = result.metadata["connector_source_items"][0]
    assert source_item["acl"] == {"tenant_ids": ["tenant-1"], "user_ids": ["user-1"]}
    assert source_item["metadata"]["acl_authority"] == "connector_verified"

    prompt_filter = filter_connector_payload_for_prompt(
        result,
        source_items=extract_connector_source_items(result, origin="tool:feishu_doc_read"),
        tenant_id="tenant-1",
        current_user_id="user-1",
        agent_id="agent-1",
    )
    assert prompt_filter.forbidden_sources == ()
    assert "authenticated Feishu content" in str(prompt_filter.payload)

    mismatched_principal = filter_connector_payload_for_prompt(
        result,
        source_items=extract_connector_source_items(result, origin="tool:feishu_doc_read"),
        tenant_id="tenant-1",
        current_user_id="different-user",
        agent_id="agent-1",
    )
    assert mismatched_principal.forbidden_sources == ("feishu://doc/doc-token",)
    assert "authenticated Feishu content" not in str(mismatched_principal.payload)
    assert "source_permission_denied" in str(mismatched_principal.payload)


@pytest.mark.asyncio
async def test_feishu_doc_openapi_read_binds_authenticated_runtime_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from app.services.agent_tool_domains import feishu_docs

    async def fake_get_feishu_token(_agent_id):
        return ("app-id", "tenant-token")

    async def fake_wiki_get_node(_document_token: str, _tenant_access_token: str):
        return None

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url: str, *, headers: dict, params: dict):
            assert url.endswith("/docx/v1/documents/doc-token/raw_content")
            assert headers == {"Authorization": "Bearer tenant-token"}
            assert params == {"lang": 0}
            return httpx.Response(200, json={"code": 0, "data": {"content": "OpenAPI document body"}})

    monkeypatch.setattr(feishu_docs, "_get_feishu_token", fake_get_feishu_token)
    monkeypatch.setattr(feishu_docs, "_feishu_wiki_get_node", fake_wiki_get_node)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeAsyncClient())

    result = await feishu_docs._feishu_doc_read_via_openapi(
        "agent-1",
        {"document_token": "doc-token"},
        tenant_id="tenant-1",
        current_user_id="user-1",
    )

    source_item = result.metadata["connector_source_items"][0]
    assert "OpenAPI document body" in result
    assert source_item["acl"] == {"tenant_ids": ["tenant-1"], "user_ids": ["user-1"]}
    assert source_item["metadata"]["acl_authority"] == "connector_verified"


@pytest.mark.asyncio
async def test_feishu_doc_read_falls_back_to_openapi_when_cli_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_docs
    from app.services.agent_tool_domains.feishu_cli import FeishuCliError

    async def fake_cli_available() -> bool:
        return True

    async def fake_cli_api_request(*_args, **_kwargs):
        raise FeishuCliError(
            "CLI auth missing",
            error_class="not_configured",
            retryable=False,
            actionable_hint="Run lark-cli auth login before enabling CLI-backed office tools.",
        )

    async def fake_doc_read_via_openapi(agent_id, arguments):
        assert agent_id == "agent-1"
        assert arguments == {"document_token": "doc-token"}
        return "openapi fallback result"

    monkeypatch.setattr(feishu_docs, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_docs, "_feishu_cli_api_request", fake_cli_api_request)
    monkeypatch.setattr(feishu_docs, "_feishu_doc_read_via_openapi", fake_doc_read_via_openapi)

    result = await feishu_docs._feishu_doc_read("agent-1", {"document_token": "doc-token"})

    assert "openapi fallback result" in result
    payload = _extract_tool_error_payload(result)
    assert payload["provider"] == "lark-cli"
    assert payload["fallback_tool"] == "feishu_doc_read:openapi"


@pytest.mark.asyncio
async def test_feishu_wiki_list_prefers_cli_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_wiki

    responses = [
        {
            "code": 0,
            "data": {
                "node": {
                    "obj_token": "doc-123",
                    "origin_space_id": "space-1",
                    "has_child": True,
                    "title": "Root",
                    "node_token": "wiki-node",
                }
            },
        },
        {
            "code": 0,
            "data": {
                "items": [
                    {
                        "title": "Child A",
                        "node_token": "child-a",
                        "obj_token": "doc-a",
                        "has_child": False,
                    }
                ]
            },
        },
    ]

    async def fake_cli_available() -> bool:
        return True

    async def fake_cli_api_request(method: str, path: str, *, params=None, body=None):
        assert method == "GET"
        assert body is None
        return responses.pop(0)

    monkeypatch.setattr(feishu_wiki, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_wiki, "_feishu_cli_api_request", fake_cli_api_request)

    result = await feishu_wiki._feishu_wiki_list("agent-1", {"node_token": "wiki-node"})

    assert "Child A" in result
    assert "child-a" in result
    assert "<tool_error>" not in result


@pytest.mark.asyncio
async def test_feishu_wiki_list_accepts_space_url_with_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_wiki

    async def fake_cli_available() -> bool:
        return True

    async def fake_cli_api_request(method: str, path: str, *, params=None, body=None):
        assert method == "GET"
        assert path == "/open-apis/wiki/v2/spaces/7641410841677564878/nodes"
        assert params == {"page_size": 50}
        assert body is None
        return {
            "code": 0,
            "data": {
                "items": [
                    {
                        "title": "运营报告",
                        "node_token": "wiki-page-token",
                        "obj_token": "doc-token",
                        "has_child": False,
                    }
                ]
            },
        }

    monkeypatch.setattr(feishu_wiki, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_wiki, "_feishu_cli_api_request", fake_cli_api_request)

    result = await feishu_wiki._feishu_wiki_list(
        "agent-1",
        {"node_token": "https://example.feishu.cn/wiki/space/7641410841677564878"},
    )

    assert "知识库空间 `7641410841677564878`" in result
    assert "运营报告" in result
    assert "wiki-page-token" in result


@pytest.mark.asyncio
async def test_feishu_wiki_list_surfaces_cli_listing_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_wiki

    responses = [
        {
            "code": 0,
            "data": {
                "node": {
                    "obj_token": "doc-123",
                    "origin_space_id": "space-1",
                    "has_child": True,
                    "title": "Root",
                    "node_token": "wiki-node",
                }
            },
        },
        {"code": 131403, "msg": "permission denied"},
    ]

    async def fake_cli_available() -> bool:
        return True

    async def fake_cli_api_request(method: str, path: str, *, params=None, body=None):
        assert method == "GET"
        assert body is None
        return responses.pop(0)

    monkeypatch.setattr(feishu_wiki, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_wiki, "_feishu_cli_api_request", fake_cli_api_request)

    result = await feishu_wiki._feishu_wiki_list("agent-1", {"node_token": "wiki-node"})

    assert "无法列出 Wiki 节点" in result
    assert "permission denied" in result
    assert "只分享了知识库里的单个页面" in result


@pytest.mark.asyncio
async def test_feishu_wiki_list_can_list_sibling_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_wiki

    responses = [
        {
            "code": 0,
            "data": {
                "node": {
                    "obj_token": "doc-123",
                    "obj_type": "docx",
                    "origin_space_id": "space-1",
                    "parent_node_token": "parent-node",
                    "has_child": False,
                    "title": "Current",
                    "node_token": "current-node",
                }
            },
        },
        {
            "code": 0,
            "data": {
                "items": [
                    {
                        "title": "Sibling A",
                        "node_token": "sibling-a",
                        "obj_token": "doc-a",
                        "obj_type": "docx",
                        "has_child": False,
                    },
                    {
                        "title": "Sheet Page",
                        "node_token": "sheet-node",
                        "obj_token": "sheet-token",
                        "obj_type": "sheet",
                        "has_child": False,
                    },
                ]
            },
        },
    ]

    async def fake_cli_available() -> bool:
        return True

    async def fake_cli_api_request(method: str, path: str, *, params=None, body=None):
        assert method == "GET"
        assert body is None
        if path.endswith("/get_node"):
            assert params == {"token": "current-node", "obj_type": "wiki"}
        else:
            assert path == "/open-apis/wiki/v2/spaces/space-1/nodes"
            assert params == {"page_size": 50, "parent_node_token": "parent-node"}
        return responses.pop(0)

    monkeypatch.setattr(feishu_wiki, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_wiki, "_feishu_cli_api_request", fake_cli_api_request)

    result = await feishu_wiki._feishu_wiki_list("agent-1", {"node_token": "current-node", "scope": "siblings"})

    assert "同目录页面" in result
    assert "Sibling A" in result
    assert "Sheet Page" in result
    assert "obj_type: `sheet`" in result
    assert "feishu_sheet_info" in result


@pytest.mark.asyncio
async def test_feishu_wiki_list_continues_when_permission_filter_returns_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_wiki

    responses = [
        {"code": 0, "data": {"items": [], "has_more": True, "page_token": "next-page"}},
        {
            "code": 0,
            "data": {
                "items": [
                    {
                        "title": "Visible After Filter",
                        "node_token": "visible-node",
                        "obj_token": "doc-visible",
                        "obj_type": "docx",
                        "has_child": False,
                    }
                ],
                "has_more": False,
            },
        },
    ]
    seen_params = []

    async def fake_cli_available() -> bool:
        return True

    async def fake_cli_api_request(method: str, path: str, *, params=None, body=None):
        assert method == "GET"
        assert path == "/open-apis/wiki/v2/spaces/space-1/nodes"
        assert body is None
        seen_params.append(dict(params or {}))
        return responses.pop(0)

    monkeypatch.setattr(feishu_wiki, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_wiki, "_feishu_cli_api_request", fake_cli_api_request)

    result = await feishu_wiki._feishu_wiki_list("agent-1", {"space_id": "space-1"})

    assert seen_params == [{"page_size": 50}, {"page_size": 50, "page_token": "next-page"}]
    assert "Visible After Filter" in result
    assert "没有可见页面" not in result


@pytest.mark.asyncio
async def test_feishu_wiki_recursive_list_traverses_every_depth_and_stops_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_domains import feishu_wiki

    children_by_parent = {
        None: [{"title": "Level 0", "node_token": "level-0", "obj_token": "doc-0", "has_child": True}],
        "level-0": [{"title": "Level 1", "node_token": "level-1", "obj_token": "doc-1", "has_child": True}],
        "level-1": [{"title": "Level 2", "node_token": "level-2", "obj_token": "doc-2", "has_child": True}],
        "level-2": [{"title": "Level 3", "node_token": "level-3", "obj_token": "doc-3", "has_child": True}],
        "level-3": [{"title": "Cycle Back", "node_token": "level-0", "obj_token": "doc-0", "has_child": True}],
    }
    expanded_parents: list[str | None] = []

    async def fake_cli_available() -> bool:
        return True

    async def fake_cli_api_request(method: str, path: str, *, params=None, body=None):
        assert method == "GET"
        assert path == "/open-apis/wiki/v2/spaces/space-1/nodes"
        assert body is None
        parent = (params or {}).get("parent_node_token")
        expanded_parents.append(parent)
        return {"code": 0, "data": {"items": children_by_parent.get(parent, []), "has_more": False}}

    monkeypatch.setattr(feishu_wiki, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_wiki, "_feishu_cli_api_request", fake_cli_api_request)

    result = await feishu_wiki._feishu_wiki_list(
        "agent-1",
        {"space_id": "space-1", "recursive": True},
    )

    assert "Level 3" in result
    assert expanded_parents == [None, "level-0", "level-1", "level-2", "level-3"]


@pytest.mark.asyncio
async def test_feishu_doc_read_routes_wiki_sheet_to_sheet_info(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.agent_tool_domains import feishu_docs

    responses = [
        {
            "code": 0,
            "data": {
                "node": {
                    "obj_token": "sheet-token",
                    "obj_type": "sheet",
                    "origin_space_id": "space-1",
                    "has_child": False,
                    "title": "Sheet Page",
                    "node_token": "wiki-sheet-node",
                }
            },
        },
    ]

    async def fake_cli_available() -> bool:
        return True

    async def fake_wiki_get_node(token_str: str):
        assert token_str == "wiki-sheet-node"
        data = responses.pop(0)
        node = data["data"]["node"]
        return {
            "obj_token": node["obj_token"],
            "obj_type": node["obj_type"],
            "space_id": node["origin_space_id"],
            "has_child": node["has_child"],
            "title": node["title"],
            "node_token": node["node_token"],
        }

    async def fake_sheet_info(agent_id, arguments):
        assert agent_id == "agent-1"
        assert arguments == {"spreadsheet_token": "sheet-token"}
        return "sheet info result"

    monkeypatch.setattr(feishu_docs, "_feishu_cli_available", fake_cli_available)
    monkeypatch.setattr(feishu_docs, "_feishu_wiki_get_node_via_cli", fake_wiki_get_node)
    monkeypatch.setattr("app.services.agent_tool_domains.feishu_sheets._feishu_sheet_info", fake_sheet_info)

    result = await feishu_docs._feishu_doc_read("agent-1", {"document_token": "wiki-sheet-node"})

    assert "Wiki 页面挂载的是电子表格" in result
    assert "sheet info result" in result
