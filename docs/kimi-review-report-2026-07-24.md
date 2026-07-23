# Kimi 独立原子化审查报告 — CCPlus / Agent-Native（2026-07-24）

原始审查者：Kimi Code（独立审查，非实现参与方）
当前版本：Codex current-source 复核修订版（2026-07-24）
审查依据：`docs/ccplus-agent-native-independent-review-prompt.md`（可复用正文全文执行）
本报告原始版本由 Kimi 新建；Codex 复核仅修订本报告，不修改业务代码、数据库或部署配置。修订遵循“保留原编号、正文同步改正、撤销项留痕”的审计规则。

---

## 1. 执行摘要与上线判断

原始审查从当前源码重新建立结论，不继承既有完成声明，覆盖 14 个审计面（CC/FreeCode 基线、kernel 与模型循环、会话生命周期、工具治理、session 中段五能力、记忆系统、自进化、A2A、知识平面、企业治理、Hive Connect、前端消费、Codex/hermes 对照、验收面）。Codex current-source 复核确认了六个原始 P0 所指向的代码事实，同时发现一项漏判的 live Model Agency 违规、两项 Hive Connect source-boundary 误报、一项前端产品状态误分类、一项员工设置的运行时实现与权限边界泄漏，以及若干不能由现有证据支持的上线表述。

**总体判断：Hive 的单 Agent 机制主干在当前源码上有较强接线证据，多处实现也体现了 CCPlus/Hive-native 增量；但“Goal 1 智能质量至少达到 hermes”尚无行为级对比证据。当前存在七个上线阻断项，其中 GV-08 是原报告漏判的 live Model Agency 违规。当前裁决为 NO-GO；修复这些阻断项只会恢复进入验收的资格，不自动构成上线判断。**

核心结论：

- **单 Agent 机制主干有较强源码证据，但行为质量未证实。** kernel 模型循环唯一接线、compaction 带 coverage ledger（强于 CC 单摘要与 Codex 单 turn 摘要）、LoopGuard 只告警且终态解释仍由模型撰写、记忆系统 T0→T2→T3→soul 以 LLM-primary 为主、Plan Mode/Subagent/Work Ledger/Skill 均有真实接线。不过不能据此推导“智能质量至少达到 hermes”；该主张仍需同模型、同任务、同证据条件下的行为级对比。
- **最重的断点是治理自伤（GV-01/GV-02）**：Preflight ASK 创建的 CoordinationCheckpoint 写入内存网关（永不落 PG）且全仓无任何批准/恢复消费者——自治车道（trigger/heartbeat/任务执行）发起的外部可见动作被 ASK 后**永远执行不了**；企业 Approval 票证批准后重入执行管道时又可能被 preflight 二次 ASK，且 ASK 裸文本被误判 `succeeded`——**效果未发生但票据记成功**。这直接削弱北极星 Goal 1 的"自治数字员工"能力。
- **七个 P0 上线阻断项**：GV-01（preflight checkpoint 死信）、GV-02（approval×preflight 票据误记）、SA-05（REST 带 confirmed_plan_id 启动 workflow 生产 500）、GV-04（platform_admin 跨租户化身无审计）、GV-05（桌面 llm_proxy 无配额无计量）、HC-01（A2A 委派本地执行结果孤立，源 Agent 永不知结果）、GV-08（对任意工具参数做凭据模式扫描并据此整调用 REFUSE）。
- **文档假完成集中爆发**：AGENTS.md 与当前源码漂移严重——`proactive_employee_loop`、`policy_replay`、`viking_client`、`knowledge_inject`、`extract_queue`、`extract_agent`、`scheduler`、Objective 实体均已不存在或被替换；迁移数 79→实际 178、前端测试 39→实际 123、Office 专用编辑面已被当前前端测试合同明确退役、"turn-level token budget gates"声明与源码矛盾。这些必须按"已知缺失/已退役"改写，否则后续审计会继续继承假完成。
- **Hook 产品边界被放反（UI-09）**：`Runtime hooks` 是平台内部生命周期、证据、恢复与治理机制，不是员工或其 Owner/Manager 的产品心智。当前员工设置却直接展示 handler 名、event、failure mode 与失败 receipt，并允许 `manage/owner` 逐项持久化禁用。原报告把“员工侧没有用户自定义 hook”判为缺失，方向相反：员工面必须移除内部 Hook 细节和开关；若 CCPlus 需要可扩展 Hook，应另建受治理的 Platform Developer/Extension 面，且不得与平台内置 Hook 混用。
- **Codex/hermes 对照**：Hive 已吸收大部分工程增量（sandbox provider、approval 路由、resume/fork、compaction）；可吸收但未做的：unified exec（PTY/持久 shell 会话）、execpolicy 命令级声明策略、hermes 的 session_search（跨会话原文检索）与 verify-on-stop。无"错误改变 CC 语义"的吸收。
- **上线判断**：**NO-GO / Acceptance incomplete**。七个 P0 修复后，仍必须重新核生产接线并完成 §11 中 Goal 1 行为对标、真 PG、多进程、自治批准回放、GV-08 Model Agency 回归和真实 Hive Connect 安装态验收；不得从“代码修复完成”直接跳到 GO。

---

## 2. 审查范围、当前环境与未覆盖面

### 2.1 环境记录

- 仓库根：`/Users/rocky243/vc-saas/hiveclaw-main`；证据时间点：2026-07-24 01:34（+0800）。
- **git 元数据损坏**：`.git` 指向不存在的 worktree gitdir（`/Users/rocky243/vc-saas/hiveclaw/.git/worktrees/hiveclaw-main`），HEAD 与 diff 不可得。本审查以当前工作树文件为唯一事实源；无法做"相对某基线的变更"分析。
- 基线源码均可访问：FreeCode（`/Users/rocky243/vc-saas/free-code-main`）、claude-code-org、claw-code（Py/Rust）、codex-rs、hermes-agent。原报告未读取 Skill 实际安装的 canonical `/Users/rocky243/vc-saas/hive-connect`，导致 HC-02/HC-04 source-boundary 误报；本修订已补读对应 `0.1.9` 源码。
- 只读验证已执行：`alembic heads` → 单头 `completion_outbox_index_0721`；178 个迁移文件；923 个后端测试文件 / 7278 个测试函数 / 7687 个收集后用例；全量后端 pytest 已复跑；canonical Hive Connect 的 Hive adapter、daemon 与 CLI 包测试已执行（结果见 §14）。
- 子审查员实跑测试证据：kernel+invoker 195 项全绿；knowledge 平面 134 项全绿（13 项 Docker-off skip）。

### 2.2 覆盖面

| 面 | 覆盖 |
|---|---|
| CC/FreeCode 基线 | 31 条生命周期能力账本（含 hooks 28 事件、权限五模式、compaction、resume/fork/rewind、MCP、后台任务） |
| 单 Agent | kernel 全 7 文件、invoker、llm_client、web_chat_runtime、websocket、RuntimeTask claim/cancel/recovery、session 中段五能力 |
| Hive Native | memory/ 全部 31 模块、自进化（skill distillation/dream/soul）、A2A/delegation、知识平面、canonical Hive Connect 与仓内 legacy local_bridge 边界 |
| 企业治理 | RLS、capability gate、GuardPolicy、approval、quota、secrets、审计双汇、AI 资产、admin/desktop 面 |
| UI/UX | 前端 16 页、57 个 api/domains、chat 传输全链、三受众分层、i18n 源码抽样；员工设置 Hook 暴露有用户提供的生产截图与 current-source 接线证据；精确 i18n 缺失数尚无可复现统一口径 |
| 验收 | CI 三门禁、178 迁移、部署契约、测试断言抽查（write_gate/preflight/approval/sandbox/migrations） |

### 2.3 未覆盖面（诚实声明）

- 真实浏览器端到端行为（未跑 Playwright）；UI-09 另有用户提供的生产页面截图，但本次未独立操作浏览器复现。
- 生产数据库事实（feature flag 行、tenant 策略行的真实配置）。
- 多进程/多实例部署行为（Redis cancel bus、WS fanout、进程内 dict 一致性）。
- Railway 生产运行证据与最近一次 CI run 状态（无网络核实）。
- FreeCode 未实际运行，基线为静态源码证据。
- `~/.hive/data/agents` 磁盘实物（T0/T2 文件）未抽样。
- canonical `@hiveclaw243/hive-connect` 已确认存在 ping/daemon 源码与包级测试，但未在真实登录设备执行 install→restart→presence 的端到端验收。

---

## 3. 双北极星与 Model Agency 裁决

### 3.1 北极星 Goal 1（最强可控数字员工）：**机制主干有较强证据，行为质量未证实**

成立面（源码与接线证据）：

- 模型循环唯一接线：`invoker.py:1617 → invocation_orchestrator.py:56 → engine.py:3811 → turn_orchestrator.py:326`，全库无第二工具循环；kernel 零 DB import 不变量成立。
- compaction 是当前三个基线中最强实现：0.7 窗口输入比、map-reduce 完整覆盖 + sha256 coverage manifest（`conversation_summarizer.py:269-407`）、20K 输出预算对齐 CC COMPACT parity、无机械语义 fallback（失败走诚实降级标记）。强于 FreeCode 单摘要（`services/compact/compact.ts:541-653`）与 codex 单 turn 摘要（`core/src/compact.rs:123`）。
- 工具治理七道有序门禁（zone→tenant→guard→mcp→capability→dangerous→hooks，`tools/governance.py:1054-1075`），deny/ask/unavailable 全 typed 且只冻结目标工具，带教学信息。
- 记忆系统全链路保留模型判断权：T2 包三个 LLM（summary/labels/独立 review），无模型配置时 **held 不降级**（`t2/segment_package.py:259-287` 明示 "no mechanical summary fallback"）；retriever 用 LLM 语义选择器，模型失败走 ref-only 不机械代选（`retriever.py:294-306`）。
- 对 hermes benchmark：动态激活（`memory/activation.py` + retriever）优于 hermes 的会话开始冻结快照注入（`hermes agent/tools/memory_tool.py`）；压缩用主模型+coverage ledger 强于 hermes aux 模型方案。

扣分面（实证）：

- **GV-01 使自治车道外部动作功能性卡死**——一个"治理更复杂但实际更容易卡住"的实证，正是北极星警告的反模式。自治 Agent（trigger/heartbeat/委派执行）发起 `send_feishu_message` 等外部可见动作时被 preflight ASK，checkpoint 写内存、无批准入口、无恢复路径、重启即丢。Agent 越自治，越撞这堵墙。
- **GV-08 在 live 工具路径上以凭据模式正则产生整调用 REFUSE**——它会把文档、测试样例或模板中的形似凭据当成真实未授权 secret，并剥夺模型在已认证执行框架内的语义与表达能力。该行为违反 Model Agency Boundary 的“精确未授权字节”边界。
- SA-01：turn 级 token 预算护栏空转（派生但无消费者），`AGENTS.md` 声明与源码矛盾。
- HN-01/HN-02：宣称的主动管理循环（proactive_employee_loop、policy_replay）在源码中不存在；当前 heartbeat 已收窄为纯记忆固化（无工具执行器）——这是更保守的安全姿态，但必须改文档而不是假装存在。
- **尚未证明的北极星主张**：没有在相同模型、相同授权证据与相同任务集上运行 Hive 与 hermes 的行为级对比，因此本报告不得把结构优势外推为“智能质量至少一样好”。

### 3.2 北极星 Goal 2（公司级控制中台）：**主体成立，证据面有缺口**

- 成立：RLS 60+ 迁移 ENABLE+FORCE、strict 启动拒 superuser/BYPASSRLS（`rls_runtime_guard.py:89`）、唯一旁路强制 reason；secrets Fernet+HKDF 密钥环、无 master key 拒启动；AI 资产（Agent/Skill/Workflow/外部能力）revision/usage/rollback 接在真实变更点；租户安全事件哈希链 + immutable/no-truncate 触发器 + entrypoint 启动 gate。
- 缺口：platform_admin 跨租户化身全程无安全审计（GV-04）；llm_proxy 裸 httpx 转发绕过全部配额计量（GV-05）；审计双汇——operator 事件退化进无链的 audit_logs 且写入失败静默（GV-06）；A2A 协作组与三个治理 router（guard_policies/feature_flags/config_history）有后端无前端（A2A-03、UI-02）；员工设置反向暴露内部 Hook 注册表、失败 receipt 与持久化禁用权（UI-09）。

### 3.3 Model Agency Boundary 裁决：**一处 live 违规，一处观察项**

多数模块未发现关键词/正则/计数器替代模型语义判断、饿输出或静默裁剪；但工具 preflight 的 PL4 分类存在一处明确 live 违规：

- 正面证据：LoopGuard 启发式只 warn 不裁决，硬终止需"工具自报重试耗尽+无副作用+进度 token 未推进"三重机械证据，终态解释仍由模型撰写（`loop_guard.py:194-212`、`turn_orchestrator.py:2700-2733`）；`infer_task_profile` 故意返回中性值、永不降级主模型（`context_budget.py:118-126`）；work ledger 明示不做关键词分类（`agent_work_ledger.py:982-997`）；A2A collaborator 注入显式"永不机械裁剪"（`a2a_collaborators.py:21`）；测试钉住"low_confidence 不机械变 abstention"（`test_write_gate.py:17`）、"平台不得伪造模型回答"（`test_engine.py:108`）。
- **GV-08（违规，P0）PL4 全参模式扫描产生硬结论**：`_build_tool_preflight_input` 对每次工具调用参数 JSON 运行凭据模式正则（`service.py:1676-1678`、`privacy_layer.py:118-145`），命中即由 `action_preflight.py:122-129` 对整个调用返回 REFUSE。`test_service.py:2208-2247` 还把“`write_file` 写入含 `api_key=sk-...` 示例文本时不得执行”钉为契约。其权威事实源只是模式命中，不是真实活跃凭据或授权秘密字节，因此不满足 secret egress 的精确事实边界。完整修复必须在可信 secret store/credential binding 边界精确识别未授权字节，只拒绝或遮蔽那些字节；模式扫描最多可发 audit/repair 信号，不能决定整调用语义。回归必须覆盖 benign 文档示例、测试 fixture、真实活跃凭据、嵌套参数与模型原始输出保真。
- **观察项**：heartbeat 不进全工具循环是显式设计收窄，当前源码下不是 Model Agency 违规；但它使 HN-01 的"heartbeat 准备低危工作"成为文档虚构。

---

## 4. CCPlus 基线账本与源码对照

基线从 FreeCode 当前源码直读建立（`free-code-main/src`，31 条），Hive 映射与判定来自各模块生产路径追踪。差异类别：缺失 / 语义退化 / 可接受实现差异 / 工程增强 / 主动超越 / 排除。

| # | 能力（生命周期节点） | FreeCode 语义要点（证据） | Hive 当前映射 | 差异类别 | 七原子状态 |
|---|---|---|---|---|---|
| 1 | 系统提示组装 | 静态段+动态 registry 段，`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 保 cache（`constants/prompts.ts:444,114`） | prompt_sections + 动态 suffix（skill catalog `agent_context.py:230`、`invoker.py:656`）+ canonical cache anchors（`prompt_cache.py:96`） | 工程增强 | 闭环 |
| 2 | CLAUDE.md / 项目指令 | 嵌套目录、条件规则、`@` include（`context.ts:155`、`claudemd.ts:618`） | soul.md + governed identity/charter 段（`prompt_sections/identity.py:34-53`、`agent_manager.py:122-184`） | 可接受差异+治理增强 | 闭环 |
| 3 | 上下文组装顺序 | system 块（git status 截断 2000 字符）+ user 块（`context.ts:116,22`） | 分层 context 组装 + ActivationContext fail-closed（`memory_service.py:199-218`） | 可接受差异 | 闭环 |
| 4 | 工具清单与发现 | 40+ 工具、deny 规则过滤（`tools.ts:193,262`） | 100+ 注册工具 + L2 发现链（`tool_search`→deferred schema 扩展 `kernel/engine.py:495`，resume 恢复 `:875-900`） | 工程增强 | 闭环 |
| 5 | 权限模式 | default/acceptEdits/plan/bypassPermissions/dontAsk（`types/permissions.ts:16`） | capability gate 五段判定 + session permission + Plan Mode（`capability_gate.py:327-523`、`session_permission_runtime.py:520-635`） | 可接受差异 | 闭环 |
| 6 | 权限规则引擎 | allow/deny/ask 三类、deny 优先、分层规则源（`permissions.ts:473,1071`） | GuardPolicy 精确工具名+argument_equals 机器契约（`tools/guard_policy.py:32-85`）；无命令级 DSL | 可接受差异（execpolicy 可吸收，见 §10） | 闭环 |
| 7 | Hooks 事件面 | 28 事件（`entrypoints/sdk/coreTypes.ts:25`） | 30+ 事件（`runtime/hooks.py:27-121`），PRE/POST_TOOL_USE 支持 block/modified_args（`kernel/engine.py:1895,2272`），receipts 落 invocation_spans | 主动超越 | 闭环 |
| 8 | Hook 扩展与产品边界 | 本地开发者 settings 可配置任意 command 钩子（`types/hooks.ts:238`） | 平台 handler 代码内置；但员工设置直接暴露内部注册表并允许 `manage/owner` 禁用（UI-09）；尚无与内置 Hook 隔离的受治理 Developer/Extension 面 | **边界映射错误；扩展面待产品契约** | 局部闭环 |
| 9 | Plan Mode | 只读探索→plan 文件→ExitPlanMode 批准；AskUserQuestion 澄清（`tools/EnterPlanModeTool`、`ExitPlanModeTool`） | 确认边界机械强制（`tools/service.py:1153,1339` 只读 block）、hash/版本绑定确认（`plan_mode_service.py:571`）、澄清卡（`handlers/plan_mode.py:482-601`） | 工程增强 | 闭环 |
| 10 | TodoWrite | 模型自维护清单，纯认知记账（`tools/TodoWriteTool`） | Work Ledger（track_todo/record_finding/read_ledger），明示不做关键词分类（`agent_work_ledger.py:982-997`），跨 compaction 恢复注入 | 语义等价 | 闭环 |
| 11 | Skills 渐进披露 | 清单 1% 上下文预算、描述截 250 字符、调用时载入全文（`SkillTool/prompt.ts:22,27`） | `load_skill` 不限字符（`workspace.py:304-406`）、catalog 预算 4000+tool_search 兜底、可执行组件走受治理运行时（`skill_runtime.py:81`） | 工程增强 | 闭环 |
| 12 | Subagent（Agent 工具） | 独立上下文、按定义过滤工具、concise report 返回（`AgentTool.tsx:196`） | spawn_subagent：standalone prompt 不继承宿主身份、结果未截断（`agents/subagent.py:1127,1262,1306`）、前后台双模式 | 工程增强 | 闭环 |
| 13 | Subagent fork/resume | sidechain transcript 可 resume（`forkSubagent.ts`、`resumeAgent.ts`） | replay-safe resume（`subagent_run_service.py:81,1106,1478`）、RuntimeTask worker 认领、完成唤醒 drain | 工程增强 | 闭环 |
| 14 | Compaction 触发 | 阈值=有效窗口−13K buffer（`autoCompact.ts:62-90`） | 75% proactive + 60% microcompact + PTL reactive 三道（`ccplus_contracts.py:124-125`、`turn_orchestrator.py:1530-1816`） | 主动超越 | 闭环 |
| 15 | Compaction 质量 | LLM 单摘要 + 重挂 plan/skill/附件（`compact.ts:541-653`） | map-reduce 完整覆盖 + sha256 coverage manifest + 20K 输出（`conversation_summarizer.py:269-407`），失败 hold 不机械兜底 | **主动超越** | 闭环 |
| 16 | Transcript 持久化 | 每会话 JSONL、sidechain 子目录、50MB 读限（`sessionStorage.ts:202,229`） | ChatTranscriptEvent（云端事务事实）+ T0 events.jsonl/source.md 双投影 hash 链（`memory/t0/ledger.py:632,655`） | 工程增强 | 闭环 |
| 17 | resume/continue/fork | `-c/-r/--fork-session/--resume-session-at`（`main.tsx:988-991`） | durable RuntimeTask 重启恢复（`resume_persisted_web_chat_runs` `web_chat_runtime.py:2807`、SKIP LOCKED reclaim `runtime_task_claim_service.py:175-210`）+ fork API（`chat_sessions.py:446,489,498`） | 工程增强 | 闭环 |
| 18 | checkpoint/rewind | 编辑快照 + rewind 恢复文件态（`fileHistory.ts:86,347`） | workspace 快照（每 user event）+ projection rewind + branch rewind 9 种 mode（`conversation_branch_service.py:326`） | 可接受差异（双 rewind 语义并存，SA-02） | 局部闭环 |
| 19 | Slash commands | 内建+`.claude/commands/*.md`（`commands.ts:476`） | session commands（rewind 等 `session_command_runtime.py`）；无用户自定义命令面 | 可接受差异 | 局部闭环 |
| 20 | MCP 客户端 | stdio/SSE/HTTP、OAuth、elicitation（`services/mcp/client.ts`） | MCP import/call + authz（拒 token passthrough/URL userinfo/access_token，`mcp_authz.py:62-104`） | 工程增强（治理） | 闭环 |
| 21 | 后台任务 | run_in_background + 持久任务态（`tasks/LocalShellTask`） | RuntimeTask + worker claim + typed delivery receipt | 工程增强 | 闭环 |
| 22 | 取消/Esc | abortController 贯穿流式与工具（`useCancelRequest.ts:63`） | durable ControlInput + 幂等键 `cancel-run:{run_id}` + Redis cancel bus + fence CAS 结算 | 工程增强 | 闭环 |
| 23 | 成本/token 追踪 | 会话成本累计、resume 恢复（`cost-tracker.ts:278`） | token_tracker 三层配额 fail-closed（`invoker.py:1507-1537`）；**但 turn 级预算空转（SA-01）** | 可接受差异 | 局部闭环 |
| 24 | 多模型路由 | 主循环覆盖 + 429/529 fallback（`model.ts:95`、`withRetry.ts:163`） | 429-only 重试×10+Retry-After、overload→fallback_model、账户类错误不 fallback（`llm_client.py:426-487`、`turn_orchestrator.py:1816-1907`） | 语义等价 | 闭环 |
| 25 | Team/多 agent | 进程内 teammate + 消息（`TeamCreateTool`、`SendMessageTool`） | A2A：send_message_to_agent 同步咨询 + delegate_to_agent 异步委派 + Lease/Signal 原语 + 权限收缩（DelegationToken） | 主动超越（但见 HC-01/A2A-03） | 局部闭环 |
| 26 | 定时/触发 | cron 调度工具（`ScheduleCronTool`） | trigger_daemon 15s tick + fire lease + RuntimeTask | 工程增强 | 局部闭环（SA-03） |
| 27 | Workflow | FreeCode 侧为 stub（`tools/WorkflowTool/` 仅 constants）——**非 CC parity 债务** | Hive-native workflow：RuntimeTask+PG step/leaf journal+quota+gate+trigger+admin ops | 主动超越 | 局部闭环（SA-05/SA-06） |
| 28 | 输出样式/statusline | output style 注入系统提示（`outputStyles/`） | 前端表达层承担（userFacingRuntimeStatus 人性化） | 可接受差异 | 闭环 |
| 29 | 会话恢复中断检测 | TurnInterruptionState（`sessionRestore.ts:409`） | terminal ghost 对账 + 尝试上限隔离（`web_chat_runtime.py:4880,4911`） | 工程增强 | 闭环 |
| 30 | 远程会话 teleport/CCR | 依赖 Anthropic 托管远程执行（`main.tsx:735-764`） | — | **排除**（供应商私有远程能力，依据充分） | 排除 |
| 31 | 文件历史跨会话复制 | resume 复制 file history（`fileHistory.ts:922`） | workspace 快照上限 50/1000 文件，跨会话文件版本 UI 缺失（UI-05） | 可接受差异 | 局部闭环 |

**基线账本结论**：31 条中闭环 22、局部闭环 8、缺失 0、排除 1。原报告把 FreeCode 的本地开发者 Hook settings 直接等同于 Hive 员工产品面，因此误报了一条“用户自定义 hook 缺失”。当前真实问题不是员工少了一个 Hook 配置入口，而是平台内部 Hook 被错误暴露且可由员工管理权限禁用。是否建设受治理的 Developer/Extension Hook 面，必须先有独立产品与权限契约；在该契约成立前，不得把员工面“不支持自定义 Hook”记为缺失，也不得据此声称 Hook 语义已经完整对齐。

---

## 5. 单 Agent 审查

### 5.1 闭环能力（择要，证据见 §4 账本）

模型循环、工具治理、compaction 三道、恢复、取消幂等、Plan Mode、Subagent、Work Ledger、Skill 渐进披露均为闭环。其中超出基线、必须保持的实现：

- **durable web chat run**：先 commit `RuntimeTask(pending)` 再唤醒 worker，执行与 socket 解耦；启动恢复扫描 + SKIP LOCKED reclaim + ghost 对账（`web_chat_runtime.py:2059,2429,2807,4880`）。
- **active-run 唯一性**：会话 advisory 锁 + DB 部分唯一索引（`models/runtime_task.py:90-107`），并发消息降级为队列注入。
- **取消幂等**：三入口统一进 durable ControlInput，幂等键+确定性 control_uuid+fence CAS 终态结算（`session_live_input.py:401-455`、`session_control_input.py:991`）。
- **工具结果驱逐**：50KB/result、200KB/round，sha256+read_file 指针可精确找回，写失败保完整证据（`turn_orchestrator.py:2305-2320`、`engine.py:3523-3548`）。

### 5.2 断点与局部闭环

**SA-01（断点，主审复核确认）turn_token_budget 空转。**
- 断裂原子：Authority→Execution。严重级：中（资源护栏缺位，无越权、不削弱模型）。
- 证据：`kernel/contracts.py:153` 字段定义、`invoker.py:117-121` 派生（min(1M, max(8192, rounds×8192))）、`invoker.py:547` 填充、`recovery_manifest_store.py:336` 快照——但 turn_orchestrator 全文零读取；`engine.py:692 _turn_token_budget_message` 生产零调用（主审 grep 复核：全 backend 仅这 5 处命中）。
- 用户影响：无 `budget_run_id` 的裸 invocation 无 turn 级 token 护栏，只能跑到 200 轮上限；`AGENTS.md` "Turn-level token budget gates where runtime config provides a budget" 与源码矛盾（假完成声明）。
- 反证：`RuntimeBudgetedLLMClient` 是 budget_run_id 激活的 run 级独立预算道（`runtime_budget_llm.py:81-251`），已接入 web_chat/trigger/subagent/workflow——但两套机制互不知晓，不能证伪"config 提供的 turn 预算无消费者"；`test_engine.py:4495/4559` 在无门禁时真空通过（绿测试≠真实路径的教科书例）。
- 最小闭环：每轮 provider 返回后按 cache-miss 口径比较累计 token 与预算，超出用已有终态函数退出并持久化；或删除字段与死函数并修订 AGENTS.md。

**SA-02（局部闭环）双 rewind 语义并存**：projection rewind（同会话 `session_command_runtime.py:1239-1290`）与 branch rewind（新会话 `conversation_branch_service.py` mode="rewind"）语义不同、均有前端消费。非双事实源，但命令面需统一文档。

**SA-03（局部闭环）trigger/heartbeat 进程内执行、恢复需人工和解**：`_tick` 建 RuntimeTask(running) 后 `asyncio.create_task`（`trigger_daemon.py:2542`、`heartbeat.py:1502`），不经 claim worker；重启后 session-bound run 置 `needs_reconciliation` 不盲重放（副作用安全的设计裁决）。代价：重启后执行中的 trigger 醒不过来，需管理员介入，且该队列无 UI 曝光（未证实）。方向：纳入 LEASE_RECLAIMABLE + worker 分发（web_chat 已证明可行），或显式记录裁决并补运营面。

**SA-04（撤销“员工用户自定义 hook 缺失”，重分类为产品边界）**：FreeCode 的 Hook 是本地开发者 harness 扩展面，不能直接映射为企业员工设置。Hive 员工与其 Owner/Manager 应配置业务可理解的权限、审批、自主性和故障结果，不应知道或选择 `turn_stop`、`post_compaction`、handler key、required/advisory 等运行时实现。若 CCPlus 后续确认需要 Hook 扩展能力，应单独定义 Platform Developer/Extension 面：只承载显式安装的扩展，声明目的、事件、权限、数据可见性、副作用、审计、版本与回滚；平台内置 Hook 与扩展 Hook 必须分 namespace、分权限、分消费面，且不得开放任意本机命令。该扩展面尚无已接受产品契约，因此本报告不再把它登记为当前缺失；当前已证实缺陷记为 UI-09。

**SA-05（断点，主审复核确认）REST 带 confirmed_plan_id 启动 workflow 生产 500。**
- 断裂原子：Execution↔Acceptance。严重级：高（用户可见 500）。
- 证据：`api/workflows.py:501-517` 在 `confirmed_plan_id` 存在时调 PlanModeGate `action_kind="start_workflow"` → `plan_mode_core.py:1042` 抛 ValueError（`ACTION_KINDS` 四元组不含 start_workflow，`plan_mode_core.py:43-48`，主审 grep 复核确认）。
- 验收造假信号：`tests/api/test_workflows.py:405` 用 `fake_gate_check` 替换 seam 并断言 `action_kind=="start_workflow"`——绿测试钉住生产必炸的接线（fake 掩盖 wiring 的教科书例）。
- 最小闭环：`start_workflow` 注册进 `ACTION_KINDS`/`_ACTION_INTENT`（或改走 lease 直验），并把该测试改为真 gate 集成测试。

**SA-06（局部闭环）tool 路径 start_workflow 确认强度弱**：确认证据是 agent 自己所在 turn 的 `turn_id`（`handlers/workflow.py:165-180`），`claim_workflow_preview_record` 只校验非空。"preview 后须用户同意"靠工具描述约束，无机械强制；高风险动作另有 preflight/capability gate 兜底，故定性为确认强度弱于文档表述，非治理绕过。

**SA-07（局部闭环）ChatSession.summary 与 T2 summary 双摘要源**：`memory_service.py:1171-1231` LLM 写 DB summary 供 episodic 检索；T3 consolidator 只读 T2 包。同一 session 两份摘要无语义对账。方向：episodic 检索以 T2 读模型（`t2/read_model.py`）为权威，DB summary 降为 UI 投影。

**SA-08（局部闭环）T2→chat prompt 固化延迟**：新知识要等 heartbeat T3 core 或 dream 才进入 resident/wiki（设计意图），"刚说过的事下次对话记不住"的感知风险存在；`save_memory` 显式覆盖层是即时补救通道。建议 UI 曝光"已记住/待巩固"状态。

**SA-09（死代码，验收瑕疵）**：`handle_web_chat_disconnect`（web_chat_runtime.py:1742）、`start_heartbeat`（heartbeat.py:1612）、`ConnectionManager`（websocket.py:78-150）、`_claim_pending_reply_suffix_for_session`（websocket.py:206-232）、`GET /chat/{agent_id}/history` + 前端 `getChatHistory`（遗留 `web_{user_id}` 方案，潜在双事实源）、`llm_utils.py` 纯 re-export shim、`engine.py:3094/3135` 惰性 shim、`agent_work_ledgers` 死表（真实账本在 AGENT_DATA_DIR 文件）。

---

## 6. Hive Connect 与 Hive Native 审查

### 6.1 Hive Native 主体：记忆与自进化（闭环，Hive 最强差异化面）

T0→T2→T3→soul 全链路经生产路径验证（证据见各条目），且**全链路 LLM-primary、失败一律 hold**，是 Model Agency 的教科书实现：

- **T0**：`append_t0_session_event`（`memory/t0/ledger.py:98`）JSONL+MD 双投影 hash 链；web/trigger/delegation/subagent 经 control bus bridge（幂等+顺序闸门+sweep）；listener 常驻 `main.py:686-688`。
- **T2**：live 调用者=TURN_STOP/IDLE/TRIGGER_END/DELEGATION_END hook → `run_t2_segment_package_job`；三个 LLM（summary/labels/独立 review）；无模型配置时 held 不降级；崩溃恢复 job manifest + 启动 sweep + heartbeat sweep。
- **T3**：两入口（heartbeat direct core 120K 输入带 coverage / agent 工具）；Platform Gate 强制 `t2://`/`explicit://` provenance + review rubric + 原子事务提交；two-plane 目标文件（`memory/self|profiles|knowledge|milestones`），docs 的 episodes/user/worker/capabilities 已退役为 LEGACY_T3_FILES。
- **soul**：dream RuntimeTask → LLM IdentityPromoter + Soul Memory Gate review + Platform Soul Gate 物理检查（frozen charter/schema/source_refs/base drift）→ owner 审批门 → rollback 快照 + 原子写；kernel 消费（frozen prefix + compaction 后恢复）。
- **召回**：ActivationContext fail-closed（principal 解析失败即 blocked_authority）→ resident profile plane（超预算告警不裁剪）→ retriever（explicit overlay+wiki+DB summary 候选→敏感性剥离→LLM 语义选择 ≤5 条 + coverage receipt；模型失败 ref-only 不机械代选）。
- **skill 自进化**：候选（T3 capability marker）→ LLM 起草 → 硬验证（sandbox artifact gate + `run_evolution_verification`）→ 独立 LLM referee → 事务化 commit → provisional trial 写 rollback baseline → 模型评审 promote/rollback。`skill_candidate_loop_v1` 默认开。
- **写旁路证伪**：唯一写面 `explicit_overlay.py`（LLM gate）；工具描述明示 "Direct file edits under memory/ are refused"；未发现绕过 Platform Gate 的 T2/T3 直写。

### 6.2 Hive Native 断点与缺失

**HN-01（缺失，假完成声明）proactive_employee_loop 不存在**（主审 `ls` 复核确认）。AGENTS.md 宣称"heartbeat 准备低危工作、外部动作必经 Checkpoint"在源码中零实现。当前 heartbeat 已收窄为无工具执行器的纯 T3 固化（`heartbeat_t3_core.py:49-61`）。处理：文档改写为"已退役/已知缺失"，或按 charter+Checkpoint 设计重建——二者择一，不得继续宣称。

**HN-02（缺失，假完成声明）memory/policy_replay 不存在**（主审复核确认）。"policy tuning 必经 replay guard"无实现。

**HN-03（缺失/已退役）Objective 系统**：`objective_service.py`/`api/objectives.py`/`Objective` model 均不存在；继任者 `AgentSessionGoal` + `api/session_goals.py` + memory goal_terms + 前端 SessionGoalPanel 已闭环。退役成立，AGENTS.md 实体清单失真。

**HN-04（已知缺失）Enterprise Knowledge 未实现**：无 `search_company_kb/read_company_kb` handler；HR 工具明示 "Company knowledge is not implemented yet"（`handlers/hr.py:73`）；退役测试钉死无 enterprise_kb 路由；施工规格 `docs/company-knowledge-base-spec-2026-07-07.md` 未落地。公司知识需求降级为 unresolved knowledge debt（`hr.py:227-237`）。按北极星这是 Goal 2 的实质性缺口，但不是回归——标已知缺失。

**HN-05（局部闭环）Knowledge grant 无审计**：grant create/revoke 直接改 `KnowledgeGrant` 不写 AuditLog（`agent_knowledge.py:392/432`；同文件 proposals 决策有审计 `personal_knowledge_proposals.py:499/583`）。授权变更不可追溯——治理证据缺口，补 AuditLog 即可。

**HN-06（已退役，文档残留）**：`viking_client`、`knowledge_inject`、`extract_queue`、`extract_agent` 均已删除（主审 `ls` 复核确认），退役测试钉死（`test_company_knowledge_retirement.py:20-22`）。Personal KB tool-only 边界反而因此更干净：全仓无任何 prompt 组装模块 import knowledge model；replay 时内容替换为 pointer-only 投影（`web_chat_runtime.py:1314-1380`）。

**HN-07（局部闭环）charter 校准提案链孤儿**：`propose_charter_calibrations_from_feedback`（auto_dream.py:1991）及下游 `decision_trace.calibration_candidates` 外部消费零调用者。

**HN-08（增强空间）KB 检索纯词法**：PG tsvector+ilike（`personal_knowledge_index_search.py:89-96`），无向量/语义召回；召回质量依赖模型构造查询。非违规，是能力增强空间。

**HN-09/HN-10（可吸收缺失）**：hermes 的 session_search（FTS5 跨会话原文零成本回忆）与 verify-on-stop（编辑后无新证据时的有界追问，policy-only 不阻断）Hive 均无（grep 反向证伪）。均不违反 Model Agency，建议吸收。

### 6.3 A2A / Delegation

闭环面：send_message_to_agent（pair session+transcript 双写走唯一写入器）、delegate_to_agent（RuntimeTask 落库+重启恢复+结果投影父 session+唤醒通知）、委派权限收缩（tool_profile→allowed/excluded + DelegationToken 签发→governance 校验，子 agent 用自身 capability gate 非继承父权）、Lease（PG ON CONFLICT 原子，重放幂等，终态释放）、Agent Card/interoperability profile 诚实标注 `not_exposed`。

**A2A-01（断点，与 GV-01 同根）**：见 §7 GV-01。
**A2A-02（局部闭环）Signal 双后端读写不对称**：写端经 gateway 默认落 PG；`consume_subagent_signals`（subagent.py:1399）直读内存且无生产调用方（死 API）；`COORDINATION_BACKEND=memory` 时唤醒静默丢失。当前默认 postgres 下闭环成立；需删死 API+加 backend 切换契约测试。
**A2A-03（断点）A2A 协作组前端缺失**：后端 `api/a2a.py` 全端点+policy 消费闭环，但前端仅 `listCollaborators` 有消费者；create/invite/approve/reject/revoke 五个 API 无 UI 调用方（`domains/a2a.ts:52-73`）。跨 owner 组队实质不可用——后端闭环但用户无法介入，按 Evidence→Consumption 登记断点。
**A2A-04（缺失/死代码）**：Sentinel 全家零生产调用（AGENTS.md 宣称存在）；`AgentAgentRelationship` 孤儿表（A2A 权威已迁 AgentCollaborationGroup）。

### 6.4 Hive Connect（canonical 外部消费者与仓内 legacy bridge）

当前产品 Skill 安装的是 npm `@hiveclaw243/hive-connect`，不是仓内 `@hiveclaw243/hive-bridge`。两套客户端必须分开裁决，legacy 实现的缺陷不能外推为 canonical Hive Connect 缺失。闭环面：设备配对（user_code/device_code 哈希+15min+一次性）、bridge token（sha256+user/tenant 绑定+scope allowlist+可 revoke）、云→本地持久队列+WS 投递+幂等键、断线重连（delivery lease+stale 重排+5 次后 needs_reconciliation）、canonical 客户端本地 durable execution receipt、浏览器聊天 UI、文件上传。

**HC-01（断点，本面最重，主审复核确认）A2A 委派本地执行结果孤立。**
- 断裂原子：Consumption（孤立结果）。严重级：高。
- 证据：`messaging.py:1684-1857` 入队成功返回 `queued`；但无任何消费者把本地结果送回源 agent——`record_channel_result` 只写 channel event/目标 agent ChatMessage/span（`local_agent_channel_service.py:1666-1780`），委派无 RuntimeTask，`check_async_task` 查不到（`messaging.py:1887`）。主审 grep 复核：`record_channel_result` 仅被 `api/local_agent_channel.py:942,1090` 调用，无回流消费者。
- 用户影响：云端 Agent 委派本地任务后永远等不到回执，只能人去 Local Agents 页看——Hive Connect"不制造孤立结果"的合同被违反。
- 最小闭环：结果到达时向 sender agent 会话注入 inbound 事件/唤醒（复用 send_message_to_agent 回送或 RuntimeTask 恢复通道），委派返回给出可轮询的 message_id 查询工具。

**HC-02（撤销，不是当前 Hive Connect 断点）presence 假离线**：原报告只读取仓内 legacy mjs/py runner，遗漏 Skill 实际安装的 canonical `@hiveclaw243/hive-connect`。对应 `0.1.9` 源码在 `platform/hive/hive.go:31,387-392,489-500` 每 25 秒启动并发送应用层 `{"type":"ping"}`，所以“在线 runner 90 秒后必假离线”不能成立。真实设备长连与 UI presence 仍需 E2E receipt，但状态应是**未验收**，不是**缺失/断点**。

**HC-03（断点）file_download/file_upload 策略骨架未接线**：种子策略+scope 映射存在（`local_bridge_service.py:45-46`），但下载端点只验 token、上传只查 scope。管理员在策略面关闭 file_download 无效。

**HC-04（撤销，不是缺失）常驻服务实现**：`.agents/skills/hive-connect/SKILL.md:21,59` 安装并调用的是 external `hive-connect`。对应 `0.1.9` 源码 `cmd/cc-connect/daemon.go:16-105` 已实现 install/uninstall/start/stop/restart/status/logs，macOS `daemon/launchd.go:42-78` 已实现 plist 写入、`launchctl bootstrap` 与 `kickstart`，且相关 Go 包测试通过。`bin/hive-bridge.mjs` 的提示只描述 legacy `hive-bridge`，不能证明 canonical daemon 缺失。真实机器 install→restart→presence 仍属于验收缺口。

**HC-05（断点，死代码）legacy gateway poll 通道**：`client.py:66,81,93` 与 `client.mjs:63-87` 调 `/api/v1/gateway/poll|send-message|report`，**后端无 gateway router**（main.py 无挂载，全仓 grep 无该路径）；Python CLI 默认 transport=poll → 必然 404 死循环。建议退役 Python 平行实现。

**HC-06（局部闭环）**：span 在 `needs_reconciliation` 时永不关闭（停留 running，审计面出现永不结束的 span）；`desktop_*` 四 router 有真实逻辑但消费者在仓外 Desktop 客户端（未证实）；多实例部署时进程内 fanout 可能丢实时投递（未证实，需部署证据）。

---

## 7. 企业治理、安全与 AI 资产审查

### 7.1 闭环面

RLS 真实强制（60+ 迁移 ENABLE+FORCE、`database.py:491-496` pin + after_begin 重钉、strict 启动拒 superuser/BYPASSRLS、唯一旁路强制 reason）、`check_agent_access` 逐层判定 fail-closed、工具执行治理唯一入口（kernel 全部经 `execute_tool`→governance runner，无绕过旁路）、CapabilityPolicy 门（STRICT 映射 fail-closed + 启动 drift 审计）、GuardPolicy 精确机器契约 shrink-only、secrets Fernet+HKDF（DEBUG=false 无 master key 拒启动、API 仅回掩码尾 4 位）、AI 资产 revision/usage/rollback 接在真实变更点、审批票证一次性消费+hash 绑定+启动 reconcile、admin 面/调试面分离（`/admin/*` 全部 platform_admin，调试面不暴露给企业管理员）。

### 7.2 断点

**GV-01（断点，P0，主审复核确认）Preflight ASK → Checkpoint 死信，自治车道外部动作功能性不可执行。**
- 断裂原子：Evidence→Recovery→Consumption。严重级：阻断（P0）。
- 证据：`ToolRuntimeService.__post_init__` 默认回填 `InProcessCoordinationGateway`（`tools/service.py:737-738`，主审 Read 复核），`gateway_scope` explicit gateway 恒胜（`coordination_wiring.py:91-93`）→ preflight ASK 创建的 checkpoint（`service.py:1576-1592`）永不落 `coordination_checkpoints` PG 表（表存在且 workflow gate 在用）；全仓无 `get_checkpoint`/`escalate_expired_checkpoints` 的生产消费者，无审批 API、无前端。模型只收到 `[Preflight:ask] ... checkpoint=<id>` 文本，重试仍被 ASK（identity 不变）。
- 用户影响：trigger/heartbeat/任务执行车道的 `send_feishu_message` 等外部可见动作**永远执行不了**；`send_email`/`reply_email`/`plaza_*` 缺 `delegated_user_authorized` flag（`email.py:12-52`、`plaza.py:47-48`），连 web 聊天 delegated_user 车道也必被 ASK 卡死。进程重启后 checkpoint 蒸发，decision_trace 里的 checkpoint_id 成悬垂引用。
- 根因权威事实源：`tools/service.py` 网关注入决策 vs `coordination_checkpoints` 表。
- 最小闭环：preflight ASK 改走既有 session/enterprise approval 车道（复用 approval ticket 精确重放），或 checkpoint 持久化 PG + 批准 API + 批准后精确重放；删除孤立 checkpoint 创建以消除与 decision_trace 的双事实源。
- 反证记录：已查全部 `escalate_expired_checkpoints`/`get_checkpoint` 调用点，确认无任何读取方；无法反证。

**GV-02（断点，P0，主审复核确认）Approval×Preflight 顺序冲突，票据误记成功。**
- 断裂原子：Evidence→Recovery。严重级：阻断（P0）。
- 证据：`execute_approved` 走完整 pipeline，`execution_pipeline.py:589` preflight 无 approval 感知——票证目标是 external_visible 工具且 envelope 身份为 agent_bot（trigger 车道），或工具缺 `delegated_user_authorized` 时，preflight 必返 ASK（`action_preflight.py:173-181`）；更糟：ASK block 是裸文本无 `<tool_error>`，`service.py:1102-1104`（主审 Read 复核）判 `execution_status="succeeded"`——**效果未发生但票据记成功**，并触发 continuation 告知源会话"已完成"。
- 验收造假信号：`test_approval_execution_runtime.py` 全部 monkeypatch 掉 `execute_approved_tool`，无任何测试覆盖 approved-execution×preflight 组合。
- 最小闭环：approved 执行将 preflight 降级为 audit-only（approval 本身已是 confirm 事实），或 ASK 文本按失败完成票据；补真路径组合测试。

**GV-03（断点）Owner charter 未接入执行治理（主审复核确认）**：`AgentAccountabilityContext.action_posture`/`zone_for` 零生产调用（主审 grep 复核：仅 `agency_charter.py` 自身命中）；`tools/service.py:1689-1713` 对一切 external-visible 工具硬编码 `CharterZone.CONFIRM_FIRST`、其余 `FULL_AUTHORITY`。owner 自定义的 full_authority/confirm_first/never_do 只进 prompt 不做机械裁决——owner 放宽的动作仍被 preflight 拦下（治理粒度回退）。最小闭环：preflight 输入装配按 agent 查 charter（typed action id），zone 仍由平台裁决。

**GV-04（断点，P0，主审复核确认）platform_admin 跨租户化身无安全审计**：`tenant_middleware.py:88-91`（主审 Read 复核）+ `security.py:151-169` 允许 `X-Tenant-Id` 化身任意活跃租户，校验充分但全程无 `write_audit_event`（仅 bypass 的 logger.warning）。化身行为不可审计、不可追溯。最小闭环：化身建立时在 operator 审计面写 `platform_security.tenant_impersonation`（actor、target_tenant、request_id）。

**GV-05（断点，P0，主审复核确认）llm_proxy 无配额无计量**：`api/llm_proxy.py:73-152` 裸 httpx 转发，有 JWT+租户模型解析，但无 quota、无 token 计量、无 rate limit（主审 grep 复核：quota/token_tracker/rate_limit 仅命中 docstring "controls quota, metering"——典型假完成信号）。任意租户成员可经此端点用租户真实 provider key 无限消费，租户级/用户级 token cap 对此路径完全失效，成本不可归因。最小闭环：接入 `check_user_token_quota` + 按 SSE usage 记账 + 路由级 rate limit。

**GV-08（断点，P0，Codex current-source 复核新增）凭据模式正则替代权威 secret 事实并产生整调用 REFUSE**：见 §3.3。断裂原子为 Authority→Execution→Acceptance；它既会误伤含示例凭据的 benign 文档/模板，也把测试钉在了 Model Agency 禁止的硬结论上。必须按可信 credential binding 做精确未授权字节识别，并补模型输出保真与 benign-text 回归。

### 7.3 局部闭环

- **GV-06 审计双汇**：租户安全事件→`security_audit_events` 哈希链（有查询/导出/验链 API）；operator 事件→`audit_logs` 无链；`write_audit_log` 失败仅 logger.error 吞掉；`desktop_audit.py` 客户端自报事件低信任直写。验链 API 只覆盖租户链。建议 operator 安全事件迁入哈希链或独立 operator 链。
- **GV-07 quota 故障全阻断**：quota 检查异常时 fail-closed 阻断整个调用（非仅受限效果，`invoker.py:1531-1537`）。显式资源不变量，登记为已知权衡；建议拆分"配额基础设施不可用"与"配额耗尽"两种 typed 状态并只对前者提供降级路径。
- **GV-09 DecisionTrace 消费弱**：preflight/决策落库闭环，但消费仅 session_feedback 回链，无 UI/API 列表端点；`DecisionTraceStore` 文件 JSONL 与 SQL store 双实现（生产用 SQL，文件版仅 feedback 默认回退）——兼容层堆积。

### 7.4 AI 资产

Agent/Skill/Workflow/外部能力资产管理闭环（revision/usage 投影接在真实变更点、rollback/reconcile 仅 admin、租户作用域）；Knowledge grant 缺审计（HN-05）；A2A 协作组与 Connector 治理有后端无前端（A2A-03、UI-02）。

---

## 8. 用户功能与 UI/UX 审查

前端消费面对账（源码级，未跑浏览器）：后端能力的前端消费总体**已有较强接线**——chat 传输（streaming/keepalive/断线重连/resume 同一事实/active run 状态）、Plan Mode 确认卡全状态机、Approval 卡、Session 权限卡、Workflow gate（approve/reject/repair/cancel/promote）、Subagent 状态与模型主导恢复、Work Ledger Dock、Session feedback、Workspace 文件 CRUD/上传/下载均有真实接线。多数高频路径做到了分层：operator_view 强制审计理由、operator drawer 门控、tool raw payload 默认折叠、状态人性化、UUID 标签抑制、token 图表仅 admin；但 UI-09 证明不能把“三受众分层整体合规”作为结论。**前端侧 Model Agency 零违规**（subagent 恢复走"请 agent 检查并重试"的模型主导路径，是正确范式），不等于产品抽象与权限消费面零缺陷。

**UI-01（撤销为产品断点，归入 DOC-01）Office 专用编辑面已显式退役**：`AgentDetailSections.test.tsx:2253-2265` 明确断言移除 Office tag、dedicated tab、`OfficeWorkbenchSection` 与 `activeTab === 'office'`，`ArchitectureSimplicityContract.test.ts:28-53` 再次锁定该退役边界。当前事实是 AGENTS.md 仍声称该面存在，因此属于文档漂移；除非新的产品 authority 明确要求恢复浏览器 WYSIWYG，否则不能把符合现行可执行合同的退役状态登记为实现断点。

**UI-02（断点，P1）三项治理 router 前端零消费**：`guard_policies`/`feature_flags`/`config_history` 后端 live 挂载（`main.py:858-890`）但无 api/domains 封装、无页面。治理能力仅 API 可达。

**UI-03（断点/Acceptance，P1）i18n 系统性欠账存在，但精确库存未证实**：原审查两种临时提取口径分别得到 349 与 306 个“双语均缺”key，且没有把可复现脚本、动态 key 规则与排除表留在仓内；“5 个文件 124 处中文硬编码”也缺少同一权威提取器。因此欠账方向与已抽样的中文/裸 key 风险成立，精确数字不能作为机械验收事实。完整闭环应先提交 AST-aware 对账器与 golden fixtures，生成唯一 inventory，再补齐双语并把 `missBoth=0` 作为 CI gate。

**UI-04（局部闭环）soul.md 全文无直接阅读入口**：workspace 浏览器 rootPath="workspace" 看不到 soul 正文；owner 只能审批候选，看不到当前 soul 全文。
**UI-05（局部闭环）文件版本历史 UI 缺失**：files API 无 version 端点，无回滚入口。
**UI-06（瑕疵）**：owner 审批卡直接 `JSON.stringify(details)`（`AgentApprovalsSection.tsx:64`）——Owner/Manager 受众不应见裸 JSON。
**UI-07（风险）**：`/design-gallery` 公开路由无鉴权无 env 门控（`App.tsx:126`），生产构建应移除或加守卫。
**UI-08（死代码）**：`LocalAgentLinkCard.tsx` 零消费者、`enterpriseApi.templates` 无页面消费者。
**UI-09（断点，P1）员工设置暴露内部 Hook，并把平台运行时禁用权下放给 `manage/owner`**：`AgentSettingsSection.tsx:444` 在普通“员工设置”中直接挂载 `HookRuntimeControlCard`；该卡片每 15 秒请求注册表（`HookRuntimeControlCard.tsx:20-25`），展示原始 `handler_name`、event、required/advisory、失败 receipt/error，并提供逐项 Enable/Disable（`:69-117`）。读取 API 只执行一般 `check_agent_access`，因此有该员工访问权的主体即可取得全局 Hook catalog、注册信息和近期失败 receipt（`api/hooks.py:120-162`）；写 API 允许 `manage/owner`、`org_admin/platform_admin` 修改，并把 per-agent 配置持久化后在运行时重载（`api/hooks.py:165-197`、`hook_runtime_config.py:40-94`）。前端测试还明确把“显示 required blocker + Disable hook”钉为正确契约（`HookRuntimeControlCard.test.tsx:58-77`）。这不是文案或视觉瑕疵，而是三重边界错误：

1. **产品抽象泄漏**：员工用户应理解“什么能力允许、何时审批、异常后如何恢复”，不应理解“哪个 callback 在哪个 runtime event 执行”。
2. **权威放错层**：T0 证据、pending reply、compaction、reflection、session lifecycle 等平台内置 Hook 属于运行时不变量；required Hook 不得按员工维度禁用，advisory Hook 也只有先产品化为业务能力后才可配置。
3. **消费面放错层**：员工/Owner 设置只显示业务政策和可行动的健康结果；Operator/Audit 面可显示聚合健康、影响与恢复动作；原始 handler/event/receipt 仅可在 Platform Developer 诊断面按需渐进披露。

最小完整闭环不是改名或把按钮藏起来，而是从员工设置移除整张卡片；收紧 raw GET/PATCH 权限与返回面；禁止 per-agent 禁用 required 内置 Hook；清理已持久化的违规覆盖配置；将员工可理解的运行时健康投影与内部诊断/扩展面分开建模。

**Frontend Experience Handoff 判断**：UI-02/UI-04/UI-05/UI-09 为确定性源码事实，其中 UI-09 还有用户提供的生产截图；i18n 精确库存与其余实际渲染、三受众信息层级、通知/恢复端到端体验无法纯源码定论——**建议输出有限范围 Frontend Experience Handoff**（见 §12），不需要全面重审。

---

## 9. 七原子矩阵与断点清单

### 9.1 七原子矩阵（能力域 × 原子，●=闭环 ◐=局部 ○=断点/缺失）

| 能力域 | 输入 | 权威 | 执行 | 证据 | 恢复 | 消费 | 验收 |
|---|---|---|---|---|---|---|---|
| 模型循环/kernel | ● | ● | ● | ● | ● | ● | ● |
| Web chat 会话生命周期 | ● | ● | ● | ● | ● | ● | ● |
| 工具治理平面 | ● | ◐(GV-03/08) | ◐(GV-01/02/08) | ◐(GV-06/09) | ◐(GV-01) | ◐(GV-09) | ○(GV-08 fake-pin 测试) |
| Plan/Subagent/Ledger/Skill | ● | ● | ● | ● | ● | ● | ● |
| Workflow | ● | ● | ○(SA-05) | ● | ● | ● | ◐(SA-05 测试) |
| Hooks | ● | ○(UI-09) | ◐(UI-09) | ● | ● | ○(UI-09 暴露错层) | ◐(禁用契约测试钉错) |
| Memory T0/T2/T3/soul | ● | ● | ● | ● | ● | ◐(SA-07/08) | ● |
| 自进化（skill/dream） | ● | ● | ● | ● | ● | ● | ● |
| 主动管理循环 | ○(HN-01/02 缺失) | — | — | — | — | — | — |
| A2A/委派 | ● | ● | ● | ◐(A2A-02) | ● | ○(A2A-03/HC-01) | ● |
| Personal KB | ● | ● | ● | ◐(HN-05) | ● | ● | ◐(真 PG skip) |
| Enterprise Knowledge | ○(HN-04 已知缺失) | — | — | — | — | — | — |
| 企业治理（RLS/配额/审计） | ● | ◐(GV-04) | ● | ◐(GV-04/05/06) | ● | ◐(UI-02) | ● |
| Hive Connect | ● | ◐(HC-03) | ◐(HC-05 legacy) | ◐(HC-06) | ● | ○(HC-01) | ◐(canonical device E2E 未验收) |
| 前端消费面 | ● | ◐(UI-09) | ● | ● | ● | ○(UI-02/04/05/09) | ○(UI-03/09) |
| 文档事实源 | — | — | — | ○(DOC-01) | — | — | ◐(DOC-02) |

### 9.2 断点登记册（按严重级排序；均含反证记录与最小闭环方向，详见各模块章节）

| 编号 | 模块 | 状态 | 严重级 | 断裂原子 | 一句话 |
|---|---|---|---|---|---|
| GV-01 | 工具治理 | 断点 | P0 | Evidence→Recovery→Consumption | preflight ASK checkpoint 内存死信，自治车道外部动作永不可执行 |
| GV-02 | 工具治理 | 断点 | P0 | Evidence→Recovery | approved 执行被 preflight 二次 ASK 且票据误记 succeeded |
| SA-05 | Workflow | 断点 | P0 | Execution↔Acceptance | REST confirmed_plan_id 启动 500，测试 fake 钉住 |
| GV-04 | 治理 | 断点 | P0 | Authority→Evidence | platform_admin 跨租户化身无审计 |
| GV-05 | 治理 | 断点 | P0 | Authority→Acceptance | llm_proxy 无配额无计量（假完成 docstring） |
| HC-01 | Connect | 断点 | P0 | Consumption | A2A 委派本地执行结果孤立，源 agent 永不知 |
| GV-08 | 工具治理 / Model Agency | 断点 | P0 | Authority→Execution→Acceptance | 凭据模式正则把 benign 参数整调用 REFUSE，测试钉住违规 |
| SA-01 | kernel | 断点 | P1 | Authority→Execution | turn_token_budget 派生但零消费者（+AGENTS.md 假完成） |
| A2A-03 | A2A | 断点 | P1 | Consumption | 协作组后端闭环前端零消费，跨 owner 组队不可用 |
| GV-03 | 治理 | 断点 | P1 | Authority→Execution | owner charter zone_for 零调用，preflight 硬编码 CONFIRM_FIRST |
| UI-09 | Hooks / 前端 | 断点 | P1 | Authority→Execution→Consumption | 员工设置暴露内部 Hook，并允许 manage/owner 持久化禁用 |
| UI-02 | 前端 | 断点 | P1 | Consumption | guard_policies/feature_flags/config_history 无 UI |
| UI-03 | 前端 | 断点 | P1 | Acceptance | i18n 欠账存在，但唯一可复现 inventory/CI gate 缺失 |
| DOC-01 | 文档 | 断点 | P1 | Evidence | AGENTS.md 事实源大面积漂移（详见 §10.2） |
| HN-01/02 | Native | 缺失 | P1（文档） | — | proactive_employee_loop / policy_replay 不存在 |
| HN-04 | Native | 已知缺失 | P2 | — | Enterprise Knowledge 未实现（规格存在） |
| HC-03 | Connect | 断点 | P2 | Authority→Execution | file up/download 策略种子无执行点 |
| HC-05 | Connect | 断点（死代码） | P2 | Execution | Python 客户端默认 poll 已删除的 gateway → 404 循环 |
| HN-05 | KB | 局部闭环 | P2 | Evidence | grant 创建/吊销无 AuditLog |
| SA-02/03/06/07/08、A2A-02、GV-06/07/09、HC-06、UI-04/05/06、DOC-02 | 各 | 局部闭环 | P2 | 各异 | 见各模块章节 |
| A2A-04、HN-07、SA-09、UI-08 | 各 | 死代码/孤儿 | P3 | — | 无生产消费者，可删除（见 §10） |

### 9.3 撤销或重分类项

| 编号 | 原始结论 | current-source 复核裁决 |
|---|---|---|
| HC-02 | canonical runner 不 ping、90 秒后必假离线 | **撤销**。`@hiveclaw243/hive-connect@0.1.9` 每 25 秒发送应用层 ping；仅真实设备 presence E2E 未验收 |
| HC-04 | `hive-connect daemon install` 未实现 | **撤销**。canonical CLI、launchd/systemd/Windows service manager 均有实现；仅真实机器安装与重启 E2E 未验收 |
| UI-01 | Office 专用浏览器编辑面缺失是产品断点 | **重分类为 DOC-01**。当前前端测试合同明确要求退役该面；AGENTS.md 声明未同步 |
| SA-04 | 员工用户自定义 hook 无注册面是 P2 缺失 | **撤销为员工产品缺失，重分类为 UI-09**。本地开发者 Hook 不能直接映射到员工设置；当前缺陷是内部 Hook 与禁用权被暴露，受治理扩展面需另立产品契约 |

---

## 10. 代码极简性与目标架构建议

### 10.1 可删除/合并清单（保留能力不变，每项均给出迁移与回归证明方式）

1. **InProcessCoordinationGateway 作为 ToolRuntimeService 默认回填**（`service.py:737`）——GV-01 根因。改为惰性 `gateway_scope` 决策或删除；回归：preflight ASK 集成测试走真 PG gateway。
2. **preflight CoordinationCheckpoint 写入**（`service.py:1576-1592`）——无消费者；与 decision_trace 构成双事实源。按 GV-01 闭环方向二选一后删除另一半。
3. **Sentinel 全家**（`agents/coordination.py` 约 60 行+单测）——零生产消费者。删除；AGENTS.md 同步。
4. **`AgentAgentRelationship` 孤儿表**（`models/org.py:80`）——已被 AgentCollaborationGroup 取代。迁移删除（alembic drop + db_bootstrap 清理）。
5. **`agent_work_ledgers` 死表**（`models/work_ledger.py`）——真实账本在 AGENT_DATA_DIR 文件。退役或接线，二选一，消除双事实源隐患。
6. **`consume_subagent_signals` 内存读取 API**（`subagent.py:1399`）——与 wake consumer 双事实源，保留 PG 版。
7. **DecisionTrace 文件 JSONL store**（`decision_trace.py:53`）——生产用 SQL；文件版仅 feedback 默认回退。收敛为 SQL 单实现。
8. **`services/t0_logger.py`（1240 行 legacy 层）**——与 `memory/t0/ledger.py` 并存；收敛为 ledger + 一次性迁移脚本。
9. **local_bridge Python 平行实现 + legacy gateway 客户端方法 + poller**——npm 是 skill 安装路径，Python 包无发布消费者且默认通道 404。退役。
10. **`hive_bridge_auto_adapter.py`**——关键词决定任务行为的 demo 脚本（典型 Model Agency 违规），不在生产路径，删除。
11. **前端死代码**：`LocalAgentLinkCard`、`getChatHistory`+`GET /chat/{agent_id}/history`（遗留双事实源）、`officeApi.createDocument`（在现行 Office 专用面退役合同下先核实无其他消费者，再退役）、`enterpriseApi.templates`（若无页面则删）、`/design-gallery` 公开路由（加守卫或移除）。
12. **后端死代码**：`handle_web_chat_disconnect`、`start_heartbeat`、`ConnectionManager`、`_claim_pending_reply_suffix_for_session`、`llm_utils` re-export、`engine.py:3094/3135/692`（SA-01 接线或删除时处理）、`direct_fallback_executor` 死字段、`agent_tools._execute_tool_inner`、heartbeat/auto_dream 内已退役循环残骸（`_build_evolution_context` 等 6 个）、charter 校准孤儿链（HN-07）、ELICITATION 无生产者分支、retriever 恒空 hook（`_retrieve_semantic_backend`/`_retrieve_external`）、`memory_service.on_conversation_start/end` 无调用方 wrapper。
13. **裸 subprocess 旁路**（非 Agent 控制代码执行但绕过 env 政策）：`agent_tool_domains/feishu_cli.py:31`、`external_capabilities/materializer.py:616`（git clone 继承全量 os.environ 含密钥）——至少过 `sanitize_agent_execution_env`。
14. **双目录 pack**：`packs/personal_knowledge_pack/pack.yaml` 与 `backend/packs/...` 逐字节相同，保留单一来源；`skill-package/` 与 `skills/` 两份 hive-bridge SKILL.md 同理。
15. **api/config_history.py**——已 410 退役的纯转发兼容适配器，可删。

### 10.2 文档事实源修复（DOC-01，P1）

AGENTS.md 必须按当前源码改写以下条目（均经 current-source 复核）：迁移数 79→178；前端测试 39→123；删除 `scheduler` 服务条目（schedule=trigger 行）；删除 `extract_queue`/`extract_agent`/`knowledge_inject`/`viking_client` 条目；删除或重建 `proactive_employee_loop`/`policy_replay` 条目；Objective 核心实体改为 AgentSessionGoal；删除已被前端可执行合同明确退役的 `OfficeWorkbenchSection` 声明，除非另有新的产品 authority 要求重建；修订 "Turn-level token budget gates"（SA-01）；删除 Sentinel/AgentAgentRelationship 宣称；刷新测试基线数（4223→本次实测，见 §14）；ONLYOFFICE env 表按当前消费路径改写。规约建议：Codebase Stats 具体数字改为生成或删除，避免再次漂移。

### 10.3 目标架构判断

未发现需要架构重做的点。现有分层（kernel 无 DB / ToolRuntimeService 唯一执行面 / RuntimeTask 唯一后台执行记录 / ChatTranscriptEvent+T0 双投影 / Memory Gate+Platform Gate 双门）是健康的能力保持型结构。三个结构性收敛建议：

- **确认车道统一**：session permission、enterprise approval ticket、workflow gate、preflight ASK 四种"请求人类确认"机制应共享同一票据与重放底座（approval ticket 已具备单次消费+hash 绑定+精确重放），preflight ASK 并入即同时修复 GV-01/GV-02/GV-03。
- **Secret egress 回到精确事实边界**：PrivacyLayer 的模式识别只能提供候选/audit 信号；硬拒绝必须来自当前 principal 无权披露的真实 credential bytes 或可信 secret reference。对最终表达只遮蔽精确禁止字节，不得因自然语言像 secret 而重写或拒绝整个工具调用。
- **execpolicy 与 unified exec 吸收**（Codex 增量，不冲突 CC 语义）：命令级声明式策略 DSL（`codex-rs execpolicy/src/decision.rs`）补 GuardPolicy 与 capability 之间的粒度空档；PTY/持久 shell 会话（`core/src/unified_exec/`）补长交互式命令场景（当前 run_command 一次性）。turn diff tracker 为次要 UI 增量。

---

## 11. Eval Handoff 与待证明能力

以下事项仅靠本次源码与运行审查无法充分证明，必须交接独立 Eval/Acceptance 阶段；这些不是“修完 P0 后可跳过”的附加项：

1. **单 Agent 智能对标 hermes 的端到端质量主张**（北极星 Goal 1 核心）。已有证据：记忆/压缩/技能机制源码闭环且多处更强。缺口：无行为级对比 trace。应比基线：hermes-agent 当前 checkout。必须机械验证的硬不变量：记忆召回命中后续 turn、skill 晋升后真实被加载使用；开放判断：回答质量。环境：双 checkout + 相同模型。不验证的风险：Goal 1"至少一样好"停留在架构宣称。
2. **真 PG 故障注入**：reclaim exactly-once（`test_runtime_task_claim_fencing_postgres.py` 本次 Docker-off skip）、RLS 跨 owner 拒绝、迁移回填（97 个真 PG 测试文件本地静默 skip）。已有证据：CI ubuntu runner 有 Docker；缺口：本 checkout 无运行证据。不验证的风险：多 worker 并发与 RLS 边界缺陷被 skip 掩盖。
3. **多进程部署行为**：Redis cancel bus cross_process 分支、WS fanout 进程内实现多实例丢失、`_summary_breaker` 进程内 dict。需双进程验收证据。
4. **GV-01/GV-02 修复后的自治车道端到端**：agent_bot 车道外部动作 ASK→人类批准→精确重放→效果发生→票据正确。当前无证据（功能不可执行）。修复后必须补真路径组合测试，不得再 monkeypatch 掉执行 seam。
5. **GV-08 Model Agency 修复验收**：benign 文档/fixture 中出现 `api_key=sk-...`、真实活跃 secret、嵌套参数、工具出参与最终表达都要覆盖；硬拒绝只能由精确 unauthorized bytes/refs 驱动，非禁止字节保持 byte-faithful。
6. **KB 检索召回质量**：词法检索在真实 owner 语料上的命中率；L2 发现链（tool_search→schema 扩展）生产命中率无 trace。
7. **Hive Connect 真实设备端到端**：canonical `@hiveclaw243/hive-connect@0.1.9` 的 ping 与 daemon source 已验证；仍需在真实登录设备验证 install→restart→presence、消息/文件/回执，以及 Desktop 对 `desktop_*` router 的真实消费。
8. **触发器/heartbeat needs_reconciliation 队列的运营闭环**：恢复后人工和解是否真实可操作（无 UI 入口证据）。

## 12. Frontend Experience Handoff

**需要，但限定范围**。确定性产品缺口集中在 UI-02（三治理面无 UI）、UI-04（soul 可见性）、UI-05（文件版本）与 UI-09（Hook 实现和权威暴露错层）；Office 专用面属于 DOC-01 文档漂移，不是默认恢复项。UI-09 的交接边界已经明确：普通用户与 Owner/Manager 只消费业务政策、可理解的健康结果和恢复动作；Operator/Auditor 消费聚合证据；raw handler/event/receipt 仅在 Platform Developer 诊断面按需披露，且 required 平台 Hook 不可按员工禁用。真实浏览器阶段还要验证：①员工设置不再出现 Hook 名、event、failure mode、raw receipt 或 Enable/Disable；②用一个入库的 AST-aware 脚本建立唯一 i18n inventory，再检查英文受众实际渲染；③三受众在 live、reconnect、reload、history、resume 下的信息层级与恢复操作体验。交接 `ccplus-frontend-product-review-prompt.md` 时不得预设恢复已退役的 Office tab，也不得把“自定义 Hook”设计回员工设置。

## 13. 完整落地方向与验收矩阵

| 项 | 最小完整闭环方向 | 迁移/回填/清理 | 验收要求 |
|---|---|---|---|
| GV-01+GV-02+GV-03（确认车道统一） | preflight ASK 并入 approval ticket 车道（单次消费+hash 绑定+精确重放）；approved 执行 preflight 降级 audit-only；preflight 装配按 agent 查 charter | 内存 checkpoint 存量无持久化无需回填；删除 InProcess 默认回填与孤立 checkpoint 创建 | 真路径组合测试（禁 monkeypatch 执行 seam）：agent_bot 车道 ASK→批准→效果发生→票据 succeeded；拒绝→票据 failed；charter full_authority 动作不被拦 |
| SA-05 | `start_workflow` 注册 ACTION_KINDS/_ACTION_INTENT | — | fake_gate 测试改真 gate 集成测试；REST confirmed_plan_id 启动 200 |
| GV-04 | 化身建立时写 operator 审计事件 | 历史化身无证据可回填（如实声明） | 审计面可查 impersonation 事件含 actor/target/request_id |
| GV-05 | llm_proxy 接入 quota+SSE usage 记账+rate limit | — | 配额耗尽 typed 拒绝；token_tracker 出现代理路径用量行 |
| GV-08 | 把模式扫描降为候选/audit；硬拒绝仅消费当前 principal 无权披露的精确 secret bytes/refs，输出只遮蔽精确禁止字节 | 识别并删除钉住 benign 模式误伤的测试契约；无需数据迁移 | benign 示例可写入；真实未授权 secret fail-closed；嵌套参数与最终表达保持非禁止字节 byte-faithful |
| HC-01 | record_channel_result 增加向 sender agent 会话的 inbound 注入/唤醒；委派返回可轮询 message_id | — | 委派本地执行→源 agent 被唤醒消费结果的端到端测试 |
| SA-01 | 接线 turn budget gate（cache-miss 口径）或删除字段 | — | 有预算时超限 typed 终态+持久化；test_engine 两个真空测试改真断言 |
| A2A-03 | 协作组五操作补前端（AgentA2ASection 扩展） | — | create→invite→approve→revoke 全流 UI 可达 |
| HC-03/05 + canonical E2E | file 策略接执行点；退役 Python/legacy gateway 实现；不重做已存在的 ping/daemon | — | 策略关闭即拒；真实设备 install→restart→presence；legacy 404 poll 不再可达 |
| UI-09 + SA-04 | 从员工设置移除 Hook registry/card；raw catalog/receipt 与 mutation API 收紧到 Platform Developer/Operator 的独立诊断边界；required 内置 Hook 不允许 per-agent disable；若建设扩展面，先定义独立安装、权限、审计、版本与回滚契约 | 盘点并清除已持久化的 per-agent 内置 Hook disable/failure-policy 覆盖；保留审计与可恢复记录 | 普通用户/Owner/Manager 的页面和 API 均不见 raw Hook；required 内置 Hook 无禁用路径；Operator 只见可行动健康投影；Developer raw 诊断需显式权限；旧禁用配置完成 dry-run 清单、确认后清理；相关测试反转错误契约 |
| UI-02/04/05 | 三治理面补页面；soul 只读视图；文件版本入口 | — | 各面 UI 可达、权限正确并有浏览器验收 |
| UI-03 | 先提交唯一 AST-aware inventory 脚本，再按其结果补双语并清理中文硬编码默认 | — | extractor fixtures 绿；inventory 可复现；`missBoth=0` 入 CI |
| DOC-01 | AGENTS.md 按 §10.2 改写 | — | 数字改为生成或删除；feishu 测试硬编码绝对路径改相对（当前在健康 checkout 必红） |
| SA-03 | trigger/heartbeat 纳入 worker 分发或曝光 needs_reconciliation 运营面 | — | 重启后 trigger run 自动恢复或运营队列可操作 |

## 14. 实测证据、未证实项与发布边界

### 14.1 本次实测验收证据（原审记录 + Codex 复核）

- `alembic heads` → **单头** `completion_outbox_index_0721`；文件系统计数为 **178 个迁移文件**。原报告的“140 个含回填”未在本次复核中建立统一机械口径，不能与已复现事实混写。
- Codex 复跑 `.venv/bin/pytest tests -q --tb=short`：**7089 passed / 594 skipped / 4 failed（60.51s）**，与原报告数量一致。4 个失败逐一定性：①`test_feishu_streaming_cards` 硬编码已失效绝对路径；②③`test_agent_native_repair_ledger` 与 ④`test_schema_startup_gate` 因当前 `.git` worktree 指针损坏而在 `git ls-files` 失败。594 skipped 含 Docker-off 真 PG 测试，不能视为 acceptance green。
- 当前规模复现为 **923 个 test 文件 / 7278 个 test function 定义 / 7687 个 pytest collected cases**。AGENTS.md "4223 passed" 基线已过时。
- 直接调用 `intent_type_for_action("start_workflow")` 可复现 `ValueError`；但 `test_confirmed_plan_is_consumed_for_the_exact_workflow_preview` 因 fake gate 仍绿。`test_tool_runtime_service_preflight_refuses_credential_arguments` 也仍绿，证明测试正在钉住 GV-08 违规。两项 targeted tests 合计 **2 passed**，不能作为路径正确证据。
- `npm view @hiveclaw243/hive-connect version bin` 返回 **0.1.9** / `hive-connect: run.js`；对应源码 `npm/package.json` 同版本。`go test ./platform/hive ./daemon ./cmd/cc-connect` 三包全部通过，反证 HC-02/HC-04 的“未实现”结论。
- 子审实测：kernel+invoker 195 项全绿；knowledge 平面 134 项全绿。

### 14.2 未证实项汇总

T0 磁盘实物抽样与 T0_STARTUP_BACKFILL 生产执行；Redis 传输不可用时 bridge 仅靠 sweep 兜底的行为；后台 subagent 完成唤醒 payload 上限；`skill_candidate_loop_v1` 生产 FeatureFlag 行真实状态；tenant 是否真对 external_visible 配置了 requires_approval（GV-02 触发面）；MCP `row.config["api_key"]` 明文 vs 加密；enterprise.py 32 端点逐一租户作用域；审批无人值守恢复全程；desktop_* 消费者；canonical Hive Connect 的真实机器安装/自启/presence；FreeCode 运行期行为；最近一次 CI run 状态；多实例 Railway 部署拓扑。

### 14.3 残余风险

git 元数据损坏使本次无法做变更面分析，也无法排除"工作树与某 HEAD 漂移"；4 个测试失败中 feishu 测试遮盖了 cardkit 主流程断言；594 skip 中可能藏着只有真 PG 才暴露的缺陷；前端浏览器行为、生产 DB、Railway、多进程与真实 Hive Connect 设备态均未经实测。上述缺口意味着本报告可以给出 NO-GO 与修复清单，但不能给出 GO。

### 14.4 证据边界声明

不再使用“约 90%”这类不可机械复算的单值置信度。当前证据分层如下：六个原始 P0 的代码事实与新增 GV-08 有 current-source 证据；HC-02/HC-04 已被 canonical source 反证；UI-01 已被当前可执行测试合同重分类；UI-09 有用户提供的生产页面截图、前后端 current-source 接线和错误契约单测三类证据，但本次未独立操作浏览器；测试、迁移头与包级 Go tests 有命令 receipt。Goal 1 行为质量、生产 DB、浏览器端到端、多进程、Railway 和真实设备态仍未证实。发布裁决必须消费 §11 中与本次变更相关的全部 receipt，不能只取前三项或把 full-suite 计数当替代品。

---

## 15. Current-source 复核修订记录

| 修订 | 原报告 | 当前裁决 | 复核证据 |
|---|---|---|---|
| C-01 | Model Agency 零违规，GV-08 仅观察 | GV-08 为 live P0 违规 | `privacy_layer.py:118-145` → `service.py:1676-1678` → `action_preflight.py:122-129`；违规测试可复现 |
| C-02 | HC-02 canonical runner 不 ping | 撤销 | `hive-connect@0.1.9` 的 `platform/hive/hive.go:31,387-392,489-500`；Hive adapter tests 通过 |
| C-03 | HC-04 daemon install 未实现 | 撤销 | `cmd/cc-connect/daemon.go:16-105`、`daemon/launchd.go:42-78`；daemon/CLI tests 通过 |
| C-04 | UI-01 Office 专用面缺失是 P1 产品断点 | 重分类为 DOC-01 | 两个前端测试合同显式要求退役该 tab/section |
| C-05 | i18n 精确缺失数为 349 | 精确库存未证实 | 原审两种口径为 349/306，未留下唯一可复现提取器 |
| C-06 | 修复六个 P0 后可进入 GO | 当前仍为 NO-GO / Acceptance incomplete | 七个 P0 + Goal 1/PG/多进程/浏览器/Railway/真实设备验收缺口 |
| C-07 | 员工用户自定义 hook 无注册面是 P2 缺失 | 撤销该员工产品缺失；新增 UI-09 P1 边界断点 | `AgentSettingsSection.tsx:444`、`HookRuntimeControlCard.tsx:20-117`、`api/hooks.py:120-197`、`hook_runtime_config.py:40-94`、错误契约单测及用户提供的生产截图 |

### 附：审查过程声明

原始报告由 Kimi 的并行只读源码审计、主审复核与实跑验证合成。Codex current-source 复核发现原报告并非所有关键断点都完成了正确 source-boundary 与 product-boundary 反证，因此已在正文、矩阵、登记册、落地方向和验收边界中同步修订，并保留撤销项追溯。整个复核只修改本报告，没有修改业务代码、数据库或部署配置。
