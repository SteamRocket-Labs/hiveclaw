"""Tests for managed channel credential boundary detection."""

from __future__ import annotations

import pytest

from app.services.managed_capability_guard import (
    MANAGED_CREDENTIAL_PREFIXES,
    detect_managed_credential_command,
    detect_managed_credential_guidance,
)


@pytest.mark.parametrize(
    ("prefix", "family"),
    [
        ("TAVILY", "web_research"),
        ("EXA", "web_research"),
        ("FIRECRAWL", "web_research"),
        ("XCRAWL", "web_research"),
        ("OPENAI", "llm_provider"),
        ("ANTHROPIC", "llm_provider"),
        ("GEMINI", "llm_provider"),
        ("GOOGLE", "platform_provider"),
        ("SMITHERY", "mcp_provider"),
        ("MODELSCOPE", "mcp_provider"),
    ],
)
def test_detects_managed_provider_prefix_env_probes(prefix: str, family: str) -> None:
    command = f"printenv | grep '^{prefix}_'"

    finding = detect_managed_credential_command(command)

    assert finding is not None
    assert finding.family == family
    assert finding.variable == f"{prefix}_*"


@pytest.mark.parametrize("prefix,family", sorted(MANAGED_CREDENTIAL_PREFIXES.items()))
def test_detects_managed_integration_prefix_env_probes(prefix: str, family: str) -> None:
    command = f"env | grep -E '^{prefix}_'"

    finding = detect_managed_credential_command(command)

    assert finding is not None
    assert finding.family == family
    assert finding.variable == f"{prefix}_*"
    assert finding.reason == "managed_credential_env_probe"


@pytest.mark.parametrize("prefix,family", sorted(MANAGED_CREDENTIAL_PREFIXES.items()))
def test_detects_managed_integration_secret_export_guidance(prefix: str, family: str) -> None:
    content = f"export {prefix}_APP_SECRET=tenant-owned-secret"

    findings = detect_managed_credential_guidance(content)

    assert len(findings) == 1
    assert findings[0].family == family
    assert findings[0].variable == f"{prefix}_APP_SECRET"


def test_dedicated_tool_examples_are_not_treated_as_env_guidance() -> None:
    content = """
feishu_sheet_info(spreadsheet_url="https://example.feishu.cn/sheets/shtcxxxx")
send_email(to="ops@example.com", subject="hello")
"""

    assert detect_managed_credential_guidance(content) == ()
