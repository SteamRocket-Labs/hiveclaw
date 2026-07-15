# Session Right Rail Runtime Console Design (2026-07-03)

> Status: docs-first product/design contract. This document records the next right-rail direction before implementation.
> Scope: Agent detail session right rail, especially the upper delivery shelf and lower runtime console.

## 0. Decision

The right rail is not a generic inspector and not a long stack of cards. It has two jobs:

```text
Right rail = Session delivery shelf + Runtime console
```

The upper half answers:

```text
What did this session deliver, and what can I open now?
```

The lower half answers:

```text
Who or what is running behind this session, where is it, and how can I intervene?
```

The lower half must not express Agent Team, Sub-agent, and Workflow as the same list item. They are different runtime concepts:

| Runtime kind | Product metaphor | User action model |
| --- | --- | --- |
| Agent Team | Room / roster | Enter member session, message a member, observe persistent collaborators |
| Sub-agent | Worker queue | Inspect output, continue/resume the worker, avoid treating it as a teammate room |
| Dynamic Workflow | Pipeline / run graph | Open run, inspect steps/leaves, handle gates, resume/repair/cancel/promote |
| Activity | Event log | Read notifications, background completions, and debug/raw details when needed |

## 1. Current State

Current implementation has enough data but the presentation is too flat:

- `frontend/src/pages/agent-detail/AgentChatSection.tsx` renders the right rail as Session artifacts plus a lower Collaboration section.
- `frontend/src/pages/session-workbench/timelineModel.ts` normalizes `runtime_sections.agent_teams/subagents/workflows/background/notifications/runs/raw`.
- The lower right rail currently exposes many peer cards: main session, metrics, Agent Teams, Sub-agents, Dynamic Workflow, Background agents, Notifications, Runs, Raw.

This is mechanically useful but visually and conceptually noisy:

1. Empty runtime types consume too much vertical space.
2. Runtime metrics compete with real controls.
3. Raw/debug surfaces are visible at the same level as product surfaces.
4. Sub-agent, Agent Team, and Workflow appear as adjacent buckets rather than distinct interaction modes.
5. When upper artifacts are long, the runtime console is pushed too far down.

## 2. Layout Contract

Right rail should keep a stable two-zone layout.

```text
┌──────────────────────── Right Rail 440-480px ────────────────────────┐
│ Header: collapse / resize                                             │
├──────────────────────── Upper: Session Delivery ──────────────────────┤
│ Current session                                                       │
│  [file] abs_research_2026Q2_comparison.md      Open  Download         │
│  [file] lbo_junk_bond_report.md                Open  Download         │
│  [file] abs_deep_lbo_clo_hy_plan.md            Open  Download         │
│  View all 9                                                           │
│ Historical collapsed                                                  │
│ Unattributed collapsed                                                │
├──────────────────────── Lower: Runtime Console ──────────────────────┤
│ main idle · running 0 · waiting 0                                     │
│ [Team] [Workers] [Workflow] [Activity]                                │
│                                                                       │
│ Selected segment content                                               │
└───────────────────────────────────────────────────────────────────────┘
```

### 2.1 Width

Default width:

```text
440-480px
```

Resizable range:

```text
min 360px
max 560px
```

Below 380px:

- Hide secondary row actions behind a menu or tooltip.
- Keep only the primary action visible.
- Avoid wrapping rows into tall multi-line cards.

### 2.2 Height Ratio

The ratio should be state-aware, not fixed.

When there is active runtime:

```text
Upper delivery shelf: 32-38%
Lower runtime console: 62-68%
```

When the session is complete and idle:

```text
Upper delivery shelf: 50-60%
Lower runtime summary: 40-50%
```

When there are no artifacts:

```text
Upper delivery shelf collapses to empty-state height.
Lower runtime console takes the remaining height.
```

## 3. Upper Zone: Session Delivery Shelf

The upper zone is a delivery shelf, not a workspace file browser.

### 3.1 Information Rules

Show:

- Current-session artifacts first.
- Strongly session-related delivery only.
- File name.
- Type.
- Size.
- Optional source agent / contributor.
- Open and download actions.

Do not show by default:

- Delivery counts such as `x2`.
- Full mutable workspace inventory.
- Historical artifacts expanded by default.
- Unattributed artifacts expanded by default.
- Raw artifact metadata.

### 3.2 Grouping

```text
Session artifacts
├─ Current session
│  ├─ visible rows: max 4-5
│  └─ View all N
├─ Historical collapsed
└─ Unattributed collapsed
```

Current-session group should be open by default. Historical and unattributed groups should stay collapsed unless there is no current-session artifact.

### 3.3 Row Shape

```text
[file icon] abs_research_2026Q2_comparison.md
            markdown · current · 22 KB · By Reviewer Bot
                                      Open  Download
```

If the width is narrow:

```text
[file] abs_research_2026Q2_comparison.md        Open
       markdown · 22 KB · By Reviewer Bot       ...
```

### 3.4 Sorting

Preferred order:

1. Artifacts explicitly delivered in final assistant message.
2. Artifacts created or updated in the active session.
3. Artifacts from child runtime completion notifications.
4. Historical artifacts.
5. Unattributed artifacts.

## 4. Lower Zone: Runtime Console

The lower zone should become a compact runtime console with one summary strip and four segment modes.

```text
Runtime console
├─ Summary strip
│  └─ main idle · running N · waiting N · blocked N
├─ Segmented control
│  ├─ Team
│  ├─ Workers
│  ├─ Workflow
│  └─ Activity
└─ Mode-specific body
```

This replaces the always-visible stack of Agent Teams / Sub-agents / Dynamic Workflow / Background / Notifications / Runs / Raw cards.

## 5. Segment 1: Team

Team is for persistent collaborator sessions.

### 5.1 Meaning

Agent Team represents a room or roster:

- Team container.
- Named members.
- Member status.
- Persistent member session.
- Mailbox / continuation semantics.

It is not a batch of one-shot workers.

### 5.2 UI Shape

```text
Team
┌ Research Team                         running ┐
│ Reviewer        running   1m35s   Enter       │
│ Analyst         waiting   0m42s   Enter       │
│ Writer          idle      -       Enter       │
└───────────────────────────────────────────────┘
```

### 5.3 Row Actions

Primary:

- Enter

Secondary:

- Message
- Resume
- Close

Disabled actions must explain why they are disabled.

### 5.4 Empty State

```text
No Agent Team in this session.
Create a Team only when this task needs persistent named collaborators.
```

Do not suggest Agent Team when the active task is a one-shot decomposition.

## 6. Segment 2: Workers

Workers is for ordinary Sub-agents and other one-shot local/background workers.

### 6.1 Meaning

Sub-agent is a worker queue item:

- One-shot or background run.
- Has output.
- Can be inspected.
- Can be continued/resumed when the runtime supports it.
- Does not become a Team member.

This follows the CC-style distinction:

```text
team_name + name => Agent Team teammate
ordinary subagent => worker/local agent
```

### 6.2 UI Shape

```text
Workers
┌ One-shot critic       completed   23s   Inspect  Continue ┐
│ Market scout         running     1m10s Inspect            │
│ Source verifier      failed      12s   Inspect  Retry     │
└───────────────────────────────────────────────────────────┘
```

### 6.3 Row Actions

Primary by state:

| State | Primary action |
| --- | --- |
| running | Inspect |
| completed | Inspect |
| completed and continuable | Continue |
| failed | Inspect |
| failed and retryable | Retry |

Do not show:

- Enter, unless this row is actually a Team member.
- Team member lifecycle actions.

### 6.4 Output Handling

Worker output should prefer concise final result and transcript pointers:

```text
summary
output_file / child_session_id
tokens / tools / duration
```

The full worker transcript should be available through Inspect, not expanded inline by default.

## 7. Segment 3: Workflow

Workflow is a deterministic pipeline expression, even if leaves are implemented by Sub-agents.

### 7.1 Meaning

Dynamic Workflow represents:

- A run.
- A step graph.
- Leaf calls.
- Gates and wait states.
- Repair/resume/cancel/promotion controls.

Workflow leaves must not be duplicated into the top-level Workers segment by default. They belong inside the workflow run unless the user drills in.

### 7.2 UI Shape

```text
Workflow
ABS diligence workflow                         running
Plan ✓  ->  Fetch ✓  ->  Review running  ->  Gate waiting

Actions: Open  Resume  Repair  Cancel

Leaves
  CLO source review       completed  detail
  ABS tranche verifier    running    detail
```

### 7.3 Row Actions

Workflow root:

- Open run.
- Resume.
- Repair.
- Cancel.
- Promote, only when eligible.

Step/leaf:

- Detail.
- Enter only if `enterable && childSessionId`.

### 7.4 Empty State

```text
No Workflow in this session.
Workflow appears when the agent uses a deterministic multi-step run.
```

## 8. Segment 4: Activity

Activity is the operational event stream.

### 8.1 Meaning

Activity absorbs items that should not compete with Team / Workers / Workflow:

- Completion notifications.
- Background-agent status.
- Generic runtime runs.
- Raw runtime events.
- Debug details.

### 8.2 UI Shape

```text
Activity
10:41 Sub-agent completed: critic finished review
10:42 Workflow gate waiting: approval needed
10:43 Background check completed

[Show raw events]
```

### 8.3 Raw Events

Raw should not be a default peer tab for normal users. It should be:

- Hidden behind `Show raw events`.
- Visible by default only in debug/admin/developer mode.
- Searchable/copyable when opened.

## 9. Summary Strip

The lower zone needs a compact status strip above the segments.

```text
main idle · running 0 · waiting 0 · blocked 0
```

If something is active:

```text
main running · Team 2 · Workers 1 · Workflow waiting
```

Rules:

- Do not show a separate metrics card by default.
- Metrics should live in the strip or inside selected segment rows.
- Token/tool/duration aggregates are secondary; they should not consume a full card unless the user opens details.

## 10. Default Segment Selection

Default selected segment should follow what is operationally most relevant:

1. If a Workflow is running/waiting/blocked, select `Workflow`.
2. Else if an Agent Team member is running/waiting, select `Team`.
3. Else if a Sub-agent/worker is running, select `Workers`.
4. Else if there are pending notifications, select `Activity`.
5. Else select the last user-selected segment for this session.
6. Else select `Team` if there is a Team, otherwise `Workers`, otherwise `Activity`.

## 11. Data Contract

The right rail should consume the existing session workbench read model, but with stricter presentation semantics.

### 11.1 Existing Inputs

```text
sessionWorkbench.runtime_sections.agent_teams
sessionWorkbench.runtime_sections.subagents
sessionWorkbench.runtime_sections.workflows
sessionWorkbench.runtime_sections.background
sessionWorkbench.runtime_sections.notifications
sessionWorkbench.runtime_sections.runs
sessionWorkbench.runtime_sections.raw
```

### 11.2 Presentation Rules

| Input | Segment | Rule |
| --- | --- | --- |
| `agent_teams` | Team | Render team container and member rows |
| `team_member` | Team | Enterable session window |
| `subagent` | Workers | Inspect/Continue, not Enter |
| `workflow` | Workflow | Pipeline/run graph |
| `workflow_step` | Workflow | Nested under workflow |
| `workflow_leaf` | Workflow | Nested under workflow; only enter if explicitly enterable |
| `background` | Activity by default | Promote only if productized as Worker later |
| `notifications` | Activity | Event stream |
| `runs` | Activity | Debug/diagnostic rows |
| `raw` | Activity raw drawer | Hidden by default |

### 11.3 Required Item Flags

Sub-agent / worker rows:

```json
{
  "runtime_kind": "subagent",
  "child_session_id": "...",
  "enterable": false,
  "inspectable": true,
  "continuable": true
}
```

Team member rows:

```json
{
  "runtime_kind": "team_member",
  "child_session_id": "...",
  "enterable": true
}
```

Workflow leaf rows:

```json
{
  "runtime_kind": "workflow_leaf",
  "child_session_id": "...",
  "enterable": false
}
```

Only an explicit `enterable: true` plus `child_session_id` should create an Enter action for workflow leaves.

## 12. Non-goals

This right-rail redesign does not:

- Redesign the left navigation.
- Redesign the center transcript.
- Replace Dynamic Workflow runtime.
- Merge Workflow with Sub-agent.
- Turn Sub-agent into Team member.
- Add a new A2A process graph.
- Change memory semantics.
- Change permission/governance semantics.
- Show every debug surface to normal users by default.

## 13. Implementation Order

Use tests first for logic changes.

### Step 1: Model Layer

Update `timelineModel.ts` to expose a product-level lower console model:

```text
RuntimeConsoleModel
├─ summary
├─ selectedSegment
├─ team
├─ workers
├─ workflow
└─ activity
```

Tests:

- Agent Team member remains enterable.
- Sub-agent remains continuable/inspectable but not enterable.
- Workflow leaf stays nested under workflow.
- Raw events move under Activity.
- Running Workflow becomes default selected segment.

### Step 2: Component Layer

Update `AgentChatSection.tsx` right rail:

- Keep Session artifacts upper zone.
- Replace lower card stack with segmented console.
- Add mode-specific renderers:
  - `RuntimeTeamPanel`
  - `RuntimeWorkersPanel`
  - `RuntimeWorkflowPanel`
  - `RuntimeActivityPanel`

Tests:

- No `Agent Team / Sub-agent` combined label.
- No always-visible Raw card for normal state.
- Team row has Enter.
- Worker row has Inspect/Continue and no Enter.
- Workflow root opens Workflow Run Window.

### Step 3: CSS / Layout

Update right rail CSS:

- Dynamic upper/lower heights.
- Internal scroll boundaries.
- Fixed compact row heights.
- Stable text overflow.
- Narrow width action collapse.

Tests:

- Rendered markup keeps right rail sections.
- Text does not require artifact count repetition.
- Collapsed groups remain accessible.

### Step 4: i18n

Add or revise strings in both English and Chinese:

- Team
- Workers
- Workflow
- Activity
- Inspect
- Continue
- View all
- Show raw events

### Step 5: Verification

Minimum targeted commands:

```bash
cd frontend
npm run test -- src/pages/session-workbench/timelineModel.test.ts
npm run test -- src/pages/agent-detail/AgentDetailSections.test.tsx
npm run build
```

If implementation touches backend projection:

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_session_control_plane.py tests/services/test_agent_session_continuation.py -q
ruff check app/services/session_control_plane.py
```

## 14. Acceptance Criteria

The redesign is accepted only when all of these are true:

1. Upper zone shows current session artifacts first and does not display delivery counts like `x2`.
2. Historical and unattributed artifacts are collapsed by default.
3. Upper zone does not consume the whole right rail when there are many files.
4. Lower zone uses Team / Workers / Workflow / Activity segments.
5. Agent Team and Sub-agent are not visually or semantically merged.
6. Agent Team member rows are enterable.
7. Sub-agent rows are inspectable/continuable but not enterable.
8. Workflow rows use pipeline/run graph expression, not a flat worker list.
9. Workflow leaves stay nested under Workflow unless explicitly opened.
10. Raw/debug events are not a first-level normal-user card.
11. Empty states do not take more vertical space than live runtime rows.
12. Metrics are compact and do not compete with controls.
13. The right rail remains usable between 360px and 560px width.
14. The center chat remains readable when the right rail is open.

## 15. Final Target

The final right rail should feel like an operations console for the current session:

```text
Top: what this session delivered.
Bottom: how this session is being operated.
```

The key product distinction is:

```text
Team is who is collaborating.
Workers are temporary helpers.
Workflow is the deterministic process.
Activity is what happened.
```
