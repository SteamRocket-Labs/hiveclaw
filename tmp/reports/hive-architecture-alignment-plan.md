# Hive Agent 架构对齐方案 — 对标 Claude Code 与 Hermes Agent

> **状态**:修订稿 v1.6 · 2026-04-27 · Phase 0R 护栏落地
> **作者**:Claude (Opus 4.7) 草案 · Codex 复核修订 · 与 Codex 报告并列(参见 `agent-session-feishu-merge-review.md`)
> **目标**:在当前 main 分支基础上,先冻结 Autonomy P0-P6 绿基线,再用 Harness H1-H6 达到全面对标 Claude Code、Hermes Agent 与主流 Harness Engineering 实践的极简、易拓展架构

---

## 0. TL;DR(给只看一眼的人)

1. **不要直接合并 `feature/agent-session-feishu`**——会和 main 上 63 个 commit 的 prompt/cache/memory/eval 演进发生真实冲突
2. **当前本地验证基线已经从"待修"变成"可用"**——截至本次再校准:backend pytest `1853 passed,7 skipped`;ruff 全绿;frontend `70 passed`;frontend build 通过;Alembic 单 head。原 Phase 0 的"先修 collection/ruff"已不再是待办,应改为 Phase 0R: **冻结当前绿基线并防回退**。
3. **feature/agent-session-feishu 仍然不能直接合并,但集成策略要变成选择性吸收**——Autonomy P0-P6 已经把 autonomy trigger/objective/runtime/UI 主干向前推进了一大段;后续只能把 feature 的 session、Feishu canonical、tool runtime、architecture tests 拿来对齐,不能用旧 feature 覆盖当前 objective ledger / autonomy BFF。
4. **自主触发模块已经从"待建设"变成"已成主干,需要护栏"**:
   - Objective Ledger 是目标事实源
   - Trigger/Wake Policy 是唤醒策略
   - RuntimeTask/Artifact 是执行账本
   - `focus.md` 是可读投影,不是事实源
   - Aware UI 默认展示目标/唤醒/结果/动作,内部 ID/config/metadata 只进 diagnostics
5. **两个仍然没有闭环的战略差距(本文档第一优先级)**:
   - **差距 A**:**Evals 驱动的 prompt 自动优化**——Hermes 生态已明确指向 DSPy + GEPA,但不能把 companion repo 直接等同为 Hermes Agent core 已全量落地;Hive 应先建 eval/bake-off/rollback
   - **差距 B**:**闭环 skill auto-extraction/refinement**——Hermes 本地源码已有后台 memory/skill review;Hive 也已有 `skill_distiller` 与 candidate lifecycle,真正缺的是 outcome-driven refine 与用户审核 promote 闭环
   - 这两个不是"锦上添花",是**架构层面真正需要补齐的系统能力**
6. **架构债不在功能,而在多代叠层和缺少常绿护栏**——`agent_tools.py` facade、feature 的架构测试/治理文档、session/Feishu/tool runtime 主干仍值得吸收;但 autonomy trigger trunk 不能再按旧分支方案重做
7. **路线改为一条主线 + 一个素材库**——主线是 Architecture Phase 0R → Harness H1-H6;旧 Architecture Phase 1-5 只作为工程素材,不再整段顺序执行
8. **结论置信度分层**——"不能直接合并,但值得系统性吸收"信心约 96%;"自主目标/触发/执行/UI 基本闭环"信心约 90%;"Harness H4/H5 长任务与自进化闭环稳定提升任务达成率"信心约 60%-72%
9. **Harness Engineering 重新定义下一步**——Autonomy P0-P6 解决的是 autonomy trunk;Harness H1-H6 必须解决 harness trunk:可执行规范、可观察环境、独立评价器、可恢复长任务、权限沙箱、上下文/记忆边界、eval 驱动的自我进化。

---

## 0.1 本次 Codex 修订说明:为什么这样改

这次修订不是推翻原始诉求,而是把原文中证据强度不够、执行顺序有风险、验证状态过期的地方改成可落地版本。2026-04-27 的 Autonomy P0-P6 实施后,本文档再次校准:自主目标/触发/执行/UI 已经变成当前主干,而不是未来分支计划。

1. **把当前状态从历史结论改为工具复核结论。** 原文写 main 有 18 个失败测试,随后一版修订又写成 pytest collection error + ruff 10 errors。当前最新本机回归已经不同:backend pytest `1853 passed,7 skipped`;backend ruff 全绿;frontend `70 passed`;frontend build 通过;Alembic 单 head。因此原 Phase 0 不再是"先修 baseline",而是 Phase 0R 的"冻结绿基线并防回退"。
2. **把 Hermes/GEPA 的表述从"核心系统已完整落地"改成"生态方向已确认、核心落地程度需谨慎"。** 已能确认 Hermes 自进化 companion repo 和 release 文档体现了 DSPy/GEPA/benchmark 方向,但本地 Hermes Agent core 未能证明"所有系统提示词都已进入 GEPA 自动优化 loop"。所以这里作为战略目标保留,作为已完成事实降级。
3. **把 Hive skill distillation 的描述从"固定模板/半人工"修正为"已有保守 distiller,但缺闭环"。** Hive 当前已有 LLM draft、workflow signature、candidate lifecycle、review-only patch 等机制,低估它会导致错误规划;真正缺口是 outcome-driven refine、用户确认 promote、失败样本回灌。
4. **移除 `git merge -X ours` 作为默认集成命令。** 这个策略在当前冲突类型下风险很高,可能静默覆盖 feature 的结构化治理资产或 main 的 prompt/memory 演进。正确做法是普通 merge 暴露冲突,再逐文件决策。
5. **把"能合并"的定义改为零红线验证。** 当前目标是长期对标 Claude Code / Hermes Agent,基础工程门槛不能接受 pytest failure、ruff error、架构测试缺失或运行态数据污染。Phase 0R 的退出条件必须是可重复、可审计、可回滚。

## 0.2 Autonomy P6 后当前状态再校准:哪些计划必须改变

P6 之后,这份计划需要从"修坏掉的自主触发模块"改为"保护已经形成的自主闭环主干"。

**当前绿基线**:
```text
backend pytest     1853 passed,7 skipped,4 warnings
backend ruff       All checks passed
frontend test      18 files,70 tests passed
frontend build     passed
alembic heads      add_agent_objectives_0427 (head)
git diff --check   clean
```

**新的事实源分层**:
```text
soul/SKILL/memory markdown
- agent 的认知、技能、长期记忆真源
- 必须保持 plain-text 可读、可 diff、可人工修

agent_objectives
- 目标事实源
- 记录目标状态、成功标准、优先级、证据、完成/拒绝理由

agent_triggers
- wake policy 真源
- 只表示何时唤醒/怎样唤醒,不再冒充目标本身

runtime_tasks + artifacts
- attempt/result ledger 真源
- 记录每次 heartbeat/trigger/objective run 的执行、跳过、失败、产物

focus.md
- 可读投影和兼容面
- 不是目标事实源,不能再被 UI 或 trigger 当作唯一账本
```

**计划影响**:
- 原 Phase 0 的 baseline cleanup 已完成,后续应改为 Phase 0R:冻结当前 Autonomy P6 自主闭环主干,补架构测试和防回退护栏。
- `feature/agent-session-feishu` 仍然值得吸收,但只能选择性迁移 session、Feishu canonical、tool runtime、architecture tests;不能用旧 autonomy trigger trunk 覆盖当前 `agent_objectives` / autonomy BFF / Aware UI。
- 架构测试需要从 feature 分支迁移并适配当前 Autonomy P6 API,尤其要固化 Objective Ledger / Wake Policy / RuntimeTask / Artifact / UI diagnostics 的边界。
- 后续战略工作应转向 skill outcome/refine 和 eval/GEPA-light,并直接复用当前 RuntimeTask、objective、artifact 数据,不要再发明第二套任务账本。

---

## 0.3 Harness Engineering 外部校准:这件事真正要做什么

本次新增校准来自 2026 年的三类资料:Anthropic 的 long-running harness 系列、OpenAI 的 Codex harness engineering 实践、以及社区/研究界围绕 Ralph loop、Jules、VeRO、CAT、SWE-EVO、ABTest 形成的共识。结论很明确:一个长期可行的 agent 框架不是"更长 prompt + 更多 trigger",而是**把目标、环境、反馈、权限、记忆、评价、回滚做成可执行的 harness**。

**Anthropic 给出的关键经验**:
- 长任务失败的核心不是模型不会写代码,而是上下文跨 session 断裂、过早宣布完成、试图一次做太多、缺少端到端验证。
- 有效 harness 需要 initializer agent 建立结构化 feature/test/progress artifact,后续 coding agent 每次只做增量,结束时留下干净状态、git/progress 记录和可验证结果。
- 最新 long-running app harness 进一步证明 generator 与 evaluator 要分离;QA/evaluator 必须用 Playwright 这类真实操作工具验证,不能只看代码或听 generator 自评。
- harness 复杂度必须定期删减:每个额外 agent、sprint、reset、gate 都是假设,模型变强后要重新验证它是否仍是 load-bearing。

**OpenAI 给出的关键经验**:
- 工程师角色从"写代码"转为"设计环境、明确意图、构建反馈回路"。
- 代码仓库是记录系统:给 agent 的应该是地图,不是一千页说明书;AGENTS/skills/docs 要短、分层、可发现。
- 应用、日志、metrics、traces、DevTools、screenshots 都要对 agent 可读,让 agent 能自己复现、修复、验证。
- 高吞吐 agent 会复制代码库里的坏模式,所以必须把"黄金原则"编码成机械规则和后台清理任务,形成垃圾回收式治理。

**社区/Google/研究界给出的关键经验**:
- Ralph loop 的有效点是简单:每轮新上下文、读规范、选最高优先级未完成任务、执行、测试、提交、记录;风险是 token/cost 高,且没有评价器时会自我确认。
- Google Jules 的产品化方向是 async cloud VM、plan 可审、diff/PR 可审、GitHub workflow 触发、并发任务、隔离执行环境;对 Hive 的启发是 long task 输出必须是可审核 artifact,不是只在聊天里说完成。
- VeRO 指向 agent 优化必须有 versioned snapshots、reward、observations、budget-controlled eval、structured traces;这正是 Hive skill/prompt 自我进化缺的账本。
- CAT 指出 context 不应只是被动压缩,而应成为 agent 可调用的工具:稳定任务语义、压缩长期记忆、高保真短期交互分层管理。
- SWE-EVO/ABTest 说明长周期软件演进和 agent 鲁棒性必须单独测;只看单 issue benchmark 或普通 pytest 会严重高估 agent。

**反过来看 Hive 的架构结论**:

```text
Autonomy P0-P6 = autonomy trunk
- 目标、唤醒、执行、artifact、UI 已成主干

Harness H1-H6 = harness trunk
- 规范、上下文、工具环境、权限、评价、自我进化、回滚要成主干
```

Hive 不应该照搬 Ralph 的死循环,也不应该照搬 Jules 的纯 PR 模式。Hive 的优势是多租户、多渠道、企业权限、长期 agent identity 和记忆。因此正确方向是:在 Hive 内部建立**持续运行的 Evolution Harness**,把每个 agent 的长期成长拆成可审计的 objective/attempt/artifact/eval/version ledger。

---

## 0.4 阶段命名边界:Autonomy P0-P6 独立于 Architecture Phase 0R-5

Autonomy P0-P6 已经是当前完成并验收过的自主目标/触发/执行/UI 主干。它和本文后面的 Architecture Phase 0R-5 不是同一套编号，不能混算。后续 Harness H1-H6 也不是 Autonomy 编号的延续，而是在 Autonomy P0-P6 之上补 harness engineering 所需的架构护栏、权限硬化、ContextEngine、MemoryProvider、长任务 runtime 和 evaluator/self-evolution ledger。

| 阶段 | 验收结论 |
|------|----------|
| P0 | 已完成:只读 autonomous audit 覆盖 focus、trigger、runtime task、heartbeat/trigger session 断点 |
| P1 | 已完成:trigger/heartbeat 执行和 skipped 路径写 `RuntimeTask` |
| P2 | 已完成:objective_task 稳定 session、trigger 分组执行、completed-focus reconciler |
| P3 | 已完成:`agent_objectives` 成为目标事实源,`focus.md` 降级为 projection |
| P4 | 已完成:Objective Intake / Gate / Wake Reconciler / Evaluator / objective tools 形成闭环 |
| P5 | 已完成:wake gate、runtime artifact、context_from、model/toolset/workdir pinning、backoff、approval、event_wait lifecycle |
| P6 | 已完成:Autonomy BFF、Aware UI、attempt/artifact 展示、trigger P6 API、前端 i18n/tests |

Autonomy P0-P6 的验收命令已经在 10.2 固化,当前记录的绿基线是:

```text
backend pytest     1853 passed,7 skipped,4 warnings
backend ruff       All checks passed
frontend test      18 files,70 tests passed
frontend build     passed
alembic heads      add_agent_objectives_0427 (head)
git diff --check   clean
```

因此下一步的排序必须是:

```text
1. 冻结 Autonomy P0-P6 验收基线。
2. 执行 Architecture Phase 0R,补架构测试防止 autonomy trunk 回退。
3. 启动 Harness H1-H6,把自主闭环升级为长期 harness trunk。
```

---

## 0.5 当前执行顺序修正:只先做 Phase 0R,不先完整跑 Phase 1-5

当前真实状态是:Autonomy P0-P6 已经完成,Harness H1-H6 是下一条主线。Architecture Phase 0R-5 不应再被理解成"Harness 之前必须完整完成的一条路线"。

**新的执行结论**:

```text
Must do now:
- Architecture Phase 0R
- 目的:冻结 Autonomy P0-P6 绿基线,补最小架构测试,防止已完成主干回退

Do not run as separate precondition:
- Architecture Phase 1-5
- 原因:里面很多内容已经被 Harness H1-H6 吸收,继续整段执行会制造第二条路线

Main track after Phase 0R:
- Harness H1-H6
- 目的:把 autonomy trunk 升级成长期可运行、可评价、可回滚的 harness trunk
```

**Architecture Phase 1-5 的新归宿**:

| 原 Architecture Phase | 新归宿 | 处理方式 |
|----------------------|--------|----------|
| Phase 1 工程治理收尾 | H1 / H2 / H6 | 拆成架构测试、ToolRuntime 单入口、SessionContext 统一，不再作为独立 1-2 周阶段 |
| Phase 2 Hooks/Subagent/Compact | H1 / H3 / H6 | Hooks/Compact 进入 ContextEngine 与 runtime contract；Subagent 进入 SessionContext/InvocationRequest contract |
| Phase 3 Skill Auto-Extraction | H5 | 作为 Self-Evolution Ledger 的 skill candidate/outcome/refine 子系统 |
| Phase 4 GEPA/DSPy-light + Evals | H5 | 作为 Evaluator + bakeoff + promote/rollback 子系统 |
| Phase 5 Plain-text/UX 收尾 | H1-H6 的验收补项 | 只在对应 harness 能力落地后做文档、statusline、inspectability 收尾 |

**当前推荐顺序**:

```text
Step 1: Architecture Phase 0R
- Autonomy P0-P6 architecture tests
- Harness H1 architecture tests skeleton
- 当前绿基线固化
- feature/agent-session-feishu 只做治理资产清点,不做大合并

Step 2: Harness H1
- Kernel / ToolRuntime / Permission / Context / Memory / Session 边界测试常绿

Step 3: Harness H2 + H6
- 权限/工具运行时硬化
- session/channel 统一

Step 4: Harness H3
- ContextEngine + MemoryProvider

Step 5: Harness H4
- Long Task Runtime

Step 6: Harness H5
- Evaluator + Self-Evolution Ledger
```

这意味着 Architecture Phase 0R 是必要的,但 Architecture Phase 1-5 不需要先完整完成。旧 Phase 1-5 保留为拆解素材和历史规划,执行权重让位给 Harness H1-H6。

---

## 1. 原始诉求(逐字保留,定盘)

> 我们的目标只有一个 — **一个极简的、极易拓展的、全面对标 Claude Code 与 Hermes Agent 的 agent 框架**。
>
> 涉及到 LLM 的系统提示词、skill、记忆蒸馏提示词等都是重点优化对象。从工程上、框架上、agent 效率上、上下文工程上、自我进化系统上、提示词工程上、工具使用上、agent 任务达成率上,全方位无死角对标并超越这两个项目。

**关键词解读**:
- **极简**:删除多代叠层,每个能力一条主路径
- **极易拓展**:hooks/skills/tools 都是 plain text + 显式 contract,企业可以加自己的层
- **全面对标 Claude Code**:plain-text-first、CLAUDE.md、SKILL.md、hooks、subagents、MCP
- **全面对标 Hermes Agent**:AIAgent loop、后台 memory/skill review、SQLite FTS5、6-backend 工具运行时、Hermes 生态中的 GEPA + DSPy 自进化方向
- **超越**:多 provider cache、企业级 governance、多渠道、多租户 — 这些 Hive 已经领先

---

## 2. 当前事实基线(2026-04-27)

### 2.1 分支状态

```
git rev-list --left-right --count origin/main...origin/feature/agent-session-feishu
# 63 52
```

| 分支 | 起点 | commits | 改动量 | 性质 |
|------|------|--------|------|-----|
| `main`(起点 `61cd68b`,2026-04-14) | — | 63 | — | LLM 系统进化主线 |
| `feature/agent-session-feishu` | 同 | 52 | +21,746 / -22,658 行,349 文件 | 架构主干治理(T0~T6) |

### 2.2 双方测试基线

| 项 | main(本机当前) | feature(Codex 验证) |
|---|---|---|
| pytest | **1853 passed,7 skipped,4 warnings** | 1223 passed |
| ruff | **All checks passed** | All checks passed |
| frontend test | **70 passed** | 67 passed |
| frontend build | passed | passed |
| alembic heads | **single head:`add_agent_objectives_0427`** | 未复核 |

> **Autonomy P6 后修正**:原文的"1720 passed / 18 failed"、上一版的"pytest collection error + ruff 10 errors"都属于历史基线。当前本地 main/Autonomy P6 工作区已经重新变绿。后续重点不是"先修 baseline",而是防止 feature 集成或后续阶段把这条绿基线打回去。

### 2.3 main 独有(feature 没有)的进化

**Memory(12 commit)**
- `memory/backend.py` — MemoryBackend Protocol
- `memory/backends/hindsight.py` — 读侧加速后端
- `memory/hindsight_sync.py` / `metrics.py`
- `267a350` query-aware T3 injection + BM25
- `a85e940` temporal search + pluggable MemoryBackend

**Cache(5 commit,完全新增)**
- `services/prompt_cache.py` — provider-agnostic 1h TTL
- Qwen / MiniMax / Anthropic 多 provider cache_control
- `12137f0` perf: frozen prefix over-invalidation 修复

**Prompt 重写(10 commit · best-practice 改造)**
- HEARTBEAT.md:91 → 193 行(decision-matrix + examples)
- DREAM.md:71 → 180 行(identity stakes + few-shot)
- EXTRACT_PROMPT 重写
- 14+ 个辅助 prompt 全部 best-practice

**Skills(2 commit)**
- 17 个 SKILL.md body 全升级
- `system_skills/memory-guide/SKILL.md`、`messaging-guide/SKILL.md`(全新)

**T0 日志(3 commit)**
- behavior/system 子目录拆分
- artifacts/ spillover(>8000 字)
- decision reasoning 落地

**Providers 解耦(2 commit)**
- 移除所有硬编码 provider 依赖
- 从 tenant DB 解析

**Autonomy P0-P6 主干(当前工作区已落地,独立于 Architecture Phase 0R-5)**
- `agent_objectives` 成为目标事实源,`focus.md` 退为可读投影
- trigger/heartbeat/objective run 写入 `RuntimeTask`,skipped 也进入账本
- trigger 分类收敛为 objective_task / scheduled_job / event_wait / system_maintenance
- runtime artifact、wake gate、context_from、toolset/workdir/model pinning 进入统一机制
- Aware UI 通过 autonomy BFF 暴露目标/唤醒/尝试/产物/下一步动作,默认隐藏 raw config/metadata/内部 ID

### 2.4 feature 独有(main 没有)的工程治理

**架构测试(10 个,纯增量)**
```
tests/architecture/
├── test_autonomy_trigger_trunk.py
├── test_channel_message_contract.py
├── test_chat_sessions_channel_contract.py
├── test_collaboration_trunk.py
├── test_legacy_agent_tools_allowlist.py
├── test_legacy_session_compat_allowlist.py
├── test_prompt_memory_trunk.py
├── test_session_identifier_contract.py
├── test_session_message_trunk.py
└── test_tool_runtime_trunk.py
```

**Tool runtime 主干切分**
- `tools/surface.py` — canonical surface 组装
- `tools/execution_entry.py` — canonical 执行入口
- `services/agent_tools.py` 已删(main 上仍 815 行)

**Session 主干统一**
- `session_identifiers.py` — 跨渠道 contract
- `session_service.py` — 单一 session 创建/归并
- `channel_message_contracts.py`

**Feishu canonical user_id 整治**(约 30 个 sub-commit)
- 解决 open_id ↔ user_id 在 send/recv/storage 三处分叉
- `db_legacy_feishu_session_migration.py` 启动期归并
- 15+ 调用点重新对齐

**12 个 trunk-governance 文档** — 完整的 T0~T6 整治路线图

### 2.5 直接 merge 会冲突的 14 个真实代码文件

| 文件 | 严重性 | main 状态 | feature 状态 | 推荐取舍 |
|------|------|---------|-----------|--------|
| `templates/HEARTBEAT.md` | 🔴 P0 | best-practice 193 行 | 旧版 91 行 | **取 main** |
| `templates/DREAM.md` | 🔴 P0 | best-practice 180 行 | 旧版 71 行 | **取 main** |
| `templates/system_skills/delegation-guide/SKILL.md` | 🟡 P1 | best-practice | 旧版 | **取 main** |
| `services/t0_logger.py` | 🔴 P0 | behavior/system 拆分 + spillover | 旧版平铺 | **取 main** |
| `services/memory_service.py` | 🔴 P0 | BM25 + temporal | 旧版 + 删 wrapper | **取 main + 拣 feature 的 wrapper 删除** |
| `memory/store.py` | 🔴 P0 | **已删除** | 仍在使用 | **取 main 的删除态** |
| `agents/orchestrator.py` | 🔴 P0 | prompt 升级 | A2A/delegation 分流 | **手动合**(两边都对) |
| `services/agent_tool_domains/messaging.py` | 🔴 P0 | — | OpenClaw transcript 修复 | **手动合** |
| `services/task_executor.py` | 🟡 P1 | (未动) | 修复 | **取 feature** |
| `services/org_sync_service.py` | 🟡 P1 | (未动) | provider-backed 重写 | **取 feature** |
| `tests/agents/test_orchestrator.py` 等多个测试 | 🟡 P1 | — | — | **手动合** |
| `.ultra/memory/chroma/*` | 🟢 丢弃 | 运行时数据 | 运行时数据 | **加 .gitignore + 删除已 track** |

---

## 3. 对标对象画像

### 3.1 Claude Code 核心设计哲学

来源:[Claude Code Memory Docs](https://code.claude.com/docs/en/memory) · [Context Engineering 6 Pillars](https://claudefa.st/blog/guide/mechanics/context-engineering) · `~/.claude/` 用户配置

**核心原则**:
1. **Plain text 是真源** — CLAUDE.md / SKILL.md / settings.json 都是用户可读、可编辑、可 git diff 的明文
2. **Auto memory** — Claude 自动累积知识,无需用户写;但所有累积都落到可读 MD
3. **Skills 是通用 playbook** — SKILL.md 一种格式,frontmatter + body,单命令安装
4. **Hooks 是用户编程接口** — 企业可注入 PreToolUse / PostToolUse / SessionStart / SessionEnd
5. **Subagents 隔离** — Task 工具开新 context 窗口,与主对话隔离
6. **MCP** — 外部工具 / 资源标准协议
7. **Context engineering 6 pillars** — 决定每个 token 的去留

### 3.2 Hermes Agent 核心设计哲学

来源:[Hermes Agent Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture) · 本地 `/Users/rocky243/vc-saas/hermes-agent` 源码 · Hermes self-evolution companion repo

**核心原则**:
1. **AIAgent loop 是中心** — 不是 gateway control plane,而是以 agent 自身的 reasoning + tool + skill + self-evaluation 闭环为一等公民
2. **Gateway 多渠道单进程** — 一个 agent 进程同时连多个 messaging 平台
3. **SQLite + FTS5** — 极简持久化,session/memory/skill 都在一个 SQLite 库
4. **6 backend tooling runtime** — local / Docker / SSH / Daytona / Singularity / Modal
5. **Cache-aware system prompt** — session 初始化冻结 snapshot,高频调用走缓存
6. **MEMORY.md + USER.md** — 两个小而精的文件注入 system prompt
7. **GEPA + DSPy 自进化方向** — 在 Hermes 生态/companion repo 中体现,但不等同于 core 已全量落地

---

## 4. Hive 当前位置评估(2026-04-27)

### 4.1 已超越对标对象的(置信度 90%)

| 维度 | Hive 现状 | Claude Code | Hermes | 备注 |
|------|---------|-----------|--------|------|
| **多 provider cache** | Anthropic + Qwen + MiniMax 全覆盖,1h TTL | 仅 Anthropic | cache-aware freezing | 我们最强,国内场景关键 |
| **企业级 governance** | security_zone + capability_gate + approval(3 层) | 无明确 governance | 无明确 governance | 真实超越 |
| **多渠道集成** | Feishu/Slack/Discord/DingTalk/WeCom/Teams/Telegram/WeChat 9 渠道 | 无 | gateway(主要 Slack/Discord) | 真实超越 |
| **多租户** | 完整 RLS + tenant_id scope | 无 | 无 | 企业刚需 |
| **记忆深度** | T0/T2/T3/soul 4 层 | CLAUDE.md + auto memory(2 层) | hot/cold + MEMORY.md(2 层) | 我们最深 |

### 4.2 已对齐的(置信度 92%)

| 维度 | Hive | Claude Code | Hermes |
|------|------|-----------|--------|
| 系统提示词架构 | frozen prefix + dynamic suffix | session 持久 + 累积 | session 冻结 snapshot |
| Skill 格式 | SKILL.md + YAML frontmatter | SKILL.md 通用格式 | 自动 extraction |
| 渐进式 skill 加载 | catalog 进 prompt + load_skill 调用 | 类似 | 类似 |
| Hooks 总线 | 15 事件生命周期 | hooks system | learning loop hooks |
| Tool registry | ToolRegistry + decorator | tool registry | tooling runtime |
| Context budget | section 化 + token 预算 | 6 pillars | cache-aware budget |

### 4.3 真实落后的(置信度 88%,这是要补的)

| 维度 | Hive 现状 | Hermes / Claude Code 现状 | 差距类型 |
|------|---------|------------------------|--------|
| **闭环 skill auto-extraction** | 有保守 `skill_distiller.py` 与 candidate lifecycle,缺 outcome refine | Hermes 本地有后台 memory/skill review | **闭环能力差距** |
| **GEPA 自动 prompt 进化** | 人工 best-practice 重写 + 初级 eval infra | Hermes 生态明确指向 GEPA/DSPy | **算法/工程差距** |
| **DSPy compile** | 无 | Hermes self-evolution 方向使用 DSPy | **工程差距** |
| **Evals 闭环** | 有 eval runner 雏形,无线上 pass-rate 驱动 | 推断两家都有更强内部 evals | **方法差距** |
| **架构测试** | P6 新增大量服务/API/UI 测试,但 feature 的 10 个 architecture tests 尚未迁移适配 | 不公开但内部一定有 | **工程债** |
| **Hooks 编程接口对外开放** | 内部 15 事件,租户拿不到 | Claude Code 完全开放 | **产品形态债** |
| **Subagent 隔离深度** | `delegate_to_agent` 已有,context 边界不够清晰 | Claude Code Task 工具完全隔离 | **架构精度债** |
| **Tooling runtime 多后端** | in-process 单一 | Hermes 6 backends | **架构能力债** |
| **多代叠层未清理** | `agent_tools.py` 815 行 facade | 未公开但理论上更干净 | **工程债** |

> 自主目标/触发/执行闭环不再列为"真实落后"。Autonomy P0-P6 已经把它推进为当前主干;剩余问题是补架构测试、长周期指标和 feature 分支迁移时的兼容护栏。

### 4.4 ⭐ 两个战略差距(本文档核心诉求)

这两点不是普通差距,是**架构层面的系统能力差距**。它们不应被包装成已经完全证实的竞品事实,但确实是 Hive 要达到"持续变强的 agent 框架"时必须建设的能力。**这是 Harness H5 的核心目标**;如果只做 Phase 0R/H1-H3 的护栏与上下文工程,没有 evaluator/self-evolution ledger,等于没有完成对标。

---

#### 差距 A:Evals 驱动的 prompt 自动优化

**Hermes / Hermes 生态可确认现状**:
- `hermes-agent-self-evolution` companion repo 明确指向 DSPy + GEPA 的 prompt/self-evolution 方向
- Hermes Agent release 文档提到 self-optimized GPT/Codex tool-use guidance 与 automated behavioral benchmarking
- 本地 Hermes Agent core 未能直接证明"每个系统提示词、记忆蒸馏提示词、技能判断提示词都已进入 GEPA loop"
- 因此这里应被视为**战略对标目标**,不是可直接照抄的已完成核心实现

**Hive 现状**:
- main 分支花 10 个 commit 把 14+ 个 prompt 重写到 best-practice 结构(`HEARTBEAT.md`、`DREAM.md`、`EXTRACT_PROMPT` 等)
- 已有 `backend/app/evals/run.py`、`bakeoff_runtime.py` 等 eval 雏形,不能说完全"无 evals"
- 但 prompt 优化仍主要是人工 best-practice 重写,缺少线上任务 pass-rate、失败样本、候选 prompt、自动 bake-off、自动回滚组成的闭环
- **这是架构层面的差距**:我们的 prompt 改进还没有被稳定的任务级指标驱动

**为什么这是战略差距**:
1. 长期看,prompt 质量上限取决于优化算法,不取决于一次性人工水准
2. 用户在不同场景(语言、行业、规模)对 prompt 的最优形态不一样,人工无法穷举
3. 模型版本更新时(Claude 4.7 → 5.0),prompt 需要重新调,人工成本不可持续

**Hive 的目标(置信度 65%)**:
- 不必照搬 GEPA 全部论文实现,做**轻量等价**:
  - 段落级 mutation(LLM 重写一个段落)
  - LLM-as-judge selection(用 evals 数据集判断新旧版本)
  - 每周自动跑一次,带 changelog
- 三个高价值 prompt 优先:`HEARTBEAT.md` / `DREAM.md` / `EXTRACT_PROMPT`
- 关键护栏:**evals pass-rate 自动回滚**——演化跑出比基线更差的版本时,自动回退
- 详细见 Harness H5

---

#### 差距 B:闭环 Skill Auto-Extraction / Refinement

**Hermes 可确认现状**:
- 本地 Hermes Agent 源码能看到后台 memory/skill review 机制,说明它把任务后复盘接入 agent loop
- 但"每次对话自动生成 SKILL.md draft、用户审核、使用后自动 refine"这一整套闭环,应作为对标目标与产品化推断,不能当成已经完全验证的 core 事实

**Hive 现状**:
- `app/services/skill_distiller.py` 已经不是简单固定模板:它包含 workflow signature、LLM draft、validation、candidate lifecycle、review-only patch
- 已有 `RESPONSE_COMPLETE` hook、heartbeat、skill distillation 相关基础设施
- 真正缺的是:
  - 任务 outcome 与 skill 使用效果的结构化记录
  - 用户确认 promote 的产品闭环
  - correction/failure 样本触发的 LLM refine
  - skill 版本、回滚、评估指标
- **这是架构层面的差距**:我们的 skill 库已经能半自动产生候选,但还没有形成"越用越准"的闭环

**为什么这是战略差距**:
1. Skill 是 agent 长期价值的核心载体——agent 跑得越久,skill 库应该越丰满
2. 规则化抽取永远抽不出"非显式"的 skill(比如"这个用户喜欢被先拒绝再说服")
3. 没有 refine 机制,skill 一旦写错,会持续误导 agent

**Hive 的目标(置信度 75%)**:
- 闭环架构:
  ```
  RESPONSE_COMPLETE hook
    ↓
  LLM judge: "this conversation reveals a reusable skill?"
    ↓ (yes, with confidence ≥ 0.7)
  生成 SKILL.md draft → skills/.draft/
    ↓
  用户审核(UI)/ 自动 promote(高置信度)
    ↓
  promote → skills/
  ```
- 自我 refine 闭环:
  - skill 被 load_skill 后,记录 outcome(成功/correction)
  - outcomes ≥ 5 且 correction ratio ≥ 30% → 触发 LLM refine
  - 用户 review diff → 接受/拒绝
- 详细见 Harness H5

---

#### 这两个战略差距与当前主路线的关系

```
Architecture Phase 0R → 保护 Autonomy P0-P6 主干 + 选择性迁移 feature 治理资产
Harness H1            → 架构护栏,禁止第二套 kernel/tool/memory/objective/session 机制
Harness H2/H6         → 权限/工具运行时/session/channel 收敛
Harness H3            → ContextEngine + MemoryProvider
Harness H4            → Long Task Runtime
Harness H5            → Skill auto-extraction + GEPA/DSPy-light + Evals
```

**Phase 0R + H1-H3 是地基**:不解决这些,差距 A/B 没法做(没有架构测试、没有干净的工具/权限/session/context 主干,自进化无从落脚)。

**H4-H5 是攻坚**:长任务 runtime 和 evaluator/self-evolution ledger 是真正让 agent 24 小时进化的关键。

**旧 Phase 1-5 是素材库**:其中有价值的内容已被拆入 H1-H6,不再作为独立顺序路线。

---

### 4.5 对标矩阵总结(置信度 87%)

```
                  Claude Code     Hermes Agent     Hive 现状
─────────────────────────────────────────────────────────────
极简度            ●●●●○            ●●●●●            ●●○○○  (815 行 facade)
易拓展度          ●●●●●            ●●●○○            ●●●○○  (hooks 不对外)
工程治理          ●●●●○            ●●●●○            ●●●○○  (P6 测试变绿,feature 架构测试待适配)
自我进化          ●●○○○            ●●●●●            ●●●○○  (有 distiller,缺闭环 refine)
提示词工程        ●●●●○            ●●●●●            ●●●●○  (人工 best-practice + 初级 eval)
上下文工程        ●●●●●            ●●●●○            ●●●●○  (frozen/dynamic + cache)
工具使用          ●●●●○            ●●●●●            ●●●●○  (governance 强,后端单)
任务达成率        ●●●●○            ●●●●○            未量化  (无线上 pass-rate 闭环)
多渠道            ●○○○○            ●●●○○            ●●●●●  (我们最强)
企业治理          ●●○○○            ●●○○○            ●●●●●  (我们最强)
```

---

## 5. 目标架构 — 极简 5 层

```
┌──────────────────────────────────────────────────────────────────┐
│  L5  Channels (Feishu/Slack/Web/A2A/Trigger) — 薄适配层           │
│       职责:外部消息 → InvocationRequest                           │
│       禁止:直接调 LLM、直接写 ChatSession、直接执行工具          │
│       架构测试:`test_channels_only_build_request.py`              │
├──────────────────────────────────────────────────────────────────┤
│  L4  Runtime (invoke_agent → AgentKernel)                        │
│       职责:LLM loop + tool rounds + context budget               │
│       禁止:直接 import DB、直接调渠道                            │
│       架构测试:`test_runtime_pure_no_db_imports.py`               │
├──────────────────────────────────────────────────────────────────┤
│  L3  Capabilities (Tools + Skills + Hooks + MCP)                 │
│       职责:可装可拆,governance 双层                             │
│       禁止:绕过 ToolRuntimeService、绕过 Hook 总线               │
│       架构测试:`test_no_direct_tool_execution.py`                 │
├──────────────────────────────────────────────────────────────────┤
│  L2  Memory & Ledgers (T0/T2/T3/soul + objective/trigger/runtime)│
│       职责:MD 保存认知/技能/记忆;DB ledger 保存目标/唤醒/执行    │
│       禁止:把 focus.md 当目标事实源;禁止 UI 解析 raw metadata    │
│       架构测试:`test_autonomy_ledger_boundaries.py`               │
├──────────────────────────────────────────────────────────────────┤
│  L1  Identity & Evolution (soul + GEPA/DSPy + skill auto-extract)│
│       职责:闭环自进化(对齐 Hermes)                              │
│       禁止:无 evals 改 soul.md                                   │
│       架构测试:`test_evolution_requires_evals.py`                 │
└──────────────────────────────────────────────────────────────────┘
```

**每层之间只有显式 contract**(dataclass / TypedDict / Protocol),不允许跨层调用。

**架构测试是不可妥协的护栏**——所有这 5 个文件必须在 main 上常绿。

---

### 5.1 Hive Evolution Harness v2 — 长期自主进化的 6 层架构

Autonomy P6 后的 5 层架构仍然成立,但如果目标是长期 harness engineering,需要把"运行时"和"评价/进化"显式拆出来。最终目标不是一个更复杂的 agent swarm,而是一套更可控的 agent harness。

```text
L0 Trust Boundary
- tenant/user/agent identity
- secrets/capability/approval/audit
- hardline deny + allow/ask/deny policy

L1 Agent Kernel
- 唯一 LLM 执行入口:AgentKernel + InvocationRequest
- 所有工具调用必须经 ToolRuntime + CapabilityGate
- 禁止 API/service/channel 直接调 LLM 或 tool handler

L2 Context Engine
- prompt sections / memory retrieval / context_from / compaction / cache
- summary 是 reference artifact,不是新指令
- context compression 可被 agent 主动请求,也可由 policy 触发

L3 Durable Ledgers
- objectives / wake policies / runtime attempts / artifacts
- memory / skill candidates / eval runs / prompt versions
- 所有长期事实必须可查询、可 diff、可回滚

L4 Control Loops
- objective intake / wake reconciler / trigger daemon / heartbeat
- dream / distill / evaluator / skill refiner / garbage collector
- 每个 loop 都必须有 skipped/failed/completed attempt 记录

L5 Surfaces
- web / Feishu / Slack / A2A / admin / autonomy dashboard
- UI 展示 decision fields 和 action fields
- raw metadata/config/internal IDs 只进入 diagnostics
```

**不可破坏的 harness invariants**:

```text
1. Objective 是目标事实源;Trigger 只是 wake policy。
2. RuntimeTask/Attempt 记录每次真实执行、跳过、失败和完成。
3. Session 是上下文容器,不是事实源。
4. Memory 记录经验/知识/偏好,不承载待办目标。
5. focus.md 只是可读投影和兼容面。
6. 所有 LLM 调用必须走 AgentKernel。
7. 所有工具调用必须走 ToolRuntime + CapabilityGate。
8. 所有外部副作用必须有 objective/authorization lineage。
9. 所有 compaction/memory injection 必须有 fence/source。
10. 所有自我进化修改必须有 candidate/eval/promote/rollback。
11. 所有长任务必须有 output artifact、状态机、可恢复/可取消语义。
12. 所有 harness 复杂度都必须能被 eval 证明仍然 load-bearing。
```

**对标后的 Hive 取舍**:

| 取舍 | Hive 方案 | 原因 |
|------|----------|------|
| 是否照搬 Ralph loop | 不照搬,只吸收 fresh context + spec/task/progress artifact | Hive 是长期身份 agent,死循环容易制造无授权副作用和成本失控 |
| 是否照搬 Jules | 不照搬,但吸收 async VM/PR-like artifact/plan approval | Hive 不是纯代码 PR 产品,需要多渠道和企业权限 |
| 是否采用 Anthropic 三 agent harness | 选择性采用 planner/generator/evaluator 分离 | 只在高风险长任务和 self-evolution 上启用,普通任务保持单 kernel |
| 是否让 heartbeat 做所有事 | 不允许 | heartbeat 是观察/蒸馏/提案 loop,不是业务万能执行器 |
| 是否把 memory 当 task queue | 不允许 | 目标必须进 objective ledger,记忆只服务上下文 |
| 是否让 evaluator 常驻 | 不默认 | evaluator 成本高,只在 long task、external side effect、self-evolution、低置信完成时启用 |

---

## 6. Architecture Phase 0R-5 历史路线：当前只执行 Phase 0R

本节保留原 Architecture Phase 0R-5 的拆解内容，作为工程素材和风险清单；它不再是当前主执行路线。当前主执行路线以 0.5 节为准：先完成 Architecture Phase 0R，然后进入 Harness H1-H6。Phase 1-5 中仍有价值的内容必须拆入 H1-H6，不再整段顺序执行。

### Phase 0R — 保护当前 Autonomy P6 主干 + 集成准备(W0,本周)

**目标**:冻结当前已经变绿的 Autonomy P6 自主闭环主干,再选择性吸收 feature 的工程治理资产;任何集成都不能倒退 Objective Ledger / Wake Policy / RuntimeTask / Artifact / autonomy UI 这条主路径。
**置信度**:92%
**预计工时**:1-2 天

**动作清单**:
1. 固化当前绿基线
   - 保留并持续运行当前回归命令:
   ```bash
   cd /Users/rocky243/vc-saas/hiveclaw-main/backend
   .venv/bin/python -m pytest
   .venv/bin/python -m ruff check app tests
   .venv/bin/alembic heads

   cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
   npm test
   npm run build

   cd /Users/rocky243/vc-saas/hiveclaw-main
   git diff --check
   ```
   - 当前期望:backend `1853 passed,7 skipped`;ruff pass;frontend `70 passed`;build pass;Alembic 单 head。
2. 增加/迁移架构测试,但先适配 P6 新事实源
   - 从 feature 迁移 architecture tests 中仍有效的 session/message/tool runtime/prompt-memory 合约。
   - `test_autonomy_trigger_trunk.py` 必须改写为当前 Autonomy P6 合约:目标事实源是 `agent_objectives`,trigger 是 wake policy,attempt/result 是 `RuntimeTask` + artifact,`focus.md` 是 projection。
3. 选择性吸收 feature 分支
   - 可以吸收:session identifiers、channel message contracts、Feishu canonical user_id、tool runtime canonical surface、trunk-governance 文档。
   - 不可覆盖:当前 `autonomy.py` / `autonomy_overview.py` / trigger P6 API / objective ledger / runtime artifact / Aware UI。
4. 清理运行态数据入库问题
   - 如果 `.ultra/memory/*` 仍被 git 跟踪,只从 index 移除,不删除本地运行态文件。
   ```bash
   git status --short .ultra/memory
   git rm --cached -r .ultra/memory/
   printf '\\n# Local agent runtime memory\\n.ultra/memory/\\n' >> .gitignore
   ```
5. feature 集成必须走普通 merge 或 cherry-pick,逐文件 review
   ```bash
   git switch main
   git pull --ff-only
   git switch -c codex/integrate-agent-session-feishu
   git merge --no-commit origin/feature/agent-session-feishu
   ```
   - prompt / memory / cache / eval 默认以当前 main/P6 为准。
   - session / Feishu canonical / tool runtime / architecture tests 默认吸收 feature,但必须适配当前接口。
   - `agents/orchestrator.py`、`task_executor.py`、`memory_service.py` 必须手动合并,不能整文件覆盖。

**Exit criteria**:
- ✅ 当前全量回归仍然保持 backend pytest / ruff / alembic / frontend test / frontend build / diff-check 全绿
- ✅ architecture tests 迁移后能表达当前 Autonomy P6 ledger 边界,而不是旧 focus.md/trigger 事实源模型
- ✅ 所有 feature 的 trunk-governance 文档进入 `docs/backend-trunk-governance/` 或等价位置
- ✅ feature 集成没有引入第二套 autonomy trigger 机制
- ✅ Aware UI 仍通过 autonomy BFF 展示 display fields,默认不暴露 raw metadata/config/内部 ID

**2026-04-27 本轮已落地**:
- 新增 `backend/tests/architecture/test_phase0r_boundaries.py`,锁住 kernel / tool runtime / objective ledger / RuntimeTask / session / memory 边界。
- 修正 approved tool 执行边界:`approval_service` 不再导入私有 `_execute_tool_direct`;业务层改走 `execute_approved_tool`,运行时由 `ToolRuntimeService.execute_approved` 统一处理。
- `execute_approved` 审计记录包含 `approved_by_user_id` 与 `approval_id`,避免 post-approval 执行成为不可追踪直通路径。
- 本轮暂未做 feature 分支合并/迁移;Phase 0R 的“保护当前 Autonomy P6 主干”部分已完成,feature 治理资产迁移应进入 Harness H1/H2 的具体任务。

**回滚方案**:集成分支独立,失败直接弃枝;P6 当前主干不通过 feature merge 回滚。

---

### Phase 1 — 工程治理收尾(W1-W2,已拆入 H1/H2/H6)

**目标**:完成 815 行 facade 削薄、工具执行主路径收敛、Feishu canonical 整治,并让架构测试成为常绿护栏
**置信度**:88%
**预计工时**:1-2 周 × 1 人

**动作清单**:

**1.1 绿基线防回退**(0.5-1 天)
- 保留 Autonomy P6 后全量回归基线:backend pytest / ruff / alembic / frontend test / frontend build / diff-check
- 把 feature 的 architecture tests 纳入常规 CI,但先适配当前 Objective/Wake/Runtime/Artifact 分层
- 为测试文件命名、Alembic single-head、UI diagnostics 隐藏策略增加轻量架构护栏,避免历史 drift 回潮

**1.2 `agent_tools.py` 削薄到 < 100 行**(4 小时)
- 拆 ContextVar 到 `app/tools/channel_context.py`
- 拆剩余的 register helper 到 `app/tools/surface.py`(已存在)
- 验证:`grep -r "from app.services.agent_tools" backend/` 等于 0

**1.3 Feishu canonical user_id 闭环**(1 周)
- 按 feature 分支约 30 个子 commit 顺序 cherry-pick
- 每个 cherry-pick 单独验证
- `db_legacy_feishu_session_migration.py` 启动期归并
- `tests/services/test_feishu_user_search.py` 全过

**1.4 废弃 deprecated 参数**(2 小时)
- `include_memory_file`, `include_focus`, `build_runtime_prompt`
- grep 出 0 调用点

**Exit criteria**:
- ✅ `pytest tests` 0 fail
- ✅ `ruff check app tests` 0 error
- ✅ frontend test/build 0 fail
- ✅ `agent_tools.py` < 100 行
- ✅ Feishu open_id ↔ user_id 在 send/recv/storage 三处一致
- ✅ 架构测试全过

---

### Phase 2 — Claude Code 关键能力对齐(W3-W4,已拆入 H1/H3/H6)

**目标**:Hooks 对外、Subagent 隔离、显式 Compact
**置信度**:88%
**预计工时**:2 周 × 1 人

**2.1 Hooks 编程接口对外开放**(1 周)
- 在 `tenants` 表加 `hooks JSONB` 字段
- 支持事件:PreToolUse / PostToolUse / SessionStart / SessionEnd / UserPromptSubmit / PreCompaction
- 每个 hook 可注入 shell 命令或 webhook URL
- 配置示例:
  ```json
  {
    "PreToolUse": [
      {"matcher": "send_*", "command": "/usr/local/bin/audit-send.sh"}
    ],
    "SessionEnd": [
      {"webhook": "https://siem.company.com/agent-events"}
    ]
  }
  ```
- **对标价值**:Claude Code 的 hook 是 enterprise selling point

**2.2 Subagent 隔离深度**(3 天)
- 现状:`delegate_to_agent` 已有 `core_tools_only=True`,但 context 边界不显式
- 升级:加 `SubagentContext` dataclass
  - 独立 messages
  - 独立 token budget
  - 共享 hooks(只读)
  - 共享 memory(只读)
  - 独立 tool result envelope
- **对标价值**:Claude Code Task 工具的核心是真正隔离

**2.3 显式 Compact 命令**(2 天)
- 新增工具 `compact_session(reason: str)`
- agent 可主动请求,不依赖 85% 阈值自动触发
- 记录 compaction reason 到 T0 system 日志
- **对标价值**:Claude Code 的 /compact 是用户日常能力

**Exit criteria**:
- ✅ 至少一个租户配置自定义 hook 并触发成功
- ✅ subagent 调用不会污染主对话 token budget
- ✅ `compact_session` 工具 agent 可主动调用并验证生效

---

### Phase 3 — ⭐ 攻克差距 B:闭环 Skill Auto-Extraction(W5-W7,已拆入 H5)

> 对应第 4.4 节差距 B。当前执行时归入 Harness H5;本节只保留原拆解细节,不作为独立阶段启动。

**目标**:闭环 skill auto-extraction + skill self-refine + 多后端 tooling runtime
**置信度**:78%(闭环产品化需要实验)
**预计工时**:3 周 × 1 人

**3.1 闭环 Skill Auto-Extraction**(1.5 周)
- 现状:`skill_distiller.py` 已有 LLM draft、workflow signature、candidate lifecycle,但还没有 outcome/refine/promote 闭环
- 升级架构:
  ```
  RESPONSE_COMPLETE hook
    ↓
  LLM judge: "this conversation reveals a reusable skill?"
    ↓ (yes)
  生成 SKILL.md draft → 落 skills/.draft/
    ↓
  用户审核(UI 或 plaza)
    ↓
  promote → skills/
  ```
- 关键 prompt:judge prompt + draft prompt
- **对标价值**:把 Hive 从"能生成 skill 候选"推进到"能根据真实任务结果持续修正 skill"

**3.2 Skill 自我 refine**(1 周)
- 当 `load_skill` 后跟着用户 correction(信号:用户说 "no", "stop", "不对" 等)
- 记录 outcome 到 `skills/<name>/outcomes.jsonl`
- 每周自动跑一次 refine pipeline
  - 读 outcomes
  - LLM 重写 SKILL.md body
  - diff 给用户审核
- **对标价值**:Hermes 接近但未闭环,这一步可以**真正超越**

**3.3 Tooling Runtime 后端化**(0.5 周)
- 抽象 `ToolBackend` Protocol
- 实现 `LocalBackend`(默认)
- 占位 `DockerBackend` / `SSHBackend`(后续按需开发)
- `execute_code` / `run_command` 走配置选择
- **对标价值**:Hermes 的 6 backends 是企业刚需(隔离工具执行)

**Exit criteria**:
- ✅ 一个 agent 跟用户对话 1 周后,自动产生至少 1 个 skill draft
- ✅ 至少 1 个 skill 经过自我 refine 进化
- ✅ `execute_code` 可以选择 local 或 docker 后端

---

### Phase 4 — ⭐ 攻克差距 A:GEPA/DSPy-light + Evals(W8-W10,已拆入 H5)

> 对应第 4.4 节差距 A。当前执行时归入 Harness H5;本节只保留原拆解细节,不作为独立阶段启动。

**目标**:先建立 evals/pass-rate/bake-off/rollback,再试点 GEPA/DSPy-light,不把未经验证的自动改写直接写入生产 prompt
**置信度**:65%(实验性,依赖 eval 数据质量)
**预计工时**:3 周 × 1 人 + LLM token 预算

**4.1 Evals 闭环(基础设施)**(1 周)
- 每个 agent 持有 task pass-rate metric
- 定义任务类型:
  - chat 完成率(用户没说"不对")
  - delegation 完成率(子 agent 完成 task)
  - trigger 执行率(scheduled task 真正完成)
- 历史任务自动比对:soul.md / SKILL.md 改了之后,pass-rate 是涨是跌
- 涨 → 保留;跌超过阈值 → 自动回滚
- **对标价值**:这是 Claude Code 与 Hermes 都不公开但内部一定有的护栏

**4.2 GEPA 试点**(1.5 周)
- 选 3 个高价值 prompt:HEARTBEAT.md / DREAM.md / EXTRACT_PROMPT
- 实现 mini-GEPA:
  - 段落级 mutation(LLM 重写一个段落)
  - LLM judge selection(对比新旧版本在 evals 上的表现)
  - 每周演进一次,生成 candidate + changelog
  - 只有 pass-rate 超过基线且人工 review 通过时才写回模板
- **对标价值**:先实现可控的等价闭环,再决定是否完整复现 GEPA 论文

**4.3 DSPy 试点**(0.5 周)
- 同三个 prompt 进 DSPy
- 用 task pass-rate 作为 metric
- DSPy compile 后的版本 vs GEPA 版本 A/B
- **对标价值**:验证 DSPy 是否适合 Hive 的 prompt 类型,不预设它一定优于轻量 mutation

**Exit criteria**:
- ✅ evals 数据集至少 100 个 task,pass-rate 自动追踪
- ✅ GEPA 至少演进过 1 个 prompt,带可读 changelog
- ✅ DSPy compile 至少跑过 1 次

---

### Phase 5 — 极简化收尾(W11-W12,作为 H1-H6 验收补项)

**目标**:对外形象 plain-text-first,对齐 Claude Code 的"inspectable"原则
**置信度**:90%
**预计工时**:2 周 × 1 人

**5.1 文档 plain-text 化**(0.5 周)
- 删除所有"ORM 浮层"文档
- 所有规则统一到 MD + YAML
- API 文档从 dataclass 自动生成

**5.2 Skill marketplace 雏形**(1 周)
- tenants 之间可分享 skills(public bucket)
- skill 评分 + 下载量
- 仿 Claude Code skill 安装命令

**5.3 Statusline / context budget UX**(0.5 周)
- 仿 Claude Code 显示 token 用量、cache 命中率
- 给 agent UI 加状态栏

**Exit criteria**:
- ✅ 所有用户可见的配置都是 plain text
- ✅ 跨租户 skill 分享至少 1 个
- ✅ UI 显示 token / cache 实时数据

---

### Harness Track H1-H6 — Harness Trunk 升级路线(独立于 Autonomy P0-P6 编号)

> Architecture Phase 0R-5 解决的是"对齐旧 feature 治理 + skill/eval 初步闭环"。Harness H1-H6 解决的是更底层的问题:Hive 自己是否已经成为一个高质量 harness engineering 平台。它不是 Autonomy 编号的延续,而是一条单独的 harness track。

| Phase | 名称 | 目标 | 验收标准 |
|-------|------|------|----------|
| H1 | Architecture Guardrails | 用架构测试锁死 kernel/tool/objective/memory/context/permission 边界 | 任何直接调 LLM、直接调 tool handler、把 focus.md 当事实源、memory/objective 混用都会测试失败 |
| H2 | Permission + Tool Runtime Hardening | 建立 hardline deny、allow/ask/deny、approval 后仍走 ToolRuntime、skill guard | 高风险命令 fail-closed;approval 不再有 direct execute 绕路;工具调用全量可审计 |
| H3 | ContextEngine + MemoryProvider | 把 context/memory 做成可插拔 harness 组件 | compaction artifact、memory fence、provider lifecycle、context_as_tool 全部有测试 |
| H4 | Long Task Runtime | 长任务支持 plan/spec/progress/output delta/resume/cancel/missed policy | 6 小时任务可跨 session 恢复;每轮都有 artifact;UI 可看进度/阻塞/下一步 |
| H5 | Evaluator + Self-Evolution Ledger | generator/evaluator 分离,skill/prompt 进入 eval/bakeoff/promote/rollback | 所有 skill/prompt 自动改动都有 version、reward、trace、rollback |
| H6 | Session/Channel Harness Unification | web/Feishu/Slack/A2A/trigger 共享 SessionContext/SessionKey contract | 渠道只生成 InvocationRequest;不再有渠道侧私有 session/trigger 逻辑 |

**H1 立即实施清单**:

```text
backend/tests/architecture/test_kernel_boundaries.py
- AgentKernel 不 import DB、渠道、API router
- invoke_agent 是 LLM runtime 唯一入口

backend/tests/architecture/test_tool_runtime_single_entry.py
- API/service/channel 不直接调用 tool handler
- approval 后执行也必须回到 ToolRuntime

backend/tests/architecture/test_autonomy_ledger_boundaries.py
- Objective 是事实源
- Trigger 是 wake policy
- RuntimeTask/Artifact 是 attempt/result ledger
- focus.md 只能是 projection

backend/tests/architecture/test_memory_objective_separation.py
- memory service 不创建业务目标
- objective intake 不把 memory markdown 当 task queue

backend/tests/architecture/test_permission_hardline.py
- destructive shell / secret exfil / external side effect 必须进入 deny 或 approval

backend/tests/architecture/test_context_engine_contract.py
- compression summary 只能是 reference artifact
- memory/context injection 必须有 source/fence

backend/tests/architecture/test_session_context_contract.py
- channel/web/trigger/A2A 都必须产出统一 SessionContext/InvocationRequest
```

**H2-H6 关键设计**:

```text
H2:
- PermissionPolicy = hardline_deny + capability_gate + approval + audit
- SkillGuard 扫描外部 skill 的 exfiltration/destructive/persistence/network/injection 风险
- ToolRuntimeBackend 抽象 local/docker/ssh/cloud VM,先落 local/docker

H3:
- ContextEngine Protocol: on_session_start / assemble / should_compact / compact / on_session_end
- MemoryProvider Protocol: prefetch / inject / sync_turn / on_pre_compress / on_session_end
- Context artifact: every compaction and memory injection is traceable

H4:
- LongTaskPlan artifact: spec / acceptance criteria / verification command / risk gates
- Runtime progress artifact: delta output / screenshots / logs / metrics links
- Resume contract: fresh context can recover from objective + runtime artifact + git/log state

H5:
- EvolutionCandidate: target_type(prompt|skill|memory_policy|tool_policy), diff, source attempts
- EvalRun: dataset, model, budget, reward, traces, failure cases
- PromotionPolicy: only promote when reward beats baseline and no critical regression

H6:
- SessionContext as request-scoped object, not env/global mutable state
- external_conv_id / channel thread / objective session / runtime task trace_id 统一映射
- Channel delivery result also writes artifact, not only chat message
```

**Harness H1-H6 之后的目标状态**:

```text
用户提出长期目标
  ↓
ObjectiveIntake 生成候选并过 gate
  ↓
WakePolicy 负责唤醒
  ↓
AgentKernel 在 ContextEngine 管理下执行
  ↓
ToolRuntime 在 PermissionPolicy 下执行工具
  ↓
RuntimeTask + Artifact 记录过程和结果
  ↓
Evaluator 验证是否真的完成
  ↓
MemoryProvider 写入经验,SkillRefiner 生成候选
  ↓
Eval/Bakeoff 决定是否 promote 自我进化改动
```

这条链路闭合后,才能说 Hive 的自主进化能力基本闭环。Autonomy P0-P6 让 agent "会醒、会做、会记账";Harness H1-H6 让 agent "会被环境约束、会被独立评价、会安全长期运行、会基于证据进化"。

---

## 7. 第一刀:本周可立即执行的 6 个动作

按优先级:

| # | 动作 | 风险 | 时间 | 谁做 |
|---|------|------|-----|-----|
| 1 | 给当前 Autonomy P6 trunk 补 architecture tests:Objective Ledger / Wake Policy / RuntimeTask / Artifact / UI diagnostics 边界 | 低 | 0.5-1 天 | Claude/Codex |
| 2 | 增加 H1 harness architecture tests:Kernel / ToolRuntime / Permission / ContextEngine / MemoryProvider / SessionContext | 中 | 0.5-1 天 | Claude/Codex |
| 3 | 移植 feature 的 trunk-governance 文档和非 autonomy 架构测试,逐个适配当前接口 | 中 | 0.5-1 天 | Claude/Codex |
| 4 | 选择性迁移 session identifiers / channel message contracts / Feishu canonical user_id,不覆盖 Autonomy P6 API | 中 | 1-2 天 | Claude/Codex + 用户 review |
| 5 | 开始 skill outcome/refine 账本设计,直接复用 RuntimeTask artifact 数据 | 中 | 0.5-1 天 | Claude/Codex |
| 6 | 开始 eval/pass-rate 数据集设计,用 objective/runtime ledger 作为样本来源 | 中 | 0.5-1 天 | Claude/Codex |

**全部完成 = 当前 Autonomy P6 自主闭环从"功能可用"进入"架构受保护",并且 H1 harness trunk 的边界开始常绿。**

---

## 8. 风险登记册

| ID | 风险 | 概率 | 影响 | 缓解 | 责任 |
|----|------|-----|------|-----|------|
| R1 | Phase 0R 选择性集成冲突解错,prompt/cache/Autonomy P6 进化丢失 | 中 | 高 | 集成分支独立 PR,每个冲突单独 commit,review 到位 | Claude + 用户 |
| R2 | Feishu canonical 修复破坏现网会话 | 中 | 高 | feature 已带 db_legacy_*_migration helper,启动期归一化可回放 | Claude |
| R3 | H5 闭环 skill 抽取出低质量 skill | 高 | 中 | draft 蓄水池 + 用户审核,不直接 promote | Claude |
| R4 | H5 GEPA/DSPy-light 让 prompt 退化 | 高 | 高 | evals pass-rate 自动回滚,配人工 override | Claude |
| R5 | Harness 主线被新需求打断 | 高 | 中 | 每个 H 阶段独立 PR,可暂停可恢复 | 用户 |
| R6 | Hermes/Claude Code 持续进化(尤其是闭源 Claude Code) | 中 | 低 | 每月 review 对标矩阵 | Claude |

**整体回滚策略**:Phase 0R 与每个 Harness H 阶段都使用独立分支 + 独立 PR,任何阶段失败不影响前一阶段绿基线。

---

## 9. 决策点与推荐默认值

如果没有新的产品方向输入,建议按以下默认值执行:

### Q1:Phase 顺序
- 当前顺序:Architecture Phase 0R 与 Harness H1 并行 → H2/H3 → H4 → H5 → H6;旧 Architecture Phase 1-5 的工程治理、skill、eval 工作并入对应 H1-H6
- 原因:当前 Autonomy P6 已经形成 autonomy trunk,下一步要保护 harness 边界,不能继续按旧"先功能后治理"顺序推进
- 备选 A:先 H2/H6 再 H3/H4/H5,优先治理工具和 session 边界
- 备选 B:H1 后直接 H5,优先 self-evolution,但会让 eval/skill 建在尚未硬化的工具主干上

### Q2:Phase 0R 集成时机
- 选项 A:先补 Autonomy P6 architecture tests,再开 `codex/integrate-agent-session-feishu`(推荐)
- 选项 B:现在立即开集成分支,但不提交 merge commit,只用于冲突侦察
- 选项 C:只 cherry-pick feature 的 session/Feishu/tool runtime 子集,暂不做整分支 merge

### Q3:H5 skill auto-extraction 信号设计
- 选项 A:LLM judge 单审(快,但容易抽出垃圾)
- 选项 B:LLM judge + 用户主动 promote(慢,但精准)
- 选项 C:LLM judge + 用户被动否决(中庸)

### Q4:H5 GEPA-light 选哪些 prompt(差距 A 攻击点)
- 当前候选:HEARTBEAT / DREAM / EXTRACT_PROMPT
- 备选:加 SKILL judge prompt / capability gate prompt / dream consolidation prompt
- 你最关心哪些 prompt 的进化?

### Q4-bis:两个战略差距的优先级
- 差距 A(GEPA-light)和差距 B(skill auto-extraction)哪个先攻克?
  - 选项 A:**先 B 后 A**(默认)——闭环 skill 抽取产生 evals 数据集,作为 GEPA 的判优依据
  - 选项 B:**先 A 后 B**——先把已有 prompt 演化能力建起来,再让 skill 抽取借用同一套 GEPA
  - 选项 C:**双线并行**——两组人马同时推进,但必须先有 H1 架构护栏
- 我的推荐:**选项 A**(B 先 A 后),因为 evals 数据集是 GEPA 的输入,B 先做能给 A 喂数据

### Q4-tris:GEPA 实现深度
- 选项 A:**轻量等价版**(默认,置信度 70%)——段落级 mutation + LLM judge,不实现完整论文
- 选项 B:**完整复现 GEPA 论文**(置信度 40%)——按论文算法完整实现
- 选项 C:**直接用 DSPy 框架**(置信度 60%)——不自研,挂 DSPy 的现成实现
- 我的推荐:**选项 A 起步,跑 4 周后看效果决定要不要升级到 B/C**

### Q5:谁来跑 Architecture Phase 0R + Harness H1-H6
- 选项 A:Claude(我)主导,你 review + 决策
- 选项 B:多 agent 协作(Codex 跑工程治理,Claude 跑自进化)
- 选项 C:你自己主导,Claude/Codex 当工具

### Q6:验收标准
- Harness 主线完成对标的"完成"是什么定义?
  - 选项 A:对标矩阵 10 个维度全部 ≥ 4/5 星
  - 选项 B(我的推荐):**两个战略差距都闭环**
    - H5 skill 闭环:agent 跑 1 周后,自动产生至少 3 个被用户接受的 skill draft
    - H5 prompt/eval 闭环:GEPA-light 跑 4 周后,至少 1 个 prompt 在 evals 上跑赢人工 best-practice 版本
    - H1 架构测试 100% 常绿
  - 选项 C:evals pass-rate 跑赢"未对标版本"基线 ≥ 10%
  - 选项 D:对外可演示——给到 Hermes 团队/Claude Code 团队看,他们承认"这是同代产品"

### Q7:超越目标
- 你说"全方位对标并超越",**超越**的定义是什么?
  - 选项 A:每个维度都比 Claude Code/Hermes 高一档
  - 选项 B:在某些维度(多渠道 + 治理 + 多 provider cache)做到独家,其余维度跟上
  - 选项 C:在自进化某个具体子能力(比如 skill marketplace + 自动 refine)做到独家

---

## 10. 附录

### 10.1 关键证据来源

**仓库内**:
- `git log --oneline 61cd68b..main` — main 63 个 commit 全列表
- `git log --oneline 61cd68b..origin/feature/agent-session-feishu` — feature 52 个 commit 全列表
- `git merge-tree main origin/feature/agent-session-feishu` — 14 个真实冲突文件
- `docs/backend-trunk-governance/` (feature 分支) — 完整治理路线图
- `tmp/reports/agent-session-feishu-merge-review.md` — Codex 报告(并列参考)

**外部**:
- [Claude Code Memory Docs](https://code.claude.com/docs/en/memory)
- [Claude Code Context Engineering 6 Pillars](https://claudefa.st/blog/guide/mechanics/context-engineering)
- [Dive into Claude Code (arXiv)](https://arxiv.org/html/2604.14228v1)
- [Hermes Agent Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- [Hermes Agent GitHub](https://github.com/nousresearch/hermes-agent)
- [Hermes Agent Memory Explained](https://www.remoteopenclaw.com/blog/hermes-agent-memory-system-explained)
- [Anthropic: Harness design for long-running agents](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Anthropic: Building effective agents](https://www.anthropic.com/research/building-effective-agents)
- [OpenAI: Harness engineering for agents](https://openai.com/zh-Hans-CN/index/harness-engineering/)
- [Google Labs: Jules, an asynchronous coding agent](https://blog.google/technology/google-labs/jules)
- [VeRO: Versioned agentic Reinforcement Learning via Order-preserving gradients](https://arxiv.org/abs/2602.22480)
- [Context as a Tool: Context Management for Long-Horizon SWE-Agents](https://arxiv.org/abs/2512.22087)
- [SWE-EVO: Benchmarking Coding Agents in Long-Horizon Software Evolution Scenarios](https://arxiv.org/abs/2512.18470)
- [ABTest: Behavior-Driven Testing for AI Coding Agents](https://arxiv.org/abs/2604.03362)

### 10.2 当前验证基线与历史失败清单(Codex 复核,2026-04-27)

**当前 Autonomy P6 后绿基线**:

```text
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
./.venv/bin/python -m pytest
# 1853 passed,7 skipped,4 warnings

./.venv/bin/python -m ruff check app tests
# All checks passed

./.venv/bin/alembic heads
# add_agent_objectives_0427 (head)

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test
# 18 files,70 tests passed

npm run build
# passed

cd /Users/rocky243/vc-saas/hiveclaw-main
git diff --check
# clean
```

**历史失败清单(仅保留审计背景,不再代表当前状态)**:

- 曾出现 pytest import mismatch:`backend/tests/runtime/test_prompt_cache.py` 与 `backend/tests/services/test_prompt_cache.py` 模块名冲突。
- 曾出现 ruff 10 个 unused import / E402 / F841 问题。
- 曾记录 frontend `68 passed` 的旧基线。

> 当前计划必须以 Autonomy P6 后绿基线为准。后续所有 feature 集成和 Harness 阶段推进,都要证明没有打破这组命令。

### 10.3 feature 分支的 trunk-governance 文档清单

```
docs/backend-trunk-governance/
├── README.md                                  # 顺序与纪律
├── 01-trunk-catalog.md                        # T0~T6 主干清单
├── 02-dependency-and-break-risk-map.md        # 依赖与风险图
├── 03-detection-and-evidence-playbook.md      # 检测剧本
├── 10-phase-1-autonomy-trigger-trunk.md       # Phase 1 自主触发
├── 11-phase-2-session-message-trunk.md        # Phase 2 会话与消息(800+ 行)
├── 12-phase-3-collaboration-delegation-trunk.md  # Phase 3 协作与委派(1400+ 行)
├── 13-phase-4-prompt-memory-trunk.md          # Phase 4 prompt/memory
├── 14-phase-5-tool-runtime-trunk.md           # Phase 5 工具运行时(1400+ 行)
├── 15-phase-6-contract-hardening-and-legacy-deletion.md  # Phase 6 契约固化(1480+ 行)
├── 20-master-regression-plan.md               # 总回归计划
└── 21-branch-repair-order.md                  # 分支修复顺序
```

> 这是一份**极高质量的工程治理蓝本**,即便不直接合并 feature 分支,这套文档也应该全部移植到 main(纯增量,零风险)。

### 10.4 对标矩阵雷达图(目标 vs 现状)

```
                        Claude Code (灰)  Hermes (蓝)  Hive 现状 (红)  Hive 目标 (绿)
─────────────────────────────────────────────────────────────────────────────────
极简度                       ●●●●○         ●●●●●         ●●○○○         ●●●●●
易拓展度                     ●●●●●         ●●●○○         ●●●○○         ●●●●●
工程治理                     ●●●●○         ●●●●○         ●●●○○         ●●●●●
自我进化                     ●●○○○         ●●●●●         ●●●○○         ●●●●●
提示词工程                   ●●●●○         ●●●●●         ●●●●○         ●●●●●
上下文工程                   ●●●●●         ●●●●○         ●●●●○         ●●●●●
工具使用                     ●●●●○         ●●●●●         ●●●●○         ●●●●●
任务达成率                   ●●●●○         ●●●●○         未量化         ●●●●●
多渠道                       ●○○○○         ●●●○○         ●●●●●         ●●●●●
企业治理                     ●●○○○         ●●○○○         ●●●●●         ●●●●●
```

### 10.5 我的置信度自评(诚实)

| 内容 | 置信度 | 说明 |
|------|------|-----|
| 不能直接 merge 的判断 | 96% | 14 个真实冲突已用 git merge-tree 验证 |
| 当前 Autonomy P6 绿基线成立 | 95% | backend pytest/ruff/alembic、frontend test/build、diff-check 已本机复核 |
| 自主目标/触发/执行/UI 基本闭环 | 90% | Autonomy P0-P6 已覆盖 objective、wake policy、RuntimeTask、artifact、Aware UI;仍需长周期线上数据验证 |
| Phase 0R 主干保护可行 | 88% | 重点从修 baseline 改为补 architecture tests 和选择性迁移 feature |
| Phase 0R 集成方案可行 | 82% | 冲突表已逐个分析,但需要人工逐文件决策 |
| Harness H1 架构护栏可达 | 90% | 主要是架构测试与边界收敛 |
| Harness H2/H6 工具权限/session 收敛可达 | 82% | feature 治理资产可复用,但要适配当前 Autonomy P6 主干 |
| Harness H3 ContextEngine/MemoryProvider 可达 | 78% | 需要重构接口但风险可控 |
| Harness H4 Long Task Runtime 可达 | 70% | 需要真实长任务验证 |
| Harness H5 self-evolution/evals 可达 | 60% | 需要实验数据,可能需要更长时间 |
| **整体达成长期 harness 目标** | **72%** | 主要不确定来自 H4/H5 的真实长周期数据质量与算法效果 |

---

## 11. 下一步(建议直接执行)

建议不再继续抽象讨论。Architecture Phase 0R 的 Autonomy P6 主干保护已落地,下一步进入 Harness H1/H2 的前 3 个动作。H1 负责把 harness 边界测试化,H2 负责把 tool runtime / permission / backend 抽象继续收敛:

1. 移植 feature 的 trunk-governance 文档和非 autonomy 架构测试,逐个适配当前接口。
2. 新增 H1 harness architecture tests,继续锁住 Permission / ContextEngine / MemoryProvider / SessionContext / KernelDependencies 边界。
3. 选择性迁移 session identifiers、channel message contracts、Feishu canonical user_id、tool runtime canonical surface。

完成这三步后,再决定是开 `codex/integrate-agent-session-feishu` 做普通 merge,还是按子系统 cherry-pick。判断标准只有一个:不能产生第二套 autonomy trigger 机制,不能回退当前 Autonomy P6 账本闭环。

---

## 12. 与原始诉求的逐字对照(确保不偏)

> 原始诉求:**一个极简的、极易拓展的、全面对标 Claude Code 与 Hermes Agent 的 agent 框架**

| 关键词 | 本文档对应方案 | 当前归属 |
|-------|-------------|----------|
| 极简 | 5 层架构 + 删除 facade + 多代叠层清理 | Phase 0R + H1/H2/H6 |
| 极易拓展 | Hooks 对外开放 + Subagent 隔离 + skill marketplace | H1/H3/H5/H6 |
| 全面对标 Claude Code | Hooks/Subagent/Compact + plain-text-first + MCP 已有 | H1/H3/H6 |
| 全面对标 Hermes Agent | 闭环 skill auto-extraction + eval/bakeoff + 多 backend tooling runtime | H2/H5 |
| 系统提示词重点优化 | GEPA-light 演化 HEARTBEAT/DREAM/EXTRACT | H5 |
| skill 重点优化 | 闭环抽取 + 自我 refine | H5 |
| 记忆蒸馏提示词 | EXTRACT_PROMPT 进入 eval/bake-off/GEPA-light | H5 |
| 工程上对标 | feature 治理资产选择性吸收,架构测试常绿 | Phase 0R + H1 |
| 框架上对标 | 6 层 Harness + 单一执行入口 + 单一工具运行时 | H1/H2 |
| agent 效率上 | Cache 命中率 + token 预算 UX + context artifact | H3 |
| 上下文工程上 | ContextEngine + MemoryProvider + reference-only compaction | H3 |
| **自我进化系统上** | **Evaluator + Self-Evolution Ledger** | **H5** |
| 提示词工程上 | GEPA-light / DSPy-light / bakeoff / rollback | **H5** |
| 工具使用上 | ToolRuntimeBackend + hardline policy | H2 |
| agent 任务达成率上 | Evals 闭环 + pass-rate 自动追踪 + 回滚护栏 | **H5** |

> **结论**:原始诉求中提到的所有维度,当前都应映射到 Phase 0R + Harness H1-H6。旧 Architecture Phase 1-5 不再是主执行线,只作为拆解素材。

---

**文档版本**:v1.6 · 2026-04-27 · Phase 0R 护栏落地
**下次修订**:Harness H1 架构测试、permission/tool runtime hardening 或 feature 选择性迁移完成后,用真实 diff/test 结果更新
