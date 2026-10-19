"""Diagnostic witnesses use an actual namespace and exclude exact transport credentials."""

import httpx
import pytest


def test_concrete_api_error_preserves_diagnostic_without_transport_credentials():
    from app.services.feishu_platform import FeishuAPIError, resolve_feishu_platform
    from app.services.feishu_read_receipts import capture_feishu_result
    from app.services.feishu_service import FeishuService
    from app.services.session_model_inputs import _hash

    platform = resolve_feishu_platform({"platform_region": "lark_global"})
    token, secret = "synthetic-private-bearer", "synthetic-private-secret"
    request = httpx.Request("GET", platform.open_api_url("/approval/v4/approvals/definition"),
        headers={"Authorization": f"Bearer {token}"})
    response = httpx.Response(403, request=request,
        json={"code": 90043, "msg": f"KEEP_DIAGNOSTIC {token} {secret}"})
    with capture_feishu_result(None, "feishu_approval_definition", {}) as capture:
        with pytest.raises(FeishuAPIError) as error:
            FeishuService._parse_api_response(response, stage="get_approval_definition", platform=platform,
                protected_values=(secret,))
        result = error.value.tool_result("feishu_approval_definition")
    assert "KEEP_DIAGNOSTIC" in str(result) and token not in str(result) and secret not in str(result)
    assert token not in str(error.value) and secret not in str(error.value)
    witness = capture.api_failures[_hash(str(result))]
    assert witness["platform_region"] == "lark_global" and witness["open_api_domain"] == platform.open_api_domain
    assert witness["http_status"] == 403 and witness["provider_code"] == 90043


def test_failure_metadata_alone_cannot_invent_a_transport_witness():
    from app.services.feishu_platform import feishu_tool_error
    from app.services.feishu_read_receipts import capture_feishu_result

    with capture_feishu_result(None, "feishu_doc_create", {}) as capture:
        result = feishu_tool_error("feishu_doc_create", message="Unverified diagnostic", http_status=403, code=90044)
    assert result.metadata["feishu_failure"]["provider_code"] == 90044
    assert capture.api_failures == {} and capture.api_actions == {}


@pytest.mark.parametrize("mutation", [None, "reader", "bytes", "serialized_marker"])
def test_private_diagnostic_never_grants_its_refused_resource(mutation):
    from dataclasses import asdict, replace
    from app.services.connector_acl import (
        extract_connector_source_items, filter_connector_payload_for_prompt, with_connector_diagnostic,
        connector_item_visible, source_items_from_tool_call,
    )
    from app.tools.result_envelope import ToolContentEnvelope

    result = with_connector_diagnostic(ToolContentEnvelope(text="Private API diagnostic"),
        source="feishu://execution/run/tool", connector="feishu", tenant_id="tenant", user_id="requester", agent_id="agent")
    refused = source_items_from_tool_call("feishu_doc_read", {"document_token": "protected_doc"})
    items = [*extract_connector_source_items(result), *refused]
    reader = "different_reader" if mutation == "reader" else "requester"
    if mutation == "bytes":
        result = replace(result, text="Different bytes")
    if mutation == "serialized_marker":
        result = replace(result, metadata={**result.metadata,
            "connector_diagnostic_receipt": asdict(result.metadata["connector_diagnostic_receipt"])})
    checked = filter_connector_payload_for_prompt(result, source_items=items, tenant_id="tenant", current_user_id=reader, agent_id="agent")
    assert "feishu://doc/protected_doc" in checked.forbidden_sources
    assert not any(connector_item_visible(item, tenant_id="tenant", current_user_id="requester", agent_id="agent") for item in refused)
    if mutation is None:
        assert str(checked.payload) == "Private API diagnostic"
        assert checked.allowed_sources == ("feishu://execution/run/tool",)
    else:
        assert "source_permission_denied" in str(checked.payload)
