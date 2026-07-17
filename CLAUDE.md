# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## North Star — Highest-Priority Goal (overrides all other guidance)

Hive exists to be **two things, and every line of code must serve one of them**:

1. **A self-evolving agent infrastructure with enterprise-grade access control** — digital employees that genuinely improve over time (memory, reflection, skill acquisition, soul evolution) while every capability, memory write, and external action stays permission-governed and auditable.
2. **A control plane (控制中台)** for operating those agents at company scale — org/permission management, governance, budgeting, coordination, and observability.

**Quality bar:** the per-agent intelligence and self-evolution must be **at least as good as `hermes-agent`** (internal benchmark at `/Users/rocky243/vc-saas/hermes-agent`) — not merely architecturally grander. A system that *feels* weaker than a lean benchmark agent is a failure of Goal 1, not a success.

**Build order:** Goal 1 (the agent's own intelligence + self-evolution) is the **foundational cornerstone** — it is hardened and judged *first*; the control-plane and agent-to-agent layers build on top of it. When a trade-off is unclear, resolve it in favor of these two goals. Current SOTA target entry: `docs/hive-sota-master-goal.md`; CCPlus boundary contract: `docs/ccplus-north-star-contract-2026-06-24.md`; foundation roadmap: `docs/self-evolution-sota-plan.md`.

## North Star Decision Order — 北极星裁决顺序

When goals, baselines, governance, delivery rules, or historical documents appear to conflict, use this order. Lower layers may strengthen higher layers but may never override them:

1. **Product purpose:** build the strongest controllable self-evolving digital employee and the company control plane that operates it. **Goal 1 is built and judged first**; a sophisticated control plane around a weaker agent is a product failure.
2. **AI-native and Model Agency Boundary:** release the model's intelligence inside an authenticated frame before constraining actions. Semantic judgment, reasoning, synthesis, learning, prioritization, and final expression belong to the LLM. The platform owns authority, data ingress, side effects, explicit resources, machine contracts, evidence, recovery, audit, and durable commit.
3. **CC / FreeCode semantic floor:** preserve the complete local agent lifecycle and capability semantics. FreeCode is a behavior and lifecycle baseline, not a line-by-line implementation template and not authority to copy a bug, vendor habit, prompt constant, or capability restriction.
4. **Codex additive delta:** adopt typed state, approval routing, sandboxing, observability, recoverable workbench state, and desktop interaction improvements only when they preserve or expand the CC capability surface.
5. **Hive-native advantage:** Memory, reflection, Skill evolution, Dynamic context, Local Agent, A2A, Workflow, Knowledge, and governed self-evolution must remain first-class product capabilities rather than being simplified away for parity.
6. **Enterprise governance:** constrain unauthorized data and external effects at the narrowest authoritative boundary. A denial of one effect must not degrade unrelated reasoning or capabilities; denied, unavailable, approval-required, and retryable infrastructure states remain typed and recoverable.
7. **Product consumption:** UI/UX is the operating and evidence-consumption surface of both product goals. Benchmark **Codex Desktop** for clarity and restraint: normal users see intent, progress, decisions, required actions, recovery, and deliverables; raw schemas, IDs, payloads, and forensic evidence remain progressively disclosed or operator-only. Current design authority: `docs/frontend-design-refinement-2026-07-03.md`.
8. **Atomic proof:** architecture and completion claims use the **seven-atom standard**—Input, Authority, Execution, Evidence, Recovery, Consumption, and Acceptance. This proves the intended capability without redefining its semantics.
9. **Delivery discipline:** complete the authorized scope in one pass with tests, migration/backfill when applicable, observability, cleanup, and evidence; KISS removes accidental complexity but never removes required model capability or product closure.

The optimization objective is **capability-preserving determinism**: make external effects, state, evidence, and recovery predictable, **not by making model behavior easier for the platform to predict**. Any design that gains determinism by deleting tools, starving context/output, scanning natural language for hard outcomes, or replacing model-authored semantics is a North Star regression.

### Context and knowledge disclosure

"Complete input visibility" means **complete authorized evidence availability** for the intelligent step: every authorized source in scope is either present or represented by a truthful, discoverable, lossless, and recoverable reference with an explicit coverage ledger. It never means injecting every datastore into the raw prompt.

The **Personal Knowledge Base is tool-only**. It must not be prefetched or statically injected into original context assembly; the agent discovers and reads it through governed `search_personal_kb` / `read_personal_kb`-style tools when relevant. Tool-only disclosure must still be genuinely usable: the capability is discoverable, authorization is explicit, citations are preserved, and denial/unavailability is distinguishable from an empty result. Enterprise Knowledge builds on the governed knowledge-tool plane with organization authority, ACL/RLS, provenance, retention, and audit; it is not simulated through Personal KB or legacy files.

## Reference Baselines — 对照物顺序

Hive is a **claude code Python evolution**, so implementation comparisons must use the current local source baselines in this order:

1. **FreeCode TS runnable baseline**: `/Users/rocky243/vc-saas/free-code-main` — first reference for answering "what is the essential CC runtime semantics?"
2. **claw-code Python port**: `/Users/rocky243/Context Engineering/claw-code/src` — only for Python-port direction and existing port boundaries; not a full parity baseline.
3. **claw-code Rust runtime**: `/Users/rocky243/Context Engineering/claw-code/rust` — reference for session hygiene, workspace partition, JSONL rotation, resume/fork/compact.
4. **claude-code-org TS source**: `/Users/rocky243/Context Engineering/claude-code-org` — cross-check against FreeCode.
5. **Codex Rust**: `/Users/rocky243/Context Engineering/codex/codex-rs` — Codex delta only; it must not override the CC baseline.

If sources conflict, judge CC parity from FreeCode first, use `claude-code-org` only as cross-check, use `claw-code/rust` for low-level runtime/session lessons, and use Codex Rust only for additive deltas that do not conflict with CC semantics.

## CCPlus Boundary Contract — 本地 CLI 与远程独占能力边界

CC / FreeCode is the semantic baseline. Codex may improve engineering control, observability, approval routing, sandboxing, typed thread/turn surfaces, and workbench ergonomics, but it must not redefine CC capability boundaries.

Do **not** exclude a feature merely because it appears in a local CLI. If a CC local CLI feature is implemented through local process, filesystem, workspace, session, transcript, tool, sandbox, hook, or terminal-state semantics, it is in CCPlus scope and should be mapped into Hive's Web UI, API, RuntimeTask, ChatSession, T0, Session Workbench, or Hive Bridge shape.

The exclusion boundary is provider-hosted or proprietary remote infrastructure: S-Work / CCR / Ant-only remote capabilities, Claude Code on the web execution, UltraPlan's remote planning session, or any inaccessible first-party hosted service. Those are not CC parity requirements. If Hive later builds an equivalent, it is a Hive-native replacement, not hidden CC parity debt.

Use this decision order:

1. CC / FreeCode semantic boundary wins.
2. Codex engineering/control improvement may be adopted only if it preserves that boundary.
3. Hive Memory / Iter / Hermes-style self-evolution and company control plane are deliberate Hive-native deltas.
4. Remote proprietary capabilities are excluded from CC parity and must be documented as such.

## Full Lifecycle Parity — 全生命周期对标

“全面对标” means **agent full-lifecycle parity**, not feature-count parity. Compare the whole agent lifecycle: definition, context assembly, accepted user prompt, transcript write, model loop, tool loop, hook boundaries, compaction, stop, resume, subagent, workflow, skill loading, and session close.

The deliberate non-parity delta is Hive's **Memory / Iter self-evolution system**. Memory stays Hive-native: T0/T2/T3/soul, governed write surfaces, Skill evolution, and Iter are not reduced to CC behavior. Everything else must have an explicit CC/Hive mapping before being called aligned.

Context composition must be mapped one-to-one:

- CC `CLAUDE.md` / project instructions → Hive `soul.md` and governed identity/context sections.
- CC skill progressive disclosure → Hive `Skill` capsule plus `load_skill` / `tool_search` split.
- CC task/todo scaffolding → Hive Work Ledger / Progress Ledger.
- CC transcript/session artifacts → Hive T0 raw evidence plus product read models.
- CC hooks/session boundaries → Hive hook events with equivalent blocking/resume semantics.

Current session-middle parity priorities are **Skill, Sub-agent, Workflow, and Hooks**. Their prompts, tool descriptions, and lifecycle events must be source-checked against FreeCode first and kept vendor-neutral. Anthropic/Claude/Codex names may appear as documented baselines, but runtime prompts must not privilege a model vendor or product identity.

## Atomic Completion Standard — 原子化闭环标准

“有 API”“有表”“有页面”都不等于能力已经落地。Every architecture audit, implementation review, refactor, retirement, and completion claim MUST check each capability through these seven atoms:

1. **输入（Input）**：谁发起，输入结构是什么，是否可恢复。
2. **权威（Authority）**：谁有权读取、决定和写入，租户、用户、Agent、代理关系如何绑定。
3. **执行（Execution）**：唯一执行入口是什么，是否可能绕过治理。
4. **证据（Evidence）**：event、span、transcript、文件和数据库中谁是机械事实源。
5. **恢复（Recovery）**：断线、重启、重试、取消、回滚、fork 是否幂等。
6. **消费（Consumption）**：Memory、Skill、Workflow、Knowledge、UI 是否真实使用产物。
7. **验收（Acceptance）**：测试、迁移、回填、故障注入、可观测性是否覆盖。

Use only these completion states:

- **闭环（Closed loop）**：七个原子均有当前真实消费路径。
- **局部闭环（Partial loop）**：主路径成立，但存在双事实源、旁路、恢复或 UI 断点。
- **断点（Breakpoint）**：能力存在，但生产路径在两个原子之间断开。
- **缺失（Missing）**：当前源码无实现；若明确暂不建设，标成“已知缺失”，不得伪装成回归或已完成。
- **排除（Excluded）**：CC/Codex 的服务商私有远程能力，不计入 Hive 的 CC parity 债务，但必须记录排除依据。

Completion status must be supported by current-checkout code paths and verification evidence. Documentation, schemas, routes, or UI shells alone are never sufficient evidence.

**两条必须主动证伪的"假完成"信号（owner 反复咬到的病根，2026-07-17 固化）：**

1. **写了函数 ≠ 接入生产（wiring proof）**：函数/模块/类存在——甚至有调用点、有 `@tool`/handler 注册、有 import——都不等于它在真实生产入口的执行路径上。判"执行（Execution）"原子闭环前，必须 grep 从 live entry（HTTP handler / 前端首屏加载 / daemon lifespan / hook / 工具注册点）一路追到该代码**真被调用**，排除孤儿、deferred、默认 `None` 短路。新写一个 service/函数从来不是接线证据。
2. **绿测试 ≠ 走过真实路径（path proof）**：测试通过只证明被测代码在测试构造的条件下正确，不等于生产真走这条路径。测试可能 pin 了生产永不走的路径、用注入 fake / 手写格式掩盖未接线、或用 `assert x == None` 把缺陷钉成契约；验收环境还可能 Docker-off 把该红的真 PG 测试整体 skip。判"验收（Acceptance）"原子前必须三问：生产入口真接线了吗？fake 是否掩盖 wiring？断言是否钉死 bug？并确认验收跑全（Docker-on / 真 PG）、部署依赖完整（Dockerfile/env）。

违反任一条，该能力仍是"断点/局部闭环"，不得标"闭环"。孤儿检测（每个新模块 trace 到 live entry point）对 daemon↔consumer、前端函数↔首屏路径尤其要查。

## AI-Native Design Law (最高设计法律 — judges every architectural decision)

Hive is an **AI-native system**. Three layers, in strict priority order:

1. **L1 — Unleash the model first.** Any step that requires intelligence (summarization, planning, extraction, synthesis, judgment) belongs to the LLM at full capability: complete authorized evidence availability (inline or through lossless, discoverable, recoverable references; no silent mechanical pruning), sufficient output budget (no starved `max_tokens`), prompts engineered to benchmark (Claude Code) quality — structure, examples, anti-drift constraints. This does not authorize raw-context injection of Personal KB or any other tool-only source. Mechanical/string-based handling of intelligent steps is allowed ONLY as an observable fallback on failure paths, never the primary path. *Case law: compaction once fed the LLM a `[-40:]` truncated slice with a 2500-token output cap and a silent regex fallback — the canonical violation (fixed in `docs/compaction-cc-alignment.md`).*
2. **L2 — Harness constrains, never replaces.** Governance, safety, budgets, audit wrap *above* model capability: they bound what the agent may **do**, not how well it **thinks**. A constraint that degrades model intelligence (instead of scoping authority) is a design bug.
3. **L3 — Hive's identity: a neutral, organization-facing control plane.** Hive is an independent third party with **model equality**: every feature, every constraint, every prompt works equally for every model — no privileged vendor, no model-specific feature gates, no prompt favoritism. On top of that equality Hive adds what no model vendor provides: the company-scoped controllable agent control plane.

**Memory / self-evolution boundary law:** LLM 负责判断、提炼、反思、归纳、候选生成；平台负责证据引用、权限、去重、回滚、审计、最终落盘。Any memory, heartbeat, dream, skill, workflow, or evolution path that replaces model judgment with counters, regexes, truncated summaries, or platform-authored "semantic" text is an AI-native violation. Any path that lets the model bypass governed write surfaces for durable memory/evolution/soul files is a governance violation.

**Memory target form:** Hive memory is an Agent Markdown Wiki / Learning Vault: T0 raw evidence, T2 tagged Markdown Segment Packages under `memory/t2/sessions/**`, a converged T3 semantic layer (`memory/t3/episodes.md`, `user.md`, `worker.md`, `capabilities.md`), source_refs-backed residual evidence verification, and `soul.md`. Skill is a progressive capability capsule grown from T3 capability evidence and eval-backed candidate packages, not a T3 page. `relations`, `contradictions`, graph/vector/search/UI views are derived and rebuildable; no external memory provider may become the T3 source of truth. `memory/indexes/wiki_map.md` is the single generated navigation map, not always-on prompt memory; control sidecars live under `memory/control/`. Current path contract: `docs/memory-vault-path-contract-2026-06-23.md`.

**Cloud runtime truth and T0 evidence truth:** `ChatTranscriptEvent` is the transactional cloud event truth for run ordering, resume, replay, fork, checkpoint, and rollback. Per-segment `memory/t0/sessions/<session_id>/segments/<segment_id>/events.jsonl` is the exactly-once portable Memory evidence projection; same-segment `source.md` is its deterministic human/LLM-readable Markdown/XML projection. T0 remains the canonical raw evidence source for T2/T3 curation, but it is not a second cloud run authority.

**Review lens — apply to every subsystem:** ① Is the LLM's input visibility complete? ② Is its output budget sufficient? ③ Is the prompt engineered to benchmark quality? ④ Does mechanical processing appear only as an observable fallback?

## Model Agency Boundary — 模型语义主权与平台治理边界

**Non-negotiable law:** once authenticated authority, data visibility, side-effect permissions, resource ceilings, and execution isolation have established the frame, the LLM is the sole owner of semantic judgment, reasoning, synthesis, prioritization, and final expression inside that frame. The platform owns mechanical facts and action governance; it does not own the meaning of those facts and must not impersonate the model.

### Hard-constraint allowlist

Platform code may hard-block or mechanically constrain a path only when the decision is grounded in one of these externally verifiable invariants:

1. **Authority and data ingress:** tenant/RLS/principal/delegation binding, source ACL, sensitivity access, credential visibility.
2. **Side effects:** explicit policy, approval/checkpoint, irreversible or externally visible action boundaries.
3. **Execution isolation:** sandbox/provider capability, host-secret isolation, path/transport/protocol safety.
4. **Resources and lifecycle:** provider context window, explicit token/cost/tool-round budgets, cancellation, timeout, cycle/depth limits.
5. **Evidence and recovery:** typed receipts, transcript/span ordering, idempotency, replay, rollback, durable-write commit rules.
6. **Machine contracts:** exact schema, syntax, or protocol validity at an API boundary; failure requests a repair/retry and never authorizes platform-written semantic content.

If a proposed hard gate cannot name one of these invariants and its authoritative fact source, it is not governance. It is a model-capability restriction and requires architecture review before implementation.

### Forbidden implementation patterns

The following are prohibited on any live intelligence path, including session runtime, Plan Mode, compaction, Memory, Soul, Skill, Workflow, A2A/subagent, model routing, and final-answer delivery:

- using keywords, regexes, counters, string similarity, or fixed thresholds to decide semantic truth, task intent, answer correctness, progress, contradiction, importance, or learning value;
- replacing, appending to, or suppressing a model-authored final answer because a natural-language scanner disagrees with it; evidence mismatches may emit audit events or trigger a new evidence-grounded LLM turn, but may not rewrite the answer;
- silently slicing head/tail/prefixes, retaining only the first N candidates, or clearing evidence before an intelligent task sees it merely to fit a convenient budget;
- allowing a mechanical fallback to accept, reject, promote, delete, or rewrite Memory/Soul/Skill/plan semantics when the LLM reviewer is unavailable;
- removing tools or delegation capability based on generic task wording rather than the explicit intersection of principal authority, capability policy, approval state, and resource limits;
- interpreting arbitrary natural-language substrings as permission, approval, or confirmation grants; authority mutations require a structured authenticated action or an explicit anchored command grammar bound to the current object/session;
- secretly downgrading the selected model through heuristic routing, or treating a client-echoed server hash as semantic authority;
- producing platform-authored prose that presents an inferred failure, denial, or contradiction as if it were the model's conclusion.

Tests that assert any forbidden behavior are not evidence that the behavior is correct; they are regression debt and must be reversed before the implementation is changed.

### Required runtime shape

Use this sequence for every agent turn and every background intelligence lane:

1. **Before model input:** establish principal, authorized sources, provenance, and explicit resource limits. Unauthorized bytes do not enter context.
2. **Inside the frame:** give the LLM all authorized evidence, sufficient output budget, the real available capability surface, and benchmark-quality instructions. The LLM decides what the evidence means and what to say or propose.
3. **Before an effect:** enforce capability policy, approval, sandbox, quota, and idempotency. A denied action stays denied without degrading unrelated reasoning.
4. **After execution:** return typed status, receipt/invocation/artifact refs, retryability, and recovery state. The LLM interprets them; the platform persists them.
5. **Before delivery or durable promotion:** enforce only exact authority/secret/write invariants. Prefer preventing unauthorized ingress. If a deterministic final failsafe finds exact forbidden bytes, redact only those bytes or ask the LLM to regenerate from authorized evidence; never replace the whole answer with fixed semantic prose.

### Mechanical fallback contract

A mechanical fallback is allowed only on a clearly identified failure path and must satisfy all of the following:

- it is observable through a typed state, span/event, and metric;
- it preserves the original evidence or a truthful, readable recovery reference;
- it does not create semantic truth and cannot accept/reject/promote/delete/rewrite semantic material;
- its allowed outcomes are limited to abstain, hold/quarantine, retry, degrade, request review, or report a typed infrastructure failure;
- recovery re-enters the LLM-primary path instead of making the fallback permanent.

When a physical context window is the limiting boundary, use model-led compaction first. If one call still cannot cover the input, use complete chunk/map-reduce coverage with source refs and an explicit coverage ledger. Mechanical dropping is only a last-resort provider-failure recovery path; it must be visible and recoverable, never routine preprocessing.

### Mandatory review and TDD gate

Any change touching prompt assembly, context selection, compaction, model routing, loop termination, tool eligibility, delegation, final-answer handling, Memory/Soul/Skill semantics, or Plan Mode must document and test:

1. the exact hard invariant being enforced and its authoritative fact source;
2. whether natural-language content is inspected to produce a hard outcome;
3. whether the same goal can be enforced at data ingress or immediately before the external effect;
4. proof that the LLM sees all authorized evidence and receives a task-sized output budget;
5. proof that model-authored output remains byte-faithful outside exact unauthorized-secret redaction;
6. fallback observability, evidence preservation, and recovery behavior;
7. current-source comparison with FreeCode/CC first and Codex only as an additive engineering baseline.

Required regressions include benign text containing security/tool keywords, decisive evidence at the end of long inputs, nested A2A receipts, unavailable-vs-denied infrastructure states, context recovery after compaction, and preservation of the model's original final answer.

## Delivery Discipline — One Complete Pass, No MVP (交付纪律 — 一次改完，零技术债)

**Owner law (2026-06-08, "必须记住"): any revision/rework round ships as ONE complete pass — no MVP, no phased "first implementation," no technical debt deferred.** Before starting a change, define the *complete* scope up front (tests, edge cases, error paths, schema migration, **legacy-data backfill**, production cleanup, observability) and deliver it in one pass. Forbidden: "ship Phase 0 first," default-off flags hiding half-built work, "add tests later," "skip the migration for now."

*Case law: the agent memory system rotted into dirty, drifting files precisely because P0–P10 took the spec-sanctioned "first implementation can encode inline rather than rewriting every existing bullet" shortcut and never paid it down — accumulating ten debts D1–D10 (`docs/agent-memory-purity-spec.md`). MVP's "later" = never.*

**Only exception:** a genuinely irreversible step (production data migration/deletion) uses a dry-run + confirmation gate — that is a safety gate, not an MVP stage; completeness never waives safety.

## Project Overview

Hive is an open-source **multi-agent collaboration platform** — enterprise "digital employees" with persistent identity, long-term memory, private workspaces, autonomous trigger-driven execution, governed self-evolution, durable web chat runs, Office workbench editing, and owner/company-aware Memory Gate + Platform Gate governance. Built with FastAPI (Python) backend + React 19 (TypeScript) frontend.

**Version:** tracked in `backend/VERSION` and `frontend/VERSION` (currently 1.7.0).

## Railway Deployment Rule — Production Only, Three Services

For Hive production/product deployments, **always deploy all three Railway services**: `backend`, `backend-api`, and `frontend`. A deploy is incomplete if any one of the three services is still on an older deployment.

The retired Railway eval environment is not a default deploy target. Eval now reads production evidence and keeps deterministic gates in CI.

Railway source layout differs by service:

- `backend`: service is configured with `rootDirectory=backend`; upload an archive that preserves a top-level `backend/` directory.
- `backend-api`: service is configured with no root directory and `configFile=/railway.json`; upload from the backend package root itself.
- `frontend`: service is configured with `rootDirectory=frontend`; upload an archive that preserves a top-level `frontend/` directory.

Use this exact pattern from the repo root:

```bash
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
tmp_root=$(mktemp -d /tmp/hiveclaw-railway-upload.XXXXXX)
mkdir -p "$tmp_root/backend-root" "$tmp_root/frontend-root"
git archive --format=tar HEAD backend | tar -xf - -C "$tmp_root/backend-root"
git archive --format=tar HEAD frontend | tar -xf - -C "$tmp_root/frontend-root"

cd "$tmp_root/backend-root"
railway up --service backend --environment production --project "$PROJECT_ID" --detach -m "deploy latest backend production archive-root"

cd "$tmp_root/backend-root/backend"
railway up --service backend-api --environment production --project "$PROJECT_ID" --detach -m "deploy latest backend-api production backend-root"

cd "$tmp_root/frontend-root"
railway up --service frontend --environment production --project "$PROJECT_ID" --detach -m "deploy latest frontend production archive-root"
```

After submit, poll production deployment status until all three targets are `SUCCESS`. `backend-api` is not publicly exposed, so the public backend health URL does not prove `backend-api` freshness by itself; Railway deployment status is the required proof for that service.

```bash
railway deployment list --service backend --environment production --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service backend-api --environment production --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service frontend --environment production --project "$PROJECT_ID" --limit 1 --json

curl -fsS https://backend-production-326d.up.railway.app/api/health
curl -I -fsS https://frontend-production-0346.up.railway.app/
```

## Current Engineering Baseline (2026-06-15)

Before making architecture claims, use the current evidence surface:

- `docs/hive-sota-master-goal.md` — canonical SOTA total goal, target matrix, and future loop-comparison ledger.
- `docs/ccplus-north-star-contract-2026-06-24.md` — canonical CCPlus boundary contract: CC semantic baseline, Codex engineering delta, Hive-native evolution, local CLI parity, and remote proprietary exclusion.
- `docs/runtime-model-agency-constraint-audit-2026-07-13.md` — canonical Model Agency Boundary audit and closure record: C-01 through C-20 findings, CC/FreeCode/Codex comparison, one-pass repairs, and acceptance evidence.
- `docs/harness-engineering-audit-2026-06-11.md` — harness audit, remediation log, and verification evidence.
- `docs/round2-sota-benchmark-2026.md` — second-round SOTA benchmark, detailed comparison sources, and milestone evidence.
- `docs/memory-clean-loop-refactor-plan-2026-06-17.md` — current memory clean-loop redesign and Agent Markdown Wiki / Learning Vault target.
- `docs/memory-system-flow-map-2026-06-17.md` — end-to-end memory flow map, including source_refs-backed residual evidence verification, T3 semantic layer, and capability candidate lanes.
- `docs/memory-vault-path-contract-2026-06-23.md` — current single-agent memory filesystem contract: canonical T0/T2/T3/session_state/index/control paths plus legacy import quarantine semantics.
- `docs/agent-memory-md-first-spec.md` — MD-first memory truth-source contract and lifecycle spec.
- `docs/self-evolution-sota-plan.md` — canonical self-evolution foundation and completed substrate baseline.
- `docs/agent-memory-purity-spec.md` — memory purity, lifecycle, and hygiene contract.

Current closures that must not regress:

- Durable self-evolution promotion requires evidence, verification/eval, rollback metadata, and audit records.
- Web chat and long tasks are `RuntimeTask` backed and restart-resumable; browser disconnects are subscription changes, not cancellation.
- Plan Mode is a first-class confirmation/planning boundary: substantive plan content must be agent-authored, clarification must be first-class, and unconfirmed autonomous/high-risk work must not execute.
- Workflow is a first-class deterministic orchestration substrate parallel to Plan Mode: `RuntimeTask(task_type="workflow")`, workflow step/leaf journals, run quotas, gate/wait/resume, trigger integration, admin ops, and governed auditability.
- Subagent/delegation is a first-class collaboration capability: lightweight workers, peer delegation, fanout, context isolation, result distillation, governed shared tool execution, and replay-safe resume boundaries must remain distinct from Workflow control flow.
- Agent TodoList / Work Ledger / Progress Ledger is the CC Task/Todo-equivalent agent-authored task board: `track_todo` records todos/dependencies, `record_finding` records findings/failures/replan, and `read_ledger` restores state. Writing a todo is cognitive bookkeeping; it must not start execution.
- Skill is a progressive-disclosure capability capsule, not merely a Markdown prompt. A Skill may package instructions, references, templates, scripts, evals, workflow definitions, and subagent definitions; loading a Skill adds context/guidance only. Executable components still run through their governed runtime (`preview_workflow`/`start_workflow`, `spawn_subagent`/`delegate_to_agent`, or approved sandbox/code execution).
- Memory/Self-Evolution target form is Agent Markdown Wiki / Learning Vault: T0 -> T2 -> T3 -> `soul.md` is the durable gradient; residual verification means T3 curation follows T2 `source_refs` back to T0 evidence; Skill may grow from memory evidence, while Workflow remains a separate execution-control system that memory can only reference or hand off evidence to.
- `invocation_spans` is the canonical PostgreSQL trace surface; JSONL spans are compatibility artifacts.
- Provider retry/overload fallback, CJK-aware token estimates, canonical assistant-turn prompt-cache anchors, output-cap telemetry, and Anthropic thinking-signature preservation are runtime contracts.
- Agent-controlled code execution is provider based: local/trusted hosts use the shared OS sandbox builder (`bubblewrap` or `sandbox-exec`), while Railway production uses `HIVE_CODE_EXEC_PROVIDER=vercel_sandbox` and Vercel Sandbox credentials. Never fall back to raw subprocesses.
- MCP authz forbids URL userinfo/token passthrough; A2A Agent Cards and `/interoperability/profile` must mark unsupported OAuth/JSON-RPC surfaces as `not_exposed`.
- Memory hygiene startup repair retires legacy shadow stores and quarantines dead stubs through a reversible shared path.
- Latest full backend evidence before the current documentation-only update: `cd backend && source .venv/bin/activate && pytest tests -q` -> `4223 passed, 7 skipped, 4 warnings`.

## Development Commands

### First-Time Setup
```bash
bash setup.sh           # Production: env, PostgreSQL, backend venv, frontend npm, DB seed
bash setup.sh --dev     # Also installs pytest, ruff, and dev tools
```

### Start/Stop Services
```bash
bash restart.sh         # Stops old processes, starts backend(:8008) + frontend(:3008)
```

### Backend (cd backend/)
```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8008 --reload  # Dev server

ruff check app/ --fix && ruff format app/   # Lint + format

pip install -e ".[dev]"
pytest                                       # All tests
pytest tests/test_foo.py -v                  # Single file
pytest tests/test_foo.py::test_bar -v        # Single case

alembic upgrade head                         # Apply migrations
alembic revision --autogenerate -m "desc"    # New migration
alembic heads                                # Must be single head
python -m app.scripts.repair_memory_hygiene  # Dry-run memory hygiene
python -m app.scripts.repair_memory_hygiene --apply --confirm
```

### Frontend (cd frontend/)
```bash
npm run dev              # Vite dev server on :3008 (proxies /api→:8008, /ws→ws://:8008)
npm run build            # tsc + vite build → dist/
```

### Docker
```bash
cp .env.example .env
docker compose up -d --build    # Full stack → :3008
```

## Architecture

```
Frontend (React 19 + Vite + TanStack Query)
    ↓ /api proxy (:3008 → :8008)
Backend (FastAPI + SQLAlchemy async)
    ↓
PostgreSQL (asyncpg) + Redis
```

### Agent Kernel — The Core Runtime

All agent execution flows through a unified kernel. This is the most important architectural layer.

```
Entry Points (web chat RuntimeTask, Feishu, Slack, DingTalk, WeChat, Teams, Trigger, Heartbeat, Delegation)
    ↓
runtime/invoker.py — invoke_agent() resolves deps, builds prompt, calls kernel
    ↓
kernel/engine.py — AgentKernel.handle() — stateless LLM loop, zero DB deps
    ↓ (injected callbacks via KernelDependencies)
tools/service.py — ToolRuntimeService.execute() — governed tool execution
    ↓
tools/governance.py — security zone → capability gate → approval flow
    ↓
tools/executors/ — core.py, extended.py, integrations.py
```

**Key files:**

| File | Purpose |
|------|---------|
| `kernel/contracts.py` | `InvocationRequest`, `InvocationResult`, `RuntimeConfig` — pure dataclasses |
| `kernel/engine.py` | `AgentKernel` — stateless LLM loop with DI. Context compaction, token budgeting, vision support |
| `runtime/invoker.py` | `invoke_agent()` — wires kernel to platform (DB, tools, memory, prompt). Single entry for ALL paths |
| `runtime/prompt_builder.py` | Assembles system prompt: agent context → knowledge → memory → active runtime tool groups → skill catalog |
| `runtime/session.py` | `SessionContext` — tracks source, channel, active runtime tool groups per invocation |
| `core/execution_context.py` | `ExecutionIdentity` ContextVar — agent_bot vs delegated_user, read by audit |

**Execution flow:** Interactive and delegated agent entry points build an `InvocationRequest` and call `invoke_agent()`. The kernel runs a multi-round LLM loop with streaming callbacks. Round budget: `max_tool_rounds` defaults to **200**. Heartbeat no longer enters the full agent tool loop; it runs the direct T3 core with no tool executor and no tool-round budget semantics. Round-pressure warnings are injected at 80% and with 2 rounds remaining for kernel invocations. Context compaction is **proactive** (≥75% utilization, checked every 3 rounds) + **reactive** (prompt-too-long retries with truncation). Individual tool results >50KB spill to `workspace/logs/.../artifacts/`; per-round aggregate budget is 200K chars. Semantic loop detection is wired via `LoopGuard` over assistant text, tool calls, and tool results; the round cap is the backstop. Invocation, generation, and tool spans are persisted through `record_invocation_span`, and provider behavior is wrapped by retry/overload fallback, output-cap telemetry, prompt-cache anchor stability, and Anthropic thinking-signature preservation.

### Tool System (`app/tools/`)

Tools follow a registry + executor + governance pattern:

| File | Purpose |
|------|---------|
| `runtime.py` | `ToolExecutionRegistry` — name → executor mapping, `try_execute()` |
| `service.py` | `ToolRuntimeService` — wraps governance + execution + timeout + logging |
| `governance.py` | `run_tool_governance()` — 2-layer preflight: security zone → capability gate |
| `governance_resolver.py` | Connects governance to real DB (security_zone, capability policies, approval) |
| `packs.py` | `ToolPackSpec` — static capability bundles (web, feishu, email, etc.) |
| `handlers/` | 17 handler files: filesystem, search, communication, email, feishu, plaza, skills, triggers, hr, mcp, office, memory, plan_mode, subagent, tasks, work_ledger, workflow |
| `workspace.py` | `ensure_workspace()` — bootstraps agent filesystem (soul.md, memory/, skills/, workspace/) |

**100+ registered built-in tool definitions** across categories: file I/O, web search/fetch, Feishu office (docs/wiki/sheets/base/tasks/calendar), OfficeCLI/ONLYOFFICE document workflows, email, messaging, Agent Circle/plaza, triggers, skills, workflows, work ledger, MCP.

### Skill System (`app/skills/`)

Progressive-disclosure capability capsules. `SkillParser` → `WorkspaceSkillLoader` → `SkillRegistry`. Skills load progressively: catalog metadata in prompt, full body via `load_skill`. A folder-based Skill may carry instructions, references, templates, scripts, evals, workflow definitions, and subagent definitions. Loading a Skill does not execute side effects or unlock schemas; workflows still run through `preview_workflow`/`start_workflow`, subagents through `spawn_subagent`/`delegate_to_agent`, scripts through the approved sandbox/code execution path, and missing schemas through `tool_search`.

### Memory System — 4-Layer MD Pyramid + Memory Gate / Platform Gate

MD files are the source of truth; the legacy SQLite/JSON shadow stores are retired and repaired through `memory/hygiene.py`.

```
T0 append-only session ledger
  → T2 Segment Package (summary.md / labels.md / review.md / manifest.json)
  → T3 Consolidation Batch (LLM pitch + Memory Gate review + Platform Gate commit)
  → accepted T3 Markdown (episodes.md / user.md / worker.md / capabilities.md)
  → soul.md (Dream soul reconsolidation)

Explicit user-commanded memories enter memory/explicit/** immediately, then may
be absorbed by the same T3 Consolidation Batch. They are not accepted T3 until
the gate commits an LLM-authored T3 block.
```

**Cadence configuration (P1-W2-5, current)**: the `evolution_daemon` dispatcher
ticks every `HEARTBEAT_TICK_SECONDS` (default 60s). Runnable agents are eligible
on the platform-managed `HEARTBEAT_DEFAULT_INTERVAL_MINUTES` cadence (default
120 minutes), not per-agent UI cadence. Subsequent heartbeat ticks skip when
there are no new T2 entries. Full Dream is gated by `MIN_HOURS_BETWEEN_DREAMS`
(24h) plus either `MIN_SESSIONS_SINCE_DREAM` (3 sessions) or
`MIN_HEARTBEAT_TICKS_SINCE_DREAM` (2 productive heartbeat ticks). Dream handles
soul-level reconsolidation; it does not rewrite accepted T3 files.

**T0 session layout (Claude Code transcript / Codex rollout aligned):**

```
memory/t0/sessions/<chat_session_id>/
  index.json
  segments/
    <segment_id>/
      events.jsonl ← append-only portable Memory evidence records with hash chain
      source.md    ← deterministic Markdown/XML projection: user_message, assistant_message, tool_result, segment_boundary
```

`logs/YYYY-MM-DD/**` is now legacy/import compatibility only. Old chat logs can be imported, but runtime chat, one-off task, trigger, delegation, heartbeat, and dream Memory evidence is projected into the append-only T0 session ledger from committed runtime events.

**T2 Segment Package layout:**

```
memory/t2/sessions/<session_id>/segments/<t2_segment_id>/
  summary.md      ← LLM-authored session/segment summary with XML blocks
  labels.md       ← LLM-authored event labels + quantified engineering labels
  review.md       ← independent Memory Gate review/rubric output
  manifest.json   ← platform evidence refs, source bundle refs, revisions, audit metadata

memory/t2/sessions/<session_id>/episodes/<episode_id>/
  synthesis.md    ← LLM-authored stitched episode for broken/continuing segments
  review.md       ← independent Memory Gate review/rubric output
  manifest.json   ← platform evidence refs, source package refs, revisions, audit metadata
```

`memory/learnings/*.md` is a legacy migration/audit surface only and must not be
used as prompt semantic memory. New T0→T2 semantic truth is the Segment Package
above. Build-time refs may exist under
`memory/.staging/t2_jobs/<job_id>/source_bundle.json`; after commit, evidence
pointers live in `manifest.json` and in-file `source_refs`.

| Layer | Location | Written By | Read By |
|-------|----------|-----------|---------|
| **T0 session ledger** | `memory/t0/sessions/<session_id>/segments/<segment_id>/events.jsonl` + deterministic `source.md` projection | committed `ChatTranscriptEvent` projector; runtime-native Memory evidence hooks for trigger/delegation/heartbeat/dream | Memory evidence replay/export reads JSONL; cloud run resume/replay/fork/rollback reads transactional `ChatTranscriptEvent` first; T2/human review may use projection and source refs |
| **T0 legacy/import logs** | `logs/YYYY-MM-DD/{behavior,system}/` | Legacy import/manual compatibility only; not a runtime T0 writer | legacy import/operators |
| **T2 Segment Package** | `memory/t2/sessions/<session_id>/segments/<t2_segment_id>/{summary.md,labels.md,review.md,manifest.json}` | LLM summary/label agents + independent review; Platform Gate commits package metadata | T3 Consolidator only when complete/standalone; residual T0 evidence lookup |
| **T2 Episode Stitch Package** | `memory/t2/sessions/<session_id>/episodes/<episode_id>/{synthesis.md,review.md,manifest.json}` | Continuity/Episode Stitcher + independent review; Platform Gate commits package metadata | T3 Consolidator for broken/continuing segments after stitching |
| **Explicit Memory Overlay** | `memory/explicit/entries/<explicit_id>.md` + `memory/explicit/manifest.jsonl` + generated `memory/explicit/MEMORY.md` | `save_memory` only for explicit user-commanded memory; write gate enforces sensitivity/privacy | Prompt activation immediately; later T3 absorption candidate |
| **T3 Accepted Memory** | `memory/t3/{episodes.md,user.md,worker.md,capabilities.md}` | T3 Consolidator submits pitch/revised patch; Memory Gate reviews; Platform Gate commits exact accepted XML blocks | Dynamic memory activation and Dream soul evidence |
| **soul.md** | Root workspace | Dream soul reconsolidation through promotion/frozen-mission gates | Prompt injection (frozen prefix) |

The pyramid is the storage and distillation path. Runtime behavior is governed by the owner/company-aware Memory Gate / Platform Gate split:

| Capability | Code paths | Rule |
|------------|------------|------|
| Principal + charter context | `services/agency_charter.py`, `services/principal_context.py` | Agent memory/action decisions must preserve direct owner, company, creator/current user, and delegating agent context when available. |
| Memory write safety | `memory/write_gate.py`, `memory/t2/segment_package.py`, `memory/t3_platform_gate.py`, `memory/explicit_overlay.py`, `tools/handlers/memory.py` | T2/T3 semantic files must be agent-authored candidates reviewed by Memory Gate and committed by Platform Gate. PL4 credentials are rejected; legacy `memory/t2_store.py` is compatibility/repair only. |
| Dynamic activation | `memory/activation.py`, `memory/retriever.py`, `services/memory_service.py`, `runtime/invoker.py` | Prompt memory is activated by objective, owner/company relevance, open-loop pressure, retention/confidence, and sensitivity access. |
| Decision trace + action preflight | `services/action_preflight.py`, `services/decision_trace.py`, `tools/service.py` | External-visible, irreversible, sensitive, or company-conflicting actions must pass preflight before tool execution. |
| Feedback learning | `services/session_feedback.py`, `memory/explicit_overlay.py`, `memory/t2/segment_package.py`, `services/auto_dream.py` | Owner feedback should carry reaction/polarity and link back to `decision/<id>` when possible; explicit feedback enters the overlay immediately and is absorbed into accepted T3 only through the consolidation lane. |
| Session calibration | `services/session_feedback.py`, `models/session_feedback.py`, `api/chat_sessions.py` | Useful/misleading session feedback is persisted and re-enters durable memory only through governed write paths. |
| Memory hygiene | `memory/hygiene.py`, `tools/workspace.py`, `scripts/repair_memory_hygiene.py` | Legacy shadow stores, dead stubs, and missing lifecycle metadata are repaired through reversible shared reports. |
| Coordination runtime | `agents/coordination.py`, `agents/orchestrator.py` | Delegation uses Lease/Signal; confirm-first actions create Checkpoint; Sentinel emits Signal or Checkpoint for trigger-like open loops. |
| Proactive steward loop | `services/heartbeat.py` | Heartbeat may prepare low-risk artifacts; external-visible actions require Checkpoint. |

**Key files:**

| File | Purpose |
|------|---------|
| `memory/t0/ledger.py` | Append-only T0 session ledger writer, sealer, replay, and legacy import |
| `services/t0_logger.py` | Legacy T0 formatter/import compatibility; runtime hooks no longer call it; chat backfill writes the session ledger |
| `memory/t2/segment_package.py` | Canonical T0→T2 builder: sealed T0 segment -> source_bundle.json -> LLM-authored `summary.md` / `labels.md` / `review.md` -> Platform Gate atomic Segment Package commit. |
| `services/extract_agent.py` / `memory/t2_store.py` | Legacy compatibility, admin backfill, and migration/read-model helpers only. They must not be reconnected to runtime canonical T0→T2 hooks. |
| `services/heartbeat.py` | Platform-managed T2→T3 curation and self-evolution tick (KAIROS persistent session, 120min default eligibility, no-new-T2 skip). Loads `templates/HEARTBEAT.md`; per-agent `workspace/HEARTBEAT.md` overrides via `_load_heartbeat_instruction` — **already SOP-driven** |
| `services/auto_dream.py` | T3→soul consolidation (24h plus 3 sessions or 2 productive heartbeat ticks; soft dream is 6h T3-pressure maintenance). Runtime system prompt now loads `templates/DREAM.md` as dream protocol guidance while preserving the JSON-only consolidator contract; durable memory/soul writeback must go through Soul Memory Gate + Platform Soul Gate, not direct `write_file` under `memory/`. |
| `services/evolution_ledger.py` | `evolution_ledger.jsonl` — candidate → eval (with `traces`) → promotion audit chain for automatic prompt/skill/policy changes. Distinct from per-invocation runtime trace. |
| `services/invocation_trace.py` | Per-invocation runtime trace: file-backed JSONL compatibility plus PostgreSQL `invocation_spans` canonical query surface. |
| `services/session_feedback.py` | Persists useful/misleading feedback and writes calibrated memory through governed paths. |
| `memory/hygiene.py` | Retires legacy shadow stores, quarantines dead stubs, and backfills lifecycle metadata with dry-run/apply reports. |
| `memory/retriever.py` | Read T3 into prompt. High-priority files are injected directly where policy allows; knowledge/strategy/user entries are scored against query. |
| `memory/md_store.py` | Maintains Markdown T3 stores and generated `memory/indexes/wiki_map.md`; the map is a navigation artifact, not the primary retriever route. |
| `runtime/hooks_setup.py` | Hook handlers: T0 writers, extraction triggers, drain on close |

### Hook System (`app/runtime/hooks.py`)

Lifecycle bus for memory pipeline and tool governance. The live `HookEvent` enum is a **42-member catalog** (CC-27 superset); of these, 7 are `_DISABLED_NOOP` events with no live emitter yet (SETUP/PERMISSION_DENIED/ELICITATION_RESULT/WORKTREE_CREATE/WORKTREE_REMOVE/CWD_CHANGED/FILE_CHANGED — catalog parity complete, emitter coverage partial; see `hooks.py:195`). The core memory/governance events below are live-emitted:

| Category | Events |
|----------|--------|
| Session | `SESSION_START`, `RESPONSE_COMPLETE`, `SESSION_IDLE`, `SESSION_CLOSE` |
| Tool | `PRE_TOOL_USE`, `POST_TOOL_USE`, `POST_TOOL_FAILURE` |
| Compression | `PRE_COMPACTION`, `POST_COMPACTION` |
| Delegation | `DELEGATION_START`, `DELEGATION_END` |
| Hive-specific | `TRIGGER_END`, `HEARTBEAT_TICK_END`, `DREAM_END` |
| Notification | `MEMORY_EXTRACTED` |

Memory pipeline hooks (registered in `hooks_setup.py`):
- `RESPONSE_COMPLETE` → append accepted runtime evidence into the T0 session ledger and route fast-reflection candidate signals; it must not reconnect the legacy `memory/learnings` hot path
- `PRE_COMPACTION` → preserve evidence before context is summarized away; semantic writes still go through Segment Packages / T3 candidates
- `SESSION_IDLE` → seal or advance T0 session ledger segments without duplicating already-flushed messages
- `SESSION_CLOSE` → finalize the T0 segment and trigger canonical T0→T2 Segment Package construction for eligible user-facing segments

### Prompt Architecture (`app/runtime/prompt_sections/`)

14 modular prompt sections assembled by `prompt_builder.py`:

| Section | Source |
|---------|--------|
| `agent_context.py` | Soul identity + tone/style rules |
| `memory_context.py` | Accepted T3 files (`memory/t3/user.md`, `worker.md`, `episodes.md`, `capabilities.md`) plus explicit overlay when activation policy allows |
| `tasks.py` | Active tasks + verification rules |
| `executing_actions.py` | Tool usage + memory save rules |
| `output_efficiency.py` | Response format and conciseness |

Cache boundary: frozen prefix (soul + memory + tools) + dynamic suffix (tasks + session context).

### HR Agent — Agent Creation Pipeline

HR agent (`hr_agent_template/`) creates new agents through conversational guidance. The creation pipeline includes LLM soul refinement:

```
HR conversation (2-3 rounds) → _refine_soul_inputs() → _render_agent_soul_from_blueprint()
                                    ↓ LLM call                    ↓ Python template
                              Refined: role_description,     Structured soul.md:
                              personality, boundaries,        Identity / Users / Outputs /
                              quality_standards, first_tasks  Style / Quality / Boundaries /
                                                              How I Learn
```

Soul refinement prompt teaches the LLM the full 4-layer architecture, soul-vs-focus boundary, and produces role-specific content with BAD/GOOD examples. Falls back to raw inputs if LLM fails.

### Multi-Agent (`app/agents/`)

`delegate_to_agent()` wraps `invoke_agent()` with `SessionContext(source="agent")`. Peer A2A receives the governed capability intersection, and bounded nested delegation is controlled by inherited authority, depth, cycle, approval, and budget checks rather than by mechanically removing the coordination surface.

### Backend Layout (`backend/app/`)

| Directory | Count | Purpose |
|-----------|-------|---------|
| `models/` | 43 files | SQLAlchemy ORM — all async, tenant-scoped with RLS, including invocation spans and session feedback |
| `services/` | 163 files | Business logic — LLM client, trigger/evolution daemons, channel streaming, memory, office, quota, approval, trace, MCP authz, interoperability |
| `services/agent_tool_domains/` | 21 files | Tool domain implementations — Feishu, messaging, tasks, workspace, email |
| `kernel/` | 3 files | Core engine — invocation loop, contracts, context management |
| `runtime/` | 17 files | Hooks, invoker, prompt builder, prompt sections, context engines, workflow runtime, session context, recovery/coordinator helpers |
| `tools/` | 18 handler modules + registry files | Tool registry, governance, runtime groups, catalog, result envelopes, workspace |
| `skills/` | 5 files | Skill parser, loader, registry |
| `memory/` | 25 files | MD-first: retriever, assembler, md_store (T3), t2_store, write gate, activation, lifecycle, retention, access log, replay corpus, hygiene, optional backends |
| `memory/enhancement.py` | — | Optional enhancement adapter boundary; currently a no-op. Native T3 Markdown remains the only memory backend. |
| `core/` | — | Security, permissions, middleware, Redis pub/sub |
| `migrations/` | 79 versions | Alembic schema evolution |

### Frontend Layout (`frontend/src/`)

| Directory | Purpose |
|-----------|---------|
| `pages/` | 16 page entries + 40 nested page/section helper files — AgentDetail, Agent Circle, Company Admin workbench/settings, Admin |
| `components/` | 9 reusable components — ChannelConfig, FileBrowser, MarkdownRenderer, etc. |
| `api/core/` | HTTP abstraction — `request<T>()` with JWT, error handling, upload progress |
| `stores/` | Zustand — `useAuthStore` (user/token) + `useAppStore` (sidebar/selection) |
| `i18n/` | i18next — `en.json` + `zh.json` (both must be updated for any UI text) |
| `types/` | Core TypeScript interfaces — User, Agent, Task, ChatMessage |
| `surfaces/` | Layout shells — App, Workspace, Admin with role-based guards |

**State:** TanStack React Query 5 for server state; Zustand 5 for UI state.
**Routing:** React Router 7 with lazy loading. Guards: ProtectedRoute, WorkspaceGuard, AdminGuard.
**Path alias:** `@/` maps to `src/`.

## Critical Conventions

### Multi-Tenancy
Every entity is tenant-scoped. All queries filter by `tenant_id`. First registered user becomes platform admin. Use `check_agent_access(db, current_user, agent_id)` before returning agent-scoped data. PostgreSQL RLS policies enforce isolation at DB level.

### Agent Kernel Invariant
All agent execution goes through `invoke_agent()` → `AgentKernel.handle()`. Never call LLM directly from a route handler. The kernel is pure (zero DB imports) — all I/O via `KernelDependencies` callbacks.

### Tool Governance Invariant
All tool execution goes through `ToolRuntimeService.execute()` → `run_tool_governance()`. Never call a tool handler directly without governance checks.

### Code Execution Provider Invariant
Agent-controlled code execution must go through `services/code_execution/`. Local/trusted hosts may use `services/subprocess_sandbox.py`; Railway production must use `HIVE_CODE_EXEC_PROVIDER=vercel_sandbox`. Do not launch raw subprocesses from tool handlers or pass host secrets into agent-controlled environments.

### MCP Authz Invariant
MCP import/execution must go through `services/mcp_authz.py`. URL userinfo, `access_token`, and token passthrough credentials are forbidden; legacy `apiKey` query credentials are normalized to authorization headers.

### Trace Invariant
Runtime evidence lives in append-only invocation spans. Spans must carry tenant, agent, user, runtime task, session, request, trace, span, and parent identifiers where available.

### Interoperability Invariant
A2A/interoperability descriptors are machine-readable contracts. Unsupported OAuth delegation or JSON-RPC task surfaces must remain `not_exposed`.

### Memory Hygiene Invariant
Do not manually edit legacy memory stores or dead stubs as a one-off fix. Use `memory/hygiene.py` or `python -m app.scripts.repair_memory_hygiene` so repairs are reversible and reportable.

### Runtime Tool Groups
Agents start with CORE tools plus the dynamic skill catalog. Deferred runtime tool groups (advanced web/crawl, Feishu, email, imported MCP tools, etc.) are discovered through `tool_search`, not by loading a Skill. Active group state is tracked as runtime tool groups for the current invocation; historical `active_packs` wording is compatibility terminology only.

### Alembic Migrations
- Check `alembic heads` before creating — must be single head
- `entrypoint.sh` applies `ALTER TABLE IF NOT EXISTS` patches for backward compatibility
- `main.py` lifespan runs `create_all` on startup

### i18n
Both `en.json` and `zh.json` must be updated for any UI text. Use `t('key')` from `useTranslation()`.

### Channel Integrations
Feishu/Lark, Discord, Slack, DingTalk, WeChat Work, WeChat Personal, Telegram, Email, Microsoft Teams — each has its own router in `api/` and streaming service or delivery path in `services/`. Channel configs are per-agent unless explicitly tenant-scoped. Feishu supports WebSocket long connections via `feishu_ws.py`.

### Environment Variables
Key vars (see `.env.example`): `DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`, `JWT_SECRET_KEY`, `SECRETS_MASTER_KEY`, `AGENT_DATA_DIR`, `EXA_API_KEY`, `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`, `XCRAWL_API_KEY`, `FEISHU_APP_ID`/`FEISHU_APP_SECRET`, `ONLYOFFICE_DOCS_URL`, `ONLYOFFICE_JWT_SECRET`, `WS_IDLE_TIMEOUT_SECONDS`, `HIVE_CODE_EXEC_PROVIDER`, `VERCEL_TEAM_ID`, `VERCEL_PROJECT_ID`, `VERCEL_TOKEN`.

### Ports
Frontend dev: 3008, Backend dev: 8008, PostgreSQL: 5432, Redis: 6379.

### Ruff
`target-version = "py311"`, `line-length = 120`.

## Design Context

**Design authority: `docs/frontend-design-refinement-2026-07-03.md`** — the single source of truth for all frontend work. Benchmark: Codex Desktop. Core requirement: 克制但是精致 (restrained but refined).

**Users:** Enterprise managers and business teams (non-technical). Interface must be approachable.

**Six principles (§3.1):**
1. **Document feel, not card feel** — separation via spacing → subtle background → hairline → border (last resort)
2. **Narrow type scale** — 11/12/13px carry 90% of the UI; 15px page titles only; 7-step scale cap
3. **Grayscale first, color = state** — status colors only on dots/small text, never large fills
4. **Every clickable element has three states** — hover/focus-visible/active, 120–160ms transitions
5. **High density, relaxed line-height** — tight in-row gaps (4–6px), clear block gaps (12–16px), body line-height 1.6
6. **Shadows belong to overlays only** — menus/popovers/modals; zero shadows on in-surface elements

**Structure:** `src/styles/tokens.css` (token authority: type/spacing/radius/shadow/colors, dark+light) + `src/styles/base.css` (reset/focus-visible/scrollbars/utilities) + `src/styles/primitives.css` (atomic component classes) + `src/components/ui/` (12 atomic components: Button/IconButton/Card/Chip/Badge/Input/Select/Modal/EmptyState/Spinner/Tooltip/SegmentedControl). Gallery at `/design-gallery`.

**Rules:** Static styles go through classes/tokens — inline `style={{}}` only for genuinely dynamic values. New UI uses `components/ui` atoms. System font stack (CJK-harmonized, no webfonts). Vanilla CSS custom properties (no framework), Tabler Icons, 4px spacing base, dark/light via `data-theme`.
