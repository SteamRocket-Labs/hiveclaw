---
document_id: weekend-rc-2026-08-30-ui-cmd-003-production-reproduction
owner: Codex
status: immutable
authority: production-defect-reproduction-evidence-not-nptcr-pass
last_reviewed: 2026-08-31
source_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
verification_status: fail-reproduced
evidence_id: UI-CMD-003-production-reproduction
deployed_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
captured_at: 2026-08-31T02:25:03Z
verdict: FAIL_REPRODUCED
journeys: [P03-CMD07, P03-CMD08, P03-CMD10]
---

# UI-CMD-003 production reproduction

## Scope

- Production: `https://frontend-production-0346.up.railway.app`
- Existing signed-in principal: `platform_admin`; the defect is in the shared command result/UI-consumption path, so this evidence is valid finding evidence but is not a frozen employee-persona PASS.
- Agent: EventPilot `03d43a5c-0d5c-4c30-bab9-2734c5691434`
- Fresh synthetic Session: `e786e8bb-671c-4a2e-a6ff-3bb2f369e36a`
- Effects: create one recoverable synthetic Session and execute only the read-only `/context`, `/usage`, and `/permissions` commands. No provider prompt, tool effect, external message, credential read, role change, or billing action was requested.

## Earliest observable failure

1. Opened EventPilot through the sidebar `New conversation with EventPilot` entry.
2. Entered `/context` and submitted with Enter. The UI briefly showed the command as an active step, then returned to an empty Session after about five seconds. No context-source/coverage panel or readable command receipt remained.
3. Entered `/usage`. The only result was `Command usage completed. session_id=e786e8bb-671c-4a2e-a6ff-3bb2f369e36a`; no token/cost/budget panel opened and an internal Session ID was exposed in the ordinary chat surface.
4. Entered `/permissions`. The only result was `Command permissions completed. session_id=e786e8bb-671c-4a2e-a6ff-3bb2f369e36a`; no effective-permission/approval/restriction panel opened and the same internal ID was exposed.
5. Hard reload of the canonical Session URL removed all three command prompts/results and returned to an empty Session. The command outcome therefore had no reload-readable consumption surface.

## Live wiring and root-cause boundary

- `AgentDetail.sendChatMsg()` calls `POST /agents/{agent_id}/commands/{command}/execute` for parsed slash commands.
- `diagnostic_command_runtime.execute_diagnostic_command()` returns source-backed data for `context` and `usage`, but no `ui_action`; `_metadata_command_payload()` does the same for `permissions`.
- `AgentDetail.handleSessionCommandUiAction()` already has consumers for `open_context_panel`, `open_usage_panel`, and `open_permissions_menu`. Without one of those typed actions, the fallback adds only a client-local generic assistant message.
- The fallback message is not canonical Session evidence, so the normal durable transcript reload removes it. This is one missing typed result-to-product-surface contract, not three independent backend failures.

## Verdict

`UI-CMD-003` advances from `Observed` to `Reproduced` at exact deployed commit `bf94b76a`. P03 remains unscored: the matching employee persona, double pass, negative authority, fault/recovery, and cleanup evidence do not exist.
