# 单 Agent 框架全面原子化 Review（对照北极星四层）

日期：2026-07-02（同日 owner 复核后修订 v1.1；2026-07-03 追加 v1.2/v1.3/v1.4/v1.5/v1.6/v1.7/v1.8/v1.9/v1.10 增量复审与修复闭合）
状态：审计报告 + 增量修复闭合记录。初始审计取证基准 HEAD `4f211ee7`（main）。**owner 复核时 main 已前进至 `cdabf1c7`（ahead 10）**，其中 `fa27f02f` 新增 web chat RuntimeTask claim/lease 路径——涉及的结论已按 §0.1 修正。**最新闭合点为 HEAD `d72e487f`（`Close remaining session framework review gaps`，已推送 `origin/main`），见 §16 v1.10。**
方法：11 路并行原子审计（内核循环 / transcript-resume / hooks / Plan Mode / Subagent-Team / Skill-MCP-命令层 / Workflow-Trigger-Ledger / 治理层 / Memory-Iter / UI 结构 / UI 微交互）+ 主控全量测试 + 基线源码对照（FreeCode `/Users/rocky243/vc-saas/free-code-main`、Codex `codex-rs`）+ 主控矛盾仲裁
评判基准：`docs/ccplus-north-star-contract-2026-06-24.md`（边界契约）、`docs/hive-sota-master-goal.md`（总目标）、`docs/cc-python-evolution-north-star-2026-06-22.md`（单 Session 总纲）
判定口径：aligned / partial / missing / intentional_delta / violation；P0=破坏北极星或生产事故级，P1=下轮必修，P2=打磨

---

## 0. 一句话总判（v1.1 修订）

**四层方案（CC 底座 → Codex 增量 → Hive 治理 → Codex Desktop 呈现）在"晴天面"已经真实立起来了**：单 Session 硬不变量（transcript-first、resume 断点矩阵、hook 核心生命周期、Plan Mode 确认边界、单入口内核、压缩三路收敛、cache 经济）全部经代码级验证为真接线，呈现层从上一轮文档快照的 35-45% 实质跃进到 ~75-85% 且微交互是真深度实装。**当前最重的实质缺口是记忆两平面重构的顶端收口**：漏切 Dream 车道（soul 演化分脑、迁移后永久空转）+"切换先于迁移"的失明窗口——直接命中 Goal-1（P1×2）。daemon 默认关停经 owner 复核为**部署契约脆弱性**（生产 env 已显式开启、三 daemon running/healthy），非当前停摆；RLS 经生产 health 证实已 cutover 至 `app_rls`（superuser=false、enforcement=strict），历史风险已缓解。治理纵深剩余两洞（审计身份非否认性、预算无公司级天花板）与 Goal-2 的控制中台承诺仍有实距。

### 0.1 复核修正记录（2026-07-02 owner 复核，逐条回代码/生产验证后采纳）

| 原判定 | 修正 | 依据 |
| --- | --- | --- |
| P0-1「生产 daemon 停摆」 | **降级为 P1 部署契约脆弱性**：Railway production 已设 `CORE_DAEMON_STARTUP_ENABLED=true`、`CHANNEL_STREAM_STARTUP_ENABLED=true`，线上 health 显示 evolution/trigger/workflow 三 daemon running/healthy（owner 一手读数）。残余风险=代码默认 False（config.py:111）+ 仓内无 IaC/文档锚点固化该 env 契约——env 漂移或新环境复制即静默停摆 | 生产 env/health 读数（owner）；代码默认值亲验属实 |
| P1-3「RLS 生产未真正生效」 | **降级为 P2 残留验证项**：生产 health 报 `rls_role=app_rls superuser=false bypassrls=false enforcement=strict`——stage-3 cutover 已在生产执行，DB 兜底在位。残留=bare session 收敛、新表 policy 维护、持续 health/迁移验证；本地测试文件记录的是历史风险 | 生产 health 读数（owner）；strict guard 代码亲验为真 |
| P1-7「PG completion signal 永不读回」 | **判定错误，降级为 P2 双路径易误读**：durable 消费者真实存在——`subagent_wake_consumer.py:67 drain_subagent_completion_wakes` 用 `DELETE ... RETURNING` 原子消费 coordination_signals，且 `workflow_daemon.py:55` 默认接生产 parent-wake invoker（该接线早于审计基准 HEAD，属**审计漏看**）。残留=in-run helper `consume_subagent_signals()` 仍读内存单例，与 durable 路径并存易误读/易漏接 | 亲验（wake consumer 8 tests passed） |
| P1-8「resume 无跨进程租约」 | **改窄**：`fa27f02f`（审计基准之后落）已建 `runtime_task_claim_service.py:19` 的 `FOR UPDATE SKIP LOCKED` claim + worker（2 tests passed）。剩余风险=`web_chat_runtime.py:1568 resume_persisted_web_chat_runs` startup 路径仍直接全量 select+dispatch 未走 claim——多 runtime 副本下仍可能重复驱动，副本化前接上即可 | 亲验 |
| §6「实测 43 成员/6 noop」 | **修正为 42 成员/6 noop**（审计员数错）。连带修正：CLAUDE.md 的"42-member"本就正确，只有"7 个 noop"过期（实为 6，PERMISSION_DENIED 已升级 live） | 亲验（枚举成员计数） |
| 元数据 ahead 8 | 复核时 ahead 10（`fa27f02f` claim 路径、`cdabf1c7` heartbeat DB session 释放） | 亲验 |

修正后计数：**P0×0；P1×11**（dream 分脑、迁移失明、daemon 部署契约、审计身份、预算帽、subagent 永久 running、startup resume 未接 claim、D7 半退役、出处标签、resume chip 掩盖、hook catalog 诚实债——其中后两项性质为"诚实债"）；其余 P2 清单相应并入 §4。Dream 分脑与失明窗口两条经复核**维持原判**（`backend/app/services/auto_dream.py:1542/1991` 仍读 flat T3；`backend/app/memory/plane_read.py:30-33/47-50` 无 legacy fallback）。

---

## 1. 基础证据（主控自验）

| 项 | 结果 |
| --- | --- |
| Backend 全量 | `pytest tests -q` → **5323 passed / 3 failed / 1 skipped**（92.5s） |
| 红点归属 | 3 failed 全在 `tests/api/test_chat_sessions_transcript_window.py`；根因 = `93e5cbb9`（性能 pass）在 `app/api/chat_sessions.py:2066` 加 `await db.commit()`，测试 fake `_DB` 无 commit 方法 → **主线测试债**（生产真 AsyncSession 不受影响） |
| Frontend | vitest **430/430 绿**（72 文件）；`npm run build` 通过 |
| 部署状态 | 审计基准时 main ahead 8；复核时 **ahead 10 未 push**（新增 `fa27f02f` claim 路径、`cdabf1c7`）——本报告涉及的本地结论均未部署到生产 |
| Daemon 默认态 | 代码默认 `CORE_DAEMON_STARTUP_ENABLED=False` + `CHANNEL_STREAM_STARTUP_ENABLED=False`（`b4622c95`）；**生产 Railway env 已显式置 true，三 daemon 线上 running/healthy（owner 复核读数）**；契约仅存在于 env，无 IaC/文档锚点 |
| RLS 生产态 | 线上 health：`rls_role=app_rls superuser=false bypassrls=false enforcement=strict`（owner 复核读数）——stage-3 cutover 已生效 |

---

## 2. 分层记分卡

### 2.1 CC 工程底座（Goal：CC/FreeCode 语义 parity）

| 维度 | 判定 | 摘要 |
| --- | --- | --- |
| Kernel / Session Loop / Compaction / Cache | **aligned（强）** | 单入口三不变量硬成立（kernel 唯一构造 invoker.py:1040/唯一调用:1373、零 DB import、vendor 分支零命中）；压缩 case-law 违规已真修复（完整旧史/主模型/20K 输出/诚实兜底，控制器 window-33K 精确=CC）；transient 注入纪律彻底（round-pressure/loop-guard/动态后缀均不进 durable transcript，frozen cache 不被毁）；mid-run steering 端到端。P2×2 同源：`compress_threshold=1.0` 双门反转使 proactive 压缩晚 13K 触发（`backend/app/runtime/session_context_controller.py:333`，注释与行为反向）+ engine 内联预算用 ASCII 估算器漏算 CJK ~3.5x |
| Transcript / T0 / Resume / Rewind / Fork | **aligned（全仓最扎实）** | transcript-first 全入口验证（web:1167/IM:710 失败即拒跑/trigger:1444/delegation:1350/task:406）；resume 断点矩阵全覆盖（kill-before-response / ghost 去重 / tool-pair recovery_manifest / compaction replacement / worker reconcile / mid-run claim）；rewind=非破坏 soft-projection 且服务端强制模型上下文（web_chat_runtime.py:2674）；subagent transcript 独立隔离。P1 前瞻：**startup resume 未接 claim 路径**（详见 §3 P1-8） |
| Hooks | **partial** | CC 27 事件全量映射、核心钩子真接线有红绿测试（PRE_TOOL_USE 真拦截、USER_PROMPT_SUBMIT 注入、STOP block→续跑、三路 PRE/POST_COMPACTION）；06-22 快照缺口（USER_PROMPT_SUBMIT/STOP/SUBAGENT_STOP/SESSION_END）**已闭合**。P1：catalog 诚实债（§3） |
| Plan Mode | **aligned（最成熟维度之一）** | 进入=纯用户显式（_LONG_TASK_RE 零命中）；clarification 一等；plan 正文 agent-authored（authored body 胜出+hash-covered）；readonly 三入口强制（execute/execute_direct/execute_approved 均拦）；exit→confirm→handoff 每 target 有 handler（长期断链已修）；stale 不泄漏；三层确认分工不混层（"A block never activates Plan Mode"）。残余=execution_contract 首轮 prompt-only 窗口（与 CC 形态一致，P2） |
| Skill / 命令层 | **aligned** | 渐进披露真实（三级预算降级 catalog + load 纯上下文不解锁 schema）；CAPABILITY_MAP **134/134 完美 1:1** + strict fail-closed；命令注册表 + schedule 自然语言 fallback（17e4b565）真接线。D7 command_pack 半退役已在 v1.9 关闭：command wrappers 为 agent_base core tool surface，历史 pack 只保留 requires_core dependency manifest。 |
| Workflow / Trigger / Ledger substrate | **aligned（真深实现）** | RuntimeTask+step/leaf journal+advisory-lock quota+admission 硬拒；leaf 真绑 governed spawn_subagent；完成副作用 SELECT FOR UPDATE 认领去重；崩溃遗留不可逆步 → unknown_requires_reconciliation 永不自动重放；四边界零混层（workflow 不调 plan mode / trigger 不自跑 tool loop / ledger 写不执行）。P1：daemon 部署契约脆弱性（§3 P1-0，生产 env 已开启）；P2：REST start 的 plan-gate 死代码 |

**结论：CC 底座成立。** 北极星 §2「当前阶段绝对目标」（session recovery / hook boundary / subagent / skill / workflow / Plan Mode 达到 CC baseline）在代码层面已达成主体，遗留为可数清单而非系统性缺位。

### 2.2 Codex 工程增量（Goal：只增强控制/观测，不改 CC 边界）

已吸收且真实：granular approval + reviewer 路由（per-(tenant,agent,capability) + creator/platform_admin/org_admin，会话卡+企业 Feishu 卡双面闭环）、mutating restart fail-closed reconciliation + admin 消费面、workflow journal 纪律、rollout 式 T0 JSONL、typed ui_action、Session Workbench 读模型（controls/steps/leaf/gate schema）、CollabAgent 生命周期（spawn/sendInput/resume/close/interrupt 全有，无同步 wait=合理 delta）。
未及 Codex 之处：fork/branch 身份存 metadata 非一等列（弱于 Codex thread/turn/item 身份）；workflow 运行中无 live per-step WS 流（持久化 per-step 转录事件+run 级推送已有）；queued mid-run 消息前端为"禁用 composer"而非 Codex 式排队（后端已支持 steering）。边界纪律良好：未发现 Codex 机制改写 CC 语义的案例。

### 2.3 Hive-native 治理 / 自进化（Goal-1 + Goal-2）

| 子层 | 判定 | 摘要 |
| --- | --- | --- |
| 治理咽喉 | **aligned** | 工具执行单一入口无旁路（agent_tools.py:870→service.execute:890→governance→preflight→dispatch，post-approval 仍过全门）；MCP authz / secrets（Fernet/HKDF+env allowlist）/ code-exec 三条 fail-closed 扎实；L2 不削 L1（工具驱逐留指针可取回；治理只限权） |
| 治理纵深 | **P1×2**（v1.1） | 委派身份不进审计哈希链也不在 canonical invocation_spans；预算仅 user 级 token 无租户/agent 天花板（§3）。RLS 经生产 health 证实已 cutover 至 app_rls（enforcement=strict），降级为 P2 残留验证 |
| Memory 管道 | **aligned** | 两平面核心收口真实：plane_read 唯一读面、旧模块真删、写治理不可绕（_write_file 封 memory/soul/skills + PL4 拒）、T2 全量输入无截断、T3 agent 亲写+Platform Gate 精确 apply（AI-Native 四问过）、T0→T2 遗留库全隔离 |
| Memory 顶端 | **P1×2（含 violation）** | Dream T3→soul 车道仍读退役 flat 布局=活双轨（分脑）；切换先于迁移的失明窗口（§3） |
| 自进化闭环 | **aligned（重大好转）** | provisional 试用制真替换历史 dark 臂：candidate→硬门+regression_report→provisional trial→rollback→ledger，**default-ON、无 admin 门、生产可达**——上一轮审计"晋升臂生产永久暗"的命门已在代码级关闭（生产 daemon 已恢复，晋升臂读数仍待持续积累/观测） |
| Soul 写路径 | aligned | Soul Gate 物理校验+owner 批+回滚快照+审计；charter 只 stage signal。P2 窄口：独立 review no-op 时生成器自带 review 可蒙混（owner 批兜底） |

### 2.4 呈现层（Goal：Codex Desktop 形式）

- **结构骨架 ~75-85%**（上一轮文档"35-45%"快照已过期）：§9.1 模型底座全建（SessionWindowModel/CheckpointTimelineNode/SessionRightPanelModel/WorkflowRunWindowModel）；child session window + breadcrumb `Main > Agent:{name}` + 可直接对成员发消息；四概念 reducer 独立 RunStepKind 不再降级（07-02 文档两个核心 ❌ 已修复）；交付物 current/historical/unattributed 分组+revisions badge+divergence+快照优先；Workflow Run Window（steps/leafCalls/controls）；右栏拖拽+localStorage。
- **微交互真深度实装**（765b498f 12 项绝大多数非贴 class）：thinking 摘要直显、真 setInterval 秒表、exec exit 着色、完成即折叠状态机、newline-gated 流式跑在真 React.memo 上、GitLine hover 结构化卡+rewound 灰显（后端 projection_reason 真喂养）、rewind 双 fallback——均有行为级测试。
- 剩余：作者出处标签 UI 未收尾（P1）、resume chip 死字段+测试掩盖（P1）、四概念图标未穿透 transcript 内联、Session 路由非全路径 URL 回写、右栏堆叠卡非 tab、WS 后轮询残留（run 3s/列表 10s/plan 10s——与池饱和同源）、projection chip 永不渲染。
- North Star UX 合规：i18n en/zh parity 0 缺失、CSS custom properties dark/light 达标、无 vendor 特权文案；chip 标签偏 API 语域（对非技术企业用户）是轻微 clarity 张力。

---

## 3. P1 详单（合并去重，按影响排序；v1.1 修订后 P0×0）

### P1-0 daemon/IM 部署契约脆弱性（v1.1 由 P0 降级）

- 证据：代码默认 `CORE_DAEMON_STARTUP_ENABLED=False`（config.py:111）+ `CHANNEL_STREAM_STARTUP_ENABLED=False`；`main.py` 把 trigger/workflow/evolution 三 daemon 全 gate 其后；`entrypoint.sh` 不设置该 flag。**生产 Railway env 已显式置 true，三 daemon running/healthy（owner 复核）**——当前不停摆。
- 残余风险：北极星 Goal-1 核心运行态（自进化/自主性/IM）的开启契约只存在于 Railway env 一处，仓内无 IaC、无部署文档锚点、无"生产必须开启"的守护检查——env 漂移、新环境复制、灾备重建都会**静默**回到停摆默认值。
- 修复方向：把该契约固化（部署文档 + health 对 daemon 期望态的显式断言/告警，或随四池 Worker 落地由架构消解）。

### P1-1 Dream T3→soul 车道分脑（两平面漏切，violation）

- 证据：`auto_dream.py:1542,1991` `_read_all_t3` 仍读退役 flat `_T3_FILES`（t3/episodes.md 等）；两平面 T3（self/profiles/knowledge/milestones）永不进 soul 车道；迁移后 flat 归档 → `if not t3_files` 早退（:1992）→ **soul dream 永久空转**。`test_auto_dream.py` 21 处钉 flat 布局、0 处两平面 → 绿测试结构上抓不到；实施台账"无双轨自检"只验了 writer=0，漏了 dream 的 legacy reader。
- 影响：Goal-1 身份演化断链；与两平面写路形成分脑。
- 修复方向：dream 输入切 plane_read；test_auto_dream 重钉两平面；台账自检补 reader 面。

### P1-2 两平面"切换先于迁移"的失明窗口

- 证据：`migrate_memory_two_planes --apply` 挂账未执行；`plane_read.py:30-33,47-50` 对未迁移 flat-T3 存量**静默返空、无 legacy fallback**。
- 影响：未迁移生产 agent 的 prompt 注入 / search_memory / growth_report / self_evolution_audit 全失明（数据在盘未丢但不可见）。
- 修复方向：owner 执行迁移（dry-run→--apply --confirm）；或给 plane_read 加可观测的迁移前告警（不建议加静默 fallback 造成新双轨）。

### ~~P1-3 RLS 生产未真正生效~~（v1.1 降级为 P2 残留验证项）

- 复核结论：生产 health 报 `rls_role=app_rls superuser=false bypassrls=false enforcement=strict`——stage-3 cutover 已在生产执行，DB 兜底在位，原判定基于本地测试文件记录的历史风险，不成立。
- 残留（P2）：14 处 bare session 收敛、新表 policy 手维列表易回归、持续 health/迁移验证。

### P1-4 委派身份不进审计哈希链、不在 canonical span（非否认性缺口）

- 证据：ExecutionIdentity 传播真闭环，但 `compute_audit_event_hash`（`backend/app/core/policy.py:39-51`）不含 execution_identity_*；`backend/app/models/invocation_span.py:33-56` 无身份列；`backend/app/core/policy.py:268-269` `except:pass` 静默清空身份。
- 影响："代表谁行动"可被篡改而不断链；主 trace 表无法区分 agent_bot vs delegated_user——对企业控制中台的审计承诺是实距。

### P1-5 预算无租户/agent 级天花板

- 证据：user 级 token 配额 fail-closed 真接线（invoker.py:1190），但 admin 全豁免（quota_guard.py:39）、owner-less/系统 agent 跳过（invoker.py:1105-1106）。
- 影响：控制中台没有公司级或单 agent 支出上限；失控 agent 只受其 owner 个人额度约束。

### P1-6 后台 subagent 重启可永久卡 running

- 证据：start_subagent_run 恒 stamp `resume_after_restart` → `reconcile_orphaned_runtime_tasks`（runtime_task_service.py:474）直接 continue 跳过；唯一安全网 `resume_persisted_subagent_runs` 只扫最老 50 条；resume 异常被 main.py:386 吞。
- 影响：>50 并发活跃任务或 resume 异常 → run 既不 resume 也不 reconcile，违背"background run 重启不永久 running"的既有闭合声明。

### ~~P1-7 协调 completion-signal 双后端裂缝~~（v1.1 判定错误，降级为 P2 双路径易误读）

- 复核结论：durable 消费者真实存在且早于审计基准——`subagent_wake_consumer.py:67 drain_subagent_completion_wakes` 以 `DELETE ... RETURNING` 原子消费 coordination_signals，`workflow_daemon.py:55` 默认接生产 parent-wake invoker（8 tests passed）。原判定属**审计漏看消费者**。
- 残留（P2）：in-run helper `consume_subagent_signals()`（subagent.py:1099-1108）仍读内存单例，与 durable 消费路径并存，易误读/新调用点易接错；tenant 缺失静默降级内存（coordination_wiring.py:100-114）。

### P1-8 startup resume 未接 claim 路径（v1.1 改窄；副本化前必修）

- 复核更新：`fa27f02f`（审计基准之后）已落 `runtime_task_claim_service.py:19` 的 `FOR UPDATE SKIP LOCKED` claim 语句 + worker（2 tests passed）——跨进程租约基建已建。
- 剩余证据：`web_chat_runtime.py:1568 resume_persisted_web_chat_runs` startup 路径仍直接 `select` 全量 active pending/running 逐一 dispatch，不经 claim。
- 影响：多 runtime 副本下 startup 恢复仍可能重复驱动同一 run。修复面已收窄为"把 startup resume 接到 claim 服务"。

### ~~P1-9 D7 command_pack 半退役（活门非 facade）~~（v1.9 已关闭）

- 证据：team_create 已正确拆出（AGENT_TEAM_DEFERRED_TOOL_NAMES 默认可见+select 可拉），但 task_create/task_update/task_list/task_get/task_output/task_stop/goal_start/advanced_plan/verify_plan 仍归属默认 inactive 的 command_pack L2 门（`backend/app/services/governance_capability_taxonomy.py:201-224` + `backend/app/builtin_packs/command_pack/pack.yaml:45-46`），调用被 `_l2_extension_policy_block`（`backend/app/tools/service.py:476-503`）返回 extension_disabled；inactive pack 也仍挡这批工具的 deferred 名字级可见性（`backend/app/services/agent_tools.py:578`）。修复文档自评 ✅5/🟡3/❌6 未关闭。
- 影响：后端 `/task` 命令在默认租户不可用；D2 可发现性合约只对 team_create 落地。
- v1.9 结论：已把 task/goal/advanced_plan/verify_plan command wrappers 提升为 `CORE_TOOL_NAMES`/`agent_base`；`@tool(pack="command_pack")` 已移除；`command_pack` 双份 manifest 改为 `requires_core` 依赖面；runtime L2 compatibility projection 过滤 core tools 并丢弃空 pack，`policy_pack_names_for_tool()` 对这批工具返回 `()`，禁用 command_pack 时调用不再返回 `extension_disabled`。

### P1-10 共享工作区文件"作者出处标签"UI 未收尾（§3.4 污染治理半接线）

- 证据：数据齐备（AgentChatSection.tsx:1163-1166 artifactWorkspaceAgentId），但 renderDocumentRow(:1717) 只渲染 previewKind/status/size/path，无"由哪个成员交付"标签。
- 影响：北极星"交付物有因果作者归属"与 thinking 污染的产品对策（共享办公桌出处标签）停在数据层。

### P1-11 resume chip 生产永久 "unknown" 且被绿测试掩盖

- 证据：前端 getResumeHealth（timelineModel.ts:929）读 `.status/.state/.kind`，后端 session_index.py:93-97 只出 has_t0_truth/has_checkpoints/truth_surface；测试 timelineModel.test.ts:153 喂生产永不产出的 `{status:'recovered'}` 形状断言通过。
- 影响：功能小（一枚死 chip），但**手写后端形状的绿测试掩盖生产死路**是必须清除的病根信号；同类：projection chip 读 buildRuntimeSummary 从不产出的字段，生产永不渲染（P2）。

### P1-12 Hook/协调 catalog 诚实债（"枚举成员≠真接线"）

- 证据：`_HOOK_RUNTIME_CONSUMERS`（`backend/app/runtime/hooks.py:227-255`）给 NOTIFICATION/ELICITATION/CONFIG_CHANGE/INSTRUCTIONS_LOADED 等 6 个零 emitter 事件赋"消费者"描述字符串，describe_event_catalog 报 lifecycle_state=active；SUBAGENT_START/STOP 虚标 "subagent_lifecycle" 消费者（实际零 handler，`backend/app/runtime/hooks.py:236-237`）；真实未接线 ≈12/42，`_DISABLED_NOOP_HOOK_EVENTS` 只诚实标注了 6 个。`backend/tests/runtime/test_hooks_cc_parity.py:24` 只 pin catalog 声明不验 live emitter。
- 影响：控制面读 catalog 会误判接线状态；与本仓"闭合宣称高于实况"的历史病根同型。

**另记（测试债，非 P 级）**：主线 3 个 transcript window 测试失败（93e5cbb9 引入 commit 与 fake 漂移）应随手收口——主线必须回到全绿口径。

---

## 4. P2 清单（压缩）

| # | 项 | 证据锚 |
| --- | --- | --- |
| 1 | proactive 压缩双门反转晚 13K 触发（"force"传 1.0 语义反向） | `backend/app/runtime/session_context_controller.py:333` |
| 2 | engine 内联预算 ASCII 估算器漏算 CJK ~3.5x（preflight/round-pressure/microcompact） | engine.py:3617/3857/5147 |
| 3 | T0 hash chain 只写不校验（防篡改声明>行为）；index 读改写无锁（多进程同根） | ledger.py:593-604/774 |
| 4 | T0 schema_version!=v2 静默丢弃（未来迁移隐患）；业务 Task 无 resume pump | ledger.py:815-816 |
| 5 | STOP 仅 clean-completion 触发（强停路径不发，TURN_STOP 兜底在）；STOP_FAILURE/ELICITATION 薄接线 | engine.py:4450 |
| 6 | 治理缓存 TTL=15s 无失效接线（撤权后 stale-allow 窗口）；clear_lookup_cache 零生产调用 | `backend/app/services/agent_tools.py:166-205` |
| 7 | ActionPreflight 分类器粗（仅 6 工具 CONFIRM_FIRST）+ preflight_enabled fail-open 可关 | `backend/app/tools/service.py:81-90/1459` |
| 8 | REST start_workflow 的 plan-gate 死代码且被测试钉死"永不调用"；confirmed_plan_id 透传可伪造（tool 层治理真实，REST 靠 preview-hash 兜底） | api/workflows.py:71-156 |
| 9 | Goal budget 臂死代码（account_goal_tokens 孤儿，tokens_used 永不自增） | session_goal_runtime.py:77-88 |
| 10 | AgentWorkLedger DB 表孤儿双轨（活存储=磁盘 JSON）；无 team 共享任务板（可辩 intentional） | agent_work_ledger.py:87-92 |
| 11 | explicit Agent Team 意图无结构化 evidence 记录（首轮 prompt-only 窗口，与 CC 形态一致）；fanout_subagents 幽灵工具（deny-list 有名无 handler）；防嵌套实为 deny-list+depth 非 core_tools_only | subagent.py / tool_policies.py:15 |
| 12 | agent 记忆认知模型陈旧：运行时提示词+CLAUDE.md 仍描述退役 flat T3 四文件（Cognitive Coherence 漂移） | prompt_sections/memory.py:15, CLAUDE.md:226-292 |
| 13 | principal 隔离仅敏感级非身份级（resident self.md/profiles 整份注入无读时脱敏） | principal_context.py:71-72 |
| 14 | soul 独立 review no-op 时生成器自带 review 可蒙混（owner 批兜底） | auto_dream.py:893-947 |
| 15 | 呈现层：四概念图标未穿透 transcript（StepIcon 全 IconTool）；Session 路由非全路径 URL 回写；右栏堆叠卡非 tab；WS 后轮询残留（run 3s/列表 10s/plan 10s）；成员 Send/Resume/Close 快捷硬禁用；queued 消息前端为禁用非排队；session_rewound 死映射键；chip 标签偏 API 语域 | RunDisclosureBlock.tsx:52-63 等 |
| 16 | vendor 名泄漏进模型可见描述（"CC-style/FreeCode-style"，L3 措辞清理）；CAPABILITY_MAP 缺全量参数化守卫测试；孤儿死码 execute_direct/_execute_tool_inner | command_registry.py:266/291 |

---

## 5. 真实达成的亮点（防回退清单）

1. **CC 单 Session 硬不变量全真**：persist-before-loop（比 CC 更耐重启）、resume 断点矩阵、rewind 服务端强制投影、subagent transcript 隔离、mid-run steering、stale plan mode 清理。
2. **压缩 case-law 违规彻底修复**：三路收敛同一 LLM 总结器（完整旧史/主模型/20K 输出/诚实省略 marker），透明注入纪律使 frozen cache 与 durable transcript 双干净。
3. **自进化晋升臂历史命门已在代码级关闭**：provisional 试用制 default-ON、无 admin 门、硬门+regression+rollback+ledger 全链（生产 daemon 已恢复，读数待持续积累/观测）。
4. **治理咽喉与三条 fail-closed 边界**（MCP authz/secrets/code-exec）+ 审批双面闭环（会话卡+Feishu 卡+角色路由）。
5. **CAPABILITY_MAP 134/134 + strict fail-closed**；Skill 渐进披露/加载纯上下文/save_skill 只写 inactive candidate。
6. **Workflow substrate 无作弊**：advisory-lock、idempotency、SELECT FOR UPDATE 副作用认领、不可逆步 fail-closed 隔离。
7. **呈现层跃迁是真的**：§9.1 模型底座、child session window、四概念 reducer 分离、交付物快照/divergence、微交互全套行为级测试；i18n 双语 0 缺失。
8. **治理化 hook 面**（GovernedHookRunner：租户作用域/allowlist/code-exec provider/env 默认空/network deny）是守住边界的正确 intentional_delta。
9. **两平面核心管道收口**：plane_read 唯一读面、旧模块真删、写治理不可绕、AI-Native 四问过。

---

## 6. 文档漂移修正清单（本轮证实的 stale 声明）

| 文档声明 | 实测 |
| --- | --- |
| CLAUDE.md「42-member HookEvent / 7 个 _DISABLED_NOOP」 | **42 成员正确 / noop 实为 6**（PERMISSION_DENIED 已升级 live audit）；本报告初版误写 43，v1.1 已纠 |
| CLAUDE.md / 提示词描述 T3 为 flat 四文件（episodes/user/worker/capabilities.md） | 已退役；现役=两平面（self/profiles/knowledge/milestones），需同步 CLAUDE.md:226-292 与 prompt_sections/memory.py |
| 07-02 TUI 文档「实装率 35-45%」「❌6 不能关闭」 | 快照已过期：765b498f/17e4b565 后骨架 ~75-85%，两个核心 ❌（右栏合并标题、reducer 降级）已修复；剩余项见 §3/§4 |
| 「background run 重启不永久 running」闭合声明 | 被 P1-6 证伪（>50 并发/异常场景） |
| 实施台账「无双轨自检」 | 只验了 writer=0，漏 dream legacy reader（P1-1） |
| T0「hash chain」契约属性 | 只写不校验（P2-3），声明>行为 |

---

## 7. 建议的下一轮顺序（按北极星权重，v1.1 修订）

1. **记忆顶端两刀收口（P1-1/P1-2）**：dream 车道切 plane_read + test_auto_dream 重钉两平面；执行生产迁移（dry-run→apply）。这两项决定 Goal-1「越用越强」是否真实运转，现为最高优先。
2. **治理纵深双修（P1-4/5）**：审计身份入 span+hash、租户/agent 预算帽——Goal-2 控制中台的"最后一道防线"。
3. **可靠性收口（P1-6 + P1-8 改窄版）**：subagent 永久 running 修复；startup resume 接 claim 服务（四池副本化前置项）+ 主线 3 个 transcript window 测试债收口。
4. **部署契约固化（P1-0）**：daemon/IM env 契约写入部署文档 + health 期望态断言，或随四池 Worker 架构消解。
5. **呈现与诚实性收尾（P1-10/11/12）**：出处标签渲染、resume/projection chip 数据源修正+删除手写形状测试、hook catalog 诚实化（未接线标 declared/planned）。
6. P2 按 §4 顺序消化（优先压缩双门+CJK 估算——同源小修，直接改善长会话质量；RLS 残留 bare session 收敛并入）。

## 8. 建议追加到 `docs/hive-sota-master-goal.md` 循环台账的行（owner 拍板后入账）

> | 2026-07-02 | 单 Agent 框架全面原子化 review（四层对照，v1.1 经 owner 复核修订） | CC 底座/Codex 增量/Hive 治理/呈现层全景 | 11 路并行原子审计+全量测试（backend 5323/3 failed=93e5cbb9 测试债；frontend 430 绿）+FreeCode/Codex 源码对照（审计基准 4f211ee7）+owner 生产 env/health 复核（复核时 HEAD cdabf1c7） | CC 底座晴天面成立、呈现层 75-85%、自进化晋升臂代码级闭环、生产 daemon/RLS 经 health 证实在位；P0×0；P1×11 集中在 dream 分脑/迁移失明/daemon 部署契约/审计身份/预算帽/subagent 卡死/startup resume 未接 claim/D7 半退役/出处标签/死 chip 掩盖/catalog 诚实债；复核纠偏 6 条（daemon/RLS 生产态、signal 消费者漏看、web chat claim HEAD 漂移、hook 计数、ahead 元数据） | `docs/single-agent-framework-atomic-review-2026-07-02.md` |

---

*本报告为只读审计产物，未修改任何生产代码；工作树中其他 session 的未提交改动（backend/app/api/files.py 等）未纳入判定。审计 file:line 证据基于 HEAD 4f211ee7；v1.1 复核修正基于 HEAD cdabf1c7 + owner 提供的生产 Railway env/health 一手读数（daemon、RLS 运行角色）。修正过程与依据见 §0.1，原始误判保留划线痕迹以供追溯。*

---

# 增量复审 v1.2（2026-07-03，HEAD `820e312a`，范围 cdabf1c7..HEAD 12 提交）

主题：**runtime pool isolation（四池拆分）落地** + 性能优化，42 文件 +2139/-500。方法：2 路增量审计 agent + 主控亲验（其中跨 plane 与拆分核心两路 agent 因会话限额中断，由主控全程亲验完成）+ 全量测试两轮。

## A. 这批提交真实解决的（防回退确认）

| 项 | 证据 |
| --- | --- |
| ✅ 上轮 3 个 transcript window 测试债修复 | `_DB` 补 async commit + fake 字段补齐 + 新增 oversize 测试，5 passed |
| ✅ **跨 plane 事件总线建成**（消解上一轮"总线未建=流式断"的拍板警告） | 发布侧 `web_chat_stream_bus.py:56-66`（Redis XADD maxlen=10_000 approximate + 单调 sequence + Pub/Sub）；消费侧 `main.py:661-664` API role 启动 `start_web_chat_stream_forwarder`（:73-104 订阅→回灌本地 broker）；health 暴露 snapshot（main.py:906）。全链 Worker→Redis→forwarder→WS 成立 |
| ✅ **T0 写点迁移正确，transcript-first 经 DB 转译保住** | API plane 受理只写 DB：`web_chat_runtime.py:1272-1295` 用户消息入 `RuntimeTask.metadata_json.initial_user_message` + `ChatMessage` 行 + `initial_user_message_t0_materialized=False` 显式标记；Worker（有 volume）claim 后補写 T0。API plane 全程不碰文件系统 |
| ✅ API role fail-closed | `main.py:772` path allowlist 中间件拒绝非 allowlist 路径；API role 禁 schema bootstrap/workspace migration/seed/resume/reconcile/worker/daemon（:367-450,646-658）；`HIVE_PROCESS_ROLE` 默认 runtime（config.py:79）→ 单服务部署行为不变 |
| ✅ claim 基建 + worker 默认开 | `RUNTIME_TASK_WORKER_ENABLED=True`（config.py:80）+ role gate（runtime_task_worker.py:66-69）；SKIP LOCKED claim（runtime_task_claim_service.py） |
| ✅ nginx 单服务回退 | `docker-entrypoint.sh`：`BACKEND_API_HOST` 未设时 `BACKEND_HOST` 同时喂两个 upstream——拆分是 opt-in，老部署不受影响 |
| ✅ 响应上轮 REST plan-gate 发现 | 新建 `tests/api/test_plan_mode_rest_gate.py`（gate 已从死代码接入，但 1 case 未转绿，见 B-2） |
| ✅ 性能三提交干净 | d401744f 索引迁移与模型 `__table_args__` 逐字匹配；bfee8756 读侧 N+1 消除语义不变；0ac4ddfe 仅收紧人机 UI 投影截断（带 `_payload_truncated` 标记，不碰模型上下文与 T0 真相） |

## B. 新问题（本批引入或暴露）

### B-1【新 P0】alembic 迁移链在干净库断裂

- 现象：全量测试 6 ERROR——`test_workflow_migration.py` 全部 upgrade path 测试 setup 阶段 `alembic upgrade head` 失败：`DuplicateColumnError: column "scheduled_at" of relation "runtime_tasks" already exists`。
- 机制：7 个历史迁移（rls_stage2a/2b/2c、backfill_patch_only_columns_0609、coordination_rls_0604、drop_workflow_step_phase_0614、subagent_evolution_auto_0605）import **活模型** `Base.metadata` 做 create_all——它们按今天的模型定义（已含 `scheduled_at`，models/runtime_task.py:83）建表，链走到 `runtime_task_claim_lease_0702` 再 `op.add_column("scheduled_at")` 即撞列已存在。经典"迁移引用活模型"反模式：**历史迁移的行为随当前代码漂移**。
- 影响：生产增量升级（库已在 head 附近）可能恰好不炸；但**任何干净环境从零 upgrade——CI、新租户独立部署、灾备重建——必炸**。同时 `test_alembic_single_head_is_current_closure_head` 红（两个新迁移未更新 `_CURRENT_CLOSURE_HEAD` 守卫，与 07-02 Part 26 同一个坑第二次发生）。
- 修复方向：`runtime_task_claim_lease_0702` 的 add_column/create_index 加存在性守护（inspector 检查或 `IF NOT EXISTS`，与 entrypoint.sh 补丁传统一致）；系统性解=历史 create_all 迁移钉死时点 metadata（工程量大，可先守护新迁移）；更新 head 守卫。

### B-2【新 P1】主线红点未收口叠加提交（5 failed + 6 ERROR）

- 当前全量：**5340 passed / 5 failed / 6 ERROR**（上轮 3 failed → 恶化）。除 B-1 的 6E+1F 外：
  - `test_plan_mode_rest_gate.py::test_create_todo_task_with_confirmed_plan_passes`——响应上轮审计新建的 REST gate 测试自身未转绿（红测建了、实现没跟完或断言与实现失配）。
  - `test_agent_message_runtime.py` 三连（async task 跨 agent 拒绝/过滤）——**定性为测试债非安全回归**（主控亲验）：外层 `_check_async_task`（messaging.py:1405-1424）保留归属校验，DB 可用时跨 agent 被拒；DB 不可用新行为返回 `not_found`（不泄露存在性，安全等价）；红因=monkeypatch 目标失配（orchestrator 模块顶部绑定的 `get_runtime_task_record` 未被 patch）+ 断言钉旧语义。
- 按「主线必须全绿」纪律，这 11 个红点应在下一个 commit 前收口。

### B-3【新 P2 清单】

| # | 项 | 证据 |
| --- | --- | --- |
| 1 | **forwarder 无自动重启**：pubsub.listen 异常退出只置 state 不重连，API plane 流式静默死亡直到进程重启（health 可见 running=false，有观测无自愈） | web_chat_stream_bus.py:100-104 |
| 2 | Stream durable 副本（XADD）当前无任何消费者——直播断档的回放依赖前端 HTTP 快照/轮询兜底（设计内 broadcast-only，但 xadd 数据纯留存无读者，回放消费者未建） | grep XREAD 零命中 |
| 3 | `check_async_delegation` DB 分支自身无 parent_agent_id 比对且读时进 RLS BYPASS scope（唯一调用点有外层拦截=纵深缺一层） | orchestrator.py:2282-2296 |
| 4 | provider-config 缓存的 `clear_provider_config_cache()` 死代码（仅测试调用）——密钥轮换有 ≤15s 失明窗口（TTL 自愈） | resource_discovery.py + test_resource_discovery_cache.py:36-37 |

## C. 旧 P1 清单状态（v1.1 §3 对照）

**除测试债转绿外，10 项 P1 全部未动**（本批主题为拆分/性能，未触碰）：P1-1 dream 分脑（auto_dream.py:1542/1991 仍 flat）、P1-2 迁移失明（plane_read 仍无 fallback、迁移未跑）、P1-4 审计身份、P1-5 预算帽、P1-9 D7、P1-10 出处标签、P1-11 resume chip（手写形状测试仍在）、P1-12 hook catalog。P1-8（startup resume 接 claim）**部分推进**：claim 基建与 worker 已建，但 `resume_persisted_web_chat_runs`（web_chat_runtime.py:1568）startup 路径仍直接全量 select+dispatch——API role 下该路径已被禁（main.py:646），残余风险收窄为「runtime plane 自身多副本」场景（当前形态 runtime plane 因 volume 单挂载恰好不能多副本，风险实质冻结）。

## D. v1.2 后的优先级重排

1. **B-1 迁移链修复 + B-2 红点收口**（干净库 upgrade + 全绿是所有后续工作的地板）。
2. 记忆顶端两刀（P1-1/2，未动，仍为 Goal-1 最高债）。
3. 拆分上线前置项：forwarder 自愈循环（B-3-1）+ 部署契约固化（P1-0，现在多一个 backend-api 服务的 env/DNS 契约）。
4. 治理纵深双修（P1-4/5）→ 其余按 v1.1 §7 顺序。

*v1.2 增量复审证据基于 HEAD 820e312a；两轮全量测试数字一致（5340/5F/6E）。审计过程中主控曾初判"WS 流式断"，随后在 main.py:661 发现 API role 的 stream forwarder 接线后撤回——与 v1.1 §0.1 的 P1-7 教训同型（断言"零消费者"前必须穷尽启动接线），记录在案。*

# 增量复审 v1.3（2026-07-03，HEAD 820e312a + 工作树未提交修复批，tracked diff +770/-110，21 文件 + 新增 runtime_control_bus）

范围：针对 v1.2 与并行双进程审查合并清单的集中修复批（尚未 commit）。**全量 `pytest tests -q` → 5359 passed / 0 failed / 0 ERROR（95s）——上轮 5F+6E 全部收口，主线回全绿（+19 新测试）。**

## A. 修复确认（逐项验证）

| 上轮问题 | 修法与判定 |
| --- | --- |
| cancel 跨进程失效（并行线 P0） | ✅ 新建 `runtime_control_bus.py`：Redis Pub/Sub `hive:runtime:control`，API 端点 `cancel_web_chat_run` 本地 set + publish（web_chat_runtime.py:1577-1588），runtime role 独占 listener（main.py:641-643，`_runtime_execution_startup_enabled` gate）→ `apply_remote_web_chat_cancel` 命中 `_CANCEL_EVENTS`。广播语义天然支持未来多 worker；delegation_cancel 同通道 |
| WS idle 记忆降级（并行线） | ✅ `_emit_ws_session_lifecycle_hook`（websocket.py:43-75）role 分支：api → 投递 bus，runtime 侧重放 emit_hook——T0 seal/T2 回到有 volume 进程执行 |
| 6 处 API 端点 T0 split-brain（并行线 P0） | ✅ 全局 role 门 `_bridge_to_t0_enabled()`（chat_transcript.py:92-97）：api role 一律跳过 T0 桥接只写 DB——split-brain 根除 |
| 迁移 6 ERROR + head 守卫（v1.2 B-1） | ✅ conftest rewind 清单补 5 claim 列 + 3 索引；`_CURRENT_CLOSURE_HEAD` 更新。**连带修正 v1.2 判定**：ERROR 直接原因=conftest rewind 清单缺新列（增量升级模拟场景），非纯"干净库从零必炸"（从零链上历史 create_all 只建缺失表，claim 迁移可正常加列）——v1.2 的 P0 定级过重，实际为测试基建债+反模式债 |
| async task 三连（v1.2 B-2） | ✅ **恢复安全语义修**而非改断言：orchestrator 新增进程内 `_async_task_parent_ids`/`_async_task_fallback_records`（含 stale 清理），DB 不可用时仍能做归属拒绝 |
| plan_mode_rest_gate 1 case 红 | ✅ 转绿（全量 0 failed 佐证） |
| 06eb13e9 startup 竞态 | ✅ `_run_after_startup_resume_gate` 门闩：worker 等 resume/reconcile 释放 claim 后启动（main.py） |
| enterprise KB 路由错 plane（并行线） | ✅ nginx 把 `enterprise` 移出 API plane 清单回 runtime plane（volume 依赖） |
| 附带 | files.py 下载 JWT 的 RLS pin（query-string token 先 pin 租户上下文再读，安全正向）；docker-compose 单机 `BACKEND_HOST` 回退显式化 |

## B. 本轮残留（新 P2 为主）

1. **P2：control bus listener 无自愈 + 无 health 暴露**——与 forwarder 同型（异常退出只置 state），且 forwarder 有 health 组件而 bus snapshot 未接 health（grep 零命中）；listener 挂 = cancel/lifecycle 投递静默退回旧 bug，无观测面。建议：两者统一加自动重连 + health 组件。
2. **P1：T0 role 门控是止血，不是完备**——API role 禁 T0 bridge 正确避免 split-brain，不阻塞当前拆分上线；但 API plane 产生的 transcript 事件（session command 结果、feedback、rewind marker 等）除 user-turn（metadata→Worker materialize）与 mid-run（`message_already_in_t0` 标记链）外，在 T0 **永久缺失**且无对账補写。T0 是机械真相源，不能长期只留 DB transcript。正确方向不是恢复 API 写 T0，而是 API 写 DB transcript 后投递 `transcript_t0_bridge` 给 runtime，由 runtime 按 transcript_event_id 从 DB 读取并写 T0。
3. **P2（并入同一落地包）：`session_lifecycle_hook` 把全量 messages 塞进 Redis publish**——当前只在 idle/close 触发，非高频 token 流，不挡当前部署；但长会话下单条 Pub/Sub 消息体积无上限，有 Redis 压力/失败风险（bus 失败仅 debug log）。建议改成只投递 agent_id/session_id/event/source/metadata，runtime 侧从 DB/T0 取会话内容。
4. **P1（维持并升格为反模式债）：迁移引用活模型**——本轮修的是 rewind 清单（第二次补），根因（7 个历史迁移 import 活模型 create_all）未动；每新增模型列都必须记得手补 conftest，第三次犯只是时间问题。建议：为新迁移立"add_column 必带存在性守护"的惯例或修 conftest 自动 diff 模型与旧 head。
5. **P2/P1 边界残留：cancel 后 terminal update 缺少 preserve-killed 守卫**——正常路径下 API 端点写 DB `killed` 并经 control bus 命中 runtime 侧 `_CANCEL_EVENTS`；但 `_apply_terminal_task_update()` 仍无"已 killed 不得被 completed 覆盖"的 DB 状态守卫。若 control bus listener 挂、Pub/Sub 丢消息或 worker 未收到 cancel_event，DB 已 killed 的 run 仍可能被后续 completed terminal update 覆盖。建议补一个 focused regression：先把 RuntimeTask 标为 killed，再模拟 worker terminal completion，断言状态保持 killed。

## B.1 下一刀落地包（T0 relay + lean lifecycle payload）

这两项应放在同一轮做：都围绕 API plane 不碰 volume、runtime plane 承担 T0/Hook 实际副作用。当前状态可部署，但作为 Hive 的长期 T0 契约不能长期停在"API 跳过写 T0"。

1. **测试先行**：`backend/tests/services/test_runtime_control_bus.py` 补 `transcript_t0_bridge` handler 测试；`backend/tests/services/test_chat_transcript.py` 补 api role 下 append_session_event 写 DB 后发布 bridge request、不直接写 T0；`backend/tests/api/test_websocket_call_llm.py` 补 lifecycle hook lean payload 测试。
2. **runtime_control_bus 新事件**：新增 `publish_transcript_t0_bridge(transcript_event_id, agent_id, session_id, source)` 与 handler；handler 只在 runtime role 侧读取 `ChatTranscriptEvent`/`ChatMessage`，再通过现有 T0 append helper 写入 ledger。payload 禁止携带大段 content/messages。
3. **append_session_event 改造**：API role 下仍不执行本地 T0 bridge；DB flush 后发布 `transcript_t0_bridge`。runtime role 保持现有直接 bridge 行为。需要幂等标记，避免 API relay 与 runtime direct bridge 双写。
4. **lifecycle hook 瘦身**：`backend/app/api/websocket.py` 的 `_emit_ws_session_lifecycle_hook` 只投递引用字段；`backend/app/services/runtime_control_bus.py` 的 `session_lifecycle_hook` handler 在 runtime 侧按 session_id 自取上下文后再 emit_hook。
5. **验收命令**：至少跑 `pytest tests/services/test_runtime_control_bus.py tests/services/test_chat_transcript.py tests/api/test_websocket_call_llm.py -q`，再跑 backend full suite，确认 T0 split-brain 不回归且 Redis payload 不再含全量 messages。

## C. 旧 P1 大清单

**依然未动**（本轮是拆分收尾修复批）：dream 分脑、迁移失明、审计身份、预算帽、D7、出处标签、resume chip、hook catalog。修复轮顺序建议不变：红点已清零 → 下一优先仍是记忆顶端两刀。

*v1.3 基于工作树未提交状态（HEAD 820e312a + working diff）；全量测试 5359/0/0 亲验。本批修复质量高：方向全部对症、多数修法恢复语义而非迁就测试。*

---

**旧合流备注已被 v1.3 吸收并 superseded**：同期另一审查线（`docs/runtime-pool-isolation-full-review-2026-07-03.md`）发现的 T0 split-brain、cancel 跨进程失效、startup 竞态已在 v1.3 A 节逐项复核：T0 split-brain 由 `_bridge_to_t0_enabled()` 全局 role 门关闭；cancel 跨进程由 `runtime_control_bus.py` + runtime listener 关闭主要路径；startup 竞态由 `_run_after_startup_resume_gate` 门闩关闭。该旧备注只保留为溯源，不再作为未修 P0 清单；仍需跟进的缩窄残留见 v1.3 B 节（control bus/forwarder 自愈与 health、T0 completeness 契约、session_lifecycle_hook 负载、preserve-killed 守卫）。

# 增量收口 v1.4（2026-07-03，HEAD `e115ec35`，已在 `origin/main`）

主题：**runtime control bus / T0 transcript bridge / lean lifecycle payload / cancel terminal guard 全面闭合**。本轮把 v1.3 B 节中的 T0 完备性与 Redis control-plane 稳定性残留集中落地，并把工作树提交为 `e115ec35 Harden runtime control bus and T0 transcript bridging`。

## A. v1.3 残留关闭情况

| v1.3 残留 | v1.4 结论 | 证据 |
| --- | --- | --- |
| B-1 control bus listener 无自愈 + 无 health 暴露 | ✅ 已关闭 | `runtime_control_bus.py` 增加 `restart_count/last_restart_at/last_error` snapshot；listener 异常或 Pub/Sub 正常返回都会 sleep 后重连；`main.py` `/api/health` 暴露 `runtime_control_bus` 组件。新增 `test_runtime_control_listener_reconnects_after_pubsub_error` 与 `test_health_includes_runtime_control_bus_component` |
| B-2 T0 role 门控只是止血，不是完备 | ✅ 已关闭主要路径 | API role 下 `append_session_event` 不写本地 T0，但在 DB flush 后标记 `t0_bridge_pending=True` 并发布 `transcript_t0_bridge`；runtime listener 读取 `ChatTranscriptEvent`/`ChatMessage` 后写 T0 并回填 `t0_bridge_relayed_at/segment_id/event_id/sequence`。写前会按 `transcript_event_id` 回查既有 T0 事件，防止 DB metadata 回写失败后的重复补写 |
| B-3 `session_lifecycle_hook` Redis payload 携带全量 messages | ✅ 已关闭 | API role `_emit_ws_session_lifecycle_hook` 只投递 event/agent_id/session_id/source/metadata（含 `message_count`），不再传 `messages`；runtime handler 按 session_id 从 DB 取最近上下文再 emit hook |
| B-5 terminal update 可覆盖 killed | ✅ 已关闭 | `_apply_terminal_task_update()` 对已 `killed` 的任务保持状态与原 `result_summary`，仅记录 `terminal_update_preserved_status=killed` 与 attempted status；新增 regression 覆盖 `killed -> completed` |
| forwarder 自愈同型问题 | ✅ 已关闭 | `web_chat_stream_bus.py` forwarder 与 runtime control bus 同步改为异常/正常返回后重连，并暴露 restart 计数 |

## B. 关键设计边界

1. **API plane 仍不碰 T0 文件**：这次不是回滚 `_bridge_to_t0_enabled()`，而是把 API 产生的 DB transcript event 转成 runtime-side relay。split-brain 止血保留，T0 completeness 通过 runtime volume 进程补齐。
2. **Relay payload 禁止携带大内容**：`transcript_t0_bridge` 只带 `transcript_event_id/agent_id/session_id/tenant_id`；lifecycle hook 只带 session 引用与 metadata。runtime 侧按引用自取 DB 内容。
3. **幂等不只靠 DB metadata**：如果 T0 已写但 DB metadata commit 失败，下一次 relay 会先扫描 T0 metadata 的 `transcript_event_id`，命中则只补 DB metadata，不重复 append T0。
4. **health 只做观测，不在 API role 上误判 degraded**：runtime control listener 只应在 runtime execution role 启动；API role 的 `running=false` 不是异常。health 暴露组件状态，是否按角色告警留给部署期望态规则处理。

## C. 验证证据

| 命令 | 结果 |
| --- | --- |
| `cd backend && source .venv/bin/activate && pytest tests/services/test_chat_transcript.py tests/services/test_runtime_control_bus.py tests/services/test_web_chat_stream_bus.py tests/api/test_websocket_call_llm.py tests/api/test_health_liveness.py tests/services/test_web_chat_runtime.py -q` | **113 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && ruff check app/services/runtime_control_bus.py app/services/chat_transcript.py app/api/websocket.py app/services/web_chat_runtime.py app/services/web_chat_stream_bus.py app/main.py tests/services/test_chat_transcript.py tests/services/test_runtime_control_bus.py tests/api/test_websocket_call_llm.py tests/services/test_web_chat_runtime.py tests/services/test_web_chat_stream_bus.py tests/api/test_health_liveness.py` | **All checks passed** |
| `cd backend && source .venv/bin/activate && pytest tests -q` | **5367 passed / 1 skipped / 6 warnings（92.62s）** |

## D. 当前剩余队列（v1.4 后）

本轮关闭的是 runtime-pool/T0 relay/cancel/control-plane 稳定性包；旧 P1 大清单仍需按北极星优先级继续推进：

1. **记忆顶端两刀仍最高优先**：Dream T3→soul 车道仍需切 `plane_read`；两平面迁移仍需 dry-run → apply。
2. **治理纵深仍未动**：委派身份入 invocation span/hash chain；租户/agent 级预算帽。
3. **后台 subagent 永久 running 风险仍未动**：`resume_after_restart` + 最老 50 条 resume 的组合仍需修。
4. **D7 command_pack、出处标签、resume/projection chip、hook catalog 诚实债仍未动**。
5. **迁移活模型反模式仍为工程债**：v1.3 已把直接红点清掉，v1.4 未处理 7 个历史迁移 import 活模型 create_all 的系统性问题；建议立"新 add_column 迁移必须存在性守护"规则或修测试基建自动对账。

# 增量收口 v1.5（2026-07-03，HEAD `29dbadd6`，已推送 `origin/main`）

主题：**记忆顶端两刀代码侧闭合：Dream T3→soul 车道切两平面读面 + plane_read 迁移前失明窗口可观测兼容**。本轮提交为 `29dbadd6 Route dream reads through two-plane memory`。

## A. v1.1/P1 关闭情况

| 旧 P1 | v1.5 结论 | 证据 |
| --- | --- | --- |
| P1-1 Dream T3→soul 车道分脑 | ✅ 代码侧已关闭 | `_read_all_t3()` 不再扫描 `memory/t3/{episodes,user,worker,capabilities}.md`，统一调用 `plane_read.list_t3_memory_documents()`；Soul Dream prompt/source-file contract 改成 `memory/self`、`memory/profiles`、`memory/knowledge`、`memory/milestones`。新增/更新 `test_reads_two_plane_t3_documents`、prompt enum tests |
| P1-2 两平面"切换先于迁移"失明窗口 | ✅ 代码侧已关闭主要读面；生产迁移仍需安全门执行 | `plane_read` 在没有两平面文档但存在 legacy flat-T3 时，返回带 `migration_required/` key 与 `metadata.migration_required=true` 的兼容读结果；`search_plane_facts()` 与 `load_plane_entries()` 都可读到 legacy 存量，但不会把它伪装成正常两平面。新增 `test_plane_read_legacy_t3_fallback_is_observable` |

## B. 关键设计边界

1. **不复活 legacy flat-T3 为正常读面**：fallback 只在两平面完全为空且 legacy flat-T3 存在时触发，key 以 `migration_required/memory/t3/...` 标记，metadata 显式 `legacy_t3=true`、`migration_required=true`。
2. **Dream 的正常输入只来自两平面**：profile entries 聚合自 `memory/self/self.md` 与 `memory/profiles/*.md`；knowledge/milestone pages 直接按页面 source 输入。
3. **生产迁移仍需 dry-run/confirm**：本轮消除静默失明与 Dream 分脑，不自动执行生产数据迁移。真正 move/archive legacy 文件仍应走 `python -m app.scripts.migrate_memory_two_planes --apply --confirm` 的安全门。

## C. 验证证据

| 命令 | 结果 |
| --- | --- |
| `cd backend && source .venv/bin/activate && pytest tests/services/test_auto_dream.py tests/services/test_dream_phase6.py tests/services/test_dream_lifecycle_patch.py tests/memory/test_retrieval_pipeline.py tests/services/test_knowledge_read_model.py -q` | **113 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && pytest tests/memory -q` | **305 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && pytest tests/services/test_auto_dream.py tests/services/test_dream_phase6.py tests/services/test_dream_lifecycle_patch.py tests/services/test_memory_dream.py tests/services/test_growth_report.py tests/services/test_self_evolution_audit.py tests/services/test_knowledge_read_model.py -q` | **119 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && ruff check app/memory/plane_read.py app/services/auto_dream.py tests/services/test_dream_phase6.py tests/memory/test_retrieval_pipeline.py tests/services/test_auto_dream.py tests/services/test_dream_lifecycle_patch.py tests/test_memory_integration.py` | **All checks passed** |
| `cd backend && source .venv/bin/activate && pytest tests -q` | **5369 passed / 1 skipped / 6 warnings（94.51s）** |

## D. 当前剩余队列（v1.5 后）

1. **生产数据迁移执行仍需 owner 安全门**：代码不再失明，但 legacy flat-T3 文件仍应 dry-run → apply 迁移并 archive，避免长期 fallback。
2. **治理纵深**：委派身份入 invocation span/hash chain 已在 v1.6 关闭；租户/agent 级预算帽仍未动。
3. **后台 subagent 永久 running 风险**：`resume_after_restart` + 最老 50 条 resume 的组合仍需修。
4. **D7 command_pack / 出处标签 / resume-projection chip / hook catalog 诚实债** 仍未动。
5. **迁移活模型反模式** 仍为工程债。

# 增量收口 v1.6（2026-07-03，HEAD `2eee3066`，已推送 `origin/main`）

主题：**治理纵深 P1-4 闭合：委派执行身份进入 tamper-evident audit hash chain 与 canonical invocation_spans**。本轮提交为 `2eee3066 Thread execution identity through audit traces`。

## A. v1.1/P1 关闭情况

| 旧 P1 | v1.6 结论 | 证据 |
| --- | --- | --- |
| P1-4 委派身份不进审计哈希链、不在 canonical span | ✅ 已关闭 | `compute_audit_event_hash()` 新增 `execution_identity_*` 输入，`write_audit_event()` 先捕获 `ExecutionIdentity` 再计算 hash；`audit_query_service.verify_chain()` 支持 identity-aware `canonical_v3`，并兼容历史 pre-identity `canonical_v2` 行。`invocation_spans` 新增 `execution_identity_type/id/label` typed columns + tenant/type/id 索引；kernel `_record_runtime_span()` 从 `InvocationRequest.execution_identity` 透传到 DB writer 与 metadata；trace tree 输出稳定 `execution_identity` 对象 |

## B. 关键设计边界

1. **无 identity 的旧 hash 输入保持不变**：`compute_audit_event_hash()` 只在 identity 字段存在时把 `execution_identity` 对象加入 hash payload，因此无 identity 的 canonical_v2 行不被重算打破。
2. **历史兼容显式可见**：已有写过 `execution_identity_*` 列但旧 hash 未覆盖 identity 的事件，`verify_chain()` 会落到 `canonical_v2`，不误判篡改；新事件带 identity 时返回 `canonical_v3`。
3. **canonical span 不再靠 metadata 承载身份**：`metadata_json.execution_identity` 仍保留为 JSONL/调试兼容面，但运营查询面以 `invocation_spans.execution_identity_type/id/label` 为准。
4. **迁移路径双验收**：新增 Alembic head `invocation_span_execution_identity_0703`，bootstrap/create_all 与 chain upgrade 两条路径都通过迁移测试。

## C. 验证证据

| 命令 | 结果 |
| --- | --- |
| `cd backend && source .venv/bin/activate && pytest tests/core/test_policy_audit.py tests/services/test_audit_query_service.py tests/kernel/test_invocation_trace.py::test_record_invocation_span_extracts_truth_evidence_fields tests/kernel/test_invocation_trace.py::test_kernel_persists_invocation_spans_with_runtime_join_keys tests/services/test_invocation_trace_service.py -q` | **13 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && pytest tests/migrations/test_workflow_migration.py::test_upgrade_path_adds_invocation_span_execution_identity_columns tests/migrations/test_workflow_migration.py::test_bootstrap_path_adds_invocation_span_execution_identity_columns -q` | **2 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && pytest tests/kernel/test_execution_identity.py tests/kernel/test_invocation_trace.py tests/services/test_invocation_trace_service.py tests/core/test_policy_audit.py tests/services/test_audit_query_service.py tests/migrations/test_workflow_migration.py -q` | **35 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && ruff check app/core/policy.py app/services/audit_query_service.py app/models/invocation_span.py app/services/invocation_trace.py app/kernel/engine.py alembic/versions/invocation_span_execution_identity_0703.py tests/core/test_policy_audit.py tests/services/test_audit_query_service.py tests/kernel/test_invocation_trace.py tests/services/test_invocation_trace_service.py tests/migrations/test_workflow_migration.py tests/migrations/conftest.py` | **All checks passed** |
| `cd backend && source .venv/bin/activate && pytest tests -q` | **5375 passed / 1 skipped / 6 warnings（93.44s）** |

## D. 当前剩余队列（v1.6 后）

1. **生产数据迁移执行仍需 owner 安全门**：两平面代码侧不再失明，但 legacy flat-T3 文件仍应 dry-run → apply 迁移并 archive。
2. **治理纵深 P1-5**：租户/agent 级预算帽已在 v1.7 关闭。
3. **后台 subagent 永久 running 风险**：`resume_after_restart` + 最老 50 条 resume 的组合仍需修。
4. **D7 command_pack / 出处标签 / resume-projection chip / hook catalog 诚实债** 仍未动。
5. **迁移活模型反模式** 仍为工程债。

# 增量收口 v1.7（2026-07-03，HEAD `e87470ee`，已推送 `origin/main`）

主题：**治理纵深 P1-5 闭合：tenant/company 与 agent 级 token hard cap**。本轮提交为 `e87470ee Add tenant and agent token quota caps`。

## A. v1.1/P1 关闭情况

| 旧 P1 | v1.7 结论 | 证据 |
| --- | --- | --- |
| P1-5 预算无租户/agent 级天花板 | ✅ 已关闭主要 runtime admission path | 新增 `tenants.quota_tokens_per_day/month + tokens_used_*`、`agents.quota_tokens_per_day/month`；`check_user_token_quota()` 扩展为 invocation quota guard，按 tenant → agent → user 顺序检查，admin 只豁免个人 user cap，不豁免 tenant/agent hard cap；`invoke_agent()` 在 kernel 前把 `agent_id` 传入 quota guard；`record_token_usage()` 同步累加 tenant/agent/user 三层 counters；tenant-only autonomous usage 也累加 tenant counters |

## B. 关键设计边界

1. **tenant/agent hard cap 高于 user cap**：企业总帽与单 agent 帽不因 `platform_admin/org_admin` 角色绕过；admin 只跳过个人用户额度。
2. **不恢复旧的 message/LLM-call/TTL quota**：仍保持 token-only 纪律，只新增 token hard cap 和 counter。
3. **兼容既有测试 monkeypatch**：invoker 对旧的一参 `check_user_token_quota` 测试替身做签名兼容；生产函数支持 `agent_id/tenant_id`。
4. **计量面补齐**：AgentKernel path 记录 token 时同时更新 company、agent、user counters；无 agent 的 tenant-only usage 至少进入 tenant counter 和 append-only `token_usage_events`。

## C. 验证证据

| 命令 | 结果 |
| --- | --- |
| `cd backend && source .venv/bin/activate && pytest tests/services/test_quota_guard.py tests/runtime/test_invoker.py::test_invoke_agent_passes_agent_to_token_quota_before_kernel -q` | **4 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && pytest tests/migrations/test_workflow_migration.py::test_upgrade_path_adds_token_quota_hard_cap_columns tests/migrations/test_workflow_migration.py::test_bootstrap_path_adds_token_quota_hard_cap_columns -q` | **2 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && pytest tests/runtime/test_invoker.py tests/services/test_quota_guard.py tests/migrations/test_workflow_migration.py -q` | **68 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && ruff check app/services/quota_guard.py app/services/token_tracker.py app/runtime/invoker.py app/models/tenant.py app/models/agent.py alembic/versions/token_quota_hard_caps_0703.py tests/services/test_quota_guard.py tests/runtime/test_invoker.py tests/migrations/conftest.py tests/migrations/test_workflow_migration.py` | **All checks passed** |
| `cd backend && source .venv/bin/activate && pytest tests -q` | **5381 passed / 1 skipped / 6 warnings（95.18s）** |

## D. 当前剩余队列（v1.7 后）

1. **生产数据迁移执行仍需 owner 安全门**：两平面 legacy flat-T3 文件仍应 dry-run → apply 迁移并 archive。
2. **后台 subagent 永久 running 风险**：已在 v1.8 关闭；未被 resume pump 确认恢复的 restart-resumable 任务会进入 `needs_reconciliation`。
3. **D7 command_pack / 出处标签 / resume-projection chip / hook catalog 诚实债** 仍未动。
4. **迁移活模型反模式** 仍为工程债。

# 增量收口 v1.8（2026-07-03，HEAD `a35a032b`，已推送 `origin/main`）

主题：**P1-6 后台 subagent 永久 running 风险闭合：未确认恢复的 resumable runtime task 不再被 reconcile 永久跳过**。本轮提交为 `a35a032b Reconcile unconfirmed resumable runtime tasks`。

## A. v1.1/P1 关闭情况

| 旧 P1 | v1.8 结论 | 证据 |
| --- | --- | --- |
| P1-6 后台 subagent 重启可永久卡 running | ✅ 已关闭主要机制 | startup 已汇总 async delegation/subagent/web_chat/trigger/heartbeat resume pump 返回的 ids 并作为 `exclude_task_ids` 传给 `reconcile_orphaned_runtime_tasks()`；本轮把 reconcile 的无条件 `_is_restart_resumable_runtime_task() -> continue` 改为只保留已确认恢复/已 exclude 的任务。对未被 resume pump 扫到、resume 异常未返回 id、或超过扫描窗口的 delegation/subagent/trigger/heartbeat，reconcile 会设置 `needs_reconciliation`，并写入 `restart_resume_blocker=restart_resume_not_confirmed`，避免永久 `running` |

## B. 关键设计边界

1. **不破坏已确认恢复的任务**：`exclude_task_ids` 仍最高优先，resume pump 成功返回的 task id 保持 running，由后台任务继续接管。
2. **不误伤专用 durable 类型**：`workflow`、`web_chat_turn`、`team_member`、`goal_continuation`、`advanced_plan` 仍由专用恢复/claim 路径处理，不进入普通 orphan reconcile。
3. **未确认恢复优先诚实化**：对 delegation/subagent/trigger/heartbeat，宁可进入 `needs_reconciliation`，也不再把 restart-resumable 元数据当成无限期 running 许可。

## C. 验证证据

| 命令 | 结果 |
| --- | --- |
| `cd backend && source .venv/bin/activate && pytest tests/services/test_runtime_task_service.py -q` | **12 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && pytest tests/services/test_subagent_run_service.py tests/agents/test_orchestrator.py::test_resume_persisted_async_delegations_rehydrates_tasks tests/agents/test_orchestrator.py::test_resume_persisted_async_delegations_refuses_mutating_profile_without_replay_contract tests/agents/test_orchestrator.py::test_resume_persisted_async_delegations_reconciles_worker_safe_even_with_spawn_journal tests/agents/test_orchestrator.py::test_resume_persisted_async_delegations_refuses_mutating_contract_without_replay_journal -q` | **23 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && pytest tests/test_startup_background_config.py -q` | **7 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && ruff check app/services/runtime_task_service.py tests/services/test_runtime_task_service.py` | **All checks passed** |
| `cd backend && source .venv/bin/activate && pytest tests -q` | **5381 passed / 1 skipped / 6 warnings（93.48s）** |

## D. 当前剩余队列（v1.8 后）

1. **生产数据迁移执行仍需 owner 安全门**：两平面 legacy flat-T3 文件仍应 dry-run → apply 迁移并 archive。
2. **D7 command_pack / 出处标签 / resume-projection chip / hook catalog 诚实债** 仍未动。（v1.9 更新：D7 已关闭，见下节。）
3. **迁移活模型反模式** 仍为工程债。

# 增量收口 v1.9（2026-07-03，HEAD `b719e84d`，已推送 `origin/main`）

主题：**P1-9 D7 command_pack 半退役闭合：CC/Codex command wrappers 不再受 inactive command_pack L2 门控制**。本轮提交为 `b719e84d Promote command wrappers to core tool surface`。

## A. v1.1/P1 关闭情况

| 旧 P1 | v1.9 结论 | 证据 |
| --- | --- | --- |
| P1-9 D7 command_pack 半退役 | ✅ 已关闭 | `task_create/task_update/task_list/task_get/task_output/task_stop/goal_start/advanced_plan/verify_plan` 进入 `CORE_TOOL_NAMES`，`capability_descriptor_for_tool()` 返回 `agent_base`；`command_parity.py` 移除 `pack="command_pack"`；`backend/packs/command_pack/pack.yaml` 与 `packs/command_pack/pack.yaml` 改为 `requires_core` 依赖面；`runtime_tool_groups.py` 过滤 core tools 并丢弃空 runtime pack；禁用 `command_pack` 时 `ToolRuntimeService.execute("task_list")` 仍会进入 registry，不再返回 `extension_disabled` |

## B. 行为边界

- 这不是恢复 inactive pack，而是把 command wrapper 明确归入 agent base command surface。
- `command_pack` 作为历史 facade/manifest 锚点保留，但不再拥有或控制任何这批 runtime command tools。
- `team_create` 仍保持此前独立的 deferred discoverability；本轮没有改变 Agent Team 容器语义。

## C. 验证证据

| 命令 | 结果 |
| --- | --- |
| `cd backend && source .venv/bin/activate && pytest tests/services/test_agent_tools_core_surface.py tests/tools/test_service.py::test_tool_runtime_service_does_not_l2_block_core_command_wrappers tests/services/test_tool_registry.py::test_minimal_kernel_tool_set_stays_small_and_explicit tests/tools/test_core_pack_disjoint.py tests/tools/test_pack_manifest.py tests/services/test_pack_policy_service.py -q` | **47 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && pytest tests/tools/test_cc_codex_parity_tools.py tests/api/test_cc_codex_parity_api.py tests/tools/test_bridge_equivalence.py tests/services/test_cc_codex_parity_substrate.py tests/runtime/test_unified_prompt_contracts.py -q` | **53 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && ruff check app/services/governance_capability_taxonomy.py app/tools/runtime_tool_groups.py app/tools/handlers/command_parity.py tests/services/test_agent_tools_core_surface.py tests/tools/test_service.py tests/services/test_tool_registry.py` | **All checks passed** |
| `cd backend && source .venv/bin/activate && pytest tests -q` | **5384 passed / 1 skipped / 6 warnings（95.53s）** |

## D. 当前剩余队列（v1.9 后）

1. **生产数据迁移执行仍需 owner 安全门**：两平面 legacy flat-T3 文件仍应 dry-run → apply 迁移并 archive。
2. **出处标签 / resume-projection chip / hook catalog 诚实债** 仍未动。
3. **迁移活模型反模式** 仍为工程债。

# 增量收口 v1.10（2026-07-03，HEAD `d72e487f`）

主题：**P1-10/P1-11/P1-12 收口 + subagent terminal follow-up 恢复 CC-compatible continuation**。本轮提交为 `d72e487f Close remaining session framework review gaps`，已推送 `origin/main`。

## A. v1.9 后剩余 P1 关闭情况

| 旧 P1 | v1.10 结论 | 证据 |
| --- | --- | --- |
| P1-10 交付物缺作者/出处标签 | ✅ 已关闭 | `chat_sessions.get_session_messages()` 汇总 artifact owner/source/download/delivery agent id 并批量查询 Agent name；`chat_message_parts` 与前端 `ChatArtifactPart` 保留四类 agent name；Session Runtime 右栏文档行显示 `By {{name}}`，无 name 时回退 agent id 短标或当前 agent 名 |
| P1-11 resume/projection chip 测试假字段 | ✅ 已关闭 | `SessionIndex.active_projection` 接生产字段；resume health 不再依赖虚构 `status=recovered`，而由 `has_t0_truth/has_checkpoints/truth_surface` 派生 `events.jsonl + checkpoints`；active projection 从 `projection_reason` 等真实字段读出 |
| P1-12 hook catalog 诚实债 | ✅ 已关闭 | `notification/elicitation/config_change/instructions_loaded/workspace_context_changed/artifact_changed` 改报 `planned_observe + planned_runtime_emitter`，不再冒充 active；`subagent_start/subagent_stop` 经当前代码复核为 `app/agents/subagent.py` 真实 emit，保持 active |

## B. 附带语义修正

- **completed subagent follow-up**：终态 subagent session 不再一律拒绝并要求 `spawn_new_session`；普通 subagent follow-up 会标记 `terminal_session_resume`、把 session metadata 重新打开，并启动同一 child session 的 continuation turn，贴近 CC `SendMessage/resumeAgentBackground` 语义。非 subagent 的终态 session 仍保持 sealed/reject。
- **Session Workbench subagent 行**：subagent child session 标记 `continuable/inspectable`，但不再作为普通 `enterable` 跳入，避免 UI 把 subagent transcript 混成主会话导航。

## C. 验证证据

| 命令 | 结果 |
| --- | --- |
| `cd backend && source .venv/bin/activate && pytest tests/api/test_chat_sessions_permissions.py::test_get_session_messages_enriches_artifact_agent_names tests/services/test_chat_artifact_delivery.py::test_serialize_chat_message_appends_artifact_parts tests/runtime/test_hooks_cc_parity.py tests/services/test_subagent_resume_ruling.py tests/services/test_agent_session_continuation.py::test_agent_session_continuation_non_subagent_terminal_session_rejects_and_writes_transcript tests/services/test_session_control_plane.py::test_runtime_task_runtime_row_bounds_oversize_metadata_values -q` | **16 passed / 0 failed** |
| `cd backend && source .venv/bin/activate && ruff check app/api/chat_sessions.py app/services/chat_message_parts.py app/runtime/hooks.py app/services/agent_session_continuation.py app/services/session_control_plane.py tests/api/test_chat_sessions_permissions.py tests/runtime/test_hooks_cc_parity.py tests/services/test_agent_session_continuation.py tests/services/test_session_control_plane.py tests/services/test_subagent_resume_ruling.py` | **All checks passed** |
| `cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/chatRuntime.test.ts src/pages/session-workbench/timelineModel.test.ts` | **3 files / 149 tests passed** |
| `cd frontend && npm test -- --run` | **73 files / 436 tests passed** |
| `cd frontend && npm run build` | **tsc + vite build passed** |
| `cd backend && source .venv/bin/activate && pytest tests -q` | **5386 passed / 1 skipped / 6 warnings（92.96s）** |

## D. 当前剩余队列（v1.10 后）

1. **生产数据迁移执行仍需 owner 安全门**：两平面 legacy flat-T3 文件仍应 dry-run → apply 迁移并 archive。
2. **迁移活模型反模式** 仍为工程债：历史迁移 import 活模型/create_all 的模式仍需后续清理，避免新列继续手补 rewind 清单。
3. **低优先 P2 运维/性能项**：runtime control bus health/self-heal、Redis pubsub payload/bridge relay 的持续观测、bare RLS session 收敛仍应进入后续 hardening 队列；不再阻塞本轮 P1 closure。
