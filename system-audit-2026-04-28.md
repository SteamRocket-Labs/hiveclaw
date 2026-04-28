# Hive 系统全面排查报告

**日期**：2026-04-28
**版本**：1.7.0（commit `c52e236`）
**审查者**：Claude Opus 4.7（6 并行 subagent + 主体综合）；Codex 代码复核修订；4 路独立 agent 团队二次复核
**置信度**：核心代码事实高（经 4 路 team 复核 + 主体亲自 grep/Read）；SOTA 对比、cache 命中率、成熟度分数为工程判断，需运行时指标继续验证
**版本**：v3.1 — 4 路 team 复核 + autonomous writeback 补充修订（2026-04-28 晚）
**目标**：评估 Hive 是否符合"具备完善权限控制的、中立的、媲美 Claude Code 和 Hermes 的自进化 agent 框架，具备极佳任务达成效率、token 使用效率、指令理解能力以及工具调用能力（SOTA 级别）"

---

## 0. 执行摘要

**结论：Hive 当前不符合"媲美 Claude Code 和 Hermes 的 SOTA"目标**。

- **架构愿景层面 SOTA 级**：4 层记忆金字塔（T0→T2→T3→soul）超越 Claude Code 单层 CLAUDE.md；双闸 governance（security zone + capability gate）+ 多租户 RLS 超越 Hermes 无权限模型；自演化 daemon + A2A 委托 + HR agent 创建管道在开源框架中独树一帜。
- **工程实现层面 MVP-Beta 阶段**：核心模块"能跑"但"经不起对抗"。

**整体成熟度：55/100（v3.1 复核后修订）**

| 模块 | 评分 | 关键缺陷 |
|------|------|---------|
| 提示词 + 上下文工程 | 45/100 | cache key 包含不必要动态字段；冻前缀体量缺少实测约束 |
| 记忆 + 自我进化 | 58/100 | 抽取 fire-and-forget 无兜底；**auto_dream 写 soul/focus 无锁**（多 worker 部署有真实并发风险） |
| 工具 + 能力 + 技能 | 62/100 | Pack 激活增加动态 prompt 成本；Skill 永不卸载；MCP 仅 stub |
| 权限控制 | 52/100 | tenant_id=None 时 capability gate 完全跳过（**真实 bypass**） |
| A2A 委托 | 58/100 | 无 visited/cycle 检测；子 agent 用 creator 权限；字符串冒泡 |
| 系统级完整性 | 60/100 | heartbeat/dream 耦合 trigger_daemon；**autonomous identity/capability writeback 直调 LLM**（auto_dream.py:371 写 soul + skill_distiller.py:527 写 skill；未走标准 runtime governance/capability gate）；admin router 级 guard 不统一 |

**用户最关心的两个指标差距**：
1. **"完善权限控制"未达成**：`tenant_id=None` capability fail-open、默认 capability mapping fail-open、委托权限令牌缺失等关键路径仍需收口
2. **"极佳 token 使用效率"未证明达成**：cache key 与 prompt 边界存在高 miss 风险，但 `<50%` 命中率目前不是实测结论，需接入生产指标验证

**修复路径**（v3.1 修订后，工作量已校准）：
- 第一波（**3 周**，原估 2 周偏乐观）关 3 个确认 P0 + 2 个高 ROI P1 → 68-70/100
- 第二波（4-5 周）cache 指标化 + 演化解耦 + auto_dream 加锁 → 78-80/100
- 第三波（6.5 周）结构化 + MCP + 委托令牌 + dream/skill_distiller writeback 走标准 governance/audit → 88/100

---

## 1. 评估方法论

### 1.1 6 路并行深度审查

派发 6 个 subagent 各自负责一个原子模块，每个 agent 被严格约束：
- 必须给出 `file:line` 证据，无证据不写
- 按 P0（功能正确性 / 安全漏洞 / 数据丢失）/ P1（架构问题 / 显著优化）/ P2（微调）分级
- 各自做 SOTA 对比（vs Claude Code / Hermes / MemGPT/Letta / CrewAI/AutoGen）
- 给出 0-100 成熟度评分

### 1.2 主体交叉验证

收到 6 份报告后，主体直接读取关键代码文件交叉验证 3 个最严重 P0：
- `main.py:344-350` lifespan 后台任务列表 → 验证 heartbeat/dream daemon 状态
- `governance.py:234-235` + `invoker.py:152/159/182` → 验证权限 bypass 路径
- `orchestrator.py:536` → 验证委托循环检测缺失

**复核修正**：
- 系统 agent 报告 "heartbeat & dream daemon 完全未启动" 是误读。实际通过 `trigger_daemon.py:1443-1491` 间接驱动（每 60s 跑一次 `_heartbeat_tick`，dream 由 heartbeat 内部触发）。但这本身暴露了架构耦合 + 文档/代码频率不一致（CLAUDE.md 声称 45min vs 实际 60s）。
- `Admin 端点零鉴权` 是误判：当前 `backend/app/api/admin.py` 的敏感端点使用函数参数级 `Depends(require_role("platform_admin"))`。问题应改为 **router 级 guard 不统一，靠逐端点维护，存在未来漏加风险**。
- `scenario section 位于 frozen prefix` 是误判：当前 `build_dynamic_prompt_suffix()` 中 `scenario_section` 属于 dynamic suffix。`output_efficiency` 虽仍被调用，但返回空字符串，不构成 token 膨胀来源。

### 1.3 v3 — 4 路独立 team 二次复核（防止再误判）

由于 Codex 一轮反驳已证明部分 subagent 论断不准，v3 加派 4 路独立 agent 团队全面复核每个论断：

| Team | 范围 | 关键发现 |
|------|------|---------|
| Team A | 3 个 P0 深度复核 | 3/3 P0 主体保留；P0-2 有 pattern fallback 缓解 LLM 失败，但 schedule/drain 缺持久队列仍按 P0 保留；A2A 静态确认缺 visited/cycle 检测，A→B→A→B 攻击路径仍需 runtime reproduction |
| Team B | Codex 修订删除项再核 | 4 项修订准确；**soul/focus 锁应收窄后升回 P1**（auto_dream.py:420 写 soul、:1295 清 focus 都无锁；多 worker 部署有真实并发） |
| Team C | 新 P1 论断验证 | 4/4 全部成立；CAPABILITY_MAP 实际 **83 个 direct tool mappings + 2 个 synthetic capability keys**；start_heartbeat 1 处定义零外部调用确认；"45 倍频率差"准确 |
| Team D | 系统集成 + 修复路线 | "6 channel 不走 invoke_agent" 是 **过度论断**——多数 channel webhook 通过 `_call_agent_llm -> websocket.call_llm -> invoke_agent` 间接走；Feishu/websocket/task 创建路径另有 `task_executor.execute_task -> invoke_agent`；**auto_dream 是写 soul 的关键直绕路径**（auto_dream.py:371 直调 `create_llm_client`），但不是全系统唯一 `create_llm_client` 调用方；工具数确认 **80**（Team D 报的 45-60 错误） |

**主体亲自交叉验证**（吸取 Codex 教训，不再轻信 agent grep 结果）：
- 验证 `invoke_agent` 真实直接调用方：`api/websocket.py` / `agents/orchestrator.py` / `services/{task_executor,heartbeat,supervision_reminder}.py`，`runtime/invoker.py` 定义入口。channel router 自身不直接调 `invoke_agent`；Slack/Discord/DingTalk/WeCom/Teams/Telegram 等通过 `_call_agent_llm -> websocket.call_llm -> invoke_agent` 间接走，Feishu/websocket/task 创建路径另有 `task_executor.execute_task -> invoke_agent`，trigger/gateway 也通过 `call_llm` 桥接
- 验证工具总数：`rg -n "^@tool" backend/app/tools/handlers -g "*.py" | wc -l` = **79**；`rg -n "@tool\\(" backend/app -g "*.py" | wc -l` = **80**。Team D "45-60" 错

---

## 2. 模块详细发现

### 2.1 提示词 + 上下文工程

**评分：45/100**（复核修订；原报告对 dynamic/frozen 边界有两处误判）

#### P1：冻结前缀边界与体量约束不足

- **证据**：`backend/app/runtime/prompt_builder.py:73-104`（frozen prefix 组装）+ `:197-201`（cache boundary 插入位置）
- **问题**：
  - 冻前缀包含 `agent_context` + `system` + `tasks` + `tools`，上限依赖各 section 自身预算，缺少直接 token 计量与告警
  - `output_efficiency.py:4-6` 虽已 DEPRECATED 且仍被调用，但当前返回空字符串，不应算作 token 膨胀来源
  - `scenario` section（`prompt_builder.py:161-166`）和 `continuity context`（`:150-157`）当前都在 dynamic suffix，不在 frozen prefix；原报告该点为误判
  - `workspace_signature` 使用 `mtime_ns` + size，文件 touch 即失效（即使内容不变），会污染 prefix cache key
- **影响**：Cache miss 风险高；但 `<50%` 命中率目前不是代码证据，需要生产日志/指标验证
- **修复**：
  - 严格限制冻前缀 <8K token
  - 保持 scenario / continuity / current_user_name 在动态后缀，不纳入 frozen cache key
  - 删除空的 deprecated `output_efficiency` 调用，避免误导
  - 将 workspace signature 改为内容 hash 或显式版本号

#### P1：Cache 中立性声称与实现不符

- **证据**：`backend/app/kernel/engine.py:477-498` `_build_frozen_prompt_cache_key`；commit `c52e236` "Harden prompt cache neutrality and prefix invalidation"
- **问题**：cache key 包含
  - `current_user_name` — 当前不渲染进 frozen prefix，却会让不同用户命中不同 cache key
  - `context_window_tokens` — fallback 模型切换时自动改变
  - `workspace_signature` 通过 `mtime_ns` 计算（`:473-474`），文件 touch 即失效（即使内容不变）
- **对比**：Claude Code 只缓存真正不变的部分（模型架构 + tool schema），用户/上下文每轮重算
- **影响**：trigger / heartbeat 路径理应高复用（同一 agent 反复调用），当前实现会制造不必要 miss；实际命中率需接入指标后确认
- **修复**：cache key 仅保留 `agent_id + tenant_id + workspace_content_hash`（非 mtime）

#### P1：Token 预算分配与压缩策略不匹配

- **证据**：`backend/app/runtime/context_budget.py:9-13`（system prompt 占 20%）+ `engine.py:35,55-62`（90% 触发，50KB tool result eviction，200KB round aggregate）
- **问题**：
  - 90% 触发压缩，目标压缩到 50% — 单次释放 45% 但下一轮立即回到 80%+，导致频繁压缩
  - tool result >200KB/round 强制截断，但未调用 LLM 总结，直接 preview_length 截断（`engine.py:1774-1777`）
  - 后缀动态部分（memory/active_packs/retrieval/continuity）无 hard cap，可能溢出消息历史预算
- **修复**：
  - 阈值改 75% / 目标 60%
  - 后缀各部分加 cap：memory 3000, active_packs 2000, retrieval 3000, continuity 1500
  - tool result >10KB 自动写 artifacts，inline 只留 2KB preview + 引用

#### P1：Active Packs 动态注入增加上下文成本

- **证据**：`backend/app/runtime/prompt_builder.py:168-170` + `engine.py:376-378`
- **问题**：active_packs 在 dynamic suffix 每轮重建，不会污染 frozen prefix cache；但激活多个 pack 后会持续增加动态 prompt 成本，尤其当前会注入工具列表
- **修复**：
  - 方案 A：active_packs 仅含名称和摘要（工具列表移到 API tools 参数）
  - 方案 B：对 active_packs 加预算、TTL 或按轮次裁剪，避免长会话持续膨胀

#### P2：Memory injection 去重不足

- **证据**：`prompt_builder.py:145-157`（memory + continuity）+ `engine.py:357-373`（session_memory + compaction_summary）
- **问题**：memory_snapshot、session continuity、compaction_summary 三个来源可能重叠，无去重
- **修复**：按内容摘要哈希去重，保留最新

#### P2：Triggers section 与 Objective Ledger 耦合不清

- **证据**：commit `1b050b2` "Unify autonomy prompt contracts and objective projection" 后 `triggers.py:6-48`
- **问题**：triggers 含 objective_id 和 trigger_class binding，但 prompt 无明确决策树说明"激活 trigger ≠ 创建 objective"
- **修复**：在 triggers section 加小决策表

#### vs SOTA 对比

| 维度 | Claude Code | Hermes | Hive |
|------|------------|--------|------|
| System prompt 大小 | <8K token | 中等 | 偏大风险，需实测 |
| Tool 表示 | API tools 参数 | function calling schema | prompt 文本 + 部分 tools |
| Cache 命中率 | 80%+ | N/A | 未接入实测；高 miss 风险 |
| Memory 注入 | 单 CLAUDE.md | 静态 | 多源（去重不足） |

---

### 2.2 记忆 + 自我进化

**评分：58/100**（v3 复核：auto_dream 写 soul/focus 无锁升回 P1，原 60 下调 -2）

#### P0：Fire-and-forget 抽取失败无兜底

- **证据**：`backend/app/runtime/hooks_setup.py:88-94` + `extract_agent.py:555-558`
- **机制**：
  - RESPONSE_COMPLETE 钩子调用 `schedule_extract()` 是非阻塞，无异常处理
  - LLM 调用失败会返回 None 并走 pattern fallback，这缓解模型错误，但不覆盖任务创建失败/进程退出
  - `schedule_extract()` 的 `asyncio.create_task()` 路径无 try/catch；资源耗尽时该轮 hot-path extraction 没有持久入队
  - SESSION_CLOSE drain 使用 `asyncio.wait_for(task, timeout=10)`；超时会取消 in-flight extraction，若没有后续 backfill/replay，T2 hot path 会丢
- **影响**：T2 黄金路径（hot path）没有 durable guarantee；T0/backfill 可能后续补回部分信息，但当前链路无法证明可恢复，heartbeat 也无法感知失败
- **修复**：
  - 抽取队列持久化到磁盘（FAILED_EXTRACTIONS 目录）
  - 启动时重放未完成任务
  - 添加失败计数 metrics + 告警阈值

#### P1：heartbeat/dream 调度耦合在 trigger_daemon

- **证据**：`backend/app/services/trigger_daemon.py:1443-1491`
  ```python
  # 每 4 tick (~60s) 执行
  if _heartbeat_counter >= 4:
      from app.services.heartbeat import _heartbeat_tick
      await _heartbeat_tick()
  ```
- **问题**：
  - `start_heartbeat()` 在 `heartbeat.py:1754` 是死代码（grep 无外部调用）
  - 实际 heartbeat 由 `_heartbeat_tick` 驱动，被 trigger_daemon 寄生调度
  - dream 由 heartbeat 内部 `heartbeat.py:1415-1420` 触发 `run_dream`
  - **CLAUDE.md 声称 45min ticks，实际 60s**（差 45 倍）
  - 单点故障：trigger_daemon 崩 = heartbeat + dream + workspace_sync 全停
- **修复**：
  - 拆出独立 `evolution_daemon` 承载 heartbeat / dream / workspace_sync
  - 频率配置化（生产 45min，dev 60s）
  - CLAUDE.md 与代码二选一对齐

#### P1：soul.md + focus.md 写入权未明确边界

- **证据**：
  - `auto_dream.py` 写 soul.md
  - `heartbeat.py` + agent kernel + objective service 都可能写 focus.md
  - 无显式锁；focus cleanup（`auto_dream.py` B8 fix）基于启发式，非原子
- **影响**：并发修改 focus.md 可能行丢失；soul.md 一致性依赖 LLM 质量，无事务保证
- **修复**：
  - 引入基于 session_id 的乐观锁或文件级 flock
  - 限制 focus 单一 owner（建议 objective service）

#### P1：Artifact 引用解析脆弱

- **证据**：`extract_agent.py:653-674`
- **问题**：通过正则解析 `[artifact: ...]` 从兄弟文件读取；artifact 移动/删除只记 warning 返回 preview；无重试或校验
- **影响**：>8000 chars 的 tool result 在 backfill 时可能丢失完整上下文
- **修复**：T2 条目嵌入 artifact hash 校验；backfill 失败入队重试

#### P2：Hindsight 同步策略不明确

- **证据**：`memory/backends/hindsight.py` + `memory/hindsight_sync.py`
- **问题**：opt-in per-tenant 但同步触发点不清；rerank 需明确 `model_config` 否则降级；无 sync daemon
- **影响**：可能返回 stale 数据
- **修复**：明确 sync 触发点（heartbeat T2→T3 后？dream 后？）+ 添加 cursor tracking

#### P2：T0 完整性监控缺失

- **证据**：6 个 channel（WebSocket/Feishu/Slack/DingTalk/WeChat/Teams）+ Trigger + Heartbeat + Delegation 都依赖 hook 写 T0
- **问题**：若某 channel 的 hook 没注册或返回值处理错误，无 alerts
- **修复**：每个 source 加 T0 write 成功/失败 metrics；heartbeat 周期扫描日期目录核对

#### vs SOTA 对比

| 维度 | Claude Code | MemGPT/Letta | Hive |
|------|------------|--------------|------|
| 持久记忆 | 单 CLAUDE.md | 分层 archival/recall | **4 层金字塔** ✓ |
| 抽取机制 | 无 | 同步 retrieval | LLM hot path + pattern fallback |
| 错误处理 | 同步抛出 | 异步 retry 队列 | **fire-and-forget 吞掉** |
| 自演化 | 无 | 定期 compact | heartbeat + dream（耦合调度）|
| 并发安全 | N/A | Redis 分布式锁 + SQLite ACID | cursor 计数器，**无锁** |

**优势**：4 层架构 + LLM 抽取 + artifact spillover + cursor 幂等性
**差距**：无真正主动 daemon；soul.md 一致性无强事务；无演化质量指标

---

### 2.3 工具 + 能力 + 技能

**评分：62/100**

#### P1：工具注册一致性有幽灵 bug 风险

- **证据**：`backend/app/tools/collector.py:103-150` auto-discovery + `packs.py:19-94` TOOL_PACKS 硬编码
- **问题**：
  - 若 handler 模块导入失败，工具被静默跳过（L94-100）
  - TOOL_PACKS 是硬编码，可能引用不存在的工具
  - 反向也可能：registry 有但 prompt 漏
- **影响**：prompt 声明 web_pack 但 runtime 缺工具 → LLM 调用失败
- **修复**：collect 后与 TOOL_PACKS 交叉验证，或 packs 从 collected_tools 推导

#### P1：Pack 激活增加动态 prompt 成本

- **证据**：`backend/app/runtime/session.py:14` + `prompt_sections/active_packs.py:20-37` + `engine.py:513`
- **问题**：active_packs 位于 dynamic suffix，变化时不会污染 frozen prefix cache，这是正确方向；但当前 active_packs 会把工具列表写进 prompt，长会话中可能持续增加动态上下文成本
- **修复**：active_packs 仅保留 pack 名称、摘要和必要使用提示；详细工具 schema 迁到 API tools 参数或按需展开

#### P1：Skill 渐进加载无卸载机制

- **证据**：`backend/app/skills/loader.py` + `session.py:24` active_skills 只追加
- **影响**：长会话中加载多个大 skill → 上下文永不释放 → token 浪费
- **修复**：lifecycle 管理（refcount / 显式 unload / TTL）；compaction 时清理未用 skill

#### P2：超时和重试策略不对称

- **证据**：`backend/app/tools/service.py:93-106`（硬编码超时：web_fetch 60s, run_command 120s）+ L152-159（timeout 标 retryable=True 但不自动重试）
- **影响**：网络波动易反复失败；governance 超时可能留部分审计状态
- **修复**：service.py 实现指数退避（2^n up to max_retries）；governance 超时原子化

#### P2：MCP 集成仅 metadata 层

- **证据**：`backend/app/tools/handlers/mcp.py:23-61`（list_mcp_resources 仅 DB 查询）+ `:170-172`（import_mcp_server 委托给 web_mcp）
- **问题**：无真实 MCP client；不支持 stdio/SSE/HTTP transport；只能列举已导入工具元数据
- **影响**：CLAUDE.md 文档声称的 MCP 集成是 stub
- **修复**：引入 mcp-python SDK 实现 ClientSession，handlers/mcp.py 添加真实 RPC 调用

#### P2：工具数量与可组合性失衡

- **证据**：grep `@tool` 注册约 **80 个**；filesystem 9 个（read/write/edit/glob/grep/delete/list/execute/read_document）
- **问题**：edit_file vs write_file 语义重叠；细粒度工具增加 LLM 选择负担
- **对比**：Claude Code ~7 核心工具（read/write/edit/bash/grep）；bash 万能
- **修复**：聚合 filesystem 9→3（read/update/list + mode 参数）

#### P2：工作区初始化并发竞速

- **证据**：`backend/app/tools/workspace.py:265-268` `open(..., "x")` 原子，但 `ensure_workspace()` 非事务
- **问题**：极低概率 TOCTOU bug：两线程同创 enterprise_dir → write 互覆
- **修复**：分布式锁（Redis）或 DB 事务包裹

#### vs SOTA 对比

| 维度 | Claude Code | Hermes | Hive |
|------|------------|--------|------|
| 工具数 | ~7 + bash | structured schema | **约 80 个**（细粒度） |
| Schema 质量 | 极简精确 | 强类型验证 | 中等（描述详尽但分散） |
| Result envelope | 统一 ToolResult | structured | 统一 ✓ |
| 错误处理 | 同步抛出 | 强类型重试 | governance + envelope 分离（流程复杂） |
| MCP 支持 | 原生 client | 无 | **metadata 层 stub** |

---

### 2.4 权限控制（用户最关心）

**评分：52/100**（复核修订；保留真实 fail-open，修正若干过度表述）

#### P0：tenant_id 缺失时 capability gate 完全跳过（security zone 仍会执行）

- **证据**：
  - `backend/app/tools/governance.py:234-235`
    ```python
    if not context.tenant_id:
        logger.info("[Governance] No tenant_id — skipping capability checks for tool %s", context.tool_name)
    ```
  - `backend/app/tools/governance.py:237-298` 所有 capability 逻辑包在 `if context.tenant_id:` 块内
  - `backend/app/runtime/invoker.py:152, 159, 182` 三处 fallback：
    ```python
    return RuntimeConfig(tenant_id=None, max_tool_rounds=200)  # 缺 agent_id
    return RuntimeConfig(tenant_id=None, max_tool_rounds=200)  # agent 不存在
    return RuntimeConfig(tenant_id=None, max_tool_rounds=200)  # DB 异常
    ```
- **机制**：`_resolve_runtime_config` 在 agent_id 缺失、agent 不存在或 DB 抖动时返回 `tenant_id=None`；governance 见 None 跳过整个 capability check
- **影响**：**真实安全漏洞**。需要精确定性：这不是完全 unrestricted，因为 security zone 检查与 dangerous command 检测仍会执行；但依赖 tenant capability policy 的工具会绕过 capability gate，DB 闪断/agent 解析失败时会破坏 fail-closed 预期
- **修复**：fail-closed
  - governance.py:234：`if not context.tenant_id: return "🔒 Tool blocked — no tenant context (fail-closed)"`
  - invoker.py:152/159/182：抛 `RuntimeError` 而非 None 兜底

#### P1：默认 capability mapping fail-open

- **证据**：`capability_gate.py:159-162`
  ```python
  capability = _resolve_capability(tool_name)
  if not capability:
      return CapabilityCheckResult(allowed=True)  # ← fail-open
  ```
- **问题**：83 行映射覆盖主要工具，但
  - 新工具忘记加映射 = 自动允许
  - commit `f2a9555` "Clarify safety boundary labels and fill capability mappings" 表示之前有遗漏
- **修复**：未映射工具默认拒绝；启动时强制全工具映射核查（v3 实测：CAPABILITY_MAP **83 个 direct tool mappings** + **2 个 synthetic capability keys**；`@tool` 装饰器约 **80 个**，但仍是 fail-open 设计，需改 fail-closed）

#### P1：ExecutionIdentity 委托链权限传递未定义

- **证据**：`backend/app/core/execution_context.py:16-22` + `governance_resolver.py:62-88`
- **问题**：
  - A→B→C 委托链：C 用谁的权限？代码未明确处理
  - `approval_service.py:70-86` `_request_approval` 只取 user_id，不检查跨租户
  - 审批后 `execute_approved` 无"批准者权限 AND 代理基础权限"的交集检查
- **修复**：
  - 明确委托链规则（建议单层，多层需特批）
  - 审批时加权限交集校验

#### P1：多租户 RLS BYPASS 路径风险

- **证据**：`add_row_level_security.py:37-45`
  ```sql
  CREATE POLICY tenant_isolation_{table} ON {table}
      USING (
          current_setting('app.current_tenant_id', true) = 'BYPASS'
          OR tenant_id::text = current_setting('app.current_tenant_id', true)
          OR tenant_id IS NULL
      );
  ```
- **问题**：
  - 'BYPASS' 模式的设置权限谁控制？
  - ORM 层 / 连接池未正确隔离会话变量时，租户查询可能污染全局
  - `tenant_id IS NULL` 行对所有租户可见 — 哪些表会有 NULL？
- **修复**：
  - 验证 'BYPASS' 仅 platform_admin 中间件可设
  - 改用 PostgreSQL 角色级隔离更稳
  - 审计哪些表行有 NULL tenant_id

#### P2：Capability check 类型契约不够强

- **证据**：`governance.py:241-244`
  ```python
  if cap_result is not None and not hasattr(cap_result, "denied"):
      logger.warning("[Governance] Unexpected capability result type: %s — blocking (fail-closed)", type(cap_result))
  ```
- **问题**：当前 unexpected type 会 block，不是 fail-open；但仍靠鸭式类型检查，契约弱，未来改动容易误接
- **修复**：强类型 `CapabilityCheckResult` 注解 + 单元测试覆盖 unexpected type fail-closed

#### P2：Trigger / Heartbeat 执行身份完整性

- **证据**：`trigger_daemon.py` 设置 `set_agent_bot_identity(agent_id, agent.name, source="trigger")`
- **问题**：
  - 谁可以创建/修改 trigger？租户校验在哪？
  - `trigger_preflight.py` / `trigger_reconciler.py` 未见明确租户检查
- **修复**：trigger CRUD 加租户隔离检查；执行时二次验证 agent.tenant_id

#### 安全风险扫描

**Bypass 路径**：
1. `_execute_without_governance()` 用于 `execute_approved()` 和 `execute_direct()` — 设计合理（approval 即权限决策），但需确保审批本身无漏洞
2. **`tenant_id` 缺失时跳过 capability**（最严重）
3. 未发现 hardcoded credentials 或 master_key 泄露（secrets_provider 使用 HKDF 派生）

**多租户隔离**：
- JWT 含 `tid`，TenantMiddleware 解析后设会话变量
- platform_admin 可通过 `X-Tenant-Id` 头覆盖（`security.py:112-129`）；当前已做 UUID 格式、tenant 存在性、active 状态校验。剩余风险是：平台管理员跨租户切换策略是否需要更细审计

**Approval 流程**：
- `approval_service.py:72-73` 检查"agent creator 或 platform_admin 能批准"，**但未检查批准者与 agent 同租户**
- 可能 cross-tenant approval

#### vs SOTA 对比

| 维度 | Claude Code | 企业 SaaS（Replit/Cursor） | Hive |
|------|------------|---------------------------|------|
| 权限模型 | permission mode（plan/auto/bypass） | RBAC + RLS + per-resource | 多租户 + 双闸 + 审批（架构 SOTA） |
| 隔离机制 | 无（本地） | tenant + member + workspace | RLS + 会话变量 |
| Fail strategy | 用户确认 | fail-closed | **fail-open（tenant=None 时）** |
| 审批 | 无 | 完整 RBAC 审批 | 有但跨租户校验缺 |
| 审计 | 无 | 完整 audit trail | execution_identity 记录完整 |

**结论**：架构上更完善（企业级隔离），但实现有真实漏洞，削弱了优势。

---

### 2.5 A2A 委托

**评分：58/100**（v3 复核：静态确认缺 visited/cycle 检测，但 depth=4 攻击链仍需 runtime reproduction）

#### P0：委托链缺循环检测

- **证据**：`backend/app/agents/orchestrator.py:536`
  ```python
  if request.depth > request.policy.max_depth:  # max_depth = 2
      return AgentDelegationResult(...failed=True)
  ```
- **问题**：仅 depth limit，无 visited agent_id 检测
- **机制**：A 委托 B，B 通过 messaging tool 触发 A（绕过 delegation tool 黑名单）→ A→B→A 循环
- **影响**：tenant 内被攻陷的 agent 可启动放大式资源消耗
- **修复**：trace_id 上挂 visited agent_id set，循环时 fail-fast

#### P1：同步 vs 异步委托语义模糊

- **证据**：
  - `orchestrator.py:492` `delegate_to_agent()` 同步阻塞返回完整结果
  - `orchestrator.py:779` `delegate_async()` 非阻塞返回 task_id
  - `communication.py:192` 工具层暴露 `delegate_to_agent`，**内部却调 `_delegate_to_agent_async`（异步版本）**
- **问题**：对 LLM 而言，调用 `delegate_to_agent` 等价 fire-and-forget，需轮询 `check_async_task`；与 Claude Code 的 Task tool 真同步语义不同
- **修复**：暴露显式 `delegate_blocking()` 工具用于同步场景；prompt 明确异步语义

#### P1：子 agent 权限继承机制不严

- **证据**：
  - `messaging.py:1114` `owner_id=source_agent.creator_id`
  - `orchestrator.py:622` `user_id=request.owner_id`（creator 而非执行者）
  - `orchestrator.py:613-617` `SessionContext(source="agent")` 无明确权限隔离标记
  - 租户强制一致 ✓（`messaging.py:133`），无跨租户检查
- **问题**：子 agent 用父 creator 权限，无受限"委托令牌"，攻陷子 agent = 攻陷创建者权限
- **修复**：引入临时 delegation_token（限范围 + TTL）；core_tools_only 改为白名单

#### P1：记忆写入租户隔离不完整

- **证据**：`orchestrator.py:611-612` `memory_messages=[]` + `memory_session_id=child_session_id`
- **问题**：子 agent 记忆写入 `logs/{child_agent_id}/logs/`，父无法审计；若含敏感信息（API key / 用户数据）可能泄露
- **修复**：内存记录到父 agent 工作空间，或受委托令牌限制的临时 session

#### P2：失败冒泡不够精细

- **证据**：`orchestrator.py:645-675`
- **问题**：超时/异常/深度限制都返回纯文本字符串，无结构化错误码
- **对比**：Claude Code subagent 返回 `ToolResult(is_error=True, content=...)` 可精确解析
- **修复**：返回 JSON `{"status": "failed", "reason": "timeout|permission|cycle|...", "detail": ""}`

#### Plaza / HR agent 创建归属

- **Plaza 不算 A2A**：仅提供社交源流（`plaza_get_new_posts`/`plaza_create_post`），无直接 agent-to-agent 调用
- **HR agent 不走委托管道**：`create_digital_employee()`（`hr.py:1050`）直接创建 Agent 记录，正确独立

#### vs SOTA 对比

| 维度 | Claude Code | AutoGen/CrewAI | Hive |
|------|------------|----------------|------|
| 委托模式 | Task tool（同步） | Message-passing（异步） | Hybrid（伪异步） |
| 结果返回 | ToolResult 对象 | 消息队列 + 回调 | 字符串 + task_id 轮询 |
| 嵌套防护 | 深度 + 文档禁止 | 角色隔离 | max_depth + core_tools_only 黑名单 |
| 权限传递 | 独立 context | 共享全局 | 继承 creator_id（**无 token**） |
| 记忆隔离 | 独立 context | 共享全局 | 独立 session_id（无父审计） |
| 失败处理 | ToolResult(is_error=True) | 异常 + 重试策略 | 字符串警告（**无错误分类**） |

**优势**：多种工具策略（worker_safe/memory_readonly/review_readonly）、租户强制、DELEGATION_START/END 钩子完整
**劣势**：字符串冒泡、伪异步、缺循环检测、缺权限令牌

---

### 2.6 系统级完整性 + SOTA 差距

**评分：60/100**（v3 复核：去除 admin 零鉴权误判，保留调度耦合与 auto_dream identity-writeback 直调 LLM）

#### 修正：Heartbeat / Dream daemon 不是死代码

- **Agent 原报告**：P0 — `start_heartbeat()` 在 `heartbeat.py:1754` 定义但从未调用；`auto_dream` 无 daemon
- **主体验证结果**：grep 确认 `start_heartbeat` 确实是死代码，但实际链路通过另一路径：

  ```
  main.py:344-350 lifespan
    → start_trigger_daemon()  (trigger_daemon.py:1443)
        → 15s tick + 每 4 tick (~60s) 跑 _heartbeat_tick (line 1476-1477)
        → heartbeat 内部 line 1415-1420 触发 run_dream
        → 同时启动 _workspace_sync_loop / _workspace_full_sweep_loop
  ```

- **修正后定性**：从 P0（死代码）降为 **P1（架构耦合）**
  - 单点故障：trigger_daemon 崩 = heartbeat + dream + workspace_sync 全停
  - 文档/代码不一致：CLAUDE.md 声称 45min ticks，实际 60s（差 45 倍）
  - 命名误导：trigger_daemon 实际承载 4 种 unrelated 调度

#### 修正：Admin 端点不是零鉴权；问题是 router 级 guard 不统一

- **原报告证据不足**：`grep -rn "dependencies=\[Depends" backend/app/api/admin*.py` → 0 命中，只能说明没有 router decorator 级 `dependencies` 参数，不能证明端点无鉴权
- **复核证据**：`backend/app/api/admin.py` 多个敏感端点通过函数参数注入 `Depends(require_role("platform_admin"))`，包括 `/autonomous-audit`、`/harness-validation`、`/autonomy-repair-plan`、`/autonomy-repair-plan/apply`、`/harness-canary/run`
- **修正后定性**：从 P0 降为 **P2/P1 hygiene**。建议加 router 级 platform_admin guard 或新增测试，防止未来新增 admin 端点漏写权限依赖

#### P1：上下文管理 4 套并存

- **证据**：
  - `runtime/context.py` — RuntimeContext dataclass（21 行极简）
  - `runtime/context_engine.py` — ContextEngine protocol + DefaultContextEngine
  - `runtime/context_budget.py` — ContextBudget + TurnModelRoute（274 行）
  - `runtime/coordinator.py` — Coordinator mode dispatch（180+ 行）
- **问题**：4 个独立文件管理不同方面，无统一接口；invoker.py 同时导入 context_engine + context_budget 但职责未分离
- **修复**：收敛为单一 RuntimeContext（含 budget 和 engine 作为 composition）

#### P1：孤儿 service

- **证据**：grep 验证以下文件 0 references
  - `services/supervision_reminder.py`
  - `services/template_seeder.py`
- **修复**：确认是否需要删除或在 lifespan 激活

#### P2：冗余的 Context 实现

- **证据**：`prompt_eval.py` + `task_eval.py` 两套独立 prompt contract 检查系统未集成
- **修复**：合并为统一的 evaluation framework

#### Entry Point 矩阵核查（v3 修订：明确直接 vs 间接路径）

| 入口 | 直接调 invoke_agent | 间接路径 | 有 governance | 备注 |
|------|--------------------|---------|--------------|------|
| WebSocket chat | ✓ `api/websocket.py` | - | ✓ | 主交互路径 |
| Trigger executor | ✗ | `trigger_daemon.py -> websocket.call_llm -> invoke_agent` | ✓ | 定时任务，不直接调 `invoke_agent` |
| Heartbeat tick | ✓ `services/heartbeat.py` | - | ✓ | 60s 一次 |
| Delegation | ✓ `agents/orchestrator.py` | - | ✓ | A2A 委托 |
| Task executor | ✓ `services/task_executor.py:341` | - | ✓ | 通用任务 |
| 主要 channel webhook（Slack/Discord/DingTalk/WeCom/Teams/Telegram 等） | ✗ | `_call_agent_llm -> websocket.call_llm -> invoke_agent` | ✓ | router 本身不直接调 `invoke_agent`；通过 shared `call_llm` 桥接 |
| Feishu / WebSocket task 创建路径 | ✗ | `task_executor.execute_task -> invoke_agent` | ✓ | `feishu.py:1654`、`websocket.py:917` 创建后台 task |
| HR agent creation | ✓ | - | ✓ | 创建 + 引导 |
| messaging tool（agent 间） | ✗ | `messaging.py -> delegate_async -> orchestrator.invoke_agent` | ✓ | A2A 消息 |
| Gateway API | ✗ | `gateway.py -> websocket.call_llm -> invoke_agent` | ✓ | gateway 桥接路径 |
| **Dream consolidation** | ✗ | **直接 `create_llm_client`，无桥接** | ✗ **关键断点** | `auto_dream.py:371` 写 soul.md（agent identity 冻前缀） |
| **Skill distiller** | ✗ | **直接 `create_llm_client`，无桥接** | ✗ **关键断点** | `skill_distiller.py:527` 输出 `promote/patch/defer/reject` 决策 + `instructions_markdown` + `declared_tools/packs`，写 agent 持久能力；promotion 后有 evolution ledger，但缺预写入治理 |

**关键断点（v3.1 扩展）**：在会改写 **agent identity/capability 的 autonomous writeback** 路径里有 2 个直绕 invoke_agent 的 LLM 调用：
1. **`auto_dream.py:371`** — 写 soul.md（身份冻前缀），缓解：tenant_id 缺失时返回 None 走 md-only 路径（line 340-341）
2. **`skill_distiller.py:527`** — LLM 决定是否 `promote/patch/defer/reject` 一个 skill，并生成 skill markdown + 声明 tools/packs，影响后续 agent 能力扩展

两者**同属 autonomous LLM 写持久 agent 状态**：都未走标准 `invoke_agent` runtime governance，也未经过 capability gate。区别是 dream 写身份，distiller 写能力；`skill_distiller` promotion 后会写 evolution ledger，但它不是预写入审批，也不是统一 DB audit。

**对比的内部辅助 LLM 调用**（**不算关键断点**，因为不写持久 agent 状态）：
- `memory/retriever.py:156` — 检索 rerank
- `services/session_recall.py:365` — 会话回忆总结
- `services/conversation_summarizer.py:562` — 上下文压缩
- `services/extract_agent.py:413` — T0→T2 抽取（写学习数据，但属于 hot path 既定流程）
- `runtime/invoker.py:691` — invoke_agent 内部正常路径（**不是绕过**）
- `api/enterprise.py:89` — admin LLM 连通性测试（用户主动触发，非 autonomous；端点 `test_llm_model` 使用函数参数级 `Depends(get_current_admin)`，限 `platform_admin` / `org_admin`）

**修复路线扩展**："Dream consolidation 走 invoke_agent" + "**Skill distiller 走 invoke_agent**"（第三波）

**v2 误报修正**：原 v2 矩阵把 6 个 channel 标"✓ 走 invoke_agent"是模糊表述。v3 明确：channel router 自身不直接调 invoke_agent；多数 webhook 通过 `_call_agent_llm -> websocket.call_llm -> invoke_agent` 间接走，Feishu/WebSocket 的 task 创建路径另通过 `task_executor.execute_task -> invoke_agent`，governance 仍生效。

---

## 3. P0 / P1 总清单

### 🔥 P0（确认必须修，1-2 周）

| # | 模块 | 问题 | 证据 | 工作量 |
|---|------|------|------|-------|
| P0-1 | 权限 | tenant_id=None 时 capability gate 完全跳过 | `governance.py:234-235` + `invoker.py:152/159/182` | 4 小时 |
| P0-2 | 记忆 | RESPONSE_COMPLETE fire-and-forget 无兜底 | `hooks_setup.py:88-94` + `extract_agent.py:555-558` | 2 天 |
| P0-3 | A2A | 委托链无循环检测 | `orchestrator.py:536` | 1 天 |

### ⚙️ P1（架构级，2-4 周）

| # | 模块 | 问题 | 证据 |
|---|------|------|------|
| P1-1 | 提示词 | 冻结前缀 cache key 不中立，包含不必要动态字段 | `engine.py:477-498` |
| P1-2 | 提示词 | Token 预算策略保守，频繁压缩风险需指标验证 | `engine.py:35,55-62` |
| P1-3 | 提示词 | Active Packs 动态注入增加 prompt 重组成本 | `prompt_builder.py:168-170` + `engine.py:513` |
| P1-4 | 记忆 | heartbeat/dream 耦合 trigger_daemon | `trigger_daemon.py:1443-1491`；CLAUDE.md 声称 45min vs 实际 60s |
| P1-5 | 记忆 | **auto_dream 写 soul.md / focus.md 无锁**（多 worker 并发风险）；heartbeat evolution files 已有 flock 覆盖 | `auto_dream.py:420` (soul `_upsert_soul_section`) + `auto_dream.py:1295` (focus `_cleanup_focus_md`) |
| P1-6 | 记忆 | Artifact 引用解析脆弱 | `extract_agent.py:653-674` |
| P1-7 | 工具 | 工具注册一致性幽灵 bug | `collector.py:103-150` + `packs.py:19-94` |
| P1-8 | 工具 | Skill 永不卸载 | `session.py:24` |
| P1-9 | 权限 | Capability mapping fail-open | `capability_gate.py:159-162` |
| P1-10 | 权限 | ExecutionIdentity 委托链权限传递未定义 | `execution_context.py:16-22` |
| P1-11 | 权限 | RLS BYPASS / tenant_id IS NULL 可见性需数据审计 | `add_row_level_security.py:37-45` |
| P1-12 | A2A | 同步 vs 异步语义模糊 | `orchestrator.py:492, 779` + `communication.py:192` |
| P1-13 | A2A | 子 agent 用 creator_id 权限 | `orchestrator.py:622` + `messaging.py:1114` |
| P1-14 | A2A | 记忆隔离不完整 | `orchestrator.py:611-612` |
| P1-15 | 系统 | 上下文 4 套并存 | `context.py / context_engine.py / context_budget.py / coordinator.py` |
| P1-16 | 系统 | 孤儿 service | `supervision_reminder.py` / `template_seeder.py` |
| P1-17 | 系统 | **Autonomous identity/capability writeback 直调 LLM 未走标准 governance** | `auto_dream.py:371`（写 soul）+ `skill_distiller.py:527`（写 skill 持久能力 + 决策 promote/patch/defer/reject；promotion 后有 evolution ledger，但缺预写入审批/统一 DB audit） |

### 🔧 P2（微调，可推迟）

| # | 模块 | 问题 |
|---|------|------|
| P2-1 | 提示词 | Memory injection 去重不足 |
| P2-2 | 提示词 | Triggers section 与 Objective Ledger 耦合不清 |
| P2-3 | 记忆 | Hindsight 同步策略不明确 |
| P2-4 | 记忆 | T0 完整性监控缺失 |
| P2-5 | 工具 | 超时和重试策略不对称 |
| P2-6 | 工具 | MCP 仅 metadata 层 |
| P2-7 | 工具 | 工具数量约 80 个偏多，可组合性弱 |
| P2-8 | 工具 | Workspace 初始化并发竞速 |
| P2-9 | 权限 | Capability check 结果验证脆弱 |
| P2-10 | 权限 | Trigger / Heartbeat 执行身份租户校验缺 |
| P2-11 | A2A | 失败冒泡用字符串 |
| P2-12 | 系统 | Admin router 缺少统一 platform_admin guard，当前靠逐端点 Depends 维护 |

---

## 4. vs SOTA 综合对比

| 维度 | Claude Code | Hermes | Letta/MemGPT | Hive 现状 | 差距 |
|------|------------|--------|--------------|-----------|------|
| **统一内核** | invoke_global | HermesAI.run | AgentRunner | invoke_agent ✓ | 持平 |
| **System prompt 大小** | <8K | 中等 | <10K | 偏大风险，缺少 token 计量 guardrail | 待量化 |
| **Cache 命中率** | 80%+ | N/A | N/A | 未接入实测；cache key 有高 miss 风险 | 待量化 |
| **工具数 / 设计哲学** | ~7 + bash 万能 | structured schema | <20 | **约 80（细粒度）** | 偏离 |
| **MCP 支持** | 原生 client | 无 | 无 | **stub** | 落后 |
| **持久记忆** | 单 CLAUDE.md | 静态 | 分层 archival/recall | **4 层金字塔** | **超前** |
| **自演化** | 无 | 无 | 定期 compact | heartbeat + dream | **超前** |
| **抽取链路可靠性** | N/A（无） | N/A | 同步 + retry | **fire-and-forget 吞掉** | 落后 |
| **A2A 委托** | Task tool（同步 + ToolResult） | 无 | 无 | 混合 + 黑名单 | 部分落后 |
| **委托循环检测** | 文档禁止 | N/A | N/A | **无** | 落后 |
| **权限模型** | permission mode | 无 | 无 | 多租户 + 双闸 + 审批（架构 SOTA） | **架构超前** |
| **权限实现** | 用户确认 | N/A | N/A | **fail-open 漏洞** | 落后 |
| **多租户** | 无 | 无 | 无 | RLS + JWT + middleware | **超前** |
| **审计** | 无 | N/A | N/A | execution_identity 完整 | 持平/超前 |
| **Recovery manifest** | N/A | N/A | N/A | 定义但未集成 | 落后 |

### 当前定位

Hive 在 **架构愿景**（多租户 + 4 层记忆 + 双闸 governance + 自演化 + A2A）上**接近或超越** SOTA，但在 **工程实现成熟度** 上落后 1-2 个迭代周期。

| 类别 | 状态 |
|------|------|
| 架构野心 | **SOTA 级**（部分维度领先） |
| 实现成熟度 | **MVP-Beta**（关键路径有真实漏洞，部分原报告问题需降级） |
| 生产就绪度 | **不足**（3 个确认 P0 + 多个高优 P1） |

---

## 5. 修复路线（ROI 排序）

### 🔥 第一波：关确认 P0 + 高 ROI P1（**3 周**，v3 工作量校准）

**目标**：让"完善权限控制"和"基本可靠性"达成

| 任务 | 文件 | v2 估算 | **v3 校准**（含测试 + 集成验证） |
|------|------|--------|-------|
| P0-1 governance fail-closed | `tools/governance.py:234` | 4h | **6-8h**（含回归测试） |
| P0-1 invoker tenant_id 抛异常 | `runtime/invoker.py:152, 159, 182` | 4h | **6h** |
| P0-2 抽取队列持久化 | `runtime/hooks_setup.py:88-94` + 新增 retry queue | 2 天 | **2.5-3 天**（重放逻辑 + 测试） |
| P0-3 委托循环检测 | `agents/orchestrator.py:536` + trace_id visited set | 1 天 | **1.5 天**（trace_id 传递链改签名） |
| P1-1 cache key 重构 | `kernel/engine.py:477-498` | 2 天 | **2.5-3 天**（workspace_signature 改 hash + 上游对账） |
| P1-1 冻结前缀 token 计量与边界整理 | `runtime/prompt_builder.py:73-104` | 1 天 | **1 天**（不变） |
| P2-12 admin guard 一致性测试 / router 级 guard | `api/admin.py` | 0.5-1 天 | **1 天**（单元测试覆盖新增端点） |
| 集成测试 + 生产兼容性验证 | 新增 P0 回归测试 + 旧数据兼容 | 2 天 | **3 天**（v2 低估） |

**v3 实际工作量**：约 **3 周**（v2 的 2 周偏乐观，未充分计入测试 + 部署验证）
**预期**：55 → 68-70 分

### ⚙️ 第二波：cache + 演化解耦（4 周）

| 任务 | 文件 | 工作量 |
|------|------|-------|
| 冻结前缀严格 <8K token | `prompt_builder.py` 各 section + 删除空的 `output_efficiency` 调用 | 3 天 |
| Token 预算 cap | `prompt_builder.py:145-170` 各后缀 hard cap | 2 天 |
| 压缩阈值调整 75%/60% | `kernel/engine.py:35` | 1 天 |
| 拆 evolution_daemon | 新增 `services/evolution_daemon.py` + 从 trigger_daemon 迁出 | 4 天 |
| heartbeat 频率配置化 | `services/heartbeat.py` + settings | 2 天 |
| CLAUDE.md 更新对齐 | 文档 | 2h |
| Pack 激活指标化 / 缩减 active_packs prompt 体量 | `prompt_sections/active_packs.py` + `session.py:14` | 2 天 |
| Skill lifecycle | `skills/loader.py` + `session.py:24` | 3 天 |
| capability mapping fail-closed | `capability_gate.py:159-162` | 1 天 |
| 启动时全工具映射对账 | `tools/collector.py` + `packs.py` 交叉验证 | 1 天 |
| soul/focus 写锁（升 P1） | `auto_dream.py:420` (soul) + `auto_dream.py:1295` (focus)；heartbeat evolution files 已有 flock | 2-3 天（含多 worker 测试） |

**预期**：68-70 → 78-80 分

### 🚀 第三波：结构化 + MCP + 委托令牌（6.5 周）

| 任务 | 工作量 |
|------|-------|
| 委托令牌机制（限范围 + TTL） | 1 周 |
| 委托结果改结构化 JSON | 3 天 |
| 委托链权限传递规则定义 | 3 天 |
| MCP 真实 client 集成（mcp-python SDK） | 1.5 周 |
| 上下文 4 件套收敛为单 RuntimeContext | 1 周 |
| 工具聚合（filesystem 9→3） | 4 天 |
| RLS BYPASS 路径加固 | 3 天 |
| Hindsight sync 策略明确 | 3 天 |
| Recovery manifest 真正集成 | 1 周 |
| Dream consolidation 走 invoke_agent | 4 天 |
| Skill distiller 走 invoke_agent / 加预写入审批 + DB audit | 3 天（同性质：autonomous capability writeback；保留 evolution ledger 作为追溯材料） |

**预期**：80 → 88 分

---

## 6. 关键 ROI 推荐

如果只能挑 **1 件事** 做：**P0-1 governance fail-closed**。

- 它直接影响"完善权限控制"承诺，而且修复面清晰：`governance.py` 无 tenant context 时 fail-closed，`invoker.py` 不再用 `tenant_id=None` 兜底
- 同时补一个回归测试：模拟 `_resolve_runtime_config` 返回 `tenant_id=None`，确认工具被阻断

如果有 **2 周**：清完 3 个确认 P0，并把 cache key / admin guard consistency 两个高 ROI 问题一起收口，达 68-70/100 分。

如果有 **3 个月（3 个 sprint）**：完成全部 17 个 P1 + 关键 P2，达 88/100 分（SOTA 媲美水平）。

---

## 7. 验证证据附录

### 7.1 关键文件 grep 验证

```bash
# Heartbeat daemon 调用链
$ rg -n "start_heartbeat|heartbeat_daemon|heartbeat_loop" backend/app -g "*.py"
backend/app/services/heartbeat.py:1754:async def start_heartbeat():  # 死代码

# 实际调用路径
$ rg -n "_heartbeat_tick" backend/app -g "*.py"
backend/app/services/trigger_daemon.py:1476:from app.services.heartbeat import _heartbeat_tick
backend/app/services/trigger_daemon.py:1477:await _heartbeat_tick()

# Dream 调用链
$ rg -n "run_dream" backend/app -g "*.py" | grep -v auto_dream.py
backend/app/services/heartbeat.py:1420:asyncio.create_task(run_dream(agent_id, agent.tenant_id))
backend/app/services/trigger_daemon.py:1210:asyncio.create_task(run_dream(agent_id, agent.tenant_id))
backend/app/services/memory_service.py:311:asyncio.create_task(run_dream(agent_id, tenant_id))

# tenant_id=None 兜底路径
$ rg -n "tenant_id=None" backend/app/runtime -g "*.py"
backend/app/runtime/invoker.py:152:return RuntimeConfig(tenant_id=None, max_tool_rounds=200)
backend/app/runtime/invoker.py:159:return RuntimeConfig(tenant_id=None, max_tool_rounds=200)
backend/app/runtime/invoker.py:182:return RuntimeConfig(tenant_id=None, max_tool_rounds=200)

# Admin 鉴权复核：router decorator 级 dependencies 为 0，但函数参数级 role guard 存在
$ grep -rn "dependencies=\[Depends" backend/app/api/admin*.py | wc -l
0
$ grep -n "require_role(\"platform_admin\")" backend/app/api/admin.py
backend/app/api/admin.py:92:    current_user: User = Depends(require_role("platform_admin")),
backend/app/api/admin.py:274:    current_user: User = Depends(require_role("platform_admin")),
backend/app/api/admin.py:292:    current_user: User = Depends(require_role("platform_admin")),
backend/app/api/admin.py:310:    current_user: User = Depends(require_role("platform_admin")),
backend/app/api/admin.py:329:    current_user: User = Depends(require_role("platform_admin")),
```

确认：`Admin 端点零鉴权` 原结论不成立；更准确的问题是 router 级统一 guard 缺失，新增端点依赖人工记得添加 `Depends(require_role(...))`。

### 7.2 main.py lifespan 后台任务列表（验证）

```python
# backend/app/main.py:344-350
for name, coro in [
    ("trigger_daemon", start_trigger_daemon()),
    ("feishu_ws", feishu_ws_manager.start_all()),
    ("dingtalk_stream", dingtalk_stream_manager.start_all()),
    ("wecom_stream", wecom_stream_manager.start_all()),
    ("wechat_personal_stream", wechat_personal_stream_manager.start_all()),
]:
    task = asyncio.create_task(coro, name=name)
```

确认：lifespan **不直接启动** heartbeat / dream / workspace_sync，而是通过 trigger_daemon 间接驱动。

### 7.3 trigger_daemon 调度验证

```python
# backend/app/services/trigger_daemon.py:1443-1491
async def start_trigger_daemon():
    logger.info("⚡ Trigger Daemon started (15s tick, heartbeat every ~60s)")  # ← 注意：60s

    asyncio.create_task(_workspace_sync_loop())
    asyncio.create_task(_workspace_full_sweep_loop())

    _heartbeat_counter = 0
    while True:
        try:
            await _tick()
        except Exception as e:
            ...

        _heartbeat_counter += 1
        if _heartbeat_counter >= 4:  # ~60s
            _heartbeat_counter = 0
            try:
                from app.services.heartbeat import _heartbeat_tick
                await _heartbeat_tick()
            except Exception as e:
                logger.error(f"Heartbeat tick error: {e}")

        await asyncio.sleep(TICK_INTERVAL)  # 15s
```

确认：实际 heartbeat 频率 **~60s**，CLAUDE.md 文档声称 **45min**，差 45 倍。

### 7.4 Governance fail-open 验证

```python
# backend/app/tools/governance.py:234-298
if not context.tenant_id:
    logger.info("[Governance] No tenant_id — skipping capability checks for tool %s", context.tool_name)
    # ← 仅 log，不 return，下方 if context.tenant_id: 块全部跳过

tenant_uuid: uuid.UUID | None = None
if context.tenant_id:
    try:
        tenant_uuid = uuid.UUID(context.tenant_id)
        cap_result = await _maybe_await(deps.check_capability(...))
        # ... 所有 capability check 逻辑
```

确认：tenant_id=None 时 capability check **完全不执行**，但 governance 函数继续向下走（不 return）。精确定性：security zone 与 dangerous-command 逻辑仍会执行，不能称为所有工具完全 unrestricted；但依赖 tenant capability policy 的工具会绕过 capability gate。

### 7.5 委托循环检测缺失验证

```python
# backend/app/agents/orchestrator.py:530-547
async def _delegate(request: AgentDelegationRequest) -> AgentDelegationResult:
    trace_id = request.trace_id or uuid.uuid4().hex
    ...
    if request.depth > request.policy.max_depth:  # ← 仅 depth 检查
        return AgentDelegationResult(
            content="⚠️ Delegation depth limit reached",
            ...
            failed=True,
        )
    # 无 visited agent_id set 检查
    # 无 trace_id 上的 cycle detection
```

确认：仅 depth limit，无任何 visited / cycle 检测机制。

---

## 8. 文档结尾

**审查交付物清单**：
- 6 份独立模块审查（subagent 输出）
- 3 处主体交叉验证（grep + Read）
- 3 个 P0 + 17 个 P1 + 12 个 P2 清单（含 file:line 证据与复核修正）
- 3 波修复路线（ROI 排序）
- vs SOTA 矩阵（Claude Code / Hermes / Letta/MemGPT / 企业 SaaS / CrewAI/AutoGen 对比）

**置信度依据**：
- 核心代码事实基于 file:line 实际代码证据
- 关键 P0 经二次验证（grep + Read）
- 修正了 3 处原报告误判/过度推断：heartbeat daemon 状态、admin 零鉴权、scenario/output_efficiency 的 frozen prefix 归属
- cache 命中率、SOTA 对比和成熟度评分属于工程判断，需要后续运行时指标验证
- 与 commit history 交叉印证（c52e236 / 1b050b2 / 2bce78e / f2a9555 / 03c0e8a / 2e0ab52 / b02ca05）

**下一步建议**：
1. Owner 评审本文档，确认 3 个 P0 优先级
2. 第一波 3 个 P0 + cache key / admin guard consistency（建议 1 sprint 内完成）
3. 第二波架构调整（建议 2 sprint）
4. 监控 cache 命中率、抽取失败率、权限拒绝率三个核心指标，作为修复成效验证

---

*报告生成：Claude Opus 4.7 (1M context)，6 并行 subagent 审查 + 主体综合 + 关键证据交叉验证。2026-04-28 由 Codex 复核修订，修正误判与过度量化结论。v3：4 路独立 team 二次复核 + 主体亲自 grep/Read 验证 channel 路径与工具数。v3.1：补齐 autonomous identity/capability writeback 断点。*

---

## 9. v3 修订日志（2026-04-28 晚）

### 9.1 4 路 team 复核延伸结论

#### Team A — P0 三件套（3/3 主体保留）

| P0 | 状态 | 关键证据补充 |
|----|------|-------------|
| P0-1 capability gate skip | ✅ 完全成立 | governance.py:178-211 的 security zone 与 :305-378 的 dangerous command 检测仍跑，但 capability gate 这层确实 skip |
| P0-2 抽取 fire-and-forget | ✅ 主体保留 P0（范围收窄） | LLM 失败有 pattern fallback 覆盖；但 `schedule_extract()` 创建任务失败、进程退出、`drain()` 10s 超时取消等场景仍无 durable queue/replay guarantee |
| P0-3 A2A 无循环检测 | ✅ 静态成立，攻击路径需运行时复现 | `send_message_to_agent` 在 base excluded tools，但 `messaging` 工具仍可绕过黑名单；静态确认无 visited/cycle 检测，A→B→A→B 需要 runtime reproduction/log 证明 |

#### Team B — Codex 修订删除项（4 项准确，1 项收窄后升回）

| Codex 修订 | 复核 | 备注 |
|-----------|------|------|
| Admin 鉴权降 P2 | ✅ 准确 | admin.py 当前 16 个 route，其中 14 个使用 `Depends(require_role("platform_admin"))`，2 个使用 `require_role("admin")`；问题是 router 级 guard 不统一 |
| scenario 在 dynamic suffix | ✅ 准确 | prompt_builder.py:161 在 `build_dynamic_prompt_suffix` 内 |
| output_efficiency 返回空 | ✅ 准确 | output_efficiency.py:4-6 直接 `return ""` |
| X-Tenant-Id 完整校验 | ✅ 准确 | security.py:116-129 含 UUID/存在性/active 三层校验 |
| **soul/focus 锁要收窄** | ⚠️ **要升回 P1** | heartbeat evolution 有 flock；**但 auto_dream.py:420 写 soul、:1295 清 focus 都无锁**，多 worker 并发写 focus（与 objective_service）有真实风险 |

#### Team C — 新 P1 论断（4/4 全部成立）

| P1 | 复核证据 |
|----|---------|
| P1-1 capability fail-open | CAPABILITY_MAP **83 个 direct tool mappings** + **2 个 synthetic capability keys**；`@tool` 装饰器约 80 个，核心问题仍是未映射工具默认 allow |
| P1-2 heartbeat 调度 + 频率不一致 | trigger_daemon.py:1476-1477 间接驱动；CLAUDE.md "45min" vs 代码 60s = **45 倍差准确**；`start_heartbeat` 仅 1 处定义零外部调用 |
| P1-3 子 agent creator_id | messaging.py:1114 + orchestrator.py:622 验证；攻陷子 agent = 攻陷 creator 表述准确 |
| P1-4 RLS BYPASS | 结构风险确认；BYPASS 设置点需运行时审计（哪些表 NULL tenant_id 实际存在） |

#### Team D — 系统集成 + 修复路线（1 关键 + 多个修正）

| 论断 | 复核 | 备注 |
|------|------|------|
| **6 channel 不走 invoke_agent** | ❌ **过度论断（被主体复核推翻）** | 多数 channel 通过 `_call_agent_llm -> websocket.call_llm -> invoke_agent` 间接走；Feishu/WebSocket task 创建路径另有 `task_executor.execute_task -> invoke_agent`；governance 仍生效 |
| Dream 绕过 invoke_agent | ✅ 准确但需收窄 | auto_dream.py:371 直调 `create_llm_client` 并写 soul；这是 identity-writeback 路径的关键断点，但不是全系统唯一 `create_llm_client` 调用方 |
| 工具数 80 是虚高 | ❌ **错误（被主体复核推翻）** | 主体 grep 实测 79 个 @tool（handlers/）+ 1 个外层 = **80**，与原报告一致 |
| 第一波 2 周偏乐观 | ✅ 采纳 | 改为 **3 周**（含测试 + 部署验证） |
| 整体 56 → 54 | ⚠️ 折中 | v3 取 **55**（soul/focus 升 P1 → 记忆模块 -2；其他模块不变） |

### 9.2 v3 vs v2 总差异

| 维度 | v2 | v3 |
|------|----|----|
| 整体评分 | 56 | **55**（soul/focus 升 P1 拉低记忆模块 2 分） |
| 记忆模块 | 60 | **58** |
| A2A 模块 | 60 | **58**（messaging 工具支持该路径假设；仍缺 runtime log 复现） |
| 系统模块 | 62 | **60**（dream identity-writeback 关键断点更明确） |
| 第一波工期 | 2 周 | **3 周** |
| Entry Point 矩阵 | "6 channel ✓ 走 invoke_agent" | 修正为"多数经 `_call_agent_llm -> call_llm -> invoke_agent`，Feishu/WebSocket task 路径经 task_executor；dream 标为 identity-writeback 直调 LLM 断点" |
| CAPABILITY_MAP 数 | 83 | **83 direct mappings + 2 synthetic keys**（实测） |
| P0 数 | 3 | 3（不变；P0-2 标注"部分由 pattern fallback 覆盖"） |
| P1 数 | 16 | **16**（v3 口径；v3.1 另新增 P1-17，当前总数为 17） |

### 9.3 v3 置信度自评

- **核心代码事实**：高（4 路 team 复核 + 主体亲自 grep/Read）
- **修复工作量**：中等偏高（已校准为 3 周非 2 周）
- **整体评分 55**：折中判断（Team D 给 54，原报告 56），合理区间在 53-58
- **Entry Point 矩阵**：中高（`call_llm` 与 `task_executor` 两条桥接路径已 file:line 验证；channel 覆盖仍建议补 runtime smoke）

**仍需运行时验证**：
1. cache 命中率实测（接入 Anthropic API 指标）
2. RLS BYPASS 触发频率（哪些 endpoint 真用了 BYPASS）
3. 抽取失败率实测（OOM / LLM 错误 / drain 超时分别占比）
4. 多 worker 部署下 auto_dream 并发写 focus.md 的实际碰撞概率
5. CLAUDE.md "45min heartbeat" 是否需要改为 "60s"，或代码改为真实 45min

**v3 与 Codex 修订的差异**：
- Codex 给的 5 个修订，v3 完全采纳 4 个，1 个（soul/focus）经 Team B 进一步细化后升回 P1
- v3 新增"channel 间接路径"明确化 + 工作量校准 + 评分微调（55 vs 56）
- v3 删除原报告"6 channel ✓ 走 invoke_agent"的简单陈述，改为更精确的"`call_llm` 桥接 + task 创建路径 `task_executor` 桥接"

### 9.4 v3.1 — autonomous identity/capability writeback 路径补全（2026-04-28 终版）

**触发**：v3 定稿后用户复核，主体抓取了一个观察——`create_llm_client` 全系统 8 处直调，v3 文档只标了 dream 一处关键断点，应该把同性质的 `skill_distiller` 也补上。

**主体亲自验证 8 处 `create_llm_client` 调用上下文**：

| 调用点 | 性质 | 是否需要 governance/audit | 处理 |
|--------|------|--------------------------|------|
| `runtime/invoker.py:691` | invoke_agent **内部**正常路径 | N/A — 本来就是 governance 入口 | 不补 |
| `api/enterprise.py:89` | admin **LLM 连通性测试端点**（让模型说 "ok"），用户主动触发 | 端点函数使用 `Depends(get_current_admin)`，属于显式 admin 操作 | 不补 |
| `memory/retriever.py:156` | 检索 rerank | 内部辅助，不写持久 agent 状态 | 不补 |
| `services/session_recall.py:365` | 会话回忆总结 | 内部辅助 | 不补 |
| `services/conversation_summarizer.py:562` | 上下文压缩 | 内部辅助 | 不补 |
| `services/extract_agent.py:413` | T0→T2 抽取 | 写学习数据，但属 P0-2 已覆盖的 hot path | 已在 P0-2 |
| **`services/auto_dream.py:371`** | **写 soul.md（agent identity 冻前缀）** | **必须**——写持久身份、无 unlearn | v3 已标关键断点 |
| **`services/skill_distiller.py:527`** | **autonomous LLM 决定 `promote/patch/defer/reject` skill + 生成 `instructions_markdown`/`declared_tools`/`declared_packs`** | **必须**——写持久能力、影响后续所有调用；promotion 后有 evolution ledger，但缺预写入审批/统一 DB audit | **v3.1 补为关键断点** |

**v3.1 vs v3 差异**：
- Entry Point 矩阵：从 1 个关键断点（dream）增至 **2 个**（dream + skill_distiller）
- 系统模块表述：从"auto_dream consolidation 直调 LLM"改为"autonomous identity/capability writeback 直调 LLM"
- P1 清单：新增 **P1-17**（合并两者的同性质问题）
- 修复路线第三波：增加 "Skill distiller 走 invoke_agent / 加预写入审批 + DB audit"（3 天）
- 评分不变（55/100）—— 因为新发现的问题与 dream 同性质，已隐含在原系统模块 60 分内
- 第三波总工期估算：6 周 → **6.5 周**（多出 skill_distiller 的 3 天）

**v3.1 真实定性**：在所有 LLM 直调中，**写持久 agent 状态（soul / skill）的 autonomous 路径有 2 处**，都没有走标准 runtime governance/capability gate。其他 6 处直调要么是内部辅助（不写持久态），要么是 invoke_agent 自己（本身是 governance 入口），要么是用户主动触发的 admin 配置（已鉴权）。

这意味着：**Hive 的"自演化"承诺在两个最关键的写持久态路径上都缺标准治理闭环**——dream 改身份缺统一 audit；distiller 改能力虽有 evolution ledger，但缺预写入审批、统一 DB audit、以及 runtime governance/capability gate。这是"自进化 agent 框架"对外宣称中需要补强的最薄弱点。
