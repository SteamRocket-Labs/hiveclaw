from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]


T3_NATIVE_CHAIN_FILES = [
    "app/memory/backend.py",
    "app/memory/retriever.py",
    "app/services/auto_dream.py",
    "app/services/heartbeat.py",
    "app/services/memory_service.py",
    "app/tools/handlers/memory.py",
]

PLATFORM_MEMORY_CONFIG_FILES = [
    "app/api/admin.py",
    "app/config.py",
    "app/models/tenant.py",
]

FORBIDDEN_T3_PROGRAM_MARKERS = [
    "Hindsight",
    "hindsight",
    "sync_t3_to_hindsight",
    "HINDSIGHT",
    "OpenViking",
    "openviking",
    "extract_to_viking",
    "viking://conversations",
]

FORBIDDEN_MEMORY_BACKEND_MARKERS = [
    "Hindsight",
    "hindsight",
    "HINDSIGHT",
    "MEMORY_BACKEND",
    "memory_backend must be one of",
]


def _offending_markers(relative_path: str, markers: list[str]) -> list[str]:
    text = (BACKEND_ROOT / relative_path).read_text(encoding="utf-8")
    return [marker for marker in markers if marker in text]


def test_t3_native_chain_has_no_hardcoded_external_memory_programs() -> None:
    offenders: dict[str, list[str]] = {}
    for relative_path in T3_NATIVE_CHAIN_FILES:
        found = _offending_markers(relative_path, FORBIDDEN_T3_PROGRAM_MARKERS)
        if found:
            offenders[relative_path] = found

    assert offenders == {}


def test_platform_memory_backend_config_has_no_external_t3_program_switches() -> None:
    offenders: dict[str, list[str]] = {}
    for relative_path in PLATFORM_MEMORY_CONFIG_FILES:
        found = _offending_markers(relative_path, FORBIDDEN_MEMORY_BACKEND_MARKERS)
        if found:
            offenders[relative_path] = found

    assert offenders == {}
