from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._value or [])


class _FakeSession:
    """Fake async DB session that returns pre-configured values for sequential queries."""

    def __init__(self, execute_values):
        self._execute_values = list(execute_values)
        self.added = []
        self._flush_count = 0
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        # RLS GUC statements (SET LOCAL app.current_tenant_id = ...) are emitted
        # by tenant_scoped_session / enter_rls_bypass before the business query.
        # They must not consume a result from the configured sequence.
        if "app.current_tenant_id" in str(_query):
            return _FakeScalarResult(None)
        self.queries.append(_query)
        if not self._execute_values:
            return _FakeScalarResult(None)
        return _FakeScalarResult(self._execute_values.pop(0))

    def add(self, obj):
        self.added.append(obj)
        # Give ChatSession a fake id on add
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid4()

    async def flush(self):
        self._flush_count += 1

    async def commit(self):
        return None


def test_read_t2_full_uses_canonical_segment_packages_not_legacy_learnings(tmp_path, monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    legacy_dir = tmp_path / str(agent_id) / "memory" / "learnings"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "insights.md").write_text("- legacy should not appear\n", encoding="utf-8")

    package_dir = tmp_path / str(agent_id) / "memory" / "sessions" / "s1" / "segments" / "seg-1"
    package_dir.mkdir(parents=True)
    (package_dir / "summary.md").write_text("<t2_summary>canonical summary</t2_summary>", encoding="utf-8")
    (package_dir / "labels.md").write_text("<t2_labels>canonical label</t2_labels>", encoding="utf-8")
    (package_dir / "review.md").write_text(
        "<t2_review><decision>approved</decision><allowed_next>t3_intake</allowed_next></t2_review>",
        encoding="utf-8",
    )
    (package_dir / "manifest.json").write_text(
        '{"schema_version":"t2.segment-package.manifest.v1","package_status":"reviewed",'
        '"source_refs":["t0://session/s1/segment/seg-1#seq=1..2"]}\n',
        encoding="utf-8",
    )

    heartbeat._t2_mtimes.pop(agent_id, None)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    snapshot = heartbeat._read_t2_full(agent_id)

    assert "canonical summary" in snapshot
    assert "canonical label" in snapshot
    assert "legacy should not appear" not in snapshot
    assert heartbeat._t2_mtimes[agent_id]


def test_read_incremental_t2_ignores_legacy_learnings_when_no_canonical_package(tmp_path, monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    legacy_dir = tmp_path / str(agent_id) / "memory" / "learnings"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "errors.md").write_text("- legacy error should not appear\n", encoding="utf-8")

    heartbeat._t2_mtimes.pop(agent_id, None)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    snapshot = heartbeat._read_incremental_t2(agent_id)

    assert snapshot == ""
    assert "legacy error should not appear" not in snapshot
    assert heartbeat._t2_mtimes[agent_id] == {}


# ─── _parse_heartbeat_outcome ───────────────────────────────────


def test_heartbeat_instruction_contains_score_rubric() -> None:
    from app.services.heartbeat import _HEARTBEAT_TEMPLATE_PATH, _compose_heartbeat_instruction

    composed = _compose_heartbeat_instruction("base")
    template = _HEARTBEAT_TEMPLATE_PATH.read_text(encoding="utf-8")

    for text in (composed, template):
        assert "<heartbeat_score_rubric>" in text
        assert "0-1: noop" in text
        assert "2-3: failure or bootstrap/recovery" in text
        assert "4-6: useful small action" in text
        assert "7-8: high-value evidence-backed action" in text
        assert "9-10: exceptional, verified, reusable impact" in text


def test_parse_heartbeat_outcome_structured_tags():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome(
        "I updated workspace/notes.md with new priorities.\n\n[OUTCOME:action_taken] [SCORE:7]"
    )
    assert outcome == "action_taken"
    assert score == 7


def test_parse_heartbeat_outcome_accepts_curated_alias():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("Curated 3 learnings into feedback.md.\n\n[OUTCOME:curated] [SCORE:7]")
    assert outcome == "curated"
    assert score == 7


def test_heartbeat_service_does_not_expose_legacy_evolution_writeback() -> None:
    from pathlib import Path

    source = Path("app/services/heartbeat.py").read_text(encoding="utf-8")

    assert "_update_evolution_files" not in source
    assert "_auto_seed_evolution" not in source
    assert "_validate_bootstrap_completion" not in source


def test_heartbeat_memory_lifecycle_maintenance_uses_agent_data_dir(monkeypatch, tmp_path):
    from app.memory.lifecycle_store import LifecycleStatus, MemoryLifecycleStore, lifecycle_path
    from app.services import heartbeat

    agent_id = uuid4()
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    store = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id))
    store.create_sketch("expired scratch memory", entry_id="expired", expires_at=now - timedelta(minutes=5))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    report = heartbeat._run_memory_lifecycle_maintenance(agent_id, now=now)

    assert report["discarded_expired"] == ["expired"]
    assert MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id)).get("expired").status == LifecycleStatus.DISCARDED


def test_compose_heartbeat_instruction_adds_strategy_boundary() -> None:
    from app.services.heartbeat import _compose_heartbeat_instruction

    text = _compose_heartbeat_instruction("Base heartbeat")

    assert "Base heartbeat" in text
    assert "Do not write durable semantic learning into legacy evolution scorecards" in text
    assert "inactive Skill Candidate Package" in text


def test_compact_heartbeat_runtime_messages_full_fidelity_under_budget() -> None:
    """C1 (docs/agent-lifecycle-cc-alignment.md 主题 C): full fidelity first —
    a single large message that still fits the TOTAL budget must NOT be trimmed
    (the curator decides on complete input; mechanical trimming is a fallback)."""
    from app.services.heartbeat import (
        _HEARTBEAT_CONTEXT_MAX_CHARS,
        _HEARTBEAT_MESSAGE_MAX_CHARS,
        _compact_heartbeat_runtime_messages,
    )

    payload_size = _HEARTBEAT_MESSAGE_MAX_CHARS * 3  # 72K — over per-message, under total
    assert payload_size < _HEARTBEAT_CONTEXT_MAX_CHARS  # sanity: scenario is under total budget
    huge_payload = "BEGIN-" + ("x" * payload_size) + "-END"

    compacted = _compact_heartbeat_runtime_messages([{"role": "user", "content": huge_payload}])

    assert len(compacted) == 1
    assert compacted[0]["content"] == huge_payload  # untouched
    assert "truncated" not in compacted[0]["content"]


def test_compact_heartbeat_runtime_messages_trims_single_oversized_message_over_budget() -> None:
    """Mechanical trimming engages only once the TOTAL exceeds the budget,
    and stays observable (truncation marker, head+tail preserved)."""
    from app.services.heartbeat import (
        _HEARTBEAT_CONTEXT_MAX_CHARS,
        _compact_heartbeat_runtime_messages,
    )

    huge_payload = "BEGIN-" + ("x" * (_HEARTBEAT_CONTEXT_MAX_CHARS * 2)) + "-END"

    compacted = _compact_heartbeat_runtime_messages([{"role": "user", "content": huge_payload}])

    assert len(compacted) == 1
    content = compacted[0]["content"]
    assert len(content) <= _HEARTBEAT_CONTEXT_MAX_CHARS
    assert "truncated to fit heartbeat context budget" in content
    assert content.startswith("BEGIN-")
    assert content.endswith("-END")


def test_compact_heartbeat_runtime_messages_summarizes_middle_history() -> None:
    from app.services.heartbeat import (
        _HEARTBEAT_CONTEXT_MAX_CHARS,
        _compact_heartbeat_runtime_messages,
    )

    messages = [{"role": "user", "content": "base heartbeat instruction"}]
    messages.extend(
        {"role": "assistant" if idx % 2 else "user", "content": f"old-{idx}-" + ("x" * 7000)} for idx in range(24)
    )
    messages.append({"role": "user", "content": "latest tick payload"})

    compacted = _compact_heartbeat_runtime_messages(messages)
    total_chars = sum(len(msg.get("content") or "") for msg in compacted)

    assert total_chars <= _HEARTBEAT_CONTEXT_MAX_CHARS
    assert compacted[0]["content"].startswith("base heartbeat instruction")
    assert any("Heartbeat context compacted" in (msg.get("content") or "") for msg in compacted)
    assert compacted[-1]["content"] == "latest tick payload"


def test_parse_heartbeat_outcome_noop():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("Nothing to do right now. HEARTBEAT_OK\n[OUTCOME:noop] [SCORE:0]")
    assert outcome == "noop"
    assert score == 0


def test_parse_heartbeat_outcome_failure():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("Attempted to search but got rate limited.\n[OUTCOME:failure] [SCORE:2]")
    assert outcome == "failure"
    assert score == 2


def test_parse_heartbeat_outcome_fallback_heartbeat_ok():
    """When no structured tags, falls back to HEARTBEAT_OK detection."""
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("HEARTBEAT_OK")
    assert outcome == "noop"
    assert score == 0


def test_parse_heartbeat_outcome_fallback_action():
    """When no tags and no HEARTBEAT_OK, assume action_taken."""
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("I successfully fixed the error in ERRORS.md")
    assert outcome == "action_taken"
    assert score == 5


def test_parse_heartbeat_outcome_none_reply():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome(None)
    assert outcome == "noop"
    assert score is None


def test_format_heartbeat_exception_includes_type_when_message_is_empty():
    import asyncio

    from app.services.heartbeat import _format_heartbeat_exception

    assert _format_heartbeat_exception(asyncio.TimeoutError()) == "TimeoutError"


def test_format_heartbeat_exception_includes_type_and_message():
    from app.services.heartbeat import _format_heartbeat_exception

    assert _format_heartbeat_exception(ValueError("bad state")) == "ValueError: bad state"


def test_parse_heartbeat_outcome_score_capped_at_10():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("[OUTCOME:action_taken] [SCORE:99]")
    assert outcome == "action_taken"
    assert score == 10


def test_parse_heartbeat_outcome_case_insensitive():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("[outcome:FAILURE] [SCORE:1]")
    assert outcome == "failure"
    assert score == 1


def test_parse_heartbeat_no_false_positive_on_error_word():
    """The old keyword-based detection would flag 'error' in reply as failure.
    The new parser should NOT do this when structured tags are absent."""
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, _score = _parse_heartbeat_outcome("I successfully fixed the error in ERRORS.md")
    # Should be action_taken (no structured tags, no HEARTBEAT_OK) — NOT failure
    assert outcome == "action_taken"


def test_heartbeat_lease_is_mutually_exclusive():
    from app.services import heartbeat

    agent_id = uuid4()
    heartbeat._heartbeat_leases.clear()
    try:
        assert heartbeat._try_acquire_heartbeat_lease(agent_id, now=datetime.now(timezone.utc))
        assert heartbeat._try_acquire_heartbeat_lease(agent_id, now=datetime.now(timezone.utc)) is False

        heartbeat._release_heartbeat_lease(agent_id)

        assert heartbeat._try_acquire_heartbeat_lease(agent_id, now=datetime.now(timezone.utc))
    finally:
        heartbeat._heartbeat_leases.clear()


@pytest.mark.asyncio
async def test_heartbeat_distributed_lease_uses_redis(monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    calls: list[tuple] = []

    class FakeRedis:
        async def set(self, key, value, ex=None, nx=False):
            calls.append(("set", key, value, ex, nx))
            return True

        async def delete(self, key):
            calls.append(("delete", key))
            return 1

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(heartbeat, "get_redis", fake_get_redis)

    acquired = await heartbeat._try_acquire_heartbeat_lease_async(agent_id)
    await heartbeat._release_heartbeat_lease_async(agent_id)

    assert acquired is True
    assert calls[0][0] == "set"
    assert calls[0][1] == f"heartbeat_lease:{agent_id}"
    assert calls[1] == ("delete", f"heartbeat_lease:{agent_id}")


@pytest.mark.asyncio
async def test_heartbeat_distributed_lease_failure_fails_closed(monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    heartbeat._heartbeat_leases.clear()

    async def fake_get_redis():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(heartbeat, "get_redis", fake_get_redis)

    acquired = await heartbeat._try_acquire_heartbeat_lease_async(agent_id)

    assert acquired is False
    assert agent_id not in heartbeat._heartbeat_leases


# ─── _build_evolution_context ───────────────────────────────────


@pytest.mark.asyncio
async def test_build_evolution_context_cold_start_bootstrap():
    """When agent has < 3 non-heartbeat activities, inject bootstrap guidance."""
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    # Only 1 heartbeat activity, no real work
    activities = [
        SimpleNamespace(action_type="heartbeat", summary="Heartbeat: OK", detail_json={}),
    ]
    result = await _build_evolution_context(agent_id, activities)
    assert "Bootstrap Mode" in result
    assert "Read soul.md" in result
    assert "Write to evolution/" not in result
    assert "runtime records evidence into governed memory/session paths" in result


@pytest.mark.asyncio
async def test_build_evolution_context_not_cold_after_enough_activities():
    """When agent has >= 3 non-heartbeat activities, no bootstrap section."""
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    activities = [
        SimpleNamespace(action_type="chat_reply", summary="Hello", detail_json={}),
        SimpleNamespace(action_type="tool_call", summary="read_file", detail_json={"tool": "read_file"}),
        SimpleNamespace(action_type="chat_reply", summary="Done", detail_json={}),
        SimpleNamespace(action_type="heartbeat", summary="OK", detail_json={}),
    ]
    result = await _build_evolution_context(agent_id, activities)
    assert "Bootstrap Mode" not in result


@pytest.mark.asyncio
async def test_build_evolution_context_includes_error_details():
    """Error details from detail_json should be included."""
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    activities = [
        SimpleNamespace(
            action_type="error",
            summary="Tool call failed",
            detail_json={"error": "ConnectionTimeout: failed to connect to API after 30s"},
        ),
        SimpleNamespace(action_type="chat_reply", summary="OK", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="OK2", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="OK3", detail_json={}),
    ]
    result = await _build_evolution_context(agent_id, activities)
    assert "ConnectionTimeout" in result
    assert "Recent error details" in result


@pytest.mark.asyncio
async def test_build_evolution_context_suggests_skill_candidate_for_repeated_workflow():
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    activities = [
        SimpleNamespace(action_type="tool_call", summary="web_search", detail_json={"tool": "web_search"}),
        SimpleNamespace(action_type="tool_call", summary="web_search", detail_json={"tool": "web_search"}),
        SimpleNamespace(action_type="tool_call", summary="web_search", detail_json={"tool": "web_search"}),
        SimpleNamespace(action_type="tool_call", summary="web_fetch", detail_json={"tool": "web_fetch"}),
        SimpleNamespace(action_type="tool_call", summary="web_fetch", detail_json={"tool": "web_fetch"}),
        SimpleNamespace(action_type="tool_call", summary="web_fetch", detail_json={"tool": "web_fetch"}),
        SimpleNamespace(action_type="chat_reply", summary="Summarized findings", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="Delivered answer", detail_json={}),
    ]

    result = await _build_evolution_context(agent_id, activities)

    # P4 candidate lane: the nudge records evidence through T3 job artifacts;
    # the skill distillation lane owns creation.
    assert "Skill Candidate Opportunity" in result
    assert "consolidation_pitch.md" in result
    assert "skill_candidate" in result
    assert "load_skill" in result
    assert "workflow" in result.lower()


@pytest.mark.asyncio
async def test_build_evolution_context_stages_pending_t3_consolidation_job(tmp_path, monkeypatch):
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    package_dir = tmp_path / str(agent_id) / "memory" / "sessions" / "s1" / "segments" / "seg-1"
    package_dir.mkdir(parents=True)
    source_ref = "t0://session/s1/segment/seg-1#seq=1..2"
    (package_dir / "summary.md").write_text(
        f"""<t2_summary id="sum-1" status="closed">
  <segment_state value="complete">complete</segment_state>
  <continuity><state>standalone</state><reason>完整片段。</reason></continuity>
  <source_refs><source_ref uri="{source_ref}"/></source_refs>
</t2_summary>""",
        encoding="utf-8",
    )
    (package_dir / "labels.md").write_text(
        f"""<t2_labels id="lbl-1">
  <package_status>closed</package_status>
  <continuity_state>standalone</continuity_state>
  <source_refs><source_ref uri="{source_ref}"/></source_refs>
</t2_labels>""",
        encoding="utf-8",
    )
    (package_dir / "review.md").write_text(
        f"""<t2_review id="rev-1">
  <decision>approved</decision>
  <allowed_next>t3_intake</allowed_next>
  <review_rubric schema_version="t2.review_rubric.v1">
    <score name="summary_fidelity" value="0.95"/>
    <score name="source_ref_coverage" value="0.95"/>
    <score name="label_alignment" value="0.90"/>
    <score name="safety_scope" value="0.95"/>
    <score name="package_closure" value="0.90"/>
    <review_score>0.95</review_score>
  </review_rubric>
  <source_refs><source_ref uri="{source_ref}"/></source_refs>
</t2_review>""",
        encoding="utf-8",
    )
    (package_dir / "manifest.json").write_text(
        '{"schema_version":"t2.segment-package.manifest.v1","package_status":"reviewed","source_refs":["t0://session/s1/segment/seg-1#seq=1..2"]}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    result = await _build_evolution_context(
        agent_id,
        [
            SimpleNamespace(action_type="chat_reply", summary="Done", detail_json={}),
            SimpleNamespace(action_type="chat_reply", summary="Done", detail_json={}),
            SimpleNamespace(action_type="chat_reply", summary="Done", detail_json={}),
        ],
    )

    assert "T3 Consolidation Job Ready" in result
    assert "source_bundle.json" in result
    assert "t3_neighborhood.md" in result
    assert "submit_t3_consolidation_pitch" in result
    assert (tmp_path / str(agent_id) / "memory" / ".staging" / "t3_jobs").exists()


@pytest.mark.asyncio
async def test_build_evolution_context_includes_proactive_steward_plan():
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    activities = [
        SimpleNamespace(
            action_type="task_updated",
            summary="Investor memo follow-up is waiting on a local draft.",
            detail_json={"open_loop": True, "objective_id": "objective-1"},
        ),
        SimpleNamespace(action_type="chat_reply", summary="OK", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="OK2", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="OK3", detail_json={}),
    ]

    result = await _build_evolution_context(agent_id, activities)

    assert "Proactive Steward Context" in result
    assert "Prepare local draft" in result
    assert "Evidence: task_updated" in result


# ─── plaza executor limits ──────────────────────────────────────


@pytest.mark.asyncio
async def test_build_heartbeat_tool_executor_enforces_plaza_limits(monkeypatch):
    from app.services.heartbeat import _build_heartbeat_tool_executor

    agent_id = uuid4()
    creator_id = uuid4()
    calls = []

    async def fake_execute_tool(tool_name, args, _agent_id, _creator_id):
        calls.append((tool_name, args, _agent_id, _creator_id))
        return f"ran:{tool_name}"

    monkeypatch.setattr("app.services.heartbeat.execute_tool", fake_execute_tool)

    executor = _build_heartbeat_tool_executor(agent_id, creator_id)

    first_post = await executor("plaza_create_post", {"content": "post-1"})
    blocked_post = await executor("plaza_create_post", {"content": "post-2"})
    first_comment = await executor("plaza_add_comment", {"content": "comment-1"})
    second_comment = await executor("plaza_add_comment", {"content": "comment-2"})
    blocked_comment = await executor("plaza_add_comment", {"content": "comment-3"})
    generic = await executor("web_search", {"query": "heartbeat"})

    assert first_post == "ran:plaza_create_post"
    assert blocked_post.startswith("[BLOCKED]")
    assert first_comment == "ran:plaza_add_comment"
    assert second_comment == "ran:plaza_add_comment"
    assert blocked_comment.startswith("[BLOCKED]")
    assert generic == "ran:web_search"
    assert calls == [
        ("plaza_create_post", {"content": "post-1"}, agent_id, creator_id),
        ("plaza_add_comment", {"content": "comment-1"}, agent_id, creator_id),
        ("plaza_add_comment", {"content": "comment-2"}, agent_id, creator_id),
        ("web_search", {"query": "heartbeat"}, agent_id, creator_id),
    ]


# ─── _execute_heartbeat integration ────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_tick_uses_platform_managed_cadence(monkeypatch):
    """Heartbeat is platform-managed: no per-agent disable, active window, or interval override."""
    from app.services import heartbeat

    agent_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Managed Heartbeat Agent",
        tenant_id=tenant_id,
        status="idle",
        heartbeat_enabled=False,
        heartbeat_interval_minutes=9999,
        heartbeat_active_hours="00:00-00:01",
        last_heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=121),  # > managed 120min cadence
        timezone="UTC",
    )
    tenant = SimpleNamespace(id=tenant_id, timezone="UTC")
    fake_session = _FakeSession([[agent], [tenant]])
    triggered: list[uuid4] = []

    async def fake_write_audit_log(*_args, **_kwargs):
        return None

    async def fake_try_acquire(_agent_id, *, now=None):
        return True

    async def fake_execute_heartbeat(_agent_id, *, tenant_id=None, lease_acquired=False):
        return None

    def fake_create_task(coro, *args, **kwargs):
        triggered.append(agent_id)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_write_audit_log)
    monkeypatch.setattr("app.services.timezone_utils.get_agent_timezone_sync", lambda *_args, **_kwargs: "UTC")
    monkeypatch.setattr(heartbeat, "_is_in_active_hours", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(heartbeat, "_try_acquire_heartbeat_lease_async", fake_try_acquire)
    monkeypatch.setattr(heartbeat, "_execute_heartbeat", fake_execute_heartbeat)
    monkeypatch.setattr(heartbeat.asyncio, "create_task", fake_create_task)

    await heartbeat._heartbeat_tick()

    query_text = "\n".join(str(query) for query in fake_session.queries)
    assert "where agents.heartbeat_enabled" not in query_text.lower()
    assert triggered == [agent_id]


@pytest.mark.asyncio
async def test_execute_heartbeat_uses_correct_settings(monkeypatch):
    """Verify invoke_agent is called with core_tools_only=False, max_tool_rounds=40,
    and a heartbeat session is created."""
    from app.services.heartbeat import _execute_heartbeat

    agent_id = uuid4()
    creator_id = uuid4()
    model_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Heartbeat Agent",
        role_description="Watcher",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
        last_heartbeat_at=None,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    participant = SimpleNamespace(id=uuid4(), type="agent", ref_id=agent_id)

    # Sequence: Agent, LLMModel, ActivityLogs, Participant
    fake_session = _FakeSession([agent, model, [], participant])
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="Did work\n[OUTCOME:action_taken] [SCORE:5]")

    async def fake_try_acquire_heartbeat_lease(_agent_id):
        return True

    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr("app.services.heartbeat.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.services.heartbeat._load_heartbeat_instruction", lambda _id: "HB")
    monkeypatch.setattr("app.services.heartbeat._try_acquire_heartbeat_lease_async", fake_try_acquire_heartbeat_lease)

    # Stub activity logger and execution context
    async def _noop_log(*args, **kwargs):
        pass

    monkeypatch.setattr("app.core.execution_context.set_agent_bot_identity", lambda *a, **kw: None)

    await _execute_heartbeat(agent_id, tenant_id=tenant_id)

    request = captured["request"]

    # Core assertions — the critical fixes
    assert request.core_tools_only is False, "Heartbeat should have full tool access"
    assert request.max_tool_rounds == 40, "Heartbeat should have enough rounds for multi-step curation"
    assert request.session_context is not None
    assert request.session_context.source == "heartbeat"
    assert request.session_context.session_id is not None, "Heartbeat must have session_id for memory"
    assert request.on_tool_call is not None, "Heartbeat must persist tool calls"
    assert request.execution_identity.identity_type == "agent_bot"


@pytest.mark.asyncio
async def test_execute_heartbeat_marks_runtime_task_skipped_when_no_model(monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="No Model Agent",
        primary_model_id=None,
        fallback_model_id=None,
        creator_id=uuid4(),
        tenant_id=uuid4(),
    )
    fake_session = _FakeSession([agent])
    created = []
    updates = []

    async def fake_create_runtime_task_record(**kwargs):
        created.append(kwargs)
        return "heartbeat-task-1"

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr(heartbeat, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(heartbeat, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.core.execution_context.set_agent_bot_identity", lambda *a, **kw: None)

    await heartbeat._execute_heartbeat(agent_id, tenant_id=agent.tenant_id, lease_acquired=True)

    assert created[0]["task_type"] == "heartbeat"
    assert created[0]["status"] == "running"
    assert created[0]["parent_agent_id"] == agent_id
    assert created[0]["metadata_json"]["resume_after_restart"] is True
    assert created[0]["metadata_json"]["resumable_heartbeat"] is True
    assert created[0]["metadata_json"]["restart_replay_contract"]["task_type"] == "heartbeat"
    assert created[0]["metadata_json"]["restart_replay_journal"][0]["phase"] == "spawn_intent_recorded"
    assert updates[-1][0] == "heartbeat-task-1"
    assert updates[-1][1]["status"] == "skipped"
    assert updates[-1][1]["metadata_json"]["skip_reason"] == "no_model"


@pytest.mark.asyncio
async def test_resume_persisted_heartbeat_runs_requeues_unstarted_run(monkeypatch):
    from app.services import heartbeat

    run_id = uuid4().hex
    agent_id = uuid4()
    tenant_id = uuid4()
    updates: list[tuple[str, dict]] = []
    scheduled: list[tuple[object, object, object]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": "heartbeat",
                "parent_agent_id": str(agent_id),
                "trace_id": f"heartbeat:{run_id}",
                "child_session_id": None,
                "metadata": {
                    "agent_id": str(agent_id),
                    "tenant_id": str(tenant_id),
                    "resume_after_restart": True,
                    "resumable_heartbeat": True,
                    "side_effect_risk": "internal_governed",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "task_type": "heartbeat",
                        "task_id": run_id,
                        "idempotency_key": f"heartbeat:{run_id}:restart",
                    },
                },
            }
        ]

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    def fake_create_task(coro, *args, **kwargs):
        frame = coro.cr_frame
        scheduled.append(
            (
                frame.f_locals["agent_id"],
                frame.f_locals["tenant_id"],
                frame.f_locals["runtime_task_id"],
            )
        )
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(heartbeat, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(heartbeat, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(heartbeat.asyncio, "create_task", fake_create_task)

    resumed = await heartbeat.resume_persisted_heartbeat_runs()

    assert resumed == [run_id]
    assert scheduled == [(agent_id, tenant_id, run_id)]
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "running"
    assert updates[-1][1]["metadata_json"]["resumed_after_restart"] is True
    assert updates[-1][1]["metadata_json"]["restart_replay_journal"][-1]["phase"] == "resume_intent_recorded"


@pytest.mark.asyncio
async def test_resume_persisted_heartbeat_runs_requires_reconciliation_after_session_bind(monkeypatch):
    from app.services import heartbeat

    run_id = uuid4().hex
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": "heartbeat",
                "parent_agent_id": str(uuid4()),
                "trace_id": f"heartbeat:{run_id}",
                "child_session_id": str(uuid4()),
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_heartbeat": True,
                    "side_effect_risk": "internal_governed",
                },
            }
        ]

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    def fake_create_task(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("session-bound heartbeat must not be replayed blindly")

    monkeypatch.setattr(heartbeat, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(heartbeat, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(heartbeat.asyncio, "create_task", fake_create_task)

    resumed = await heartbeat.resume_persisted_heartbeat_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "session_bound_heartbeat"


@pytest.mark.asyncio
async def test_execute_heartbeat_does_not_resurrect_session_state_after_reset(monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    creator_id = uuid4()
    model_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Reset Race Agent",
        role_description="Watcher",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
        last_heartbeat_at=None,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    participant = SimpleNamespace(id=uuid4(), type="agent", ref_id=agent_id)
    fake_session = _FakeSession([agent, model, [], participant])

    async def fake_invoke_agent(_request):
        heartbeat._reset_heartbeat_session(agent_id)
        return SimpleNamespace(content="Nothing changed\n[OUTCOME:noop] [SCORE:1]")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr(heartbeat, "invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(heartbeat, "_load_heartbeat_instruction", lambda _id: "HB")
    monkeypatch.setattr("app.core.execution_context.set_agent_bot_identity", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.activity_logger.log_activity", _noop)

    heartbeat._heartbeat_contexts.pop(agent_id, None)
    heartbeat._heartbeat_session_ids.pop(agent_id, None)
    heartbeat._heartbeat_tick_counts.pop(agent_id, None)
    heartbeat._heartbeat_session_ctxs.pop(agent_id, None)

    await heartbeat._execute_heartbeat(agent_id, tenant_id=tenant_id, lease_acquired=True)

    assert agent_id not in heartbeat._heartbeat_contexts
    assert agent_id not in heartbeat._heartbeat_session_ids


@pytest.mark.asyncio
async def test_execute_heartbeat_recovers_from_incomplete_persistent_session(monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    creator_id = uuid4()
    model_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Recovered Heartbeat Agent",
        role_description="Watcher",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
        last_heartbeat_at=None,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    participant = SimpleNamespace(id=uuid4(), type="agent", ref_id=agent_id)
    fake_session = _FakeSession([agent, model, [], participant])
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="Recovered\n[OUTCOME:noop] [SCORE:1]")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr(heartbeat, "invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(heartbeat, "_load_heartbeat_instruction", lambda _id: "HB")
    monkeypatch.setattr("app.core.execution_context.set_agent_bot_identity", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.activity_logger.log_activity", _noop)

    heartbeat._heartbeat_contexts[agent_id] = [{"role": "user", "content": "stale context"}]
    heartbeat._heartbeat_session_ids.pop(agent_id, None)
    heartbeat._heartbeat_tick_counts[agent_id] = 3
    heartbeat._heartbeat_session_ctxs.pop(agent_id, None)

    await heartbeat._execute_heartbeat(agent_id, tenant_id=tenant_id, lease_acquired=True)

    assert captured["request"].messages[0]["content"].startswith("HB")
    assert agent_id in heartbeat._heartbeat_contexts
    assert agent_id in heartbeat._heartbeat_session_ids
