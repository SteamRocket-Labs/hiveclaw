from __future__ import annotations

import json
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_approval_create_maps_label_form_to_definition_widgets(monkeypatch):
    from app.services.agent_tool_domains import feishu_approval

    calls: dict[str, object] = {}

    async def fake_get_credentials(agent_id, tool_name):
        assert tool_name == "feishu_approval_create"
        return ("app-id", "app-secret")

    async def fake_get_definition(app_id, app_secret, approval_code):
        assert (app_id, app_secret, approval_code) == ("app-id", "app-secret", "approval-code")
        return {
            "approval_code": "approval-code",
            "form": {
                "form_content": json.dumps(
                    [
                        {"id": "widget_project", "name": "项目名称", "type": "input", "required": True},
                        {"id": "widget_applicant", "name": "NDA用章申请人", "type": "input"},
                        {"id": "widget_entity", "name": "用章主体", "type": "input"},
                    ],
                    ensure_ascii=False,
                )
            },
        }

    async def fake_create(app_id, app_secret, approval_code, user_id, form_data):
        calls["payload"] = {
            "app_id": app_id,
            "app_secret": app_secret,
            "approval_code": approval_code,
            "user_id": user_id,
            "form": json.loads(form_data),
        }
        return {"instance_code": "instance-1"}

    monkeypatch.setattr(feishu_approval, "_get_approval_credentials", fake_get_credentials)
    monkeypatch.setattr(feishu_approval.feishu_service, "get_approval_definition", fake_get_definition)
    monkeypatch.setattr(feishu_approval.feishu_service, "create_approval_instance", fake_create)

    result = await feishu_approval._feishu_approval_create(
        uuid4(),
        {
            "approval_code": "approval-code",
            "user_id": "u_submitter",
            "form": {
                "项目名称": "测试",
                "NDA用章申请人": "缪荣高",
                "用章主体": "上海常春藤投资有限公司",
            },
        },
    )

    assert "instance-1" in result
    assert calls["payload"] == {
        "app_id": "app-id",
        "app_secret": "app-secret",
        "approval_code": "approval-code",
        "user_id": "u_submitter",
        "form": [
            {"id": "widget_project", "type": "input", "value": "测试"},
            {"id": "widget_applicant", "type": "input", "value": "缪荣高"},
            {"id": "widget_entity", "type": "input", "value": "上海常春藤投资有限公司"},
        ],
    }


@pytest.mark.asyncio
async def test_approval_create_repairs_label_ids_and_trailing_punctuation(monkeypatch):
    from app.services.agent_tool_domains import feishu_approval

    captured: dict[str, object] = {}

    async def fake_get_credentials(agent_id, tool_name):
        return ("app-id", "app-secret")

    async def fake_get_definition(app_id, app_secret, approval_code):
        return {
            "form": {
                "form_content": json.dumps(
                    [
                        {"id": "widget_project", "name": "项目名称", "type": "input"},
                        {"id": "widget_entity", "name": "用章主体", "type": "input"},
                    ],
                    ensure_ascii=False,
                )
            }
        }

    async def fake_create(app_id, app_secret, approval_code, user_id, form_data):
        captured["form"] = json.loads(form_data)
        return {"instance_code": "instance-2"}

    monkeypatch.setattr(feishu_approval, "_get_approval_credentials", fake_get_credentials)
    monkeypatch.setattr(feishu_approval.feishu_service, "get_approval_definition", fake_get_definition)
    monkeypatch.setattr(feishu_approval.feishu_service, "create_approval_instance", fake_create)

    await feishu_approval._feishu_approval_create(
        uuid4(),
        {
            "approval_code": "approval-code",
            "user_id": "u_submitter",
            "form": [
                {"id": "项目名称.", "value": "测试"},
                {"id": "widget_entity.", "value": "上海常春藤投资有限公司"},
            ],
        },
    )

    assert captured["form"] == [
        {"id": "widget_project", "type": "input", "value": "测试"},
        {"id": "widget_entity", "type": "input", "value": "上海常春藤投资有限公司"},
    ]
