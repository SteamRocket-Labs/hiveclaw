# Hive Agent 架构对齐方案 — 对标 Claude Code 与 Hermes Agent

> **状态**:修订稿 v1.2 · 2026-04-27 · Codex 事实校准 + 执行顺序修正
> **作者**:Claude (Opus 4.7) 草案 · Codex 复核修订 · 与 Codex 报告并列(参见 `agent-session-feishu-merge-review.md`)
> **目标**:在当前 main 分支基础上,12 周内达到全面对标 Claude Code 与 Hermes Agent 的极简、易拓展架构

---

## 0. TL;DR(给只看一眼的人)

1. **不要直接合并 `feature/agent-session-feishu`**——会和 main 上 63 个 commit 的 prompt/cache/memory/eval 演进发生真实冲突
2. **先修当前 main 的验证基线,再做集成分支**——当前复核结果不是"18 fail",而是 pytest collection error + ruff 10 errors;frontend test/build 通过
3. **走集成分支策略**——`codex/integrate-agent-session-feishu`,以 main 为基底吸收 feature 的 trunk cleanup,普通 merge 暴露冲突,不要默认使用 `git merge -X ours`
4. **两个战略差距(本文档第一优先级)**:
   - **差距 A**:**Evals 驱动的 prompt 自动优化**——Hermes 生态已明确指向 DSPy + GEPA,但不能把 companion repo 直接等同为 Hermes Agent core 已全量落地;Hive 应先建 eval/bake-off/rollback
   - **差距 B**:**闭环 skill auto-extraction/refinement**——Hermes 本地源码已有后台 memory/skill review;Hive 也已有 `skill_distiller` 与 candidate lifecycle,真正缺的是 outcome-driven refine 与用户审核 promote 闭环
   - 这两个不是"锦上添花",是**架构层面真正需要补齐的系统能力**
5. **架构债不在功能,而在多代叠层和缺少常绿护栏**——`agent_tools.py` 815 行 facade、feature 有 10 个架构测试但 main 尚未吸收、当前 main 验证不绿
6. **方案分 6 个 Phase,12 周推进**——Phase 0-2 是地基(基线清洁 + 消化合并 + 工程治理 + Claude Code 对齐),**Phase 3-4 是核心(直接攻击两个战略差距)**,Phase 5 收尾
7. **结论置信度分层**——"不能直接合并,但值得系统性吸收"信心约 96%;"12 周内完整自我进化系统稳定提升任务达成率"信心约 65%-75%

---

## 0.1 本次 Codex 修订说明:为什么这样改

这次修订不是推翻原始诉求,而是把原文中证据强度不够、执行顺序有风险、验证状态过期的地方改成可落地版本。

1. **把当前状态从历史结论改为工具复核结论。** 原文写 main 有 18 个失败测试,但当前重新运行后不是这个状态:pytest 在 collection 阶段因重复 `test_prompt_cache` 模块名报错,ruff 有 10 个明确问题,frontend test/build 通过。因此 Phase 0 必须先修 baseline,不能直接拿旧失败数当合并门槛。
2. **把 Hermes/GEPA 的表述从"核心系统已完整落地"改成"生态方向已确认、核心落地程度需谨慎"。** 已能确认 Hermes 自进化 companion repo 和 release 文档体现了 DSPy/GEPA/benchmark 方向,但本地 Hermes Agent core 未能证明"所有系统提示词都已进入 GEPA 自动优化 loop"。所以这里作为战略目标保留,作为已完成事实降级。
3. **把 Hive skill distillation 的描述从"固定模板/半人工"修正为"已有保守 distiller,但缺闭环"。** Hive 当前已有 LLM draft、workflow signature、candidate lifecycle、review-only patch 等机制,低估它会导致错误规划;真正缺口是 outcome-driven refine、用户确认 promote、失败样本回灌。
4. **移除 `git merge -X ours` 作为默认集成命令。** 这个策略在当前冲突类型下风险很高,可能静默覆盖 feature 的结构化治理资产或 main 的 prompt/memory 演进。正确做法是普通 merge 暴露冲突,再逐文件决策。
5. **把"能合并"的定义改为零红线验证。** 当前目标是长期对标 Claude Code / Hermes Agent,基础工程门槛不能接受 pytest collection error、ruff error、架构测试缺失或运行态数据污染。Phase 0 的退出条件必须是可重复、可审计、可回滚。

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
| pytest | **collection error**:`backend/tests/runtime/test_prompt_cache.py` 与 `backend/tests/services/test_prompt_cache.py` 模块名冲突;1745 items collected / 1 error | 1223 passed |
| ruff | **10 errors**:unused imports/vars + E402 import-order 问题 | All checks passed |
| frontend test | 68 passed | 67 passed |
| frontend build | passed | passed |

> ⚠️ **本次修正**:原文的"1720 passed / 18 failed"属于过期基线。当前 main 的第一红线是 pytest 还不能完整 collection,第二红线是 ruff 不绿。合并 feature 前应先修复这些 baseline hygiene 问题,否则集成后无法判断失败来自 main、feature 还是冲突解决。

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
| **架构测试** | main 尚未吸收 / feature 10 个 | 不公开但内部一定有 | **工程债** |
| **Hooks 编程接口对外开放** | 内部 15 事件,租户拿不到 | Claude Code 完全开放 | **产品形态债** |
| **Subagent 隔离深度** | `delegate_to_agent` 已有,context 边界不够清晰 | Claude Code Task 工具完全隔离 | **架构精度债** |
| **Tooling runtime 多后端** | in-process 单一 | Hermes 6 backends | **架构能力债** |
| **多代叠层未清理** | `agent_tools.py` 815 行 facade | 未公开但理论上更干净 | **工程债** |

### 4.4 ⭐ 两个战略差距(本文档核心诉求)

这两点不是普通差距,是**架构层面的系统能力差距**。它们不应被包装成已经完全证实的竞品事实,但确实是 Hive 要达到"持续变强的 agent 框架"时必须建设的能力。**这是 12 周路线的真正意义所在**,如果只完成 Phase 0-2 没有触及这两个,等于没有完成对标。

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
- 详细见 Phase 4

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
- 详细见 Phase 3

---

#### 这两个战略差距与 12 周路线的关系

```
Phase 0  → 合并基线        (基础设施)
Phase 1  → 工程治理        (基础设施)
Phase 2  → Claude Code 对齐 (基础设施 — Hooks/Subagent/Compact)
Phase 3  → 闭环 skill auto-extraction  ← ⭐ 攻击差距 B
Phase 4  → GEPA / DSPy / Evals          ← ⭐ 攻击差距 A
Phase 5  → 极简化收尾      (打磨)
```

**Phase 0-2 是地基**:不解决这些,差距 A/B 没法做(没有架构测试、没有 hooks 总线对外、没有干净的 LLM 调用面,自进化无从落脚)。

**Phase 3-4 是攻坚**:这两个 Phase 占整个路线 50% 的难度和 60% 的置信度损失,但**这是对标对齐的真正意义所在**。

**Phase 5 是打磨**:有了 Phase 0-4,最后的极简化才有意义。

---

### 4.5 对标矩阵总结(置信度 87%)

```
                  Claude Code     Hermes Agent     Hive 现状
─────────────────────────────────────────────────────────────
极简度            ●●●●○            ●●●●●            ●●○○○  (815 行 facade)
易拓展度          ●●●●●            ●●●○○            ●●●○○  (hooks 不对外)
工程治理          ●●●●○            ●●●●○            ●●○○○  (feature 有 10 个,main 待吸收)
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
│  L2  Memory (T0/T2/T3/soul + Backend Protocol)                   │
│       职责:plain-text MD 是真源,SQLite/Hindsight 是只读加速器    │
│       禁止:把业务真源写到 MD 之外                                │
│       架构测试:`test_md_is_source_of_truth.py`                    │
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

## 6. 12 周路线图

### Phase 0 — 基线清洁 + 集成准备(W0,本周)

**目标**:先把当前 main 修到可验证基线,再把 feature 的工程治理吸收到 main,不损失 main 的 prompt/cache/memory/eval 演进
**置信度**:90%
**预计工时**:1-2 天

**动作清单**:
1. 修 main 当前验证红线
   - pytest collection error:`backend/tests/runtime/test_prompt_cache.py` 与 `backend/tests/services/test_prompt_cache.py` 模块名冲突
   - ruff 10 errors:unused imports/vars + E402 import-order
   - 先做到 main 自身 `pytest` 可以完整 collection、`ruff check` 全绿
2. 清理运行态数据入库问题
   - `git rm --cached` 只从 git index 取消跟踪,不删除本地运行态文件
   ```bash
   git status --short .ultra/memory
   git rm --cached -r .ultra/memory/
   printf '\\n# Local agent runtime memory\\n.ultra/memory/\\n' >> .gitignore
   ```
3. 切集成分支,普通 merge 暴露冲突
   ```bash
   git switch main
   git pull --ff-only
   git switch -c codex/integrate-agent-session-feishu
   git merge --no-commit origin/feature/agent-session-feishu
   ```
4. 按第 2.5 节冲突表逐文件解决
   - prompt / memory / cache / eval 默认以 main 为准
   - tool runtime / session / Feishu canonical / architecture tests 默认吸收 feature
   - `agents/orchestrator.py`、`task_executor.py`、`memory_service.py` 必须手动合并,不能用整文件覆盖
5. 跑完整验证
   ```bash
   cd /Users/rocky243/vc-saas/hiveclaw-main/backend
   .venv/bin/python -m pytest tests
   .venv/bin/python -m ruff check app tests

   cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
   npm run test -- --run
   npm run build

   cd /Users/rocky243/vc-saas/hiveclaw-main
   git diff --check
   ```
6. 提 PR 到 main(独立 review,不直接合并)

**Exit criteria**:
- ✅ `pytest tests/architecture` 全过(从 0 涨到 10+)
- ✅ `pytest tests` 可完整 collection 且 0 fail;若存在历史 flaky,必须列出具体测试与复现证据
- ✅ `ruff check` 全过
- ✅ frontend test/build 全过
- ✅ `git diff --check` 全过
- ✅ 所有 feature 的 trunk-governance 文档进入 `docs/backend-trunk-governance/`

**回滚方案**:集成分支独立,失败直接弃枝

---

### Phase 1 — 工程治理收尾(W1-W2)

**目标**:完成 815 行 facade 削薄、工具执行主路径收敛、Feishu canonical 整治,并让架构测试成为常绿护栏
**置信度**:88%
**预计工时**:1-2 周 × 1 人

**动作清单**:

**1.1 验证基线归零并防回退**(0.5-1 天)
- 保留 Phase 0 对 pytest collection、ruff、frontend test/build 的修复
- 为 `test_prompt_cache` 模块名冲突增加命名约束或重命名测试文件,避免再次 import mismatch
- 把 feature 的 architecture tests 纳入常规 CI

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

### Phase 2 — Claude Code 关键能力对齐(W3-W4)

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

### Phase 3 — ⭐ 攻克差距 B:闭环 Skill Auto-Extraction(W5-W7)

> 对应第 4.4 节差距 B。**这是 12 周路线的核心攻坚之一**——不完成这个 Phase 就等于没有建立长期自我成长能力。

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

### Phase 4 — ⭐ 攻克差距 A:GEPA/DSPy-light + Evals(W8-W10)

> 对应第 4.4 节差距 A。**这是 12 周路线的另一个核心攻坚**——不完成这个 Phase,提示词优化就仍然主要依赖人工经验。

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

### Phase 5 — 极简化收尾(W11-W12)

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

## 7. 第一刀:本周可立即执行的 5 个动作

按优先级:

| # | 动作 | 风险 | 时间 | 谁做 |
|---|------|------|-----|-----|
| 1 | 修当前 main 的 pytest collection error + ruff 10 errors | 低 | 0.5-1 天 | Claude/Codex |
| 2 | `.ultra/memory/*` 加 `.gitignore` + 删除已 track 运行态数据 | 低 | 10 分钟 | Claude/Codex |
| 3 | 先移植 feature 的 10 个架构测试与 trunk-governance 文档(不改行为) | 低(纯增量) | 2-4 小时 | Claude/Codex |
| 4 | 切 `codex/integrate-agent-session-feishu`,普通 merge 并逐文件解决冲突 | 中 | 1 天 | Claude/Codex + 用户 review |
| 5 | `agent_tools.py` 按 canonical surface/execution 收敛到 < 100 行 | 中(动多个调用方) | 0.5-1 天 | Claude/Codex |

**全部完成 = main 进入"可验证基线",可以承接 Phase 1 之后的所有改造。**

---

## 8. 风险登记册

| ID | 风险 | 概率 | 影响 | 缓解 | 责任 |
|----|------|-----|------|-----|------|
| R1 | Phase 0 合并冲突解错,prompt/cache 进化丢失 | 中 | 高 | 集成分支独立 PR,每个冲突单独 commit,review 到位 | Claude + 用户 |
| R2 | Feishu canonical 修复破坏现网会话 | 中 | 高 | feature 已带 db_legacy_*_migration helper,启动期归一化可回放 | Claude |
| R3 | Phase 3.1 闭环 skill 抽取出垃圾 skill | 高 | 中 | draft 蓄水池 + 用户审核,不直接 promote | Claude |
| R4 | Phase 4 GEPA 让 prompt 退化 | 高 | 高 | evals pass-rate 自动回滚,配人工 override | Claude |
| R5 | 12 周路线被新需求打断 | 高 | 中 | 每个 Phase 独立 PR,可暂停可恢复 | 用户 |
| R6 | Hermes/Claude Code 在 12 周内继续进化(尤其是闭源 Claude Code) | 中 | 低 | 每月 review 对标矩阵 | Claude |

**整体回滚策略**:每个 Phase 一个独立分支 + 独立 PR,Phase N 失败不影响 Phase N-1。

---

## 9. 决策点与推荐默认值

如果没有新的产品方向输入,建议按以下默认值执行:

### Q1:Phase 顺序
- 当前顺序:0 → 1 → 2 → 3 → 4 → 5(工程债先,自进化后)
- 备选:0 → 3 → 1 → 2 → 4 → 5(自进化优先,但风险更高)
- 备选:0 → 1 → 3 → 2 → 4 → 5(工程债 + 自进化双线并行)

### Q2:Phase 0 集成时机
- 选项 A:先修 main baseline,再开 `codex/integrate-agent-session-feishu`(推荐)
- 选项 B:现在立即开集成分支,但不提交 merge commit,只用于冲突侦察
- 选项 C:先做 5 个"第一刀"动作中的非合并部分(action 1、action 2、action 3),合并放到下周

### Q3:Phase 3.1 skill auto-extraction 信号设计
- 选项 A:LLM judge 单审(快,但容易抽出垃圾)
- 选项 B:LLM judge + 用户主动 promote(慢,但精准)
- 选项 C:LLM judge + 用户被动否决(中庸)

### Q4:Phase 4 GEPA-light 选哪些 prompt(差距 A 攻击点)
- 当前候选:HEARTBEAT / DREAM / EXTRACT_PROMPT
- 备选:加 SKILL judge prompt / capability gate prompt / dream consolidation prompt
- 你最关心哪些 prompt 的进化?

### Q4-bis:两个战略差距的优先级
- 差距 A(GEPA-light)和差距 B(skill auto-extraction)哪个先攻克?
  - 选项 A:**先 B 后 A**(默认)——闭环 skill 抽取产生 evals 数据集,作为 GEPA 的判优依据
  - 选项 B:**先 A 后 B**——先把已有 prompt 演化能力建起来,再让 skill 抽取借用同一套 GEPA
  - 选项 C:**双线并行**——两组人马同时推进,12 周变 8 周(如果你有人手)
- 我的推荐:**选项 A**(B 先 A 后),因为 evals 数据集是 GEPA 的输入,B 先做能给 A 喂数据

### Q4-tris:GEPA 实现深度
- 选项 A:**轻量等价版**(默认,置信度 70%)——段落级 mutation + LLM judge,不实现完整论文
- 选项 B:**完整复现 GEPA 论文**(置信度 40%)——按论文算法完整实现
- 选项 C:**直接用 DSPy 框架**(置信度 60%)——不自研,挂 DSPy 的现成实现
- 我的推荐:**选项 A 起步,跑 4 周后看效果决定要不要升级到 B/C**

### Q5:谁来跑 Phase 0 ~ Phase 5
- 选项 A:Claude(我)主导,你 review + 决策
- 选项 B:多 agent 协作(Codex 跑工程治理,Claude 跑自进化)
- 选项 C:你自己主导,Claude/Codex 当工具

### Q6:验收标准
- 12 周完成对标的"完成"是什么定义?
  - 选项 A:对标矩阵 10 个维度全部 ≥ 4/5 星
  - 选项 B(我的推荐):**两个战略差距都闭环**
    - 差距 B 闭环:agent 跑 1 周后,自动产生至少 3 个被用户接受的 skill draft
    - 差距 A 闭环:GEPA-light 跑 4 周后,至少 1 个 prompt 在 evals 上跑赢人工 best-practice 版本
    - 架构测试 100% 常绿
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

### 10.2 main 当前验证失败清单(Codex 复核,2026-04-27)

**pytest 当前第一红线**:

```text
backend/tests/runtime/test_prompt_cache.py
backend/tests/services/test_prompt_cache.py

import file mismatch:
imported module 'test_prompt_cache' has this __file__ attribute:
  backend/tests/runtime/test_prompt_cache.py
which is not the same as the test file we want to collect:
  backend/tests/services/test_prompt_cache.py
```

实际结果:1745 items collected / 1 collection error。也就是说,当前 main 还没有进入可比较的"多少 passed / failed"阶段。

**ruff 当前 10 个错误**:

```text
backend/app/runtime/invoker.py:41 F401 unused apply_prompt_cache_hints
backend/app/runtime/prompt_builder.py:27 E402 module level import not at top of file
backend/app/services/feishu_service.py:9 F401 unused loguru.logger
backend/tests/api/test_admin_memory_backend.py:229 E402 module level import not at top of file
backend/tests/api/test_desktop_auth.py:162 F841 local variable fake_feishu_user assigned but never used
backend/tests/services/test_feishu_calendar_runtime.py:3 F401 unused SimpleNamespace
backend/tests/services/test_feishu_service_api.py:3 F401 unused SimpleNamespace
backend/tests/services/test_skill_distiller.py:192 E402 module level import not at top of file
backend/tests/services/test_tool_config_service.py:8 F401 unused resolve_tool_config_for_tenant_display
backend/tests/services/test_tool_config_service.py:60 F841 local variable tool_enabled assigned but never used
```

**frontend 当前状态**:

```text
npm run test -- --run  # 17 files,68 tests passed
npm run build          # passed
```

> 原文中的"18 个失败测试"应视为历史/过期基线。现在最重要的是先修 collection 和 lint,否则无法判断后续集成质量。

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
工程治理                     ●●●●○         ●●●●○         ●●○○○         ●●●●●
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
| 当前 main 验证不绿的判断 | 95% | pytest collection error + ruff 10 errors 已本机复核 |
| Phase 0 baseline cleanup 可行 | 85% | 问题明确,但修完后可能暴露下一层测试失败 |
| Phase 0 集成方案可行 | 82% | 冲突表已逐个分析,但需要人工逐文件决策 |
| Phase 1 工程治理可达 | 88% | feature 已验证,但要在 main 的新 prompt/memory 基线上重做 |
| Phase 2 Claude Code 对齐可达 | 85% | hook/subagent/compact 三件事都是已知工程 |
| Phase 3 Hermes 对齐可达 | 75% | skill auto-extraction 需要 LLM judge 调优 |
| Phase 4 GEPA-light 可达 | 60% | 需要实验,可能需要更长时间 |
| Phase 5 极简化可达 | 88% | 收尾工作,主要是文档和 UX |
| **整体 12 周达成对标** | **70%** | 主要不确定来自 Phase 3/4 的 eval 数据质量与算法效果 |

---

## 11. 下一步(建议直接执行)

建议不再继续抽象讨论,直接进入 Phase 0 的前 3 个动作:

1. 修当前 main 的 pytest collection error 和 ruff 10 errors。
2. 清理 `.ultra/memory` 运行态数据入库问题。
3. 移植 feature 的 10 个架构测试和 trunk-governance 文档,先不改运行逻辑。

完成这三步后,再开 `codex/integrate-agent-session-feishu` 做普通 merge 和冲突解决。这样做的原因是:先把验证基线变干净,再讨论合并质量,否则所有失败都会混在一起。

---

## 12. 与原始诉求的逐字对照(确保不偏)

> 原始诉求:**一个极简的、极易拓展的、全面对标 Claude Code 与 Hermes Agent 的 agent 框架**

| 关键词 | 本文档对应方案 | 哪个 Phase |
|-------|-------------|----------|
| 极简 | 5 层架构 + 删除 815 行 facade + 多代叠层清理 | Phase 0/1/5 |
| 极易拓展 | Hooks 对外开放 + Subagent 隔离 + skill marketplace | Phase 2/5 |
| 全面对标 Claude Code | Hooks/Subagent/Compact + plain-text-first + MCP 已有 | Phase 2 |
| 全面对标 Hermes Agent | **闭环 skill auto-extraction(差距 B)** + **GEPA/DSPy-light(差距 A)** + 多 backend tooling runtime | **Phase 3 + Phase 4** |
| 系统提示词重点优化 | GEPA-light 演化 HEARTBEAT/DREAM/EXTRACT(差距 A) | **Phase 4** |
| skill 重点优化 | 闭环抽取 + 自我 refine(差距 B) | **Phase 3** |
| 记忆蒸馏提示词 | EXTRACT_PROMPT 进入 eval/bake-off/GEPA-light(差距 A) | **Phase 4** |
| 工程上对标 | main 吸收 feature 的 10+ 架构测试,815 行 facade → < 100 行 | Phase 0/1 |
| 框架上对标 | 5 层架构 + 单一执行入口 + 单一工具运行时 | Phase 1 |
| agent 效率上 | Cache 命中率 + token 预算 UX | Phase 5 |
| 上下文工程上 | 已对齐(frozen/dynamic + section 化 + cache),Phase 0 后稳态 | Phase 0 |
| **自我进化系统上** | **差距 A + 差距 B 双闭环**——这是核心 | **Phase 3 + Phase 4** |
| 提示词工程上 | GEPA-light 演化(差距 A) | **Phase 4** |
| 工具使用上 | 多 backend(local/Docker/SSH) | Phase 3.3 |
| agent 任务达成率上 | Evals 闭环 + pass-rate 自动追踪 + GEPA 回滚护栏 | **Phase 4.1** |

> **结论**:原始诉求中提到的所有维度,本文档都有对应 Phase。**最关键的"自我进化系统"和"提示词工程"两个维度,正是差距 A + 差距 B 攻击的目标**——这是 12 周路线的真正意义所在。

---

**文档版本**:v1.2 · 2026-04-27 · Codex 事实校准 + Phase 0 执行顺序修正
**下次修订**:Phase 0 前三项执行完成后,用真实 diff/test 结果更新
