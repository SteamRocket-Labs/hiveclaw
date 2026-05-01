# Find Skills Source Vetting

Use this reference before recommending an external skill. The purpose is to
avoid catalog bloat, duplicate installs, and unsafe executable guidance.

## Discovery Order

1. Check the currently available skills first.
2. Search the skill catalog with the narrowest capability keyword.
3. Prefer high-install, actively maintained, trusted-source candidates.
4. Fetch the candidate `SKILL.md` and any executable resources before ranking.
5. Hand off to Skill Vetter before installation.

## Ranking Signals

| Signal | Strong | Weak |
| --- | --- | --- |
| Install count | 10K+ installs | Fewer than 1K installs |
| Maintainer | Known vendor or widely used OSS org | New or anonymous source |
| Scope | One clear capability | Broad system access |
| Files | Mostly instructions/templates | Hidden scripts or installers |
| Maintenance | Updated within 6 months | Stale or archived |

## Reject Conditions

- The candidate duplicates an installed or built-in skill.
- The skill requests credentials it should not own.
- The source cannot be fetched completely enough for review.
- The install path or package name is ambiguous.
- The candidate is itself a skill finder or installer loop.
