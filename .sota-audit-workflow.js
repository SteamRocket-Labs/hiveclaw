export const meta = {
  name: 'hive-sota-atomic-audit-live',
  description: 'Fresh atomic SOTA audit of Hive vs hive-sota-master-goal.md G1-G15 with LIVE verification (run pytest, grep live wiring, read local CC/Hermes/Codex), adversarially verified, synthesized to a 95%-confidence verdict',
  phases: [
    { title: 'Investigate', detail: 'one auditor per goal G1-G15 + one cross-cutting hunter; run tests, grep wiring, read reference repos' },
    { title: 'Verify', detail: 'adversarial refutation of each goal verdict; independently re-run key checks' },
    { title: 'Synthesize', detail: 'aggregate into the 95%-confidence overall answer + per-goal matrix + cross-cutting + vs existing audit' },
  ],
}

const CTX = `You are auditing the Hive multi-agent platform against its canonical SOTA goal doc. This is an ATOMIC, EVIDENCE-FIRST audit. LIVE verification (actually running things) is mandatory and is what separates this from a code-reading audit.

REPOS ON DISK (read freely with Read/Grep/Bash):
- Hive (the subject): /Users/rocky243/vc-saas/hiveclaw-main  (HEAD 4a02e2be, branch main)
  - backend (Python): /Users/rocky243/vc-saas/hiveclaw-main/backend  (venv at backend/.venv)
  - goal doc (the ONLY goal source): docs/hive-sota-master-goal.md
  - existing CODE-READ audit from today to confirm/refute/refine: docs/sota-atomic-system-audit-2026-06-15.md
- CC / Claude Code (reference): /Users/rocky243/vc-saas/free-code-main  (HEAD 7dc15d6, TypeScript, src/)
- Hermes (reference -- the lean benchmark Hive MUST meet/beat per Goal 1): /Users/rocky243/vc-saas/hermes-agent  (HEAD 75643a615, Python, agent/)
- Codex (reference): /Users/rocky243/Context Engineering/codex  (HEAD 9f4fac8ec4, Rust in codex-rs/, TS in codex-cli/sdk/)

HOW TO RUN LIVE CHECKS:
- pytest must be one shell command (shell state does not persist): cd /Users/rocky243/vc-saas/hiveclaw-main/backend && source .venv/bin/activate && pytest <specific test files> -q
  Target SPECIFIC files, never the whole suite. If a named test file does not exist, grep tests/ to find the right one.
  If a pytest run shows errors that look like resource contention (DB locked / port in use / connection refused) -- other auditors may be running tests in parallel -- re-run it ONCE; if it still fails, capture the real error.
- grep: ALWAYS quote glob args in zsh, e.g. grep -rn 'pattern' app --include='*.py'  (unquoted --include=*.py fails in zsh).
- "live wiring" test: a mechanism is only LIVE if it is CALLED from a production runtime path (kernel/engine.py, runtime/invoker.py, tools/service.py, web-chat runtime, daemons). If it is only called from tests/eval/distiller/canary, it is DARK. Grep the call sites and classify.

CONFIRMED FACTS (verified inline already -- you may rely on these, but re-confirm if central to your goal):
- backend/app/evals/baselines/core_behavior_v1.json: provisional=true, commit_sha="pending-e2-live-run", all 6 scenario score_p50=0.0, transport="pending". The live behavior baseline is NOT real.
- All 4 repo HEADs match the existing audit, so reference code is unchanged since it was written.

JUDGING RULES (from the goal doc, non-negotiable):
- achieved = current code path + test + deploy/production evidence, mechanism reachable in production.
- near = main path wired, but missing live behavior eval / production timing / full connector coverage / external-standard completeness.
- partial = mechanism exists but significant gap or only replay-safe lane.
- not_achieved = absent, doc-only, fixture-only, or key path bypassable.
- A "built but never called from a live path" arm (dark) is NOT achieved no matter how green its tests are.
- Do NOT claim Hive overall-exceeds SOTA before external live behavior eval exists. Be honest and specific. Evidence must be concrete: file:line, exact command, exact output snippet.`

const GOALS = [
  { id: 'G1', title: '治理化运行时自进化',
    mechanisms: 'learning brain, skill candidate loop, patch-first distiller, skill_guard, T3 counters, session feedback',
    gap: 'live behavior eval score trend; stronger sleep-time memory edit; contradiction reconciliation',
    refs: 'Hermes patch-first (read agent/ skill lifecycle + RELEASE_v*.md auto-benchmark notes); DGM/SEAL/AZR/Voyager/Reflexion/AlphaEvolve external-evaluator principle',
    live: 'grep -rn record_skill_execution app --include=\'*.py\' -- is it called from a LIVE path (kernel/tool runtime/web-chat) or only eval/distiller/canary (dark)? ; inspect decide_behavior_gated_promotion + RuntimeConfig: is behavior_report EVER set on a live invocation? if never -> promotion arm is permanently hold (dark) and distiller promotes zero skills; is save_skill the only live skill-write path and does it only run a static scan (= self-authorization bypass)? ; run pytest tests/services/test_skill_distiller.py tests/services/test_evolution_verification.py -q ; check evolution_daemon/heartbeat: does promotion actually fire in production?' },
  { id: 'G2', title: '外部硬验证门',
    mechanisms: 'skill_guard, behavior gate, artifact gate, CI fail-closed',
    gap: 'more programmable evaluators; human-review promotion contract',
    refs: 'Voyager exec gate, AlphaEvolve evaluator (external GitHub)',
    live: 'run pytest tests/evals/test_hive_live_runner.py -q ; baseline is provisional (confirmed) so the gate has NO real passing score -- confirm behavior_eval_passed() only proves fallback/partial/untrusted cannot pass, not that a real score exists ; read .github/workflows/harness-ci.yml -- is nightly behavior eval fail-closed (non-zero exit + evidence when secrets missing)? ; can a single PR change grader+expected_hash together to self-pass (DGM-style self-attack)? look for CODEOWNERS / protected trusted-root' },
  { id: 'G3', title: '长期记忆 SOTA',
    mechanisms: 'governed write gate, activation context, principal stripping, memory hygiene',
    gap: 'bi-temporal KG, multi-hop/PPR, TTL, reference re-validation, conflict ledger',
    refs: 'Letta/MemGPT, Zep/Graphiti bi-temporal KG (external GitHub), Codex memory pipeline (codex-rs), Hermes external memory provider (agent/)',
    live: 'run targeted pytest on memory write gate/activation/retriever (grep tests/ for the files) ; grep for bi-temporal/conflict ledger/TTL/reference-revalidation -- do these EXIST in code or only in docs? ; confirm write_gate PL4 credential reject is live ; read Hermes agent/ memory + Codex codex-rs memory consolidation to state the concrete delta' },
  { id: 'G4', title: 'Durable execution',
    mechanisms: 'RuntimeTask, web-chat resume, workflow completion side-effect dedup, orphan reconcile',
    gap: 'mutating delegation/subagent step-level journal replay; Temporal-like completion boundary full coverage',
    refs: 'Temporal, LangGraph checkpoint (external GitHub)',
    live: 'run pytest tests/runtime/test_workflow_restart_resume.py tests/runtime/test_workflow_worker_lease.py -q ; grep subagent_run_service for which subagent/delegation types are replay-safe vs reconciliation-only (mutating lane is NOT full durable replay) ; confirm web-chat disconnect is a subscription change not a cancellation' },
  { id: 'G5', title: 'Workflow 确定性编排',
    mechanisms: 'WorkflowEngine, ephemeral/registered definitions, workflow_steps/workflow_leaf_calls, run quotas, gate/wait/resume, trigger integration, workflow-native Deep Research',
    gap: 'workflow live/product eval; more promote/fork evidence; cross-worker operational strength',
    refs: 'Temporal workflow/activity, LangGraph graph runtime, CC workflow baseline (does free-code-main even have a workflow primitive? check)',
    live: 'run pytest tests/services/test_workflow_runtime_service.py tests/runtime/test_workflow_gate_step.py tests/runtime/test_workflow_wait_until.py -q ; confirm WorkflowEngine is a zero-DB interpreter with step/leaf journal + quota ; check trigger.config.workflow_ref version/hash binding' },
  { id: 'G6', title: 'Plan Mode / confirmed autonomy',
    mechanisms: 'agent-authored plan, ask-user clarification, plan hash/version, confirmed handoff, readonly planning boundary',
    gap: 'plan-mode live UX quality; scheduled/monitoring opt-out audit; unified gate for all autonomous entries',
    refs: 'CC Plan Mode -- read the actual impl in free-code-main src/',
    live: 'run pytest tests/services/test_plan_mode_core.py tests/kernel/test_plan_mode_reminder.py -q ; verify plan content is agent-authored (plan_markdown becomes the plan body), NOT a structured-fill skeleton; clarification is first-class (not stuffed into assumptions); unconfirmed high-risk/autonomous work does NOT execute -- trace the gate' },
  { id: 'G7', title: 'Subagent / Delegation',
    mechanisms: 'spawn_subagent, peer delegation, worker profiles, tenant-scoped definitions/memory, coordination signals, replay-safe resume',
    gap: 'mutating subagent/delegation step journal; live multi-agent behavior eval; subagent memory evolution',
    refs: 'CC Agent tool (free-code-main src/), Codex subagents/thread (codex-rs), Hermes delegate_task (agent/) incl. default-deny subagent memory/execute/recursive-delegate',
    live: 'run pytest tests/services/test_subagent_run_service.py tests/kernel/test_subagent_memory_isolation.py -q ; confirm mutating lane is fail-closed reconciliation vs replay-safe explorer/critic ; check subagent uses source=subagent and is excluded from parent T2 leakage' },
  { id: 'G8', title: 'Agent TodoList / Work Ledger / Progress Ledger',
    mechanisms: 'track_todo, record_finding, read_ledger, todo owner/dependencies, Progress Ledger review, needs_replan reminder',
    gap: 'UI/agent default visibility; complex-task completion/replan quality; compaction-reboot ledger restore',
    refs: 'CC Task/Todo (free-code-main), Magentic-One Task/Progress Ledger',
    live: 'run pytest tests/tools/handlers/test_work_ledger_handler.py tests/services/test_agent_work_ledger.py tests/kernel/test_work_ledger_scaffold.py -q ; CRITICAL: confirm track_todo(add) is cognitive-only -- writing a todo MUST NOT start execution. grep the handler to prove no execute path is triggered' },
  { id: 'G9', title: '企业身份与控制面 / RLS',
    mechanisms: 'agent sponsor, participant, soft-delete lifecycle, RLS, access guards',
    gap: 'external directory CA, access packages, A2A agent-to-agent audit',
    refs: 'MS Entra Agent ID, Glean (external)',
    live: 'run pytest tests/services/test_audit_rls_coverage.py tests/api/test_rls_bypass_audit.py -q ; HIGHEST-PRIORITY BLIND SPOT: is RLS actually ENFORCED at the DB at runtime, or does the owner/superuser connection BYPASS it? read core/database.py (get_db, tenant_scoped_session, enter_rls_bypass) and db bootstrap; how many tables have FORCE RLS; is a non-owner DB role actually used in production or does prod connect as owner (bypassing RLS)? cross-check docs/rls-stage3-cutover.md claims vs actual code -- was cutover really done?' },
  { id: 'G10', title: '权限感知数据面 (connector ACL)',
    mechanisms: 'runtime memory/knowledge principal prefilter, connector ACL mirror choke point',
    gap: 'Feishu/Drive/Office full source ACL ingest; post-generation recheck',
    refs: 'Glean permissions-aware retrieval (external)',
    live: 'run pytest tests/services/test_connector_acl.py tests/services/test_knowledge_inject.py -q ; determine which connectors (Feishu/Drive/Office) actually have authoritative ACL ingest + prompt prefilter + generation recheck vs which are stubs/partial -- grep the connector + ingest code' },
  { id: 'G11', title: '安全执行隔离 (sandbox)',
    mechanisms: 'services/code_execution provider selector, local OS sandbox, vercel_sandbox provider, sandbox probe, MCP authz',
    gap: 'continuous production microVM probe artifact/trend; credential egress proxy',
    refs: 'Codex OS sandbox (codex-rs exec/seatbelt/landlock -- read it), Vercel Sandbox, E2B Firecracker (external)',
    live: 'run pytest tests/services/test_vercel_code_execution.py tests/services/test_code_execution_probe.py tests/services/test_mcp_authz.py -q ; grep for ANY raw subprocess fallback in tool handlers that bypasses the sandbox (grep -rn subprocess app/tools --include=\'*.py\') ; is there a PERSISTED latest probe sample in system_settings, or only the script? ; read Codex codex-rs sandbox to state the delta' },
  { id: 'G12', title: 'Context/cache/tool economy',
    mechanisms: 'CJK-aware token estimate, canonical last-assistant cache anchor, provider cache hints',
    gap: 'Code-Execution-over-MCP module tree when tool surface explodes',
    refs: 'Claude context engineering, Manus KV-cache, Code-Execution-over-MCP',
    live: 'grep tests/ for token-estimate / cache-anchor / prompt-budget tests and run them ; confirm in kernel/engine.py: CJK-aware token estimate + a stable canonical last-assistant cache anchor (frozen prefix vs dynamic suffix) ; verify proactive+reactive compaction feeds the LLM full visibility (not a mechanical truncated slice)' },
  { id: 'G13', title: '多 agent 编排',
    mechanisms: 'team context, teammate mailbox, Progress Ledger, coordination signals',
    gap: 'live multi-agent eval proving benefit; mutating subagent durable replay',
    refs: 'Magentic-One ledger, Anthropic multi-agent research, Cognition context critique (external)',
    live: 'grep tests/ for multi-agent / coordination / orchestrator tests and run a targeted subset ; confirm team context / teammate mailbox / Progress Ledger reminder / coordination signals exist AND are wired into a live path (agents/coordination.py, agents/orchestrator.py)' },
  { id: 'G14', title: 'Eval / 观测闭环',
    mechanisms: 'invocation_spans PG canonical surface, Prometheus metrics, behavior eval evidence',
    gap: 'Decagon-style 100% QA, live score trend, self-evolution promotion bound to live report',
    refs: 'SWE-bench Verified, tau-bench, GAIA, Decagon Watchtower, OpenAI trace (external)',
    live: 'confirm invocation_spans is the canonical PG trace surface (read the model + record_invocation_span writer; grep live call sites) ; re-confirm baseline provisional => no score trend exists ; run a targeted invocation-span test' },
  { id: 'G15', title: '互操作诚实',
    mechanisms: 'MCP passthrough hard gate, A2A card/profile not_exposed declarations',
    gap: 'real OAuth delegation, RFC 9728/RFC 8707 full MCP resource-server flow',
    refs: 'MCP authz spec, A2A, Okta XAA (external)',
    live: 'run pytest tests/services/test_interoperability.py tests/api/test_interoperability_api.py tests/services/test_mcp_authz.py -q ; confirm MCP rejects URL userinfo/access_token/token passthrough (hard gate, not advisory) ; confirm A2A Agent Card + /interoperability/profile mark unsupported OAuth/JSON-RPC surfaces as not_exposed (no marketing overstatement)' },
]

const INVESTIGATE_SCHEMA = {
  type: 'object',
  required: ['goal','title','verdict','confidence','hive_mechanisms','live_verification','reference_delta','blockers','contradicts_existing_audit','key_evidence'],
  properties: {
    goal: { type: 'string' },
    title: { type: 'string' },
    verdict: { type: 'string', enum: ['achieved','near','partial','not_achieved'] },
    confidence: { type: 'number' },
    hive_mechanisms: { type: 'array', items: { type: 'object', required: ['name','path','live_status','evidence'], properties: {
      name: { type: 'string' }, path: { type: 'string' },
      live_status: { type: 'string', enum: ['live','dark','fixture_only','bypassable','partial','unknown'] },
      evidence: { type: 'string' } } } },
    live_verification: { type: 'array', items: { type: 'object', required: ['check','command','result','conclusion'], properties: {
      check: { type: 'string' }, command: { type: 'string' }, result: { type: 'string' }, conclusion: { type: 'string' } } } },
    reference_delta: { type: 'string' },
    blockers: { type: 'array', items: { type: 'string' } },
    contradicts_existing_audit: { type: 'string' },
    key_evidence: { type: 'array', items: { type: 'string' } },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['goal','original_verdict','holds','adjusted_verdict','adjusted_confidence','refutation_evidence'],
  properties: {
    goal: { type: 'string' },
    original_verdict: { type: 'string' },
    holds: { type: 'boolean' },
    adjusted_verdict: { type: 'string', enum: ['achieved','near','partial','not_achieved'] },
    adjusted_confidence: { type: 'number' },
    refutation_evidence: { type: 'string' },
    notes: { type: 'string' },
  },
}

const CROSS_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: { type: 'array', items: { type: 'object', required: ['title','severity','evidence','affects_goals'], properties: {
      title: { type: 'string' },
      severity: { type: 'string', enum: ['critical','high','medium','low'] },
      evidence: { type: 'string' },
      affects_goals: { type: 'string' },
      is_dark_or_bypass: { type: 'boolean' },
      contradicts_master_goal_claim: { type: 'boolean' } } } },
  },
}

const SYNTH_SCHEMA = {
  type: 'object',
  required: ['overall_answer','overall_achieved_sota','overall_confidence','per_goal','cross_cutting_findings','vs_existing_audit','reference_deltas','unlock_steps','test_run_summary'],
  properties: {
    overall_answer: { type: 'string' },
    overall_achieved_sota: { type: 'boolean' },
    overall_confidence: { type: 'number' },
    per_goal: { type: 'array', items: { type: 'object', required: ['goal','verdict','confidence','blocker'], properties: {
      goal: { type: 'string' }, title: { type: 'string' }, verdict: { type: 'string' }, confidence: { type: 'number' },
      blocker: { type: 'string' }, live_evidence: { type: 'string' } } } },
    cross_cutting_findings: { type: 'array', items: { type: 'object', required: ['title','severity','evidence'], properties: {
      title: { type: 'string' }, severity: { type: 'string' }, evidence: { type: 'string' }, affects_goals: { type: 'string' } } } },
    vs_existing_audit: { type: 'string' },
    reference_deltas: { type: 'object', properties: { cc: { type: 'string' }, hermes: { type: 'string' }, codex: { type: 'string' }, external: { type: 'string' } } },
    unlock_steps: { type: 'array', items: { type: 'string' } },
    test_run_summary: { type: 'string' },
  },
}

function investigatePrompt(g) {
  return CTX + '\n\n=== AUDIT TARGET: ' + g.id + ' — ' + g.title + ' ===\n' +
    'Master-goal mechanisms that must be live: ' + g.mechanisms + '\n' +
    'Master-goal next-layer gap: ' + g.gap + '\n' +
    'Reference SOTA line(s): ' + g.refs + '\n\n' +
    'YOUR LIVE-VERIFICATION TASKS (do them; capture the actual command + actual output):\n' + g.live + '\n\n' +
    'STEPS:\n' +
    '1. Locate Hive mechanisms for ' + g.id + ' (file:line). For EACH, classify live_status: live / dark / fixture_only / bypassable / partial. Grep the real call sites to prove it.\n' +
    '2. Run the live tasks above. Record real pass/fail counts and grep hits. No hand-waving.\n' +
    '3. Read the relevant LOCAL reference repo(s) and state the concrete delta. For external-only SOTA, state the bar; use web/exa only if your verdict hinges on a contested recent claim.\n' +
    '4. Decide verdict + confidence, grounded in LIVE evidence, not code reading. Apply the dark-arm rule.\n' +
    '5. State whether your LIVE findings confirm / refute / refine the existing 06-15 code-read audit for this goal.\n\n' +
    'Return the structured object. key_evidence entries must be concrete file:line or exact command output snippets.'
}

function verifyPrompt(inv) {
  return CTX + '\n\nYou are an ADVERSARIAL verifier. Default to skepticism. An investigator produced this verdict for ' + inv.goal + ':\n\n' +
    JSON.stringify(inv) + '\n\n' +
    'Your job: try to REFUTE it.\n' +
    '- If verdict is achieved/near: hunt for the bypass, the DARK arm (built but never called from a live path), the fixture-only test, the placeholder/provisional value, or a prompt that does not actually constrain behavior. Independently RE-RUN at least one of the cited pytest commands and spot-check at least one cited file:line yourself. If you cannot break it, it holds.\n' +
    '- If verdict is not_achieved/partial: check the investigator was not too harsh -- is there a live path they missed?\n' +
    '- Be concrete: list the exact commands you ran and the file:line you inspected.\n\n' +
    'Return adjusted verdict + whether the original holds + concrete refutation/confirmation evidence.'
}

const CROSS_PROMPT = CTX + '\n\n=== CROSS-CUTTING CONTRADICTION HUNT ===\n' +
  'Ignore single-goal scoring. Hunt for SYSTEM-WIDE issues that contradict the master-goal §5 matrix "必须保住的机制 / 已实装" self-claims. Prioritize these specific hypotheses and VERIFY each with grep/read/pytest:\n' +
  '1. Self-evolution auto-promotion arm is permanently DARK in production: RuntimeConfig never carries a behavior_report on live invocations -> decide_behavior_gated_promotion() always holds -> distiller promotes ZERO skills; record_skill_execution() is never called from a live path; save_skill is the only live skill-write path and only runs a static scan (= self-authorization bypass). PROVE or DISPROVE with call-site greps.\n' +
  '2. RLS runtime DB enforcement: does production connect as the table owner (bypassing RLS)? how many tables actually FORCE RLS? is the non-owner role real in prod? (core/database.py, db bootstrap, audit_rls_coverage).\n' +
  '3. Schema drift: new columns added via entrypoint ALTER patches without alembic migrations; alembic heads single? (grep migrations + entrypoint.sh).\n' +
  '4. Any tool/governance choke-point bypass (a path that reaches a tool handler or LLM without run_tool_governance / invoke_agent).\n' +
  '5. Live behavior eval / score trend genuinely absent (baseline provisional confirmed) -- so any G1/G2/G14 "已实装" claim of working self-evolution effect is overstated.\n' +
  'For each finding give severity, concrete evidence (command + output / file:line), which goals it affects, and whether it contradicts a master-goal claim. Return the findings array.'

function synthPrompt(verified, cross) {
  return CTX + '\n\n=== FINAL SYNTHESIS ===\n' +
    'You have all 15 goal verdicts (post adversarial verification) and the cross-cutting findings. Produce the owner-facing 95%-confidence answer to: "Against docs/hive-sota-master-goal.md, has Hive achieved its SOTA goals?"\n\n' +
    'VERIFIED GOAL RESULTS (investigation + adversarial verification per goal):\n' + JSON.stringify(verified) + '\n\n' +
    'CROSS-CUTTING FINDINGS:\n' + JSON.stringify(cross) + '\n\n' +
    'Produce, grounded ONLY in the live evidence above:\n' +
    '1. overall_answer: a precise, honest 95%-confidence verdict. Be specific about what is SOLID (live+tested) vs PROVISIONAL (gated but no real score) vs DARK (built but uncalled) vs MISSING. Use the goal doc judging rules.\n' +
    '2. overall_achieved_sota (bool) + overall_confidence (number).\n' +
    '3. per_goal: verdict + confidence + one-line blocker + one-line live_evidence summary for each of G1-G15 (use the post-verification adjusted verdict).\n' +
    '4. cross_cutting_findings ranked by severity, especially anything contradicting master-goal §5 "已实装/必须保住" claims (dark promotion arm, RLS DB enforcement, overstated milestones).\n' +
    '5. vs_existing_audit: does LIVE verification CONFIRM, REFUTE, or REFINE the 06-15 code-read audit? Call out specifically where live evidence changed the picture (e.g. a goal the old audit rated "near" that is actually dark, or vice versa).\n' +
    '6. reference_deltas: cc / hermes / codex / external -- the concrete remaining gap vs each.\n' +
    '7. unlock_steps: the minimum that would let Hive honestly claim overall SOTA.\n' +
    '8. test_run_summary: aggregate of which live tests were actually run across all goals and pass/fail.\n' +
    'Honesty over optimism. This is the canonical verdict the owner will act on.'
}

log('Starting Hive SOTA atomic audit (live-verified) over ' + GOALS.length + ' goals + cross-cutting hunt.')

phase('Investigate')
const crossP = agent(CROSS_PROMPT, { label: 'cross-cutting', phase: 'Investigate', schema: CROSS_SCHEMA })
const verifiedP = pipeline(
  GOALS,
  (g) => agent(investigatePrompt(g), { label: 'investigate:' + g.id, phase: 'Investigate', schema: INVESTIGATE_SCHEMA }),
  (inv, g) => {
    if (!inv) return null
    return agent(verifyPrompt(inv), { label: 'verify:' + g.id, phase: 'Verify', schema: VERIFY_SCHEMA })
      .then((v) => ({ investigation: inv, verification: v }))
  },
)

const cross = await crossP
const verified = (await verifiedP).filter(Boolean)
log('Investigation + verification complete: ' + verified.length + '/' + GOALS.length + ' goals returned; ' + (cross && cross.findings ? cross.findings.length : 0) + ' cross-cutting findings.')

phase('Synthesize')
const synthesis = await agent(synthPrompt(verified, cross), { label: 'synthesis', phase: 'Synthesize', schema: SYNTH_SCHEMA })

return { synthesis, cross, verified }
