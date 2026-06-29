"""Guards for platform-managed integration/provider credentials.

Hive is a public multi-tenant platform. Agent/channel credentials live in
database-backed channel/tool/model configuration, not in shell environment
variables that agents should inspect. This module centralizes detection so
governance, skill loading, and skill import checks stay aligned across all
managed integrations and provider-backed capabilities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


MANAGED_CREDENTIAL_PREFIXES: dict[str, str] = {
    "FEISHU": "feishu",
    "LARK": "feishu",
    "SLACK": "slack",
    "DISCORD": "discord",
    "DINGTALK": "dingtalk",
    "WECOM": "wecom",
    "WECHAT": "wechat",
    "TELEGRAM": "telegram",
    "MICROSOFT_TEAMS": "microsoft_teams",
    "TEAMS": "microsoft_teams",
    "EMAIL": "email",
    "SMTP": "email",
    "IMAP": "email",
    "TAVILY": "web_research",
    "EXA": "web_research",
    "FIRECRAWL": "web_research",
    "XCRAWL": "web_research",
    "OPENAI": "llm_provider",
    "ANTHROPIC": "llm_provider",
    "GEMINI": "llm_provider",
    "GOOGLE": "platform_provider",
    "SMITHERY": "mcp_provider",
    "MODELSCOPE": "mcp_provider",
}

_PREFIX_PATTERN = "|".join(sorted(MANAGED_CREDENTIAL_PREFIXES, key=len, reverse=True))
_SECRET_SUFFIX_PATTERN = (
    r"(?:APP_ID|APP_SECRET|CLIENT_ID|CLIENT_SECRET|BOT_TOKEN|TOKEN|SECRET|API_KEY|KEY|PASSWORD|AUTH_CODE)"
)
_MANAGED_ENV_VAR_RE = re.compile(
    rf"\b(?P<name>(?P<prefix>{_PREFIX_PATTERN})(?:_[A-Z0-9]+)*_{_SECRET_SUFFIX_PATTERN})\b",
    re.I,
)
_MANAGED_ENV_PREFIX_RE = re.compile(rf"(?P<prefix>{_PREFIX_PATTERN})_", re.I)
_ENV_PROBE_RE = re.compile(
    r"(\benv\s*\|\s*grep\b|\bprintenv\b|\bos\.getenv\s*\(|\bos\.environ\b|\bprocess\.env\b|\bcat\s+\.env\b)",
    re.I,
)
_EXPORT_OR_ASSIGN_RE = re.compile(r"^\s*(?:[`$>#]\s*)?(?:(?i:export)\s+)?[A-Z][A-Z0-9_]{2,}\s*=")


@dataclass(frozen=True, slots=True)
class ManagedCredentialFinding:
    family: str
    variable: str
    reason: str


def _family_for_prefix(prefix: str) -> str:
    return MANAGED_CREDENTIAL_PREFIXES[prefix.upper()]


def _find_managed_variable(text: str) -> ManagedCredentialFinding | None:
    text = text or ""
    match = _MANAGED_ENV_VAR_RE.search(text)
    if match:
        variable = match.group("name").upper()
        prefix = match.group("prefix")
    else:
        match = _MANAGED_ENV_PREFIX_RE.search(text)
        if not match:
            return None
        prefix = match.group("prefix")
        variable = f"{prefix.upper()}_*"
    return ManagedCredentialFinding(
        family=_family_for_prefix(prefix),
        variable=variable,
        reason="managed_credential_env_reference",
    )


def detect_managed_credential_command(command: str) -> ManagedCredentialFinding | None:
    """Return a finding when a shell command tries to inspect managed creds."""
    finding = _find_managed_variable(command)
    if not finding:
        return None
    if _ENV_PROBE_RE.search(command) or _EXPORT_OR_ASSIGN_RE.search(command):
        return ManagedCredentialFinding(
            family=finding.family,
            variable=finding.variable,
            reason="managed_credential_env_probe",
        )
    return None


def detect_managed_credential_guidance(content: str) -> tuple[ManagedCredentialFinding, ...]:
    """Scan skill text for instructions that route agents to shell/env creds."""
    findings: list[ManagedCredentialFinding] = []
    for raw_line in (content or "").splitlines():
        finding = _find_managed_variable(raw_line)
        if not finding:
            continue
        if _ENV_PROBE_RE.search(raw_line) or _EXPORT_OR_ASSIGN_RE.search(raw_line):
            findings.append(
                ManagedCredentialFinding(
                    family=finding.family,
                    variable=finding.variable,
                    reason="managed_credential_env_guidance",
                )
            )
    return tuple(findings)


def sanitize_managed_credential_guidance(content: str) -> str:
    """Remove unsafe managed-credential lines and prepend the platform rule."""
    findings = detect_managed_credential_guidance(content)
    if not findings:
        return content

    sanitized_lines: list[str] = []
    for line in content.splitlines():
        if detect_managed_credential_guidance(line):
            continue
        sanitized_lines.append(line)

    families = ", ".join(sorted({finding.family for finding in findings}))
    boundary = (
        "> Managed capability credential boundary: this skill contained instructions "
        "to inspect platform or channel credential env vars, so those lines were removed. "
        f"Affected integrations: {families}. Use the platform's dedicated tools and "
        "per-agent or tenant channel config instead; if config is missing, report the "
        "channel config gap instead of using shell commands.\n"
    )
    return boundary + "\n".join(sanitized_lines).lstrip()


def managed_credential_block_message(finding: ManagedCredentialFinding) -> str:
    return (
        "Tool 'run_command' blocked: managed channel credentials are stored in "
        "platform channel config, not shell environment variables visible to agents. "
        f"Use the dedicated tools for {finding.family} and report a channel config gap "
        "if those tools cannot authenticate."
    )
