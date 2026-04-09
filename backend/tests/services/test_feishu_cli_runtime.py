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
