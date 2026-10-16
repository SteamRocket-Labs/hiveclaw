"""Company policy is a ceiling; an employee policy may only narrow it."""

from __future__ import annotations

import uuid

import pytest

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.tenant import Tenant
from app.models.user import User
from app.services.capability_gate import CAPABILITY_MAP, check_capability


@pytest.mark.parametrize("tool_name", ["preview_agent_blueprint", "send_feishu_message"])
@pytest.mark.parametrize(
    "company_allow,company_approval,agent_allow,agent_approval,denied,approval",
    [
        (False, False, True, False, True, False),
        (True, True, True, False, False, True),
        (True, False, False, False, True, False),
        (True, False, True, True, False, True),
        (True, False, True, False, False, False),
    ],
)
async def test_employee_cannot_widen_company_capability_policy(
    owner_sessionmaker, app_user_sessionmaker, tool_name,
    company_allow, company_approval, agent_allow, agent_approval, denied, approval,
):
    tenant_id, agent_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    capability = CAPABILITY_MAP[tool_name]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Policy ceiling", slug=f"policy-{tenant_id.hex}"))
        db.add(User(id=user_id, tenant_id=tenant_id, username=f"policy-{user_id.hex}",
                    email=f"{user_id.hex}@policy.test", display_name="Policy owner", password_hash="synthetic"))
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, creator_id=user_id, owner_user_id=user_id,
                     name="Native employee", role_description="Scoped work", agent_type="native",
                     agent_class="internal_tenant", status="idle"))
        await db.flush()
        db.add_all([
            CapabilityPolicy(tenant_id=tenant_id, agent_id=None, capability=capability,
                             allowed=company_allow, requires_approval=company_approval),
            CapabilityPolicy(tenant_id=tenant_id, agent_id=agent_id, capability=capability,
                             allowed=agent_allow, requires_approval=agent_approval),
        ])
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        decision = await check_capability(db, tenant_id, agent_id, tool_name)
    assert decision.denied is denied
    assert decision.escalate_to_l3 is approval
    assert decision.allowed is (not denied and not approval)
    assert decision.policy_found is True
    assert decision.capability == capability
