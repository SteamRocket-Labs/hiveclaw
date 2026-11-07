from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.audit_schemas import AuditLogSummaryOut


@pytest.mark.parametrize("action,fields", [
    ("user:offboarded", {"target_user_id": str(uuid4()), "successor_user_id": str(uuid4())}),
    ("user:role_changed", {"target_user_id": str(uuid4()), "from_role": "member", "to_role": "org_admin"}),
])
def test_admin_audit_keeps_only_the_people_and_roles_needed_for_management(action, fields):
    log = AuditLogSummaryOut(id=uuid4(), action=action, created_at=datetime.now(timezone.utc),
        details={**fields, "reason": "private business reason", "schema": "private", "api_key": "secret"})
    assert log.details == fields


def test_other_audit_events_do_not_gain_access_to_management_person_fields():
    log = AuditLogSummaryOut(id=uuid4(), action="another_action", created_at=datetime.now(timezone.utc),
        details={"target_user_id": str(uuid4()), "from_role": "member", "status": "completed"})
    assert log.details == {"status": "completed"}
