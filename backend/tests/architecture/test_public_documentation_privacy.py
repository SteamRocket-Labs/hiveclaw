from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = ROOT / "docs/railway-production-runbook.md"
PRIVATE_PATHS = (
    ".claude/agent-memory/",
    ".serena/memories/",
    ".ultra/.runtime/",
    ".ultra/runtime/",
    ".ultra/debug/",
    ".ultra/memory/",
    ".ultra/telemetry/",
    ".ultra/compact-snapshot.md",
    ".ultra/state.db-shm",
    ".ultra/state.db-wal",
    ".ultra/tasks/tasks.json",
    "tmp/reports/",
    "bp-kingdee/",
)


def _check_runbook(text: str) -> None:
    for variable in ("RAILWAY_PROJECT_ID", "HIVE_BACKEND_URL", "HIVE_FRONTEND_URL"):
        assert f"${{{variable}:?" in text, f"missing local configuration check: {variable}"
    assert not re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", text, re.I)
    assert not re.search(r"[a-z0-9-]+\.up\.railway\.app", text, re.I)


def test_public_runbook_requires_local_targets_and_rejects_embedded_identifiers() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    _check_runbook(text)
    with pytest.raises(AssertionError):
        _check_runbook(text + "\nPROJECT_ID=11111111-2222-4333-8444-555555555555\n")
    with pytest.raises(AssertionError):
        _check_runbook(text + "\nhttps://example-production.up.railway.app\n")


def test_private_runtime_artifacts_stay_untracked_and_ignored() -> None:
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--", *PRIVATE_PATHS], cwd=ROOT)
    assert not tracked, "private runtime artifacts must not be Git-tracked"
    examples = [path + "private-record.json" if path.endswith("/") else path for path in PRIVATE_PATHS]
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=ROOT,
        input="\n".join(examples) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert set(ignored.stdout.splitlines()) == set(examples)
