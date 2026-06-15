"""Tests for kernel contract dataclasses.

Mostly defensive — these dataclasses are pure data, but we lock down field
defaults that other layers rely on as sentinels (e.g. quota_message and the
new tenant_resolution_error sentinel introduced for P0-1b).
"""
from __future__ import annotations

import uuid

from app.kernel.contracts import RuntimeConfig


def test_runtime_config_defaults():
    cfg = RuntimeConfig(tenant_id=uuid.uuid4(), max_tool_rounds=200)
    assert cfg.quota_message is None
    assert cfg.execution_mode is None
    assert cfg.runtime_continuity_enabled is False
    assert cfg.skill_candidate_loop_enabled is False
    assert cfg.skill_distiller_behavior_report is None
    # P0-1b: new sentinel must default to None so existing successful paths
    # behave unchanged. Only invoker fallback paths set this to a string.
    assert cfg.tenant_resolution_error is None


def test_runtime_config_tenant_resolution_error_can_carry_message():
    cfg = RuntimeConfig(
        tenant_id=None,
        max_tool_rounds=200,
        tenant_resolution_error="Agent abc not found",
    )
    assert cfg.tenant_id is None
    assert cfg.tenant_resolution_error == "Agent abc not found"


def test_runtime_config_quota_message_and_tenant_error_are_independent():
    """Both sentinels can coexist; kernel checks them in order."""
    cfg = RuntimeConfig(
        tenant_id=None,
        max_tool_rounds=200,
        quota_message="Token quota exceeded",
        tenant_resolution_error="DB exception",
    )
    assert cfg.quota_message == "Token quota exceeded"
    assert cfg.tenant_resolution_error == "DB exception"
