# CCPlus Session Permission, Enterprise Hard Rule, and Hook Contract

Date: 2026-06-25

Status: canonical design for the current production fix

Companion ledger: `docs/ccplus-session-native-closure-gap-ledger-2026-06-25.md` is the full Session-native closure gap ledger. This document owns the permission / hard-rule / CC Hook contract slice; the companion ledger owns the cross-cutting CC Session capability map and implementation gap list.

## Decision Summary

Hive is operating as CCPlus: CC / FreeCode session semantics are the base layer, while Hive adds an enterprise control plane above it. The permission incident came from mixing those layers.

The current product ruling is:

- The composer permission menu is a session-local CCPlus control, not an enterprise approval settings page.
- The user-facing composer menu exposes only three modes: `default`, `auto`, and `bypassPermissions`.
- New sessions use the owning Agent's persisted default (`default`, `auto`, or `bypassPermissions`); existing/new Agents initially use `default` unless a user with Agent `manage` authority changes it.
- Session permission mode is not enterprise authorization. `bypassPermissions` skips ordinary session-local prompts only; tenant/RLS/resource authority, enterprise policy, sandbox, secrets, quotas, destructive confirmation, and final governance hooks remain mandatory.
- `bypassPermissions` is not break-glass. It does not require an organization administrator, a reason, or a time-to-live. A first-use warning may acknowledge risk, but it is not an authorization grant.
- `dontAsk` is a backend compatibility mode only. It must not appear in the composer permission menu.
- `plan` is not part of this menu because Hive already has Plan Mode as a separate runtime phase.
- `acceptEdits` is not part of this menu. It may remain as an internal / compatibility mode for post-plan execution, but it is not a user-facing choice.
- Backend approval rows are enterprise approvals only. Missing capability policy must not create approval rows for routine CC session prompts.
- The first enterprise hard rule is: employees can create Agents, but only admins can delete Agents.
- Agent Detail settings must not show active-session runtime or enterprise permission-policy controls, but may show the per-Agent default for future conversations; it must not show a Delete Agent button.
- Session-local permission prompts must happen inside the current chat session. A tool that needs permission must pause the run, render an in-session prompt, accept the user's decision, and continue or deny from that same session.
- Work products are stored in the Agent workspace, but delivery happens in the current chat session. Created or modified documents must be rendered as clickable session artifacts with preview / open / download actions.
- Session-native does not mean every admin page moves into chat. It means every runtime decision, control signal, task notification, produced artifact, and continuation point that affects a run must be represented in the session timeline / transcript / T0 envelope.
- The current Hook target is complete CC Hook contract parity first: all CC lifecycle events, input / output schemas, matcher semantics, blocking semantics, async semantics, and event broadcasting must exist before Hive-specific enterprise governance is layered on top.
- Custom company-specific governance is deferred. Do not design ad hoc enterprise `rule_config`, `modify_input`, or approval-routing semantics until the CC Hook substrate is complete.

## Source Baseline

FreeCode exposes the following external permission modes:

- `default`
- `acceptEdits`
- `bypassPermissions`
- `dontAsk`
- `plan`

FreeCode also has internal runtime modes including `auto`. Claude Code SDK exposes the external set without `auto`.

Hive follows CC / FreeCode as the runtime semantic baseline, but the user-facing CCPlus Web UI intentionally narrows the composer menu. The Web UI does not need to expose every persisted compatibility value.

## User-Facing Session Modes

The composer should show these modes in this order:

| Stored value | UI label | Meaning | Codex-style mapping |
| --- | --- | --- | --- |
| `default` | Ask first / 请求批准 | Standard CC-style behavior: safe reads run, sensitive actions ask in-session. | 请求批准 |
| `auto` | Approve for me / 替我批准 | Automatically allows deterministic low-risk actions, asks for ambiguous or high-risk actions. | 替我批准 |
| `bypassPermissions` | Full access / 完全访问权限 | Bypasses session-local permission prompts. Enterprise hard rules and sandbox boundaries still apply. | 完全访问权限 |

`default` is both a stored permission value and the initial per-Agent default. Agent Detail → Settings may change the default for future conversations without mutating existing sessions. Any user with Agent `manage` authority may save any of the three user-facing values. A saved `bypassPermissions` preference is consumed directly by future sessions, but it grants no new tenant, resource, capability, policy, network, secret, or sandbox authority.

Enterprise governance is an always-on outer layer, not a fourth user-facing mode. Ordinary Agent and session UI must not expose an enterprise-governance enable/disable switch. When an outer rule blocks or escalates an action, the session shows only a concise reason and recovery action; policy authoring remains in the company control plane.

## Non-Menu Compatibility Modes

### `dontAsk`

`dontAsk` remains backend-compatible because CC / FreeCode exposes it and persisted sessions may contain it. It denies actions that would otherwise need an in-session prompt. That behavior is useful as a strict internal mode, but it is not part of the CCPlus user-facing composer menu.

### `plan`

`plan` remains a backend-compatible permission mode because CC exposes it and persisted sessions may contain it. It must not appear in the composer permission menu.

Hive Plan Mode is a separate runtime phase with its own UI, state, confirmation flow, and auditable transition. Treating `plan` as just another permission dropdown item would collapse two different concepts:

- Plan Mode: planning / confirmation / no mutation until accepted.
- Permission mode: how tool permission prompts are handled inside an executable session.

### `acceptEdits`

`acceptEdits` remains backend-compatible because CC exposes it and it can be useful as an internal post-plan execution mode. It must not appear in the composer permission menu for the current product surface.

If Hive later adds "execute this accepted plan and automatically apply workspace edits", that flow may transition internally into `acceptEdits`. That is not the same as asking users to pick `acceptEdits` directly.

## Layering Contract

Tool execution must evaluate layers in this order:

1. Enterprise hard-deny rules.
2. Explicit enterprise policy overrides, when they are deliberately implemented.
3. CC Hook substrate hooks that apply to the current session / tool flow.
4. Session-local CCPlus permission mode.
5. Tool-specific safety preflight and sandbox / provider execution.

Earlier layers always win. Later layers may narrow execution but cannot bypass an earlier hard deny.

Examples:

- `bypassPermissions` can bypass session-local prompts, but cannot allow an employee to delete an Agent.
- `auto` can allow low-risk workspace edits, but cannot override an explicit hard-deny rule.
- A CC Hook may block a tool even when the session mode would allow it.

## Backend Approval Rows

Backend approval rows are reserved for enterprise approval. They should be created only when:

- An explicit enterprise policy requires approval.
- MCP / tool policy explicitly requires approval.
- A deliberate enterprise exception flow routes to an admin approval queue.

Backend approval rows must not be created for ordinary CC session-local prompt cases.

Missing policy is not the same as "requires backend approval". With no explicit enterprise policy, the request falls through to session permission mode.

## In-Session Permission Prompt Contract

CC's permission prompt is a session control flow, not an admin approval flow. Hive must follow that shape for CCPlus.

Target flow:

1. A tool call reaches session permission mode and needs human confirmation.
2. Runtime creates a pending session permission request.
3. Runtime broadcasts the request to the current session.
4. Frontend renders an in-session permission card / modal.
5. The run enters `waiting_for_user` and waits for a response.
6. The user responds inside the same session.
7. Runtime applies the response and either executes the original tool call or returns a denial.
8. The decision is written into the transcript / T0 audit trail.

This prompt must not send the user to the enterprise approval page. The user should be able to finish the decision where the tool request appeared.

### Request shape

The session permission request should carry:

- `permission_request_id`
- `session_id`
- `runtime_task_id`
- `turn_id`
- `tool_call_id`
- `tool_name`
- `tool_display_name`
- `arguments`
- `capability`
- `permission_mode`
- `decision_reason`
- `blocked_path` when applicable
- `suggestions`
- `created_at`
- `expires_at`

The request is durable enough to survive browser refresh or WebSocket reconnect while the backend task is still waiting.

### Response shape

The session permission response should support:

- `allow`
- `deny`
- optional `updated_input`
- optional feedback text
- optional permission updates

The response is scoped to the same session and pending request. A response for a stale or already resolved request must be rejected.

### User-facing choices

The Web UI should support at least:

| Choice | Scope | Effect |
| --- | --- | --- |
| Allow once / 仅本次允许 | current tool call | Execute this exact pending tool call only. No rule persists. |
| Allow for this session / 本 Session 允许 | current session | Add a session-scoped allow rule and retry this tool call. |
| Deny / 拒绝 | current tool call | Return a denial to the model. Optional feedback can tell the agent what to do instead. |

Optional later choices:

- Always allow for me / 个人持久允许.
- Always deny for me / 个人持久拒绝.
- Request enterprise exception / 请求企业例外审批.

Persistent choices must obey enterprise policy. For the production fix, the required choices are current-call allow, session allow, and deny.

Tool-specific prompts may refine the session-scoped rule:

- File edit / write: allow this file, this directory, or all workspace edits for the current session.
- Web fetch / search: allow this exact URL, this domain, or this web tool for the current session.
- Command execution: allow this exact command only, unless a safer command-family rule is explicitly implemented.

### Mode interactions

- `auto`: asks only when the action is ambiguous or high risk.
- `default`: asks for sensitive actions.
- `bypassPermissions`: does not render a session permission prompt; enterprise hard rules still apply.

Compatibility-only `dontAsk` does not render a prompt; it denies unless a matching rule already exists. It must not be shown as a composer option.

### Hook interaction

CC-compatible `PermissionRequest` hooks run as part of this flow.

The CC-compatible behavior is:

- A hook may allow, deny, modify input, or pass through.
- If a hook makes a decision before the user responds, the pending UI prompt is cancelled / resolved.
- If the user responds first, that decision wins for the pending request.
- Hook decisions and human decisions both write audit metadata.

This preserves CC's "hook and user prompt race" behavior. Future Hive enterprise governance can be layered above this substrate later.

### Current implementation state

Implemented in the current production fix:

- Backend emits `session_permission_required` with a stable `permission_request_id` and structured `permission_request` payload.
- Web runtime persists the request in the session transcript and marks the run as `awaiting_session_permission`.
- Frontend renders the request inside the current chat session, including when the request is grouped inside an active-run disclosure.
- The in-session UI supports Allow once, Allow for this session, and Deny.
- `POST /agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve` resolves the request without navigating to enterprise approvals.
- Allow once executes the original tool call under a one-call session permission override and then starts a continuation run.
- Allow for this session also appends the tool to `ChatSession.transcript_metadata_json.session_permission_allowed_tools`.
- Deny records the decision, emits the CC-compatible `PermissionDenied` hook, and resolves the session with a denied status.
- The decision is written back into the session transcript and broadcast to the session.
- `PermissionRequest` hooks run before rendering the session-local prompt. A hook can allow, deny, or rewrite `updatedInput`; allow continues the tool call without creating a backend approval row, deny records a session-local denial.
- The CC 27-event Hook contract is pinned in code and tested against the FreeCode event set. CC JSON output semantics, including `hookSpecificOutput`, `continue`, `decision`, exit code `2`, nonzero diagnostic exits, `async`, and permission decision payloads, normalize into Hive `HookResult`.

Remaining hardening:

- The pending request is transcript-backed. A dedicated pending-request table can be added later for stricter expiry / stale-response enforcement.
- Deterministic first-wins racing between long-running async hooks and the user UI prompt can be hardened further; the synchronous hook decision path is implemented.
- Tool-specific narrower scopes, such as URL-domain-only or file-path-only session rules, are not yet split out from the current tool-level session allow list.

## Session-Native Work Product Delivery

CCPlus sessions must complete the whole work loop inside the session timeline. The workspace is the durable storage layer. It is not the primary user delivery surface.

The required product rule is:

- Documents and generated files may be written under `workspace/`.
- The session must still show the created / modified artifact inline.
- The artifact card must be clickable from the chat message.
- Markdown and text artifacts should preview inline.
- Office / PDF / image artifacts should open in the appropriate preview or viewer path, with download as a secondary action.
- The final assistant response must not rely on "go to workspace and find the file" as the only delivery.

This applies to both creation and modification:

1. The agent creates a report, plan, document, spreadsheet, slide deck, PDF, or image.
2. The runtime registers the output path as a session artifact.
3. The chat transcript receives an `artifact_delivery` event or assistant message artifact part.
4. The frontend renders a session artifact card.
5. The user can click from the session to preview, open, or download.
6. If the agent later edits the same artifact, the session must show the updated artifact state instead of hiding the modification inside the workspace tab.

### Artifact event shape

Session artifact parts should carry:

- `type: "artifact"`
- `artifact_id`
- `session_id`
- `runtime_task_id`
- `path`
- `name`
- `mime_type`
- `size`
- `modified_at`
- `preview_kind`
- `source`

Optional later fields:

- `revision_id`
- `action`: `created`, `updated`, or `finalized`
- `tool_call_id`
- `open_url`
- `download_url`
- `diff_summary`

### Current implementation state

Current working pieces:

- Backend has `ChatArtifact` / `artifact_delivery`.
- Web chat tool-result persistence creates artifact parts immediately for file/document write tools.
- Web chat finalization also converts `recent_writes` into artifact parts as a safety net.
- Kernel tracks `write_file`, `edit_file`, `fs_write`, `office_document_create`, and `office_document_apply` outputs as recent session writes.
- Frontend `AgentChatSection` can render artifact cards and inline previews.

Current gap:

- Workspace and Office surfaces must remain storage / editing surfaces, not required navigation for delivery.
- Artifact updates should add richer `revision_id`, `action`, `tool_call_id`, and `diff_summary` metadata so repeated edits are visibly versioned in the same session.

## Session-Native Boundary Ledger

This section records the product boundary after comparing CC / FreeCode session behavior with Hive's current surfaces.

The core rule:

```text
Admin configuration may live outside a session.
Runtime control, permission / hook decisions, work products, task notifications, and continuation points that affect a run must appear inside the session.
```

CC stores artifacts and transcripts on disk, but the model-visible and user-visible control flow returns through the session:

- User input is appended to the session transcript before the model loop runs.
- Large tool outputs are persisted under a session-scoped path and represented in the session as preview plus readable path.
- Async subagents and team workers may execute outside the active turn, but they rejoin the parent session through task notifications / sidechain transcripts.
- Hook, permission, stop, and notification effects are injected into the same message / transcript pipeline when they affect the active run.

### Must Be Session-Native

These surfaces must be represented as session events / cards / artifacts, even when execution is asynchronous or backed by a durable workspace:

| Surface | Session requirement | Storage / execution layer |
| --- | --- | --- |
| User prompt and assistant turn | Durable append before runtime execution; replayable in T0 / transcript. | `ChatSession`, T0 session ledger, `RuntimeTask` for active run. |
| Tool calls and tool results | Tool request, permission status, result, denial, or failure visible in the same session. | Tool runtime, capability gate, invocation spans. |
| Session permission prompt | Prompt, allow once, allow for session, deny, and continuation happen inside the current session. | Session permission resolver plus session metadata. |
| Plan Mode | Request, plan card, approval / rejection, and exit state are session controls. | Plan runtime and transcript events. |
| File and document creation | Created file appears as clickable session artifact; final answer must not rely on workspace navigation. | Agent workspace, `ChatArtifact`, Office / file services. |
| File and document modification | Updated artifact state, revision, action, and diff summary appear in session. | Workspace / Office editor remains storage and editing layer. |
| Dynamic Workflow | Run tree, step state, waits, gate decisions, leaf outputs, and final artifact appear in session. | Workflow runtime, journals, workspace artifacts. |
| Sub-agent / Agent Team work | Parent session shows child-session card, wake notification, result, continuation actions, and close state. | Child `ChatSession`, runtime task, mailbox / task notification. |
| Goal / one-off task execution | Creation may be command/UI driven, but execution creates or binds a session and returns result there. | Objective/task scheduler plus session-bound run. |
| Scheduled task execution | Schedule definition can be admin/UI state; every fire creates/binds a session and delivers result there. | Scheduler / trigger daemon plus session-bound run. |
| Hook / approval decision that affects a run | Block / allow / modify / request-approval decision appears as a session event with reason. | CC Hook substrate, capability gate, and explicit enterprise approval logs when applicable. |
| Memory / knowledge / evolution candidate caused by a session | Candidate, acceptance, rejection, or queued review appears as a session-visible event when the current run produced it. | Memory Gate, Platform Gate, T2/T3 vault. |
| Runtime interruption / resume / compact / clear | State change is represented in the session timeline and replay/export path. | Session metadata, T0, runtime control API. |

### Outside Session, But Requires Session Projection When Triggered By A Session

These are allowed to remain control-plane pages or backend modules. The requirement is projection, not relocation.

| Surface | Outside-session source of truth | Required session projection |
| --- | --- | --- |
| Enterprise approval queue | Enterprise / admin approval module. | If a session tool triggers approval, the session shows pending approval, resolution, and continuation or denial. |
| CC Hook configuration | Agent / workspace / plugin / Skill hook config; future enterprise governance module only after CC Hook parity. | Runtime hook decisions affecting the active run appear in session with decision layer and reason. |
| Skill library | Company / Agent Skill registry. | Loading a Skill, selecting a Skill for a task, or generating a Skill candidate appears in session. |
| Sub-agent registry | Company / Agent Sub-agent registry. | Spawning, delegating to, or continuing a Sub-agent appears in session. |
| Fixed Workflow registry | Company workflow library. | Starting or modifying a workflow run appears in session; fixed asset management stays in control plane. |
| Workspace file browser | Agent workspace. | Session-produced and session-modified files are always rendered as session artifacts. |
| Office workbench | Office editor / document service. | Session-created or session-edited documents are shown as session artifacts with preview/open actions. |
| Knowledge / memory admin views | Knowledge and memory control-plane surfaces. | Session-originated memory / knowledge writes show candidate and decision state in session. |
| Audit / invocation spans | Audit, activity, and trace stores. | Current-run decisions and failures should be summarized as session cards, with deep trace remaining in inspector/admin views. |
| Global schedule / automation list | Automation control plane. | Each execution instance has a session; list management remains outside. |

### Must Stay Outside Session

These are not session work products or runtime continuation points:

- Tenant / organization / member administration.
- Role management.
- Billing, quota, and provider-key configuration.
- Enterprise hard rules such as "employees cannot delete Agents".
- Admin-only deletion / destructive asset management.
- Governance policy authoring and publication.
- Platform health / deployment / migration operations.

These surfaces may create audit logs, but they do not need to become chat messages unless a specific session initiated or depended on the action.

### Current Hive Gaps Against This Boundary

| Gap | Current risk | Required closure |
| --- | --- | --- |
| Enterprise approval projection | Approval rows can live only in approval pages, so the originating session feels blocked without an in-session loop. | Add session pending-approval card / event for enterprise approvals triggered by a session, plus resolution wake / continuation. |
| Hook / control-plane decision projection | Hook or approval decisions can become invisible control-plane behavior. | Record decisions with `session_id` / `runtime_task_id` and render relevant decisions in session. |
| Artifact revision metadata | Artifact cards exist, but repeated edits can be hard to follow. | Add revision/action/tool_call/diff fields and render update state in the same session. |
| Workflow session envelope | Workflow journals/artifacts can be stronger than the session view. | Render workflow run tree, step events, waits, and final artifacts in session and export them through session/T0. |
| Sub-agent continuation | Task result summaries can hide the child conversation. | Return and render `child_session_id`; parent receives wake notifications and can continue the child session. |
| Scheduled / one-off task execution | Automation pages can become the only visible result surface. | Every fire/run must create or bind a session and deliver completion there. |
| Workspace / Office manual bridge | Standalone editing surfaces can hide work from the session. | Session-initiated workspace/Office changes must append artifact/update events back to the session. |
| Memory / evolution decisions | Memory candidate and gate outcomes can be invisible to the user who caused them. | Show session-originated memory/evolution candidates and gate outcomes in the session. |
| Audit-only failures | Important governance/tool failures may require admin log inspection. | Add user-facing session cards for current-run blocks/failures with inspector links for full trace. |

Implementation work should not call a surface "complete" if its only replay path is a workspace file, notification, `RuntimeTask.result_summary`, local event table, workflow journal, or admin approval row. The replay path must include the session / T0 envelope.

## Session Permission Behavior

### `auto`

`auto` is an available per-Agent default for new sessions. New and existing Agents initialize to `default` unless an administrator or authorized manager explicitly saves `auto` in Agent Detail → Settings.

It should:

- Allow safe read and discovery tools.
- Allow deterministic low-risk local workspace edits.
- Ask in-session for external side effects, dangerous commands, ambiguous code execution, and high-risk mutations.
- Never bypass enterprise hard rules.

### `default`

`default` should:

- Allow safe read and discovery tools.
- Ask in-session for sensitive writes, external side effects, code execution, dangerous commands, and ambiguous actions.
- Avoid backend approval rows unless explicit enterprise policy requires approval.

### Compatibility-only `dontAsk`

`dontAsk` should:

- Allow safe read and discovery tools.
- Allow explicitly preapproved actions.
- Deny non-preapproved sensitive actions immediately.
- Avoid backend approval rows as a fallback.
- Stay hidden from the user-facing composer menu.

### `bypassPermissions`

`bypassPermissions` should:

- Bypass session-local permission prompts.
- Be selectable by the current authorized session operator without an administrator, reason, or TTL.
- Be persistable as the default for future conversations by a user with Agent `manage` authority.
- Still enforce enterprise hard rules.
- Still enforce tenant boundaries, credential boundaries, managed memory / soul paths, and sandbox / provider safety.
- Still require bypass-immune confirmation for destructive deletion and other explicitly protected actions.

## Enterprise Hard Rule V1

The first hard enterprise rule is intentionally narrow:

- Employees can create Agents.
- Employees cannot delete Agents.
- Only `org_admin` or `platform_admin` can delete Agents.

This rule is not a CC permission prompt. It cannot be bypassed by `bypassPermissions`.

The Agent Detail settings UI must not show a Delete Agent button. Agent deletion belongs in an enterprise / admin control-plane surface only.

## Enterprise Asset Protection Scope

Sub-agent, Skill, and Workflow protection is real, but its scope must be precise.

The protected asset rule applies when an asset has entered a company-governed state, for example:

- a Skill promoted into the company library or evolution loop,
- a Sub-agent definition promoted into a company-governed registry,
- a Workflow fixed / registered as a company workflow.

Drafts, local experiments, or session-generated candidates should not be treated as the same enterprise asset class until promoted.

The complete asset-protection implementation must model this lifecycle explicitly instead of applying a blunt delete ban to every draft object.

## Agent Detail Settings Boundary

Agent Detail settings must not expose these controls:

- broad access permission settings,
- Delete Agent.

The composer owns the active session permission mode. Agent Detail Settings may own only the default mode for future conversations; it is an Agent preference, not an enterprise allow rule. Enterprise / admin pages own company-level governance and destructive asset operations.

Agent Detail settings may still own non-governance configuration such as display metadata, channels, schedules, timezone, and runtime limits, as long as those controls do not represent enterprise permission policy.

## CC Hook Contract Target

The current implementation target is CC Hook parity, not Hive enterprise governance policy authoring.

Hive must build the complete CC Hook event contract in one pass, then add Codex-style engineering advantages around observability, typed state, replay, and session control. Enterprise-specific governance rules come later as an overlay on this substrate.

### Complete CC Hook Event Set

Hive must support the full CC lifecycle event set:

| Group | Events | Hive mapping |
| --- | --- | --- |
| Tool lifecycle | `PreToolUse`, `PostToolUse`, `PostToolUseFailure` | Tool runtime, capability gate, tool result mapping, failure handling. |
| Session / turn | `Notification`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`, `Stop`, `StopFailure` | `ChatSession`, T0 session envelope, turn lifecycle, stop-hook continuation. |
| Subagent | `SubagentStart`, `SubagentStop` | Lightweight subagent child sessions / replayable child segments. |
| Compaction | `PreCompact`, `PostCompact` | Context compaction lifecycle and transcript export. |
| Permission | `PermissionRequest`, `PermissionDenied` | Session-local permission prompt / denial flow. |
| Setup | `Setup` | Cloud session/runtime setup, config/trust/onboarding equivalent. |
| Team / task | `TeammateIdle`, `TaskCreated`, `TaskCompleted` | Agent Team / task notification / child session mailbox. |
| MCP elicitation | `Elicitation`, `ElicitationResult` | MCP or tool-originated interactive user input inside session. |
| Environment | `ConfigChange`, `WorktreeCreate`, `WorktreeRemove`, `InstructionsLoaded`, `CwdChanged`, `FileChanged` | Hive workspace config/root/file/artifact/instruction changes. |

This replaces the previous 7-event V1 list. Partial event coverage is not acceptable for the CC Hook substrate, even if some events initially have disabled/no-op runtime producers.

### CC Hook Sources

Follow CC source semantics before inventing enterprise policy:

| CC source | Hive equivalent |
| --- | --- |
| settings hooks | Agent / workspace / project-level hook config once exposed. |
| plugin hooks | Installed extension / capability pack hooks. |
| skill hooks | Skill package hook metadata. |
| internal hooks | Runtime-owned callback hooks used for built-in session/workspace/accounting behavior. |

Tenant-wide enterprise hook policy is a future source. It must not be the first implementation path because it would force premature enterprise rule design before the CC substrate exists.

### CC Hook Execution Types

The hook substrate must support the same execution families at the contract level:

| CC type | Current Hive stance |
| --- | --- |
| `command` | Supported only through an approved sandbox / code execution provider, never raw API-process subprocess in production. |
| `http` | Supported through outbound HTTP hook execution with timeout, audit, and credential stripping. |
| `agent` | Mapped to governed child session / subagent execution. |
| `prompt` | Mapped to prompt expansion / additional context injection. |
| `callback` / `function` | Internal runtime hooks only; not user-uploaded arbitrary backend code. |

Python hooks may exist later as a convenience implementation of `command` or sandboxed code execution. They are not a separate governance model.

### CC Hook Input / Matcher Semantics

Follow CC first:

- Every hook input has `hook_event_name`.
- Hook input includes session / transcript / cwd or Hive workspace equivalent.
- Tool events include tool name, tool input, tool id, and permission context.
- `PreToolUse` matchers match tool names.
- `SessionStart` matchers match source.
- `FileChanged` matchers match file paths.
- Environment events map local CLI concepts to Hive cloud workspace equivalents instead of being dropped.

Hive-specific additions must be additive and typed:

- `tenant_id`
- `agent_id`
- `session_id`
- `runtime_task_id`
- `turn_id`
- `tool_call_id` when applicable
- `hook_invocation_id`
- `source_ref` / T0 references when applicable

### CC Hook Output Semantics

Follow CC output behavior before designing enterprise-specific actions:

| Output | CC behavior | Hive behavior |
| --- | --- | --- |
| `continue: false` | Stop the flow after hook. | Set session/run state to blocked or stopped with reason. |
| `suppressOutput` | Hide hook stdout from transcript. | Do not render raw hook stdout; keep audit metadata. |
| `stopReason` | Message shown when continuation is stopped. | Session-visible stop/block reason. |
| `decision: "approve"` | Approve permission path. | Convert to session permission allow where applicable. |
| `decision: "block"` | Block and return reason to model. | Block tool/turn and append session-visible reason. |
| exit code `2` | Blocking feedback; stderr becomes reason. | Same semantic for command-like hooks. |
| other non-zero exit | Non-blocking error shown to user. | Session/audit warning, not automatic block. |
| `additionalContext` | Inject context into the model flow. | Append typed context injection event / runtime context. |
| `updatedInput` | Rewrite `PreToolUse` input. | Execute tool with rewritten typed input after validation. |
| `updatedMCPToolOutput` | Rewrite MCP tool output in `PostToolUse`. | Rewrite external/MCP tool output with audit trail. |
| `PermissionRequest.decision` | Allow/deny permission request, possibly with updated input/permissions. | Resolve session-local permission request with the same semantics. |
| `retry` on `PermissionDenied` | Retry when allowed by event contract. | Resume permission/tool flow only through typed continuation. |
| `async: true` | Fire-and-forget hook. | Background hook run with trace, timeout, and optional session notification. |

Do not introduce `request_enterprise_approval`, custom rule DSLs, or broad enterprise policy actions in the CC Hook substrate. Those are future Hive overlays.

### Codex Engineering Advantages To Add

CC semantics are the behavioral baseline. Codex-style additions should improve control and observability without changing the contract:

- Typed hook invocation records with stable IDs and schema versions.
- `session_id`, `runtime_task_id`, `turn_id`, and `tool_call_id` correlation on every hook event.
- Durable hook event stream that can be exported with the session/T0 envelope.
- Separate hook progress / result notifications, analogous to CC `includeHookEvents`, without polluting the core assistant transcript unless the hook affects the run.
- Idempotent retry / resume behavior for hook execution and continuation.
- Structured run states: `running`, `waiting_for_permission`, `blocked_by_hook`, `continued_by_hook`, `failed_hook_non_blocking`, `completed`.
- Inspector / audit views backed by the same hook invocation records.
- Tests that compare Hive's canonical hook event enum against the CC 27-event set.

### Current Enterprise Governance Ruling

For this production line, Hive has only one enterprise hard rule:

```text
Non-admin users cannot delete Agents.
```

This is a platform hard-deny rule, not a Hook rule and not a session permission prompt. It cannot be bypassed by `bypassPermissions`, CC hooks, plugin hooks, Skill hooks, or future enterprise hooks.

Everything else in enterprise governance is deferred until after the CC Hook substrate is complete.

Future enterprise governance may add:

- tenant-wide hook policy,
- policy assignment to all current / future Agents,
- impacted-Agent preview,
- publish / pause / rollback,
- richer enterprise approval routing,
- protected Skill / Sub-agent / fixed Workflow asset rules.

Those are deliberately not current implementation blockers. They must be designed after CC Hook parity is in place.

## Hook Execution Safety Contract

Hook customization may eventually support command / Python-like scripts, HTTP hooks, agent hooks, and prompt hooks, but executable hooks must not execute inside the API process or inherit raw host credentials.

Required safety properties:

- Script execution happens in a sandboxed worker or approved external code execution provider.
- Input is a typed JSON hook context.
- Output is a typed JSON hook result.
- Runtime has a strict timeout.
- Runtime has no ambient access to tenant secrets.
- Scripts are versioned, auditable, and reversible.
- Executable user / workspace hooks require explicit trust / admin activation when exposed.
- Syntax validation and dry-run simulation are required before activation.

This keeps hooks powerful without turning them into arbitrary backend code execution. Enterprise governance can later reuse this safety substrate, but it is not the current rule-design target.

## Implementation Targets

### Backend

Files / surfaces:

- `backend/app/runtime/ccplus_contracts.py`
- `backend/app/tools/governance.py`
- `backend/app/services/capability_gate.py`
- `backend/app/api/agents.py`
- session permission request model / store
- session permission resolve API or WebSocket control response
- CC Hook contract schemas, invocation store, runtime executor, event stream, and session/T0 projection

Required changes:

- Done: Persist a per-Agent default for new sessions; initialize it to `default`; preserve explicit `auto` and `bypassPermissions`; consume the saved value directly without conflating session mode with enterprise break-glass.
- Done: Keep `plan` and `acceptEdits` normalization for compatibility.
- Done: Keep `plan` and `acceptEdits` out of the user-facing command / composer menu contract.
- Done: Make no-policy fall through to session permission mode.
- Done: Create backend approval rows only for explicit enterprise approval.
- Done: Replace session permission "blocking message only" with a pending request / await / resume flow.
- Done: Add in-session permission request events with stable request IDs.
- Done: Add a resolver that applies `allow once`, `allow for session`, and `deny`.
- Done: Persist session-scoped permission updates in the current session metadata / runtime state.
- Done: Register user-facing document outputs as session artifacts, including `write_file`, `edit_file`, `fs_write`, `office_document_create`, and `office_document_apply`.
- Done: Persist artifact delivery into the chat transcript / T0 event stream.
- Done: Enforce admin-only Agent deletion.
- Add canonical CC Hook event enum with all 27 events.
- Add typed hook input / output schemas following CC.
- Add matcher semantics for tool, session source, file path, and workspace/environment events.
- Add hook invocation records with stable IDs, schema version, session/run/turn/tool correlation, status, output, and error metadata.
- Add hook execution adapters for contract-level `command`, `http`, `agent`, `prompt`, and internal callback hooks.
- Add blocking semantics for `decision: "block"` and exit code `2`.
- Add non-blocking error semantics for other non-zero exits.
- Add `additionalContext`, `updatedInput`, `updatedMCPToolOutput`, `PermissionRequest.decision`, `PermissionDenied.retry`, and `async: true` handling.
- Add hook progress / result event broadcasting and session/T0 export.
- Add no-op / disabled producers for CC events that do not yet have an active Hive runtime producer, so the contract remains complete.
- Defer tenant-wide enterprise Hook policy, assignment, impacted-Agent preview, publish / pause / rollback, and enterprise rule DSLs until after the CC Hook substrate is complete.

### Frontend

Files / surfaces:

- `frontend/src/pages/agent-detail/AgentChatSection.tsx`
- `frontend/src/pages/agent-detail/LocalAgentChatSection.tsx`
- `frontend/src/pages/agent-detail/AgentSettingsSection.tsx`
- `frontend/src/pages/OpenClawSettings.tsx`
- `frontend/src/i18n/en.json`
- `frontend/src/i18n/zh.json`

Required changes:

- Done: Replace the static composer access badge with a clickable permission menu.
- Done: Menu items are `default`, `auto`, and `bypassPermissions`.
- Done: New sessions inherit the persisted per-Agent default; bare-session and atomic create+run paths consume the same value.
- Done: The selected mode is included in session / run metadata sent to the backend.
- Done: Do not render `plan` or `acceptEdits` in the menu.
- Done: Render session-local permission requests inside the chat session.
- Done: Support Allow once, Allow for this session, and Deny.
- Done: Resolve permission prompts without navigating to enterprise approval settings.
- Done: Keep pending permission prompts visible across reconnect / refresh while the backend request is pending.
- Done: Render generated / modified workspace artifacts inside the session as clickable cards.
- Done: Support inline preview for Markdown / text artifacts and viewer / download actions for Office, PDF, and image artifacts.
- Done: Keep enterprise permission-policy authoring out of Agent Detail settings; expose only the per-Agent default for future conversations.
- Done: Remove Delete Agent from Agent Detail settings.
- Add hook progress / result rendering in the session timeline when a hook affects the active run.
- Add inspector / audit UI backed by hook invocation records.
- Do not add enterprise governance / tenant policy authoring UI in the current CC Hook substrate pass.

## Test Plan

### Backend tests

Required coverage:

- New `PermissionProfileV1()` defaults to `default`.
- `build_permission_profile()` normalizes:
  - `default`
  - `auto`
  - `dontAsk`
  - `bypassPermissions`
  - legacy aliases
  - compatibility values `plan` and `acceptEdits`
- `auto` allows safe read / discovery tools without backend approval.
- `auto` asks in-session for high-risk actions.
- `default` asks in-session for sensitive actions.
- Compatibility-only `dontAsk` denies non-preapproved sensitive actions without backend approval.
- `bypassPermissions` bypasses session prompts but does not bypass Agent deletion hard rules.
- A sensitive tool in `default` creates a pending session permission request and waits.
- Allow once resolves the pending request and executes only the original tool call.
- Allow for session updates session-scoped permission rules and executes the original tool call.
- Deny resolves the pending request and returns a denial to the model.
- Browser reconnect can reload the pending permission request.
- Workspace file writes are surfaced as chat artifacts.
- Office document create / apply writes are surfaced as chat artifacts.
- Artifact delivery events are written to the session transcript / T0 stream.
- Explicit enterprise approval policy still creates backend approval rows.
- Non-admin Agent deletion returns 403.
- Canonical Hook event enum equals the CC 27-event set.
- Hook input / output schema accepts CC-compatible payloads and rejects invalid event-specific output.
- `PreToolUse` can block a tool through `decision: "block"` and exit code `2`.
- `PreToolUse.updatedInput` rewrites typed tool input before execution.
- `additionalContext` is injected into runtime context where the event supports it.
- `PostToolUse.updatedMCPToolOutput` rewrites MCP / external tool output with audit metadata.
- `PermissionRequest.decision` resolves a session-local permission request.
- `PermissionDenied.retry` can resume only through the typed continuation path.
- `async: true` hook execution records background status without blocking the main run.
- Hook invocation records include session/run/turn/tool correlation IDs and are exportable with session/T0.

### Frontend tests

Required coverage:

- Composer renders three user-facing permission modes.
- Composer defaults to the persisted per-Agent preference for a new conversation; users with valid session access receive the same three modes regardless of company role.
- Selecting Full access requires at most a one-time risk acknowledgement; it never asks for an administrator, reason, scope, or TTL.
- Saving Full access as the Agent default requires Agent `manage` authority and applies only to future conversations.
- `bypassPermissions` cannot override tenant/RLS/resource authority, GuardPolicy, CapabilityPolicy, MCP trust, secrets, quotas, sandbox, destructive confirmation, or final governance hooks.
- Selecting a mode updates outgoing session / run metadata.
- Menu does not render `plan` or `acceptEdits`.
- Chat renders in-session permission prompt cards.
- Allow once, Allow for session, and Deny call the session permission resolver.
- Session permission prompts do not navigate to the enterprise approval queue.
- Assistant messages with artifact parts render clickable artifact cards.
- Clicking a Markdown / text artifact previews it inside the chat surface.
- Office / PDF / image artifacts expose open / download actions from the session.
- Agent Detail settings renders the three-value future-conversation default, but no enterprise permission-policy authoring controls.
- Agent Detail settings does not render Delete Agent.
- Session timeline renders hook block / warning / context-injection events when they affect the active run.
- Inspector can show hook invocation status and correlation IDs.
- Enterprise Settings does not expose tenant Hook policy authoring in this pass.

## Deployment Verification

Before deployment:

```bash
cd backend
source .venv/bin/activate
pytest tests/api/test_chat_session_runs.py tests/services/test_cc_permission_modes.py tests/services/test_permission_profile_v1.py tests/api/test_enterprise_asset_hard_rules.py -q
pytest tests/services/test_web_chat_runtime.py tests/kernel/test_engine.py -q -k "permission or artifact or office_created_document_as_session_artifact or office_apply_output_as_session_artifact"
ruff check app/runtime/ccplus_contracts.py app/tools/governance.py app/services/capability_gate.py app/api/chat_sessions.py app/services/web_chat_runtime.py app/services/chat_artifact_delivery.py app/kernel/engine.py tests/services/test_cc_permission_modes.py tests/services/test_permission_profile_v1.py tests/services/test_web_chat_runtime.py tests/kernel/test_engine.py tests/api/test_enterprise_asset_hard_rules.py tests/api/test_chat_session_runs.py

cd ../frontend
npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/chatRuntime.test.ts
npm run build
```

Production verification:

- A fresh chat starts in `auto`.
- Basic read / discovery calls do not create backend approval rows.
- High-risk actions produce in-session permission prompts that can be allowed or denied without leaving the session, unless explicit enterprise policy requires backend approval.
- A generated Markdown report appears as a clickable artifact in the same chat session and previews inline.
- A created or modified Office document appears as a clickable artifact in the same chat session.
- Non-admin users cannot delete Agents through API or UI.
- Agent Detail settings no longer contains permission settings or Delete Agent.
- Hook substrate tests prove the CC 27-event contract and core output semantics before any enterprise governance overlay is added.

## Non-Goals

This document does not redesign Plan Mode.

This document does not make every Skill, Sub-agent, or Workflow immutable immediately. It defines the promotion boundary for future enterprise asset protection.

This document does not design tenant-wide enterprise Hook policy, rule DSLs, or policy assignment. Those are future overlays after CC Hook parity.

This document does not permit raw Python execution inside the backend process. Command / Python-like hooks must use the sandboxed hook contract above.
