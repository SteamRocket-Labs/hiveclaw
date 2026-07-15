# CCPlus North Star Contract

Date: 2026-06-24
Status: canonical boundary contract
Scope: CC semantic baseline, Codex engineering delta, Hive-native evolution, local CLI parity, and remote proprietary exclusion

## 0. One-Line Rule

CC is the capability and lifecycle semantic baseline. Codex may improve engineering control and observability, but must not redefine or shrink CC capability boundaries. This is a semantic floor, not a line-by-line implementation template; every adoption remains subordinate to Hive's AI-Native Design Law and Model Agency Boundary.

Hive becomes CCPlus by:

```text
CC / FreeCode runtime semantics
+ Python server-side multi-tenant execution substrate
+ selected Codex engineering controls
+ Hive-native Memory / Iter / Hermes-style self-evolution
+ company-grade governance and control plane
= CCPlus / Hive agent infrastructure
```

### 0.1 Decision Order and Anti-Convergence Rule

When a parity, governance, reliability, cost, or UX proposal conflicts with another rule, decide in this order:

```text
product dual North Star, with single-agent intelligence built and judged first
-> AI-native model capability and Model Agency Boundary
-> CC / FreeCode lifecycle and capability semantics
-> Codex additive engineering and desktop UX improvements
-> Hive-native self-evolution and enterprise control plane
-> atomic proof and complete delivery
```

The goal is capability-preserving determinism: make authority, external effects, typed state, evidence, idempotency, and recovery predictable. Do not obtain determinism by making model behavior easier for the platform to predict. Deleting tools, starving context or output, hard-deciding semantics with natural-language heuristics, and replacing model-authored output are anti-parity regressions even when they make tests simpler.

## 1. Final Product Definition

Hive has two non-negotiable goals:

1. Build the strongest controllable digital employee: a single agent whose intelligence, memory, skill growth, reliability, and safety boundary are at least as strong as the `hermes-agent` internal benchmark and keep improving over time.
2. Build the company control plane for operating those agents at scale: identity, permission, budget, audit, coordination, observability, lifecycle, and governance.

These goals are coupled. A strong agent without company governance is not Hive. A control plane around weak agents is also not Hive.

## 2. Source Priority

For CCPlus parity decisions, use local sources in this order:

1. `/Users/rocky243/vc-saas/free-code-main` as the runnable CC / FreeCode semantic baseline.
2. `/Users/rocky243/Context Engineering/claw-code/src` as Python-port reference only, not a full parity source.
3. `/Users/rocky243/Context Engineering/claw-code/rust` for session hygiene, workspace partition, JSONL rotation, resume, fork, and compact lessons.
4. `/Users/rocky243/Context Engineering/claude-code-org` as a cross-check against FreeCode.
5. `/Users/rocky243/Context Engineering/codex/codex-rs` as Codex delta only.

If sources conflict, CC / FreeCode wins for lifecycle and capability semantics. It does not authorize copying an implementation accident, prompt constant, provider habit, or model-capability restriction. Codex wins only where it is a non-conflicting engineering or interaction improvement, and both remain subordinate to the AI-native and Model Agency laws.

## 3. Scope Matrix

| Source class | Rule | Examples |
| --- | --- | --- |
| CC / FreeCode local runtime semantics | Must implement or map into Hive | session loop, transcript append, tool loop, hooks, Plan Mode, AgentTool/subagent, Team, Skill, command layer, resume/rewind/fork/compact, local workspace behavior |
| CC provider-hosted / S-Work / CCR / Ant-only remote capability | Not required for CC parity | UltraPlan remote session, Claude Code on the web execution, proprietary remote task service, first-party hosted planning/execution |
| Codex local engineering controls | Adopt when they improve control without changing or reducing CC boundaries | typed thread/turn events, discoverable deferred-tool transport, approval reviewers, sandbox policy, model-authored structured plan/checklist surfaces, workbench observability |
| Hive-native evolution and governance | Preserve as deliberate non-parity | T0/T2/T3/soul, Memory Gate, Platform Gate, Iter, Skill evolution, company control plane, RLS, principal-aware memory/action boundaries |

## 4. Local CLI Parity Rule

Do not exclude a feature merely because it appears in a local CLI.

If a CC local CLI feature is implemented through local process, filesystem, workspace, session, transcript, tool, sandbox, hook, or terminal-state semantics, it is in CCPlus scope. Hive may translate the interaction surface from TUI into Web UI, API, RuntimeTask, ChatSession, T0, or Session Workbench, but the underlying semantics remain in scope.

Examples:

- A terminal approval dialog becomes a Web approval card or API confirmation.
- A local plan file becomes a governed plan artifact plus session event.
- A local transcript path becomes transactional `ChatTranscriptEvent` cloud run truth plus exactly-once T0 `events.jsonl` and Markdown Memory evidence projections; T0 is not a second run-ordering authority.
- A local command or workspace operation becomes a governed tool/runtime call.
- A local runner becomes Hive Bridge / Local Agent Channel when the process lives on the user's machine.

The only exclusion is provider-hosted or proprietary remote infrastructure. When a feature requires a vendor remote session, first-party cloud worker, CCR/S-Work service, or inaccessible server-side capability, it is not a CC parity requirement. Hive may later build a Hive-native replacement, but it must be named as Hive-native, not CC parity.

## 5. UltraPlan Boundary

UltraPlan is not a normal local Plan Mode feature in the observed FreeCode source. It is a remote planning path:

```text
local CLI
  -> launchUltraplan
  -> teleportToRemote
  -> CCR / Claude Code on the web session
  -> pollForApprovedExitPlanMode
```

Therefore:

1. Do not reproduce UltraPlan as a required CC parity layer.
2. Do reproduce the local Plan Mode semantics around plan approval, plan file, edited plan handoff, context strategy, permission strategy, and optional team hint.
3. If Hive later wants an "advanced remote planning" feature, design it as a Hive-native remote workstation / agent-team workflow, not as hidden CC parity debt.

## 6. Codex Delta Rule

Codex strengths are valuable only in the engineering/control layer:

- clearer typed thread and turn surfaces
- typed runtime notifications
- deferred/dynamic tool transport and discovery that remain genuinely reachable through the real capability surface
- granular approval and reviewer routing
- sandbox and permission profile controls
- structured checklist/progress surfaces
- better Session Workbench observability
- fork/rollback/read/list controls

These improvements optimize transport, presentation, and control ownership. They must not use task wording, keywords, coordinator labels, fixed top-k limits, or hidden routing to remove tools, delegation, authorized evidence, or the user's selected model. A deferred tool is valid only when its discovery path is visible to the model, governed, recoverable, and equivalent in capability to eager exposure.

Codex Desktop is the UI/UX benchmark for restraint, state continuity, progressive disclosure, and deliverable-first interaction. This does not mean exposing Codex internals to ordinary users: typed runtime evidence powers the product, while raw IDs, schemas, provider payloads, and forensic metadata stay operator-only or explicitly expanded.

Codex must not change:

- when CC would expose a tool
- whether Plan Mode automatically becomes Agent Team
- when subagent vs team is agent-triggerable
- what counts as CC lifecycle parity
- what is considered a deliberate Hive-native non-parity layer

## 7. Conflict Resolution

Use this order for decisions:

1. Does the current FreeCode source define a lifecycle or capability semantic boundary?
   - If yes, preserve that observable boundary without assuming its exact implementation is normative.
2. Does Codex offer a better engineering/control implementation that preserves the boundary?
   - If yes, adopt it as a CCPlus improvement.
3. Does Hive need an enterprise or self-evolution capability beyond CC?
   - If yes, mark it as Hive-native and keep it outside CC parity claims.
4. Does a feature require a proprietary remote service?
   - If yes, exclude it from CC parity and optionally design a Hive-native replacement.

## 8. Atomic Capability Boundary

Every capability must keep its own boundary:

- Plan Mode: confirmation and planning boundary, not execution orchestration.
- Workflow: deterministic typed state transition, gate, retry, idempotency, and recovery control flow, not platform ownership of semantic judgment and not Plan Mode or Subagent.
- Subagent / AgentTool: lightweight delegated worker/session, not Agent Team by default.
- Agent Team: explicit or tool-driven collaboration container, not hidden Plan Mode handoff.
- Hooks: lifecycle interception with typed inputs, outputs, and real runtime consumers.
- Schedule / Trigger: start condition, not execution controller.
- Goal: objective/budget/progress contract, not a hidden task runner.
- Work Ledger: agent-authored cognitive task board, not execution trigger.
- Local Agent Channel: IM-like session between Hive Cloud and a user's local agent runtime.
- Memory / Iter: Hive-native self-evolution layer, not CC parity.
- Personal Knowledge Base: governed tool-only knowledge source, never a raw-context preload; Enterprise Knowledge extends the knowledge-tool authority plane instead of impersonating it through legacy files.

## 9. Documentation Discipline

When a review says "aligned with CC", it must name the exact CC semantic source and the Hive mapping.

When a review says "CCPlus", it must separate:

```text
CC semantic parity
Codex engineering/control delta
Hive-native enterprise/evolution delta
Remote proprietary exclusion
```

Any old document that says "local CLI is out of scope" must be corrected. The correct rule is:

```text
Local CLI semantics are in scope.
Provider-hosted proprietary remote capabilities are out of CC parity scope.
```

Any old document that treats compact pointers, deterministic checks, fail-closed behavior, or direct memory as blanket authority must also be read through the current Model Agency Boundary:

```text
Pointers must be lossless, discoverable, coverage-accounted, and recoverable.
Deterministic checks govern exact facts and durable effects, not open semantic judgment.
Fail-closed freezes only the affected unauthorized or unprovable effect.
No durable Memory, Soul, or Skill write bypasses the governed commit path.
```
