# Workflow vs Skill vs A2A:Hive 确定性编排层的讨论起点

> **状态**:讨论稿 v0.1 — 这是用来**起讨论**的,不是结论,更不是计划。
> **日期**:2026-05-29 · **分支**:main
> **方法**:由 3 个 file-grounded 调查产出(A2A recon + 三方辩论 + 多租户底座调查),共 16 个 subagent,关键 file:line 经独立 verify 阶段复核(delegation 44/44、kernel 8/8、coordination 20/20;skills 8/10 有 2 处小错已剔除)。
> **证据等级**:`Fact`=工具/契约写明 · `实证`=本会话亲自从文件确认 · `调查`=subagent 报告的 file:line(多数可信,**安全相关的几条标注"待人工复核",未当既成事实**)。

---

## 0. 一页纸:这场讨论到底在争什么

我们**不是**在争"要不要让多个 agent 协作"——Hive 已经有了(A2A + 子代理委派,生产在跑)。

我们在争一件更窄、更深的事:

> **要不要给 Hive 的"确定性关键管线"(deep-research、自我进化、定时治理)加一层"由代码、而非 LLM 拥有控制流"的编排?**

三个事实把这个问题从"该不该引入陌生概念"重构成了"该不该推广一个我们已经在用的内部模式":

1. **Hive 已经有一个代码驱动 workflow** —— `finance_analysis/workflow_runner.py` 的 `FinanceWorkflowRunner`。它是纯同步 Python、单 agent、无检查点/预算,但**证明了"代码拥有控制流"在 Hive 架构里不是外来物**。
2. **Hive 已经有一个休眠的预算驱动循环** —— `deep_research/controller.py`(`controller_mode` 默认 `False`,有 `_DEFAULT_TOKEN_BUDGET=200K` + 85% 阈值)。能力在,没接生产。
3. **真实痛点已经流血** —— deep-research RC13/RC15:`COVERAGE IS MANDATORY` 这句**散文指令**(`reasoner.py:827`)压不住"6 lane 只写 3 维度";一个 `for dim in plan: assert report.has(dim)` 就能确定性压住。

**但是**(这是你今天戳的关键):Hive 是**云端多租户**平台,它的"子代理"底座和 Claude Code 的"单机单用户 Workflow"底座**根本不同**——移植不是搬机制,是在一个**更受限的底座**上重建,难度乘上了"租户隔离 + 公平性 + 跨进程 + 计费"。这一层是第 3 节,是本稿的重心。

---

## 1. 四个概念,各在哪一层

| 概念 | 是什么 | 控制流归属 | Hive 现状(file:line) |
|---|---|---|---|
| **Skill** | 带 YAML frontmatter 的 markdown,注入提示词 | **LLM**(纯声明,零逻辑) | `skills/types.py` SkillMetadata 全是标量/元组字段 |
| **子代理 / 委派** | 父 agent 派生隔离子 agent 干活 | **LLM**(模型调 `delegate_to_agent` 才发生) | `orchestrator.py:592`(同步)`:945`(后台 asyncio.Task) |
| **A2A**(peer) | agent ↔ agent 同步问答(双方全权限) | **LLM**(模型调 `send_message_to_agent`) | `messaging.py:840-1073`,共享 `agent_pair_session` |
| **Coordinator Mode** | 教 agent "分解→fan-out→综合"的 SOP | **LLM**(散文 decision matrix,只收窄工具面不排序) | `coordinator.py:46-184` |
| **Workflow**(Claude Code) | 主循环提交的 JS 脚本,引擎保证执行顺序 | **代码**(LLM 只填叶子) | 无对应物(本讨论的标的) |

> **关键**:Hive 今天的"编排"——Coordinator Mode、skill 里的 plan-gate、`reasoner.py` 的 coverage 强制——**全部是写给模型看的散文**。`coordination.py` 的 Lease/Signal/Checkpoint 是代码拥有的,但它们回答"谁抢到锁",不回答"下一步该 fan-out 还是综合"。

---

## 2. 核心洞察:正交两轴

这把"我们都有 A2A 了为啥还要 Workflow"的结解开了:

```
            谁拥有控制流?
            LLM 决定每一步        代码决定每一步
          ┌──────────────────┬──────────────────┐
 单 agent │ 普通对话 agent     │ FinanceWorkflow   │  ← 已有!纯同步单 agent
          │ (kernel 主循环)    │ Runner            │
谁  ──────┼──────────────────┼──────────────────┤
干活      │ A2A / 子代理委派   │  ❓ 空白           │  ← 这格是标的:
 多 agent │ Coordinator Mode  │  (多 agent +       │     多 agent 且代码拥有控制流
          │ (已有)            │   代码控制流)      │
          └──────────────────┴──────────────────┘
```

- **A2A、子代理、Coordinator** 全在**左列**(LLM 拥有控制流),只是从单 agent 走到了多 agent。
- **Workflow** 是把系统推到**右下格**:多 agent **且**代码拥有控制流。
- `FinanceWorkflowRunner` 已经在**右上格**——证明右列在 Hive 能存在,只是还没走到多 agent。

**所以移植 Workflow 的"意义" = 把已经验证过的右列模式(代码控制流),从单 agent 推广到多 agent 的确定性关键管线。** 不是"更多 agent"(那是左列,已有),是"可确定、可恢复、可审计的控制流"。

---

## 3. ⭐ Hive 的真实底座(云端多租户)—— 移植的硬约束

**这是和 Claude Code Workflow 最根本的差异。** Claude Code 跑在单机单用户:subagent 是本地 sidechain 进程、journal 是本地文件、并发 = `min(16, cpu-2)` 一台机器、budget = 一个人的额度、**无租户概念**。

Hive 的底座完全不同,而且约束比直觉更严(`调查`,关键条经 file 确认):

### 3.1 执行底座

| 约束 | 严重度 | file:line | 含义 |
|---|---|---|---|
| `delegate_async` = 在**当前 FastAPI worker 进程**里 `asyncio.create_task` | — | `orchestrator.py:933` | 不是 OS 进程/线程/外部队列 |
| `_async_tasks` 是**模块全局 dict,跨所有租户共享,零隔离** | 🔴 blocker | `orchestrator.py:340` | 一个租户能占满全局池 |
| `_MAX_TRACKED_TASKS=200` 是**全进程全局上限**,溢出只 WARN 不拒绝 | 🟠 constraint | `orchestrator.py:341,391-397` | 无 admission control |
| **无 per-tenant 公平/配额/并发限制**,先到先得 | 🔴 blocker | `orchestrator.py:962,979,993` | 吵闹邻居零隔离 |
| **CPU-bound/阻塞叶子会卡住共享 event loop,饿死同 worker 所有租户** | 🔴 blocker | `orchestrator.py:801-810` | 无 `run_in_executor` 隔离 |
| 重启**丢光 in-flight 任务**,lifespan 关闭不 drain `_async_tasks` | 🔴 blocker | `main.py:399-413` | 每次部署/重启都孤儿化在跑的任务 |

### 3.2 部署拓扑

| 约束 | 严重度 | file:line | 含义 |
|---|---|---|---|
| 生产是**单 async worker**(`uvicorn` 无 `--workers`) | 🔴 blocker | `entrypoint.sh:172` | 全平台一个 event loop |
| 横向扩展时 in-flight asyncio.Task **钉死单进程,跨 worker 不可见** | 🔴 blocker | `orchestrator.py:340,1076` | 加 `--workers` 即破 |
| `check_async_delegation` 内存找不到会**回落查 DB `RuntimeTask`** | 🟢 缓解 | `orchestrator.py:1076-1090` | 跨 worker 查"状态"可以,但驱动任务跑完不行 |
| `resume_persisted_async_delegations` **只在 worker 启动时跑一次** | 🟠 constraint | `main.py:270-274` | 非持续,孤儿任务无人接管 |
| **Redis Streams `event_bus.py` 已存在(含 consumer group),但没接委派** | 🟢 机会 | `core/event_bus.py:1-110` | 跨 worker 持久编排的现成 seam |

### 3.3 租户隔离 + 配额(最尖锐,含安全)

| 约束 | 严重度 | file:line | 含义 |
|---|---|---|---|
| `tenant_id` **未显式传给** `delegate_async` 子任务;子 agent 自己从 Agent 记录解析 | 🔴 blocker | `orchestrator.py:962`(参数存在却没用) | 父子租户不一致时有歧义 |
| 子任务 RLS 依赖 **ContextVar 自动复制**,而背景任务**不走 TenantMiddleware** → tenant_id 可能 stale/None | 🔴 blocker **(安全,待人工复核)** | `database.py:49`,`tenant_middleware.py:58-98` | 可能 RLS 作用域错误 |
| per-tenant token 配额**只在 User 级、且只在入口预检**,委派子任务不复检 | 🔴 blocker | `quota_guard.py:24-66`,`token_tracker.py:32-84` | fan-out N 个子 agent 可绕过用户限额 |
| **无 workflow 级 / 跨任务树的 token 预算池** | 🔴 blocker | NOT FOUND | Claude Code 那个单一 budget 池不能照搬 |

> ⚠️ **3.3 的安全条目我没有亲自复核**(ContextVar × asyncio.Task × RLS 的真实行为需要写测试验证)。但若属实,它**独立于本讨论**就是个值得排查的 pre-existing 风险——见第 7 节。

**结论(第 3 节)**:任何"移植 Workflow"的方案,如果照搬 Claude Code 的"本地 spawn + 内存 journal + 单一 budget",在 Hive 多租户底座上**会引入 noisy-neighbor、RLS 越界、配额绕过、部署即孤儿**四类问题。移植的真实工作量,大头在**让底座多租户正确**,不在控制流语法本身。

---

## 4. "意义"三问

**Q1:Hive 真的需要吗?**
需要,但**有范围**。对话型 / 开放探索型 agent **不需要**——用户期待的就是 LLM 自主决策,Coordinator Mode 够用。需要的是**确定性关键管线**:deep-research 多 lane + 综合、evolution daemon 的 candidate→eval→promote、定时多步治理任务。这些地方"跳了一步/重复一步/恢复时从头跑"是真实损失。

**Q2:哪里最痛?**
deep-research(RC13/RC15 实锤:散文压不住覆盖)、自我进化(eval 跑一半崩 → 从头重跑 30 个测试)、未来的公司级定时治理(跨 lane 共享预算,一个 lane 不能吃光全公司额度)。

**Q3:哪里是过度工程?**
把 workflow DSL 强加给所有 agent 交互;为单步任务建 DAG;在 80% 是 1-3 个工具调用的短任务上做分步 journal。**范围纪律是这个方案成立的前提。**

---

## 5. 三方辩论(独立产出,非一家之言)

每方都由独立 subagent 论证,并被要求"为自己的立场找最强反驳"。

### 🟥 怀疑派:"先别建 runtime,硬化现有栈"
- **核心**:A2A + Coordinator + LoopGuard + objective ledger 已覆盖 ~85%;RC13/RC15 是 prompt 问题不是 runtime 问题;新建确定性 runtime 是维护负担。
- **要害论据**:`loop_guard.py:41-97` 已有确定性阈值;`objective.py` AgentObjective 已是"80% 的 journal";skill 声明式是**设计选择**不是缺陷(锁进代码 DSL 反而失去敏捷)。
- **自承软肋**:**分步 resume + 确定性它压不住**——10 步任务崩在第 8 步,从头跑;高负载下 LLM 会忘综合步。
- **何时这方对**:多数任务是只读/松耦合;团队愿意花 3-6 个月严格执行 Coordinator SOP 并实测失败模式再决定。

### 🟩 拥护派:"代码确定性是正交且缺失的层,刚需"
- **核心**:LLM 拥有的控制流**根本不可确定/不可恢复**;这对"自我进化 + 公司控制中台"是关键漏洞。
- **要害论据**:`COVERAGE IS MANDATORY`(`reasoner.py:827`)是散文,预算耗尽时模型要么违规略过维度、要么编造,**都失败**;`for dim: assert` 才确定性。自我进化的 candidate→eval→promote 若 eval 崩,`resume`(`:1249`)从**原始输入**重跑,丢光中间结果。
- **自承软肋**:"把 LLM 调得更强 + 激活 `controller.py`"也许够;但 `controller.py` 休眠,且 LLM 预算循环仍不解决"崩溃后拿不到子任务部分结果"。
- **何时这方对**:扩到 5-10 并发 run 时,resume 失败 + 预算超支让 deep-research 综合不可靠。

### 🟦 实用派:"移植内核,不移植平台;复用 A2A 当叶子"
- **核心**:**不重建**子代理/A2A;只加两个原语(**代码控制流 + 分步 checkpoint journal**),用现成 `delegate_async` 当执行叶子、`acquire_lease` 当并发、`action_preflight` 当门。**只用在确定性关键管线**。
- **要害论据**:`FinanceWorkflowRunner` 就是现成范本(代码决定步序,quality_gates 是结构体字段不是散文);复用 `delegate_async` 避免重造并发/租户/回调机器。
- **自承软肋**:实用主义可能藏债——"workflow lite"缺 workflow 级超时/审计/回滚/fork-join,团队迟早要;须**前置定义最小边界**。
- **何时这方对**:有确定性危机 + 团队认可 finance 范本 + 领导层承诺只用于受限的确定性域。

> **三方共识**:控制流确定性的差距是真的;分歧在**时机**(现在建 vs 先硬化散文)和**范围**(只建内核 vs 全套)。**没有一方主张照搬 Claude Code 的本地模型**——都默认要适配 Hive 底座。

---

## 6. 若要做:移植 ≠ 重建(多租户正确版)

把第 3 节的约束焊进实用派的最小方案:

1. **分步 journal**:扩 `models/coordination.py` 加 `CoordinationWorkflowStep`(run_id/step_id/status/input_hash/result_ref/**tenant_id**),`coordination_repository.py` 加 `record_step/load_steps`——照抄 `acquire_lease` 的原子 upsert + **tenant 作用域**。已 done 的步跳过 = 分步 resume。
2. **代码控制流引擎** `runtime/workflow_engine.py`:`phase/parallel/pipeline`,每步前后写 journal。**直接照 `FinanceWorkflowRunner` 的形**,只是把同步方法换成 `delegate_async` 叶子。
3. **`agent()` = `delegate_async` 叶子**,但补多租户:
   - **显式传 `tenant_id`** 进子任务并 `set_current_tenant`(别赖 ContextVar 自动复制);
   - 叶子的阻塞/CPU 工作包 `run_in_executor`,**别卡 event loop**;
   - **per-(tenant,run) admission**:超并发/超配额**硬拒**,不是 WARN。
4. **per-tenant 预算信封**(不是单一池):`workflow_quotas(tenant_id, run_id, allocated, consumed)`,**spawn 时扣、不是完成时扣**,Postgres advisory lock 跨任务树原子聚合;耗尽 → 失败/降级。复用 `controller.py` 已有的 budget 概念。
5. **安全门**:每个对外/不可逆 `phase` 边界过 `action_preflight.evaluate`;ESCALATE/ASK 写 `CoordinationCheckpoint`(复用现成 human-in-the-loop)。
6. **跨 worker 持久化**:in-flight 状态从内存 `_async_tasks` 迁到 **Redis Streams `event_bus`(已存在)** 或 DB 为单一真相;lifespan **优雅 drain**;K8s SIGTERM 宽限期足够持久化。

> **最大缺口仍是**:exactly-once 的分步持久化——step 完成必须在副作用可见**前**原子落盘,而工具副作用(发邮件/转账)和 Postgres 写不在同一事务。`action_preflight` 的可逆性分级能帮判断哪些步可安全重试。

---

## 7. 一个 pre-existing 风险(独立于本讨论,建议独立排查)

第 3.3 节的调查**顺带**发现:**背景 `delegate_async` 子任务的租户作用域可能不可靠**——它不走 `TenantMiddleware`,RLS 的 `_current_tenant_id` 靠 ContextVar 在 `asyncio.create_task` 时的自动复制;若父请求上下文已结束或被置 None,子任务可能用错(或空)tenant 作用域查库。

- **这与要不要做 Workflow 无关**——是当前后台委派路径就存在的潜在问题。
- **我没有亲自验证**(`调查`级)。建议:写一个集成测试,在请求结束后触发 `delegate_async` 子任务,断言其 DB 会话的 `app.current_tenant_id` 仍正确。
- 若属实,优先级应高于 Workflow 讨论(涉及租户数据隔离)。
- 证据:`orchestrator.py:933,962`、`database.py:29,49`、`core/tenant_middleware.py:58-98`、`runtime/invoker.py:159-202`。

---

## 8. 待决问题清单(留给我们一起拍)

1. **范围**:认同"只用于确定性关键管线(deep-research/进化/定时治理),不碰对话型 agent"这条边界吗?还是想讨论更大/更小范围?
2. **时机**:先按怀疑派硬化散文 SOP 跑一阵实测失败率,还是直接按实用派建最小内核?
3. **底座顺序**:多租户正确(第 6 步:Redis Streams + 优雅 drain + per-tenant 配额)是 Workflow 的**前置**,还是**伴随**?——注意现在单 worker,blocker 多数"还没爆"但加 `--workers` 即爆。
4. **第 7 节安全风险**:要不要**先于本讨论**独立排查 + 修?
5. **复用 vs 新建**:`controller.py`(休眠预算循环)+ `FinanceWorkflowRunner`(代码步序)——是把这两个**长成**通用引擎,还是另起 `workflow_engine`?
6. **要不要 walking skeleton**:`CoordinationWorkflowStep` 表 + 最小 `run_workflow` 跑两个顺序 phase + 一个 kill-resume 集成测试(Testcontainers 真 Postgres),把"代码拥有控制流 + journal 真持久 + 分步 resume"一次性立住?

---

## 附录:证据索引

- **A2A / 委派**:`orchestrator.py:592/945/1070/1249/340/341`、`messaging.py:840-1073/1075-1135`、`communication.py:107-297`、`delegation_token.py:32-111`(delegation 维度 verify 44/44)
- **内核 / 控制流**:`kernel/engine.py:1169(handle)/1522(主循环)/2038(parallel gather)`、`runtime/invoker.py:920(invoke_agent)`、`runtime/coordinator.py:46-184`(kernel 维度 verify 8/8)
- **协调 / 治理**:`coordination.py:72-227`、`coordination_repository.py:46-99`、`models/coordination.py:23-68`、`governance.py:155-178`、`action_preflight.py:73-100`、`evolution_ledger.py:30-288`(coordination 维度 verify 20/20)
- **已有 workflow 范本**:`finance_analysis/workflow_runner.py`、`finance_analysis/workflows.py:12-55`、`deep_research/controller.py`(休眠)、`deep_research/reasoner.py:827`(COVERAGE 散文)
- **多租户底座**:`entrypoint.sh:172`、`main.py:270-274/399-413`、`core/event_bus.py:1-110`、`core/events.py:22-25`、`quota_guard.py:24-66`、`token_tracker.py:32-84`、`database.py:29/49`、`core/tenant_middleware.py:58-98`(本轮未单独 verify,安全条目标注待复核)

> 本稿由 Claude 基于会话内 3 个调查 workflow 撰写;`实证`/`Fact` 之外的 file:line 属 `调查`级,采纳前建议对安全相关条目做针对性验证。
