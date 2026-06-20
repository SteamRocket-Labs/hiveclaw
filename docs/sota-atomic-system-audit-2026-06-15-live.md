# Hive SOTA 原子化系统审计 · Live 实跑版（2026-06-15）

> 结论先行：以 `docs/hive-sota-master-goal.md` 为唯一目标口径，对 G1–G15 做一轮**全新、实跑取证**的原子化对照（每个目标一个独立审计员真跑 pytest + grep live 接线 + 读本地 CC/Hermes/Codex，每条结论再派对抗复核员证伪，外加横切猎手 + 主理人独立抽查）。**95% 置信度判定：Hive 尚未整体达成 SOTA 总目标**，且按本目标文档 §0 自身规则，在做完真实外部行为 eval 前**本就不允许**宣称整体超越。
>
> 本文是 `docs/sota-atomic-system-audit-2026-06-15.md`（纯读码版）的 **live 验证升级版**：方向一致（未达成、机制广泛接线但 live 行为基线 provisional），但用实跑证据**修正/锐化了 5+1 处**关键判断，并独立**纠正了本轮 workflow 自身的一处过判**（见 §4）。

> 2026-06-15 Codex follow-up：核心 Goal-1 晋升暗臂已做代码级闭环。新增普通租户 `tenant_behavior_eval_publisher`，distiller 在 patch/promote 候选真正写入前会候选驱动地为当前 tenant 产出并持久化 `behavior_eval_latest_report`；只有 `behavior_eval_passed()` 接受的 trusted live report 才注入 runtime config，失败/缺前置仍 fail-closed 并节流。验证：`cd backend && source .venv/bin/activate && pytest tests -q` -> `4590 passed, 7 skipped, 4 warnings`。剩余诚实边界：此 follow-up 关闭的是“普通租户没有 writer 导致永久 HOLD”的代码路径；整体 SOTA/超越 Hermes 仍需要真实生产 live report、rebaseline 和分数时序。

> 2026-06-15 Codex follow-up 2：三项“可立即代码优化”已做代码级闭环。G10：Feishu doc、Feishu Drive、Office 成功读取结果现在携带 authoritative `connector_source_items`，并进入现有 generated-source ACL choke point；G3：新增 memory lifecycle maintenance，heartbeat 会丢弃 expired sketch 并报告 conflict/revalidation hold；G4/G7：mutating subagent/delegation 现在必须有 restart replay journal，否则进入 fail-closed reconciliation，成功 resume 会追加 journal。验证：connector/Feishu/Office 28 passed；memory lifecycle 28 passed；subagent/delegation replay 57 passed。剩余诚实边界：这关闭的是代码路径，不等于 Glean 全连接器生产 ACL、Letta/Zep 级记忆行为证明或 Temporal exactly-once mutating side-effect replay 已达成。

## 0. 取证口径

- 方法：实跑取证（不止读码）。32 个审计 agent，306 万 token，846 次工具调用，~13.6 分钟；主理人对承重发现逐条独立抽查。
- Hive checkout：`4a02e2be`，分支 `main`，worktree 有未提交改动（主要修正 Plan Mode / Work Ledger 命名边界），按当前真实状态审计。
- 本地对标项目（commit 与读码版一致 → 参考代码未变）：
  - CC / Claude Code：`/Users/rocky243/vc-saas/free-code-main` `7dc15d6`
  - Hermes（须达到/超过的 lean benchmark）：`/Users/rocky243/vc-saas/hermes-agent` `75643a615`
  - Codex：`/Users/rocky243/Context Engineering/codex` `9f4fac8ec4`
- 外部对标：Voyager、Reflexion、DGM、ADAS、Letta/MemGPT、Graphiti/Zep、LangGraph、Magentic-One/AutoGen、Temporal、Vercel Sandbox、E2B、Glean、SEAL、AlphaEvolve、AZR、R-Zero、MS Entra Agent ID、Purview。
- 判定口径取自目标文档 §0：`achieved` 必须有当前代码路径 + 测试 + 部署/生产证据且生产可达；`near`/`partial`/`not_achieved` 见目标文档；**built-but-never-called（暗臂）即使测试全绿也不算 achieved**；做完外部行为 eval 前不宣称整体超越。

## 1. 95% 置信答案

**没有整体达成。** 按"机制是否 live + 是否有真实分数"分四层：

- **SOLID（live + 测试 + 生产可达）**——工程地基真扎实，多数轴达到或超过 Hermes：G4 durable（orphan reconcile + web-chat resume + workflow 副作用去重，main.py lifespan 接线，真 Testcontainers PG 测试绿）、G5 workflow（zero-DB 引擎 + PG step/leaf journal + advisory-lock quota + gate/wait/resume + trigger workflow_ref hash 绑定 + DR workflow-native，74 测试含真 PG16 quota 绿）、G6 Plan Mode（plan_markdown 胜出、ask_user 一等、plan hash/version、gate 经 `registry.calls==[]` 证明不可绕过）、G8 Work Ledger（`asyncio.create_task` spy 断言 `created_tasks==[]` 证明写 todo 不触发执行）、G11 本地 OS sandbox（探针**实跑 passed=True**、sandbox-exec 隔离 + deny-all 网络真生效 + 凭据 env 剥离）、G14 invocation_spans（PG-canonical、无条件注入 kernel、Prometheus span 驱动）、G15 互操作诚实（MCP token passthrough 硬门在构造 client 前就拦、A2A/profile 如实标 not_exposed 并列出缺的 RFC）。**RLS 真生效**：64 张表每次启动 `FORCE ROW LEVEL SECURITY`（main.py:196），enabled-but-not-forced = 0 → **证伪 owner 绕过假设**。**自进化遥测 + candidate 管线也是 live 的**（见 §4 纠正）。
- **PROVISIONAL（机制接线 + fail-closed，但无真实分数 → 不能宣称达标/超越）**——整个行为 eval + 自进化效果面：`core_behavior_v1.json` `provisional=true`、`commit_sha=pending-e2-live-run`、6 场景 `score_p50` 全 `0.0`、`transport=pending`。门是对的（fail-closed），但从未观测到一次真实 PASS；没有 Hive-vs-Hermes live delta、没有分数时序、没有 Plan Mode / durable restart / connector access-denial 的生产 trace。
- **DARK（built + 测试绿，但 live 主链从不调用 → 按暗臂律不算达成）**——自进化**晋升步**（非遥测）：整条链每次 heartbeat 真跑（daemon 启动、candidate 记录、skill_guard/verification/session feedback/T3 counters 全 live），但 `decide_behavior_gated_promotion` 对每个普通租户**永久 HOLD**，因为它读的 `behavior_report` 只由 token 门控的 `/eval-ci/behavior` admin 端点在隔离的 `HIVE_EVAL_TENANT_ID` 下写入 —— **没有任何 daemon 为普通租户产出它 → distiller 在生产晋升零技能**。Teammate Mailbox（G13）在默认 `COORDINATION_BACKEND=memory` 部署下暗（信号进进程内单例，prompt 读的 Postgres 表从不被写）。artifact 执行门（G2 真正的 Voyager exec-gate）只能经未被调用的 adversarial_suite 触达。
- **CODE-LEVEL CLOSED BUT LIVE-PENDING**——Feishu/Drive/Office tool-result source ACL metadata、memory TTL/revalidation/conflict maintenance、mutating subagent/delegation replay journal 已接代码主路径并有定向测试；但还缺生产 access-denial trace、live recall eval、mutating exactly-once side-effect replay 样本。仍属 **MISSING** 的外部 SOTA 面：bi-temporal KG、完整外部引用 live revalidation（G3）；CODEOWNERS/branch-protection（G2 人审晋升契约）；完整 MCP resource-server OAuth flow RFC 9728/8707（G15，已如实标缺）；external directory CA / access packages（G9）。

**Goal 1（须达到/超过 Hermes）的底线**：Hive 在 durability、治理、记忆治理、sandbox、trace 面**等于或超过** Hermes；但自进化**结果**当前**落后** lean benchmark —— Hermes 的 curator patch-first（`curator.py:403` action=patch，**无外部行为门**）live 落地技能补丁、agent 越用越强；Hive 更宏大的外部门在生产晋升零技能。**Goal 2（控制中台）**：RLS、sponsor/participant/lifecycle、ExecutionIdentity 审计、workflow 治理 live 且领先 Hermes（无租户概念），但在 directory CA、access packages、权威连接器 ACL ingest 上落后 MS Entra/Glean。

**一句话**：一个诚实埋点、fail-closed、工程扎实的 harness，"机制已接线"与"自进化效果已观测"之间存在真实的生产鸿沟。

## 2. 总目标原子矩阵（post-verification）

| 目标 | 判定 | 置信 | live 证据要点 | 阻断点 |
|---|---:|---:|---|---|
| G1 治理化运行时自进化 | partial | 92% | loop 默认真跑（main.py:480 + invoker.py:91）；skill_guard/feedback/T3 counters live；遥测+candidate live（§4）；34+9 测试绿 | 晋升门对普通租户永久 HOLD（behavior_report 只 admin 端点写）→ 生产晋升零技能 |
| G2 外部硬验证门 | near | 87% | skill_guard 在 save_skill 前 fire（skills.py:132）；`--api-url ''`→EXIT=2 fail-closed；evaluator_integrity 防 in-PR 自 hash 攻击；66 测试绿 | 行为门从未真 PASS；真 exec gate（artifact_gate）全暗；无 CODEOWNERS |
| G3 长期记忆 | partial+ | 88% | write gate + PL4 零保留拒绝 live；PPR over wikilink **live**（wiki_retrieval.py:34→invoker.py:455）；dream supersession live；lifecycle maintenance 接 heartbeat，expired sketch discard + conflict/revalidation hold 报告；62+28 测试绿 | 无 live recall eval；bi-temporal KG 缺失；外部引用 live revalidation 仍缺生产样本 |
| G4 Durable execution | near | 90% | main.py:337 reconcile + :333 resume；断连=订阅变更非取消；完成去重 with_for_update+idempotency_key；mutating subagent/delegation 有 restart replay journal + fail-closed reconciliation；47+19+3+57 测试绿；Hermes 无持久层 | mutating lane 仍非 Temporal exactly-once side-effect replay；无生产 restart trace |
| G5 Workflow 确定性编排 | near | 91% | 74 测试绿含真 PG16 quota（非 skip）；daemon main.py:479；trigger fire trigger_daemon.py:1150；DR 统一 deep_research.py:315；CC/Hermes 无确定性引擎 | 无 live/product eval；无生产 promote/fork lineage trace |
| G6 Plan Mode | near | 92% | plan_markdown 胜出（plan_mode_core.py:1413）；空→missing_plan_body 导向 ask_user；gate 在治理前 fire 且 `registry.calls==[]`；129 测试绿 | 无生产 session trace；无 live UX 样本 |
| G7 Subagent/Delegation | partial+ | 88%* | spawn+delegate live+plan-gated；source=subagent 跳 persist+RESPONSE_COMPLETE（engine.py:3280）；递归 deny-list+深度 cap；mutating restart replay journal 防 contract-only 重放；148+57 测试绿；超 Hermes | 无 live multi-agent eval；mutating lane 仍需生产级 exactly-once side-effect replay；coordination 默认进程内非跨重启持久 |
| G8 Work Ledger | near | 90% | track_todo cognitive-only **证明**（create_task spy `created_tasks==[]`）；invoker.py:1051 注入；replan reminder live（engine.py:2722）；99 测试绿；CC 平表 Todo 的严格超集 | 无 live replan-quality eval；UI 默认可见性未代码证明 |
| G9 企业身份与控制面/RLS | near | 85% | 64 表 FORCE RLS 每次启动（main.py:196，0 漏 force）；21 Testcontainers 测试证非 owner 隔离 + 空 GUC fail-closed；sponsor/participant/soft-delete live | 生产未做 stage-3 role-flip（连 owner，靠 FORCE 无纵深）；external directory/access packages 缺 |
| G10 权限感知数据面 | partial+ | 92%* | choke point live：真 `AgentKernel.handle()` **拦截**泄露的 feishu:// 引用（`[Permission Check]`、chunks==[]、span blocked）；Feishu doc/Drive/Office 成功读取结果携带 authoritative source ACL metadata；11+8+28 测试绿 | 仍缺全 connector / per-document production ACL coverage 与真实生产 access-denial trace |
| G11 安全执行隔离 | near | 90% | 探针**实跑 passed=True**（sandbox-exec、deny-all 网络、workspace round-trip、EXIT=0）；无绕 sandbox 的裸 subprocess；MCP authz live；16 测试绿 | 无持续生产 microVM 样本（store_latest_sandbox_probe 零 daemon 调用）；Vercel 臂仅 mock；Darwin profile allow-default 弱于 Codex deny-default |
| G12 Context/cache 经济 | near | 90% | CJK 估算 GATE 压缩（memory_service.py:421）；cache anchor 只标 last-assistant（engine.py:2770）；压缩送 full old_messages（修复 [-40:] 违例）；120 测试绿 | Code-Execution-over-MCP 模块树缺失；无生产 cache 命中率快照 |
| G13 多 agent 编排 | partial | 90% | Team Context from RuntimeTask live（invoker.py:581）；Progress Ledger replan reminder live（reminder_scheduler.py:362）；131 测试绿 | Teammate Mailbox 默认 backend 暗（§3-#4）；无 live multi-agent eval |
| G14 Eval/观测闭环 | partial | 92% | persist_invocation_span 无条件注入（invoker.py:932→engine.py:777）；/metrics span 驱动；admin trace-tree live；18 测试绿 | baseline provisional 全 0.0；晋升绑定 live report 暗；无分数时序 |
| G15 互操作诚实 | near | 92% | passthrough 门在构造 MCPClient 前 fire（web_mcp.py:1148，`calls==[]`）；URL userinfo/token query 在 10 处 __init__ 拒；A2A/profile not_exposed + 列缺的 RFC；9 测试绿 | 完整 MCP resource-server OAuth flow（RFC 9728/8707）缺（已如实标）；OAuth client 仅单测未对真服务器跑 |

\* G7/G10 的对抗复核 agent 撞服务端瞬时限流失败；其 investigation 完整且判定本就保守（partial），矩阵采用 investigation 判定（见 §9 诚实边界）。

无任何目标达到 `achieved` —— 与目标文档"无 live 行为 eval 即不达 achieved"的规则一致。

## 3. 横切发现

| # | 严重度 | 发现 | 证据 | 影响 |
|---|---|---|---|---|
| 1 | **critical** | 自进化**晋升臂**生产永久暗 → 晋升零技能，抵触 §5 G1"learning brain/patch-first 已实装"自评 | `decide_behavior_gated_promotion`（evolution_verification.py:574）读的 `behavior_report` 唯一 writer 是 `store_latest_behavior_eval_report`（eval_ci_service.py:191/211），其唯一非测试调用方是 token 门控的 `POST app/api/eval_ci.py:62` —— 跨 daemon/kernel/runtime grep 无自治 publisher；只有隔离的 `HIVE_EVAL_TENANT_ID` 被喂。**主理人独立复核确认**（§4） | G1、G2（门可用但永不满足→闭环不闭）、G14、北极星 Goal 1 |
| 2 | high | 无真实 live 行为基线（provisional 全 0）→ 任何"自进化生效/整体超越"声明无支撑且被 §0 禁止 | provisional=true、pending-e2-live-run、6 场景 0.0/pending；**主理人复核确认** | G14、G1/G2、北极星 ≥hermes 标尺 |
| 3 | ~~high~~→**已纠正为 low** | ~~`record_skill_execution` 遥测从未接 live 主链~~ → **证伪**：有 live 来源 | **workflow 过判**。`invoker.py:1089/1201/1204` 在每次 invocation 终态经 `skill_runtime_telemetry.record_skill_runtime_usage_for_invocation` → `record_skill_runtime_usage` → `record_skill_execution`，抓 `load_skill` 调用并判 success/failed/noop。横切猎手只 grep 直接调用名漏了间接层。**主理人独立逮到并纠正**（§4） | 实际：遥测/candidate 管线 live，使 G1 地基比 synthesis 评价更好；不改 G1 partial（晋升仍暗） |
| 4 | medium | Teammate Mailbox 默认部署暗：信号进进程内单例，prompt 读从不被写的 Postgres 表 | `COORDINATION_BACKEND` 默认 `memory`（config.py:169），全仓 env/yaml/toml/Dockerfile/sh **零覆盖**；`CoordinationSignal` 行只由 coordination_repository.py:113（postgres 路径）写；renderer agent_team_context.py:151 读 Postgres → 默认恒空。**主理人复核确认** | G13、G7 |
| 5 | low（净正） | RLS 运行时 DB 真生效（64 表 FORCE 每次启动）→ **证伪 owner 绕过假设** | `len(RLS_FORCED_TENANT_TABLES)=64`、enabled-but-not-forced=0、apply 于 main.py:196；FORCE 连表 owner 也约束→生产以 owner 连仍隔离；21 测试证。残留：`OR tenant_id IS NULL` 逃生门 + 未做 stage-3 role-flip（无纵深）。**主理人复核确认 64** | G9/G10 净正 |
| 6 | medium | 生产 alembic 迁移失败非致命，被 create_all + 44 条幂等 entrypoint ALTER 掩盖（belt-and-suspenders，非纯漂移） | entrypoint.sh:164 `alembic upgrade head || echo WARNING`（非致命）+ 44 条 `ADD COLUMN IF NOT EXISTS`；main.py create_all 仅 log。缓解：抽样列均有 migration，alembic heads 单头，未发现具体缺 migration 的列 | G9 schema 完整性；运维健壮性非直接旁路 |

## 4. 主理人独立复核日志（逮到 1 处过判 + 确认承重项）

为达 95% 置信，对最承重、最"打脸已实装自评"的发现逐条亲自实跑：

- **确认 · 晋升臂暗（#1 主因）**：`grep store_latest_behavior_eval_report` → 唯一非定义调用方 `app/api/eval_ci.py:62`（admin 端点）。✔
- **确认 · RuntimeConfig 字段存在（修正读码版）**：`contracts.py:93 skill_distiller_behavior_report: dict|None = None`。读码版"无字段→永久 hold"框定**不准**——字段在、每次 live invocation 赋值；暗的根因是**租户级缺写**（更精确也更致命）。✔
- **确认 · RLS 64 表 FORCE（#5）**：`from app.db_bootstrap import RLS_FORCED_TENANT_TABLES; len=64`。✔
- **确认 · Teammate Mailbox 暗（#4）**：`COORDINATION_BACKEND` 默认 memory、零覆盖、writer 仅 postgres 路径、renderer 读 Postgres。✔
- **🔧 纠正 · 遥测 live 来源（#3）**：synthesis/横切猎手判 `record_skill_execution`"零 live 调用方"。实跑 `grep record_skill_runtime_usage` 发现 **`invoker.py:69/1089/1201/1204`**（所有路径单一 live 入口）在每次终态经 `skill_runtime_telemetry.py:113` → `record_skill_runtime_usage`（skill_lifecycle.py:351）→ `record_skill_execution`。读 `skill_runtime_telemetry.py` 确认它抓 `load_skill` tool_events、判 success/failed/noop、落 lifecycle，**真实且有功能**。→ **横切 #3 证伪、#1 次级子句"零 live 调用方"证伪**；但 #1 主因（晋升门永不满足）**不变**。净效果：自进化的"采集/记录候选"是 live 的，只有"激活晋升"那一步暗。

**方法论意义**：workflow 的横切猎手 + synthesis 双双漏了一层间接调用，把一个 low 误判成 high "暗臂"。独立复核承重声明在本轮直接改变了一条结论——这正是 95% 置信门槛的价值。

## 5. 与本地对标项目差距

- **CC**：Plan Mode 对齐（plan_markdown≈CC 自由计划、ask_user≈AskUserQuestion）并加 hash/version + 共享 gate；唯一缺 CC 的 allowedPrompts（批准时的 scoped Bash 权限），Hive 用 gate+handoff 替代。Hive 在 workflow（CC 只有 flag 后的 LocalWorkflowTask 无确定性引擎）、租户/RLS、PG-canonical invocation_spans 上**超过** CC。Hive 在完整 MCP resource-server OAuth flow 上**落后** CC（xaa.ts/auth.ts 有 RFC 9728/8707/8693 + WWW-Authenticate step-up，Hive 如实标 not_exposed）。
- **Hermes**：Hive 工程地基在 durability（Hermes 无 DB/持久层）、治理、记忆治理、sandbox、递归 deny、trace 面**等于或超过** Hermes。**决定性差距方向相反且正中 Goal 1**：Hermes curator（curator.py:403 经辅助模型 agent action=patch，**无外部行为门**）live 落地补丁、agent 真变强；Hive 更宏大的外部门**生产晋升零技能**。按北极星"须达到/超过 Hermes"，Hive 自进化**结果**当前落后，尽管门设计更严更对。
- **Codex**：Hive 本地 OS sandbox 真且实测可用，但 Darwin profile 是 allow-default+denylist（subprocess_sandbox.py:47），弱于 Codex Chrome 系 deny-default+whitelist（seatbelt_base_policy.sbpl）。Hive **无**凭据 egress proxy；Codex 有真 per-sandbox HTTP(S) proxy-routing（proxy_routing.rs 注入 HTTP_PROXY/NPM/PIP_PROXY），Hive 只有 deny-all/hostname-allowlist + cred:// 占位句柄。Codex Linux 加 landlock+bwrap，Hive 仅 bwrap。Hive **领先**处：远程 microVM provider（Vercel Sandbox）用于不可信 Railway 生产，Codex 无；A2A 描述面 Codex 无。Codex 记忆（per-rollout 抽取 + consolidation sub-agent）与 Hive extract_agent+auto_dream 架构平行，均 LLM 驱动、均非结构化时序图。

## 6. 外部 SOTA 差距（均为目标文档自列的下一层）

1. Voyager 执行门 / AlphaEvolve 可编程 evaluator —— Hive skill_guard 是**内容扫描**非 exec 门，真 exec 门（artifact_gate microVM）暗。
2. Zep/Graphiti bi-temporal KG + Letta sleep-time edit + Copilot TTL/引用重校验 —— Hive 已有 heartbeat lifecycle maintenance，但仍缺 bi-temporal KG、live recall eval 和外部引用 live revalidation。
3. Temporal/LangGraph exactly-once mutating-activity replay —— Hive mutating lane 已有 restart replay journal + fail-closed reconciliation，但还不是 exactly-once side-effect replay。
4. Glean 权威 per-document source ACL ingest —— Hive 有检索侧 choke point，也已给 Feishu/Drive/Office tool result 补 source ACL metadata；仍缺全 connector/per-document production ACL coverage 与生产 denial trace。
5. MS Entra Agent ID directory CA + access packages + Purview agent-to-agent 审计图 —— 缺失。
6. Decagon 式 100% QA + 公布的 live 分数时序（SWE-bench/tau-bench/GAIA）—— Hive 无任何真实行为分。

## 7. 解锁"整体 SOTA"的步骤（按依赖序）

1. **产出真实 live 行为基线**（前置一切）：对部署的 Railway/Vercel target 跑 `hive_live_runner`（带 `HIVE_EVAL_API_URL` + `HIVE_EVAL_CI_TOKEN`），采 6 场景 trusted-transport 分数，rebaseline `core_behavior_v1.json`（commit_sha、provisional=false）。按 §0/§6 这是任何"整体超越"声明的硬前置。
2. **闭合普通租户的晋升臂**：(a) 接一个生产 daemon 跑 live 行为 eval 并按租户写 `BEHAVIOR_EVAL_LATEST_REPORT_SETTING_KEY`（不止 `HIVE_EVAL_TENANT_ID`），让 `decide_behavior_gated_promotion` 能过，并由 Skill Distiller / Skill Gate exact-commit reviewed `SKILL.md.draft`；或 (b) 加 Hermes 式 curator-judgment 低风险补丁晋升 lane（带可逆 archive）让"有东西真落地"。当前候选面已迁到 `evolution/skill_candidates/<candidate_id>/` package，旧 `skill_activation_candidates.md` 队列不再是 runtime path。
3. ~~把 `record_skill_execution` 接进 live 主链~~ —— **§4 复核显示已完成**（invoker 终态 hook）；本步可移除。
4. 采一次 live multi-agent/delegation 行为 eval（G13 首要下一层）+ 复杂多步任务的 Hive-vs-Hermes live delta，证明编排有可测收益。
5. 修 Teammate Mailbox 默认暗：生产 deploy 配置默认 `COORDINATION_BACKEND=postgres`，或让 renderer 读 memory backend 真写的信号源。
6. 把 Feishu/Drive/Office 新接入的 source ACL metadata 跑到生产 access-denial trace，并继续扩展到全部 connector / per-document ACL coverage；目标不是只有 tool-result metadata，而是 Glean 级“模型完全收不到不可见项”。
7. 落地人审晋升契约：加 CODEOWNERS + branch-protection/required-review 让 DGM 防御的 hash 比对在 merge 真生效，并把 evaluator_integrity + live 行为门按 PR 跑（非只 nightly）。
8. 执行 stage-3 RLS role-flip（NOBYPASSRLS app role）做纵深；采生产 restart-recovery trace（G4）+ access-denial trace（G10）；把 `store_latest_sandbox_probe_evidence` 接 daemon/cron 持续采 microVM 样本。

## 8. 本轮 live 测试汇总

15 个目标共实跑 **~1,100+** 条 targeted 后端测试（durability/RLS 处用真 Testcontainers PG16），**零失败**。代表性：G1 distiller+evolution_verification 34 + session_feedback+skill_guard 9；G2 66（含 `--api-url ''`→EXIT=2 fail-closed）；G3 62；G4 47+19+3；G5 74（含真 PG16 quota PASSED 非 skip）；G6 129（gate 不可绕过 `registry.calls==[]`）；G7 148；G8 99（cognitive-only `created_tasks==[]`）；G9 21 Testcontainers RLS + 11 lifecycle；G10 19（真 handle() 拦泄露源）；G11 16 + **探针实跑 passed=True**；G12 120；G13 131；G14 18；G15 9。主理人本会话独立复核：provisional baseline、晋升臂暗、RLS 64 FORCE、RuntimeConfig 字段、遥测 live 来源（纠正）。

**注意**：所有行为级声明都建立在机制 + 单测/集成测试 + 一次本地 sandbox 探针实跑之上 —— **没有任何测试跑过真实生产 agent-core live 行为 eval**（需当前环境不可得的 secrets），这正是无目标达 `achieved`、不宣称整体 SOTA 的根本原因。

## 9. 诚实边界

1. G7、G10 的对抗复核 agent 撞服务端瞬时限流失败；二者 investigation 完整、判定保守（partial），主理人未对其逐条独立复核 —— 其 live 证据强度略低于其余 13 目标。
2. workflow 横切猎手/synthesis 漏过一层间接调用导致一处 high 过判（§4 已纠正）；不排除其余 medium/low 发现仍有类似精度瑕疵，但承重的 critical/high 与所有 "dark/REFUTE" 结论均经主理人独立实跑确认。
3. 生产运行态的 RLS FORCE、Vercel microVM 真样本、晋升臂闭合后的真实晋升，均无法在本沙箱验证，须按部署节奏取证。
