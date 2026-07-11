from __future__ import annotations

import os
from pathlib import Path
import re


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

    assert re.search(r"(?m)^\s*run:\s*pytest tests -q\s*$", content)
