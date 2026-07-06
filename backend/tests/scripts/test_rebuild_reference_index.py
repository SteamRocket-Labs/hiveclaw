from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4


def _write_explicit_overlay(tmp_path: Path, agent_id, *, entry_id: str = "ex-rebuild") -> str:
    source_ref = "t0://session/s1/segment/seg-1#seq=1..2"
    overlay = tmp_path / str(agent_id) / "memory" / "explicit"
    (overlay / "entries").mkdir(parents=True, exist_ok=True)
    (overlay / "manifest.jsonl").write_text(
        json.dumps(
            {
                "id": entry_id,
                "status": "active",
                "category": "constraint",
                "target_hint": "worker",
                "sensitivity": "PL1_public",
                "created_at": "2026-07-05T00:00:00+00:00",
                "source_refs": source_ref,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (overlay / "entries" / f"{entry_id}.md").write_text(
        "<normalized_memory>用户要求重建派生索引后 source_refs 不能丢。</normalized_memory>",
        encoding="utf-8",
    )
    return source_ref


def test_rebuild_reference_indexes_dry_run_does_not_create_index(tmp_path: Path) -> None:
    from app.memory.reference_index import index_db_path
    from app.scripts.rebuild_reference_index import rebuild_reference_indexes

    agent_id = uuid4()
    _write_explicit_overlay(tmp_path, agent_id)

    report = rebuild_reference_indexes(data_root=tmp_path, agent_id=agent_id, apply=False)

    assert report["apply"] is False
    assert report["agents"] == [{"agent_id": str(agent_id), "status": "dry_run"}]
    assert not index_db_path(tmp_path, agent_id).exists()


def test_rebuild_reference_indexes_apply_restores_activation_keys_and_source_refs(tmp_path: Path) -> None:
    from app.memory.reference_index import index_db_path, query_activation_keys, rebuild_reference_index
    from app.scripts.rebuild_reference_index import rebuild_reference_indexes

    agent_id = uuid4()
    source_ref = _write_explicit_overlay(tmp_path, agent_id)
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)
    index_db_path(tmp_path, agent_id).unlink()

    report = rebuild_reference_indexes(data_root=tmp_path, agent_id=agent_id, apply=True)

    assert report["apply"] is True
    assert report["agents"][0]["status"] == "rebuilt"
    assert report["agents"][0]["activation_key_rows"] >= 1
    rows = query_activation_keys(
        agent_id=agent_id,
        data_root=tmp_path,
        scope="explicit_overlay",
        key_axis="category",
        key_value="constraint",
    )
    assert rows
    assert {row["source_ref"] for row in rows} == {source_ref}
