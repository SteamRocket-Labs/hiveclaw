"""Growth comparisons read real native work, not synthetic capability scores."""

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4g_learned_skill_sources_postgres import controlled_provider_boundary  # noqa: F401
from tests.integration.test_m4h_native_feedback_postgres import complete_work

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("different_model", [False, True])
async def test_growth_observability_reads_recorded_work_pair_and_preserves_unknown_metrics(
    shared_memory, owner_sessionmaker, app_user_sessionmaker, different_model, monkeypatch,  # noqa: F811
):
    from app.api import agent_knowledge, chat_sessions

    n = shared_memory.n
    async def no_worker_notification(**_kwargs):
        return None
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_worker_notification)
    n.client._transport.app.include_router(agent_knowledge.router)
    n.client._transport.app.include_router(chat_sessions.router)
    baseline = await complete_work(n, owner_sessionmaker, app_user_sessionmaker,
        model="controlled-growth-model", answer="PRIVATE_BASELINE_RESULT")
    candidate = await complete_work(n, owner_sessionmaker, app_user_sessionmaker,
        model="controlled-other-model" if different_model else "controlled-growth-model", answer="PRIVATE_CANDIDATE_RESULT")
    feedback = await n.client.post(f"/agents/{n.agent}/sessions/{candidate.session}/feedback", json={
        "label": "useful", "item_id": str(candidate.message), "reason": "The owner verified this work result."})
    assert feedback.status_code == 200, feedback.text
    session_rating = await n.client.post(f"/agents/{n.agent}/sessions/{candidate.session}/feedback", json={
        "label": "misleading", "reason": "Session context, not an exact result or a counted correction."})
    assert session_rating.status_code == 200, session_rating.text
    response = await n.client.get(f"/agents/{n.agent}/knowledge/observability", params={
        "baseline_run_id": str(baseline.run), "candidate_run_id": str(candidate.run), "include_work_samples": "true"})
    assert response.status_code == 200, response.text
    growth = response.json()["growth"]
    comparison = growth["work_comparison"]
    assert comparison["schema"] == "hive.growth_work_comparison.v1"
    assert comparison["baseline"]["result"] == baseline.answer
    assert comparison["candidate"]["result"] == candidate.answer
    for arm in ("baseline", "candidate"):
        assert comparison[arm]["status"] == "completed"
        assert comparison[arm]["duration_seconds"] >= 0
        assert comparison[arm]["usage"]["effective_tokens"] == 20
        assert comparison[arm]["cost"]["amount"] is None
        assert comparison[arm]["human_corrections"] is None
    assert comparison["candidate"]["feedback"]["useful"] == 1
    assert comparison["candidate"]["feedback"]["misleading"] == 0
    assert comparison["baseline"]["feedback"]["useful"] == 0
    conditions = {row["key"]: row for row in comparison["conditions"]}
    assert conditions["model"]["status"] == ("different" if different_model else "same")
    assert comparison["baseline"]["request_hashes"] != comparison["candidate"]["request_hashes"]
    assert conditions["business_input"]["status"] == "same"
    assert conditions["authorized_evidence"]["status"] == "same"
    assert conditions["tools"]["status"] == "same"
    assert conditions["budget"]["status"] == "unknown"
    assert comparison["status"] == ("not_comparable" if different_model else "insufficient_evidence")
    assert comparison["interpretation"] == "observations_only" and "winner" not in comparison
    assert {row["run_id"] for row in growth["work_samples"]["items"]} >= {str(baseline.run), str(candidate.run)}
    page = await n.client.get(f"/agents/{n.agent}/knowledge/observability", params={
        "include_work_samples": "true", "before_run_id": str(candidate.run)})
    assert page.status_code == 200, page.text
    assert [row["run_id"] for row in page.json()["growth"]["work_samples"]["items"]] == [str(baseline.run)]


@pytest.mark.parametrize("problem", ["member_recipient", "admin_recipient", "corrupt_input", "uncommitted"])
async def test_growth_pair_rechecks_current_recipient_and_native_sources_before_disclosing_either_result(
    shared_memory, owner_sessionmaker, app_user_sessionmaker, problem, monkeypatch,  # noqa: F811
):
    from app.api import agent_knowledge
    from app.core.security import get_current_user
    from app.database import tenant_scoped_session
    from app.models.session_v2 import SessionCommand, SessionRunOutcome
    from app.models.user import User
    from sqlalchemy import select

    n = shared_memory.n
    async def no_worker_notification(**_kwargs):
        return None
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_worker_notification)
    app = n.client._transport.app
    app.include_router(agent_knowledge.router)
    first = await complete_work(n, owner_sessionmaker, app_user_sessionmaker,
        model="controlled-growth-model", answer="PRIVATE_BASELINE_RESULT")
    second = await complete_work(n, owner_sessionmaker, app_user_sessionmaker,
        model="controlled-growth-model", answer="PRIVATE_CANDIDATE_RESULT")
    if problem.endswith("recipient"):
        if problem == "admin_recipient":
            async with owner_sessionmaker() as db:
                (await db.get(User, n.foreign)).role = "org_admin"
                await db.commit()
        async def other_user():
            async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
                return await db.get(User, n.foreign)
        app.dependency_overrides[get_current_user] = other_user
    else:
        async with owner_sessionmaker() as db:
            if problem == "uncommitted":
                outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.run_id == second.run))
                outcome.state = "needs_reconciliation"
            else:
                command = await db.scalar(select(SessionCommand).where(SessionCommand.session_id == second.session))
                command.request_hash = "0" * 64
            await db.commit()
    response = await n.client.get(f"/agents/{n.agent}/knowledge/observability", params={
        "baseline_run_id": str(first.run), "candidate_run_id": str(second.run), "include_work_samples": "true"})
    assert response.status_code == 200, response.text
    growth = response.json()["growth"]
    if problem == "admin_recipient":
        assert growth["work_comparison"]["status"] == "insufficient_evidence", response.text
        assert growth["work_comparison"]["candidate"]["result"] == "PRIVATE_CANDIDATE_RESULT"
        assert len(growth["work_samples"]["items"]) == 2
        return
    assert growth["work_comparison"]["status"] == "unavailable", response.text
    assert "PRIVATE_BASELINE_RESULT" not in response.text and "PRIVATE_CANDIDATE_RESULT" not in response.text
    if problem.endswith("recipient"):
        assert growth["work_samples"]["items"] == []
