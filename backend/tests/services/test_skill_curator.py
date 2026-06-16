"""Tests for the skill curator anti-entropy cleanup loop.

No mocks — every test drives real files under ``tmp_path``. The pure
state-machine (``apply_skill_auto_transitions``) is exercised in isolation,
and the orchestrator (``run_skill_curator_pass``) is verified end-to-end
against a real workspace with on-disk SKILL.md folders.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _write_skill(skills_dir: Path, slug: str, *, name: str | None = None) -> Path:
    skill_dir = skills_dir / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f'---\nname: "{name or slug}"\ndescription: "test skill"\n---\n# {slug}\nbody\n',
        encoding="utf-8",
    )
    return skill_dir


# ---------------------------------------------------------------------------
# Usage store read/write round-trip
# ---------------------------------------------------------------------------


def test_usage_store_roundtrip(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, save_skill_usage

    assert load_skill_usage(tmp_path) == {}

    data = {"deploy": {"created_by": "agent", "use_count": 3, "state": "active"}}
    save_skill_usage(tmp_path, data)

    reloaded = load_skill_usage(tmp_path)
    assert reloaded["deploy"]["use_count"] == 3
    assert reloaded["deploy"]["created_by"] == "agent"
    assert (tmp_path / "evolution" / "skill_usage.json").exists()
    assert not (tmp_path / "skills" / ".usage.json").exists()


def test_load_skill_usage_migrates_legacy_sidecar(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / ".usage.json").write_text(
        '{"deploy": {"created_by": "agent", "use_count": 2, "state": "active"}}',
        encoding="utf-8",
    )

    usage = load_skill_usage(tmp_path)

    assert usage["deploy"]["use_count"] == 2
    assert (tmp_path / "evolution" / "skill_usage.json").exists()
    assert not (skills_dir / ".usage.json").exists()


def test_load_skill_usage_tolerates_corrupt_file(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage

    evolution_dir = tmp_path / "evolution"
    evolution_dir.mkdir(parents=True, exist_ok=True)
    (evolution_dir / "skill_usage.json").write_text("{not valid json", encoding="utf-8")

    assert load_skill_usage(tmp_path) == {}


# ---------------------------------------------------------------------------
# mark_skill_created
# ---------------------------------------------------------------------------


def test_mark_skill_created_initializes_record(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, mark_skill_created

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mark_skill_created(tmp_path, "deploy-checklist", created_by="agent", now=now)

    rec = load_skill_usage(tmp_path)["deploy-checklist"]
    assert rec["created_by"] == "agent"
    assert rec["created_at"] == _iso(now)
    assert rec["use_count"] == 0
    assert rec["view_count"] == 0
    assert rec["last_used_at"] is None
    assert rec["state"] == "active"
    assert rec["pinned"] is False
    assert rec["archived_at"] is None


def test_mark_skill_created_does_not_overwrite_existing_provenance(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, mark_skill_created

    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mark_skill_created(tmp_path, "deploy", created_by="agent", now=first)

    later = datetime(2026, 2, 1, tzinfo=timezone.utc)
    mark_skill_created(tmp_path, "deploy", created_by="template", now=later)

    rec = load_skill_usage(tmp_path)["deploy"]
    # created_at / created_by are immutable once set.
    assert rec["created_at"] == _iso(first)
    assert rec["created_by"] == "agent"


# ---------------------------------------------------------------------------
# bump_skill_use
# ---------------------------------------------------------------------------


def test_bump_skill_use_increments_and_refreshes(tmp_path: Path) -> None:
    from app.services.skill_curator import bump_skill_use, load_skill_usage, mark_skill_created

    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mark_skill_created(tmp_path, "deploy", created_by="agent", now=created)

    used = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bump_skill_use(tmp_path, "deploy", kind="use", now=used)
    bump_skill_use(tmp_path, "deploy", kind="use", now=used)

    rec = load_skill_usage(tmp_path)["deploy"]
    assert rec["use_count"] == 2
    assert rec["last_used_at"] == _iso(used)
    assert rec["view_count"] == 0


def test_bump_skill_view_increments_view_count_only(tmp_path: Path) -> None:
    from app.services.skill_curator import bump_skill_use, load_skill_usage, mark_skill_created

    mark_skill_created(tmp_path, "deploy", created_by="agent", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    bump_skill_use(tmp_path, "deploy", kind="view", now=datetime(2026, 1, 5, tzinfo=timezone.utc))

    rec = load_skill_usage(tmp_path)["deploy"]
    assert rec["view_count"] == 1
    assert rec["use_count"] == 0
    assert rec["last_used_at"] is None


def test_bump_skill_use_creates_record_when_missing(tmp_path: Path) -> None:
    from app.services.skill_curator import bump_skill_use, load_skill_usage

    used = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bump_skill_use(tmp_path, "orphan", kind="use", now=used)

    rec = load_skill_usage(tmp_path)["orphan"]
    assert rec["use_count"] == 1
    assert rec["last_used_at"] == _iso(used)


def test_bump_skill_use_revives_stale_skill(tmp_path: Path) -> None:
    from app.services.skill_curator import bump_skill_use, load_skill_usage, save_skill_usage

    save_skill_usage(
        tmp_path,
        {
            "deploy": {
                "created_by": "agent",
                "created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                "last_used_at": None,
                "use_count": 0,
                "view_count": 0,
                "state": "stale",
                "pinned": False,
                "archived_at": None,
            }
        },
    )

    bump_skill_use(tmp_path, "deploy", kind="use", now=datetime(2026, 3, 1, tzinfo=timezone.utc))

    rec = load_skill_usage(tmp_path)["deploy"]
    assert rec["state"] == "active"
    assert rec["use_count"] == 1


def test_bump_skill_use_does_not_revive_archived_skill(tmp_path: Path) -> None:
    from app.services.skill_curator import bump_skill_use, load_skill_usage, save_skill_usage

    save_skill_usage(
        tmp_path,
        {
            "deploy": {
                "created_by": "agent",
                "created_at": _iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                "last_used_at": None,
                "use_count": 0,
                "view_count": 0,
                "state": "archived",
                "pinned": False,
                "archived_at": _iso(datetime(2026, 4, 1, tzinfo=timezone.utc)),
            }
        },
    )

    bump_skill_use(tmp_path, "deploy", kind="use", now=datetime(2026, 5, 1, tzinfo=timezone.utc))

    rec = load_skill_usage(tmp_path)["deploy"]
    # Archived skills are off-disk; a stray bump must not silently revive them.
    assert rec["state"] == "archived"


# ---------------------------------------------------------------------------
# set_skill_pinned
# ---------------------------------------------------------------------------


def test_set_skill_pinned(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, mark_skill_created, set_skill_pinned

    mark_skill_created(tmp_path, "deploy", created_by="agent", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    set_skill_pinned(tmp_path, "deploy", True)
    assert load_skill_usage(tmp_path)["deploy"]["pinned"] is True

    set_skill_pinned(tmp_path, "deploy", False)
    assert load_skill_usage(tmp_path)["deploy"]["pinned"] is False


# ---------------------------------------------------------------------------
# apply_skill_auto_transitions — pure function
# ---------------------------------------------------------------------------


def _usage_record(
    *,
    created_by: str = "agent",
    created_at: datetime,
    last_used_at: datetime | None = None,
    state: str = "active",
    pinned: bool = False,
    use_count: int = 0,
    view_count: int = 0,
) -> dict:
    return {
        "created_by": created_by,
        "created_at": _iso(created_at),
        "last_used_at": _iso(last_used_at) if last_used_at else None,
        "use_count": use_count,
        "view_count": view_count,
        "state": state,
        "pinned": pinned,
        "archived_at": None,
    }


def test_auto_transition_active_to_stale_after_30_days(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    usage = {
        "deploy": _usage_record(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=40),
            state="active",
        )
    }

    transitions = apply_skill_auto_transitions(usage, now=now)

    assert len(transitions) == 1
    assert transitions[0]["slug"] == "deploy"
    assert transitions[0]["from"] == "active"
    assert transitions[0]["to"] == "stale"


def test_auto_transition_to_archived_after_90_days(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    usage = {
        "deploy": _usage_record(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=100),
            state="stale",
        )
    }

    transitions = apply_skill_auto_transitions(usage, now=now)

    assert len(transitions) == 1
    assert transitions[0]["to"] == "archived"


def test_auto_transition_use_counters_delay_stale_transition(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    usage = {
        "deploy": _usage_record(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=45),
            state="active",
            use_count=10,
        )
    }

    assert apply_skill_auto_transitions(usage, now=now) == []


def test_auto_transition_high_use_stale_skill_is_not_archived_by_recency_alone(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    usage = {
        "deploy": _usage_record(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=100),
            state="stale",
            use_count=20,
            view_count=4,
        )
    }

    assert apply_skill_auto_transitions(usage, now=now) == []


def test_auto_transition_uses_created_at_when_never_used(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    # Created 40 days ago, never used → anchored on created_at → stale.
    usage = {
        "fresh": _usage_record(
            created_at=now - timedelta(days=40),
            last_used_at=None,
            state="active",
        )
    }

    transitions = apply_skill_auto_transitions(usage, now=now)
    assert len(transitions) == 1
    assert transitions[0]["to"] == "stale"


def test_auto_transition_recent_skill_is_untouched(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    usage = {
        "deploy": _usage_record(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=5),
            state="active",
        )
    }

    assert apply_skill_auto_transitions(usage, now=now) == []


def test_auto_transition_skips_pinned_skill(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    usage = {
        "deploy": _usage_record(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=120),
            state="active",
            pinned=True,
        )
    }

    assert apply_skill_auto_transitions(usage, now=now) == []


def test_auto_transition_skips_non_agent_skills(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    usage = {
        "bundled": _usage_record(
            created_by="bundled",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=120),
            state="active",
        ),
        "template": _usage_record(
            created_by="template",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=120),
            state="active",
        ),
    }

    assert apply_skill_auto_transitions(usage, now=now) == []


def test_auto_transition_already_archived_is_terminal(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    usage = {
        "deploy": _usage_record(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=200),
            state="archived",
        )
    }

    assert apply_skill_auto_transitions(usage, now=now) == []


def test_auto_transition_does_not_mutate_input(tmp_path: Path) -> None:
    from app.services.skill_curator import apply_skill_auto_transitions

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    usage = {
        "deploy": _usage_record(
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_used_at=now - timedelta(days=100),
            state="active",
        )
    }
    snapshot = dict(usage["deploy"])

    apply_skill_auto_transitions(usage, now=now)

    # Pure function: input dict untouched, mutation is the orchestrator's job.
    assert usage["deploy"] == snapshot


# ---------------------------------------------------------------------------
# archive_skill — real directory move
# ---------------------------------------------------------------------------


def test_archive_skill_moves_directory_to_archive(tmp_path: Path) -> None:
    from app.services.skill_curator import archive_skill

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "deploy")

    ok = archive_skill(tmp_path, "deploy")

    assert ok is True
    assert not (skills_dir / "deploy").exists()
    archived = skills_dir / ".archive" / "deploy"
    assert archived.exists()
    # Content is preserved — never deleted.
    assert "deploy" in (archived / "SKILL.md").read_text(encoding="utf-8")


def test_archive_skill_collision_appends_timestamp(tmp_path: Path) -> None:
    from app.services.skill_curator import archive_skill

    skills_dir = tmp_path / "skills"
    # Pre-existing archive entry occupies the slot.
    (skills_dir / ".archive" / "deploy").mkdir(parents=True, exist_ok=True)
    (skills_dir / ".archive" / "deploy" / "SKILL.md").write_text("old", encoding="utf-8")
    _write_skill(skills_dir, "deploy")

    ok = archive_skill(tmp_path, "deploy")

    assert ok is True
    # Original archive entry survives.
    assert (skills_dir / ".archive" / "deploy" / "SKILL.md").read_text(encoding="utf-8") == "old"
    # New one lands under a timestamped sibling.
    siblings = [p for p in (skills_dir / ".archive").iterdir() if p.name.startswith("deploy-")]
    assert len(siblings) == 1
    assert (siblings[0] / "SKILL.md").exists()


def test_archive_skill_missing_directory_returns_false(tmp_path: Path) -> None:
    from app.services.skill_curator import archive_skill

    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    assert archive_skill(tmp_path, "ghost") is False


# ---------------------------------------------------------------------------
# run_skill_curator_pass — orchestration end-to-end
# ---------------------------------------------------------------------------


def test_run_curator_pass_marks_stale_and_archives(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, run_skill_curator_pass, save_skill_usage

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "stale-one")
    _write_skill(skills_dir, "archive-one")
    _write_skill(skills_dir, "fresh-one")

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    save_skill_usage(
        tmp_path,
        {
            "stale-one": _usage_record(
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_used_at=now - timedelta(days=40),
                state="active",
            ),
            "archive-one": _usage_record(
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_used_at=now - timedelta(days=120),
                state="stale",
            ),
            "fresh-one": _usage_record(
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_used_at=now - timedelta(days=2),
                state="active",
            ),
        },
    )

    report = run_skill_curator_pass(tmp_path, now=now)

    assert report["scanned"] == 3
    assert report["to_stale"] == ["stale-one"]
    assert report["to_archived"] == ["archive-one"]

    usage = load_skill_usage(tmp_path)
    assert usage["stale-one"]["state"] == "stale"
    assert usage["archive-one"]["state"] == "archived"
    assert usage["archive-one"]["archived_at"] is not None
    assert usage["fresh-one"]["state"] == "active"

    # Archived skill directory physically moved.
    assert not (skills_dir / "archive-one").exists()
    assert (skills_dir / ".archive" / "archive-one").exists()
    # Stale skill stays on disk (only flagged).
    assert (skills_dir / "stale-one").exists()


def test_run_curator_pass_writes_audit_events(tmp_path: Path) -> None:
    from app.services.skill_curator import run_skill_curator_pass, save_skill_usage

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "archive-one")

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    save_skill_usage(
        tmp_path,
        {
            "archive-one": _usage_record(
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_used_at=now - timedelta(days=120),
                state="active",
            ),
        },
    )

    run_skill_curator_pass(tmp_path, now=now)

    review = (tmp_path / "evolution" / "skill_review.md").read_text(encoding="utf-8")
    assert "archive-one" in review
    assert "archived" in review


def test_run_curator_pass_respects_pinned_and_bundled(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, run_skill_curator_pass, save_skill_usage

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "pinned-one")
    _write_skill(skills_dir, "bundled-one")

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    save_skill_usage(
        tmp_path,
        {
            "pinned-one": _usage_record(
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_used_at=now - timedelta(days=200),
                state="active",
                pinned=True,
            ),
            "bundled-one": _usage_record(
                created_by="bundled",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_used_at=now - timedelta(days=200),
                state="active",
            ),
        },
    )

    report = run_skill_curator_pass(tmp_path, now=now)

    assert report["to_stale"] == []
    assert report["to_archived"] == []
    usage = load_skill_usage(tmp_path)
    assert usage["pinned-one"]["state"] == "active"
    assert usage["bundled-one"]["state"] == "active"
    # Both directories untouched.
    assert (skills_dir / "pinned-one").exists()
    assert (skills_dir / "bundled-one").exists()


def test_run_curator_pass_reports_revived(tmp_path: Path) -> None:
    """A skill bumped back to active after being stale is reported as revived."""
    from app.services.skill_curator import load_skill_usage, run_skill_curator_pass, save_skill_usage

    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "revived-one")

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # state=stale but recently used → the pass should pull it back to active.
    save_skill_usage(
        tmp_path,
        {
            "revived-one": _usage_record(
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_used_at=now - timedelta(days=2),
                state="stale",
            ),
        },
    )

    report = run_skill_curator_pass(tmp_path, now=now)

    assert report["revived"] == ["revived-one"]
    assert load_skill_usage(tmp_path)["revived-one"]["state"] == "active"


def test_run_curator_pass_empty_workspace(tmp_path: Path) -> None:
    from app.services.skill_curator import run_skill_curator_pass

    report = run_skill_curator_pass(tmp_path, now=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert report == {"scanned": 0, "to_stale": [], "to_archived": [], "revived": []}


def test_bump_does_not_label_unknown_skill_as_agent(tmp_path: Path) -> None:
    """A skill bumped without prior provenance (e.g. a bundled/default skill the
    agent merely loaded) must NOT be recorded as agent-authored — otherwise the
    curator would treat default skills as disposable and archive them."""
    from app.services.skill_curator import bump_skill_use, load_skill_usage

    bump_skill_use(tmp_path, "bundled-default", kind="use")

    rec = load_skill_usage(tmp_path)["bundled-default"]
    assert rec["created_by"] != "agent"


def test_curator_pass_does_not_archive_merely_loaded_default_skill(tmp_path: Path) -> None:
    """End-to-end provenance guard: a default skill that was only loaded (bumped)
    but never created via save_skill must survive a curator pass even when
    long-dormant — its directory stays put and its state never goes archived."""
    from app.services.skill_curator import bump_skill_use, load_skill_usage, run_skill_curator_pass

    skill_dir = tmp_path / "skills" / "default-helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# default helper\n", encoding="utf-8")

    long_ago = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bump_skill_use(tmp_path, "default-helper", kind="use", now=long_ago)

    report = run_skill_curator_pass(tmp_path, now=long_ago + timedelta(days=200))

    assert "default-helper" not in report["to_archived"]
    assert "default-helper" not in report["to_stale"]
    assert (skill_dir / "SKILL.md").exists()
    assert load_skill_usage(tmp_path)["default-helper"]["state"] != "archived"
