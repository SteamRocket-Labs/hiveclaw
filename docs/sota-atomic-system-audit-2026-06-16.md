# Hive 第三轮 SOTA 原子化系统审计（2026-06-16）

> 结论先行：以 `docs/hive-sota-master-goal.md` 为唯一目标口径，对 G1–G15 做第三轮全新实跑取证（41 个审计 agent / 478 万 token / 1276 次工具调用 / 27 分钟；每个目标取证员 + 对抗复核员，7 路 delta 命门深挖，3 路横切猎手，主理人对 3 处最承重发现逐条独立实跑复核）。**95% 置信判定：Hive 尚未整体达成 SOTA 总目标**，且按目标文档 §0 自身规则，在做完真实外部行为 eval 前**本就不允许**宣称整体超越。
>
> 本文是 `docs/sota-atomic-system-audit-2026-06-15-live.md` 的下一轮验证。HEAD 已从上轮 checkout `4a02e2be` 推进到 `82c60ac7`（领先五个提交，含 `82947830 Close tenant behavior eval promotion gate`、`38fd6f2f Harden restart replay and connector source ACLs`）。本轮核心价值：**验证这批"已闭环"提交是真闭环还是"代码存在≠生产活着"的换皮暗臂**。结论：是诚实的真实工程进步，但**移动的是失败点而非消除了失败**。

## 0. 取证口径

- 方法：实跑取证（不止读码）。41 agent，pipeline 流水（G1-G15 取证→对抗复核）+ 并行 delta 深挖 + 横切，主理人对承重发现独立抽查。
- Hive checkout：`82c60ac7`，分支 `main`。
- 本地对标项目：CC/Claude Code `/Users/example-owner/vc-saas/free-code-main`；Hermes（须达到/超过的 lean benchmark）`/Users/example-owner/vc-saas/hermes-agent`；Codex `/Users/example-owner/Context Engineering/codex`。
- 外部对标：Voyager、Reflexion、DGM、Letta/MemGPT、Graphiti/Zep、LangGraph、Magentic-One/AutoGen、Temporal、Vercel Sandbox、E2B、Glean、SEAL、AlphaEvolve、AZR、MS Entra Agent ID、Purview。
- 判定口径取自目标文档 §0：`achieved` 必须当前代码路径 + 测试 + 生产可达三者齐全；built-but-never-called（暗臂）即使测试全绿也判 dark；fail-closed 门若在真实生产永远不满足前置 = 暗臂换皮，不算闭环；provisional/0.0 baseline 不算真实分数；做完外部行为 eval 前不宣称整体超越。

## 1. 95% 置信答案

**没有整体达成。** 判定分布：**SOLID 0 / near 8 / partial 6 / 无一 achieved（全部受 §0 封顶）**。

硬证据（实跑级，非推断）：部署 baseline `backend/app/evals/baselines/core_behavior_v1.json` 仍 `provisional=True` / `baseline_version=0.1.0-provisional` / `commit_sha=pending-e2-live-run`，6 个确定性场景（coding/review/research/operations/memory_recall/long_context_after_compaction）**全部 score_p50=0.0、transport=pending**。`git log` 确认该文件自 `3dd14578`（2026-06-13 E1 种子）后**零次修改**——本轮 HEAD 领先上轮五个提交，但没有产出过一个真实行为分数点。Goal-1「单 agent 智能/记忆/可靠 ≥ hermes」的**核心验证信号至今 0 个真实数据**。

**净判定与上一轮一致**：内核晴天对齐 CC 基线（治理咽喉不可绕过、压缩 L1、replace 语义、Plan Mode 确认边界、durable workflow exactly-once），SOTA 增量层广度领先 lean benchmark；但每一条「自进化是否真让 agent 变强」「记忆生命周期是否真生效」「跨重启是否 exactly-once」的硬指标，要么 provisional（接线就绪、无真实分数），要么 dark（built + 绿但 live 从不调用）。**机制广度 ≥ 各家，唯一硬指标全空。**

诚实度总评：本轮自评文档（目标文档 §5/§6/§7 + `sota-atomic-system-audit-2026-06-15-live.md`）**基本诚实**——所有「code-level closed」声明都带「仍缺生产 live evidence」对冲，未发现「文档说已达成、代码 dark 且未披露」的虚报。唯一需更新的过时框定见 §4/§5。

## 2. 四层分类

| 层 | 定义 | 归此层 |
|---|---|---|
| **SOLID**（live + 测试 + 生产可达，持平/超过 lean benchmark） | G8 Work Ledger（cognitive-only 不变量真 spy 测试钉死，owner-loop 本轮真接通）、G9 身份/生命周期机制层（soft-delete + Participant + ExecutionIdentity，64 表 FORCE schema 级应用）、G12 Context/cache（CJK 估算/last-assistant anchor/provider hints/压缩送 full old_messages 全 live）、G15 互操作诚实（MCP passthrough 三处硬门 + A2A not_exposed）、G5 Workflow 编排机制（daemon live + leaf exactly-once 真 PG 验证 + trigger pin hash + admin ops） |
| **PROVISIONAL**（接线 live + fail-closed 正确，但无真实分数/无生产 live PASS） | **G1 自进化晋升臂**（命门，§4）、**G2 外部硬验证门**（behavior gate 严格不可绕过但从未真 PASS）、**G14 Eval/观测**（invocation_spans/metrics SOLID，behavior 分数面全空）、G6 Plan Mode（机制全 live，缺生产 trace + live UX）、G11 安全执行隔离（本地 OS sandbox 真生效 ≥ Hermes，Vercel microVM 臂 mock-only + probe 无 scheduler） |
| **DARK**（built + 测试绿但 live 主链从不调用 = 暗臂换皮） | **artifact_gate / adversarial_suite**（G2 宣称的 Voyager 式 exec 门，唯一引用是 integrity path-string）、**Teammate Mailbox**（G13，默认 `COORDINATION_BACKEND=memory` 写读跨后端裂脑恒空）、**memory lifecycle 三 hold 臂**（G3，create_sketch/record_conflict/mark_reference_revalidation_required 三 writer 零生产调用方，maintenance 报告恒 0）、**需 mutating 重入的 reconciliation 闸门**（G7，spawn 无条件盖 journal→闸门恒满足→生产实为重复副作用重跑）、`needs_reconciliation` 写侧无消费者（write-only dead-letter） |
| **MISSING**（外部 SOTA 面缺失，已诚实标 not_exposed/下一层缺口） | Code-Execution-over-MCP（G12，CC/Hermes 同缺）、完整 MCP resource-server OAuth flow（RFC 9728/8707，G15 已标 not_exposed）、bi-temporal KG（G3，Zep/Graphiti 双时态）、external directory Conditional Access + Purview A2A 审计图（G9，MS Entra Agent ID 纵深）、Glean 式权威 per-document connector ACL ingest（G10，Feishu ACL 自指非权威） |

## 3. G1–G15 矩阵（复核后判定，采用对抗复核 corrected_verdict）

| 目标 | 判定 | 置信 | live 证据要点 | 阻断点（命门优先） |
|---|---|---:|---|---|
| **G1 治理化运行时自进化** | partial | 92% | heartbeat→`_maybe_run_skill_distillation`→`ensure_skill_distiller_behavior_report`(skill_distiller.py:158) 真接 live；`BEHAVIOR_EVAL_AUTO_PUBLISH_ENABLED=True`+`skill_candidate_loop_v1=True` 默认开；晋升门 `decide_behavior_gated_promotion` 硬要求 `behavior_eval_passed` 不可绕过 | **从未观测一次真 live PASS**；baseline 全 0.0/provisional；测试 mock 掉 `run_hive_behavior_eval`；distiller 路径不传 regression_report→E1 回归门跳过 |
| **G2 外部硬验证门** | near | 90% | behavior_eval_passed 严格门（transport∈{hive_live,live_cli}∧benchmark_complete∧not fallback∧全 ready）；skill_guard 内容硬扫 live；CI fail-closed（无 secrets exit 2 非静默 skip） | **真 exec gate（artifact_gate microVM）全 dark**（唯一引用是 integrity path-string）；无 CODEOWNERS 人审契约；从未 live PASS |
| **G3 长期记忆 SOTA** | partial | 90% | 写门 live（PL4 拒绝）；读侧 activation 抑制 live（TTL expired + reference invalid 真抑制）；dream 矛盾对账 live；PPR wikilink 检索 live | **三 hold 臂 dark**（三 writer 零生产调用方）；bi-temporal KG 完全缺失；OpenViking prefilter 默认休眠（OPENVIKING_URL=''） |
| **G4 Durable execution** | near | 93% | lifespan resume+reconcile 三链 live；workflow leaf **真 exactly-once**（engine.py:480-488 真 PG 测试硬断言 done step 不重跑）；web-chat finalization 去重 | mutating delegation/subagent lane **非 exactly-once**（从持久化 prompt 整段重跑）；零生产 restart trace |
| **G5 Workflow 编排** | near | 90% | daemon 生产 lifespan live；leaf exactly-once 真 PG 验证；trigger pin version+hash mismatch→suspend；DR workflow-native；promote 强制 human approver | **无 live WorkflowEngine 行为 eval**（bakeoff transport=repo_evidence 写死分数）；无生产 promote/fork lineage trace |
| **G6 Plan Mode** | near | 90% | gate 三入口共享不可绕过（execute_approved 不能跳，`registry.calls==[]` 钉死）；hash/version 精确匹配；exit_plan_mode 拒空正文；request_plan_mode 真接 live + 前端审批环闭合 | 无 plan-mode 语义阶段 trace（未接 decision_trace）；无 live UX 样本 |
| **G7 Subagent/Delegation** | partial | 93% | spawn_subagent 治理咽喉 live；递归 deny-list+depth+环检测 live；source=subagent 防 T2 泄漏 live；tenant 隔离 live | **mutating 重入 journal 闸门=暗臂换皮**（恒满足→生产重复副作用重跑，方向相反恶化，§4/§5-③）；delegation 被排除出硬门 eval；coordination 默认进程内 |
| **G8 Work Ledger** | near | 90% | cognitive-only 不变量真 spy 测试钉死（reminders 永不入 api_messages）；owner-loop ledger_todo_id→stamp→write-back fail-closed 本轮真接通；UI 默认可见代码证明 | 无 live replan-quality eval；needs_replan 启发式未对真实多步运行验证 |
| **G9 企业身份/RLS** | near | 90% | apply_rls_policies 无条件 startup（main.py:196）；49⊆64 表全 FORCE（实跑确认）；soft-delete 级联 + lifecycle fail-closed；**stage-3 role-flip scaffolding 已就绪**（grant_rls_app_role.py + entrypoint Step 2.6 + SCHEMA_DATABASE_URL 分离） | **stage-3 cutover 默认未激活**（SCHEMA_DATABASE_URL=None→连 owner）；**无运行时角色断言**→若生产连 superuser 则 FORCE 失效（§5-②）；`OR tenant_id IS NULL` 逃生门；external directory CA/Purview A2A 缺 |
| **G10 权限感知数据面** | partial | 93% | choke point 真拦截（stream buffer 防 mid-stream + 终态硬 enforce）；OpenViking prompt-entry prefilter live（Glean 式，但默认休眠） | **Feishu ACL 自指 tautology**（读取 agent 恒 allow）；Office 仅 tenant-scope；无生产 access-denial trace；非权威 per-document ingest |
| **G11 安全执行隔离** | near | 92% | provider selector + 本地 OS sandbox live（Darwin sandbox-exec 探针实跑 PASSED）；治理不可绕；env credential allowlist 边界二次执行 | **probe 永不调度**（无 daemon/cron→score trend 空）；Vercel microVM 臂 mock-only；Darwin allow-default 弱于 Codex deny-default；无 egress proxy |
| **G12 Context/cache** | near | 92% | CJK 估算驱动压缩触发 live；last-assistant anchor live in-loop（engine.py:2775）；压缩送 full old_messages（24e13b97 修复累计 usage 误判 bug）；命中率快照端到端 | 无真实生产命中率样本（in-memory 计数器无持久化）；Code-Execution-over-MCP 缺；anchor 单断点弱于多断点 SOTA |
| **G13 多 agent 编排** | partial | 95% | Team Context（RuntimeTask 派生）真 live（两端同走 PG）；workflow_daemon live | **Teammate Mailbox 默认 dark**（写内存/读 PG 永久裂脑恒空）；coordination 完成唤醒链默认 dark；无 live multi-agent eval |
| **G14 Eval/观测闭环** | partial | 93% | invocation_spans PG canonical live（单入口 + NULL-tenant fail-closed）；Prometheus metrics 真计数器；behavior 硬门设计正确 | **0 个真实行为分数**（baseline provisional/全 0.0）；per-PR gate 纯静态（不调 invoke_agent）；真 exec-gate dark |
| **G15 互操作诚实** | near | 90% | MCP passthrough 三处硬门（URL userinfo + token-query + config-dict，构造器级不可绕过）live；A2A card 访问控制 + /interoperability/profile not_exposed 反注水 | 完整 resource-server OAuth flow 缺（已标 not_exposed）；OAuth client 仅单测；oauth_authorization_client.status="implemented" 措辞略乐观（有 honesty_boundary 兜底） |

## 4. 与上一轮命门逐一裁决（最关键章节）

### 命门 #1 — 自进化晋升臂「生产永久暗」 → **半修复（dark → provisional/live-pending）**

上一轮 critical：普通租户无 behavior_report writer → `decide_behavior_gated_promotion` 永久 HOLD → 生产晋升零技能（结构性 by-construction dark）。

本轮 `82947830` 新增 `tenant_behavior_eval_publisher.py`（289 行）。**逐行核实为真 delta、非换皮**：`ensure_skill_distiller_behavior_report` 唯一调用方确为 `skill_distiller.py:158`（candidate 驱动），普通租户 fallback 到当前 agent+creator（不需 CI token），`BEHAVIOR_EVAL_AUTO_PUBLISH_ENABLED=True`+`skill_candidate_loop_v1=True` 默认开。结构性「永久 HOLD」已被移除——**这是诚实且真实的代码改进**。**同时证伪了上一轮悬而未决的「save_skill 自授权旁路」假设**：`_submit_skill_activation_candidate` docstring 明写 "without activating it"，只排候选不写 live SKILL.md，两条路径已收敛同一行为门。

**但仍非真闭环**：(1) 所有 publisher 测试 monkeypatch 掉 `run_hive_behavior_eval` 返回手写 `_passing_report`，**真 invoke_agent 跑出全 6 场景 ready 从未在任何测试或生产被验证**；(2) 晋升门要求 6/6 场景各 ≥80 的 all-or-nothing 硬门，一个弱场景 HOLD 全部技能补丁，而租户可能用小模型；(3) distiller 路径不传 regression_report → E1 回归门跳过。

裁决：**dark → provisional（live-pending）。从「结构性不可能」推进到「代码可达但生产从未触发一次真晋升」。**

### 命门 #2 — provisional baseline 全 0.0 → **零修复（仍 provisional，本轮字节级未变）**

实跑确认 `core_behavior_v1.json` 仍 provisional=True、6 场景全 0.0、transport=pending、commit_sha=pending-e2-live-run，自 E1 种子（`3dd14578`）后零修改。`82947830` 是晋升臂喂料端，**根本不触碰 baseline、不产生真实分数**。CI 门实跑确认 fail-closed 真硬（把 provisional 当 live report 喂入→exit 2），但门后从来没有真实分数进来。

裁决：**仍 provisional。Goal-1 核心验证信号 0 个真实数据点，本轮 0 进展。**

### 命门 #4 — Teammate Mailbox 默认暗 → **零修复（仍 dark，逐字相同）**

实跑确认 `COORDINATION_BACKEND="memory"`（config.py:174），全仓 env/yaml/toml/Dockerfile/sh 零覆盖。写侧三个 signal 发送方经 `gateway_scope` 默认走 `InProcessCoordinationGateway`→写进程内 list；读侧 `build_prompt_facing_team_context` 无条件直查 Postgres `CoordinationSignal` 表。**写内存、读自空 PG，默认部署永久裂脑，Mailbox 块恒空且静默消失**。本轮唯一触碰这些文件的 `2f89c611` 是泛化重构，未改默认后端。

裁决：**仍 dark。多 agent 协作上下文在默认部署从不进入 prompt。**

### 新 delta 裁决（`38fd6f2f`/`4a02e2be` 那批「已闭环」）

- **connector source ACL（G10）→ 半修复**：metadata 确进 choke point（真接通 live），choke point 真拦截（非 no-op）。但本轮新增的 Feishu/Office ACL 是**自指 tautology**（读取 agent 用同一 agent_id 校验→恒 allow），生成输出 block 分支对单 agent 生产**不可达**。真正 live 的 denial 是**预先就存在**的 OpenViking prompt-entry prefilter（且默认休眠）。**加了一条没扣环的安全带。**
- **mutating restart replay journal（G4/G7）→ 换皮恶化**（主理人代码级确认，§5-③）：`start_subagent_run` 把 resumable 从 replay_safe 条件改为**无条件 True** + spawn 时无条件盖 `spawn_intent_recorded` journal → `has_mutating_restart_replay_journal` 闸门**恒满足**→ needs_reconciliation 分支生产死 → mutating worker 从 prompt 整段重跑（重复副作用）。git diff + claude-mem 历史证此前是 non-resumable fail-stop。**方向相反的退步，自评「fail-closed reconciliation」与真实生产行为矛盾。**
- **memory lifecycle maintenance（G3）→ 换皮暗臂**：maintenance 任务本体 live 接 heartbeat，但它报告的 `discarded_expired`/`conflict_holds`/`revalidation_holds` 三计数的输入 writer（create_sketch/record_conflict/mark_reference_revalidation_required）**全零生产调用方**→生产恒为 0。

## 5. 主理人独立复核日志（3 处承重发现逐条实跑）

为达 95% 置信，对最承重、最会改变结论的发现亲自实跑核实：

- **确认 · policy_replay doc-drift（横切 #2）**：`ls backend/app/memory/policy_replay.py` → No such file；`CLAUDE.md:240` 仍把 `memory/policy_replay.py` 列为「activation policy changes must pass replay guard」live 治理控制；`git log --diff-filter=D` 证它在 `2d368d64`（"delete dead orphan modules"）被删，连同 `replay_corpus.py`；全仓零非测试 import。✔ **裁决：doc-drift after orphan deletion（不是凭空虚构，但净效果相同——一条文档化硬门无实现）。可立即修文档。**
- **② 精确化 · RLS superuser（横切 #3，纠正 workflow + 纠正上一轮）**：workflow 判「stage-3 role-flip 未执行、无运行时角色断言」。实跑发现 **stage-3 scaffolding 已就绪**：`grant_rls_app_role.py` 创建 `app_rls` LOGIN（NOSUPERUSER NOBYPASSRLS）、`entrypoint.sh:170` Step 2.6 prep、`SCHEMA_DATABASE_URL` owner/app 角色分离、config/database/main 全有 role-flip 处理。但 `SCHEMA_DATABASE_URL` 默认 `None`（注释："Unset = same as DATABASE_URL，pre-cutover：both are the table owner"）→ **默认部署连 owner**；`grep rolbypassrls/rolsuper/pg_has_role=0` → **确无运行时角色断言**；entrypoint grant 失败 non-fatal。**精确裁决：不是「没做」，是「开关建好但默认没拨 + 无角色断言兜底」。** 若 Railway 用默认 postgres superuser 连接，**PostgreSQL FORCE ROW LEVEL SECURITY 对 SUPERUSER 不生效** → 64 表 FORCE 在生产买不到隔离。**这同时纠正了上一轮「FORCE 证伪 owner 绕过」——那是过度声明**（FORCE 只约束非 superuser 的 table owner，对 superuser 连接无效）。
- **③ 确认 · mutating replay 换皮恶化（命门新 delta）**：读 `runtime_task_service.py:146-168` `has_mutating_restart_replay_journal` → line 166-167 检查 `phase=="spawn_intent_recorded"` 即 return True；`subagent_run_service.py:62/73-83` spawn 时无条件给 mutating 也盖此 entry → resume 段（line 197/202）`replay_journal_ok`+`replay_contract_ok` 恒 True → `if not replay_safe and (not ... or not ...)` 恒 False → 永不进 needs_reconciliation。claude-mem 历史确认此前（6-15）subagent non-resumable（crash 留 parent dangling，无重复副作用）。✔ **裁决：换皮恶化属实——needs_reconciliation 在正常 spawn→crash→resume 流程恒不触发，mutating subagent 无条件 resume 整段重跑；自评「fail-closed reconciliation」与生产行为矛盾。**（对冲：metadata 损坏/journal schema 不匹配时仍会进 reconciliation，故是「正常路径闸门空转」而非「代码死分支」。）

## 6. 横切 critical/high 发现汇总

1. **【high — DARK ARM】Voyager 式 exec 门 dark**：`run_artifact_execution_gate` 唯一调用方 `adversarial_suite.py`，后者唯一非自引用是 `evaluator_integrity.py:28` 的 path-string。晋升的唯一硬信号是 skill_guard 内容扫描 + behavior eval，**真 exec gate 从不触达**。
2. **【medium — STALE GATE】`memory/policy_replay.py` 不存在**：CLAUDE.md:240 仍列为 live 治理控制；模块在 `2d368d64` 删除（§5-①）。activation-policy 变更实际不经任何 replay 检查。
3. **【high — RLS 生产 superuser 风险】**：stage-3 scaffolding 就绪但默认未 cutover + 无运行时角色断言；FORCE 对 superuser 失效（§5-②）。
4. **【high — Teammate Mailbox dark】**：见命门 #4。
5. **【medium — connector ACL fail-open】**：无 governed prefix 的源 = fail-open 进 prompt/output；generated-output 校验仅 source-id 子串匹配，转述/摘要的禁止内容若不回显 source-id 永不被捕获。
6. **【medium — write-only dead-letter】**：`needs_reconciliation` 状态零消费者（api/frontend grep=0），运维无界面查找/清理被隔离的 mutating restart-orphans。
7. **【low — 反向验证 live 确认】**：`record_skill_execution` 遥测、connector ACL 拦截、action_preflight、proactive_employee_loop 经 grep 确认确为 live（非 dark），真实暗面 narrows 到 artifact-exec-gate + 删除后未更新文档的 policy_replay + Teammate Mailbox 默认。

## 7. 北极星裁决

**Goal-1（自进化结果 ≥ Hermes）：未达到，且诚实成立。** 晋升臂代码已闭环（dark→live-pending）是真实进步，但 Hive 晋升门要求 `behavior_eval_passed`（全 6 场景 ready + trusted transport），而 baseline 仍 provisional/全 0.0、生产从未触发一次真晋升。对照 Hermes `curator.py:403` `skill_manage(action=patch)`——由 curator LLM 自身判断 + 仅 cadence/idle 门、**无外部行为硬门**→patch-first 真落地、agent 真变强。**Hive 设计更严更对（外部硬验证是护城河方向），但自进化实际效果当前落后 lean benchmark。** 按 §0，跑出真实 live baseline 前不能宣称追上或超越。

**Goal-2（控制中台）：机制地基 SOLID 且结构性超过 lean benchmark，但承重开关未拨下、纵深未交付。** Hive 有 Hermes/CC 都没有的一等 agent 身份（Participant type=agent）、sponsor 生命周期、64 表 RLS schema、durable RuntimeTask、MCP 多租户硬门、A2A 诚实契约——真增量。但：(1) RLS 真隔离的 stage-3 cutover 生产未激活（默认连 owner，superuser 下 FORCE 失效）；(2) Teammate Mailbox 多 agent 协作默认裂脑恒空；(3) external directory CA/Purview A2A 审计图缺位；(4) Glean 式权威 per-document ACL 未实装。**控制面是「纸面 ≥ 各家、生产纵深半成」。**

## 8. 解锁整体 SOTA 的下一步（按依赖序）

1. **【解锁 §0 的唯一钥匙】配 eval secrets，跑一次真实 live behavior eval 并回填 baseline**。这是所有 PROVISIONAL→SOLID 转化的前置：G1 晋升、G2 硬门、G14 分数面全部卡在「机制就绪 + 0 真实数据」。nightly 跑真 invoke_agent 6 场景，把 provisional/全 0.0 替换为真实分数 + 真实 commit_sha，并与 Hermes 做首次 live delta。**在此之前任何「已达成/超越」声明都被 §0 禁止。**
2. **【RLS 真生效，Goal-2 纵深第一仗】执行 stage-3 cutover**：设 `RLS_APP_PASSWORD`→创建/校验 NOSUPERUSER NOBYPASSRLS `app_rls`→切 DATABASE_URL（SCHEMA_DATABASE_URL 留 owner）→**加运行时角色断言**（启动拒 superuser/bypassrls 连接）。**前置硬约束**：先穷举 ~184 处 bare session accessor 影子验证（漏一个=生产 fail-closed 崩），并硬化 pre-auth 路径（login/register/SSO 走 enter_rls_bypass，消除对 `OR tenant_id IS NULL` 逃生门的依赖——否则复现 2026-06-11 全员 401）。
3. **【接通三条 dark 臂或诚实降级文档】**：(a) Teammate Mailbox——默认 `COORDINATION_BACKEND=postgres` 或让 reader 读 memory 信号源，消除写读裂脑；(b) memory lifecycle 三 hold——给 create_sketch/record_conflict/mark_reference_revalidation_required 接生产 writer，否则从 maintenance 报告移除恒-0 字段；(c) artifact_gate——接进晋升/CI 路径，或文档撤回「Voyager exec 门实装」表述。
4. **【清理文档-代码漂移】**：从 CLAUDE.md:240 移除已删除的 `memory/policy_replay.py` 引用及「activation policy changes must pass replay guard」不变量。给 `needs_reconciliation` 接消费者（admin API + 运维界面），消除 write-only dead-letter。
5. **【mutating lane exactly-once 或回退 fail-closed】**：G7 当前 journal 闸门恒满足→重复副作用重跑是方向性退步。要么实装 leaf-level completion journal 真去重（Temporal 式），要么回退到上一轮 fail-closed 不重跑——当前状态比修复前更差且自评矛盾。
6. **【Glean 对齐 G10】**：用 `feishu_sharing` collaborator API 拉文档真实分享权限填 source ACL（替代自指 agent_id），并把 OpenViking per-document prefilter 从默认休眠改为默认部署激活（设 OPENVIKING_URL）。

---

**一句话收束**：本轮 delta 是诚实的真实工程进步（晋升臂从结构性死锁解开、connector ACL 接通、workflow exactly-once 验证），但**移动的是失败点而非消除了失败**——Goal-1 的核心命门（真实 live 行为分）零进展，三条 dark 臂未接通，一条 mutating lane 反而退步。Hive 仍是「晴天对齐 CC、机制广度 ≥ 各家、但所有硬指标待真实 eval 点亮」的状态，按 §0 **不得宣称整体达成或超越 SOTA**。
