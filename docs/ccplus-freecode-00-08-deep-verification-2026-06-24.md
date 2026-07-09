# CCPlus：FreeCode 00-08 深度对抗复核与技术债总账

日期：2026-06-24
状态：terminal-audit 的对抗复核与最后一轮技术债结清账（不替代、不重写 `docs/ccplus-freecode-00-08-terminal-audit-2026-06-24.md`，作为其旁证与执行账本）
范围：FreeCode `docs/00`-`docs/08` 语义、Hive 当前实现、Codex 可吸收工程 delta、已有 terminal-audit 文档
上位契约：`docs/ccplus-north-star-contract-2026-06-24.md`
合并裁决：`docs/ccplus-v1-deep-verification-reconciliation-2026-06-24.md`
方法：在逐章对抗判定（00-07 各 30 项 + 08 独立 pass-2 27 项 = 267 条 `CC-XX-NN` verdict，全部回溯到 `file:line`）基础上，本轮做了独立源码抽查复核（synthesis 自带 11 处 + 主复核额外 5 处承重断言全部命中），并按四个交叉镜头（doc-drift / atomic-matrix / chapter-blocker / completeness）对 terminal-audit 文档逐项校准。ch08 因主 run 结构化输出截断失败，经独立定向 pass-2（run `wf_ab6cd8f3-e55`）补全至同等取证强度；ch04 因主 run API 连接中断失败，经 resume 补回。

---

## 1. 总结论与诚实定位

**一句话结论：CCPlus 方向正确，CC base 是强 substrate 且治理咽喉不可绕过，但整体未达终态 CCPlus，禁止宣称"已完成"。**

本轮 95%+ 置信度覆盖的是**诊断、边界裁决、断点定位、技术债清单**，**不覆盖完成度**。这与 terminal-audit §0/§5 的诚实定位一致，本复核**确认其定位准确、未过宽**：

- **覆盖（95%+ 置信）**：北极星边界已明确（CC/FreeCode 是语义 MUST、Codex 是 MAY、Memory/Iter 是 Hive-native、provider-hosted remote 不是 parity）；00-08 主要断点已落到具体 `file:line` 证据与实施包；P0/P1 阻断项足够具体可直接进入实现与测试。
- **不覆盖**：① 不代表 Hive 已完成 CCPlus；② 不代表可跳过 backend/frontend/local_bridge 测试矩阵；③ 不代表 Railway/cloud sandbox/local runner 的生产验证已完成。

**关键量化事实（本轮独立复核确认）**：在 267 条原子要求中（00-07 各 30 + 08 独立 pass-2 27）——

- **真实 P0（破坏统一生命周期契约的行为级断裂）= 0**。所有"缺失"项要么是 CC 本身外部 build 也不活的特性（FreeCode §15 证实 `reactiveCompact`/`contextCollapse` 文件不存在、`snip` feature-flag-false），要么是 CC TUI/Node 进程内 AppState/prompt-cache 字节优化机制（无 Web 适用性），要么是 Hive 用更强机制（durable Postgres、T0 hash 链、frozen prefix）等价达成的 LAW。
- **真实行为级 CC-LOCAL 语义债 = 5 条 P1**：`CC-00-04`（StreamingToolExecutor 流式工具执行缺失）、`CC-01-04`（terminal reason 枚举缺失）、`CC-07-25`（coordinator 可阻塞式 delegate）、`CC-04-07`/`CC-04-08`（prompt-cache 字节稳定 / resume 幂等缺失，且 resume 当前**主动偏移**——按 flat 50K 重新截断 + 合成 `call_{id}`）。
- 其余为契约统一项（ToolSpec/ToolResult/PermissionProfile/SessionWorkbench 收敛）、observability 项、安全覆盖加固项（`CC-03-08` run_command 子命令解析）。
- **08 章经独立 pass-2 补全（27 项）后新增**：1 条覆盖/治理 P1（`CC-08-05` skill 访问控制 flag 缺失——`render_catalog` 无条件列出每个 skill，无 model-invocation/user-invocable/hidden gating）+ 一组 P2（`CC-08-19` 7 个 `_DISABLED_NOOP` hook 事件无 live emitter、`CC-08-24` `updatedMCPToolOutput` 零消费者、skill budget/frontmatter/inline-fork drift、MCP 发现 union 不全）。据此**修正"42 事件覆盖 CC-27 全集"口径**为"42 enum 成员含 CC-27、其中 7 个为无 emitter 的 `_DISABLED_NOOP` 桩"。

**被本复核证实为已实现且 live-wired（不得回归、不应重新论证）的核心 substrate**：单一多消费者 kernel loop（`engine.py:2773` `for round_i`，被 `web_chat_runtime.py:2397` 唯一消费）；user-message 先于 loop 落 T0 JSONL+hash 链（`web_chat_runtime.py:693` commit 早于 `:722` 后台 spawn）；工具结果溢出落盘 + 每轮 200K 聚合预算双路径（`engine.py:1937/3720/3957`）；零-API microcompact（`engine.py:3996-4062`）；autocompact 单-LLM-摘要 + 3-连败熔断（`memory_service.py:344-477`）；PTL/output-cap 多级恢复带 trip count（`engine.py:2965-3268`）；stop-hook block/prevent（`engine.py:3405-3463`）；maxTurns 硬上限（`engine.py:4178`）；frozen/dynamic cache 边界 + `cache_control`（`prompt_cache.py:34/127-166`）；governance 咽喉不可绕过（`tools/service.py` → kernel → invoker）；Hook 目录 42 enum 成员含 CC-27 全集（`hooks.py:58-111`；其中 7 个为 `_DISABLED_NOOP` 无 live emitter 桩，见 §3 ch08 CC-08-19）。

按仓库纪律（"无证据不得宣称完成"）：上述每一条均有 `file:line` 支撑。终态完成证明只能来自 terminal-audit §8 完成定义的测试、回放、生产/本地双侧验证，本文档**不是**上线完成证明。

---

## 2. 北极星边界校准复核

本复核确认 terminal-audit 的 scope 分类（`cc_must` / `codex_may` / `hive_native` / `remote_excluded`）**整体应用正确**，未发现把本地 CLI 语义误排除、也未把远程私有服务误纳入的边界误判。逐项确认：

| 分类边界 | terminal-audit 应用 | 复核结论 |
|---|---|---|
| CC local runtime semantics = MUST | §3 Scope Matrix、A00-A13 每章先看 FreeCode | **正确**。本地 process/filesystem/session/transcript/tool-loop/sandbox/hook/terminal-state 语义全部纳入；"TUI 可转译、语义不可删"在每章贯彻。 |
| provider-hosted / proprietary remote = NOT parity | §2.2、A14、North Star §5 UltraPlan | **正确且取证扎实**。UltraPlan 排除以真实 FreeCode 远程路径符号（`launchUltraplan`→`teleportToRemote`→CCR→`pollForApprovedExitPlanMode`）为依据，满足 North Star §9"命名确切 CC 语义来源"纪律。`CC-03-14` GrowthBook killswitch、`CC-04-12/13` cache_edits beta、`CC-04-20` fork prefix 共享均正确归为 remote/provider-proprietary 排除。 |
| Codex engineering controls = MAY adopt | §3、C-03、A02/A03/A07 | **正确**。typed thread/turn、approval reviewer、sandbox policy、deferred tools 均标 MAY；未让 Codex 改 CC 边界。`CC-00-20` task_budget→server、`CC-04-30` GrowthBook override 正确识别为 Codex 风格工程 delta 而非 CC 语义。 |
| Hive Memory/Iter = Hive-native delta | §2.2、C-04、A08、06 章 | **正确**。06 章保留 native 但显式映射 CC memory laws（`CC MEMORY.md` → `wiki_map.md`+T3；`CC stop-hook extraction` → `RESPONSE_COMPLETE`/`SESSION_IDLE`/`TURN_STOP`）；A08 明令"不得把 Memory 当 CC parity 完成证明"——正是契约要求的纪律。 |

**唯一一处需注意的边界细化（非误判，是颗粒度提示）**：FreeCode §15 区分两种"CC 本身不活"的状态——`reactiveCompact.ts`/`contextCollapse.ts` 是**文件不存在**，而 `snipCompact.ts`/`snipProjection.ts` 是**文件存在但 feature-flag-false**。terminal-audit 在 04/00/01 章把这几项一并按"CC build 不活→Hive 非 parity 债"处理，结论正确；若日后有人想"补齐"，应分清这两类，避免把 stub 当 parity 债重新引入。本复核交叉确认 §15 引用真实（`free-code-main/docs/00-architecture-overview.md:578` `## 15` 标题，`:586` "实际活跃只有 3 级"），据此的 `CC-00-09/17`、`CC-04-09/14`、`CC-01-09` 严重度降级**成立**。

**结论**：scope 分类无误判，可作为执行裁决基线。

---

## 3. Hive vs FreeCode 00-08 逐章合规对比

每章给出合规判定，并把原子要求拆成 已实现 / 部分 / 缺失 / Hive-native映射 / 排除 五态，逐条带 `file:line` 证据。判定口径遵循北极星：CC LOCAL 语义按 parity 计；CC TUI/Node 进程内机制按"Hive 等价机制满足 LAW 即 hive-native-ok"计；provider-hosted remote 按排除计。

### 00. 架构总览 — 合规判定：partial-high（强 substrate，统一契约未收敛；行为级 P0=0）

| 要求 | 能力 | 状态 | 证据 |
|---|---|---|---|
| CC-00-01 | async generator query loop（单实现多消费者） | 部分 | 单 loop+多消费者 live（`engine.py:2773`/`web_chat_runtime.py:2397`），但为 buffered `async def handle()->InvocationResult` + 回调，非 `yield AsyncGenerator`；0 处 `yield`/`AsyncGenerator`（`engine.py:2157`） |
| CC-00-02 | 显式状态机 + transition 追踪 | 缺失 | 命令式 `for`+内层 `while True`，局部标量；无 `State`/`transition` 类（`engine.py:2773/2850`） |
| CC-00-03 | user message 先于 loop 持久化 | 已实现 | `append_session_event` user_message → `db.commit()`（`web_chat_runtime.py:659-693`）先于 `:2397` 独立 executor 调 `invoke_agent`；T0 JSONL+hash 链先写（`ledger.py:95-176`） |
| CC-00-04 | 流式中执行 tool_use（StreamingToolExecutor） | **缺失（P1）** | `_stream_with_cancel` await 整条流后才解析/执行 tool_calls（`engine.py:2877→3524→3720/3957`）；无 mid-stream 工具执行——真实 CC local 延迟隐藏语义 |
| CC-00-05 | memory prefetch + 异步释放 | 缺失 | loop 前同步 await `resolve_memory_context`（`engine.py:2219`），非后台 prefetch（CC local 延迟优化，P2） |
| CC-00-06 | 每轮 skill discovery prefetch | 缺失 | catalog-in-prompt + 按需 `load_skill`（`engine.py:2317`），无每轮投机 prefetch（CC local 延迟优化，P2） |
| CC-00-07 | tool-use summary 异步流生成 | 缺失 | tool batch 后直接进下一轮（`engine.py:3690-3994`），无 Haiku fire-and-forget 摘要（CC local 延迟优化，P2） |
| CC-00-08 | 对 SDK 抑制可恢复错误 | 部分 | PTL 先 catch+retry 不外露（`engine.py:2943/2965`），耗尽才 `_build_error_result`；buffered-result 语义达成 GOAL，无 `isWithheld` 事件门（架构差异，P2） |
| CC-00-09 | 级联恢复 collapse→reactive-compact→surrender | 部分 | Hive 有多级带 trip 保护（`engine.py:2965-3268`，`_PTL_MAX_RETRIES=3`）；FreeCode §15 证实 reactiveCompact/contextCollapse 在 CC build 不活→非 parity 债（P2 命名差异） |
| CC-00-10 | API 错误跳过 stop hook 防死循环 | 缺失 | 错误路径不 fire STOP（`engine.py:2943-3331`），STOP 仅在 clean 完成路径（`:3405`）；死循环结构性不可能，但非显式守卫（P2） |
| CC-00-11 | needsFollowUp 由物理 tool_use 块决定 | 部分 | `if not response.tool_calls:` 终止（`engine.py:3371`），不依赖 finish_reason；行为满足，仅 provider-normalized 字段非 raw-block 扫描 |
| CC-00-12 | 流式回退孤儿消息墓碑清理 | 部分 | 同-provider HTTP retry 有 `STREAM_RETRY_TOMBSTONE`（`llm_client.py:880`/`engine.py:2668`）；跨-model fallback 仅 `runtime_fallback` 事件无 per-message 墓碑（`engine.py:3222-3263`，P2） |
| CC-00-13 | cloned message 上 backfill observable input | 缺失 | 有 `_clone_api_messages`（`engine.py:1347`）但用途不同；无 tool_use input 扩展回填（无 SDK observer，P2） |
| CC-00-14 | 流式回退墓碑 + 消息长度回滚 | 缺失 | model 回退重发同 api_messages（`engine.py:3222-3263`），无 length=0 数组回滚（provider 层 scalar 已 reset，P2） |
| CC-00-15 | tool 结果预算：溢出落盘 + 上下文预览 | 已实现 | `_maybe_evict_tool_result` 写 eviction_dir 返回 preview+path（`engine.py:1937/1980-2001`）；双路径 force-evict（`:3720-3741`/`:3957-3978`） |
| CC-00-16 | microcompact：旧 tool 结果零-API 清除 | 部分 | L1 时间触发每 3 轮，`_MICROCOMPACT_CLEARED_MARKER` 零 API（`engine.py:3996-4062`）；选择标准为 AGE+pressure 而非 COMPACTABLE_TOOLS 名集，50% 以下 no-op |
| CC-00-17 | context collapse：读时投影不拷贝 | 缺失 | mid-loop 破坏式替换 api_messages（`engine.py:4135`）；FreeCode §15 证实 contextCollapse 在 CC build 不存在；T0 ledger 保全历史（P2） |
| CC-00-18 | autocompact：token 阈值 + 熔断 | 部分 | 阈值触发→单-LLM-摘要→前缀+tail（`memory_service.py:396/477/498`），3-连败熔断 600s half-open（`:344-356`）；阈值 %-based vs CC 绝对量 |
| CC-00-19 | 压缩排序保信息保真 | 部分 | per-round budget→microcompact→L3 compact，便宜步先跑使昂贵步可成 no-op（`engine.py:3720/3996/4064`）；无 snip/collapse（CC build 也无） |
| CC-00-20 | task budget 跨压缩边界追踪 | 缺失 | 客户端 `accumulated_tokens`/`usage_anchor`（`engine.py:2768/2715`）用于阈值，不发 server（CC server 计费精度优化，P2） |
| CC-00-21 | token budget 续写 + nudge 注入 | 部分 | output-cap 续写注入 nudge ≤3（`engine.py:2083-2126`），stop-hook-block 注入（`:3438`）；keyed off finish_reason 非 TOKEN_BUDGET，turn_token_budget 硬停非 nudge |
| CC-00-22 | compact/session_memory 跳过 blocking limit | 缺失 | compaction 是独立 `_llm_summarize`（`memory_service.py:477`）非 forked governed agent；架构前提不存在→死锁不会发生（P2） |
| CC-00-23 | QueryEngine session 级 mutableMessages splice | 缺失 | mid-loop 替换列表引用（`engine.py:4135`）非 splice；T0 ledger 持久；list 替换在 Python 等价 GC（P2） |
| CC-00-24 | tool abort 检查防中断后工作 | 部分 | round 起/批前/每工具/compact 前多检查点（`engine.py:2774/3589/641-665/4067`）；无专用 post-tool abort 门跳过 attachment/hook（P2） |
| CC-00-25 | stop hook 经 attachment 通道阻断 | 已实现 | STOP emit→`prevent_continuation`/`block` 注入消息+`stop_hook_active`+continue（`engine.py:3405-3463`），re-block 守卫（`:3412-3416`） |
| CC-00-26 | maxTurns 硬上限 | 已实现 | `range(max_rounds)`+耗尽返回 terminal（`engine.py:2761/2773/4178-4184`）+ LoopGuard backstop（`:2413/2527`） |
| CC-00-27 | ToolUseContext DI 贯穿 loop | 部分 | `KernelDependencies`+`InvocationRequest`+`SessionContext`+`ExecutionIdentity` ContextVar（`engine.py:223/2183`）；拆成多对象，工具扩展为 in-place mutate 非 `update.newContext` 返回（`:3829-3934`） |
| CC-00-28 | 消息 UUID 分裂确定性派生 | 缺失 | 每轮一条 assistant 消息（`engine.py:3526`）；tool_call-id split 是不同关注点（`:1439`）；resume 用 T0 hash 链替代（P2） |
| CC-00-29 | isMeta 区分注入 vs user 内容 | 缺失 | 注入内容无 isMeta；transient reminder 仅 clone（`engine.py:2852-2855`）部分缓解，persisted nudge 为 plain user role（`:2110/3440`，P2） |
| CC-00-30 | 系统 prompt 静/动边界做 cache scope | 部分 | frozen prefix+`PROMPT_CACHE_BOUNDARY`+dynamic suffix+`cache_control` ttl（`prompt_cache.py:34/127-166`/`engine.py:307`）；session 级 prefix cache 非 CC global scope |

### 01. 查询引擎 — 合规判定：partial-high（loop 真统一；唯一真债 = terminal reason）

| 要求 | 能力 | 状态 | 证据 |
|---|---|---|---|
| CC-01-01 | user message 先于 loop 持久化 | 已实现 | RuntimeTask flush→`append_session_event`→`db.commit()`（`web_chat_runtime.py:693`）→`create_task`（`:722`）；强于 CC fire-and-forget |
| CC-01-02 | assistant message fire-and-forget 持久化 | 部分 | 流式 chunk live，terminal 一次性 await 写（`engine.py:3473-3482`）；无 per-message mutable 流式对象（机制不同，行为达成） |
| CC-01-03 | 显式状态机 + transition | 缺失 | flat `for`+`while True`+int `ptl_retries`+局部标志（`engine.py:2773/2849/3449`）；无 transition 枚举 |
| CC-01-04 | terminal reason 枚举 | **缺失（P1）** | `InvocationResult` 仅 content/tokens_used/final_tools/parts/reasoning_signature，**无 reason 字段**（`contracts.py:76-81`）；terminal 由 content 前缀 + `RuntimeTask.status` 推断——本章最强真债 |
| CC-01-05 | progress message inline 记录 | 已实现 | `_persist_tool_call`/`_persist_runtime_event` 各 append+`commit`（`web_chat_runtime.py:1454-1547`），inline 调用 |
| CC-01-06 | 消息排序：压缩边界投影 | 部分 | 压缩物理替换历史（`engine.py:4135`/PTL `3024-3195`）；无持久边界标记可投影 |
| CC-01-07 | tool result 预算执行 | 部分 | 200K 聚合 per-result append 时执行（`engine.py:3721-3741/3958-3977`）；非建模为有序 [4]-before-[6] 步 |
| CC-01-08 | microcompact 先于 autocompact | 部分 | 代码序 microcompact（`:4001`）先于 autocompact（`:4064`）同 %3 块；无"microcompact 已降阈则跳 autocompact"门 |
| CC-01-09 | context collapse 先于 autocompact | 缺失 | 唯一前置是破坏式 microcompact clear；CC build 也无 collapse（P2） |
| CC-01-10 | 流式中追踪 tool_use 块 | 已实现 | 退出信号 = `response.tool_calls` 存在（`engine.py:3371`），非 stop_reason |
| CC-01-11 | 可恢复错误 withholding | 部分 | PTL 在 loop 内恢复后才返回 error（`engine.py:2943-3268`）；单 InvocationResult 返回结构性避免早 yield error 失败模式 |
| CC-01-12 | collapse drain 单次守卫 | 缺失 | 无 collapse-drain 阶段、无 transition 字段；PTL 用 int counter 收敛（`engine.py:2965-3219`，机制不同，P2） |
| CC-01-13 | reactive compact 螺旋防护 | 部分 | 无 `hasAttemptedReactiveCompact` 标志；ptl_retries 每轮 reset（`engine.py:2849`）；LoopGuard+max_rounds 提供等效防护 |
| CC-01-14 | max output tokens 恢复+升级 | 部分 | 同-request 8k→ceiling 仅在 `complete()`（`llm_client.py:534`）；stream 不升级但起始即 ceiling+`_continue_after_output_cap` ≤3（`engine.py:2064-2126`）；收敛护栏在，仅无 8k→64k 字面阶梯 |
| CC-01-15 | blocking limit 硬刹 | 缺失 | 无 pre-API 拒绝；策略为总是压缩（`engine.py:4064/2965`）；CC-CLI `/compact` 余量特有，无 Hive UI 等价（P2） |
| CC-01-16 | 流式工具执行并发控制 | 已实现 | 分段并行（`engine.py:3582-3756`），safe 工具入 gather、unsafe 经 done_events barrier 等所有前序，semaphore=10 |
| CC-01-17 | Bash 错误的兄弟 abort controller | **缺失（P2）** | `gather(return_exceptions=True)`（`engine.py:3663`），错误转字符串（`:3679-3687`），兄弟不 abort；唯一取消是整 run cancel_event |
| CC-01-18 | progress 与 result 消息分离 | 部分 | running 先 emit、done 按原序 emit（`engine.py:3604-3719`）；用 status 字段单通道而非 CC 两结构通道 |
| CC-01-19 | 非流式 tool batch 分区 | 部分 | gate 并行（`engine.py:3582`）；用 per-tool order-barrier 等待替代离散 partition，结果等效 |
| CC-01-20 | tool 执行 context modifier 延迟 | 缺失 | 无 `context_modifier` 概念（0 hits）；`track_file_*` per-tool 在 hook 内不按 serial/parallel 延迟 |
| CC-01-21 | memory prefetch 零等待消费 | 缺失 | deps 一次性解析注入；无 per-iteration zero-wait poll（Codex 风格延迟优化，非契约项） |
| CC-01-22 | skill discovery prefetch one-per-turn | 缺失 | catalog+按需 load_skill（`contracts.py:53-59`）；无投机 skill prefetch（非契约项） |
| CC-01-23 | tool use summary 跨迭代流 | 缺失 | 结果直接 append（`engine.py:3749-3755`）；无异步 batch 摘要（非契约项） |
| CC-01-24 | feature flag 编译期 vs 运行期分离 | 缺失 | Python 无 tree-shaking；DB-backed flag live 评估（`invoker.py:33/59`）；JS-bundler 特有，非 Hive gap |
| CC-01-25 | DI 可测性 | 已实现 | kernel 零 DB import（`engine.py` grep=0），全 I/O 经 `KernelDependencies`（`:222-243`），real wiring（`invoker.py:970-983`） |
| CC-01-26 | max turns + attachment 信号 | 已实现 | `range(max_rounds)`+user-visible limit 消息+RuntimeTask terminal（`engine.py:2761/4176-4184`）；信号为消息+status 非 typed reason（见 CC-01-04） |
| CC-01-27 | API 错误 stop hook fire-and-forget | 部分 | 错误返回前不调 STOP（`engine.py:3266-3331`），skip-on-error 达成；无显式 stop-FAILURE-hook on API error |
| CC-01-28 | token budget 边界终止 | 部分 | turn_token_budget 硬停（`engine.py:3355-3369`）；无 90% 软阈 nudge 或 diminishing-returns 阶梯 |
| CC-01-29 | command 生命周期仅正常返回通知 | 缺失 | 无 thin query() shell；command 经自身 runtime 解析（`api/commands.py`）；CC 内部机制无 Hive 行为后果 |
| CC-01-30 | abort 在 turn 边界处理 | 部分 | round 顶/流式/工具前后多检查（`engine.py:2774/3589/4067`）；`_build_cancelled_result` 折叠所有 abort 为单形态（无 reason split、无 missing tool_result 块重建，P2） |

### 02. 工具系统 — 合规判定：partial-high（核心元数据/并发/eviction live；缺 side-effect 通道与 cache 字节优化）

| 要求 | 能力 | 状态 | 证据 |
|---|---|---|---|
| CC-02-01 | 工具接口多态 | 部分 | frozen `ToolMeta` 聚合 name/desc/params/flags（`decorator.py:28-77`），单一 source 派生 schema/exec/governance（`collector.py:63-167`）；但非单一泛型五面 Tool（无 per-tool validateInput/checkPermissions/UI face） |
| CC-02-02 | fail-closed 默认值 | 已实现 | frozen 默认 read_only/parallel_safe/destructive=False，仅显式 True 才入集（`decorator.py:42-47`/`collector.py:149-156`）；`execute_code`/`run_command` 未声明→默认非并发+写假定 |
| CC-02-03 | input 验证 audience 分离 | 缺失 | 无 validateInput vs checkPermissions 分流；governance 单链返回一个 block 串（`service.py:268-283`） |
| CC-02-04 | input schema 解析 + 错误上报 | 部分 | 无 safeParse 前置门；deferred schema-not-sent hint live（`prompt_builder.py:624-635`） |
| CC-02-05 | deferred tool 发现 + 延迟逻辑 | 部分 | deferred 组 live（`runtime_tool_groups.py`/`workspace.py:1063-1109`/`invoker.py:786`）；延迟决策静态（`engine.py:601-604`），无 token/char-budget 自动计算 |
| CC-02-06 | 并发安全元数据 | 已实现 | `_is_concurrency_safe_tool = parallel_safe AND not destructive`（`engine.py:722-728`）+ 分段执行器（`:3582-3656`） |
| CC-02-07 | 只读分类 | 部分 | `read_only` 静态喂并发；但 Bash 动态分析（ls/grep→isReadOnly）缺失——`run_command` 无 read_only 声明，每命令当写工具 |
| CC-02-08 | interrupt behavior 控制 | 缺失 | `ToolMeta` 无 interrupt 字段（0 hits）；只有全局 cancel_event（`engine.py:641-674`），无 per-tool block vs cancel |
| CC-02-09 | ToolUseContext DI | 部分 | `ToolExecutionContext` 仅 agent/user/tenant/workspace/session（`runtime.py:17-25`）；无 readFileState/FileStateCache（`filesystem.py:147` 仅软提示） |
| CC-02-10 | subagent 状态隔离 + 基础设施例外 | 缺失 | 无 setAppState 二元性（`runtime.py:17-25` 无 setter）；Hive 隔离用 source='agent'+core_tools_only 不同机制 |
| CC-02-11 | prompt cache 稳定经上下文继承 | 部分 | frozen prefix 在 cache 边界冻结（`prompt_builder.py:684`/`invoker.py:405-420`）；无显式"捕获父 bytes 复用"契约（Hive 无 flag 可中途翻） |
| CC-02-12 | 嵌套 memory 注入去重守卫 | 缺失 | 无 `loadedNestedMemoryPaths` Set；soul/memory 作 frozen prefix 一次注入（`prompt_builder`），规避而非实现 |
| CC-02-13 | ToolResult side-effect 信封 | **缺失（P0 契约项）** | `ToolContentEnvelope` 仅 content+media（`result_envelope.py:21-53`），无 data/newMessages/contextModifier/mcpMeta（0 hits）；工具不能注入消息/改上下文 |
| CC-02-14 | 受限 contextModifier 应用 | 缺失 | contextModifier 不存在（0 hits）→约束 moot；分段执行器有序列化基底但无 modifier 能力可门 |
| CC-02-15 | 工具注册表 cache 稳定排序 | 部分 | OrderedDict 插入序保留（`registry.py:196`），`_always_tools` 后置（`agent_tools.py:852-856`）；无字母分区+cache breakpoint，新 MCP 工具可移位 |
| CC-02-16 | 一刀切 deny 规则预过滤 | 部分 | 可见列表组装前过滤（`agent_tools.py:814-836/864`）；capability/assignment 驱动非单一 getDenyRuleForTool 复用 |
| CC-02-17 | prompt cache 稳定 input 三元身份 | 缺失 | 单一 mutable `effective_args` 同时供 call 和 observer（`engine.py:951/965-966`）；无独立 immutable API-original（0 hits） |
| CC-02-18 | 投机并行 Bash 分类器 | 缺失 | run_command 走同步 governance preflight（`service.py:268-287`），无并行投机分类器（0 hits） |
| CC-02-19 | 结果尺寸 per-tool 阈值 | 已实现 | `ToolMeta.max_result_chars`+`RESULT_CHARS_UNLIMITED=0` 哨兵（`decorator.py:25/48-52`）；live 落盘（`engine.py:174-188/1937-2001/3720`） |
| CC-02-20 | 三层结果尺寸节流 | 部分 | 两层 live（tool-declared + 全局 50K 默认，`engine.py:183-188`）；缺中层 GrowthBook override，且 declared>50K 不被 clamp 到 50K（CC 用 min()） |
| CC-02-21 | 幂等磁盘持久化 | 部分 | 确定性 `{tool_call_id}.txt`（`engine.py:1983-1985`）；用 `write_text` 截断覆盖非 `wx` 排他（0 hits），replay 静默重写 |
| CC-02-22 | 溢出预览信封 | 已实现 | preview[:4000]+结构化"[Full output saved... read_file]"（`engine.py:1990-2001/137`） |
| CC-02-23 | ContentReplacementState 冻结-重放 | 缺失 | 无 freeze-on-first seenIds 替换态；每次重算 preview（`engine.py:1937-2001`），microcompact 用通用 marker（`:4010-4047`） |
| CC-02-24 | 单消息聚合结果预算 | 已实现 | `_TOOL_RESULTS_AGGREGATE_BUDGET=200000` 双路径 force-evict（`engine.py:138-139/3721-3741`） |
| CC-02-25 | backfillObservableInput hook 契约 | 缺失 | 0 hits；唯一 input mutation 是 PRE_TOOL_USE 替换工作副本（`engine.py:951/965-966`），与 CC clone 语义相反 |
| CC-02-26 | toAutoClassifierInput 投影 | 缺失 | 0 hits；`_build_tool_preflight_input` JSON-dump 全参（`service.py:716-757`），无 per-tool 紧凑投影/skip 声明 |
| CC-02-27 | 别名受限回退 | 部分 | 别名 live（`decorator.py:88-90`/`search.py:45`）；但无"仅当匹配名在 aliases 才回退到 base list"安全约束（`agent_tools.py:171-186` 无条件回退） |
| CC-02-28 | tool 执行 abort on signal | 部分 | 入口前 raise（`engine.py:651-652`），批前检查（`:3589/3761`）；返回通用 cancelled InvocationResult 非 per-tool CANCEL_MESSAGE（0 hits） |
| CC-02-29 | tool call with progress streaming | 部分 | kernel emit running/done 生命周期事件（`engine.py:3604-3718`）；非 CC `onProgress(ToolProgress)` 增量流（0 hits） |
| CC-02-30 | 工具调用优先级查询 | 部分 | try_execute 先查 registry，miss 回退 MCP（`runtime.py:47-53`/`service.py:536-542`）；registry 全局，无 alias-only escape 限制（同 CC-02-27 根因） |

### 03. 权限系统 — 合规判定：hive_native_ok（SAFETY LAW 满足；CC TUI rule-grammar 机制 N/A；两处 LAW 级注意）

| 要求 | 能力 | 状态 | 证据 |
|---|---|---|---|
| CC-03-01 | 四态权限决策 | 部分（hive-native） | `run_tool_governance` 返回 str|None=3 效果态（`governance.py:201/692-711`）+ `ActionPreflight` 5-轴（`action_preflight.py:23-29`）；无 PermissionResult union 第四态 passthrough |
| CC-03-02 | 决策 reason 审计链 | 部分 | `write_audit_event` capability.denied + status 串 live（`governance.py:439-461`）；非 typed `PermissionDecisionReason` union |
| CC-03-03 | deny 规则最高优先 | 部分 | deny>approve>allow 单 policy 内（`capability_gate.py:438-466`），zone block 先于 gate（`governance.py:232-253`）；非 8-source 规则树 |
| CC-03-04 | 工具特定权限委派 | 缺失 | 无 per-tool checkPermissions 接口（0 hits）；唯一工具特定分支是中央 run_command 正则（`governance.py:153-163`） |
| CC-03-05 | bypass-免疫安全检查 | 缺失（N/A） | Hive 无 bypassPermissions runtime 态（仅 evals CLI flag）；fail-closed by default（`governance.py:212-276`） |
| CC-03-06 | passthrough-to-ask 回退 | **部分（P2，LAW 倒置）** | mapped 但无 policy 行→`allowed=True, policy_found=False`（`capability_gate.py:435-436` "allow everything"）= fail-OPEN，与 CC never-silently-grant 相反；`STRICT_CAPABILITY_MAPPING=True`（`config.py:155`）仅护 UNMAPPED |
| CC-03-07 | 模式化权限变换 | 缺失 | `permission_mode` 是静态标签（`command_registry.py:29`），仅喂 prompt 文本（`engine.py:861-895`），无 transform pipeline；Plan Mode 是独立 Hive-native 门 |
| CC-03-08 | Bash 子命令解析 | **缺失（P1，安全覆盖）** | `_detect_dangerous_command` 整串小写正则（`governance.py:153-163`，live `:557`），无 `&&`/`||`/`|` 切分/shlex/per-sub map；`npm install && rm -rf /` 当一块 |
| CC-03-09 | 内容特定 vs 工具级规则 | 缺失 | CAPABILITY_MAP 键为工具名→类别，policy 按 capability 等值（`capability_gate.py:415-419`）；无 `Bash(npm:*)` 内容规则 |
| CC-03-10 | Bash 子命令 per-sub 结果 map | 缺失 | 同 CC-03-08；安全关注归 P1（`governance.py:153-163/557`） |
| CC-03-11 | 规则串解析 + 转义处理 | 缺失（N/A） | 无 permissionRuleValueFromString（0 hits）；DB policy 模型无规则串语法 |
| CC-03-12 | shell pattern 匹配（exact/prefix/wildcard） | 缺失（N/A） | 仅 FIXED 危险正则（`governance.py:92-109`），无用户规则 prefix/wildcard 语义 |
| CC-03-13 | bypass 模式免疫清单 | 缺失（N/A） | 无 bypass 可刺穿；交互工具 end turn（`plan_mode.py:535/625`）是 turn-control 非 bypass escape |
| CC-03-14 | service 侧 bypass killswitch | 排除 | GrowthBook `tengu_disable_bypass`（0 hits）= provider-hosted remote gate，按契约排除 |
| CC-03-15 | auto 模式 fast-path 优化 | 缺失（N/A） | 无 ant-only auto 模式；SAFE_TOOLS 是固定只读集（`governance.py:33-50`）非分类器避让 |
| CC-03-16 | 分类器决策 + 置信阈 | 缺失（N/A） | 无 LLM Bash allow-classifier；决策全确定（DB policy+正则+zone） |
| CC-03-17 | AI auto-mode 分类器两阶段 | 缺失（N/A） | 无 yolo/auto-mode AI 分类器（0 hits） |
| CC-03-18 | 分类器 fail-closed/open | 缺失（N/A） | 广义 fail-closed 在（`governance.py:212-276/539-552`），但无 classifier→无 `tengu_iron_gate` |
| CC-03-19 | 拒绝追踪双限 | 缺失（N/A） | 无 denialTracking 计数器（0 hits）；denial 每次返回 fresh teaching block |
| CC-03-20 | 拒绝限回退到 prompting | 缺失（N/A） | 依赖不存在的 denial tracking |
| CC-03-21 | 危险权限模式检测 | 部分 | 运行时扫 run_command 危险模式 live（`governance.py:92-109/557/561-598`）；非 CC 进入 auto 模式时扫 RULES 的 strip-restore |
| CC-03-22 | 规则遮蔽检测 | 缺失（N/A） | 无 shadowedRule（0 hits）；`audit_capability_mapping` 检测 tool↔MAP 漂移（`capability_gate.py:247-304`）非规则遮蔽 |
| CC-03-23 | TOCTOU 经语法拒绝防护 | **部分（P1）** | FILE 工具 resolve+relative_to 容器化（`workspace.py:51-56`），plan_mode 拒 absolute/`..`（`:166-174`）；但无 UNC/`~user`/`$VAR`/`$(cmd)` shell-expansion 拒绝清单，且 **run_command 参数路径未做此校验** |
| CC-03-24 | 路径安全检查排序 | 缺失（N/A） | 扁平单步容器化（`workspace.py:51-56`），无 isPathAllowed 有序 7 步（无 acceptEdits 模式） |
| CC-03-25 | 投机 allow 分类器并行 | 缺失（N/A） | 无投机分类器（0 hits）；governance 全顺序确定 |
| CC-03-26 | 投机结果用户交互取消 | 缺失（N/A） | 依赖不存在的投机分类 |
| CC-03-27 | 异步 subagent 本地拒绝追踪 | 缺失（N/A） | 无 denial 追踪；subagent 经同 governance（`governance.py:492-538`） |
| CC-03-28 | requiresUserInteraction 工具免疫 | 部分 | 有交互工具强制 user-decision turn 边界（`plan_mode.py:413-625`/`web_chat_runtime.py:1558-1606`）；非 requiresUserInteraction flag（无 bypass，capability-exempt `capability_gate.py:231-236`） |
| CC-03-29 | 模式 double-check（Shift+Tab 稳定） | 缺失（N/A） | 无模式 cycling（0 hits）；TUI 交互稳定性无 Web 对应 |
| CC-03-30 | plan 模式权限快照 + auto 子程序 | 部分 | 有 runtime Plan Mode activate/restore + 只读 allowlist（`web_chat_runtime.py:1631-1735`/`plan_mode_policy.py:22/138`/`service.py:206-231`）；无 prePlanMode 权限模式快照、无 auto-as-subroutine（Hive 无 permission modes） |

### 04. 上下文管理 — 合规判定：partial-high（昂贵/有损级强；cache 字节稳定半边缺失）

| 要求 | 能力 | 状态 | 证据 |
|---|---|---|---|
| CC-04-01 | token 计数（anchor+增量） | 部分 | 整 payload CJK char 估算（`conversation_summarizer.py:35-68`），显式忽略 usage_anchor（`memory_service.py:426-439`）；非 CC anchor+delta（P2） |
| CC-04-02 | 并行 tool call 修正（兄弟 anchor） | 缺失 | 无 usage-anchor→无可锚（char 估算下 moot） |
| CC-04-03 | 有效上下文窗口公式 | 部分 | `effective_limit=max(ctx-20000, ctx//2)`*threshold（`memory_service.py:421-424`）；flat -20000 而非 min(maxOut,20K) |
| CC-04-04 | autocompact 阈值（buffer 公式） | 部分 | %-阈（0.75/0.82）每 3 轮（`engine.py:4065/4111`）；无 13K/3K buffer 常量（P2） |
| CC-04-05 | 上下文窗口解析优先级 | hive-native 映射 | override>ProviderSpec>128000（`memory_service.py:614-625`）；CC 特定层（CLAUDE_CODE_MAX/[1m]/beta header）按 L3 model-equality 正确不移植 |
| CC-04-06 | tool result 预算（L1 零-API） | 部分 | 溢出落盘+preview+200K 聚合 force-evict（`engine.py:1937-2001/3720-3741`）；preview 4000（CC 2000），非 cache-frozen（见 CC-04-07，P2） |
| CC-04-07 | 内容替换状态机（冻结决策） | **缺失（P1）** | 无 ContentReplacement/mustReapply/frozen/seenIds（0 hits 于 compaction）；eviction 每次重算（`engine.py:1937-2001`），保留的结果后续可被 evict→破坏 prefix cache |
| CC-04-08 | tool result 替换跨 resume 幂等 | **缺失（P1，主动偏移）** | resume 重载用 flat 50000 截断（`web_chat_runtime.py:433-437`），`tool_call_id='call_{msg.id}'`（`:414`）非原 streamed id；实现幂等的**反面**，每次 resume 侵蚀 cache |
| CC-04-09 | snip 机制（L2 zombie 消除） | 缺失 | 无 snip（仅 snippet）；`/context` 静态列 'snip_or_evict'（`diagnostic_command_runtime.py:222`）但 engine 无 snip（over-claim，P2） |
| CC-04-10 | compactable tools 集 | 部分 | 经 `ToolMeta.max_result_chars` per-tool 豁免（`engine.py:174-188/4034-4044`）；倒置（opt-out flag）而非 COMPACTABLE_TOOLS allowlist |
| CC-04-11 | 时间触发 microcompact（cache TTL） | 已实现 | 60min gap 清旧 tool 结果保 5 最近+marker（`engine.py:3996-4062`）；pressure-aware 10min+never-clear-below-50% 守卫（`:151-171`） |
| CC-04-12 | cached microcompact（cache_edits） | 排除 | 依赖 Anthropic cache_edits beta（0 hits）；文档明确不做（`docs/archive/legacy-docs/compaction-cc-alignment.md`）→ provider-proprietary 排除 |
| CC-04-13 | cached microcompact 主线程限制 | 排除 | CC-04-12 子约束，随父排除 |
| CC-04-14 | context collapse：读时投影（L4） | 缺失 | mid-loop in-place mutate（`engine.py:4135`）与读时投影相反；FreeCode §15 证实 contextCollapse 在 CC build 不存在；T0 保全（P2） |
| CC-04-15 | context collapse 双阈交接 | 缺失 | 依赖 CC-04-14；仅单 `_MIDLOOP_COMPACT_THRESHOLD=0.75`（P2） |
| CC-04-16 | autocompact 触发守卫（递归+污染） | 缺失 | 仅 round%3+len>6（`engine.py:4065`）；无 querySource 递归守卫；summarizer 独立一次性调用（`conversation_summarizer.py:631`）部分 moot（P2） |
| CC-04-17 | autocompact 熔断（连败限） | 部分 | `_SUMMARY_BREAKER_MAX_CONSECUTIVE_FAILURES=3` per-tenant 600s（`memory_service.py:344-369`）；熔断在 summary 调用而非 autocompact loop，开时降级 trim 仍压缩（`:517-519`，P2） |
| CC-04-18 | session memory 压缩（轻量替代） | 缺失 | 直接调 LLM summarizer 无 Haiku-substitute 尝试（`memory_service.py:460-519`，P2） |
| CC-04-19 | compact prompt：九段结构 + no-tools | 已实现 | 11 字段超集 CC-9，`<analysis>/<summary>`+BAD/GOOD+anti-drift（`conversation_summarizer.py:347-468`）；no-tools 结构性（stream 无 tools 参，`:631-638`）强于 CC 指令式 |
| CC-04-20 | compact cache prefix 共享（fork） | 排除 | 自有 system prompt+set max_tokens（`conversation_summarizer.py:633-636`）；文档明确 defer（无 fork 机制）→ fork 原语排除 |
| CC-04-21 | compact PTL retry（截头） | 已实现 | summarizer 输入超窗 head-drop（`conversation_summarizer.py:536-577`）+ kernel PTL 丢旧 round-group 20%（`engine.py:3075`）≤`_PTL_MAX_RETRIES=3` |
| CC-04-22 | post-compact 文件恢复（≤5/50K） | 部分 | recent_files[-5:] fresh 重读+<100K 守卫+per-file cap（`engine.py:1855-1877/4119-4135`）；char-based、无 readFileState 排序/排除过滤（P2） |
| CC-04-23 | post-compact 状态恢复（plan/skill/deferred） | 部分 | 恢复 Work Ledger+session memory+summary+T3（`engine.py:1754-1853`）；不恢复 plan-mode/full-skill；mid-loop 保 `api_messages[0]`（系统 prompt 含 active schema）使 deferred 不丢（P2） |
| CC-04-24 | post-compact 清理（cache+state reset） | 部分 | `reminder_scheduler.reset()`+POST_COMPACTION hook（`engine.py:4138/4155-4167`）；无全面 runPostCompactCleanup（多数 CC cache 在 Hive 不存在，P2） |
| CC-04-25 | blocking limit 检查（autocompact-disabled 硬门） | 缺失 | 无 pre-call 硬门（0 hits）；仅 reactive PTL（`engine.py:2965`）；`/context` 静态列 'blocking_limit'（`diagnostic_command_runtime.py:226`）无机制（over-claim，P2） |
| CC-04-26 | reactive compact（413 回退） | 部分 | LLMError+PTL 压缩重试 full→round-group→full→fallback ≤3（`engine.py:2943-3212`）；无 collapse-first、无 stop-FAILURE-hook 区分（P2） |
| CC-04-27 | reactive vs proactive 互斥 | 缺失 | 二者无条件并存（`engine.py:4064/2965`），无互斥门（0 hits）；实践 75% proactive 常先触发使 reactive backstop（P2） |
| CC-04-28 | 压缩管线执行序（五级） | 部分 | evict→microcompact→autocompact 便宜-先-昂贵（`engine.py:3720/3996/4064`）；缺 snip/collapse/blocking（CC build 也无），3 级子集（P2） |
| CC-04-29 | 压缩结果消息重建（边界排序） | 部分 | `_wrap_compressed_summary` 系统边界消息+restoration（`memory_service.py:372-393/498`/`engine.py:4129-4135`）；边界 merge 进 summary 单消息而非独立 record（P2） |
| CC-04-30 | per-tool 持久化阈值解析 | 部分 | override>50000 默认>never（`engine.py:174-188`/`registry.py:166`）；无 GrowthBook remote override（0 hits，Anthropic 内部实验面，可接受缺失） |

### 05. 状态与界面 — 合规判定：hive_native_ok（state LAW 经 Zustand/Postgres 满足；TUI 渲染机制 N/A）

| 要求 | 能力 | 状态 | 证据 |
|---|---|---|---|
| CC-05-01 | 同步 store 通知 | hive-native 映射 | Zustand `listeners.forEach` 同步（`zustand/vanilla.js:11`），58 callsite（`stores/index.ts`）；NIH 正确规避 |
| CC-05-02 | Object.is 短路相等 | hive-native 映射 | `if (!Object.is(...))`（`vanilla.js:8`）；CC 机制由库提供 |
| CC-05-03 | onChange diff 副作用通道 | **缺失（P2）** | 0 hits onChangeAppState；`governance.py:196` 是工具 preflight 非 state-diff 通道；无单一 state-diff 副作用 choke（工程便利，非生命周期阻断） |
| CC-05-04 | selector 字段级精度订阅 | hive-native 映射 | `useSyncExternalStore`+selector（`zustand/react.js:7-12`）；TanStack Query 字段精确 queryKey |
| CC-05-05 | selector 反模式强制（ant-only） | 缺失（排除） | ant-build dev 断言，vendor-internal，正确排除 |
| CC-05-06 | 读写 hook 分离 | hive-native 映射 | Zustand 内建三层（subscribe/setState/getState，`vanilla.js:6/14/20`） |
| CC-05-07 | AppStateProvider 单例 + 嵌套守卫 | hive-native 映射 | module 级 create() 单例（`stores/index.ts:15/49`），结构上不可嵌套 |
| CC-05-08 | store 经 lazy useState 初始化 | hive-native 映射 | module load 一次 create()，引用不变 |
| CC-05-09 | 集中式 state 变更副作用派发 | **缺失（P2）** | 同 CC-05-03（工程便利，非阻断） |
| CC-05-10 | ref-state 双轨同步读 | 缺失 | refs 服务 WS 重连 pending 合并（`AgentDetail.tsx:347-348`），非 CC messagesRef 双轨；Web 无非-React 同步读者需要 |
| CC-05-11 | QueryGuard 同步状态机 | 部分（更强） | 无 React 状态机；映射到 server Postgres PARTIAL UNIQUE INDEX（`runtime_task.py:26-43`）+IntegrityError→409/queue（`web_chat_runtime.py:694-718`），强于内存版 |
| CC-05-12 | generation number 防陈旧回调 | 缺失 | 用 `_reconcile_terminal_transcript_ghost`（`web_chat_runtime.py:271`）+DB 唯一性，不同有效方案 |
| CC-05-13 | useSyncExternalStore 集成 | 缺失 | 0 直接 useSyncExternalStore；active-run 经 TanStack 轮询+WS（`chat.ts:209`/`AgentDetail.tsx:985`），状态机在 Postgres |
| CC-05-14 | 三层 query 流消费 | 部分 | 三职责跨前后端分布（guard=`web_chat_runtime.py:475/557`，loop=kernel，event-apply=`AgentDetail.tsx:1025-1141`）；后端驱动 WS 架构非 client 三方法 |
| CC-05-15 | 纯函数 focus 仲裁 | 缺失（N/A） | 0 getFocusedInputDialog；浏览器 DOM 原生管理 focus/modal 栈 |
| CC-05-16 | 打字抑制 dialog 显示 | 缺失（N/A） | TUI 单输入流特有；浏览器 modal 不门控于 typing-active |
| CC-05-17 | 虚拟滚动范围计算 | 缺失（N/A） | 0 useVirtualScroll/库；浏览器原生 composit；性能优化非语义 |
| CC-05-18 | 虚拟滚动常量 | 缺失（N/A） | 随 CC-05-17 缺失 |
| CC-05-19 | 流式中延迟消息渲染 | 缺失（N/A） | 0 useDeferredValue；同步 setChatMessages（`AgentDetail.tsx:1111-1139`）；浏览器无相同 input-lag 病理 |
| CC-05-20 | offscreen freeze 经元素引用缓存 | 排除 | OffscreenFreeze 是 Ink terminal-reset 机制，浏览器 DOM 原生处理 |
| CC-05-21 | React Compiler opt-out for freeze | 排除 | 0 'use no memo'；护 OffscreenFreeze，随父排除 |
| CC-05-22 | DeepImmutable 类型强制 | 缺失 | 0 DeepImmutable；Zustand set() 约定松散持 LAW，无编译期强制（影响低） |
| CC-05-23 | mutable ref 排除于 DeepImmutable | 缺失 | 随 CC-05-22 moot |
| CC-05-24 | 有意单体 REPL 组件 | 部分 | `AgentDetail.tsx`(2410 行)+`AgentChatSection.tsx`(2135 行) 大耦合 chat 面；分两文件非单 5009 行 |
| CC-05-25 | Context vs store 角色分离 | 缺失 | 用 Zustand/TanStack/local state；无显式 Context-vs-store 角色分离纪律（设计 note 非能力） |
| CC-05-26 | OverlayContext 混合模式 | 缺失（N/A） | 0 OverlayContext；ESC 仲裁是 TUI 单输入流关注，浏览器原生 |
| CC-05-27 | 消息占位 baseline 追踪 | 缺失 | `appendOptimisticUserMessage`（`AgentDetail.tsx:448`）不同关注点；dev-UX 优化（非阻断） |
| CC-05-28 | 用户输入时滚动 repin 窗口 | 缺失 | 无 3s repin 启发；DOM 原生保滚动位（非阻断） |
| CC-05-29 | Ink fork 终端渲染 | 排除 | 浏览器 React SPA；Ink 终端机制零 Web 适用 |
| CC-05-30 | 渲染时读 vs 陈旧值 | 排除 | scrollToElement/Yoga calculateLayout 是 Ink 布局机制；浏览器原生几何 |

### 06. 记忆系统 — 合规判定：hive_native_ok（CC memory LAW 满足；CC Node/local-CLI 机制正确排除）

| 要求 | 能力 | 状态 | 证据 |
|---|---|---|---|
| CC-06-01 | 四型 memory 分类 | hive-native 映射 | 11-类超集 + 未知归 general 优雅降级（`types.py:39-53`/`explicit_overlay.py:64-66`）；PL4 硬拒（`privacy_layer.py:88-90`） |
| CC-06-02 | Markdown frontmatter memory | hive-native 映射 | .md+YAML frontmatter+`<explicit_memory>` XML（`explicit_overlay.py:310-349`）；T3 per-entry XML（`md_store.py:759-794`） |
| CC-06-03 | MEMORY.md index 双维截断 | hive-native 映射 | `_MAX_ROWS=20` heat 排序+4000-char trim（`memory_navigation.py:17`/`prompt_builder.py:580`）；wiki_map 是导航非 always-on |
| CC-06-04 | relevance 经 sideQuery+JSON Schema | hive-native 映射 | live LLM reranker+0-based 白名单+observable fallback（`retriever.py:87-202`），live wired（`memory_service.py:142-148`/`invoker.py:509`）；vendor-neutral 模型 |
| CC-06-05 | 目录扫描单遍 IO 优化 | hive-native 映射 | manifest build join 元数据排序（`md_store.py:745-794`）；CC Node-fs 微优化非 LAW |
| CC-06-06 | memory age 人类可读非 ISO | 部分 | '[Nd ago — verify]' 仅 stale 路径>7d（`assembler.py:31-44`）；其他处仍 ISO；memory 在 dynamic suffix 外于 frozen prefix（`invoker.py:476-478`）→age 变不破 cache |
| CC-06-07 | staleness 警告分级阈值 | hive-native 映射 | `_FRESHNESS_WARNING_DAYS=7`（`assembler.py:14-16`）+lifecycle 抑制 ttl_expired/conflict（`activation.py:91-105`） |
| CC-06-08 | TRUSTING_RECALL：assert 前验证 | 部分 | 无专用 TRUSTING_RECALL 段；verify-before-assert 分布于 staleness 后缀+scenario（`scenario.py:47/77`）+skill 模板；弱于 CC eval-tuned 段 |
| CC-06-09 | feedback/project 的 Why/How body | hive-native 映射 | （**前一轮过宽已纠**）`form_lint` 仅查 empty/pronoun/relative-time 非 Why/How；Why LAW 经 `explicit_overlay` 硬编码 why_it_matters、runtime memory section 与 memory tool schema 满足（`explicit_overlay.py:138/344`） |
| CC-06-10 | 不该存：显式排除列表 | 已实现 | live prompt 排除列表（`prompt_sections/memory.py:52-57`），save_memory 限显式命令（`:34-39`），episodic 重定向（`memory.py:126-130`） |
| CC-06-11 | 异步 prefetch 管线：per-turn 发起 | 部分 | loop 前一次性 await（`engine.py:2219`），4 re-render site 复用不 re-fetch；per-turn-single 满足，async overlap 缺失（Node/TUI 优化，非阻断） |
| CC-06-12 | 可释放资源 [Symbol.dispose] | 排除 | TS/Node generator 生命周期优化，绑 async prefetch（reranker 已 try/finally+wait_for self-clean，`retriever.py:166-201`） |
| CC-06-13 | settledAt 零等待轮询 + 多机会消费 | 排除 | Hive 同步解析无 race（`engine.py:2219`）；async-prefetch 设计的工程后果 |
| CC-06-14 | filter-then-mark 去重序 | 排除 | 无 readFileState 缓存/prefetch→约束 moot；去重经 content-hash/jaccard/similarity |
| CC-06-15 | 多层去重深度 | hive-native 映射 | pre-write jaccard+rerank+assembly content-hash+episodic（`explicit_overlay.py:287-307`/`retriever.py:128-131/712-718`/`assembler.py:65-75`） |
| CC-06-16 | worktree-aware canonical git root | 排除 | CC local-CLI fs 概念；Hive AGENT_DATA_DIR/<agent_id>/memory 多租户更强隔离（`workspace.py:24`） |
| CC-06-17 | memory path 解析层级 | 排除 | CC local-CLI 路径解析；Hive server config+agent id 确定（`explicit_overlay.py:156-157`） |
| CC-06-18 | memory path 校验拒危险模式 | 部分 | subagent name resolve+is_relative_to 拒逃逸（`subagent_memory.py:66-72`）+symlink skip（`workspace.py:161`）；广义 CC 模式列表多 N/A（server 派生路径） |
| CC-06-19 | trust-source 层级：projectSettings 排除 | 排除 | Hive 无 per-repo memory-path 设置→攻击面不存在；server 身份固定（`workspace.py:24`） |
| CC-06-20 | team memory symlink 逃逸防护 | 部分 | symlink skip+containment 基线（`workspace.py:161`/`subagent_memory.py:66-72`）；CC 两遍 realpath 硬化多 N/A（server tenant 隔离） |
| CC-06-21 | team memory 作 auto-memory 子目录 | hive-native 映射 | owner/company-aware activation scoping+sensitivity（`activation.py:50-68`/`memory_service.py:181-192`）；公司级控制面更强 |
| CC-06-22 | EXTRACT_MEMORIES：fork turn-end 提取 | hive-native 映射 | SESSION_CLOSE seal+`run_t2_segment_package_job`（`hooks_setup.py:484-584/886-887`）；governed sealed-segment 管线 |
| CC-06-23 | 提取 cursor+throttle+merge+trailing | hive-native 映射 | `discover_pending_t3_sources`+stage（`heartbeat.py:589-595`）+T0 seal cursor（`ledger.py` hash 链） |
| CC-06-24 | 提取 agent canUseTool 沙箱 | hive-native 映射 | heartbeat 已退为 direct T3 core，不进入完整 tool loop，仅 stage T3 不直写；Platform Gate commit；write_gate 全程 |
| CC-06-25 | agent memory：per-type 隔离目录 | hive-native 映射 | `SubagentMemoryStore` governed write+containment（`subagent_memory.py:60-178`），live spawn wiring（`subagent.py:310-317`）；standalone 不继承 host memory（`invoker.py:482-483`） |
| CC-06-26 | KAIROS 日志 append-only | hive-native 映射 | T0 append-only events.jsonl+SHA-256 链（`ledger.py:90-95/562-569`）；Dream 是分离 distillation |
| CC-06-27 | isAutoMemoryEnabled 优先链 | 部分 | 平台级 gating：principal 未解析抑制（`memory_service.py:129-139`）+cadence+sensitivity；无单一 env 优先链（local-CLI 概念，无 Hive 对应） |
| CC-06-28 | isAutoMemPath 前缀 containment | 部分 | containment 经 resolve+is_relative_to（`subagent_memory.py:68-72`）+write_gate；非单一 isAutoMemPath helper（Hive 经 tool/gate 路由非 fs carve-out） |
| CC-06-29 | memory prompt caching 经 systemPromptSection | hive-native 映射 | **倒置 CC**：memory 在 dynamic suffix 外于 frozen prefix（`invoker.py:476-478`/`engine.py:2349`）→fresh memory 不抖 cache |
| CC-06-30 | memory attachment 经 system-reminder 包装 | 部分 | headed sections 嵌 dynamic suffix（`assembler.py:87-115`/`prompt_sections/memory.py:3-61`）；非 createUserMessage(isMeta)+per-attachment 信封（设计差异，cache 理由 moot） |

### 07. 子代理与团队 — 合规判定：hive_native_ok / partial-high（隔离/递归/原子去重 live；唯一真债=coordinator force-async）

| 要求 | 能力 | 状态 | 证据 |
|---|---|---|---|
| CC-07-01 | call() 决策树 dispatch | 部分 | spawn/delegate/send 三 CORE 工具 live（`agent_tools.py:234-261`）；非单一优先级互斥 call()，coordinator-force-async 缺（见 CC-07-25） |
| CC-07-02 | subagent 上下文隔离 + 选择性共享通道 | hive-native 映射 | fresh 请求+独立 session（`subagent.py:846-879`）；per-field 共享是 CC TUI in-process AppState（0 hits），Hive 进程级隔离更强 |
| CC-07-03 | 经 root store 注册任务 | hive-native 映射 | durable RuntimeTask row 先于调度（`subagent_run_service.py:221-224`），restart-reconcilable（`:381`）；durable Postgres 等价 in-process root store |
| CC-07-04 | worker tool pool 独立于父 | 已实现 | `resolve_subagent_tools` 按 spec union base 排除（`subagent.py:427-439`），invoker filter（`invoker.py:947-951`）；父 deny 不泄漏 |
| CC-07-05 | sync 共享 AppState 写，async no-op | 排除 | shareSetAppState 是 CC TUI 进程内二元门（0 hits）；Hive 无共享父 AppState 回调 |
| CC-07-06 | 异步 subagent 本地拒绝追踪 | 排除 | localDenialTracking 补偿 CC async no-op（0 hits）；Hive per-call governance 无共享计数器 |
| CC-07-07 | fork 继承父 system prompt bytes | 缺失 | 无字节继承；subagent 自建 standalone prompt（`subagent.py:467-483`）；**且 fork=all 在 live 入口死代码**——spawn handler 从不填 ctx.parent_messages（0 live producer），CC prompt-cache fork 子系统无 Hive 对应（P2） |
| CC-07-08 | fork 用父精确 tool pool | 缺失 | 无 useExactTools（0 hits）；自解析 spec pool（CC fork cache 优化，非契约项） |
| CC-07-09 | fork override model='inherit' | 部分 | model-inherit 默认 live（`subagent.py:448-456`）；非 fork-specific cache-key lock，spec 可 override |
| CC-07-10 | fork 占位 tool 结果多路 cache 共享 | 缺失 | 无 FORK_PLACEHOLDER（0 hits）；`fanout_subagents` 仅在 deny-list（`subagent.py:115`）非可调；Hive 独立 invoke fan-out |
| CC-07-11 | fork 双检测防递归 | 已实现 | base tool-deny + depth guard（`DEFAULT_MAX_SUBAGENT_DEPTH=2`）+ per-trace visited cycle（`subagent.py:111-135/719-734`/`orchestrator.py:276-283/836-855`）；覆盖全 spawn 类型，强于 CC fork-only |
| CC-07-12 | fork 注入 'not-main-agent' 身份 | hive-native 映射 | standalone prompt 替换 host 身份（`subagent.py:467-483`），递归 tool-denied；replace-not-layer→无父 fork 指令可纠正 |
| CC-07-13 | 全 agent 禁用工具单一真源 | **部分（P2，双源漂移）** | subagent deny（`subagent.py:111-135`）含 check_subagent/ask_user_question/request_plan_mode/fanout_subagents；delegation deny（`orchestrator.py:41-60`）**不含**这些——两手维护列表分歧 |
| CC-07-14 | async-only 工具白名单 | 缺失 | 无 ASYNC_AGENT_ALLOWED_TOOLS（0 hits）；bg 用同 spec（`subagent.py:1129-1206`，P2） |
| CC-07-15 | in-process teammate 解锁 task+send | hive-native 映射 | team member 全 RuntimeTask web session（`agent_teams.py:481-539`），CORE 含 track_todo/send_message（`agent_tools.py:267-269`） |
| CC-07-16 | async 启动立即返回 agentId | 已实现 | bg spawn 先记 RuntimeTask running 再调度，立即返回 run_id+status（`subagent.py:1173-1206`/`subagent_run_service.py:221-224`） |
| CC-07-17 | async abort controller 脱钩父 | 已实现 | bg 自有 asyncio task 强引用（`subagent.py:1036/1198-1200`），不随父返回杀；delegation `_async_tasks` 独立（`orchestrator.py:304`） |
| CC-07-18 | task 通知入队原子去重 | hive-native 映射 | 原子 DELETE...RETURNING（`subagent_wake_consumer.py:230-256`）+per-tick woken_parents 守卫；强于 in-process flag |
| CC-07-19 | task 通知 tool-round 边界 drain | hive-native 映射 | in-run O(1) consume_signals（`subagent.py:1068-1084`）+idle daemon re-invoke（`subagent_wake_consumer.py`） |
| CC-07-20 | user input 永优先于 task 通知 | hive-native 映射 | wake daemon defer-while-active（`subagent_wake_consumer.py:156-161`）；弱于 CC 显式 PRIORITY_ORDER 但保 LAW（user turn 不被 system wake 抢占） |
| CC-07-21 | Mailbox in-process 消息派发 | hive-native 映射 | durable transcript mailbox（`subagent.py:490-572`/`agent_session_continuation.py:102-200`）；'transcript event 是 durable mailbox truth' |
| CC-07-22 | team roster 扁平单 leader | 已实现 | `AgentTeam.lead_agent_id`+`AgentTeamMember.team_id` 无 member-parent 列（`agent_team.py:16-95`）；schema 结构性防嵌套 |
| CC-07-23 | teammate spawn in-process vs tmux | hive-native 映射 | member 始终 RuntimeTask web session（`agent_teams.py:481-539`）；tmux/iTerm 是 local-CLI 终端面板分支，正确排除 |
| CC-07-24 | coordinator 模式工具白名单门 | 已实现 | `filter_tools_for_coordinator` live-wired kernel（`coordinator.py:198-214`/`engine.py:2292/2384-2385/3931`），detect via execution_mode/invocation_scope |
| CC-07-25 | coordinator 强制全 worker async | **缺失（P1，本章唯一真 CC-runtime 语义债）** | 0 forceAsync（0 hits）；`COORDINATOR_ALLOWED_TOOLS` 含阻塞式 delegate_to_agent（`coordinator.py:26`→`communication.py:233`→`orchestrator.py:711-754` await）；prompt 仅推荐并行（`:80/113`）；coordinator 可阻塞 leader |
| CC-07-26 | coordinator system prompt 编码编排纪律 | 已实现 | ~140 行 COORDINATOR_SYSTEM_PROMPT（角色/7-phase/anti-patterns/report，`coordinator.py:42-181`）live 注入 protected suffix（`engine.py:2294`） |
| CC-07-27 | subagent 资源清理 finally | hive-native 映射 | 进程级 self-clean + bg 强引用 add_done_callback discard（`subagent.py:1199-1200`）+tenant ContextVar reset finally（`:1194-1196`）；CC 进程内 leak 枚举无 Hive 对应 |
| CC-07-28 | subagent sidechain transcript for resume | hive-native 映射 | child T0 session append/seal（`subagent.py:585-661`）+ChatSession 投影（`subagent_run_service.py:82-158`）；T0 events.jsonl hash 链是显式等价 |
| CC-07-29 | 经 SendMessage resume async subagent | **部分（P2）** | non-terminal session resume live（`subagent.py:490-572`）；但 terminal session **被拒**（`agent_session_continuation.py:26/128-148`），无法 resume 已完成 subagent（CC resumeAgentBackground 可）；new-spawn 可替代 |
| CC-07-30 | deny 规则 per-agent 非继承 | 部分 | TOOL deny per-spawn 非继承 live（`subagent.py:427-439`/`invoker.py:947-951`）；但 roster 级 per-agent deny（filterDeniedAgents）不存在（0 hits）——subagent 反正 tool-denied 不能 spawn |

### 08. 扩展系统 — 合规判定：partial-high（独立 pass-2 重验 27 项；Skill 渐进披露/deferred 单源/MCP authz/plugin fail-closed/hook block 真 live；2 处"已实现"被证伪降级；新 P1 = skill 访问控制 flag 缺失）

> 本章经独立定向 pass-2（baseline→verify→challenge，run `wf_ab6cd8f3-e55`，27 项）重验——弥补主 run 中 ch08 结构化输出截断失败（StructuredOutput retry cap）留下的缺口；下表与其余 8 章同等 `file:line` 取证强度。

| 要求 | 能力 | 状态 | 证据 |
|---|---|---|---|
| CC-08-01 | Skill prompt expansion system | 已实现 | `workspace.py:201` _load_skill 仅返回 body；runtime_tool_group fallback 'load_skill does not make these schemas callable' |
| CC-08-02 | Progressive disclosure 1% budget | 部分 **(P2)** | `context_budget.py:523` skill_catalog_budget=_clamp(int(system_budget*0.08),4000,12000)；无 0.01/250 公式 |
| CC-08-03 | Four skill sources | 部分 | `skill_seeder.py:59` 扫 packs/*/pack.yaml；templates/system_skills+templates/skills；无 skill:// MCP 源 |
| CC-08-04 | Skill frontmatter contract | 部分 **(P2)** | `parser.py:81-83` 'any other frontmatter keys ... silently ignored'；`types.py:9-23` trimmed SkillMetadata（when_to_use/context/agent/hooks/paths/model 被丢弃） |
| CC-08-05 | Skill access control flags | 缺失 **(P1)** | app/skills 内 0 disable-model-invocation/user-invocable/isHidden；`registry.py:68` render_catalog 无条件列出每个 skill |
| CC-08-06 | Inline vs fork skill execution | 缺失 **(P2)** | `workspace.py:201` 仅 inline body 返回；无 fork_skill/executeForkedSkill；subagent fork 独立存在但不由 skill frontmatter 驱动（根因 CC-08-04） |
| CC-08-07 | Bundled skill reference file extraction | 已实现 | `loader.py:75-98` read_resource relative_to(skill_root) 越界 raise PermissionError；RESOURCE_DIRS gate（eager-seed vs CC lazy） |
| CC-08-08 | MCP skill no inline shell | Hive-native映射 | `workspace.py:201` 只读+返回；`skill_guard.py:158` remote_shell_pipe block——构造上无内联 shell，强于 CC |
| CC-08-09 | Skill safe-property whitelist | 部分 | 无 SAFE_SKILL_PROPERTIES per-property 白名单；等价为 fail-closed `skill_guard.py:83` scan_skill_files+sensitive governance |
| CC-08-10 | Plugin as container | 已实现 | `catalog_reader.py:1-8` 'install/composition source NOT executable schema'；plugin_install_service 持久 TenantInstalledPlugin（RLS） |
| CC-08-11 | Plugin discovery marketplace vs local | 部分 **(P2)** | `plugin_install_service.py:120-124` 'source kind not installable in v1 — fail-closed'；builtin/local 可装，git/url/npm/pip 识别但 fail-closed（需 signature+sandbox） |
| CC-08-12 | Builtin plugins toggleable | 部分 **(P2)** | `plugin_hook_service.py:134` status=='enabled' gate；无 CC builtin-toggle UI / user>default>true fallback |
| CC-08-13 | Eight MCP transports | 部分 | `mcp_client.py:1-8` 仅 HTTP+SSE；stdio/*-ide 是 local-CLI transport 出 remote scope，适用的 http/sse 已实现 |
| CC-08-14 | In-process MCP transport | 排除 | 仅 httpx HTTP/SSE；无 sdk-transport（CC in-process SDK 原语） |
| CC-08-15 | MCP namespace isolation | 已实现 | `mcp_naming.py:49/90/97` build/is_mcp/parse；MAX_MCP_TOOL_NAME_LEN=64+_short_hash |
| CC-08-16 | MCP one-shot discovery union | 部分 **(P2)** | tools→DB rows + resources on-demand；非 CC 并行 union——prompts/list→commands 不导入、skill://→skills 不导入（`mcp.py:503/556`） |
| CC-08-17 | OAuth token refresh non-blocking | 部分 **(P2)** | `mcp_oauth.py:99` is_expired EXPIRY_SKEW=60+refresh-before-expiry；`:12` assert_no_mcp_token_passthrough；诚实边界：unit-verified only 非 live（`:18-21` fail-closed） |
| CC-08-18 | claude.ai hosted MCP notifications | 排除 | 无 claudeai/markClaudeAi（provider-hosted remote） |
| CC-08-19 | 27 lifecycle hook events | 部分 **(P2)** | **由"已实现"降级**：enum 达 42 成员但 `hooks.py:195` `_DISABLED_NOOP` 7 事件无 live emitter（SETUP/PERMISSION_DENIED/ELICITATION_RESULT/WORKTREE_CREATE/REMOVE/CWD_CHANGED/FILE_CHANGED）；其余 live emit（`subagent.py:774/913`/`engine.py:951/1116`） |
| CC-08-20 | Hook sources settings/plugin/skill/internal | 部分 **(P2)** | 4 源中 3：internal(register_memory_hooks)/settings(hook_runtime_config)/plugin(`plugin_hook_service.py:158` status=='enabled')；skill-frontmatter hooks 缺（parser 无 hooks 字段） |
| CC-08-21 | Hook impl command/http/agent/prompt/callback | Hive-native映射 | `catalog_reader.py:40` HOOK_HANDLER_ALLOWLIST（3 类）；`plugin_hook_service.py:6` 'Raw code/import paths/webhooks never executed'——更收紧 |
| CC-08-22 | Hook JSON protocol stdin+sync schema | 部分 | `hooks.py:329-397` input/output schemas+HookResult dataclass:423+describe_event_catalog:903 |
| CC-08-23 | Hook blocking decision=block + exit 2 | 已实现 | `engine.py:983` 'Blocked by hook'；`:3442-3449` '[Stop hook blocked stopping]'；`hooks.py:782` _blocking_supported set |
| CC-08-24 | Hook rewriting updatedInput/additionalContext/updatedMCPToolOutput | 部分 **(P2)** | **由"已实现"降级**：updatedInput(`engine.py:965`)+additionalContext(`invoker.py:1263`) live；但 updatedMCPToolOutput/output_rewrite **零消费者**（仅 `hooks.py:385` schema 声明） |
| CC-08-25 | Hook safety trust/timeout/async | 部分 **(P2)** | timeout(asyncio.wait_for+plugin 5s)+async fire-and-forget(`engine.py:289` _emit_hook_background)+failure isolation(`hooks.py:1024-1043`)；无 trust/workarea 字段 |
| CC-08-26 | Skill registration bridge for MCP cycle | 排除 | 无 mcpSkillBuilders/registerMCPSkillBuilders（CC MCP↔skill 注册桥） |
| CC-08-27 | Deferred tool discovery + searchHint | 已实现 | `agent_tools.py:595` 单源双路径；`:666` 'denied == not reachable'；`:536-550` deny_by_server——tool_search 与 schema 注入共享 deny-gated 单源 |

**关键降级与口径修正**：① CC-08-19 由"已实现"降级——`HookEvent` enum 虽达 42 成员，但 `hooks.py:195` `_DISABLED_NOOP` 中 7 个事件（均为 in-scope 本地状态事件）**无 live emitter**：catalog parity 完整、emitter 覆盖不完整（catalog-存在≠live-接线）。这**修正主报告/附录"42 事件覆盖 CC-27 全集"口径**为"42 enum 成员含 CC-27，其中 7 个为无 emitter 的 `_DISABLED_NOOP` 桩"。② CC-08-24 由"已实现"降级——`updatedMCPToolOutput`/output_rewrite **零消费者**，仅 schema 声明。

**跨章项（不属 FreeCode 08 doc 扩展原子项，独立追踪）**：Command 层（七源汇流，FreeCode 00 §6 一等能力，North Star §3 MUST-map）归 cross-cutting 债 **D-10**（见 §4 ADJ-M1）；ExtensionRegistryV1 总账缺失归 **D-11**。两者不并入上表，按 §5 债账独立追踪。

---

## 4. terminal-audit 文档调整建议

按四镜头逐项给出：错误 / 过宽 / 引用漂移 / 缺失断点 / 可优化。每项给确切 section 与具体改动。

### 4.1 错误（必须纠正的事实性问题）

| ID | section | 类型 | 问题与改动 |
|---|---|---|---|
| ADJ-E1 | §00/§04 章末 prose + §5 矩阵隐含 + （上游）仓库 `CLAUDE.md` | 引用漂移→事实纠正 | terminal-audit A12 信"CC parity catalog"未列举，仓库 CLAUDE.md 仍写"15-event lifecycle bus"。live `hooks.py:58-111` = **42 事件**覆盖 CC-27 全集。改动：在 A12/§08 加锚 "live HookEvent catalog = `runtime/hooks.py:58-111`（42 enum 成员含 CC-27 全集，但其中 7 个为 `_DISABLED_NOOP` 无 live emitter 桩 → catalog parity 完整、**emitter 覆盖不完整**；开口是 per-entry runtime-consumer wiring + 7 桩 emitter，见 CC-08-19/D-29）"；并修上游 CLAUDE.md "15-event"→"42-event enum（CC-27 超集，7 桩待接 emitter）"。 |

### 4.2 过宽（overstated — 读起来比 runtime 支持更完整）

| ID | section | 问题与改动 |
|---|---|---|
| ADJ-O1 | §4.1 A07 Hive-evidence cell | "active run/fork/regenerate API 已存在"暗示可用 resume/regenerate，实则 `agent_session_continuation.py:26/128-148` 拒 terminal session→**无法 resume 已完成会话**（CC-07-29）。改：限定为 "...已存在（仅 non-terminal session；completed-session resume 仍缺，见 07 章 CC-07-29）"。 |
| ADJ-O2 | §4.1 A00 执行状态 "code-level closed for boundary" | A00 是纯策略/边界裁决行，无 runtime artifact，"closed/code-level closed"读作完成声明，违 §0"状态指向断点非完成"。改："boundary ruling fixed（scope 决策，非 runtime-completeness 声明）"。 |
| ADJ-O3 | §4.1 A06 "near；缺统一 ContextPolicyV1 matrix 和 resume/fork proof" | "near"+泛 "resume/fork proof" 未传达 resume **主动偏移**：`web_chat_runtime.py:414/433` 按 flat 50K 重截断+合成 `call_{id}`（CC-04-08），且无 frozen ContentReplacementState（CC-04-07）。改：caveat 锐化为 "缺统一 ContextPolicyV1；resume 当前按 flat 50K 重新截断（非 byte-identical 幂等，CC-04-08）+无 frozen ContentReplacementState（CC-04-07）"。 |

### 4.3 引用漂移（doc-drift）

| ID | section | 问题与改动 |
|---|---|---|
| ADJ-D1 | §4.1 A12 + §08（依赖 hook 目录准确） | 见 ADJ-E1。A12 hook-parity 闭环若按 CLAUDE.md 陈旧 "15-event" 衡量会 under-scope P0。改同 ADJ-E1（锚到真实 42-event enum）。 |
| ADJ-D2 | §04 章 + ContextPolicyV1（§4.1/§6 Package D）| 未记录 §15 build-reality 理由——`reactiveCompact`/`contextCollapse`/`snip` 在 CC 外部 build feature-flag-false/文件不存在。无此理由，未来实现者可能"补齐"为假 parity 债。改：04 章加一段引 FreeCode §15："snip/contextCollapse/reactiveCompact 在 CC 外部 build 不活；Hive 3 活级（evict/microcompact/autocompact+reactive-PTL）是 CC-build parity 非 5-级 gap，勿重新引入 stub 级"。 |

### 4.4 缺失断点（missing — 应列入阻断/包但未列）

| ID | section | 问题与改动 |
|---|---|---|
| ADJ-M1 | §4.1 矩阵 A00-A15 + §08 章 | **无 Command 层行/章**。`command_registry.py`/`api/commands.py`/`session_command_runtime.py` 存在，FreeCode 00 §6 七源+三类型（prompt/local/local-jsx）+bridge-safety 是一等 CC 能力，North Star §3 明列 MUST-map。改：加 A 行（A11b "Command Layer / slash-command 收敛"）+ 08 章 Command 子节，映射 CC 七源汇流→Hive command_registry 源序、CC bridge-safety→Local Agent Channel 安全分类、CC mid-session re-eval→Hive availability gating，给验证 `pytest -k command_registry`。 |
| ADJ-M2 | §00/§01 章 + §4.2 P0 | **无跨-model-fallback/abort 孤儿-tool 配对重建行**（normalizeMessagesForAPI 等价）。`_build_cancelled_result`（`engine.py:2004`）折叠所有 abort 不补 missing tool_result；跨-model fallback 无 per-message 墓碑（CC-00-12/CC-01-30）。改：TurnStateV1 加 "terminal reconciliation" 规则（interrupt/abort/fallback 时每个 dangling tool_use 补合成 tool_result 再 seal）+ A 行（A02b "Message pairing / orphan reconciliation"）+ §4.2 P0 验收出口 "abort mid-tool-batch 和跨-model fallback 在 T0/api_messages 留无未配对 tool_use（resume-after-abort test）"。 |
| ADJ-M3 | §4.2 P0/P1 + Package C | **run_command 子命令解析 + TOCTOU 路径拒绝缺失**（CC-03-08/23 P1）。改：Package C 加子交付 "command-text 安全分析"：① `&&`/`||`/`|`/`;` 切分 per-subcommand 跑 `_detect_dangerous_command` 聚合最高 verdict；② run_command 参数路径 shell-expansion 拒绝列表（UNC/`~user`/`$VAR`/`$(cmd)`/glob）镜像 `plan_mode.py:166-174`。加 §4.2 P1 行 "run_command subcommand + TOCTOU safety"，验收：per-subcommand 危险模式 test + 路径语法拒绝 test。 |
| ADJ-M4 | §4.2 P1 + Package D | **prompt-cache 字节稳定/resume 幂等缺失**（CC-04-07/08 P1，唯一主动偏移 CC LAW 处）。改：Package D 加 P1 子交付 "content-replacement byte stability"：首次遇到时持久化 per-tool_call_id ContentReplacementRecord（模型所见字节+eviction 决策），后续 pass 与 resume 逐字 re-apply 不重算/不 flat-截断；eviction 改 exclusive-create-or-skip。验收：resume 产出 byte-identical tool-result 消息 + 保留结果跨 pass 不被 re-evict。 |
| ADJ-M5 | §4.2 P1（SessionGraphV1）+ Package C | **coordinator force-async 缺失**（CC-07-25 P1）。改：Package C/SessionGraphV1 加 "coordinator force-async invariant"：coordinator 模式强制每 delegate/spawn `run_in_background=true`（或从 `COORDINATOR_ALLOWED_TOOLS` 移除阻塞 delegate 仅留 async 变体）。验收：coordinator 发起的 delegation 不能阻塞 leader loop（test 断言 leader 立即返回 run handle）。 |
| ADJ-M6 | §4.2 P0（ToolSpecV1/ToolResultV1）+ Package C | **并行 tool 错误无兄弟-abort**（CC-01-17 P2）。改：ToolResultV1/Package C 注："destructive/Bash 工具在并行批中错误时取消该批 in-flight 兄弟（per-batch child cancel scope 不传播整 turn）"。验收：并行批 Bash 错误取消其兄弟但不取消父 turn。 |
| ADJ-M7 | §4.2 P1（SessionGraphV1）+ Package B | **terminal subagent resume 缺失**（CC-07-29 P2）。改：Package B/SessionGraphV1 continuation_controls 验收加 "resume 已完成 subagent session"（重开 terminal child，过滤孤儿 tool_uses，append follow-up turn）；或显式记为有意 Hive-native 非-parity（new-spawn-only）避免静默债。 |
| ADJ-M8 | §00/§01 章 + §4.2 | **latency-hiding 家族未列**（CC-00-04 StreamingToolExecutor P1 + CC-00-05/06/07 prefetch/summary P2）。这些是 CC-LOCAL runtime 语义（非 remote/SDK）在 scope 内。改：(a) 加 'Latency-hiding overlap' 包（StreamingToolExecutor mid-stream P1 + prefetch/summary P2）或 (b) §4.2/§5 显式记为 "deferred Codex-class 工程优化，本轮接受非债" 附理由。勿让 CC-00-04 静默缺席。 |

### 4.5 可优化（optimization — 不阻断但应改进）

| ID | section | 问题与改动 |
|---|---|---|
| ADJ-P1 | §4.1 A02/A07/A09 | A02 P0 应交叉链 CC-01-04（唯一 P1 terminal-reason 债）；A07/A09 应点名 CC-07-25 coordinator force-async P1（当前隐于矩阵）。改：A09 执行状态加子弹 "Coordinator force-async（CC-07-25，P1）"；A02 链 CC-01-04。 |
| ADJ-P2 | §04 章 + ContextPolicyV1（§4.1/§6）| ContextPolicyV1 schema 漏 autocompact 连败熔断字段，但已是 live 机制（`memory_service.py:344` `_SUMMARY_BREAKER_MAX_CONSECUTIVE_FAILURES=3`）+CC LAW（FreeCode 04 §10.2）。drop live 安全字段易回归。改：ContextPolicyV1 加 `autocompact_failure_breaker_limit`(默认 3)+`breaker_half_open_seconds`。 |
| ADJ-P3 | §6 Package A 验证命令 | `pytest -k "session_contract or turn_state or permission_profile or tool_contract"` 部分空洞：实测 `permission_profile`→**0** test 文件（而它是 P0 阻断），`session_contract`→6、`turn_state`→1、`tool_contract`→1。改：标注 permission_profile 当前无 test，Package A 命令在补齐前不得引为 green；为 PermissionProfileV1 P0 先建 contract test（含"mapped capability 无 policy 行→escalate 非 allow"，对应 CC-03-06）。 |
| ADJ-P4 | §6 全 Package A-G + §8 完成定义 | 完成定义 12 项多为不可独立测试的 prose 谓词（#6/#7/#12 无 test/metric/命令）；仅 Package A 带命令且部分空洞，B-G 无验证命令。违仓库 verification Iron Law。改：每项转 (test-id 或命令, 期望结果) 对；B-G 加验证命令块；#12 加具体 bypass-audit（assert 每入口在 kernel 前调 `invoke_agent`+`append_session_event`；`pytest -k accepted_prompt_first` 跨 9 入口）。 |
| ADJ-P5 | §6 intro "需要时带 migration/backfill" + 全 Package | "需要时"是 MVP-deferral hedge，违 CLAUDE.md "一次改完零债"。多包需 legacy 处理未列：A 冻结 TurnStateV1 须 backfill 历史 RuntimeTask/ChatSession 的 terminal_reason；PermissionProfileV1 须迁散落 mode strings（§8 #5 自承）；ExtensionRegistryV1 须 backfill 现有 MCP/skill/hook install。改：每包加显式 "Migration/Backfill/Rollback" 行（哪些历史行/文件需 backfill、不可逆步的 dry-run+confirm 门、rollback 命令），"需要时"换为 per-package 明确裁定。 |
| ADJ-P6 | §04 章 + Package D | `/context` 诊断 over-claim：`diagnostic_command_runtime.py:222/224/226` 广告 `snip_or_evict`/`read_time_projection_collapse`/`blocking_limit` 三阶段，engine 0 实现（grep=0）。这是 user-facing 假-parity 面。改：改 `:222-226` 仅列 Hive 实跑阶段（tool-result evict、time-based microcompact、mid-loop autocompact、reactive PTL retry），删/标 not_implemented 三标签。验收：test 断言诊断列的每阶段有 live 代码路径。 |
| ADJ-P7 | §4.1 A12 + §08 + Package G | 双源漂移 CC-07-13：`subagent.py:111-135` vs `orchestrator.py:41-60` 两手维护 deny 列表分歧（前者含 ask_user_question/request_plan_mode/check_subagent，后者无）。改：Package C/G 统一两 spawn 路径到单一 base-deny 真源（单 tuple 被 `resolve_subagent_tools` 与 delegation profile 共用）。验收：单 test 枚举 deny 集，subagent+delegation child 一致应用。 |
| ADJ-P8 | 全文 §5 矩阵 vs 267 条 per-item verdict | 粗章状态（partial/near/aligned）未与对抗 per-item 对账，读作"广泛未完成"而证据是"substrate 多 live + ~5 真行为债 + 数项契约统一/observability"。改：每章加 "verified-live vs open" 子节（带 file:line 列已确认 live 项免再论证 + 残留 open 项），章标从笼统 partial 降为 "substrate-live；open: <具体清单>"，把 2 条已证 P1 行为债（run_command 子命令、coordinator force-async）提升为 §4.2 具名可测阻断。 |

---

## 5. 技术债总账（Debt Ledger）

本轮发现的**全部** P0/P1/P2，每条带 id/severity/title/location/fix 方向/owning Package。用户要求本轮结清，故须 exhaustive + executable。

> 说明：行为级真债（破坏 CC-LOCAL 语义或主动偏移 LAW）以 **P1** 计；契约统一/observability/安全覆盖加固以 P1/P2 计；CC TUI/Node 进程内机制与 provider-proprietary 不入债账（已在第 3 节标 hive-native/排除）。真实 **P0=0**（terminal-audit 的 P0 是契约收敛工程项，非已坏行为）。

### 5.1 行为级真债（P1 — 破坏/偏移 CC-LOCAL 语义，必须修）

| ID | severity | title | location | fix 方向 | Package |
|---|---|---|---|---|---|
| D-01 | P1 | terminal reason 枚举缺失（结果由 content 串推断） | `kernel/contracts.py:76-81` | InvocationResult 加 `terminal_reason` 枚举（turn_stop/turn_abort/tool_budget/loop_guard/user_cancel/provider_error/hook_stopped/clarification_required），每 terminal 路径 stamp；UI/replay 读枚举非 content 前缀 | A（TurnStateV1） |
| D-02 | P1 | coordinator 可阻塞式 delegate（无 force-async） | `runtime/coordinator.py:26`；`agents/orchestrator.py:711-754` | coordinator 模式强制 delegate/spawn `run_in_background=true`，或移除阻塞 delegate 仅留 async 变体；test 断言 leader 立即返回 | C/SessionGraphV1 |
| D-03 | P1 | prompt-cache 内容替换无冻结决策（保留结果可被后续 evict） | `kernel/engine.py:1937-2001` | 持久化 per-tool_call_id ContentReplacementRecord（首遇冻结字节+决策），后续 pass 逐字 re-apply 不重算 | D（ContextPolicyV1） |
| D-04 | P1 | resume 非幂等（flat 50K 重截断+合成 call_{id}，主动偏移 CC LAW） | `services/web_chat_runtime.py:414,433-437` | resume 从 ContentReplacementRecord 逐字还原模型所见字节，保原 tool_call_id 与 per-tool 阈值；验收 byte-identical | D（ContextPolicyV1） |
| D-05 | P1 | StreamingToolExecutor 缺失（流式中不执行 tool_use） | `kernel/engine.py:2877→3524→3720` | 评估 mid-stream 工具执行（model 仍生成时首个 concurrency-safe 工具已开跑）；若架构暂不支持，显式记为接受非债并说明理由（勿静默缺席） | A/Latency-hiding（新增） |
| D-06 | P1 | run_command 子命令不解析（整串正则，危险子命令可埋藏） | `tools/governance.py:153-163,557` | `&&`/`||`/`|`/`;` 切分 per-subcommand 跑危险检测聚合最高 verdict | C（命令安全分析） |
| D-07 | P1 | run_command 参数路径无 TOCTOU/shell-expansion 拒绝 | `tools/governance.py`（缺）；对照 `plan_mode.py:166-174` | run_command 路径参数加 UNC/`~user`/`$VAR`/`${}`/`%()`/`$(cmd)`/glob 拒绝列表 | C（命令安全分析） |

### 5.2 契约统一/覆盖缺口债（P1 — 工程契约或 scope 覆盖）

| ID | severity | title | location | fix 方向 | Package |
|---|---|---|---|---|---|
| D-08 | P1 | ToolResult 无 side-effect 通道（newMessages/contextModifier 缺失） | `tools/result_envelope.py:21-53` | 冻结 ToolResultV1 含 new_messages/context_modifier/permission_request/terminal_signal/t0_refs；定义受限 contextModifier 应用（仅非并发-safe 工具，并发批延迟） | A（ToolResultV1）/C |
| D-09 | P1 | 跨-model fallback/abort 孤儿 tool_use 无配对重建 | `kernel/engine.py:2004,3222-3263` | TurnStateV1 加 terminal reconciliation：interrupt/abort/fallback 每 dangling tool_use 补合成 tool_result 再 seal | A/B（TurnStateV1） |
| D-10 | P1 | Command 层无矩阵覆盖（七源汇流未判 parity） | `services/command_registry.py`、`api/commands.py`、`services/session_command_runtime.py`（存在但未审） | 加 Command 层原子行+章：CC 七源→registry 源序、CC bridge-safety→Local Agent Channel 分类、CC mid-session re-eval→availability gating；给 `pytest -k command_registry` | G/A11b（新增行） |
| D-11 | P1 | ExtensionRegistryV1 总账缺失（Skill/MCP/hook/workflow/plugin 无统一治理） | 全扩展面（0 ExtensionRegistry） | 冻结 ExtensionRegistryV1（id/type/source/trust/owner/enabled_scope/exposed+deferred tools/hook_events/permission_req/install_review/runtime_effects/audit_refs），install/load/enable/audit/revoke/replay tests | G |
| D-12 | P1 | PermissionProfileV1 无 test（P0 阻断却 0 测试覆盖） | `tests/`（permission_profile→0 文件） | 建 PermissionProfileV1 contract test（plan/default/acceptEdits/bypass/local/cloud matrix），含 D-13 的 fail-closed 断言 | A/C |
| D-28 | P1 | skill 访问控制 flag 缺失（无 model-invocation/user-invocable/hidden；catalog 无条件列全部 skill） | `skills/registry.py:68`；`app/skills`（0 flag） | skill frontmatter 加访问控制（disable-model-invocation/user-invocable/hidden），`render_catalog` 按 flag 过滤、仅列 model-invocable（CC-08-05） | G |

### 5.3 安全/正确性/observability 债（P2 — 不阻断但本轮应清）

| ID | severity | title | location | fix 方向 | Package |
|---|---|---|---|---|---|
| D-13 | P2 | capability_gate 对 mapped-但-无-policy 容量 fail-OPEN（违 never-silently-grant） | `services/capability_gate.py:435-436` | mapped capability 无 policy 行时默认 escalate/deny（gated by profile default_decision），非硬编码 allow；contract test：mapped+无 policy→escalate | C/PermissionProfileV1 |
| D-14 | P2 | 双源 deny 列表漂移（subagent vs delegation 分歧） | `agents/subagent.py:111-135`；`agents/orchestrator.py:41-60` | 统一单一 base-deny tuple 被两 spawn 路径共用；test 枚举 deny 集断言一致 | C/G |
| D-15 | P2 | 并行 tool 错误无兄弟-abort | `kernel/engine.py:3663,3679-3687` | destructive/Bash 错误时 per-batch child cancel scope 取消 in-flight 兄弟（不传播整 turn） | C |
| D-16 | P2 | terminal subagent 无法 resume（completed-session 续问缺失） | `services/agent_session_continuation.py:26,128-148` | 支持重开 terminal child（过滤孤儿 tool_uses，append follow-up），或显式记 Hive-native non-parity | B/SessionGraphV1 |
| D-17 | P2 | `/context` 诊断 over-claim（广告 snip/collapse/blocking 三未实现阶段） | `services/diagnostic_command_runtime.py:222,224,226` | 改列仅实跑阶段（evict/microcompact/autocompact/reactive-PTL），删/标 not_implemented；test 断言每列阶段有 live 路径 | D |
| D-18 | P2 | tool 结果 eviction 用 write_text 截断覆盖非排他创建 | `kernel/engine.py:1985` | 改 exclusive-create-or-skip（`'x'` 模式或 exists 判断），replay 不静默重写 | D |
| D-19 | P2 | ContextPolicyV1 schema 漏 autocompact 连败熔断字段（live 机制未入契约） | `services/memory_service.py:344`；ContextPolicyV1 schema | schema 加 `autocompact_failure_breaker_limit`(3)+`breaker_half_open_seconds`(600) | D |
| D-20 | P2 | per-tool declared 阈值不被 clamp 到全局 50K（CC 用 min()） | `kernel/engine.py:1963-1964` | declared 阈值与全局默认取 min（Infinity 哨兵除外）；中层 override 缺失记为可接受 | D |
| D-21 | P2 | memory age 仅 stale 路径人类可读（fresh 项/index 仍 ISO） | `assembler.py:31-44`；`memory_navigation.py:45-47` | 统一全 memory 项 'Nd ago' 渲染（或显式裁定仅 stale 需 caveat） | F |
| D-22 | P2 | 无 TRUSTING_RECALL 专用记忆段（memory-claim 代码存在性验证弱于 CC） | `prompt_sections/memory.py`（缺专用段） | 加 TRUSTING_RECALL 段：memory 命名文件/函数/flag 时须 grep/file-check 后再推荐 | F |
| D-23 | P2 | latency-hiding 三项（memory/skill prefetch + tool-summary）缺失 | `engine.py:2219/2317/3690`（同步/无 prefetch） | 评估异步 prefetch overlap；或显式记为接受 Codex-class 工程优化非债 | A/Latency-hiding（新增） |
| D-24 | P2 | state-diff 副作用通道缺失（CC onChangeAppState 等价） | 前后端（0 onChangeAppState） | 评估集中式 state-diff 副作用 dispatch（工程便利，非 SessionWorkbenchV1 必需，可记 nice-to-have） | E |
| D-29 | P2 | 7 个 hook 事件为 `_DISABLED_NOOP` 无 live emitter（"42 全集"口径需修正） | `runtime/hooks.py:195` | 为 SETUP/PERMISSION_DENIED/WORKTREE_*/CWD_CHANGED/FILE_CHANGED/ELICITATION_RESULT 接 live emitter，或显式记为有意未实现（catalog 不宣称 emitter parity 完整） | G |
| D-30 | P2 | hook `updatedMCPToolOutput`/output_rewrite 零消费者（仅 schema 声明） | `runtime/hooks.py:385`（无 consumer） | POST_TOOL_USE 消费 MCP 工具输出 rewrite，或从 schema 删除该面（CC-08-24） | G |
| D-31 | P2 | skill 契约漂移：budget 0.08 非 CC 1%/250 + frontmatter 键(when_to_use/context/agent/hooks)被丢弃 + 无 inline-fork | `skills/parser.py:81-83`；`context_budget.py:523`；`workspace.py:201` | 对齐 listing budget/desc 截断；parser 消费 context/agent/hooks 键（驱动 CC-08-06 fork）；评估 skill-frontmatter hooks 源（CC-08-02/04/06） | G |
| D-32 | P2 | MCP 发现 union 不全（prompts/list→commands、skill://→skills 未导入） | `services/mcp.py:503/556`（仅 tools→DB+resources on-demand） | 扩展 MCP 导入并集：prompts→command_registry、skill resources→skills（CC-08-16） | G |

### 5.4 文档/纪律债（P1/P2 — terminal-audit 自身需改，见第 4 节）

| ID | severity | title | location | fix 方向 | Package |
|---|---|---|---|---|---|
| D-25 | P1 | 完成定义不可独立测试（12 项 prose 谓词无 test 绑定） | terminal-audit §8 + Package A-G | 每项转 (test-id/命令,期望) 对；B-G 加验证命令块；#12 加 bypass-audit | 文档（全 Package） |
| D-26 | P1 | 上游 CLAUDE.md hook-count 漂移（"15-event" vs live 42） | 仓库 `CLAUDE.md` | 修 "15-event lifecycle bus"→"42-event（CC-27 超集）" | 文档 |
| D-27 | P2 | Package "需要时带 migration/backfill" 是 MVP-hedge（违一次改完零债） | terminal-audit §6 intro + 全 Package | 每包加显式 Migration/Backfill/Rollback 行，"需要时"换 per-package 裁定 | 文档（全 Package） |

---

## 6. 与现有 Package A-G 的差异

回答："terminal-audit §6 的 Package A-G 是否覆盖完整债账？"——**主体覆盖，但有 8 处缺口需补**。逐 Package 对照：

| Package | 现有范围 | 覆盖的债 | **未覆盖/需补** |
|---|---|---|---|
| A 冻结 Runtime Contract | AgentSession/TurnState/SessionGraph/PermissionProfile/ContextPolicy/ToolSpec/ToolResult/ExtensionRegistry V1 | D-01(terminal reason)、D-08(ToolResult side-effect)、D-12(PermissionProfile test) | **D-05/D-23 latency-hiding** 无家可归→建议在 A 旁立 "Latency-hiding overlap" 子包或显式接受非债；**ADJ-P3** Package A 验证命令含 0-test 的 permission_profile，须先建 test 才能引 green |
| B Accepted Prompt + Terminal State 闭环 | 9 入口 accepted-prompt-first + terminal | D-09(孤儿配对，部分)、D-16(terminal subagent resume) | **D-09 的 terminal reconciliation 规则**未在 B 显式列；**D-16** 需加 continuation_controls "resume completed subagent" |
| C Tool + Permission 闭环 | tool metadata/result side-effect/preflight/gate/sandbox/patch-exec | D-02(coordinator force-async)、D-06/D-07(命令安全)、D-13(fail-open)、D-14(双源 deny)、D-15(兄弟 abort) | **C 现文未提**：子命令解析、run_command 路径 TOCTOU、fail-open no-policy 默认、双源 deny 统一、兄弟-abort——全部需作 C 显式子交付 |
| D Context + Compaction 闭环 | thresholds/budget/trace/recovery/reactive/post-compact | D-03(冻结决策)、D-04(resume 幂等)、D-17(诊断 over-claim)、D-18(排他写)、D-19(熔断字段)、D-20(min clamp) | **D 现文未提 byte-stability**（D-03/D-04 是本轮唯一主动偏移 CC LAW，最高优先）、诊断 over-claim 修复、熔断字段入 schema——须加入 D |
| E UI Workbench 闭环 | active turn/timeline/tool/approval/hook/compaction/branch/graph/profile | （SessionWorkbenchV1 single source） | **D-24 state-diff 通道**可记 nice-to-have（非必需） |
| F Memory 边界闭环 | T0/T2/T3 bridge/activation/stale-disclosure/extraction timing | D-21(age 渲染)、D-22(TRUSTING_RECALL) | 两项均为 06 章 partial 残留，须加入 F |
| G Extension 闭环 | skill/MCP/hooks/workflow/tool packs/plugin install + trust/audit | D-10(Command 层)、D-11(ExtensionRegistryV1)、D-14(双源 deny)、**D-28(skill 访问控制 P1)**、**D-29~D-32**(hook NOOP 桩/updatedMCPToolOutput 零消费/skill 契约 drift/MCP union 不全) | **D-10 Command 层**当前完全无 Package 归属（矩阵无行）→须在 G 立 Command 子节；08 独立 pass-2 新增 D-28(P1)+D-29~D-32(P2) 全部落 G，ExtensionRegistryV1(D-11) 应把 skill 访问控制 flag + hook emitter 覆盖纳入治理面 |
| **新增建议包** | — | — | **Latency-hiding overlap**（D-05 P1 + D-23 P2）：StreamingToolExecutor mid-stream + memory/skill prefetch + tool-summary；CC-LOCAL 语义在 scope，须有家或显式接受非债 |
| **文档纪律（跨 Package）** | — | D-25/D-26/D-27 | 完成定义可测化、CLAUDE.md hook-count 修正、Migration/Backfill/Rollback per-package 化——非单一包，须贯穿全文档 |

**结论**：Package A-G 的骨架正确（契约冻结优先），但当前**遗漏了本轮全部 7 条行为级 P1 中的 6 条的显式落点**（D-02/D-03/D-04/D-06/D-07 在 C/D，D-05 无家），以及 Command 层（D-10）、诊断 over-claim（D-17）、fail-open（D-13）、双源 deny（D-14）这些已确认的具体断点。按用户"本轮全部结清"要求，必须把第 5 节 32 条债（含 08 pass-2 新增 D-28~D-32）逐条挂到上表"未覆盖/需补"列指定的 Package，并对 latency-hiding 立新包或显式裁定接受非债——否则文档会被当成一串 MVP 切片执行，重蹈 CLAUDE.md "记忆变脏" 的 MVP-deferral 旧债。

---

## 附：本轮独立抽查命中记录（证据可复现）

| 抽查项 | 命令/位置 | 结果 |
|---|---|---|
| HookEvent 数量 | `grep -cE '^\s+[A-Z_]+\s*=\s*"' app/runtime/hooks.py` | **42** enum 成员（含 CC-27；其中 7 个 `_DISABLED_NOOP` 无 live emitter，见 CC-08-19）证实 ADJ-E1/D-26/D-29 |
| 诊断 over-claim | `diagnostic_command_runtime.py:222/224/226` vs `grep -c snip\|context_collapse\|blocking_limit engine.py` | 诊断列 3 标签，engine **0** 实现（证实 D-17/ADJ-P6） |
| coordinator force-async | `grep force_async app/`=0；`coordinator.py:26` 含 delegate_to_agent | 证实 D-02/CC-07-25 |
| capability_gate fail-open | `capability_gate.py:435-436` "allow everything" policy_found=False | 证实 D-13/CC-03-06 |
| 双源 deny 漂移 | `subagent.py:114-134`（含 ask_user_question/request_plan_mode/check_subagent）vs `orchestrator.py:41-60`（无） | 证实 D-14/CC-07-13 |
| run_command 整串检测 | `governance.py:153-163` 整串小写正则，无 shlex/`&&` | 证实 D-06/CC-03-08 |
| terminal reason 缺失 | `contracts.py:76-81` 无 reason 字段 | 证实 D-01/CC-01-04 |
| resume flat 50K + 合成 id | `web_chat_runtime.py:414`(call_{msg.id})/:433(50000 截断) | 证实 D-04/CC-04-08 |
| eviction write_text | `engine.py:1985` write_text 非 wx | 证实 D-18/CC-02-21 |
| Command 文件存在但无矩阵行 | `ls command_registry.py/api/commands.py/session_command_runtime.py` 存在 | 证实 D-10/ADJ-M1 |
| permission_profile 0 test | `grep -rl permission_profile tests/`=0；session_contract=6/turn_state=1/tool_contract=1 | 证实 D-12/ADJ-P3 |
