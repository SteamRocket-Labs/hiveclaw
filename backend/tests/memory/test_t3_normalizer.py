"""Tests for T3 format normalizer (PR-9).

The heartbeat LLM is trusted to write T3 files in canonical `- [YYYY-MM-DD] desc`
form, but nothing enforces that. These tests lock down the auto-repair so
drifted bullets don't silently disappear from the dream's consolidation.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.memory.md_store import ensure_t3_layout, validate_and_normalize_t3


def _seed(mem_dir: Path, filename: str, body: str) -> Path:
    path = mem_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    # Force a recent mtime so the normalizer sees it.
    os.utime(path, None)
    return path


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _run(data_root: Path, agent_id: uuid.UUID) -> dict:
    return validate_and_normalize_t3(data_root, agent_id)


class TestNormalizerRepairs:
    def test_fixes_star_bullets(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()
        ensure_t3_layout(tmp_path, agent_id)
        mem_dir = tmp_path / str(agent_id) / "memory"
        _seed(
            mem_dir,
            "t3/user.md",
            "# Feedback\n\n* user prefers concise output\n* always ask before sending\n",
        )

        report = _run(tmp_path, agent_id)

        content = (mem_dir / "t3" / "user.md").read_text(encoding="utf-8")
        today = _today()
        assert f"- [{today}] user prefers concise output" in content
        assert f"- [{today}] always ask before sending" in content
        assert "*" not in content  # every star bullet repaired
        assert report["fixed"] == 2
        assert "t3/user.md" in report["files_touched"]

    def test_fixes_numbered_bullets(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()
        ensure_t3_layout(tmp_path, agent_id)
        mem_dir = tmp_path / str(agent_id) / "memory"
        _seed(
            mem_dir,
            "t3/capabilities.md",
            "# Strategies\n\n1. break down into subtasks\n2. verify after each step\n",
        )

        report = _run(tmp_path, agent_id)

        content = (mem_dir / "t3" / "capabilities.md").read_text(encoding="utf-8")
        today = _today()
        assert f"- [{today}] break down into subtasks" in content
        assert f"- [{today}] verify after each step" in content
        assert report["fixed"] == 2

    def test_adds_missing_date_when_content_long_enough(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()
        ensure_t3_layout(tmp_path, agent_id)
        mem_dir = tmp_path / str(agent_id) / "memory"
        _seed(mem_dir, "t3/capabilities.md", "# Knowledge\n\n- this is a real durable observation\n")

        report = _run(tmp_path, agent_id)

        content = (mem_dir / "t3" / "capabilities.md").read_text(encoding="utf-8")
        today = _today()
        assert f"- [{today}] this is a real durable observation" in content
        assert report["fixed"] == 1

    def test_short_dateless_bullet_left_alone(self, tmp_path: Path) -> None:
        """Don't invent dates for tiny bullets — they're probably template artifacts."""
        agent_id = uuid.uuid4()
        ensure_t3_layout(tmp_path, agent_id)
        mem_dir = tmp_path / str(agent_id) / "memory"
        _seed(mem_dir, "t3/worker.md", "# Blocked Patterns\n\n- short\n")

        report = _run(tmp_path, agent_id)

        content = (mem_dir / "t3" / "worker.md").read_text(encoding="utf-8")
        assert content.count(f"- [{_today()}]") == 0
        assert report["fixed"] == 0


class TestNormalizerSafety:
    def test_plain_text_line_warns_but_stays_in_file(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()
        ensure_t3_layout(tmp_path, agent_id)
        mem_dir = tmp_path / str(agent_id) / "memory"
        _seed(mem_dir, "t3/user.md", "# User Profile\n\nHello world.\n")

        report = _run(tmp_path, agent_id)

        content = (mem_dir / "t3" / "user.md").read_text(encoding="utf-8")
        assert "Hello world." in content  # not deleted
        assert any("Hello world." in w for w in report["warnings"])
        assert report["fixed"] == 0

    def test_preserves_file_headers(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()
        ensure_t3_layout(tmp_path, agent_id)
        mem_dir = tmp_path / str(agent_id) / "memory"
        body = "# Feedback\n\n## Some section heading\n\n* need normalization here long enough\n"
        _seed(mem_dir, "t3/user.md", body)

        _run(tmp_path, agent_id)

        content = (mem_dir / "t3" / "user.md").read_text(encoding="utf-8")
        assert content.startswith("# Feedback")
        assert "## Some section heading" in content

    def test_idempotent_on_clean_file(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()
        ensure_t3_layout(tmp_path, agent_id)
        mem_dir = tmp_path / str(agent_id) / "memory"
        body = "# Feedback\n\n- [2026-04-15] already canonical\n- [2026-04-15] also canonical\n"
        path = _seed(mem_dir, "t3/user.md", body)
        original = path.read_text(encoding="utf-8")

        first = _run(tmp_path, agent_id)
        second = _run(tmp_path, agent_id)

        assert first["fixed"] == 0
        assert second["fixed"] == 0
        assert path.read_text(encoding="utf-8") == original

    def test_skips_files_outside_recent_window(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()
        ensure_t3_layout(tmp_path, agent_id)
        mem_dir = tmp_path / str(agent_id) / "memory"
        path = _seed(mem_dir, "t3/user.md", "# Feedback\n\n* drifted line long enough to trigger repair\n")
        # Backdate far beyond the default 1-hour window.
        old_ts = time.time() - 7200
        os.utime(path, (old_ts, old_ts))

        report = _run(tmp_path, agent_id)

        assert report["fixed"] == 0
        assert path.read_text(encoding="utf-8").startswith("# Feedback\n\n*")

    def test_missing_memory_dir_returns_empty(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()  # no ensure_t3_layout → no memory dir
        report = _run(tmp_path, agent_id)
        assert report == {"fixed": 0, "warnings": [], "files_touched": []}


class TestNormalizerReportShape:
    def test_files_touched_only_lists_actually_modified(self, tmp_path: Path) -> None:
        agent_id = uuid.uuid4()
        ensure_t3_layout(tmp_path, agent_id)
        mem_dir = tmp_path / str(agent_id) / "memory"
        _seed(mem_dir, "t3/user.md", "# Feedback\n\n* needs normalization to be longer than ten chars\n")
        _seed(mem_dir, "t3/capabilities.md", "# Knowledge\n\n- [2026-04-15] clean entry\n")

        report = _run(tmp_path, agent_id)

        assert report["files_touched"] == ["t3/user.md"]
        assert report["fixed"] == 1
