# Hive SOTA 总目标与循环对照总表

> 状态：canonical，2026-06-15 起作为 Hive 以后每轮 SOTA 对照、工程排查、优化闭环和状态更新的第一入口。
>
> 本文不替代详细证据库。详细 benchmark、竞品材料、完成记录和命令证据仍在 `docs/round2-sota-benchmark-2026.md`；第一轮 harness 审计证据在 `docs/harness-engineering-audit-2026-06-11.md`；历史 foundation plan 在 `docs/self-evolution-sota-plan.md`。

## 0. 使用规则

以后任何一轮“是否达到 SOTA / 还差什么 / 要优化什么 / 部署后是否闭环”的判断，都先读本文，再回到代码、测试、生产和 benchmark 原文取证。

循环对照必须按这个顺序执行：

1. 在本文选定目标维度、SOTA 对标线和验收证据。
2. 回到当前代码、提示词、工具面、数据路径、部署配置和生产状态取证；不能只读旧文档。
3. 对每个缺口写 red test 或可复现证据；纯文档整理可跳过 TDD，但要做 docs 校验。
4. 完成实现、迁移、回填、提示词、观测、部署和验证；不能把工程债留成“后续再说”。
5. 把结果更新到本文“循环台账”，并把详细证据放回对应专题文档或新增证据文档。

判断口径：

- “已达成”必须有当前代码路径、测试、部署或生产证据。
- “接近”表示主链路已接通，但缺少 live 行为 eval、生产时序、全连接器覆盖或外部标准完整实现。
- “未达成”表示机制不存在、只在文档里、只在 fixture 里、或关键路径可绕过。
- 做完外部行为 eval 前，不宣称 Hive 已经整体“超越”SOTA。

## 1. 北极星

Hive 的总目标只有两个，所有路线都服务这两个目标：

1. 最强数字员工：单个 agent 的智能、自进化、记忆、技能、可靠性和安全边界，必须至少达到并超过 `hermes-agent` 这条内部 lean benchmark，同时对齐各家公开 SOTA 的最强分维度。
2. 公司级控制中台：在企业内大规模运营这些 agent，覆盖身份、权限、预算、审计、协作、观测、治理和生命周期。

两者必须结合在一起。只做强 agent 但没有公司治理，不是 Hive；只做控制台但 agent 本身弱，也不是 Hive。

## 2. 不可妥协设计律

这些规则优先级高于局部实现便利：

1. AI-native L1：凡是需要智能判断的步骤，主路必须交给完整 LLM 能力，给完整可见输入和足够输出预算。机械规则只能作为可观测 fallback。
2. 外部硬验证：自进化晋升、技能保留、记忆强化和“变强”声明，必须由 agent 改不动的外部硬信号裁决；LLM 自评只能作为辅助。
3. 企业治理包在外层：治理约束限制 agent 能做什么，不能削弱 agent 怎么思考。
4. 多租户和 principal 隔离：用户、owner、company、tenant 看不到的数据，模型也不能在 prompt、retrieval、tool result、memory activation 中看到。
5. 模型平等：所有能力对 OpenAI、Anthropic、Gemini、compatible provider 保持中立；不做 vendor 特权路径。
6. 零基座权重自改：Hive 默认不改 base model 权重。自进化主战场是平台层 memory、skill、policy、prompt 和 evaluation loop；per-tenant LoRA 只能作为隔离、人审、低优先级 R&D。
7. 进化可追溯可回滚：candidate、eval、promotion、rollback、owner feedback、trace 都是一等审计对象。
8. 生产 fail-closed：代码执行、MCP authz、外部凭据、connector ACL、migration 和 eval gate 缺前置条件时失败，不能静默降级到裸跑或绿色跳过。

## 3. Agent 原子能力版图

这一节是以后整理 `docs/` 重复设计稿时的能力分类。Plan Mode、Workflow、Subagent 都是 agent 的原子能力，不应互相吞并；Deep Research、Office、scheduled automation 这类产品能力可以组合这些原子能力，但不能反过来定义它们。

| 原子能力 | 职责边界 | 当前 canonical / 证据文档 | 总目标关系 |
|---|---|---|---|
| Agent Kernel / Session Loop | 单一 `invoke_agent()` 主循环、工具轮次、prompt/cache、provider、runtime context。所有入口都应回到这里。 | `docs/harness-engineering-audit-2026-06-11.md`、`docs/session-loop-cc-alignment-plan.md`、`docs/runtime-guidance-cc-alignment.md` | G4 / G11 的 runtime、trace、retry、cache 和行为 eval 地基。 |
| Plan Mode | 确认边界：能不能开始、是否需要澄清、计划是否由 agent authored、用户确认哪个 plan hash。它不负责执行控制流。 | `docs/plan-mode-design.md`、`docs/plan-mode-agent-authored-planning.md`、`docs/plan-mode-runtime-paradigm.md`、`docs/plan-mode-path-unification.md`、`docs/plan-mode-agent-work-ledger.md` | 必须成为 autonomous / high-risk / long-running work 的一等 gate；不能退化成 schema skeleton 或工具参数填表。 |
| Workflow | 确定性执行控制流：ephemeral/registered definition、step/leaf journal、gate/wait/resume、quota、trigger integration。它不是 Plan Mode，也不是 Multi-Agent 子项。 | `docs/workflow-source-capability.md`、`docs/workflow-ops-runbook.md`、`docs/execution-mode-spectrum.md` | G5；对标 Temporal/LangGraph/Claude Code workflow baseline。 |
| Subagent / Delegation | 协作执行体：轻量 worker spawn、peer delegation、fanout、context isolation、result distillation、governed tool sharing、restart-safe resume。 | `docs/subagent-source-capability.md`、`docs/subagent-evolution-loop.md` | G7 / G10；Multi-Agent 是 subagent/delegation 的组合形态，不等于 Workflow。 |
| Agent TodoList / Work Ledger / Progress Ledger | agent 自己写给自己的 task board / TodoList（对标 CC Task/Todo）：`track_todo` 记录 todo 和依赖，`record_finding` 记录发现/失败/replan，`read_ledger` 读取当前工作状态；Progress Ledger 是基于 Work Ledger 派生的 stall/replan/owner 判断。写 todo 只是认知记录，不会触发执行。 | `docs/agent-task-cognitive-scaffold.md`、`docs/plan-mode-agent-work-ledger.md` | G8 / G10；对标 CC Task/Todo 和 Magentic-One Task/Progress Ledger，并给 Plan Mode/Workflow/Subagent 提供可见进度。 |
| Memory / Self-Evolution | T0/T2/T3/soul、feedback、skill candidate、promotion、rollback、memory hygiene、activation。 | `docs/self-evolution-sota-plan.md`、`docs/agent-memory-md-first-spec.md`、`docs/agent-memory-purity-spec.md`、`docs/agent-memory-research.md`、`docs/owner-steward-agent-memory-design.md` | G1 / G2 / G3；数字员工越用越强的核心。 |
| Skills / MCP / Extension Surface | agent 可扩展能力面：Skill 是渐进式能力胶囊，可打包指令、上下文、模板、脚本、eval、workflow 定义和 subagent 定义；MCP Server 是外部工具/资源连接器；pack/capability 是内部治理概念。Skill 只负责发现、加载和引导，不吞并 Workflow/Subagent/Code Execution 的运行时边界。 | `docs/agent-extension-surface-skill-mcp.md`、`docs/SKILLS_AND_PACKS_V2.md`、`docs/capability-pack-consolidation.md`、`docs/cc-tooling-alignment-and-plugin-system.md` | G2 / G12 / G13；必须保持 user-facing 模型简单、runtime governance fail-closed。 |
| Tools / Action Governance | ToolRuntimeService、capability gate、approval、ActionPreflight、external-visible/sensitive/irreversible action boundary。 | `docs/harness-engineering-audit-2026-06-11.md`、`docs/runtime-guidance-cc-alignment.md` | 所有原子能力的执行咽喉；任何工具旁路都破坏总目标。 |
| Trigger / Autonomy / Scheduling | 什么时候启动 agent/workflow/objective；自动唤醒、周期任务、事件任务和 plan-gated automation。 | `docs/trigger-cc-alignment.md`、`docs/execution-mode-spectrum.md` | Plan Mode 管确认，Workflow 管执行，Trigger 管启动条件；三者不能混层。 |
| Deep Research | 组合能力，不是源能力：Plan Mode gate + Workflow control flow + Subagent fanout + Work Ledger/trace/eval。 | `docs/workflow-source-capability.md`、`docs/external-behavior-eval-ci.md`、`docs/round2-sota-benchmark-2026.md` | 用来证明原子能力组合质量；不能用 Deep Research 的局部补丁替代 Subagent/Workflow 底座。 |
| Office / Document / Multimodal | 文档编辑、转换、阅读、结构化提取、ONLYOFFICE workbench、document conversion。 | `docs/document-conversion-multimodal-design.md`、`docs/workflow-source-capability.md` | 典型 workflow/agent tool 场景；验收要看文件、格式、权限和可恢复执行。 |
| Remote Workstation / Code Execution | agent 远程本地能力、持久浏览器/工作站状态、code execution provider、sandbox/microVM。 | `docs/remote-workstation-runtime.md`、`docs/round2-sota-benchmark-2026.md` | G9；属于 agent 能动性和安全隔离的交界。 |
| Connector / Knowledge / ACL | Feishu/Drive/Office/OpenViking 等外部知识源接入，source ACL ingest、retrieval prefilter、生成后复检。 | `docs/knowledge-container-boundaries.md`、`docs/org-agent-asset-rights-model.md`、`docs/round2-sota-benchmark-2026.md` | G8；对标 Glean，模型看不到 principal 看不到的数据。 |
| Identity / Control Plane / RLS | tenant、agent identity、sponsor、participant、asset rights、RLS、lifecycle、audit。 | `docs/org-agent-asset-rights-model.md`、`docs/rls-stage0-findings.md`、`docs/rls-enforcement-migration-plan.md`、`docs/rls-stage3-cutover.md` | G6；Goal-2 公司级控制中台的身份与权限地基。 |
| Eval / Observability | invocation spans、behavior eval、CI gate、Prometheus、admin trace reader、100% QA 趋势。 | `docs/external-behavior-eval-ci.md`、`docs/agent-framework-cc-sota-atomic-audit-2026-06-15.md`、`docs/harness-engineering-audit-2026-06-11.md` | G11；没有外部行为证据就不能宣称整体超越。 |

归并规则：

- 同一原子能力下可以有多份历史设计稿，但本文表格里的职责边界优先。
- 如果某篇设计稿把 Workflow 写成 Plan Mode 子项、把 Subagent 写成 Deep Research 内部实现、或把 Work Ledger 写成执行源，按本文纠正。
- 后续要整理 `docs/` 时，先按这张表归档或合并重复文档；不要按文件创建时间决定真相源。

## 4. SOTA 对标地图

| 维度 | 当前最强参考线 | Hive 总目标 |
|---|---|---|
| 自进化学习脑 | Claude memory/skills、Letta sleep-time、hermes patch-first、Devin success/failure | 完整模型判断学什么，patch-first 修既有技能，成败对比进入候选，晋升走外部硬验证。 |
| 不退化验证 | Voyager、AlphaEvolve、SEAL/AZR、Reflexion | 验证器在 agent 可改写面之外；单测、执行结果、ground truth、人审和 live eval 决定晋升。 |
| 长期记忆 | Letta sleep-time、Zep/Graphiti 双时态 KG、Copilot Memory TTL/引用校验、ACE counters | T0/T2/T3/soul 保持治理化；补齐矛盾对账、双时态 multi-hop、用前引用校验、TTL、helpful/harmful counters。 |
| 企业数字员工 | Devin、Decagon、Sierra、Agentforce | agent 能真实完成长任务、复用经验、被 QA 和运营闭环约束，而不是后台配置生成器。 |
| 企业控制面 | Microsoft Entra Agent ID、Purview、Google Agent Gateway、Glean permissions-aware retrieval | 一等 agent 身份、sponsor 生命周期、A2A 审计、权限预过滤、预算、审计、策略和观测统一。 |
| Durable execution | Temporal、LangGraph checkpoint、Claude Agent SDK session store | 崩溃恢复不重复已完成外部副作用；LLM/tool completion 成为去重边界；长任务 restart-resumable。 |
| Plan Mode / confirmed autonomy | Claude Code Plan Mode、human-in-the-loop agent systems | 计划内容由 agent authored；系统只做 envelope、权限、hash、确认和 handoff；自主/高风险/长任务必须确认后执行。 |
| Workflow 确定性编排 | Claude Code workflow baseline、Temporal workflow/activity、LangGraph graph runtime | Workflow 是与 Plan Mode 并列的 runtime 底座，不是 Multi-Agent 子项；同一引擎支持 ephemeral/registered definition、step/leaf journal、gate/wait/resume、quota、trigger/office/deep research 调用。 |
| Subagent / Delegation | Claude Code Agent tool、Codex subagents/thread、Anthropic multi-agent research | 轻量 worker 与 peer delegation 分层；context 隔离、fanout、结果蒸馏、治理共享、replay-safe resume 边界清晰。 |
| Agent TodoList / Work Ledger / Progress Ledger | CC Task/Todo、Magentic-One Task/Progress Ledger、Claude Todo/plan artifacts | Work Ledger 就是 Hive 的 agent-authored TodoList / task board：agent 主动写 todo、依赖、发现、失败和验收状态；Progress Ledger 是派生 review，不是第二套 todo。写 todo 不启动执行，stall/replan/owner 信号进入 runtime reminder。 |
| 多 agent 编排 | Magentic-One ledger、Anthropic multi-agent research、Cognition context critique | 并行收集、串行决策；共享完整 traces；Progress Ledger 触发 replan；用 live multi-agent eval 证明收益。 |
| Context/cache/tool economy | Claude context engineering、Manus KV-cache、Code-Execution-over-MCP、tool search | 稳定 cache anchor、CJK-aware token estimate、工具按需加载、prompt budget 可观测且不饿死模型。 |
| 执行隔离 | Codex OS sandbox、Vercel Sandbox、E2B Firecracker | Railway 生产不裸跑 agent 代码；local/trusted 走 OS sandbox，prod 走 microVM provider；凭据不进 agent 环境。 |
| Eval/观测 | SWE-bench Verified、tau-bench、GAIA、Decagon Watchtower、OpenAI trace | invocation spans 是 canonical trace；CI/live eval fail-closed；分数时序证明进步和不退化。 |
| 互操作 | MCP authz、A2A、Okta XAA | 如实暴露已支持标准；未实现 OAuth delegation / JSON-RPC task 时标 `not_exposed`，不营销伪装。 |

## 5. 总目标矩阵

| # | 总目标 | 必须保住的机制 | 下一层缺口 | 达成证据 |
|---|---|---|---|---|
| G1 | 治理化运行时自进化 | learning brain、skill candidate、patch-first、skill_guard、T3 counters、session feedback | live behavior eval 分数时序；更强 sleep-time memory edit；矛盾对账 | tests + evolution ledger + promotion report + live eval artifact |
| G2 | 外部硬验证门 | skill_guard、behavior gate、artifact gate、CI fail-closed | 更多任务子集的可编程 evaluator；人审 promotion contract | red/green tests + gate fail/hold evidence |
| G3 | 长期记忆 SOTA | governed write gate、activation context、principal stripping、memory hygiene、lifecycle sidecar、heartbeat TTL/revalidation/conflict maintenance | 双时态 KG、temporal multi-hop、live recall eval、外部引用重校验生产样本 | retrieval tests + memory sidecar + lifecycle maintenance artifact + live task recall eval |
| G4 | Durable execution | RuntimeTask、web-chat resume、workflow completion side-effect dedup、orphan reconcile、mutating subagent/delegation restart replay journal + reconciliation boundary | Temporal-like per-tool completion boundary 全覆盖；mutating lane 仍需 exactly-once side-effect replay 生产 trace | restart tests + duplicate side-effect tests + replay-journal tests + production traces |
| G5 | Workflow 确定性编排 | `WorkflowEngine`、ephemeral/registered definitions、`workflow_steps`/`workflow_leaf_calls`、run quotas、gate/wait/resume、trigger integration、workflow-native Deep Research | workflow live/product eval；更多 source capability promotion/fork 证据；跨 worker/event bus 运营化强度 | workflow runtime tests + migration/RLS tests + ops metrics + admin journal export |
| G6 | Plan Mode / confirmed autonomy | agent-authored plan、ask-user clarification、plan hash/version、confirmed handoff、readonly planning boundary | plan-mode live UX 质量；scheduled/monitoring opt-out 审计；所有 autonomous entry 的统一 gate 证据 | plan-mode E2E tests + prompt/tool contract tests + production session traces |
| G7 | Subagent / Delegation | `spawn_subagent`、peer delegation、worker profiles、tenant-scoped definitions/memory、coordination signals、replay-safe resume、mutating restart replay journal/reconciliation fail-closed | live multi-agent behavior eval；subagent memory evolution 证据；mutating exactly-once side-effect replay 仍需生产 trace | subagent runtime tests + orchestration traces + replay-journal tests + behavior eval |
| G8 | Agent TodoList / Work Ledger / Progress Ledger | agent-authored todo board：`track_todo`、`record_finding`、`read_ledger`、todo owner/dependencies、Progress Ledger review、needs_replan reminder | UI/agent 默认可见性；复杂任务 completion/replan 质量；compaction reboot 后 ledger 恢复证据；避免把 todo 写入误当执行触发 | work-ledger tests + runtime reminder tests + chat UX evidence |
| G9 | 企业身份与控制面 | agent sponsor、participant、soft-delete lifecycle、RLS、access guards | external directory CA、access packages、A2A agent-to-agent audit | migration + RLS tests + lifecycle tests + audit spans |
| G10 | 权限感知数据面 | runtime memory/knowledge principal prefilter、connector ACL mirror choke point、Feishu/Drive/Office tool-result authoritative source ACL metadata | 全 connector / per-document ACL 生产覆盖；生成后复检的真实 access-denial trace | connector ACL tests + source ACL fixtures + production access denial evidence |
| G11 | 安全执行隔离 | `services/code_execution/` provider selector、local OS sandbox、`vercel_sandbox` provider、sandbox probe latest evidence、MCP authz | 定期生产 probe artifact / score trend；credential egress proxy | sandbox tests + probe JSON / `system_settings` latest evidence + Railway/Vercel deploy evidence |
| G12 | Context/cache 经济 | CJK-aware token estimate、canonical last-assistant cache anchor、provider cache hints | tool surface 爆炸时引入 Code-Execution-over-MCP 模块树 | prompt budget tests + provider payload tests + usage metrics |
| G13 | 多 agent 编排 | team context、teammate mailbox、Progress Ledger、coordination signals | live multi-agent eval 证明复杂任务收益；mutating subagent durable replay | prompt contract tests + behavior eval + trace tree |
| G14 | Eval/观测闭环 | invocation_spans PG canonical surface、Prometheus metrics、behavior eval evidence | Decagon 式 100% QA、live score trend、self-evolution promotion 强绑定 live report | trace API + metrics + CI artifacts + score trend |
| G15 | 互操作诚实 | MCP passthrough hard gate、A2A card/profile `not_exposed` 声明 | 真 OAuth delegation、RFC 9728/RFC 8707 完整 MCP resource-server flow | authz tests + descriptor snapshots + external protocol tests |

## 6. 当前诚实边界

这些不是失败，但不能被写成“已达成 SOTA”：

1. live behavior eval 仍需要真实目标环境、secrets、分数时序和回归趋势，当前 artifact/gate 只能证明不会静默 green skip。
2. Workflow 已是独立 runtime 底座，不是 Multi-Agent 子项；但它仍需要 live/product eval、更多 promote/fork 证据和跨 worker 运营化证据来证明达到 Temporal/LangGraph 级长期稳定性。
3. Plan Mode 目标是 agent-authored planning + confirmation boundary；任何 structured-fill skeleton 或把澄清塞进 assumptions 的路径，都不能算达标。
4. Subagent / delegation 已是原子能力；mutating delegation/subagent 现在有 restart replay journal 和 fail-closed reconciliation boundary，但还不是 Temporal 式 exactly-once 全链路 side-effect replay，仍需生产 restart trace 证明。
5. Agent TodoList / Work Ledger 是 agent 自己写的 todo/task board，不是另一套执行器；`track_todo(add)` 只记录“我要做什么”，不会启动后台任务。Progress Ledger 是基于这个 board 派生的进度 review，不是第二套 todo。它必须可见、可恢复、可触发 replan，但不能被 workflow engine 当控制流。
6. Feishu、Drive、Office 已补 tool-result authoritative source ACL metadata，能把成功读取的来源喂给现有 generated-source ACL choke point；但这还不是 Glean 式全 connector/per-document production ACL coverage，也还缺真实生产 access-denial trace。
7. Vercel Sandbox provider 已接主路径，且已有 `python -m app.scripts.probe_code_execution_sandbox --persist --confirm` 采集 microVM uname、deny-all network、workspace round-trip 并写入 latest system setting；真实 Railway/Vercel 生产样本仍需按部署节奏持续执行并保留 artifact。
8. 长期记忆工程纯净度已改善，TTL discard、revalidation hold、conflict hold 也已接入 heartbeat maintenance；但 Letta/Zep 级矛盾对账、双时态 multi-hop、外部引用 live revalidation 还没有完整行为级证明。
9. A2A/MCP 描述已诚实暴露能力，但完整 OAuth delegation 和 MCP resource-server flow 尚未实现。
10. “整体超越”必须等外部行为 eval 和生产反馈时序支撑；当前最多按单维度说“机制接近 / 已接主链 / 仍需 live 验证”。

## 7. 每轮循环模板

每次新一轮优化或排查，都在本节追加一行，并在必要时更新总目标矩阵。

| 日期 | 轮次/主题 | 对标维度 | 取证范围 | 结论 | 证据入口 |
|---|---|---|---|---|---|
| 2026-06-15 | 总目标文档整理 | 全部 | `round2-sota-benchmark-2026.md`、`docs/README.md`、`AGENTS.md`、`CLAUDE.md` | 新增 canonical 总入口；round2 保留为详细 benchmark/evidence；Claude Code 入口同步 | 本文；`docs/README.md`；`docs/round2-sota-benchmark-2026.md` 顶部定位；`CLAUDE.md` |
| 2026-06-15 | Workflow 目标补显 | Workflow / Durable execution / Multi-Agent | `docs/workflow-source-capability.md`、`docs/workflow-ops-runbook.md`、workflow runtime/API/model/test 路径 | Workflow 是独立确定性编排底座，Multi-Agent 是可选 leaf/fanout 执行器；总目标矩阵显式拆出 G5 | 本文 G5；`docs/workflow-source-capability.md`；`docs/workflow-ops-runbook.md` |
| 2026-06-15 | Agent 原子能力版图 | Plan Mode / Workflow / Subagent / Work Ledger / Extension / Trigger / Office / Remote Workstation / Control Plane | `docs/*.md` 当前设计入口、agent-framework atomic audit、memory quick-pass | 总目标新增原子能力层；Plan Mode、Workflow、Subagent、Work Ledger 明确为 agent 基础能力；Deep Research/Office/Trigger 作为组合或调用方归位 | 本文 §3；`docs/README.md` 原子能力索引 |
| 2026-06-15 | code execution sandbox probe | 执行隔离 / G11 | `backend/app/services/code_execution/*`、`backend/app/scripts/probe_code_execution_sandbox.py`、`backend/tests/services/test_code_execution_probe.py`、Vercel provider tests | 新增可重复生产探针：`microvm_uname`、`network_denied`、`workspace_round_trip` 三项聚合，支持 JSON artifact 和 latest `system_settings` 写入；仍需在真实 Railway/Vercel 环境持续跑样本 | `python -m app.scripts.probe_code_execution_sandbox --persist --confirm`；`pytest tests/services/test_code_execution_probe.py tests/services/test_vercel_code_execution.py -q` |
| 2026-06-15 | 全系统 SOTA 原子化审计 | G1-G15 全部 | `hive-sota-master-goal.md`、当前代码/提示词/tool/runtime/eval 路径、本地 CC/Hermes/Codex、外部 GitHub/官方 SOTA 项目、目标测试 | 95% 置信度：Hive 尚未整体达成 SOTA；机制层大量接主链，但 live behavior baseline 仍 provisional，缺 Hermes live delta、生产 microVM trend、全 connector ACL、mutating subagent replay、Letta/Zep 级 memory proof | `docs/sota-atomic-system-audit-2026-06-15.md` |
| 2026-06-15 | 全系统 SOTA 原子化审计 · Live 实跑版 | G1-G15 全部 | 32-agent live workflow（真跑 ~1,100+ pytest + grep live 接线 + 读本地 CC/Hermes/Codex）+ 主理人独立复核 | 95% 置信度：仍未整体达成；live 验证确认读码版方向并修正 5+1 处（PPR 实为 live、RLS 64 表 FORCE 证伪 owner 绕过、晋升臂暗真因是租户级缺写非缺字段、Teammate Mailbox 默认暗），并纠正本轮 workflow 自身一处过判（skill 遥测有 live 来源 invoker 终态 hook）。核心 Goal-1 命门=自进化晋升臂生产永久 HOLD→晋升零技能，而 Hermes patch-first 真落地 | `docs/sota-atomic-system-audit-2026-06-15-live.md` |
| 2026-06-15 | Goal-1 普通租户晋升臂代码闭环 | G1 / G2 / G14 | `tenant_behavior_eval_publisher`、`skill_distiller` patch/promote 路径、`hive_live_runner` trusted gate、promotion hard gate tests、完整 backend tests | 代码级关闭“普通租户没有 behavior_report writer → distiller 永久 HOLD”的暗臂：候选驱动跑 tenant-local trusted live eval，按 tenant 写 `behavior_eval_latest_report`，只有 passing report 注入 runtime config；失败/缺前置仍 fail-closed。仍未宣称整体 SOTA：还缺生产 live report、rebaseline 和分数时序 | `backend/app/services/tenant_behavior_eval_publisher.py`；`backend/app/services/skill_distiller.py`；`pytest tests -q` -> `4590 passed, 7 skipped` |
| 2026-06-15 | SOTA 缺口 3/5/4 代码级闭环 | G3 / G4 / G7 / G10 | `connector_acl.py`、Feishu doc/drive tool domains、Office tool handler、`memory/lifecycle_maintenance.py`、heartbeat、RuntimeTask replay journal、subagent/delegation resume path | 代码级关闭三项可立即优化缺口：Feishu/Drive/Office 成功读取结果携带 authoritative source ACL metadata；memory lifecycle maintenance 在 heartbeat 中丢弃 expired sketch 并报告 conflict/revalidation hold；mutating subagent/delegation 需要 restart replay journal，否则 fail-closed reconciliation，成功 resume 追加 journal。仍未宣称整体 SOTA：还缺生产 access-denial trace、live recall eval、mutating exactly-once side-effect replay 生产样本 | `pytest tests/services/test_connector_acl.py tests/kernel/test_generated_source_acl.py tests/services/test_feishu_cli_runtime.py tests/services/test_feishu_drive_runtime.py tests/tools/test_office_tools.py -q` -> `28 passed`；`pytest tests/memory/test_lifecycle_state_machine.py tests/memory/test_lifecycle_maintenance.py tests/memory/test_retrieval_pipeline.py tests/services/test_heartbeat.py::test_heartbeat_memory_lifecycle_maintenance_uses_agent_data_dir -q` -> `28 passed`；`pytest tests/services/test_subagent_run_service.py tests/agents/test_orchestrator.py tests/services/test_runtime_task_service.py -q` -> `57 passed` |
| 2026-06-16 | 第三轮全系统 SOTA 原子化审计 | G1-G15 全部 | 41-agent workflow（478 万 token / 1276 工具调用）：G1-G15 取证+对抗复核 pipeline + 7 路 delta 命门深挖 + 3 路横切 + 主理人 3 处独立实跑复核；HEAD `82c60ac7`（领先上轮五提交） | 95% 置信：仍未整体达成（SOLID 0 / near 8 / partial 6，无 achieved；Goal-1 核心验证信号仍 0 真实数据，baseline 自 `3dd14578` 字节级未变）。命门裁决：#1 晋升臂 `82947830` dark→provisional **半修复**（真 delta 非换皮，但生产从未触发一次真晋升）、#2 baseline 全 0.0 **零修复**、#4 Teammate Mailbox 仍 **dark**；新 delta：connector ACL 半修复（Feishu/Office 自指 tautology）、mutating replay journal **换皮恶化**（闸门恒满足→重复副作用，方向退步）、lifecycle maintenance **换皮暗臂**（三 hold writer 零生产调用）。纠正上轮"FORCE 证伪 owner 绕过"为**过度声明**（FORCE 对 superuser 无效；stage-3 cutover 默认未激活 + 无运行时角色断言）+ CLAUDE.md:240 policy_replay doc-drift | `docs/sota-atomic-system-audit-2026-06-16.md` |
| 2026-06-16 | SOTA 修复 Phase 1：mutating restart replay fail-closed | G4 / G7 / G13 | 修改 subagent 与 async delegation restart resume：mutating `worker_safe` / `worker` lane 不再因 `spawn_intent_recorded` journal 自动重放；restart 后进入 `needs_reconciliation`，read-only `explorer` / `review_readonly` 继续兼容恢复 | 代码级关闭 6/16 审计中的“mutating replay journal 换皮恶化”：`spawn_intent_recorded` 只保留为审计意图，不再作为安全重放凭证。仍未宣称 exactly-once：mutating lane 当前正确状态是 fail-closed reconciliation，后续需要 reconciliation UI/API 和真实 side-effect idempotency journal | `cd backend && source .venv/bin/activate && pytest tests/services/test_subagent_run_service.py tests/agents/test_orchestrator.py tests/services/test_runtime_task_service.py -q` -> `57 passed, 4 warnings` |
| 2026-06-16 | SOTA 修复 Phase 2：Teammate Mailbox durable 默认后端 | G5 / G7 / G13 | 将 `COORDINATION_BACKEND` 默认值从 `memory` 改为 `postgres`；新增默认 `gateway_scope(tenant_id=...)` 写入 `CoordinationRepository` 后由 `build_prompt_facing_team_context()` 读出同一 mailbox signal 的测试；旧 in-process workflow signal 测试显式固定 `memory` backend | 代码级关闭默认部署写内存/读 Postgres 的 Teammate Mailbox 裂脑：默认生产路径现在写读同源于 PostgreSQL coordination tables。仍未宣称 multi-agent SOTA：还缺 live multi-agent eval、生产 mailbox trace 和 completion wake chain 全链路样本 | `cd backend && source .venv/bin/activate && pytest tests/agents/test_coordination_wiring.py tests/services/test_agent_team_context.py tests/agents/test_coordination_repository.py tests/services/test_workflow_completion_signal_gateway.py tests/runtime/test_workflow_completion_signal.py tests/agents/test_subagent_async.py tests/services/test_workflow_checkpoint_integration.py -q` -> `38 passed, 4 warnings` |
| 2026-06-16 | SOTA 修复 Phase 3：RLS runtime role strict guard | G9 / G10 / G13 | 新增 `rls_runtime_guard` 启动前置检查：`RLS_RUNTIME_ROLE_ENFORCEMENT=strict` 默认拒绝 PostgreSQL `rolsuper` / `rolbypassrls` runtime role；`/api/health` 增加 `rls_runtime_role` component；Red tests 覆盖 strict fail-fast、warn degraded、off skip、unverifiable fail-closed 和 health 暴露 | 代码级关闭“RLS FORCE 但 runtime role 可能是 superuser/bypassrls”的假安全：生产默认 strict，无法验证或发现 superuser/BYPASSRLS 会阻断 startup；health 可观测当前 runtime-role assertion 状态。仍未宣称 stage-3 全面 cutover：还缺 Railway 真实 role readback、pre-auth bare session 穷举、`OR tenant_id IS NULL` 逃生门收敛和生产 access-denial trace | `cd backend && source .venv/bin/activate && pytest tests/services/test_rls_runtime_guard.py tests/api/test_health_liveness.py -q` -> `9 passed, 3 warnings` |
| 2026-06-16 | SOTA 修复 Phase 4：artifact execution gate 接入 promotion / CI | G1 / G2 / G14 | `decide_behavior_gated_promotion()` 新增 `artifact_gate_report`；`skill_distiller` 对 `skill` / `skill_patch` promotion 在 skill_guard 通过后运行 sandbox-backed artifact gate，并把 report 写入 promotion ledger；`ci_gate` 新增 artifact/adversarial report 输入和 `EXIT_ARTIFACT_GATE_FAILED`；GitHub harness/夜间 eval 生成 `hive-adversarial-suite.json` 并传入 gate | 代码级关闭 “artifact_gate/adversarial_suite 存在但晋升/CI 主链 dark”：代码型/skill 型候选即使 behavior report passing，没有 artifact report 或 artifact failed 也会 hold；CI 可用 adversarial JSON fail-closed。仍未宣称生产已达 G2：还缺真实 eval secrets 下的 GitHub artifact、Railway/Vercel microVM provider 样本和长期 score trend | `cd backend && source .venv/bin/activate && pytest tests/services/test_skill_distiller.py tests/services/test_promotion_hard_gate.py tests/evals/test_artifact_gate.py tests/evals/test_adversarial_suite.py tests/evals/test_ci_gate.py tests/evals/test_harness_ci_workflow.py -q` -> `71 passed, 4 warnings` |
| 2026-06-16 | SOTA 修复 Phase 5：behavior eval runner / baseline updater | G1 / G2 / G14 | 新增 `python -m app.evals.run_sota_behavior_eval --target hive|hermes --output ...`，fixture 模式可重复测试、live 模式拒绝 fallback/partial；新增 `python -m app.evals.update_behavior_baseline --report ... --commit-sha ...`，只接受 `behavior_eval_passed(report)` 的 trusted live report 并写 `provisional=false` baseline | 代码级关闭“没有可重复路径把真实行为分数写成 baseline”的流程缺口：runner 能生成可审计 report，updater 对 fallback/untrusted report exit 2 且不写 baseline。仍未宣称已有真实分数：`core_behavior_v1.json` 仍需由真实 eval secrets/目标环境产出的 trusted live report 显式更新 | `cd backend && source .venv/bin/activate && pytest tests/evals/test_hive_live_runner.py tests/evals/test_behavior_baseline_update.py tests/evals/test_ci_gate.py -q` -> `37 passed, 3 warnings`; `ruff check app/evals/run_sota_behavior_eval.py app/evals/update_behavior_baseline.py tests/evals/test_behavior_baseline_update.py` -> `All checks passed` |
| 2026-06-16 | SOTA 修复 Phase 6：promotion regression report 强制化 | G1 / G2 / G14 | `decide_behavior_gated_promotion()` 对行为变更候选缺 `regression_report` fail-closed；`skill_distiller` 从 behavior report 构造 `compare_to_baseline()` regression report，并在 promotion ledger 写入 `behavior_report_id`、`artifact_gate_report_id`、scenario scores、baseline version/provisional 状态 | 代码级关闭 distiller 晋升路径“只靠 passing behavior report、未显式记录 E1 回归判定”的弱门：候选现在必须有 candidate -> behavior eval -> regression decision -> artifact gate -> promotion/hold 审计链。仍未宣称 baseline 质量达标：当前 `core_behavior_v1.json` 仍是 provisional 0 分 seed，Phase 5 的 updater 需真实 live report 后显式更新 | `cd backend && source .venv/bin/activate && pytest tests/services/test_skill_distiller.py tests/services/test_promotion_hard_gate.py -q` -> `43 passed, 4 warnings`; `ruff check app/services/evolution_verification.py app/services/skill_distiller.py tests/services/test_promotion_hard_gate.py tests/services/test_skill_distiller.py` -> `All checks passed` |
| 2026-06-16 | SOTA 修复 Phase 7：memory lifecycle hold writers 接入 T2 主写入路径 | G3 / G8 / G14 | `append_t2_entries()` 将低置信 tentative/uncertain/volatile/stale 抽取落为 lifecycle sketch 而非 active T2；消费 `conflicts_with` metadata 写 conflict hold；发现缺失的本地 `workspace/` / `memory/` / `runtime_artifacts/` source ref 时写 reference revalidation hold | 代码级关闭“lifecycle maintenance 的三类 hold writer 只有测试构造、生产 T2 写入不调用”的暗臂：真实 T2 抽取写入现在可产生 sketch、conflict、revalidation hold，并由既有 maintenance/retrieval 路径消费。仍未宣称 G3 达到 Letta/Zep 级：语义矛盾发现仍依赖上游 metadata，缺 live recall eval、外部引用真实重校验样本和双时态 KG | `cd backend && source .venv/bin/activate && pytest tests/memory/test_t2_store.py tests/memory/test_lifecycle_maintenance.py tests/memory/test_retrieval_pipeline.py::test_retriever_suppresses_conflicted_and_revalidation_required_t3_entries -q` -> `17 passed`; `ruff check app/memory/t2_store.py tests/memory/test_t2_store.py` -> `All checks passed` |
| 2026-06-16 | SOTA 修复 Phase 8：connector ACL authority fail-closed + protected snippet check | G10 / G14 / G15 | `authoritative_connector_source_item()` 支持显式 document ACL（user/department/group/tenant scope），Feishu/Drive 只有 `agent_id` 时写 `deny_by_default` 和 `acl_authority=connector_unverified`；Feishu doc/drive 读结果写入 content digest / protected snippet signatures；生成后校验同时检查 source URI 与 forbidden protected snippet | 代码级关闭“Feishu/Drive source item 用当前 agent_id 自指授权”的安全假象：没有外部权威 ACL 或 identity mapping 时不能作为放行证据，明显复述 forbidden source 片段也会被 block。仍未宣称 Glean 式全覆盖：Feishu collaborator API、外部 open_id/department/group 到 Hive principal 的映射、全 connector per-document ACL 和生产 access-denial trace 仍待接入 | `cd backend && source .venv/bin/activate && pytest tests/services/test_connector_acl.py tests/kernel/test_generated_source_acl.py tests/services/test_feishu_cli_runtime.py tests/services/test_feishu_drive_runtime.py tests/tools/test_office_tools.py -q` -> `30 passed, 4 warnings`; `ruff check app/services/connector_acl.py app/services/agent_tool_domains/feishu_docs.py app/services/agent_tool_domains/feishu_drive.py app/tools/handlers/office.py tests/services/test_connector_acl.py tests/kernel/test_generated_source_acl.py tests/services/test_feishu_cli_runtime.py tests/services/test_feishu_drive_runtime.py tests/tools/test_office_tools.py` -> `All checks passed` |
| 2026-06-16 | SOTA 修复 Phase 9：`needs_reconciliation` admin/API/UI 消费面 | G4 / G7 / G13 | 新增 `runtime_reconciliation` service 和 `/admin/runtime-reconciliation` list/get/action API；Platform Dashboard 增加 Runtime Reconciliation 队列；action 支持 resolve/archive/retry，retry 仅在 metadata 显式 `reconciliation_retry_allowed=true` 时切回 `pending` | 代码级关闭“mutating restart fail-closed 之后只写 `needs_reconciliation`、无人可消费”的运营暗面：平台管理员现在能按 tenant 查询、查看原因、归档或标记处理；默认 retry 仍 fail-closed，避免重新引入重复副作用风险。仍未宣称 exactly-once：真实 mutating side-effect 仍需人工核验记录、生产 SOP 和未来 per-effect idempotency journal | `cd backend && source .venv/bin/activate && pytest tests/services/test_runtime_task_service.py tests/services/test_runtime_reconciliation.py tests/api/test_admin_runtime_reconciliation.py tests/api/test_admin_workflow_ops.py -q` -> `16 passed`; `ruff check app/services/runtime_reconciliation.py app/api/admin.py tests/services/test_runtime_reconciliation.py tests/api/test_admin_runtime_reconciliation.py` -> `All checks passed`; `cd frontend && npm run test -- src/api/domains/admin.test.ts src/pages/admin-companies/AdminRuntimeReconciliationSection.test.tsx` -> `2 passed`; `npm run build` -> success |

追加新行时必须写清楚：

- 对标的是哪条 SOTA 线，不要只写“优化系统”。
- 取证看了哪些代码、提示词、tool surface、runtime path、DB/migration、部署或生产日志。
- 哪些已经达成，哪些只是接近，哪些仍缺 live/product 验证。
- 验证命令、artifact、deploy URL、migration head、trace id 或 production log 放在哪里。

## 8. 文档分工

| 文档 | 角色 |
|---|---|
| `docs/hive-sota-master-goal.md` | 总目标、总矩阵、循环台账、未来第一入口。 |
| `docs/round2-sota-benchmark-2026.md` | 详细 SOTA benchmark、对标项目、来源、各 milestone 证据。 |
| `docs/harness-engineering-audit-2026-06-11.md` | 第一轮 harness 工程审计、P0/P1/P2 证据、修复记录。 |
| `docs/self-evolution-sota-plan.md` | 自进化 foundation 历史路线和已完成 substrate。 |
| `docs/plan-mode-design.md` | Plan Mode 总设计入口；其余 plan-mode 文档是分支细化或迁移路线。 |
| `docs/workflow-source-capability.md` | Workflow 作为确定性执行编排底座的设计边界、Plan Mode 关系、definition 生命周期和路线。 |
| `docs/workflow-ops-runbook.md` | Workflow runtime 的生产开关、指标、admin repair、Railway rollout 和 DB 要求。 |
| `docs/subagent-source-capability.md` | Subagent / delegation 源能力边界和与 Workflow、Deep Research 的关系。 |
| `docs/agent-task-cognitive-scaffold.md` | Work Ledger / Progress Ledger 作为 agent 认知脚手架的设计入口。 |
| `docs/agent-extension-surface-skill-mcp.md` | Skill + MCP extension surface 的用户模型入口。 |
| `docs/trigger-cc-alignment.md` | Trigger / automation 与 CC 执行基线的对齐入口。 |
| `docs/document-conversion-multimodal-design.md` | Document / multimodal capability 的设计入口。 |
| `docs/remote-workstation-runtime.md` | Remote workstation / persistent local capability 的设计入口。 |
| `docs/external-behavior-eval-ci.md` | 外部行为 eval CI 和 fail-closed 方案。 |
| `docs/agent-memory-purity-spec.md` | memory purity、lifecycle、hygiene contract。 |

维护规则：

- 本文只保留总目标和循环结论；详细命令、长证据、长调研不要塞进本文。
- `round2-sota-benchmark-2026.md` 的目标或竞品口径变更时，必须同步更新本文对应矩阵。
- 如果新增第三轮 benchmark，先更新本文的总目标和文档分工，再追加专题证据文档。
- 旧文档如果与本文冲突，先以本文为路线入口，但仍必须回到当前代码和生产状态验证事实。
