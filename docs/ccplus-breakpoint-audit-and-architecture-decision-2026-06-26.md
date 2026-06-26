# CCPlus 技术断点全景台账 + 架构决策(CLI / Rust)

Date: 2026-06-26
Status: canonical breakpoint ledger + architecture ruling + fix evidence
Scope: CCPlus V1 单 Agent runtime 收口后的全系统断点取证 + "是否做 CLI / 是否 Rust 重写核心" 决策

本文档由 9 个并行取证审计 agent(8 子系统 + 1 参照基线)的 file:line 证据综合而成。审计镜头统一为本仓库的标志性失败模式:**契约种下未接线(seeded-not-wired)/ 绿测试钉死死路径 / 文档高估成熟度 / 静默吞错当主路径**。

---

## 0. 一句话结论

所有真实技术断点,**没有一个是语言/性能问题**。它们全部归为三个病根:① session-native 闭环只做了一半(工作发生了,但产物没进 session 真相面);② 契约种下未接线(仓库标志性失败模式);③ 多租户隔离与自进化两条北极星命门"机制就绪但生产从未验证"。

因此:
- **Rust 重写核心 = 用最贵手段解一个 Hive 没有的问题**(对标质量标杆 FreeCode/CC 的核心本身就是 TypeScript)。否决,仅保留"profiler 证明后的底层 T0/transcript 局部加速"这一未来口子。
- **CLI 的直觉是对的,但翻译要换**:不是做 CC 式 CLI 产品,而是基于已经 headless 的 `invoke_agent` 做一个薄 **headless session runner**(对标 `codex exec`/FreeCode `-p`)当验证/eval/开发 harness,并把 session/T0 信封做成完整可回放真相面。

---

## Part A — 架构决策

### A.1 参照基线的真实形态(源码取证)

| 基线 | 核心语言 | 入口 | 核心 vs 客户端边界 | "编译语言"承担什么 |
|---|---|---|---|---|
| **FreeCode / CC** `free-code-main` | **TypeScript(Bun)** | bin `claude → ./cli`;`src/entrypoints/cli.tsx` | `src/query.ts` 的 `query()` 异步生成器是核心 loop;Ink TUI(`REPL.tsx`)与 headless `-p`(`cli/print.ts` 经 `QueryEngine.ask()`)共用同一核心 | 几乎无——核心全 TS;native/wasm(ripgrep/yoga/xxhash/sharp)只是叶子工具/渲染/哈希 |
| **claw-code Rust** `claw-code/rust` | **Rust**(~104K 行,12 crate) | bin `claw`;`rusty-claude-cli/src/main.rs` | `runtime`(41K 行,agent loop+session+权限+mcp+sandbox 高度聚合)+`api`(HTTP 模型调用)+`tools`(工具执行);`rusty-claude-cli` 是终端客户端(rustyline/crossterm/syntect) | 全部——agent 循环、LLM 流式调用、工具执行、权限、session JSONL/rotation/fork/compact、MCP、sandbox、OAuth |
| **Codex Rust** `codex/codex-rs` | **Rust**(93 crate) | `cli/src/main.rs` 多工具;`exec`(headless)+`tui`(交互)并列 | `codex-core`(95K 行,**零 ratatui/crossterm 依赖**,显式以"可复用 library crate"为目标);`tui` 与 `exec` 是 core 之上两个并列客户端 | 全部核心,但拆成几十个边界清晰的库 crate |

### A.2 三者真正的共同不变量

**不是语言(分裂:1 TS / 2 Rust),而是 "headless 核心 + 薄客户端"**:agent loop + 模型调用 + 工具执行 + session/compaction 收敛进一个**零终端渲染依赖**的核心,交互 TUI 与 headless/JSON automation runner(`-p` / `prompt` / `exec`)是同一核心之上的两个客户端,且都暴露 JSON/stream-json 结构化输出供自动化。三者的核心稳定性来自"headless 边界清晰可脚本化"这一结构共性,**不是来自某种语言**。

### A.3 Hive 自身的核心边界事实

- `invoke_agent()`(`runtime/invoker.py:1140`)→ `AgentKernel.handle(request) -> InvocationResult`(`kernel/engine.py:2308`)**已经是纯 DI、零 DB import 的 headless 引擎**(`KernelDependencies` at `engine.py:239`)。
- **已被证明能脱离 web server 跑**:eval `app/evals/hive_live_runner.py` 从 `__main__` headless 驱动 `invoke_agent`。
- Hive 缺的不是"核心能否 headless",而是**没有面向用户的 agent CLI/REPL**;且 Hive 的 headless 是**服务端** headless(要 Postgres/租户上下文/agent 记录),不是 CC 那种纯本地跑文件的 CLI。

### A.4 裁决

**(a) CLI**:不做 CC 式 CLI 产品(对 Hive=多租户 web 控制中台是 scope creep)。做一个薄 **headless session runner**,基于现成 `invoke_agent`,对真实 session 跑一轮并吐完整 session/T0 JSON 信封——**作为验证/eval/开发 harness,不是产品**。价值:① 直接打击病根 A(信封解释不了的 runtime effect = session-native 断点,当场暴露);② 给自进化(C-2)一个确定性可脚本化 runner 去生产击发 live eval;③ 零核心改动(核心已 headless)。Codex `core`+`exec`+`tui` 即模板:`invoke_agent`=core,runner=exec,Web UI=tui-等价客户端。

**(b) Rust 重写核心**:**否决**。理由(证据级):
1. 断点无一是语言问题——全是接线纪律/闭环完整度/部署事实/生产验证。Rust 重写只会产出"未接线的 Rust",同病换贵语言。
2. 参照基线证伪"核心稳定要靠 Rust":质量标杆 **FreeCode/CC 核心是 TypeScript**,compaction/token budget/tool loop/session 全在 TS 实现。
3. kernel 审计确认 Python 核心**已是"晴天真闭环"**;重写一个正常工作的核心冻结 Goal-1/Goal-2 数月、巨大回归风险,违反 KISS/YAGNI 和"Goal-1 优先夯实"。
4. AI-Native L1(发挥模型)是提示词/上下文/预算工程,任何语言一样;模型调用是 HTTP,瓶颈是 LLM 延迟不是 Python。
5. claw-code/Codex 用 Rust 的前提是"产品本身是本地单二进制 CLI 工具";Hive 是 Railway 服务端多租户 web 平台,Rust 单二进制/启动/内存优势用不上,Python 生态(FastAPI/SQLAlchemy/async/LLM 生态)是真资产。

**唯一保留口子**:仅当 profiler 真测出某 CPU-bound 热稳定底层机制(T0 JSONL append/hash-chain/rotation、大规模 transcript replay/fork/compact、tokenizer)有瓶颈时,做**附加式可选**优化(claw-code `runtime` crate 占的生态位),**永不碰 agent loop/治理/记忆判断**。当前零证据存在此瓶颈(全量测试 88s,无任何性能抱怨)。

---

## Part B — 技术断点全景台账

本次工作树全量测试 **5260 passed / 2 skipped / exit 0**(本次幸运绿;B-1 是被测试顺序依赖掩盖的真断点)。

### 病根 A — session-native 闭环只做了一半(2026-06-25 台账 P0/P1 的代码坐实)

| # | 断点 | 证据 | 判定 | 严重度 |
|---|---|---|---|---|
| A-1 | **Deep Research 零 session artifact**:报告+来源只落 workspace 文件,DR 全树 grep `append_session_event/chat_artifact_delivery` 为空 | `deep_research/workflow_definition.py:263-289`、`leaf_presets.py:210/646` | 声称但死 | **Critical** |
| A-2 | **Workflow 完成只投状态行**,outputs/leaf artifact/deliverable 从不进 session(走 channel push+Signal,在时间线外) | `workflow_runtime_service.py:788-799` vs `:1093/:1117` | 声称但死 | High |
| A-3 | **A2A peer/async delegation 是 result-summary-first**,父时间线无完成投影(subagent 路径已解决、delegation 没做) | `agents/orchestrator.py:1350/1499`、`messaging.py:1254` | 缺失 | High |
| A-4 | **Turn-state 机器是桩**:enum 缺 `waiting_for_permission/blocked_by_hook/waiting_for_child/waiting_for_workflow/cancelled`,未知态静默 coerce 成 `running` | `ccplus_contracts.py:23-31`、`session_control_plane.py:204` | 桩 | High |
| A-5 | **artifact 只认 5 个工具名**,DR/workflow/subagent/code-exec/MCP 产物全不可点 | `chat_artifact_delivery.py:28-39` | 部分 | High |
| A-6 | **headless 来源不建 session**:standalone/定时/admin 触发的 workflow run 静默 no-op(硬门控 `parent_session_id AND agent_id`) | `workflow_runtime_service.py:785`、`subagent_run_service.py:323` | 部分/静默降级 | High |

### 病根 B — 契约种下未接线 / 死代码

| # | 断点 | 证据 | 判定 | 严重度 |
|---|---|---|---|---|
| B-1 | **WIP 半成品切片**:`chat_sessions.py:920` permission-resolve 失败无 try/except → 返回 500 而非设计的 200;测试已写好但隔离跑 3/3 必挂 | `chat_sessions.py:920-986`、`test_chat_session_runs.py:424` | 缺失 | **阻断 commit** |
| B-2 | **Hook 外部 runner 是未接线能力**:`hooks.py` 统一维护 Hive Hook wire standard;`hook_runner.py` 的 `GovernedHookRunner` 生产从不实例化;async hook 解析能力存在但未执行;无 durable `HookInvocation` 表 | `main.py:419-428`;`hook_runner` 全仓零生产引用 | deferred, 不再拆独立兼容层 | High |
| B-3 | **ToolResultV1 side-effect channel**:engine 消费端+测试齐全,但全仓零生产者,且与已 live 的 `_tool_result_requests_user_clarification` 重叠 | `engine.py:707-727/4264`、`result_envelope.py:37` | 冗余死基建 | P1 |
| B-4 | **capability_gate 对未映射工具名默认 fail-open**(静默 allow),`STRICT_CAPABILITY_MAPPING` 默认关 | `capability_gate.py:398-417` | 静默放行 | P1 |
| B-5 | **PERMISSION_REQUEST/DENIED hook emit-into-void**:有 live emitter 但零注册 consumer;PERMISSION_DENIED 同时被列入 `_DISABLED_NOOP` 又在发射(catalog 漂移) | `hooks.py:197/964`、`governance.py:500/528` | 部分/漂移 | P1 |
| B-6 | 死代码/漂移:dead `_extract_summary` 机械压缩链、`resolve_no_policy_decision`/`default_decision` 死契约、6 个前端事件类型无 emitter、streaming output-cap 不 escalate | `conversation_summarizer.py:71-280`、`capability_gate.py:511-529`、`chat_message_parts.py:10-37` | 死代码 | P2 |

### 病根 C — 命门链路"机制就绪但生产从未验证"(动摇北极星两大目标)

| # | 断点 | 证据 | 判定 | 严重度 |
|---|---|---|---|---|
| C-1 | **多租户 RLS 真生效与否押在代码看不到的部署事实**:运行时以 owner 连库(从不切 `app_rls`);若 owner 是 superuser(测试注释自述生产 `clawith` 是)则 FORCE 全被绕过。策略 USING-only(无 WITH CHECK)+`OR tenant_id IS NULL`→NULL 行跨租户可见、写入侧不设防。无测试证明 FORCE 拦得住 owner 连接 | `entrypoint.sh:226`、`conftest.py:148-153`、`db_bootstrap.py:148-160` | 未决取证 | **P0** |
| C-2 | **自进化"上膛但从未击发"**:6-15 两条 P0 命门(save_skill 自授权、无 behavior writer)已修复,晋升链全 live、默认开、硬门正确;但 committed baseline 自 6-13 仍是 `pending-e2-live-run`、6 场景全 0.0,无证据表明生产真跑出过一份全绿 live 报告 | `evolution_verification.py:596-634`、baseline `core_behavior_v1.json` | 运行时未证 | **P0** |

### 取证确认的"真闭环"(勿误伤)

Agent Kernel 主路径(完整历史喂 LLM 压缩、10×重试+model fallback、LoopGuard、thinking-signature 往返、output 预算不饿死)、后台 subagent 真 child session + 父时间线投影、inline subagent 可 replay T0 段、workflow run/step 状态事件三路接、内核 27 个 live hook + memory pipeline、RLS 对**非 owner 角色**已被实跑测试证明有效。

---

## Part C — 修复计划与范围切分

### C.1 本轮代码完整修复(16 项,一次改完零债)

执行序(依赖优先,小解锁先行):

1. **B-1**(commit 阻断):`chat_sessions.py` permission-resolve 失败补 try/except + 消除测试顺序依赖。
2. **A-4**(turn-state 地基,被 A-2/A-3/A-6 投影消费):补全 `TurnStatus` enum + 真发射 + 投影 + 前端 cell。
3. **A-5 + A-1**(artifact 真相面):broaden `chat_artifact_delivery` 覆盖到 DR/workflow/subagent/code-exec 产物。
4. **A-2 + A-6**(workflow session 闭环):完成事件投 outputs/artifact;headless 来源建/绑 session。
5. **A-3**(A2A 父投影):delegation 完成时像 subagent 一样投 `child_session` 事件进父时间线。
6. **B-2**(CC hook 接线 or 退役)、**B-5**(permission hook consumer + catalog 漂移)、**B-3**(退役冗余 side-effect channel)、**B-4**(capability map 覆盖审计后 fail-closed)、**B-6**(死代码退役)。
7. **C-1 代码侧安全硬化**:补"owner 连接 + FORCE 表 + 跨租户读被拦"的行为级测试(test-only,零生产风险),坐实 FORCE 到底拦不拦得住。

每项遵循:真实 live 消费者/发射点 + revert-sensitive 测试(禁死 dataclass、禁 pin 死路径)。

### C.2 主理人执行的生产步骤(代码侧已备好,不擅自翻)

这两项**不是"改代码就完事"**,需要生产访问 + 影子验证,且贸然翻转有事故先例:

- **C-1 RLS 运行时角色翻转 + 策略 WITH CHECK / NULL 收敛**:属仓库已立项的受保护"RLS enforcement migration"独立主线,硬约束=**必先影子验证穷举 accessor,漏一个=生产 fail-closed 崩**;且 2026-06-11 一次 pre-auth 翻转已造成全员 401。本轮只做行为测试坐实问题 + 文档化步骤。**翻转由主理人在影子验证后执行。**
- **C-2 自进化生产击发**:需 Railway + secrets 跑一次 `run_and_store_tenant_behavior_eval`(或 `run_behavior_eval --mode live --persist`),看是否产出 `transport=hive_live` 且 6 场景全 ready 并落 TenantSetting。**由主理人执行**;在那之前不宣称"自进化已达成/超越 hermes"。

---

## Part D — 修复证据(2026-06-26 执行)

修复纪律:每项 = 真实 live 消费者/发射点(非死 dataclass)+ revert-sensitive 测试(钉死真实接线行为,非死路径)。修复前对每个断点先独立核实其在当前 WIP 里仍真实存在——**这一步证伪了 2 个审计误报(B-1、B-4),避免了对已正确代码的 scope 违规改动**。

### 病根 A — session-native 闭环(全部修复)

| # | 解决 | 真实 live 接线(file:line) | revert-sensitive 测试 |
|---|---|---|---|
| A-1 | DR 最终报告/来源现注册成 session artifact(免迁移、绕开 `message_id NOT NULL`) | `deep_research/workflow_definition.py:345` `_deliver_deep_research_report_to_session` 经新 `chat_artifact_delivery.py:154` `build_session_artifact_parts`(row-free)发 `artifact_delivery` 事件到父 session | `test_build_session_artifact_parts_returns_rowless_parts_for_safe_paths`、DR delivery 测试 |
| A-2 | workflow 完成事件投 `outputs`/deliverable,不再只投状态行 | `workflow_runtime_service.py` `_append_run_session_event(outputs=...)` + 完成调用点 `outputs=outcome.outputs` | `test_completion_session_event_projects_run_outputs` |
| A-3 | delegation 完成投 `child_session` 进父时间线(镜像 subagent) | `orchestrator.py:1242-1322` `_project_delegation_completion_to_parent`,在 `_spawn_async_delegation_task` 终态调用 | `test_delegation_completion_projects_child_session_event_to_parent`(+3) |
| A-4 | `TurnStatus` 补 5 态 + 从 session 事件真实派生 wait 态 + `killed→cancelled`(修"killed 静默成 running") | `ccplus_contracts.py` TurnStatus;`session_control_plane.py` `_derive_active_turn_status`/`_coerce_turn_status`,接线于 `build_session_workbench:609`;API 类型 `ccParity.ts:131` 已暴露 `active_turn.status` | `test_turn_state_derives_waiting_for_permission`、`_blocked_by_hook_child_and_workflow`、`_coerce_maps_killed_..._to_cancelled`(5 新)+ 更正既有 killed 测试 |
| A-5 | `tool_session_write_paths` 覆盖所有**有可推导路径**的产文件工具;`execute_code` 无声明输出路径,**显式记录为不可用此机制覆盖**(不假覆盖) | `chat_artifact_delivery.py` `tool_session_write_paths` | `test_tool_session_write_paths_resolves_path_bearing_writers`、`_skips_non_derivable_or_consumer_tools` |
| A-6 | headless(standalone/定时/admin)workflow run 现建/绑 ChatSession,不再静默 no-op | `workflow_runtime_service.py` `_ensure_run_session`,接线于 `start_run`/`_execute` | `test_headless_run_binds_a_chat_session`、`test_run_with_parent_session_does_not_create_a_new_session` |

> A-4 的可视化 turn-state badge 是薄 UI 后续:断点本质(后端 stub)已修,typed 态现经投影/export/API(`active_turn.status`)流出,前端可直接消费;徽标渲染属 gap-ledger 的"Codex-style improvement"增强列,非闭环本身。

### 病根 B — 契约种下未接线 / 死代码

| # | 解决 | file:line | 测试 |
|---|---|---|---|
| B-1 | **审计误报,证伪** | `chat_sessions.py:939-1049` try/except 早已实现(失败→记 `permission_resolved`(failed)→返回 200) | 既有 `test_resolve_session_permission_allow_failure_records_session_event_instead_of_500` 隔离+整文件双跑全过 |
| B-2 | Hook 标准 → 收敛到 `hooks.py`;外部 runner → 显式 DEFERRED CONTRACT(证据:Hive 只跑进程内 Python handler;未来外部 hook 才激活,durable 记录用现有 `invocation_spans` 不新建表) | `hooks.py` owns Hook wire standard;`hook_runner.py` DEFERRED docstring | `test_governed_hook_runner_is_deferred_not_wired_into_startup`、`_is_absent_from_live_hook_catalog`、`test_hook_wire_parser_has_no_production_caller` |
| B-3 | ToolResultV1 side-effect channel → 显式 DEFERRED CONTRACT(consumer+测试已在,与 live JSON-marker clarification 机制冗余) | `result_envelope.py` `ToolContentEnvelope` docstring(`new_messages`/`terminal_signal`) | `test_side_effect_channel_has_no_production_producer`(源码扫描守卫) |
| B-4 | **审计误报,证伪** | `config.py:155` `STRICT_CAPABILITY_MAPPING: bool = True`——未映射工具名默认已 fail-closed(审计读的是 docstring 里的 legacy 描述) | 既有 capability_gate 测试 |
| B-5 | PERMISSION_DENIED 移出 `_DISABLED_NOOP`、加进 `_ACTIVE_OBSERVE_ONLY` + 真 audit consumer,catalog 漂移修复 | `hooks.py`、`hooks_setup.py` `_audit_permission_denied`(key `governance.permission_denied.audit`) | `test_permission_denied_reports_live_not_disabled_noop`、`test_memory_hook_plan_registers_permission_denied_audit_consumer`(count 16→17) |
| B-6 | 删 6 个死的机械压缩函数(212 行)+ 收窄 `memory_service.py:33` import | `conversation_summarizer.py`(保留 live 的 `_extract_summary_from_response`) | `test_dead_mechanical_extract_helpers_are_removed`(revert guard) |

### 病根 C — 命门链路(代码侧已硬化,生产执行属主理人)

| # | 代码侧(本轮已做) | 生产执行(主理人) |
|---|---|---|
| C-1 | **新 `tests/integration/test_rls_force_owner_bypass.py`(5 用例)实跑证伪盲区**:生产镜像 owner 连接实测 `rolsuper=t/rolbypassrls=t`,**FORCE 拦不住、读到两个租户的行**;非 owner `rls_app_user` 在同 FORCE 表只见本租户。把"FORCE 拦不拦得住 owner"钉成 red/green | 非 superuser `app_rls` 角色 cutover(`RLS_APP_PASSWORD`/stage-3)+ 策略加 `WITH CHECK`/收敛 `OR tenant_id IS NULL`——属受保护"RLS enforcement migration"主线,**必先影子验证穷举 accessor**(2026-06-11 贸然翻转已致全员 401) |
| C-2 | 自进化晋升链已 live 接线、默认开、硬门正确(`evolution_verification.py:596-634`),无新代码缺口 | Railway 跑一次 `run_and_store_tenant_behavior_eval`(或 `run_behavior_eval --mode live --persist`),确认产出 `transport=hive_live` 且 6 场景全 ready 并落 TenantSetting;**在此之前不宣称"自进化已达成/超越 hermes"** |

### 验证

- 已完成区域定向回归:turn_state/side_effects/hooks/fast_reflection/memory_integration/orchestrator/workflow_runtime/conversation_summarizer/memory_service/compression_alignment → **163 passed**;DR/artifact/web_chat/deep_research → **175 passed**;hook wire standard → **14 passed**。
- 全量后端回归:见下方"最终全量回归"。
- ruff:所有改动文件 `ruff check` 全过。
- 范围纪律:未触碰 migrations/models/schema;C-1/C-2 的生产翻转未擅自执行(安全闸门)。

**最终全量回归**:`cd backend && pytest tests -q` → **`5295 passed, 2 skipped`(exit 0,88s)**。开工前工作树为 `5260 passed`(其中 B-1 是被测试顺序掩盖的 flaky);本轮 +35 个新 revert-sensitive 测试、−3 个 B-6 死代码测试后全绿,零失败零错误。上一轮遗留的 alembic 单头 fail 已由 WIP 修复(closure-head 常量已指向 `a2a_collaboration_groups_0624`),forced_rls 集成测试在本机 Docker 下 17 passed。所有改动文件 `ruff check` 全过(仓库权威门;3 处 `ruff format --check` 长行属既有、按 surgical 纪律未动)。
