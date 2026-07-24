from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.services.company_knowledge_control_plane import (
    CompanyKnowledgePermissionGrantInput,
    CompanyKnowledgePermissionService,
    business_capabilities_for_actions,
    normalize_company_knowledge_permission_grant,
)


def _request(**overrides) -> CompanyKnowledgePermissionGrantInput:
    values = {
        "principal_type": "role",
        "principal_id": None,
        "principal_key": "role:member",
        "resource_type": "company_knowledge_namespace",
        "resource_id": None,
        "resource_key": "namespace:company/policies",
        "actions": ("discover", "search", "read", "cite"),
        "effect": "allow",
        "sensitivity_ceiling": "PL2_pii",
        "purposes": ("interactive_session",),
        "expires_at": None,
        "idempotency_key": "permission:members:company-policies:v1",
    }
    values.update(overrides)
    return CompanyKnowledgePermissionGrantInput(**values)


def test_permission_grant_normalization_is_exact_and_maps_business_capabilities() -> None:
    normalized = normalize_company_knowledge_permission_grant(_request())

    assert normalized.principal_key == "role:member"
    assert normalized.resource_key == "namespace:company/policies"
    assert normalized.actions == ("cite", "discover", "read", "search")
    assert normalized.purposes == ("interactive_session",)
    assert business_capabilities_for_actions(normalized.actions) == ("find_and_read",)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"principal_type": "hook"}, "unsupported_company_knowledge_principal_type"),
        ({"principal_id": uuid.uuid4()}, "exactly_one_principal_reference_required"),
        ({"resource_key": None}, "exactly_one_resource_reference_required"),
        ({"actions": ("read", "disable_hook")}, "unsupported_company_knowledge_permission_action"),
        ({"purposes": ("natural_language_approval",)}, "unsupported_company_knowledge_permission_purpose"),
        (
            {"effect": "allow", "expires_at": datetime(2020, 1, 1, tzinfo=timezone.utc)},
            "company_knowledge_permission_expiry_must_be_future",
        ),
    ],
)
def test_permission_grant_normalization_rejects_untrusted_machine_contracts(overrides, message) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_company_knowledge_permission_grant(_request(**overrides))


def test_permission_business_projection_does_not_echo_raw_machine_actions() -> None:
    assert business_capabilities_for_actions(
        (
            "discover",
            "search",
            "read",
            "cite",
            "propose",
            "review",
            "approve",
            "publish",
            "retire",
            "restore",
            "query",
            "simulate",
        )
    ) == (
        "find_and_read",
        "propose_updates",
        "review_and_publish",
        "manage_lifecycle",
        "use_company_model",
    )


class _PrincipalLookupSession:
    def __init__(self, row) -> None:
        self.row = row

    async def get(self, _model, _principal_id):
        return self.row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal_type", "row"),
    [
        ("user", SimpleNamespace(tenant_id=uuid.uuid4(), is_active=False)),
        (
            "agent",
            SimpleNamespace(
                tenant_id=uuid.uuid4(),
                deleted_at=datetime.now(timezone.utc),
                deactivated_at=None,
            ),
        ),
        (
            "team",
            SimpleNamespace(
                tenant_id=uuid.uuid4(),
                status="completed",
                closed_at=datetime.now(timezone.utc),
            ),
        ),
        ("integration", SimpleNamespace(tenant_id=uuid.uuid4(), status="revoked")),
    ],
)
async def test_permission_principal_validation_rejects_inactive_targets(
    principal_type,
    row,
) -> None:
    request = _request(
        principal_type=principal_type,
        principal_id=uuid.uuid4(),
        principal_key=None,
    )

    with pytest.raises(ValueError, match="company_knowledge_permission_principal_inactive"):
        await CompanyKnowledgePermissionService._validate_principal(
            _PrincipalLookupSession(row),
            tenant_id=row.tenant_id,
            request=request,
        )
