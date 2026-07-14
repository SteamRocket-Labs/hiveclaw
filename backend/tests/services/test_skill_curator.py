"""Model-owned Skill lifecycle review with platform-owned safety gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _iso(value: datetime) -> str:
    return value.isoformat()


def _write_skill(workspace: Path, slug: str, *, body: str = "body") -> Path:
    path = workspace / "skills" / slug / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nname: "{slug}"\ndescription: "test skill"\n---\n# {slug}\n{body}\n',
        encoding="utf-8",
    )
    return path


def _record(
    now: datetime,
    *,
    created_by: str = "agent",
    state: str = "active",
    pinned: bool = False,
    age_days: int = 200,
) -> dict:
    return {
        "created_by": created_by,
        "created_at": _iso(now - timedelta(days=age_days)),
        "last_used_at": _iso(now - timedelta(days=age_days)),
        "use_count": 0,
        "view_count": 0,
        "state": state,
        "pinned": pinned,
        "archived_at": None,
    }


def test_usage_store_roundtrip_and_legacy_migration(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, save_skill_usage

    legacy = tmp_path / "skills" / ".usage.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"deploy": {"created_by": "agent", "use_count": 2, "state": "active"}}')

    assert load_skill_usage(tmp_path)["deploy"]["use_count"] == 2
    assert not legacy.exists()

    save_skill_usage(tmp_path, {"research": {"created_by": "agent", "use_count": 7, "state": "active"}})
    assert load_skill_usage(tmp_path)["research"]["use_count"] == 7


def test_mark_use_and_pin_only_record_facts(tmp_path: Path) -> None:
    from app.services.skill_curator import bump_skill_use, load_skill_usage, mark_skill_created, set_skill_pinned

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mark_skill_created(tmp_path, "deploy", now=now)
    bump_skill_use(tmp_path, "deploy", kind="use", now=now + timedelta(days=1))
    bump_skill_use(tmp_path, "deploy", kind="view", now=now + timedelta(days=2))
    set_skill_pinned(tmp_path, "deploy", True)

    record = load_skill_usage(tmp_path)["deploy"]
    assert record["use_count"] == 1
    assert record["view_count"] == 1
    assert record["last_used_at"] == _iso(now + timedelta(days=1))
    assert record["pinned"] is True


def test_usage_event_does_not_mechanically_revive_stale_skill(tmp_path: Path) -> None:
    from app.services.skill_curator import bump_skill_use, load_skill_usage, save_skill_usage

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    save_skill_usage(tmp_path, {"deploy": _record(now, state="stale")})

    bump_skill_use(tmp_path, "deploy", kind="use", now=now)

    assert load_skill_usage(tmp_path)["deploy"]["state"] == "stale"


def test_evidence_only_compatibility_pass_never_mutates_old_skill(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, run_skill_curator_pass, save_skill_usage

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    skill = _write_skill(tmp_path, "old-skill")
    save_skill_usage(tmp_path, {"old-skill": _record(now)})

    report = run_skill_curator_pass(tmp_path, now=now)

    assert report["status"] == "model_review_required"
    assert report["scanned"] == 1
    assert skill.exists()
    assert load_skill_usage(tmp_path)["old-skill"]["state"] == "active"


def test_lifecycle_evidence_contains_every_skill_and_decisive_tail(tmp_path: Path) -> None:
    from app.services.skill_curator import build_skill_lifecycle_review_evidence, save_skill_usage

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    tail = "SKILL_LIFECYCLE_DECISIVE_TAIL"
    _write_skill(tmp_path, "active-skill", body=("evidence " * 500) + tail)
    archived = tmp_path / "skills" / ".archive" / "archived-skill" / "SKILL.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("archived full content", encoding="utf-8")
    save_skill_usage(
        tmp_path,
        {
            "active-skill": _record(now),
            "archived-skill": _record(now, state="archived"),
            "telemetry-only": _record(now),
        },
    )

    evidence = build_skill_lifecycle_review_evidence(tmp_path, now=now)
    by_slug = {item["slug"]: item for item in evidence["skills"]}

    assert set(by_slug) == {"active-skill", "archived-skill", "telemetry-only"}
    assert tail in by_slug["active-skill"]["skill_markdown"]
    assert by_slug["archived-skill"]["skill_markdown"] == "archived full content"
    assert by_slug["telemetry-only"]["skill_markdown"] == ""


@pytest.mark.asyncio
async def test_model_keep_active_overrides_age_observation(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, review_skill_lifecycle, save_skill_usage

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _write_skill(tmp_path, "old-skill")
    save_skill_usage(tmp_path, {"old-skill": _record(now)})

    async def reviewer(evidence):
        assert evidence["skills"][0]["usage"]["last_used_at"] == _iso(now - timedelta(days=200))
        return {"decisions": [{"slug": "old-skill", "decision": "keep_active", "reason": "Still essential."}]}

    report = await review_skill_lifecycle(tmp_path, reviewer=reviewer, now=now)

    assert report["status"] == "reviewed"
    assert report["applied"] == [{"slug": "old-skill", "decision": "keep_active"}]
    assert load_skill_usage(tmp_path)["old-skill"]["state"] == "active"


@pytest.mark.asyncio
async def test_model_archive_decision_moves_skill_reversibly(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, review_skill_lifecycle, save_skill_usage

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _write_skill(tmp_path, "retire-me", body="preserve me")
    save_skill_usage(tmp_path, {"retire-me": _record(now)})

    async def reviewer(_evidence):
        return {"decisions": [{"slug": "retire-me", "decision": "archive", "reason": "Superseded."}]}

    report = await review_skill_lifecycle(tmp_path, reviewer=reviewer, now=now)

    assert report["applied"] == [{"slug": "retire-me", "decision": "archive"}]
    assert not (tmp_path / "skills" / "retire-me").exists()
    assert "preserve me" in (tmp_path / "skills" / ".archive" / "retire-me" / "SKILL.md").read_text()
    assert load_skill_usage(tmp_path)["retire-me"]["state"] == "archived"
    audit = (tmp_path / "evolution" / "skill_curator_reviews.jsonl").read_text()
    assert '"semantic_authority": "model_review"' in audit


@pytest.mark.asyncio
async def test_model_restore_decision_reverses_archive(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, review_skill_lifecycle, save_skill_usage

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    archived = tmp_path / "skills" / ".archive" / "restore-me" / "SKILL.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("restored content", encoding="utf-8")
    save_skill_usage(tmp_path, {"restore-me": _record(now, state="archived")})

    async def reviewer(_evidence):
        return {"decisions": [{"slug": "restore-me", "decision": "restore", "reason": "Needed again."}]}

    report = await review_skill_lifecycle(tmp_path, reviewer=reviewer, now=now)

    assert report["applied"] == [{"slug": "restore-me", "decision": "restore"}]
    assert (tmp_path / "skills" / "restore-me" / "SKILL.md").read_text() == "restored content"
    assert load_skill_usage(tmp_path)["restore-me"]["state"] == "active"


@pytest.mark.asyncio
async def test_reviewer_failure_preserves_all_semantic_state(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, review_skill_lifecycle, save_skill_usage

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    skill = _write_skill(tmp_path, "keep-me")
    save_skill_usage(tmp_path, {"keep-me": _record(now)})

    async def reviewer(_evidence):
        raise RuntimeError("model unavailable")

    report = await review_skill_lifecycle(tmp_path, reviewer=reviewer, now=now)

    assert report["status"] == "held"
    assert report["reason"] == "reviewer_failed"
    assert skill.exists()
    assert load_skill_usage(tmp_path)["keep-me"]["state"] == "active"


@pytest.mark.asyncio
async def test_platform_authority_blocks_pinned_and_non_agent_mutation(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, review_skill_lifecycle, save_skill_usage

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _write_skill(tmp_path, "pinned")
    _write_skill(tmp_path, "bundled")
    save_skill_usage(
        tmp_path,
        {
            "pinned": _record(now, pinned=True),
            "bundled": _record(now, created_by="bundled"),
        },
    )

    async def reviewer(_evidence):
        return {
            "decisions": [
                {"slug": "pinned", "decision": "archive", "reason": "Model proposal."},
                {"slug": "bundled", "decision": "mark_stale", "reason": "Model proposal."},
            ]
        }

    report = await review_skill_lifecycle(tmp_path, reviewer=reviewer, now=now)

    assert {item["slug"] for item in report["blocked"]} == {"pinned", "bundled"}
    assert (tmp_path / "skills" / "pinned").exists()
    assert (tmp_path / "skills" / "bundled").exists()
    usage = load_skill_usage(tmp_path)
    assert usage["pinned"]["state"] == usage["bundled"]["state"] == "active"
