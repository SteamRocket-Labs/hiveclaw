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


@pytest.mark.parametrize("already_inactive", [True, False])
def test_offboarding_audit_preserves_the_actual_inactive_outcome(already_inactive):
    log = AuditLogSummaryOut(id=uuid4(), action="user:offboarded", created_at=datetime.now(timezone.utc),
        details={"already_inactive": already_inactive, "probe_id": "private-probe", "schema": "private"})
    assert log.details == {"already_inactive": already_inactive, "probe_id": "private-probe"}


@pytest.mark.parametrize("already_inactive", [None, 0, 1, "true", "false", [], {}, {"private": "payload"}])
def test_offboarding_audit_drops_non_boolean_inactive_outcomes(already_inactive):
    log = AuditLogSummaryOut(id=uuid4(), action="user:offboarded", created_at=datetime.now(timezone.utc),
        details={"already_inactive": already_inactive, "status": "completed"})
    assert log.details == {"status": "completed"}


@pytest.mark.parametrize("action,key", [
    ("user:offboarded", "target_user_id"), ("user:offboarded", "successor_user_id"),
    ("user:role_changed", "target_user_id"), ("user:role_changed", "from_role"),
    ("user:role_changed", "to_role"),
])
@pytest.mark.parametrize("value", [None, 1, False, [], {"private": "payload"}])
def test_g3f_review_6_drops_non_string_people_and_roles(action, key, value):
    log = AuditLogSummaryOut(id=uuid4(), action=action, created_at=datetime.now(timezone.utc),
        details={key: value, "status": "completed"})
    assert log.details == {"status": "completed"}
