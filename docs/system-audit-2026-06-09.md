# Hive 全系统审计 — 2026-06-09

> 范围：除 A2A 内部协调外的全部 Agent 体系。方法：6 路并行深查（租户隔离/AI-Native L1/提示词/CC 对齐接线/技术债/记忆系统），每项结论带 file:line 证据与 FACT/INFERENCE 标签；P0 级发现由主审逐一亲读代码复核。
> 基线：main @ a4c30ab7 + 未提交工作区（飞书 Base 分页等在途工作，TDD 配套完整，非烂尾）。

## 0. 一句话总判

**内核是真的，边缘是假的，地基有洞。** Agent 自我进化内核（提取/压缩/dream/heartbeat/subagent）经接线级验证已达 CC benchmark 质量且生产真实可达；但 (1) 多租户隔离的 DB 级防线实际是摆设、API key 存量泄漏属实且明文回显，(2) 交互边缘层（Plan Mode 入口、IM 渠道）仍有正则在替模型说话，(3) ~10 处核心管线静默吞错让"断了没人知道"的事故类随时可复发。

---

## 修复进度（逐 phase 更新 · 当前全部未 push）

提交序在 `main`，自 `a4c30ab7` 起。

### ✅ Phase 0 — 在途工作落地
- `713014cf` workspace `_grep_search` 正则修正（此前宣称 grep 实为子串匹配）
- `5beb5998` 飞书 Base 分页/全表扫描/字段过滤（在途功能，24 测试绿）

### ✅ Phase 1 — 租户安全一揽子（P0-1/P0-3 完整闭环；P0-2 转独立迁移主线）

| 子项 | commit | 证据 | 状态 |
|------|--------|------|------|
| 1A mask 止血 | `1cefa3c4` | 序列化出口 secret→`__HIVE_SECRET_SET__`；写回 merge 防哨兵覆盖原值；API 边界单测钉「明文绝不进 payload」「新租户不继承哨兵」 | **生产即生效** |
| 1B 存量 key 清洗 | `e1e48c47` | `plan_global_secret_scrub` 纯函数（6 测试）+ dry-run 脚本搬全局行 secret 到 platform 租户并清空全局行；`--apply` 需 `--confirm` | **代码完成待生产执行** |
| 1C 加密落盘 | `678ab241` | `enc:v1:` tag 策略：无 master key 存明文零回归、存量明文不喂 decryptor；runtime `resolve_tool_config` 唯一读路径出口解密；4 测试 | **生产即生效**（有 master key 时新写入自动加密） |
| 1D RLS 覆盖审计 | `a9b8d5bd` | `analyze_rls_coverage` 纯函数分类 UNPROTECTED/INERT/ENFORCED（6 测试）+ 扫 pg_catalog 脚本 | 路线图就绪 |
| 1E 下线清理 | `02a8c32c` | `delete_tenant` 调 `scrub_tenant_tool_secrets` 擦租户 secret override；2 测试 | **生产即生效** |

回归：tool 读写面 62 passed，全程 ruff clean。

**P0-2 RLS 判定（重要，非偷懒）**：让 RLS 真正成为防线需要 ①app 改非 owner 角色（或全表 FORCE）+ ②37 处 bare `async_session` 迁到 `tenant_scoped_session`（GUC 注入）+ ③agent-scoped 表写 join policy。三者缺一而贸然 FORCE，会让无 GUC 的 bare session 只见 `tenant_id IS NULL` 行 → **锁死生产**。这是独立的、需灰度+回滚预案的迁移主线，不能塞进本 phase「一次改完」——本 phase 交付 1D 审计脚本量化 gap 作为该迁移路线图。当前隔离仍由应用层 WHERE 承担（与改前一致，未降级）。

**待生产执行清单（owner-gated）**：① `python -m app.scripts.scrub_global_tool_secrets --apply --confirm`（清存量泄漏 key，根治 runtime fallback）；② 设 `SECRETS_MASTER_KEY` 后存量 key 仍为明文，重保存一次即加密（或后续补一次性 re-encrypt 脚本）。

### ✅ Phase 2 — 交互边缘去机械化（Goal 1 体感，5 commits）

| 子项 | commit | 证据 |
|------|--------|------|
| 2-1 砍 `_SCHEDULE_RE` pre-LLM 劫持（P0-5） | `722428d7` | classify schedule 文本→none 不再 recommend；web+8 IM 删 canned 模板拦截，agent 用提示词自行建议；保留 recommendation API/accept 路径；净删 131 行；pin 测试更新为 fall-through |
| 2-2 IM 历史去 10 条帽（P1） | `4deb2256` | feishu `history[-10:]`→全量（已窗口感知 limit）；42 渠道测试无回归 |
| 2-3 trigger 传 session_source（P1-1） | `536d6817` | trigger_daemon call_llm 传 source/channel=trigger；修无人值守 plan lane 死路 + T0 bucket 污染；测试钉死 captured session_source |
| 2-4 裸确认无 plan fall-through（P0-6） | `bdc3e3b2` | bare「可以」无 plan→fall through 给 agent；显式确认无 plan 保留提示；对照测试 |
| 2-5 IM 续跑结果回投（P1-2） | `a2478352` | execute_web_chat_run 完成投递回原渠道（web 跳过）；fail-soft 带日志；IM 投递 + web 跳过双测试 |

**剩余 L1 机械项（audit-l1 识别，归后续 phase）**：① kernel 60min 时间微压缩（零压力清工具结果）+ 撞 max_tokens 不 escalate 重试 + web_search Tier-2 关键词硬拒 → **归 Phase 3**（均在 engine.py/routing，与可观测性同改）；② 隐私 phone 正则改写记忆内容 → **归 Phase 4**（需 LLM 分类，跨记忆系统）；③ rerank 3s 超时 / 上传 8000 截断 / delegation 5000 suffix 帽 / 反思 6 关键词 / T2 提取 2500 截断 → 较小 L1，最终报告列明取舍。

### ✅ Phase 3 — 可观测性 + schema 权威 + kernel L1（3 commits）

| 子项 | commit | 证据 |
|------|--------|------|
| 3-1 核心管线吞错可观测 | `adcf2281` | 7 处静默 fail-soft 加 WARNING/ERROR：kernel 压缩后重建(soul/T3/recent file)、DR-routing import、heartbeat SOP 降级(error 级)、proactive(debug→warn)、memory_config；44 测试无回归 |
| 3-2 schema 权威（P0-4） | `99a87ede` | 5 patch-only 列补幂等 alembic migration（oidc_*、execution_identity_*）；与 entrypoint patch 收敛单一来源；single-head 测试更新 |
| 3-3 kernel 微压缩低压豁免（audit-l1 #5） | `f07b71a0` | <50% 窗口利用率不再按时间销毁工具结果（heartbeat KAIROS/DR 长会话保留证据）；纯函数 gap 测试钉死边界 |

**Phase 3 范围判断（诚实）**：① max_tokens 撞顶 escalate 重试（CC 升 64K）= 重试逻辑较大改动，归后续 CC 细化；② web_search Tier-2 关键词硬拒**移除** = 行为改变需评估测试影响，本 phase 仅给 import 失败加 log（F13），完整移除归后续；③ entrypoint `alembic upgrade head` 失败转 fatal = 运维决策（需先一次性 stamp 验证），owner-gated 不在代码层贸然改（避免锁死启动）。

---

## 1. P0 — 立即处理

### P0-1 租户 API key 泄漏 — 根因已坐实（主审亲核）

用户观察属实，且**比观察到的更严重：不止显示泄漏，运行时也在用旧租户的 key 调用**。

三层叠加：

1. **存量数据债**。`c492fdc6`（2026-04-13）修复了写路径——现在 builtin 工具（tenant_id IS NULL）一律写 `TenantToolConfig`，连 platform_admin 也不再碰共享行（`api/tools.py:665-670`，主审亲核）。但该修复的 migration 是纯 DDL（`add_tenant_tool_config_0413.py`，33 行零数据清洗），**修复之前写进全局 `tools.config` 行的 key 从未迁出**；`tool_seeder.py:78-82` 还刻意保留已有 config（"NEVER overwrite user-configured config"）。
2. **显示与运行时都以共享行为底座**。`tool_config_service.py:152` 与 `:58` 均 `base_config = dict(tool.config or {})`；新租户没有 override 行 → 原样拿到全局行内容。模块 docstring 自己承认："Direct reads from Tool.config leak API keys across tenants"。
3. **明文回显**。`api/tools.py:345` `_serialize_tool` 把 merged config 原样进 JSON，零 mask；`:437` `_serialize_agent_tool_row` 还额外裸暴露 `global_config: tool.config`。

**修复形态**：① 一次性迁移：把全局行中 secret 字段搬进拥有者租户的 `TenantToolConfig` 并清空全局行（dry-run + 确认门）；② 服务端 mask（读端只回 `has_value`/`***`，永不回明文）；③ `global_config` 字段同样处理。

> 模式注记：这是「修复落地、存量不迁」的第 3 个实证（同病：记忆 D2/D8 生产未跑、`db_legacy_*` 出生即孤儿）——正是交付纪律「一次改完含 legacy-data backfill」要堵的洞。

### P0-2 RLS 对核心表是摆设；隔离唯一防线 = 应用层 WHERE

- app 以**表 owner** 连接，PostgreSQL owner 默认绕过 RLS；代码自认："the production connection IS the table owner … so ENABLE alone is inert there"（`db_bootstrap.py:39-41`）。只有 workflow + coordination 表加了 `FORCE`（`db_bootstrap.py:44-52`）。
- 9 张核心表（agents/users/llm_models/skills/tools/plaza_posts/org_departments/org_members/config_revisions）+ mcp_servers 仅 ENABLE → 全部被 owner 绕过。
- 另有一批租户表**完全没有 RLS 策略**：chat_sessions、chat_messages、tasks、runtime_tasks、triggers、schedules、tenant_settings、tenant_tool_configs、channel_configs、notifications、agent_tools。
- 后台任务 37 处裸 `async_session()` vs 仅 8 处 `tenant_scoped_session` 采纳——daemon 内任何漏 `tenant_id` 过滤的查询都是静默跨租户读。
- CLAUDE.md 宣称 "PostgreSQL RLS policies enforce isolation at DB level" 目前**只对 workflow/coordination 成立**。

**修复形态**：全部租户表 ENABLE+FORCE（或改非 owner 角色连接）→ 补缺失表的策略 → 按租户后台工作迁 `tenant_scoped_session`。

### P0-3 租户 secret 明文存储 + 软删不清

- `Tool.config`/`TenantToolConfig.config`/`AgentTool.config` 纯 JSONB 明文落库；Fernet（SECRETS_MASTER_KEY）只用在 WeChat 渠道凭据（`wechat_personal_service.py:43-57`）。DB dump = 全租户 key 外泄。
- 租户删除是软删且无清理（`api/tenants.py:269-270` 自认无统一 cascade）——下线租户的 key/渠道配置/workspace 永久存留。

### P0-4 Schema 权威三裂，alembic 失败被吞（主审亲核）

- `backend/entrypoint.sh:149`：`alembic upgrade head || echo "...non-fatal..."` —— `set -e` 下迁移失败照常起服务；patch 循环逐条 try/except 只 print。头部注释还在描述旧的 `stamp head` 设计，与代码不符。
- **5 列只存在于 entrypoint patch、无任何 alembic migration**：`users.oidc_sub`、`users.oidc_issuer`、`security_audit_events.execution_identity_type/_id/_label`。Docker 外按 CLAUDE.md 跑 `alembic upgrade head` 的环境缺列 → ORM SELECT 500。
- 实际契约：新库靠 lifespan `create_all`；存量 Docker 库靠 entrypoint patch；alembic 能跑就跑、跑不动静默。
- **修复形态**：拍板唯一权威——补 5 列真 migration + 一次性核实后 stamp + alembic 失败转 fatal；或正式宣布 create_all+patch 为权威并降级 alembic。alembic 本身健康：69 revisions 单 head（`retire_task_supervision_0608`）。

### P0-5 Plan Mode 入口正则劫持用户回合（主审亲核）— AI-Native L1 最大现行违例

- `plan_mode_core.py:120-124` `_SCHEDULE_RE`：消息中任意位置出现「每天/每周/定时/提醒我/监控/盯着/有变化/schedule/daily/monitor/watch」→ `classify_plan_mode_entry` 返回 `mode="recommend"`。
- web：`web_chat_runtime.py:581-595` 直接返回 canned 推荐模板、run 标记 completed，**LLM 该回合根本未被调用**；IM：`feishu.py:2178-2202` 同样拦截（服务全部 8 渠道）。
- 「这份报告和上次比有变化吗？」「'watch' 这个词什么意思？」→ 拿到一段固定中文模板。与已砍的 `_LONG_TASK_RE` 同形；prompt 层的 `plan_mode_guidance`（建议判断已教给 agent）**已存在且已注入**（`prompt_builder.py:514-520`）——正则层是重复且覆盖它的旧层。
- **修复形态**：删除 pre-LLM recommend 拦截，建议权完全归 agent 提示词（显式进入 plan mode 的协议解析保留）。

### P0-6 IM 裸确认双向伤害（主审亲核）— 全部 8 渠道

`_BARE_PLAN_CONFIRM_RE`（确认/同意/开始/执行/可以/confirm/approve/start/go）+ `allow_bare_latest=True`（8 渠道全开）：

- **无待确认计划时**：用户一句"可以"（回答 agent 任何问题）→ 返回模板「没有找到当前会话待确认的计划。请带上 plan_id…」（`api/feishu.py:78-79`），**agent 永远看不到这条消息**。中文聊天最高频的单字回复被吞。
- **有待确认计划时**：回答无关问题的"可以"会误确认并启动计划（`feishu.py:86-93`）。
- **修复形态**：无 plan 时必须 fall through 给 agent；确认收窄到显式形态（plan_id / 「确认这个计划」），或给 agent 一个 confirm_plan 工具由模型判断语义。

---

## 2. P1 — 核心目标受损

### 交互边缘的机械化残留（"体感机械"的来源）

| # | 发现 | 证据 | 影响 |
|---|------|------|------|
| 1 | **IM 历史砍到 10 条**（主审亲核） | `feishu.py:2332` `history[-10:]`；各渠道先按窗口算 limit 加载、再被统一砍 | IM agent 超过 ~5 轮即失忆，256K 窗口闲置；web 不受影响 |
| 2 | **trigger daemon 伪装 web**（主审亲核） | `trigger_daemon.py` 零处 session_source；`websocket.py:256` 默认 `"web"` | 无人值守 plan lane 对 trigger 死路（测试钉的是合成 SessionContext——"绿测试生产空转"又一实证）；T0 双写 chat-*.md + trigger-*.md，污染 T2 桶权重，直接喂养记忆变脏 |
| 3 | **IM plan 确认后结果不回投** | `web_chat_runtime.py` 续跑只 broadcast web 事件 + 写 DB，无渠道投递调用 | IM 用户收到「已启动执行」后永久沉默，结果只在 web 端 |
| 4 | **IM 显式进 plan 只回 plan_id 不回计划正文** | `feishu.py:2280-2289`（plan 已生成但不渲染） | 8 渠道盲确认流：用户确认一个看不见的计划 |
| 5 | **kernel 按时间销毁工具结果** | `engine.py:2656-2722`：>500 char、60 分钟前、非最近 5 条 → 无条件覆写为占位符，零压力也清、无 spill 指针 | 长会话 / heartbeat KAIROS 持久会话（tick 间隔 2h 必超 60min）丢证据 |
| 6 | **web_search Tier-2 关键词硬拒** | `routing_reminder.py:149-194`：第 6 次 web_search + 历史含「市场分析」等 15 词 → 硬 block | L2（约束行为）越界成替模型决策；Tier-1 advisory 才是对的机制 |
| 7 | **DR 路由/参数由正则定** | `web_chat_runtime.py:424-496`：关键词决定 handoff_target/mode/depth | 「深入研究一下这家公司」无魔法词 → 错过 DR lane |
| 8 | **隐私 phone/email 正则仍是主分类器** | `privacy_layer.py:58` 经 `write_gate.py:42` 改写每条持久记忆；D9 只修了时钟误杀 | 生产已实证 `<Phone_1>` 误杀 ID/金额；credential 硬拦是合法 L2，PII 判断应归提取 LLM |
| 9 | **撞 max_tokens 不升级重试** | `llm_client.py:212` 检测在、metric 在，无 escalate（CC 升 64K 重试） | 已知未修项，确认仍开放 |
| 10 | **反思触发 = 6 关键词表** | `hooks_setup.py:191-199`（"wrong/错了/不是/failed/失败/loop guard"扫最后 6 条） | "不是"海量误报；换措辞的不满漏报 |
| 11 | **rerank LLM 3 秒超时** | `retriever.py:92`；超时 → 机械序 fallback（INFERENCE：典型延迟 >3s ⇒ fallback 事实主路径，需查 `memory_rerank_fallback` metric 确认） | 记忆排序退化为词重叠打分 |
| 12 | **上传文档静默截 8000 字符** | `upload.py:53-90`，无截断标记 | 50 页 PDF 模型只见 2 页且不知道还有更多 |
| 13 | **delegation/coordinator suffix 5000 字符帽** | `prompt_builder.py:38,595-599` | 已知项确认仍在：超长委派 brief 被行截断 |
| 14 | **T2 提取每条消息先截 2500/2000** | `extract_agent.py:428-458`，在窗口预算之前无条件截 | 长纠正的尾部对提取器不可见 |

### 静默吞错批次（与 06-05 summary-model 事故同族，一个小 PR 可清）

后端共 442 处无日志异常处理（多数是返回错误信封给 agent 的可观测路径，原始计数偏高）；**真正危险的 ~10 处核心管线**：

- `kernel/engine.py:1062,1119,1144,1164` — **压缩后重建上下文吞错**：soul.md 读失败 → 静默无身份重启，T3/最近文件同样 continue 吞掉。直接对应「记忆漂移」体感类。
- `heartbeat.py:430-438` — HEARTBEAT.md 模板读失败 → 整套蒸馏 SOP 静默降级为一行硬编码 stub，蒸馏器继续"工作"。
- `trigger_daemon.py:130-137` — trigger 去重状态静默 fallback 到系统 temp 目录（重启即被清 → 重复 fire 零信号）；happy path 本身也是单进程本地文件业务态（违反持久化纪律）。`:583` `_since_ts` 解析失败静默放宽扫描窗口。
- `heartbeat.py:767-768` — proactive 层失败只记 **DEBUG**，生产 INFO 级下整层可以永久断掉无信号（它是该模块唯一生产消费点）。
- `memory_service.py:609-622` — 租户记忆配置 DB 错误 → 静默 `{}` 跑默认值。
- `memory_service.py:134-136` — 记忆上下文构建任何异常 → 返回空串，agent 静默失忆，无 metric。
- `engine.py:746` — DR hard-reject 层 import 失败 → 整层静默关闭。
- `heartbeat.py:518` / `trigger_daemon.py:358,421` — active-hours 解析失败 → 静默永远活跃。

**修复形态**：统一 WARNING + counter（`memory/metrics.py` 模式已现成，提取管线事故后已这么修过）。

### 记忆系统专项

D1-D10 状态：**D1✅ D2 代码✅/生产未跑 D3 部分 D4✅(一处 P1 缺陷) D5✅ D6✅ D7✅ D8 读路径✅/文件未删 D9✅ D10✅**。系统性出血已止（写门不可绕、telemetry 不再改写正文、dream 不能静默推翻 Mission、增长有泄压阀）。剩余腐烂向量排名：

1. **P1 — dream merge 会消灭它要合并的规则**：`retire_t3_entries`（`t3_store.py:407-418`）只保护「包含 keep 原文」的行，而 DREAM few-shot（`auto_dream.py:185-191`）明确教 LLM 输出**改写合成**的 keep 行 → 所有旧变体按 drop 针匹配归档、canonical keep 永不写入 → 活跃 T3 丢失三次确认的规则（仅 archive.md 可考古）。测试只钉了 verbatim 情形。这是新机制下唯一让记忆**主动变糟**的路径。
2. **P1 — D2/D8 生产清洗未执行**：代码齐（`admin.py:728-751` dry-run 默认），存量脏数据 + 半文件格式分裂仍在；无 sidecar 的旧条目 heat=0 → 退役排序歧视存量。
3. **P2 — T2 在 curation 停摆时无界增长**：active 行永不归档（`t2_store.py:607`），retryable hold 持续时 RESPONSE_COMPLETE 还在追加，且 hold 无聚合告警（只有逐 tick 日志 + 拉式面板）。
4. **P2 — 提取 cursor 按 agent 全局**，并发会话（web + trigger 交错）下竞态 → 重复提取（paraphrase 过 T2 精确去重）或漏提取。
5. P3 组：`lifecycle.json` 无清理（生产已 80KB 且现在是唯一遥测库）；INDEX.md 每次 T3 写都重建但零读者；T0 chat cursor 仅内存（重启重写整线程）；absorb-mark 按文件粒度有一 tick 竞态窗。

孤儿裁决（grep 实证）：`memory/policy_replay.py` + `replay_corpus.py` = **302 行整链孤儿**（且 CLAUDE.md "activation policy changes must pass replay guard" 是空头治理声明）；`PromotionRouter` 仅测试 import（spec §6 中央路由未上岗，路由纪律分散无裁判）；`memory/retention.py` 孤儿且 `activation.py:66-69` 读一个没人写的 `retention_score`（该评分项永久为 0）；`retrieval_eval.py` 孤儿。`access_log`/`decision_trace`/`action_preflight`/`orchestrator` 均为 LIVE。

### 其他 P1 债

- **`db_legacy_feishu/gateway` 两个数据迁移出生即孤儿**（359 LOC，唯一 importer 是测试；`99f05a07` 引入时就没接 main.py）——若生产有 pre-cutover 会话，迁移从未跑过。先核一次生产数据再 delete-or-wire。
- **`fanout_subagents` 死代码**（`agents/subagent.py:113`）——生产 fanout 走 workflow `fanout_step`→真 `spawn_subagent`；helper 被测试钉着。

---

## 3. 提示词状态 — 单篇达标，舰队失修

10 个 surface 判定：**7 个 benchmark**（extract 提取 prompt 是全库最强文本；HEARTBEAT/DREAM/CONSOLIDATOR 正文；subagent 内置（critic 反合理化超 CC 原版）；HR soul refinement；plan-mode reminder 对 + suggest-only 入口；工具描述大面；动态 reminder 调度器；DR synthesis 论点驱动栈），**3 个 adequate**。缺陷全部是**舰队级机械修**，无需重写任何正文：

1. **P1 — 20 个 SKILL.md 描述写着 "Use when Codex needs…"**——vendor 名 + 身份错认进了每个 agent 的 frozen prefix（经 `agent_context.py:111-129` 渲染进 `## Skills` 目录）。L3 模型平等违例。INFERENCE：Codex 批量起草自指泄漏。
2. **P2 — 节奏数字烂在 6 处**：system.py:50-51 / memory.py:10-11 / memory-guide / HEARTBEAT.md:24 / DREAM.md:33 / auto_dream.py:82 还说 45min/4h，实际 2h/24h（config.py:120）；extract_agent.py 已是 2h —— 同一 assembled prompt 在「treat as facts」标题下自相矛盾。应从 Settings 模板化或删数字。
3. **P2 — save_memory 工具描述 vs 三处 escape-hatch-only 教义打架**（`handlers/memory.py:32` 邀请式 vs memory.py:23-29 / system.py / memory-guide 反模式）。
4. **P2 — 两套 3-failure 规则并存**（tasks.py:17 「架构信号」 vs executing_actions.py:104-110 「能力/诊断缺口」三分支）——同一事件两种教义同时下发。
5. **P2 — 静态 section ~6.2K tokens 固定样板**，同一规则讲 4-6 遍（save_memory×6、load_skill×5、tool_search×4）；CC 同一规则至多两个高度各讲一遍。重复即漂移面（#3 就是已实现的漂移）。
6. **P2 — soul 写权限三种说法**：DREAM.md:128-135 教直接 append；DREAM_CONSOLIDATOR 强制 candidate-only;HEARTBEAT.md 说 control plane 持有终写权——记忆纯净债在提示词层的残留，需统一为 candidate-gate 教义。
7. **P2 — HR heartbeat 克隆是退化 fork**（无决策矩阵/无例子/15 轮 vs 40 轮）——从 canonical 重新生成 + HR overlay。
8. P3：objective 残留 4 处提示词（HEARTBEAT.md:209 等）；heartbeat bootstrap 注入编号断档（1,2,3,6）+ 「(10 failures)」标题 vs `>=5` 门不符；system.py 压缩讲两遍；`set_trigger` schema 还在教已退役的 focus.md 检查单概念（`handlers/triggers.py:45,106`）。

---

## 4. CC 对齐 — 核心已真接线，两个洞集中一个主题

接线级验证**确认为真**（非测试假象）：kernel ReAct loop（200 轮/80% round-pressure/三通道 LoopGuard/75% 主动压缩 + PTL 反应式 + Work Ledger 5 问重启）被全部入口共用（web WS、durable run、8 IM、trigger、heartbeat、delegation、subagent wake、system plan run）；`_LONG_TASK_RE` 真没了，continue_current_session handler 启动时注册、合法兼容 long_task；10 个 IM `_call_agent_llm` 调用点 session_source/channel/裸确认 flag 全部正确（早期 Slack/Discord/Teams 用 feishu 默认值的 bug 已修）；todo 单板真实（track_todo 纯 upsert 零执行、supervision 后端**零引用**、manage_tasks 已从 LLM 面退役）；trigger 三桶活在 daemon、fire = 喂 prompt 进同一 kernel 无平行子系统；subagent 二元 fork + 进化闭环（蒸馏→提名→apply 唯一写入方）在生产 spawn handler 真接线；capability map 启动审计 UNMAPPED=[]；动态 reminder T-G1/G2 全接。

**两个真洞共享一个主题——非 web 路径的 session-source 保真**：① trigger daemon 伪装 web（P1，见上）；② IM 续跑结果不回投（P1，见上）。均为单调用点小 diff，非架构问题。

次要：hooks 15/15 有 emit、**3/15 无订阅者**（PRE_TOOL_USE / POST_TOOL_FAILURE / DELEGATION_START——PRE_TOOL_USE 的 block 语义永久 no-op，治理全在 ToolRuntimeService，属设计而非 bug，但应知情）；前端还带着 supervision 类型/字段/i18n + 监听后端永不 emit 的 `supervision_tick/fire/error` 审计事件（`WorkspaceAuditSection.tsx:23-25`）；`execution_mode` 半改名缝（contracts.py 旧别名回拷 invocation_scope + cache key 仍叫 execution_mode + agent 级 coordinator 开关与 per-invocation scope 两域被 shim 静默合并）。

---

## 5. 北极星可达性评估

**Goal 1（自我进化 agent ≥ hermes-agent）：内核已达标，体感差距来自边缘与可观测性。** 蒸馏五管线（提取/压缩/会话摘要/dream/heartbeat）L1 质量是真 benchmark 级——全输入优先、8192/20K 输出地板、熔断、可观测 fallback，经典压缩违例修复后保持住了。拖后腿的三件事：① 交互边缘机械层（P0-5/P0-6/IM 10 条/正则路由）让用户在**接触面**上感到机械——体感来自边缘不是内核；② dream merge 缺陷是当前唯一让记忆主动变糟的机制；③ 静默吞错让"管线断了"不可见（已发生过一次一天半的事故）。三者都是收敛性的修复，不是重构。**结论：可达，且距离不远。**

**Goal 2（企业级控制中台）：当前承诺不成立，需要一条专门主线。** 多租户隔离是中台的存在理由，而现状是：应用层 WHERE 是唯一防线、RLS 对核心表摆设、key 明文存储且明文回显、存量泄漏已被用户在生产撞见、软删租户秘密永存。这不是 polish 级缺口，是 Goal 2 的地基。好消息：TenantToolConfig 覆盖层设计是对的、文件/Redis/MCP 隔离健康、写路径已修——缺的是存量迁移 + mask + RLS FORCE 一仗，工程量有限但必须当 P0 打。**结论：可达，但租户安全必须立刻成为下一条主线。**

## 6. 建议攻坚顺序

1. **租户安全一揽子（P0，Goal 2 地基）**：存量 key 迁移+清洗（dry-run+确认门）→ 服务端 mask（含 global_config）→ RLS FORCE+补缺失表（或非 owner 连接）→ tool config 加密落盘 → 租户下线清理。
2. **交互边缘去机械化（P0/P1，Goal 1 体感最大单仗）**：砍 `_SCHEDULE_RE` 劫持（prompt guidance 已在岗）→ 裸确认无 plan 时 fall through → IM 历史去 10 条帽 → trigger 传 session_source → IM 续跑结果回投。前四项都是单文件小 diff。
3. **可观测性一揽子 PR**：~10 处核心管线吞错加 WARNING+counter；schema 权威决断（5 列补真 migration + alembic 失败转响）。
4. **记忆收尾**：dream merge 缺陷修复（保护合成 keep 行）→ D2/D8 生产清洗执行（挂账 #7 的实证验收一并做）→ curation hold 聚合告警 + T2 retention。
5. **提示词舰队修**：Codex×20 改中性 → 节奏数字模板化 → save_memory/3-failure/soul 权限三处教义统一 → 静态 section 去重。全机械修。
6. **孤儿与残留裁决**：policy_replay 链 / db_legacy_* / PromotionRouter / retention.py / fanout_subagents / INDEX.md —— 逐个 delete-or-wire；前端 supervision/objective 残留清理；execution_mode 别名缝收口。

## 7. 健康面（校准——这些是真的好）

- 蒸馏内核 L1 质量 benchmark 级且接线为真（见 §5）；extract 提取 prompt 是全库最强文本。
- 写门对新增持久内容**不可绕**（workspace 层硬拒 memory/ 直写；PL4 拒绝；D6 frozen-Mission 门带弃权回退）；可逆性全覆盖（archive.md + lifecycle 边）。
- 文件 API / workspace 路径 / Redis 事件流 / MCP 模型的租户隔离干净；`list_tools` MCP 跨租户已正确防泄。
- supervision 后端退役彻底（零引用、enum 收敛、单 head 防御性迁移）；`build_runtime_prompt` 等此前挂账孤儿确实清掉了；Settings 零幽灵字段；TODO 标记全库仅 1 处。
- hooks 声明式注册全量走通（durable extract queue + 启动重放 + 关闭 drain）；subagent 记忆双向隔离两点均验证成立。
- 工作区未提交改动是连贯的 TDD 配套在途工作（飞书 Base 真分页 + workspace grep 修正——后者还顺手修了"宣称 grep 实为子串匹配"的正确性 bug），建议尽快提交保护。
