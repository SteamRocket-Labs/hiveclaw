from __future__ import annotations

from pathlib import Path


def test_heartbeat_no_longer_contains_legacy_evolution_writeback_symbols() -> None:
    source = Path("app/services/heartbeat.py").read_text(encoding="utf-8")

    assert "_update_evolution_files" not in source
    assert "_auto_seed_evolution" not in source
    assert "_validate_bootstrap_completion" not in source
