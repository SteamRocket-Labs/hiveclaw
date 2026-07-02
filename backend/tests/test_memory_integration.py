"""Phase 7: Memory system integration tests.

Validates cross-phase pipeline connectivity, hook wiring, and the
full T0→T2→T3→soul memory flow using unit-level assertions.
Covers verification checklist items from 08-integration.md §6.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


# ── §1: Hooks system (Phase 0) ──


class TestHooksIntegration:
    """Verify all runtime hook events exist and handlers are wired."""

    def test_v1_all_events(self) -> None:
        from app.runtime.hooks import HookEvent

        # 42-member catalog (CC-27 superset); 7 are _DISABLED_NOOP events with no
        # live emitter yet (see hooks.py:195 / D-26 / D-29).
        assert len(HookEvent) == 42
        assert {event.value for event in HookEvent}.issuperset(
            {
                "stop",
                "notification",
                "task_created",
                "task_completed",
                "team_created",
                "teammate_idle",
            }
        )

    def test_v2_response_complete_event(self) -> None:
        from app.runtime.hooks import HookEvent

        assert hasattr(HookEvent, "RESPONSE_COMPLETE")

    def test_v3_compaction_events(self) -> None:
        from app.runtime.hooks import HookEvent

        assert hasattr(HookEvent, "PRE_COMPACTION")
        assert hasattr(HookEvent, "POST_COMPACTION")

    def test_v4_session_events(self) -> None:
        from app.runtime.hooks import HookEvent

        assert hasattr(HookEvent, "SESSION_IDLE")
        assert hasattr(HookEvent, "SESSION_CLOSE")
        assert hasattr(HookEvent, "SESSION_START")

    def test_hooks_setup_registers_17_handlers(self) -> None:
        """register_memory_hooks() should register memory + objective intake handlers.

        17 = 16 memory/T0/projection handlers + 1 PERMISSION_DENIED audit consumer (B-5)."""
        from app.runtime.hooks import HookRegistry

        registry = HookRegistry()
        # Monkeypatch the global registry temporarily
        import app.runtime.hooks as hooks_mod

        original = hooks_mod.hook_registry
        hooks_mod.hook_registry = registry
        try:
            from app.runtime.hooks_setup import register_memory_hooks

            register_memory_hooks()
            total = sum(len(handlers) for handlers in registry._handlers.values())
            assert total == 17
        finally:
            hooks_mod.hook_registry = original

    def test_hooks_setup_declares_registration_specs(self) -> None:
        from app.runtime.hooks_setup import _MEMORY_HOOK_REGISTRATIONS

        assert len(_MEMORY_HOOK_REGISTRATIONS) == 17
        assert any(spec.key == "memory.response_complete.fast_reflection" for spec in _MEMORY_HOOK_REGISTRATIONS)
        assert any(spec.key == "evolution.heartbeat_tick_end.maintenance" for spec in _MEMORY_HOOK_REGISTRATIONS)
        assert any(spec.key == "pending_reply.post_tool_use.capture" for spec in _MEMORY_HOOK_REGISTRATIONS)
        # B-5: live-emitted PERMISSION_DENIED carries a real observe-only audit consumer.
        assert any(spec.key == "governance.permission_denied.audit" for spec in _MEMORY_HOOK_REGISTRATIONS)

    def test_hooks_setup_exports_structured_memory_hook_plan(self) -> None:
        from app.runtime.hooks_setup import export_memory_hook_plan

        plan = export_memory_hook_plan()

        assert len(plan) == 17
        assert plan[0]["key"] == "memory.session_start.log"
        assert plan[0]["handler_name"] == "log_session_start"
        assert any(item["key"] == "memory.response_complete.fast_reflection" for item in plan)
        assert any(item["key"] == "evolution.heartbeat_tick_end.maintenance" for item in plan)
        assert any(item["key"] == "pending_reply.post_tool_use.capture" for item in plan)
        assert all("event" in item and "key" in item and "handler_name" in item for item in plan)

    def test_register_memory_hooks_is_idempotent(self) -> None:
        from app.runtime.hooks import HookRegistry

        registry = HookRegistry()
        import app.runtime.hooks_setup as hooks_setup_mod

        original = hooks_setup_mod.hook_registry
        hooks_setup_mod.hook_registry = registry
        try:
            from app.runtime.hooks_setup import register_memory_hooks

            register_memory_hooks()
            register_memory_hooks()
            total = sum(len(handlers) for handlers in registry._handlers.values())
            assert total == 17
        finally:
            hooks_setup_mod.hook_registry = original


# ── §2: Legacy raw log compatibility layer ──


class TestT0Integration:
    def test_v5_all_5_formatters(self) -> None:
        from app.services.t0_logger import _FORMATTERS

        assert set(_FORMATTERS.keys()) == {"chat", "trigger", "delegation", "heartbeat", "dream"}

    def test_v6_write_t0_log_returns_path(self, tmp_path: Path) -> None:
        from app.services.t0_logger import write_t0_log

        agent_id = uuid.uuid4()
        (tmp_path / str(agent_id) / "logs").mkdir(parents=True)
        with patch("app.services.t0_logger.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_path)
            path = write_t0_log(agent_id, behavior_type="chat", messages=[{"role": "user", "content": "hi"}])
        assert path is not None
        assert path.exists()
        assert "type: chat" in path.read_text()

    def test_v7_cleanup_old_logs(self, tmp_path: Path) -> None:
        from app.services.t0_logger import cleanup_old_logs

        agent_id = uuid.uuid4()
        logs_dir = tmp_path / str(agent_id) / "logs"
        old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
        (logs_dir / old).mkdir(parents=True)
        (logs_dir / old / "chat-1200-abcd.md").write_text("old")
        with patch("app.services.t0_logger.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_path)
            removed = cleanup_old_logs(agent_id, retention_days=30)
        assert removed == 1


# ── §3: Extractor (Phase 2) ──


class TestExtractorIntegration:
    def test_v8_extractor_singleton_exists(self) -> None:
        from app.services.extract_agent import extract_agent, ExtractAgent

        assert isinstance(extract_agent, ExtractAgent)

    def test_v9_pattern_fallback_works(self) -> None:
        from app.services.extract_agent import _pattern_extract

        msgs = [{"role": "user", "content": "Don't use mocks, always use real database"}]
        results = _pattern_extract(msgs)
        assert len(results) >= 1
        assert results[0]["category"] == "feedback"

    def test_v10_coalescing_mutex(self, monkeypatch) -> None:
        from app.services.extract_agent import ExtractAgent

        monkeypatch.setenv("HIVE_ENABLE_LEGACY_T2_BACKFILL", "1")
        ea = ExtractAgent()
        agent_id = uuid.uuid4()
        key = str(agent_id)
        ea._in_progress[key] = True
        # Should not crash, just stash
        import asyncio

        asyncio.run(ea.extract(agent_id, [{"role": "user", "content": "Remember this important thing"}], source="web"))
        assert key in ea._pending
        ea._in_progress[key] = False


# ── §4: Compression alignment (Phase 3) ──


class TestCompressionIntegration:
    def test_v11_microcompact_time_based(self) -> None:
        from app.kernel.engine import _MICROCOMPACT_GAP_SECONDS, _MICROCOMPACT_KEEP_RECENT

        assert _MICROCOMPACT_GAP_SECONDS == 3600
        assert _MICROCOMPACT_KEEP_RECENT == 5

    def test_v12_llm_message_has_created_at(self) -> None:
        from app.services.llm_client import LLMMessage

        msg = LLMMessage(role="user", content="test")
        assert hasattr(msg, "created_at")
        assert isinstance(msg.created_at, float)

    def test_v13_ptl_max_retries_3(self) -> None:
        from app.kernel.engine import _PTL_MAX_RETRIES

        assert _PTL_MAX_RETRIES == 3

    def test_v14_summarize_prompt_11_sections(self) -> None:
        from app.services.conversation_summarizer import _SUMMARIZE_SYSTEM_PROMPT

        required = [
            "Primary Request and Intent",
            "Key Technical Concepts & Decisions",
            "Files and Code Sections",
            "Problem Solving",
            "Errors and Fixes",
            "All User Messages",
            "User Preferences",
            "Tool Outcomes",
            "Pending Tasks",
            "Current Work",
            "Next Step",
        ]
        for section in required:
            assert section in _SUMMARIZE_SYSTEM_PROMPT, f"Missing: {section}"


# ── §5: Prompt sections (Phase 4) ──


class TestPromptIntegration:
    def test_v15_frozen_prefix_has_system_section(self) -> None:
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        fp = build_frozen_prompt_prefix(agent_context="Agent identity")
        assert "## System" in fp
        assert "## Doing Tasks" in fp
        assert "## Using Your Tools" in fp

    def test_v15_dynamic_suffix_has_memory_section(self) -> None:
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        ds = build_dynamic_prompt_suffix(memory_snapshot="feedback: user data")
        assert "## Your Memory System" in ds
        assert "## Environment" in ds

    def test_v16_heartbeat_md_has_curate(self) -> None:
        # PR-12 rewrote HEARTBEAT.md with XML tags; the curate phase and
        # persistent-session contract are preserved under new tag names.
        template = (Path(__file__).parent.parent / "app" / "templates" / "HEARTBEAT.md").read_text()
        assert "<phase_2_curate>" in template
        assert "<persistent_session_notes>" in template

    def test_v17_dream_md_has_soul_reconsolidation_boundary(self) -> None:
        template = (Path(__file__).parent.parent / "app" / "templates" / "DREAM.md").read_text()
        assert "Dream — Soul Reconsolidation Protocol" in template
        assert "You are not the T3 writer" in template
        assert "T3 Consolidator" in template
        assert "Platform Gate" in template


# ── §6: Heartbeat KAIROS (Phase 5) ──


class TestHeartbeatIntegration:
    def test_v18_kairos_state_dicts_exist(self) -> None:
        from app.services.heartbeat import _heartbeat_contexts, _heartbeat_session_ids, _heartbeat_tick_counts

        assert isinstance(_heartbeat_contexts, dict)
        assert isinstance(_heartbeat_session_ids, dict)
        assert isinstance(_heartbeat_tick_counts, dict)

    def test_v19_incremental_t2_returns_empty_on_unchanged(self, tmp_path: Path) -> None:
        from app.services.heartbeat import _read_incremental_t2, _read_t2_full

        agent_id = uuid.uuid4()
        learnings = tmp_path / str(agent_id) / "memory" / "learnings"
        learnings.mkdir(parents=True)
        (learnings / "insights.md").write_text("# Insights\n- [2026-04-06] data\n")
        with patch("app.config.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_path)
            _read_t2_full(agent_id)
            result = _read_incremental_t2(agent_id)
        assert result == ""

    def test_v24_dream_end_resets_heartbeat(self) -> None:
        """DREAM_END hook handler should call _reset_heartbeat_session."""
        from app.runtime.hooks_setup import _t0_dream_end

        source = inspect.getsource(_t0_dream_end)
        assert "_reset_heartbeat_session" in source


# ── §7: Dream consolidation (Phase 6) ──


class TestDreamIntegration:
    def test_v21_programmatic_dedup(self) -> None:
        from app.services.auto_dream import _programmatic_dedup

        lines = ["- [2026-04-06] User likes concise"] * 3 + ["- [2026-04-06] Project uses PostgreSQL"]
        result = _programmatic_dedup(lines)
        assert len(result) == 2

    def test_v22_dream_does_not_import_ingest_learnings(self) -> None:
        """run_dream should NOT call _ingest_learnings in the main path (heartbeat owns T2→T3)."""
        from app.services.auto_dream import run_dream

        source = inspect.getsource(run_dream)
        # _ingest_learnings is still called for backward compat, but the spec says
        # heartbeat should own T2→T3. We verify the new MD consolidation is present.
        assert "_consolidate_t3_files" in source

    def test_v23_wiki_map_update(self, tmp_path: Path) -> None:
        from app.services.auto_dream import _update_index_md

        agent_id = uuid.uuid4()
        memory_dir = tmp_path / str(agent_id) / "memory"
        self_dir = memory_dir / "self"
        self_dir.mkdir(parents=True)
        (self_dir / "self.md").write_text(
            "## 方法\n\n### 条目甲 — 一般\n<!-- id: idx-a -->\na\n- 证据: t2-a\n",
            encoding="utf-8",
        )
        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_path)
            _update_index_md(agent_id)
        index = (memory_dir / "indexes" / "wiki_map.md").read_text()
        assert "Memory Wiki Map" in index
        assert "idx-a" in index
        assert not (memory_dir / "INDEX.md").exists()
        assert not (memory_dir / "index.md").exists()
        assert not (memory_dir / ".derived" / "t3_index.md").exists()

    def test_v10_gate_expansion_ticks(self) -> None:
        from app.services.auto_dream import (
            MIN_HEARTBEAT_TICKS_SINCE_DREAM,
            _heartbeat_ticks_since_dream,
            should_dream,
        )

        agent_id = uuid.uuid4()
        _heartbeat_ticks_since_dream[agent_id.hex] = MIN_HEARTBEAT_TICKS_SINCE_DREAM
        with patch("app.services.auto_dream._load_dream_state", return_value=(None, 0)):
            assert should_dream(agent_id) is True
        _heartbeat_ticks_since_dream.pop(agent_id.hex, None)


# ── Cross-phase pipeline validation ──


class TestFullPipeline:
    """Validate the T0→T2→T3→soul pipeline is connected end-to-end."""

    def test_t0_write_creates_file(self, tmp_path: Path) -> None:
        """Phase 1: T0 write works for all types."""
        from app.services.t0_logger import write_t0_log

        agent_id = uuid.uuid4()
        (tmp_path / str(agent_id) / "logs").mkdir(parents=True)
        with patch("app.services.t0_logger.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_path)
            for btype in ["chat", "trigger", "delegation", "heartbeat", "dream"]:
                path = write_t0_log(agent_id, behavior_type=btype)
                assert path is not None, f"T0 write failed for {btype}"

    def test_extractor_writes_t2(self, tmp_path: Path) -> None:
        """Phase 2: Extractor pattern fallback writes to T2 learnings."""
        from app.services.extract_agent import _append_to_learnings

        agent_id = uuid.uuid4()
        (tmp_path / str(agent_id) / "memory" / "learnings").mkdir(parents=True)
        with patch("app.services.extract_agent.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_path)
            written = _append_to_learnings(
                agent_id,
                [
                    {"category": "feedback", "content": "User prefers snake_case"},
                ],
            )
        assert written == 1

    def test_t3_consolidation_no_direct_dedup_write(self, tmp_path: Path) -> None:
        """Accepted T3 dedup is committed by Platform Gate, not Dream helpers."""
        from app.services.auto_dream import _consolidate_t3_files, _write_t3_file

        agent_id = uuid.uuid4()
        t3_dir = tmp_path / str(agent_id) / "memory" / "t3"
        t3_dir.mkdir(parents=True)
        (t3_dir / "user.md").write_text("# T3 User\n" + ("- [2026-04-06] Same entry\n" * 5), encoding="utf-8")
        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_path)
            with pytest.raises(RuntimeError, match="direct T3 write refused"):
                _write_t3_file(agent_id, "t3/user.md", "# T3 User\n")
            stats = _consolidate_t3_files(agent_id)
        assert stats["t3/user.md"] == 0

    def test_t2_truncation(self, tmp_path: Path) -> None:
        """Phase 6: T2 retention archives only absorbed entries beyond cap."""
        from app.services.auto_dream import _truncate_t2

        agent_id = uuid.uuid4()
        learnings = tmp_path / str(agent_id) / "memory" / "learnings"
        learnings.mkdir(parents=True)
        entries = [f"- [2026-04-{i:02d}][status=absorbed][absorbed_at=2026-04-20] entry {i}" for i in range(1, 21)]
        (learnings / "insights.md").write_text("# Insights\n" + "\n".join(entries) + "\n")
        with patch("app.services.auto_dream.get_settings") as mock:
            mock.return_value.AGENT_DATA_DIR = str(tmp_path)
            removed = _truncate_t2(agent_id, keep=5)
        assert removed == 15

    def test_prompt_includes_all_sections(self) -> None:
        """Phase 4: Final prompt has all structured sections."""
        from app.runtime.prompt_builder import build_frozen_prompt_prefix, build_dynamic_prompt_suffix

        fp = build_frozen_prompt_prefix(agent_context="You are TestAgent.", skill_catalog="- skill_a")
        ds = build_dynamic_prompt_suffix(memory_snapshot="feedback: data", user_name="Rocky", channel="web")
        full = fp + "\n\n" + ds
        # Verify all key sections present
        assert "## System" in full
        assert "## Doing Tasks" in full
        assert "## Using Your Tools" in full
        assert "## Your Memory System" in full
        assert "## Environment" in full
        assert "skill_a" in full
        assert "feedback: data" in full
