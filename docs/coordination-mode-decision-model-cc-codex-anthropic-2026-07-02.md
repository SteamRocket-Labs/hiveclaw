# Coordination Mode Decision Model: Plan Mode, Sub-agent, Agent Team, Dynamic Workflow

Date: 2026-07-02

Scope: explain the current Hive failure mode and define the repair model for Session pollution, Plan Mode open questions, Sub-agent, Agent Team, Workflow, and Plan Mode dispatch.

## Executive Conclusion

The current product confusion is not caused by a shared workspace alone. CC and Codex also allow multiple sessions to share a repository or inherit context, but they prevent pollution through active-instruction boundaries, addressed message queues, per-session task/session topology, and UI surfaces that do not collapse different runtime identities into one bucket.

Hive currently has parts of those boundaries in the backend, but the full product contract is inconsistent:

1. Plan Mode is treated as if a confirmable plan may still contain blocking open questions.
2. Workflow is sometimes perceived as the route for Plan Mode, Sub-agent, or Agent Team, even though the tool contracts say otherwise.
3. Agent Team is implemented as a Team container plus teammate child sessions, but the UI still collapses it with Sub-agent and does not expose the expected enterable session workbench well enough.
4. Dynamic Workflow leaf calls are synchronous workflow leaf work, not enterable Agent Team member sessions.
5. Session pollution comes from missing active-session/current-artifact boundaries and overly broad inherited workspace/history interpretation, not from lack of workspace isolation.

## Source Findings

### Hive Current Checkout

Plan Mode:

- `backend/app/services/plan_mode_core.py` has no auto-entry mode: Plan Mode is explicit or recommended only.
- `backend/app/tools/handlers/plan_mode.py` tells the model to call `ask_user_question` before `exit_plan_mode` if a blocking decision is open.
- `backend/app/services/plan_mode_system_run.py` repeats that missing high-impact decisions should use `ask_user_question` before finalizing.
- However, `exit_plan_mode` still persists `open_questions` into `plan_json`, and `frontend/src/pages/agent-detail/PlanCard.tsx` allows `awaiting_confirmation` plans with `open_questions` to be confirmed.

Workflow:

- `backend/app/tools/handlers/workflow.py` says `start_workflow` should be used only when fixed step order, mandatory mid-run gates, or large budgeted fan-out are required.
- The same tool description says one-off parallelism or isolation should use `spawn_subagent`, and it says not to enter Plan Mode unless the user explicitly asks.

Sub-agent and Agent Team:

- `backend/app/tools/handlers/subagent.py` describes `spawn_subagent` as a session-local worker and separately says Agent Team uses `team_create` plus `spawn_subagent(team_name + name)`.
- `backend/app/services/agent_team_runtime_service.py` states Agent Team is the CC AgentTool teammate branch in Hive runtime terms: `team_create` creates a session-local container; `spawn_subagent(team_name + name)` creates addressable teammate child sessions.
- `backend/app/services/agent_team_runtime_service.py` rejects TeamCreate with inline members: "TeamCreate creates the Team container only; spawn teammates with spawn_subagent team_name + name".
- `frontend/src/pages/agent-detail/AgentChatSection.tsx` currently groups the panel title as "Agent Team / Sub-agent", which is too ambiguous for the runtime distinction.

Dynamic Workflow:

- `docs/ccplus-session-tui-unified-expression-plan-2026-06-28.md` already says Dynamic Workflow root is `RuntimeTask(task_type="workflow")`; its leaf calls are workflow step/leaf records, currently without `child_session_id`.
- Therefore a Dynamic Workflow leaf should render as workflow leaf detail, not as an enterable Agent Team member session.

### CC / FreeCode Local Source

FreeCode keeps these runtime concepts distinct:

- `src/Task.ts` has separate task types: `local_agent`, `in_process_teammate`, and `local_workflow`.
- `src/tools/AgentTool/AgentTool.tsx` routes `team_name + name` to `spawnTeammate(...)`; otherwise it uses the subagent/local-agent path.
- `src/tasks/InProcessTeammateTask/types.ts` stores teammate identity with `agentName`, `teamName`, `planModeRequired`, `parentSessionId`, mailbox-like pending user messages, and lifecycle fields.
- `src/commands.ts` and `src/tasks.ts` gate workflow command/task behind `WORKFLOW_SCRIPTS`, so workflow is not a default daily path.
- `src/query.ts` drains a shared queue by address: main drains unaddressed messages, subagents drain only task notifications addressed to their own `agentId`, and subagents never see the user prompt stream.

This is the key anti-pollution pattern: shared queue/workspace is acceptable only when active messages and notifications are addressed to the correct session/agent.

### Codex Local Source

Codex also keeps planning and agent collaboration distinct:

- `codex-rs/core/gpt-5.2-codex_prompt.md` says to skip the planning tool for straightforward tasks and not to create single-step plans.
- `codex-rs/collaboration-mode-templates/templates/plan.md` says Plan Mode is a collaboration mode; it can request user input and eventually issue a proposed plan. It is not the same thing as the TODO/progress plan tool.
- The same Plan Mode template says a final plan must be decision complete and should not leave decisions for the implementer.
- `codex-rs/core/src/session/turn.rs` parses `<proposed_plan>` into `PlanDelta` and `TurnItem::Plan`; this is a rendering/approval surface, not a workflow executor.
- `codex-rs/core/src/session/tests.rs` rejects automatic idle input while in Plan Mode.
- `codex-rs/tui/src/app/side.rs` inserts a hidden side-conversation boundary: inherited history is reference only, and only messages after the boundary are active.
- `codex-rs/agent-graph-store/src/store.rs` has a storage-neutral parent/child topology for spawned agent threads.

This is the other anti-pollution pattern: a session may inherit context, but inherited context is reference-only unless a new active instruction says otherwise.

### Public / Official References

Anthropic Claude Code docs now explicitly split the modes:

- "Run agents in parallel" compares Subagents, Agent view, Agent Teams, and Dynamic Workflows. It says subagents are delegated workers inside one session, Agent Teams are multiple coordinated sessions with shared tasks and inter-agent messaging, and Dynamic Workflows are scripts that run many subagents and cross-check results.
- "Agent teams" says Agent Teams are experimental and disabled by default; they add coordination overhead and token cost, and work best when teammates can operate independently.
- "Dynamic workflows" says a workflow moves the plan into code/script: with subagents, skills, and teams, Claude remains the turn-by-turn orchestrator; with a workflow, the script holds loop/branch/intermediate results.
- Anthropic's multi-agent research system article says multi-agent works best for breadth-first, high-value, heavily parallel research, but agents use about 4x chat tokens and multi-agent systems about 15x chat tokens. It also says many coding tasks are less suitable because they have fewer truly parallelizable subtasks and many dependencies.
- LangChain's multi-agent guide says not every complex task requires multi-agent; a single agent with the right tools and prompt can often achieve similar results. It separates subagents, handoffs, skills, routers, and custom workflows.

## Correct Concept Boundaries

### 1. Single-Agent Default

Default path when the user does not ask for Sub-agent, Agent Team, Workflow, or background delegation.

Use when:

- The task can be handled by one agent within reasonable context.
- The work is sequential or tightly coupled.
- The user did not ask for parallelism.
- Subtasks touch the same files or require a single coherent edit loop.

Do not escalate just because the task sounds "important".

### 2. Sub-agent

Lightweight session-local worker. It can have a separate context window and return a summary/result to the parent. It is good for focused side work.

Use when:

- The user explicitly asks for subagents.
- The parent needs 1-5 independent research, verification, or review shards.
- The parent wants to keep noisy file reads/search logs out of the main context.
- Only the final finding matters; direct user interaction with each worker is not required.

Do not use when:

- Workers need to message each other.
- The user expects named teammates they can enter and steer.
- The work requires a durable shared task list and member lifecycle.
- A deterministic phase/gate/retry script is needed.

### 3. Agent Team

Enterable multi-session collaboration. A lead coordinates named teammates; teammates have independent context windows, can be addressed, and should appear as child sessions.

Use when:

- The user explicitly says Agent Team or asks for a named team/roles.
- The task benefits from sustained multi-turn collaboration, competing hypotheses, or cross-layer ownership.
- The user needs to enter or redirect individual teammates.
- Teammates need to share findings, challenge each other, or coordinate beyond one summary return.

Do not use when:

- A quick focused worker can return one summary.
- Workers do not need to communicate.
- The task is tightly sequential or same-file dependent.

### 4. Dynamic Workflow

Heavy harness orchestration. It moves coordination out of the model's turn-by-turn discretion and into a workflow definition/script with phases, leaves, gates, retries, budgets, and verification.

Use when:

- The user explicitly asks for workflow / Dynamic Workflow / ultracode-like orchestration.
- Step order itself is the requirement.
- Mandatory gates, wait/resume, retries, budget caps, or audit trails are required.
- Fan-out is larger than a handful of subagents, especially if results need cross-checking.
- The job is repeatable and worth saving as a reusable workflow.

Do not use when:

- The user only needs 2-5 subagents.
- The agent can coordinate turn by turn without losing state.
- The task is a normal implementation/debugging loop.
- The user asked for Agent Team; Agent Team is not a workflow unless the user explicitly asks to run the team through a workflow harness.

### 5. Plan Mode

Approval and decision-completion boundary. Plan Mode is not a workflow, not an Agent Team, and not a subagent dispatcher by itself.

Use when:

- The user explicitly asks to plan first or enters Plan Mode.
- The task would cause external side effects, autonomous recurring behavior, expensive/long background work, production mutations, or high-risk irreversible action.
- Product logic recommends planning, but the user must still opt in unless a separate hard security gate blocks execution.

Do not use Plan Mode as:

- A default wrapper around Agent Team.
- A default wrapper around Dynamic Workflow.
- A substitute for asking blocking clarifying questions.
- A place to produce a confirmable plan that still has unanswered blocking questions.

## Dispatch Policy

The runtime and prompt should follow this ladder:

1. If the user says "just do it" and does not mention multi-agent/workflow/team: single-agent execution.
2. If the user says "use subagent(s)": use `spawn_subagent`; do not use Workflow unless the user separately asks for workflow or the task meets the high workflow admission threshold.
3. If the user says "use Agent Team": create or use an Agent Team and spawn named teammates; do not silently replace it with plain subagents.
4. If the user says "use workflow" or "Dynamic Workflow": propose/preview/start workflow after explicit approval.
5. If the user says "multi-agent" without specifying Team/Sub-agent/Workflow:
   - default to Sub-agent for 1-5 independent one-shot shards;
   - ask a short clarification if the expected UX depends on enterable teammates vs summary-only workers;
   - use Agent Team only if named roles, peer communication, or enterability is needed;
   - use Workflow only if admission criteria are met.
6. If Plan Mode is active and a blocking decision remains: call `ask_user_question`; do not call `exit_plan_mode`.
7. If Plan Mode produces `open_questions`, the plan is not confirmable unless every question is non-blocking and explicitly recorded as an assumption.

## Session Pollution Root Cause

The problem is not simply "same workspace". CC and Codex can share a checkout without semantic pollution because they preserve active-task identity.

Hive needs these invariants:

1. Every active run, child session, teammate, subagent, and workflow leaf must carry `session_id`, `root_session_id`, `parent_session_id`, `runtime_task_id`, and owner surface where applicable.
2. Inherited transcript/history/workspace files are reference context unless addressed by the current session's active instruction.
3. Tool notifications must be addressed to the intended agent/session. Main should not drain child-only events; children should not drain main user prompts.
4. Artifacts shown in a session must be scoped by current session/run/turn provenance, not just by "same workspace has this file".
5. Workspace files can be shared, but "current deliverable" must come from the session artifact manifest or explicit user selection, not from arbitrary recent filenames.
6. If a session resumes/forks/branches, pre-boundary instructions and old plan artifacts must be marked reference-only unless the user explicitly reactivates them.

This directly explains the ABS pollution case: if one session scans shared workspace files and treats another session's ABS artifact or plan as current work without active-session provenance, it will import the wrong topic. Isolation by workspace would hide the symptom, but the real fix is provenance and active-instruction scoping.

## Plan Mode Open Questions Repair

Current failure:

- `exit_plan_mode` can persist `open_questions`.
- `PlanCard` displays them but still allows "Implement this plan".
- The user sees a plan plus unresolved questions but only gets approval/revision actions, not a first-class answer flow.

Correct behavior:

1. Blocking questions must be asked through `ask_user_question` before `exit_plan_mode`.
2. `PlanCard` must treat required `open_questions` as `needs_clarification`, not `awaiting_confirmation`.
3. Confirm/Implement must be disabled while required questions are unanswered.
4. The UI must render answer controls under each question and submit them as a revision/clarification payload.
5. `open_questions` should become typed objects, not strings:

```ts
type PlanOpenQuestion = {
  id: string;
  question: string;
  required: boolean;
  answerType: 'text' | 'single_choice' | 'multi_choice';
  options?: Array<{ label: string; description?: string }>;
  answer?: string | string[] | null;
};
```

6. If the model includes non-blocking unknowns, they should be displayed under Assumptions/Risks, not as unanswered open questions blocking execution.

## UI Repair

1. Rename the right panel bucket from "Agent Team / Sub-agent" to separate sections:
   - Runtime Agents
   - Agent Teams
   - Subagents
   - Workflows
2. Agent Team rows must be clickable child session windows with member status, role, last activity, token/tool counts, mailbox state, and enter/resume/close affordances.
3. Subagents should show worker status and result summary; if they have `child_session_id`, allow viewing filtered transcript; otherwise show result detail only.
4. Workflow runs should show workflow root, phase tree, leaf table, gate/wait/resume controls, and verifier/cross-check state. Leaf rows are not Agent Team members unless a `child_session_id` is present.
5. Parent transcript should keep compact markers only. It should not inline every child transcript or every workflow leaf as if it were the main session.
6. The to-do/progress ledger must stream during work, not appear only at terminal completion. Frontend should subscribe to ledger deltas or runtime events and update checklist state incrementally.

## Backend / Prompt Repair

1. Add a single coordination-mode decision prompt section to the main runtime prompt:
   - single-agent default;
   - subagent for focused shards;
   - Agent Team for enterable named teammates;
   - Workflow only for high-threshold orchestration;
   - Plan Mode only as explicit/approval/decision boundary.
2. Add a guardrail in Agent Team intent handling:
   - if user explicitly requested Agent Team, plain `spawn_subagent` without `team_name + name` should be flagged as a wrong-lane action unless the agent first created/selected a Team container.
3. Tighten Plan Mode:
   - reject or fail `exit_plan_mode` if `open_questions` contains required/blocking questions;
   - ask with `ask_user_question` first.
4. Add session artifact manifests:
   - every delivered file/artifact records `session_id`, `runtime_task_id`, `trace_id`, `created_by_agent_kind`, and `current_for_session` where applicable.
5. Add active-boundary injection for fork/resume/side/session switches:
   - inherited history is reference-only;
   - only new messages after the boundary are active instructions.
6. Ensure addressed notification draining:
   - main session drains main/user events;
   - subagent drains only child-addressed task notifications;
   - Agent Team member drains only its mailbox/member messages;
   - workflow root consumes workflow events, not child user prompts.

## Tests To Add

1. Plan Mode:
   - `exit_plan_mode` rejects required blocking open questions.
   - `ask_user_question` keeps Plan Mode active and does not land a confirmable PlanCard.
   - `PlanCard` disables Implement when required questions are unanswered.
   - answering questions submits revision payload and regenerates/updates the plan.
2. Dispatch:
   - explicit "use Agent Team" produces team container + teammate spawn lane, not plain subagent lane.
   - explicit "use subagents" does not start workflow.
   - explicit "use workflow" goes through preview before start.
   - unspecified ordinary task stays single-agent.
3. Session pollution:
   - child session does not receive parent user prompt stream.
   - parent does not drain child-only task notification as active instruction.
   - inherited/forked context before boundary is reference-only.
   - artifact panel only marks current-session artifacts as current deliverables.
4. UI:
   - Runtime panel separates Agent Team, Subagents, and Workflow.
   - workflow leaf without `child_session_id` renders detail only, no Enter Session action.
   - Agent Team member with `chat_session_id` switches center session window.

## Final Product Rule

Do not bind Plan Mode, Agent Team, and Dynamic Workflow by default.

Correct default:

- No explicit mode: single-agent.
- Explicit Sub-agent: light subagent fanout.
- Explicit Agent Team: enterable team sessions.
- Explicit Workflow or high admission threshold: Dynamic Workflow.
- Explicit/high-risk Plan Mode: decision-complete plan first; if questions remain, ask before final plan.

