from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def test_pytest_process_uses_ephemeral_home_and_agent_data_root() -> None:
    hermetic_root = Path(os.environ["HIVE_TEST_HERMETIC_ROOT"]).resolve()
    home = Path(os.environ["HOME"]).resolve()
    agent_data = Path(os.environ["AGENT_DATA_DIR"]).resolve()

    assert home == hermetic_root / "home"
    assert agent_data == hermetic_root / "agents"
    assert home.is_dir()
    assert agent_data.is_dir()
    assert home.is_relative_to(hermetic_root)
    assert agent_data.is_relative_to(hermetic_root)


def test_cached_application_settings_consume_hermetic_agent_data_root() -> None:
    from app.config import get_settings

    expected = Path(os.environ["AGENT_DATA_DIR"]).resolve()

    assert Path(get_settings().AGENT_DATA_DIR).resolve() == expected


def test_ci_uses_the_same_full_backend_suite_command() -> None:
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "harness-ci.yml"
    content = workflow.read_text(encoding="utf-8")

    assert re.search(
        r"(?m)^\s*run:\s*pytest tests -vv --tb=short --maxfail=0 "
        r"--failure-receipts=pytest-failures.jsonl --junitxml=pytest-results.xml\s*$",
        content,
    )


def _run_worker_failure(tmp_path: Path, *, linked_log: Path | None = None, nested_logs: bool = False):
    case = tmp_path / "failure-case"
    case.mkdir()
    shutil.copyfile(Path(__file__).with_name("conftest.py"), case / "conftest.py")
    (case / "test_failure.py").write_text(
        """import logging
import os
import sys

def test_failed_worker(tmp_path, request):
    worker_log = tmp_path / 'worker.log'
    if os.environ['FAILURE_TEST_LINK']:
        worker_log.symlink_to(os.environ['FAILURE_TEST_LINK'])
    else:
        worker_log.write_text(os.environ['FAILURE_TEST_LOG'])
    if os.environ['FAILURE_TEST_NESTED']:
        for name in ('worker-1', 'worker-2'):
            directory = tmp_path / name
            directory.mkdir()
            (directory / 'worker.log').write_text(name + '\\n' + os.environ['FAILURE_TEST_LOG'])
        if os.environ['FAILURE_TEST_LINK']:
            (tmp_path / 'linked-worker').symlink_to(os.path.dirname(os.environ['FAILURE_TEST_LINK']))
    print('synthetic worker stdout')
    print('synthetic worker stderr', file=sys.stderr)
    logging.warning('synthetic worker cause')
    request.node.add_report_section('call', 'Synthetic target inventory', 'target-run-evidence')
    assert False, 'synthetic worker failed'
"""
    )
    body = "first-diagnostic-cause\n" + "native worker output\n" * 700 + "final-worker-event\n"
    backend = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(backend), os.environ.get("PYTHONPATH", "")]),
        "DATABASE_URL": "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:1/synthetic",
        "REDIS_URL": "redis://127.0.0.1:1/15",
        "FAILURE_TEST_LOG": body,
        "FAILURE_TEST_LINK": str(linked_log) if linked_log is not None else "",
        "FAILURE_TEST_NESTED": "1" if nested_logs else "",
    }
    receipts = case / "failures.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "test_failure.py",
            "-q",
            "--tb=short",
            "--maxfail=0",
            "--basetemp",
            str(case / "test-tmp"),
            "--failure-receipts",
            str(receipts),
        ],
        cwd=case,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    rows = [json.loads(line) for line in receipts.read_text().splitlines()]
    assert len(rows) == 1
    return case, rows[0], body.encode()


def test_failure_receipt_retains_captured_output_and_complete_worker_log(tmp_path: Path) -> None:
    case, receipt, body = _run_worker_failure(tmp_path)
    sections = {section["name"]: section["content"] for section in receipt.get("sections", [])}
    assert "synthetic worker stdout" in sections.get("Captured stdout call", "")
    assert "synthetic worker stderr" in sections.get("Captured stderr call", "")
    assert "synthetic worker cause" in sections.get("Captured log call", "")
    assert any("target-run-evidence" in content for content in sections.values())
    files = receipt.get("files", [])
    assert len(files) == 1 and files[0]["name"] == "worker.log"
    archive = case / files[0]["archive"]
    assert archive.is_relative_to(case)
    assert gzip.decompress(archive.read_bytes()) == body
    assert files[0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert files[0]["bytes"] == len(body) > 5000


def test_failure_receipt_does_not_follow_worker_log_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside.log"
    canary = "outside-log-content-must-stay-out"
    outside.write_text(canary)
    case, receipt, _body = _run_worker_failure(tmp_path, linked_log=outside)
    assert receipt.get("files", []) == []
    assert not (case / "pytest-failure-files").exists()
    assert canary not in json.dumps(receipt)
    assert outside.read_text() == canary


def test_phase_timings_survive_a_failed_suite_without_a_final_summary(tmp_path: Path) -> None:
    case, receipt, _body = _run_worker_failure(tmp_path)
    timings = [json.loads(line) for line in (case / "pytest-timings.jsonl").read_text().splitlines()]

    assert [row["when"] for row in timings] == ["setup", "call", "teardown"]
    assert [row["outcome"] for row in timings] == ["passed", "failed", "passed"]
    assert all(row["nodeid"] == receipt["nodeid"] for row in timings)
    assert all(row["duration"] >= 0 and row["stop"] >= row["start"] for row in timings)


def test_failure_receipt_retains_nested_worker_logs_without_following_linked_directories(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_log = outside / "worker.log"
    canary = "outside-nested-worker-log-must-stay-out"
    linked_log.write_text(canary)
    case, receipt, body = _run_worker_failure(tmp_path, linked_log=linked_log, nested_logs=True)

    files = receipt.get("files", [])
    assert {item["name"] for item in files} == {"worker-1/worker.log", "worker-2/worker.log"}
    assert len(files) == 2
    for item in files:
        expected = item["name"].split("/")[0].encode() + b"\n" + body
        assert gzip.decompress((case / item["archive"]).read_bytes()) == expected
        assert item["sha256"] == hashlib.sha256(expected).hexdigest()
        assert item["bytes"] == len(expected) > 5000
    assert canary not in json.dumps(receipt)
    assert linked_log.read_text() == canary
