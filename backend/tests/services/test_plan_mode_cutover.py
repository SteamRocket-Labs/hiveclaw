"""Tests for the Plan Mode cutover helper (§9.0 / §9.3).

``mark_existing_triggers_plan_exempt`` is the compatibility-mode cutover step:
before the daemon backstop goes live it stamps every currently-enabled trigger
with ``config.metadata.plan_exempt_reason="preexisting_before_cutover"`` so the
fail-closed preflight does not quarantine triggers that pre-date Plan Mode.

It is idempotent: triggers that already trace to a confirmed plan
(``config.plan_id``) or already carry an exemption are left untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import plan_mode_core


class _ScalarsCollection:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _ScalarsCollection(self._values)


class _CutoverSession:
    def __init__(self, triggers):
        self._triggers = list(triggers)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        return _ScalarsResult(self._triggers)

    async def commit(self):
        self.commits += 1


def _trigger(*, config=None, is_enabled=True, name="t"):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        is_enabled=is_enabled,
        config=dict(config or {}),
    )


@pytest.fixture()
def patch_session(monkeypatch):
    def _install(triggers):
        from app.services import plan_mode_cutover as mod

        session = _CutoverSession(triggers)
        monkeypatch.setattr(mod, "async_session", lambda: session)
        return session

    return _install


@pytest.mark.asyncio
async def test_stamps_exemption_on_plain_enabled_trigger(patch_session):
    from app.services.plan_mode_cutover import mark_existing_triggers_plan_exempt

    trigger = _trigger(config={"trigger_class": "scheduled_job"})
    session = patch_session([trigger])

    report = await mark_existing_triggers_plan_exempt()

    assert report["stamped"] == 1
    assert trigger.config["metadata"]["plan_exempt_reason"] == plan_mode_core.PLAN_EXEMPT_PREEXISTING
    # The exemption is enough for the gate to allow this trigger now.
    assert (
        plan_mode_core.extract_plan_exempt_reason({"config": trigger.config})
        == plan_mode_core.PLAN_EXEMPT_PREEXISTING
    )
    assert session.commits == 1


@pytest.mark.asyncio
async def test_skips_trigger_with_confirmed_plan_id(patch_session):
    from app.services.plan_mode_cutover import mark_existing_triggers_plan_exempt

    trigger = _trigger(config={"plan_id": str(uuid4())})
    session = patch_session([trigger])

    report = await mark_existing_triggers_plan_exempt()

    assert report["stamped"] == 0
    assert report["skipped"] == 1
    assert "metadata" not in trigger.config  # untouched
    assert session.commits == 0


@pytest.mark.asyncio
async def test_idempotent_when_already_exempt(patch_session):
    from app.services.plan_mode_cutover import mark_existing_triggers_plan_exempt

    trigger = _trigger(
        config={"metadata": {"plan_exempt_reason": plan_mode_core.PLAN_EXEMPT_PREEXISTING}}
    )
    session = patch_session([trigger])

    report = await mark_existing_triggers_plan_exempt()

    assert report["stamped"] == 0
    assert report["skipped"] == 1
    assert session.commits == 0


@pytest.mark.asyncio
async def test_preserves_other_metadata_keys(patch_session):
    from app.services.plan_mode_cutover import mark_existing_triggers_plan_exempt

    trigger = _trigger(config={"metadata": {"foo": "bar"}, "trigger_class": "scheduled_job"})
    patch_session([trigger])

    await mark_existing_triggers_plan_exempt()

    assert trigger.config["metadata"]["foo"] == "bar"
    assert trigger.config["metadata"]["plan_exempt_reason"] == plan_mode_core.PLAN_EXEMPT_PREEXISTING


@pytest.mark.asyncio
async def test_custom_reason_is_honoured(patch_session):
    from app.services.plan_mode_cutover import mark_existing_triggers_plan_exempt

    trigger = _trigger(config={})
    patch_session([trigger])

    await mark_existing_triggers_plan_exempt(reason="grandfathered_2026")

    assert trigger.config["metadata"]["plan_exempt_reason"] == "grandfathered_2026"
