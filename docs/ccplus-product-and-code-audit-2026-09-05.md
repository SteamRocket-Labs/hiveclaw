---
document_id: hive-ccplus-product-and-code-audit-2026-09-05
owner: Codex
status: review-report
authority: advisory-audit-snapshot
last_reviewed: 2026-09-05
source_commit: 0ce51f049e03c689a440075a5de8a7a9d99c609c
verification_status: source-inspected-targeted-checks-passed-production-acceptance-open
---

# Hive CC+ 产品目标、系统差距与代码精简审计

[当前验收入口](acceptance/2026-08-30-weekend-rc/README.md) · [CCPlus 产品合同](ccplus-north-star-contract-2026-06-24.md) · [当前状态](acceptance/2026-08-30-weekend-rc/03-current-status.md)

审计日期：2026-09-05，Asia/Shanghai。成文状态核对时间：04:56（2026-09-04 20:56 UTC）。

本文回答：我们的目标到底是什么；当前架构方向是否正确；系统距离目标还有哪些实质差距；代码是否过度抽象、符合 KISS 和奥卡姆剃刀；是否具备成熟企业 SaaS 的生产交付条件。

本文是一次有日期的审计快照和建议，不是新的目标合同、执行状态或发布 verdict。用户授权本轮评估与成文；代码精简、依赖变更、提交和部署均未在本轮执行。后续事实变化以现有验收文档组为准。

## 1. 结论与建议

**总体方向正确，核心运行底座已经形成；当前还不能认定达到 CC+，也尚不具备成熟企业 SaaS 的整体生产交付条件。**

已有实现覆盖模型调用、工具执行、Session、记忆、知识、权限、协作、外部连接和恢复。关键差距集中在用户主路径贯通、产品规则一致性、同条件能力评测、复杂度收敛及生产恢复证明。

| 核心问题 | 本轮判断 | 对下一阶段的含义 |
|---|---|---|
| 单 Agent 架构是否走对 | 基本正确：有统一 Kernel、原生模型/工具循环和受治理执行边界 | 保留核心形态，优先证明完整任务能力 |
| 是否已经达到 CC+ | 尚未证实 | 用同模型、同工具、同预算、同任务条件的评测确认 |
| 是否轻量、小白友好 | 尚未稳定达到 | 降低入口、概念和恢复操作负担；技术详情按需披露 |
| 企业能力是否完整 | 覆盖广，关键合同与生命周期仍未闭环 | 对齐三角色规则，验证真实正负路径和撤销恢复 |
| 是否存在过度抽象 | 存在，有具体代码与调用关系支持 | 先删无消费者的层，再收敛核心依赖和重复状态 |
| 是否符合 KISS、奥卡姆剃刀 | 局部符合，整体仍需收敛 | 每层都应有实际消费者、职责和可验证收益 |
| 是否达到生产级 | 有较好的工程基础，整体未达 | 补齐构建一致性、行为证据、恢复与发布验证 |

建议下一阶段沿现有修复流程推进，把重点放在普通员工从创建 Agent、发起任务、使用工具、取得交付物，到刷新和故障后继续工作的完整路径。代码精简应降低这条路径的维护与排障成本。

## 2. 目标的准确含义

### 2.1 一句话产品定义

**让普通员工无需理解 Agent 框架，就能使用具备 CC 级任务执行能力、持续记忆和成长能力的数字员工；企业能够安全地配置、管理、连接、观测和恢复这些数字员工。**

这一表述综合了本次用户要求与既有 [SOTA 总目标](hive-sota-master-goal.md)、[CCPlus 合同](ccplus-north-star-contract-2026-06-24.md)、[当前 North Star](acceptance/2026-08-30-weekend-rc/01-north-star-and-boundaries.md)。本报告没有新增产品范围或替代 owner 已确认的决定。

### 2.2 CC+ 的三个部分

| 部分 | 实质要求 | 应提供的证明 |
|---|---|---|
| CC 能力与生命周期基线 | 保留模型、授权上下文、工具、规划、执行、压缩、取消、恢复、分叉等能力 | 真实任务与生命周期对照；能力不能仅按接口是否存在计分 |
| Hive 原生增量 | 持久身份、记忆、学习、Skill 成长、知识与企业管理 | 用户或后续 Agent 真实消费这些能力，并取得可核验结果 |
| 产品与工程增量 | Web 交互、状态连续性、可解释失败、权限、审计、可靠恢复 | 用户从入口走到交付；异常后能继续且不重复副作用 |

CC 的本地生命周期语义在范围内；依赖供应商私有远程基础设施的能力，按既有合同处理。本轮没有重新研究当日最新 CC 版本，也没有运行与最新 CC 的完整 bakeoff，因此不作最新竞品胜负声明。

### 2.3 单 Agent 的边界

主 Agent 需要独立完成理解、探索、规划、调用工具、解释反馈、修正策略和交付。平台负责身份、资源、外部效果、持久化与恢复。

单 Agent 架构允许按需使用原生 subagent。Plan、Goal、Workflow、Team、Trigger 和 Work Ledger 各有不同职责，不应隐式替换主 Agent 的语义判断，也不应成为每个普通任务的前置流程。

本次开发协作角色与产品运行时的 Agent 拓扑是两个问题。审计不改变 PDEC-012 的开发分工。

### 2.4 轻量、小白友好、企业完整与外联性

| 目标 | 用户能够观察到的行为 |
|---|---|
| 使用轻量 | 默认围绕任务、必要决定、进度和交付物操作；无需理解内部对象和状态机 |
| 小白友好 | 清楚知道系统在做什么、为什么需要确认、失败后可以做什么 |
| 企业完整 | 管理员在合法公司范围内完成管理，员工正确使用自己的和公开的 Agent，凭据受保护 |
| 鲁棒性 | 断线和刷新不丢工作；重试不重复效果；权限变化后旧运行与缓存正确收敛 |
| 外联性 | 能连接、发现、调用、观察结果，并处理断连、撤销和外部不可用 |
| 运维轻量 | 能定位失败层、重复构建已验版本、恢复数据和任务，减少人工修补 |

代码行数是复杂度观察信号。是否轻量，最终还要看职责、运行成本、排障成本和用户操作负担。

## 3. 审计范围与证据强度

### 3.1 本轮实际完成

- 核对当前 Git 根目录、分支、HEAD 和未提交变更；工作区存在其他验收任务正在处理的候选。
- 读取目标、产品边界、当前状态、Findings、manifest、CI 与生产 Dockerfile。
- 扫描仓库结构、源码规模、长函数、抽象定义、调用关系和部分精确重复函数体。
- 深读核心模型/工具调用链、Session 历史与终态、共享权限、HR 入口、前端状态投影和部分知识界面。
- 执行 104 项后端测试、110 项前端测试、TypeScript 检查与后端 Ruff 扫描。
- 只读查询 Railway 三个应用服务的部署状态。

这属于全仓结构扫描与关键路径审计，没有逐行审核所有应用代码。未执行生产全量 E2E、正式能力 bakeoff、生产压测、数据恢复演练或新的跨租户生产探针。对“缺少证据”的判断不自动等于“实现不存在”。

### 3.2 证据分类

| 标记 | 含义 | 本文示例 |
|---|---|---|
| 源码事实 | 本轮直接读到的实现或调用关系 | HR 拒绝平台管理员；生产未消费 Python 锁文件 |
| 本轮验证 | 本会话实际执行并返回成功的检查 | 定向测试、类型检查、Ruff、Railway 状态 |
| 当前记录 | 从既有状态/证据文档读取，本轮未独立重跑 | NPTCR、既有生产故障、其他 review 的完成情况 |
| 审计判断 | 基于事实形成的工程或产品判断 | 核心依赖传递过重、用户路径需要收敛 |
| 推进建议 | 尚未实施或单独验收的改进 | 删除孤立框架、收敛状态所有权、统一构建输入 |

Hindsight 用于定位项目决策和文档结构，当前源码与 canonical 文档用于核验。记忆中的旧执行分工、旧管理员拒绝规则或历史 PASS 不作为当前授权与验收依据。

### 3.3 源码规模观察

按 Git 已跟踪文件清单、读取工作区内容统计：后端应用 Python 源码排除 `app/evals` 后约 34.1 万行；前端 TS/TSX/CSS 排除测试后约 9.0 万行。合计约 43 万行，包含注释、空行和生成代码，不能作为过度设计的单独证据。

更有意义的信号是：核心 turn 函数约 2,590 行、单次调用绑定 91 个模块名称，以及 Session 页面同时维护多套状态与兼容投影。它们直接影响代码阅读、变更和排障。

## 4. 当前系统形态与目标差距

### 4.1 已检查的主路径

```mermaid
flowchart TD
    A[用户输入与 Session] --> B[持久任务与 Web Chat 生命周期]
    B --> C[Invoker 与调用上下文]
    C --> D[AgentKernel 与模型工具循环]
    D --> E[权限 秘密保护 租约与工具执行]
    E --> D
    D --> F[结果 终态事务与恢复]
    F --> G[前端投影与交付物]
    F --> H[提交后的学习与记忆处理]
```

这是检查范围内的职责示意，不表示每个入口都已经通过生产验证，也不意味着所有子模块都必须串行执行。

| 路径 | 当前代码入口 | 观察 |
|---|---|---|
| Web Chat 生命周期 | [web_chat_runtime.py](../backend/app/services/web_chat_runtime.py)、[web_chat_run_orchestrator.py](../backend/app/services/web_chat_run_orchestrator.py) | 持久 run、事件、上下文和终态有真实代码，但依赖装配较重 |
| 调用与 Kernel | [invoker.py](../backend/app/runtime/invoker.py)、[engine.py](../backend/app/kernel/engine.py)、[turn_orchestrator.py](../backend/app/kernel/turn_orchestrator.py) | 存在统一模型/工具循环，接口和内部支持代码仍高度耦合 |
| 工具执行 | [execution_pipeline.py](../backend/app/tools/execution_pipeline.py)、[service.py](../backend/app/tools/service.py) | 授权、预执行检查与实际执行已连接 |
| Session 历史 | [session_semantic_history.py](../backend/app/services/session_semantic_history.py) | 从 canonical transcript 和已提交模型结果重建历史 |
| 终态后处理 | [runtime_terminal_boundary_outbox.py](../backend/app/services/runtime_terminal_boundary_outbox.py)、[web_terminal_boundary_processor.py](../backend/app/services/web_terminal_boundary_processor.py) | 有持久 outbox、绑定验证与提交后处理机制 |
| 前端消费 | [AgentDetail.tsx](../frontend/src/pages/AgentDetail.tsx)、[sessionEventStore.ts](../frontend/src/pages/session-workbench/sessionEventStore.ts) | 有 canonical store，同时保留多种实时、历史及兼容状态 |

### 4.2 分维度判断

| 维度 | 已有基础 | 尚缺的关键证明 | 判断 |
|---|---|---|---|
| 单 Agent 智能 | 原生模型/工具循环、配置模型路由、工具发现与受治理执行 | 同条件开放任务、真实交付物、重复运行可靠性、成本和人工介入数据 | 机制已有，CC+ 未证实 |
| Session | 输入、历史、事件、分叉、恢复及终态处理 | 普通员工真实入口下的连续任务、刷新/断线/取消/重试双遍 | 主链较完整，交付未闭环 |
| 记忆与成长 | 真实记忆检索、提交后处理和学习管线 | T0→T2→T3→Soul/Skill 的真实消费，以及相对基线的收益 | 基础存在，效果未充分证明 |
| Personal/Company KB | 工具入口、管理界面、导入与权限实现 | 多格式、多入口、解析索引、引用、角色和恢复的完整生产旅程 | 覆盖广，质量与体验待验 |
| 企业权限 | 租户、角色、Agent/Session 权限、审计和撤销机制 | PDEC-013 全面落地，真实 actor 与公司范围贯穿生命周期 | 合同对齐未完成 |
| 外联能力 | provider 客户端、MCP、渠道服务、本地 Agent 和代码执行路径 | connect→call→result→disconnect→reconnect→revoke，以及外部不可用时的恢复 | 实现存在，完整性待验 |
| 小白体验 | 部分渐进披露、折叠面板、状态和错误翻译 | 创建、导航、知识详情、窄屏、键盘、危险确认等真实可用性 | 尚未稳定达标 |
| 生产运营 | CI、数据库事务、RLS、租约、outbox、部署流程 | 构建一致性、最终部署、故障恢复、回滚、清理和全量双遍 | 整体未达交付门 |

### 4.3 如何理解“还差多少”

当前 manifest 是 35 组、96 条旅程；状态文档记录 current-manifest pass 1/pass 2 均未完成，NPTCR 为 0/96。此数字描述发布验收闭环，不能拿来表示功能开发完成度。

现有证据不足以给出可信的“已完成 80%”或“只差两周”。剩余工作可以明确为四个交付集合：

1. **能力集合**：固定任务条件，验证主 Agent 能力和记忆成长收益。
2. **用户集合**：普通员工通过真实入口完成任务、交付和恢复，无需工程人员补状态。
3. **企业集合**：三角色规则、公司范围、凭据保护、撤销与外部连接一致。
4. **发布集合**：同一应用版本完成三服务部署、双遍、故障、回滚与清理。

当前表现属于功能覆盖广、关键链路仍在收敛的阶段。应通过这些集合的可观察结果衡量推进，避免只按代码量、接口数或测试数计进度。

### 4.4 成文时的并行进展

以下来自成文时重新读取的 [当前状态](acceptance/2026-08-30-weekend-rc/03-current-status.md) 与 [Findings](acceptance/2026-08-30-weekend-rc/05-findings.md)，不是本审计重新执行的结果：

- ChatMessage 跨租户谓词小切片已完成 CC→Codex 独立审查与本地验证，不能继续记作等待首审。
- Feishu 维护路径新增两个已被既有验收流程真实 PostgreSQL 复现的跨租户问题，处于修正流程中。
- 窄屏列表高度、真实 resize 和危险确认焦点仍有候选推进；本报告不把候选写成已发布结果。
- PDEC-013 方案已收敛，源码、manifest 和角色 UI 的整体实施仍未完成。
- 正式三方 bakeoff、M0 重大节点对账与最终生产验收仍未完成。

这些进展说明工作区会继续变化。本文只保留本次快照，后续状态不在这里追加维护。

## 5. 应保留的架构选择

### 5.1 模型能力与平台边界分开

[resolve_turn_model_route](../backend/app/runtime/context_budget.py) 明确保留配置的主模型，不根据用户措辞决定降级。[LoopGuard](../backend/app/kernel/loop_guard.py) 的启发式 `_escalate` 主要输出一次模型可见警告；精确失败证据另有受约束的终止条件。

这与“模型负责语义，平台负责精确事实和效果”的方向一致。本轮没有测量所有控制的能力损耗，不能据此声称所有限制都已合理。

### 5.2 持久化与恢复机制有真实职责

canonical transcript、已提交结果、租约、终态事务和 outbox 分别保护顺序、证据、执行归属和崩溃恢复。这些保护不能因为行数多就删除。

精简应消除重复实现和模糊归属。某条路径若已经由共享事务或 outbox 保证，应检查周边是否还保留独立的重复状态与补偿路径。

### 5.3 多 provider 与真实执行环境需要适配边界

[llm_client.py](../backend/app/services/llm_client.py) 有实际的 OpenAI-compatible、Responses、Gemini、Anthropic 客户端实现。[代码执行服务](../backend/app/services/code_execution/service.py) 有 local OS sandbox 与 Vercel provider 的真实分流。

这些属于存在多个实际实现和协议差异的边界。应与没有消费者的接口、只做转发的占位类区别处理。

## 6. 主要审计发现

本节严重度是审计建议。已有产品缺陷回链现有 Findings；新增维护性和构建观察尚未自动成为执行工单或发布门。

### 6.1 P1：CI 与生产依赖输入不一致

**源码事实。** [Harness CI](../.github/workflows/harness-ci.yml) 使用 `uv sync --frozen --extra dev`；根 [Dockerfile](../Dockerfile) 只复制 `backend/pyproject.toml` 并执行 `pip install .`。依赖声明大量使用宽版本范围；Dockerfile 还全局安装未固定版本的 `@larksuite/cli`。

**影响。** 同一个源码提交在不同时间可能得到不同依赖集合；CI 的绿结果不能完整约束生产依赖。回滚源码也不保证恢复原先运行时。本文确认的是重现性缺口，没有实测两个构建的具体依赖差异。

**最小建议。** CI 与生产使用相同锁定输入，生产只安装所需依赖组；固定独立 CLI 版本。复用现有 `uv.lock`，无需建立新发布框架。

**验证要求。** 比较同一提交的测试与生产依赖清单；验证重复构建及目标环境启动/关键调用。任何供应链变更仍按既有执行授权处理。

### 6.2 P1：产品权限合同、源码与验收断言不一致

**源码事实。** [core/permissions.py](../backend/app/core/permissions.py) 的 `authorize_session_action()` 仍将跨用户读取绑定到独立 `operator.inspect`；[api/agents.py](../backend/app/api/agents.py) 的 HR 入口直接拒绝 `platform_admin`。生产 manifest 的 P15-ADMIN 与 P29-CADMIN 仍要求拒绝管理员访问员工私有内容。

**合同事实。** PDEC-013 已明确：平台/公司管理员可以管理各自范围内的业务内容，凭据不明文展示；员工只使用自己的或公开 Agent；operator 不是第四种产品身份。

**影响。** 管理员从合法业务入口仍可能被阻断；旧测试预期会把新合同需要的行为判错。这是已登记的 `ROLE-CONTRACT-ALIGNMENT-001`，不是新发现的生产攻击结果。

**最小建议。** 在既有共享权限路径对齐角色与目标公司，贯穿 input、运行、恢复、文件、KB 和 UI；同步修改相关 manifest 语义及回归。避免逐页面补例外，也避免通过全局 bypass 替代业务授权。

**验证要求。** 三角色正负用例、真实 actor、公司切换、撤销、缓存、历史 Session、new run 与 existing run 分别验证；管理员业务授权不能扩大机器连接或凭据权限。

### 6.3 P2：核心 turn 是长函数与模块级耦合的组合

**源码事实。** [turn_orchestrator.py](../backend/app/kernel/turn_orchestrator.py) 的 `run_agent_turn()` 约 2,590 行，起始绑定 91 个 `support` 成员。[engine.py](../backend/app/kernel/engine.py) 的 `AgentKernel.handle()` 传入 `sys.modules[__name__]`。

**影响。** 文件拆开后仍依赖原模块的大量内部符号；调用约束通过 `Any` 和运行时属性访问表达，阅读与重构时需要同时跟踪多个文件。函数长度本身不是错误，但该耦合扩大了修改和审查范围。

**最小建议。** 保留单一生命周期负责人；按实际阶段提取有清晰输入输出的函数，把普通内部帮助函数放回直接 import 路径。依赖注入保留给模型客户端、时钟、持久化等真实替换边界。

**验证要求。** 先保持行为基线，再验证正常完成、工具调用、取消、预算、provider 异常和恢复；拆函数不能改变结果顺序、模型选择或权限语义。不进行一次性大重写。

### 6.4 P2：端口和签名兼容机制超出了实际替换需要

**源码事实。** [ToolExecutionPorts](../backend/app/tools/execution_pipeline.py) 注入 `json`、`asyncio`、`inspect`、`traceback` 和路径类型；[invoker.py](../backend/app/runtime/invoker.py) 在调用同仓 `execute_tool` 时反复检查其签名，再决定是否传入权限、Session、事件和执行参数。

**影响。** 同仓内部函数签名变更时，部分不兼容会变成运行时参数省略，而非更早暴露的调用错误。测试替身的兼容需求可能反向塑造生产代码。真实外部 executor 的兼容检查则可能有用途，不能一并删除。

**最小建议。** 标准库直接 import；同仓函数使用显式参数和类型；动态适配留在实际插件/外部调用边界。同步更新测试替身，不为旧替身永久保留宽泛兼容。

**验证要求。** 保证权限 frame、principal、Session、pre-effect callback 等实际进入执行边界；不以测试能通过为理由丢弃参数。

### 6.5 P2：Session 前端承担多套状态的同步与恢复

**源码事实。** [AgentDetail.tsx](../frontend/src/pages/AgentDetail.tsx) 约 2,900 行，同时维护 history messages、transcript events、canonical store、compatibility timeline、pending input、active run、回填与可见性边界等状态。

**影响。** 这些状态不全是冗余：乐观输入、历史兼容、权限边界和 durable state 有不同用途。但维护时需要证明各投影收敛、不重复、不被旧事件覆盖，页面承担了较高的协调成本。

**最小建议。** 明确 canonical store 的状态更新职责，让可推导状态尽量成为派生值；把临时输入与历史兼容限制在明确边界。为兼容数据定义可迁移、可验证的退出条件。继续拆 JSX 文件本身不足以解决状态问题。

**验证要求。** 真实浏览器覆盖初始载入、流式到终态、断线回填、重复/乱序事件、切换 Session、权限变化和 reload；不得通过丢弃历史或弱化错误展示来减少状态。

### 6.6 P2：部分测试固定实现形状，保护了无消费者的抽象

**源码事实。** [test_h3_context_engine_contract.py](../backend/tests/architecture/test_h3_context_engine_contract.py) 断言 `MemoryProvider` 类字符串存在；[test_h2_harness_hardening.py](../backend/tests/architecture/test_h2_harness_hardening.py) 要求 `DockerToolRuntimeBackend` 存在。

**影响。** 删除没有运行价值的类会导致测试失败；保留类名却不实现行为则可能通过此类检查。结构测试适合保护精确约束，但“某类必须存在”缺少实际消费者支撑时会形成维护负担。

**最小建议。** 将这些检查替换为对应行为保护，或随无用实现一起移除。RLS allowlist、manifest 指纹、禁止敏感数据出境等有明确机械属性的结构检查继续保留。

**验证要求。** 每个保留测试都能指出保护对象及会被捕获的实际回归；不能为了删除代码而削弱仍有效的安全与恢复断言。

### 6.7 P2：部分普通用户界面仍直接披露内部表示

**源码事实。** [PersonalKnowledge.tsx](../frontend/src/pages/PersonalKnowledge.tsx) 已有错误翻译与 backend `retryable` 消费；同时，部分检索结果直接展示 `source_ref`，部分 proposal 直接连接展示 `policy_reason_codes`。

**影响。** 正常用户可能看到工程标识，却不清楚它与任务的关系或下一步操作。本文未重新浏览所有登录角色页面；既有 live UX 问题与候选状态以 Findings 为准。

**最小建议。** 默认展示来源名称、业务原因和实际可执行的恢复动作；保留来源引用和详细证据，通过详情展开访问。重试按钮继续消费真实 retryability，不把所有失败一概描述为可重试或不可重试。

## 7. 可精简对象与预计收益

| 分类 | 对象 | 已核对的调用事实 | 建议与边界 |
|---|---|---|---|
| `delete` | [memory/backend.py](../backend/app/memory/backend.py) | `get_memory_backend` 未发现正常产品调用；模块引用主要是测试与 main shutdown。真实检索走 MemoryRetriever/MemoryAssembler | 移除旧内部层及对应空清理；保留实际记忆管线，单独处理 API/持久字段兼容 |
| `delete` | [channels/base.py](../backend/app/channels/base.py) 与 [registry.py](../backend/app/channels/registry.py) | 该 ChannelAdapter 无实际实现；registry 未被其他应用模块消费，真实渠道服务走其他路径 | 删除孤立通用框架，不能把删除它等同于删除真实渠道能力 |
| `yagni` | [MemoryProvider](../backend/app/runtime/context_engine.py) | 无实际实现与运行期消费者，结构测试要求名称存在 | 删除未消费协议，保留有调用者的 ContextEngine |
| `delete` | [DockerToolRuntimeBackend](../backend/app/tools/backends.py) | 只有禁用路径测试；`enabled=True` 直接运行 executor，未提供 Docker 隔离 | 删除误导性占位，保留真实 code_execution provider；未发现其被生产启用 |
| `shrink` | Kernel support / 通用端口 | 大量模块符号、标准库和内部函数通过 Any/回调转接 | 改为直接依赖与少量有明确职责的注入边界 |
| `shrink` | Session 多套投影 | 同一页面维护多个状态表示与同步过程 | 在行为保护下减少可推导状态，为兼容路径设退出条件 |

前四项被定位到的源码主体共约 396 行，包含注释和协议声明；连带导入、测试和清理如何调整尚未实施。因此“约 400 行”是候选规模，不是承诺净删行数。本轮没有确认可以直接删除的依赖包。

实施删除前，需用届时源码再次确认动态加载、运维脚本和打包入口等消费者，并运行对应行为检查。本报告的静态调用扫描不授予跳过这些验证的依据。

核心调用与 Session 收敛可能有更大收益，但需要保持行为后测量。代码行数下降、状态来源减少、排障跳转减少和修改影响面缩小，应一起观察。

本轮扫描还发现少量精确重复辅助函数，例如 UUID 归一化。单纯合并十几行帮助函数的收益低于主路径收敛，不应优先建立新的全局 util 层。

## 8. KISS、奥卡姆剃刀与必要复杂度

判断一个层次是否值得保留，建议逐项回答：它服务哪个实际消费者；保护什么事实或效果；更直接的现有实现是否足够；移除它会发生什么具体回归。

| 应保留的复杂度 | 可以收敛的复杂度 |
|---|---|
| 多租户、principal、凭据和外部效果边界 | 无消费者的协议、注册器和 provider 占位 |
| 已有失败证明需要的事务、租约、幂等与 outbox | 多处维护同一状态、重复终态处理与重复兼容路径 |
| 不同真实 provider 的协议适配 | 对标准库或固定内部函数的宽泛端口包装 |
| 能够定位具体缺陷的行为和结构测试 | 仅保护类名/文件形状且没有产品义务的测试 |
| 完整授权证据的可发现、可恢复访问 | 为缩短 prompt 而丢弃证据，或反过来静态塞入所有数据 |

因此，KISS 在本项目里的方向是“更少的状态所有者、更直接的调用、更清晰的边界”。它不要求削弱企业能力，也不要求把所有代码合并为一个大文件。

## 9. 生产级代码与交付条件评估

本报告用以下工程维度评估“国际大厂生产级”，不把它当作某项正式认证，也不以某家公司的全部实现作为模板。

| 维度 | 已有正面证据 | 当前缺口 | 本轮结论 |
|---|---|---|---|
| 正确性 | 关键路径有较多行为测试，本轮定向测试通过 | 部分产品规则未落地，存在当前修复中的已知问题 | 局部有保障，整体待验 |
| 可维护性 | 有域划分、明确部分生命周期归属 | 长函数、模块级支持注入、前端多投影和死抽象 | 需收敛 |
| 类型与静态检查 | 前端 strict TypeScript；本轮 tsc、Ruff 通过 | Any/Callable/签名探测使部分内部合同无法静态保护 | 基础合格，不等于强类型贯通 |
| 测试质量 | 定向行为测试、真实数据库测试和浏览器 CI 已存在 | 部分结构测试固定形状；当前生产双遍未完成 | 体系有基础，结果不能越界外推 |
| 构建重现性 | 仓库已有锁文件，CI 冻结安装 | 生产安装未统一消费锁文件，独立 CLI 未固定版本 | 明确缺口 |
| 隔离与权限 | RLS、actor、租约、秘密保护和审计 | 新角色合同、撤销/恢复、跨租户维护残余 | 最终验证未完成 |
| 可恢复性 | durable task、outbox、reconciliation 机制 | 当前版本真实 crash/replay/rollback 全链未验 | 机制存在，运营结论待证 |
| 可运营性 | health、trace、部署与错误状态基础 | 本轮未取得生产负载、SLO 达成、数据恢复演练证据 | 不作成熟运营声明 |
| 用户体验 | 已有渐进披露和 UX 回归 | 普通用户主路径、窄屏和失败恢复仍待闭环 | 尚未稳定达标 |

整体判断：项目具备继续收敛为生产级产品的基础，但不能仅凭源码规模、测试数量或部署成功给出整体达标结论。

## 10. 建议的推进顺序与完成条件

以下是评估后的建议，不是新的执行授权，也不替代现有 Runbook、分工或发布合同。当前候选先按既定审查完成；独立且已获授权的工作继续推进，不以单一外部阻塞暂停整个项目。

| 顺序 | 工作焦点 | 应观察到的完成条件 |
|---|---|---|
| 1 | 完成当前修复候选审查与集成，保持有效修复 | exact candidate、真实缺陷回归与独立 review 对齐，未部署项如实保留 |
| 2 | 普通员工主路径及阻断使用的角色合同 | 创建/选择 Agent→真实任务→工具→交付→继续；合法管理员入口和公司上下文正确 |
| 3 | CC+ 同条件能力评测与记忆收益 | 任务结果、重复成功率、成本、延迟和人工介入可比较，能力无未披露缩水 |
| 4 | 小步精简及构建一致性 | 删除候选无消费者；核心状态/依赖减少且行为不退化；CI 与生产依赖一致 |
| 5 | 现有完整发布验收 | coherent D、96 条双遍、权限负向、故障恢复、rollback、cleanup、evidence-only E |

顺序 2～4 中独立部分可以按现有所有权与隔离规则并行。本报告不新增调度器、审批层、任务状态文件或另一套执行队列。

### 10.1 CC+ 评测需要回答的问题

1. 在同模型、同推理配置、同工具权限、同初始文件和同预算下，Hive 是否能完成对照系统可完成的任务？
2. 工具发现、上下文组装、治理和恢复是否造成明显的能力损耗或额外人工介入？
3. 失败来自模型、工具、上下文、权限、状态恢复还是评测自身？是否有原始可核查证据？
4. 记忆与学习是否改善后续任务成功和复用，是否引入错误记忆或额外维护成本？
5. 普通员工能否只通过产品入口完成相同任务，而无需管理员或终端补操作？

具体任务与发布判据复用 [Single Agent 领域合同](acceptance/2026-08-30-weekend-rc/domains/single-agent-and-session.md)、[Memory/Knowledge/Growth 合同](acceptance/2026-08-30-weekend-rc/domains/memory-knowledge-and-growth.md) 与现有 P08-J4。新增探索用例不能替代或缩减冻结的 96 条旅程。

### 10.2 本阶段暂缓的工作

- 为尚无实际消费者的能力增加新抽象、注册系统或配置开关。
- 在主 Agent 与用户路径尚未稳定前，扩展新的控制台入口和编排层。
- 把完整核心运行时或 Session 页面作为一次性重写对象。
- 把多个不同语义的能力强行统一成一个通用 Workflow。
- 以减少行数为目标删除权限、恢复、真实外联或证据可访问性。

## 11. 本轮验证记录与复核命令

以下结果来自本会话前一轮审计的实际工具输出。成文没有重新运行相同代码测试；工作区有并行候选，这些结果不证明后续全部变更。原始输出留在本会话，本文保存命令与摘要，不作为 frozen production journey 的不可变证据。

| 检查 | 结果 | 能证明什么 |
|---|---|---|
| 后端定向 pytest | 104 passed，37.99 秒 | 所选循环、历史、终态、权限和旧 backend 测试的本地行为 |
| 前端定向 Vitest | 4 files / 110 tests passed，约 0.5 秒 | 所选 reducer、事件消费、投影和体验策略测试 |
| TypeScript | `tsc --noEmit` exit 0 | 当前类型检查配置通过 |
| Ruff | `ruff check app --statistics` exit 0 | 当前后端应用源码符合已配置的 Ruff 检查 |
| manifest 解析 | 35 families / 96 variants / denominator 96 | 分母结构，不能判定语义通过 |
| Railway 只读查询 | backend/backend-api/frontend 均 SUCCESS | 部署机械状态，不能证明功能闭环或 exact source |

成文后的文档验证：报告自身 47 个链接与结构检查通过；现有验收文档组的索引完整性、metadata、行数边界和链接检查共 4 项通过（0.78 秒）；文档 diff 空白检查通过。它们只验证文档结构。

在仓库根目录下，后端检查命令为：

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://hive:hive@127.0.0.1:1/hive_audit_no_db \
REDIS_URL=redis://127.0.0.1:1/15 \
.venv/bin/python -m pytest \
  tests/kernel/test_loop_guard.py \
  tests/services/test_session_semantic_history.py \
  tests/services/test_web_terminal_boundary_processor.py \
  tests/services/test_runtime_terminal_boundary_outbox.py \
  tests/api/test_chat_sessions_permissions.py \
  tests/memory/test_backend_resolution.py \
  -q -p no:cacheprovider
.venv/bin/ruff check app --statistics
```

数据库和 Redis 地址故意指向不可用端口，避免测试误连业务服务；既有 pytest 配置提供临时文件目录。通过这些测试不代表运行了新的真实 PostgreSQL 竞态或生产外部调用。

另从仓库根目录运行前端检查：

```bash
cd frontend
./node_modules/.bin/tsc --noEmit
NODE_OPTIONS=--no-experimental-webstorage ./node_modules/.bin/vitest run \
  src/pages/session-workbench/threadItemReducer.test.ts \
  src/pages/agent-detail/sessionEventConsumer.test.ts \
  src/pages/agent-detail/sessionSocketEventProjector.test.ts \
  src/pages/session-workbench/SessionExperiencePolicy.test.tsx
```

Railway 查询使用已有 CLI 的 `status --json` 和各服务 `deployment list --environment production --limit 2 --json`。连接器认证不可用后，CLI 只读查询成功；没有登录、改变量、SSH、部署或读取业务内容。

| 服务 | 本轮实时查询的部署 ID | 状态 |
|---|---|---|
| backend | `637818b5-1fd3-4ecc-9390-f1484d95a649` | SUCCESS |
| backend-api | `0bce9b71-2ca0-4845-9918-dcf65b09464c` | SUCCESS |
| frontend | `5dccd5b8-1eee-4b11-be5a-e0c72b5c02cb` | SUCCESS |

部署 ID 与现有状态文档一致。CLI 结果没有提供可用的 commit hash；文档记载生产基线为 `6d46459e3a3dcf50dd32043583f4ab57667b0701`，该源码绑定本轮未独立验证。也未在本轮重新调用 health 或执行 signed-in 产品验收。

## 12. 后续维护与评审边界

本文的新建议先作为审计输入。采用后按既有文档职责记录，避免把报告维护成第二套状态系统：

| 内容 | 后续记录位置 |
|---|---|
| owner 接受的产品裁决 | [02-owner-decisions.md](acceptance/2026-08-30-weekend-rc/02-owner-decisions.md) 与对应产品合同 |
| 新复现缺陷及修复进展 | [05-findings.md](acceptance/2026-08-30-weekend-rc/05-findings.md) |
| 当前执行情况与下一动作 | [03-current-status.md](acceptance/2026-08-30-weekend-rc/03-current-status.md) |
| 验收条件与发布步骤 | 对应 domains 与 [06-runbook-and-release-gates.md](acceptance/2026-08-30-weekend-rc/06-runbook-and-release-gates.md) |
| exact commit 的实际生产结果 | 现有 evidence 目录，遵守证据与纠错规则 |

本文交付完成不表示上述修复、精简或生产目标完成。本轮没有新增执行 Goal，没有派发 worker，没有修改业务代码，没有提交、推送或部署。
