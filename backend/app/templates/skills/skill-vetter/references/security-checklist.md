# Skill Vetter Security Checklist

Use this checklist for every third-party skill before installation.

## Required Evidence

- Source URL and exact package identifier.
- Files inspected, including `SKILL.md`, `scripts/`, templates, assets, and install hooks.
- Tool permissions requested by frontmatter.
- Shell commands or network calls introduced by the skill.
- Risk rating and installation decision.

## Immediate Reject Flags

- Reads credential locations such as `.env`, `~/.ssh`, `~/.aws`, browser cookies, or token stores.
- Sends local files, credentials, or workspace content to unknown endpoints.
- Uses obfuscated execution, dynamic `eval`, base64 decode plus execution, or hidden installers.
- Writes outside the workspace or modifies agent identity/memory files directly.
- Requires root privileges or escalates system permissions.

## Risk Levels

| Level | Meaning |
| --- | --- |
| LOW | Read-only or scoped workspace-only behavior from a trusted source |
| MEDIUM | Writes workspace files or calls known APIs with clear scope |
| HIGH | Handles credentials, external delivery, payments, production systems, or broad file access |
| EXTREME | Any reject flag, credential theft, hidden execution, or data exfiltration |

`EXTREME` is not user-overridable.
