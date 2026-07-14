from __future__ import annotations

import json


def test_retire_onlyoffice_metadata_is_dry_run_atomic_and_idempotent(tmp_path):
    from app.scripts.retire_onlyoffice_metadata import retire_onlyoffice_metadata

    manifest = tmp_path / "agent-1" / ".office_meta" / "digest" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "path": "workspace/demo.docx",
                "active_editor_session": {"session_id": "secret-session", "user_id": "secret-user"},
                "current_version": 3,
            }
        ),
        encoding="utf-8",
    )
    invalid = tmp_path / "agent-2" / ".office_meta" / "digest" / "manifest.json"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("not-json", encoding="utf-8")

    dry_run = retire_onlyoffice_metadata(tmp_path, apply=False)

    assert dry_run == {"scanned": 2, "needs_update": 1, "updated": 0, "errors": 1}
    assert "active_editor_session" in manifest.read_text(encoding="utf-8")

    applied = retire_onlyoffice_metadata(tmp_path, apply=True)

    assert applied == {"scanned": 2, "needs_update": 1, "updated": 1, "errors": 1}
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert "active_editor_session" not in payload
    assert payload["current_version"] == 3
    assert invalid.read_text(encoding="utf-8") == "not-json"

    repeated = retire_onlyoffice_metadata(tmp_path, apply=True)
    assert repeated == {"scanned": 2, "needs_update": 0, "updated": 0, "errors": 1}
