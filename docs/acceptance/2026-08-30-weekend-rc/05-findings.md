---
document_id: weekend-rc-2026-08-30-findings
owner: Codex
status: active
authority: canonical-active-finding-ledger
last_reviewed: 2026-08-31
source_commit: b23e94210e7e9523bafc3b591b35db8fc2762224
verification_status: admin-audit-default-disclosure-production-verified
---

# 当前 Findings 与 Blockers

[返回索引](README.md) · [当前状态](03-current-status.md) · [Journey Ledger](04-journey-ledger.md)

历史包、旧 PASS 和已被取代的根因只保留在 [archive](archive/README.md)。本文件只接纳当前仍需处理的 finding；旧账内容若没有 fresh reproduction，不自动成为当前缺陷。

## Finding 状态

`Observed` → `Reproduced` → `Fix Candidate` → `Verified` → `Closed`。Review 失败使用 `Review Failed`；确认不属于范围且不是现有契约缺陷时才用 `Excluded`。

只有 `Reproduced` 且已记录最早错误状态的 finding 才能生成修复 Issue。Issue 必须回链本文件的 finding ID 和冻结 Journey ID；worker 回执、PR、CI 或 Issue closed 都不能自动推进 finding 状态。

## 当前 P1/P2 findings

### 当前状态

| ID | 状态 | Severity | Journey | 最早错误状态 | 当前根因边界 | 下一动作 |
|---|---|---:|---|---|---|---|
| SESSION-AUTHORITY-PRESENTATION-001 | Verified | P1 | P29-PADMIN | backend 对无 operator authority 的跨用户 Session message/lineage 返回 `Session not found`，但旧前端仍显示“完成 / Read-only · User / 1 个步骤 / 运行错误”与完整 Session runtime shell | exact `bbf6d234` 在 authority resolution 前只显示 skeleton；403/404 清除 Session timeline/runtime cache并呈现 truthful denied/not-found，安全返回 `/agents`；5xx/network retry 与合法 Session 消费保持原语义 | 保持 `Verified`；provider health/audit finding 已关闭，仍须完成 P29-PADMIN 其余 API/compliance 正向面、pass-2、role-change/reload 与四角色 screenshot matrix 后才可 `Closed` 或写 P29 PASS |
| RUNTIME-GUARD-PRESENTATION-001 | Verified | P2 | P29-PADMIN | runtime protection heading/badge 显示“被保护的任务 0”，却列出 5 条 `active` run 并称“系统保护机制已介入” | exact `6a6695e8` 让 API 的 active reason 表达正常运行；无 protected run 时 UI 诚实标为“最近运行”，保留 active rows 与暂停能力，真正 protected run 仍优先展示 | 保持 `Verified`；provider health/audit finding 已关闭，P29 其余 API/compliance evidence、fault/reload、pass-2 与四角色 matrix 完成后才可 `Closed` 或写 P29 PASS |
| LLM-PROBE-AUDIT-001 | Verified | P1 | P29-PADMIN / P33-GLM | `/enterprise/llm` Test 发生真实 provider/token/cost effect，但 backend 不写 canonical audit；audit UI 只读 legacy agent-bound log，platform admin selected tenant 也未固定到 canonical audit query | exact `cc6e7262` 在 provider effect 前 durable commit started event，终态 durable commit completed event；effect 后 terminal audit 失败返回 non-retryable typed result；canonical selected-tenant audit 与 legacy log 在 UI 合并消费 | 保持 `Verified`；MiniMax/GLM/DeepSeek 的 bounded health verdict 已记录，但 P33 frozen compatibility tasks、P29 pass-1/pass-2、role/fault/negative matrix 均仍 open，不写 Journey PASS |
| AUDIT-DEFAULT-DISCLOSURE-001 | Verified | P1 | P29-PADMIN | admin audit 默认展开 raw details：production DOM 含 `session_id` 110、`job_id/issues` 各 94、`reason` 41、`agent_name` 77、raw provider error 90；search/CSV/API 也可读 raw payload，export/chain 未固定 selected tenant | exact `b23e9421` 以 server summary schema + CSV/search boundary + frontend allowlist 只暴露 control-plane facts；raw canonical evidence 不改写；list/export/chain 共用 selected-tenant RLS scope | 保持 `Verified`；P29 的 employee/company-admin/operator principals、四角色 screenshot/API matrix、双遍与完整 negative/fault 仍 open，不写 Journey PASS |
| TOOL-ARTIFACT-SETTLEMENT-001 | Verified | P1 | P01-MAIN / PJ-02 / PJ-04 | `write_file` effect 已完成，但 canonical terminal `tool_call`/`tool_result` 与 ChatArtifact 在 `chat_artifacts_message_id_fkey` 处回滚；kernel 仍准备下一 provider round | `c37fefc5` 已原子提交 owner/artifact/V2/outbox 并在持久化失败时 hard-stop；`3482b57a` 对 exact unknown-effect invocation fail closed，唯一 operator acknowledgement 保持 unknown fact、禁止旧轮重放并释放 fresh-turn admission | normal/reload 与 supported recovery/no-replay 已 production PASS；保持 `Verified`，完成 clean P01-MAIN/PJ-02/PJ-04 双遍、authority-negative 与 cleanup 后才可 `Closed` |
| SESSION-RETRY-INPUT-001 | Verified | P1 | P01-MAIN / P02-STREAM | edit branch 的 canonical `human_input.accepted` 保存完整 retry prompt，但首个 `result_commit.prepared.bound_input_ids=[]`；provider 未调用工具并错误回复“这条消息只有「1」”，产品仍把 run/final 标成 `completed` | exact commit `2cee9f3e` 的 production retry Session `b3962147…` 已把完整输入绑定为唯一 `bound_input_id=1fd5cc5b…` 并进入 GLM/Work Ledger；随后失败属于独立的 tool-artifact settlement 与 provider 429，不回退本 finding | 保持 `Verified`；完整 P01/P02 双遍、recovery、authority-negative 和 cleanup 后才能 `Closed` |

#### SESSION-AUTHORITY-PRESENTATION-001 复现、修复与生产验证

- production exact commit `3482b57a`、signed-in `platform_admin` 直接打开不属于当前 principal 的 Session URL，且未提交 operator view/reason。message 与 lineage API 正确返回 `Session not found`，DOM 没有跨用户 title/message/transcript/artifact；但前端渲染“会话 / 完成 / Read-only · User / 1 个步骤 / 运行错误”及 runtime/artifact/activity shell，server verdict 与 product presentation 不一致。
- immutable FAIL evidence：`evidence/3482b57a383d3c5bd33a5bcf813b87c6fab23339/P29-PADMIN-fault-denied-session-shell.md`。该文件只证明负向断点，不进入 NPTCR。
- source wiring：direct route 从 `listSessions('mine')` 找不到目标后构造 `is_pending_session_lookup=true` 的只读占位 Session；`selectSession()` 在 403/404 catch 仍构造 `runtime_action_failed/session_load_failed` timeline event，`AgentChatSection` 因 active Session 未清除而展示完整 workbench。
- failing-first mounted test 在 candidate 前精确失败：找不到 `role=alert`，DOM 仍含 `Read-only · User` shell。candidate 后覆盖 403、404、pending no-shell、successful resolution 与返回恢复；最终 frontend **154 files / 1148 tests passed**，i18n 双语各 3993 keys、production build 与 AgentDetail/vendor budgets 通过，Weekend/atomic architecture **24 passed**。
- candidate 只消费 exact `ApiError.status in {403,404}`，属于 server authority/machine contract，不扫描自然语言。它在解析中只显示 skeleton；authority terminal 时清空该 Session timeline/replay/event/runtime 状态并显示 denied/not-found；网络/5xx 继续走既有 durable retry，成功解析继续进入 verified read-only Session。
- production course correction：`d4ae15fd` 已消除假 shell，但从 denied route 返回共享 HR Agent 的 chat 会被产品自动选中另一不可访问 Session；`57823bcf` 排除了 stale ref/Effect 重入后，production hard reload 证明根因是目标 Agent 的默认 chat auto-selection。最终 `bbf6d234` 复用既有数字员工列表作为安全恢复边界，按钮文案为“返回数字员工”并导航 `/agents`，没有新 abstraction 或后端改动。
- exact `bbf6d2340afe593b44f740fabfa178d126b5beca` 已 push；Railway backend `4ad99e93-d3be-48c9-be8d-0107dff44f82`、backend-api `8aa5ccbc-fe9d-4da2-bb39-f16497de044f`、frontend `638da152-1ef6-444c-bcd8-4dd00fa0296d` 均 `SUCCESS`，backend health `status=ok`，frontend HTTP 200。
- signed-in `platform_admin` 对同一负向 URL 的 production DOM 只显示“找不到此会话 / 此会话不存在，或当前账号无法访问 / 返回数字员工”，没有 `Read-only · User`、完成、运行错误、会话交付物或跨用户正文。点击后 URL 精确为 `/agents`，无 stale denied/not-found alert；随后 hard navigation 到合法 MAPLE Session 仍显示 marker `P01-MAIN-PASS1-3482B-MAPLE-581`、完成终局、3/3 todos、一个 artifact、0 running/0 waiting，且无 authority alert。
- immutable production verification：`evidence/bbf6d2340afe593b44f740fabfa178d126b5beca/SESSION-AUTHORITY-PRESENTATION-001-production-verification.md`。该证据只把 finding 推进到 `Verified`；P29 正向 platform health/compliance、双遍、role-change recovery、四角色 matrix 与 cleanup 未完成，NPTCR 仍为 `0/96`。

#### RUNTIME-GUARD-PRESENTATION-001 复现、修复与生产验证

- deployed `bbf6d234`、signed-in `platform_admin` 只读打开 `/enterprise/runtime-budgets`：section heading/badge 为“被保护的任务 0”，同一 section 却有 5 条 `active` run、5 个“暂停”按钮，且每条原因均为“系统保护机制已介入”。没有点击暂停或保存。
- live wiring：`WorkspaceRuntimeBudgetsSection` 计算的 `protectedRuns=[]`，却用 `protectedRuns.length > 0 ? protectedRuns : runs.slice(0, 5)` 渲染普通 recent runs；backend `_user_reason('active', None)` 未有 explicit branch，落入 intervention 默认值。错误同时存在于 canonical API presentation 与唯一 control-plane consumer，不是纯翻译问题。
- 最小共享修复：backend explicit active reason 为“运行正在正常进行”；frontend 在无 protected run 时把同一 fallback 列表/计数/说明标为“最近运行”，继续保留 active “暂停”控制；一旦存在 protected run，仍沿原路径优先显示 protected section。无 schema、迁移、依赖或持久配置改动。
- production-shaped RED：backend helper 精确得到旧 intervention 字符串；frontend static render 精确得到 protected heading + active row + intervention reason。GREEN：focused backend 8 / frontend 6，相邻 backend 87 / frontend 142；完整 backend **8439 passed, 2 skipped, 1 warning**、frontend **154 files / 1149 tests**，i18n 3995/3995、Ruff/format、production build/budgets、24 architecture tests 与 manifest validator 全绿。
- exact `6a6695e88d915a0e37b44e64dcdfe5bdd90a9454` 已 push；Railway backend `cdef3ce1-85e6-4662-a5aa-a6fb9793a21b`、backend-api `2261b169-3c8a-4c3e-a42b-7a1239b2b8e2`、frontend `feb46b17-e017-457a-8c09-b94065730ce1` 均 `SUCCESS`，backend health `status=ok`，frontend HTTP 200。
- production hard navigation 后页面显示“最近运行 5 / 最近的运行活动；正在运行的任务可在此暂停。”；5 条 active row 全部显示“运行正在正常进行 / 等待当前运行完成”，5 个暂停按钮仍在，旧“系统保护机制已介入”和“被保护的任务”均不在 DOM。证据：`evidence/bbf6d2340afe593b44f740fabfa178d126b5beca/P29-PADMIN-fault-active-runtime-guard-presentation.md` 与 `evidence/6a6695e88d915a0e37b44e64dcdfe5bdd90a9454/RUNTIME-GUARD-PRESENTATION-001-production-verification.md`。
- finding 推进为 `Verified`；当前没有 production protected run 用于正向 protected-state screenshot。后续 `LLM-PROBE-AUDIT-001` 已关闭 provider health audit 断点，但 P29 其余 API/compliance evidence、fault/reload、pass-2、四角色 matrix 尚未完成，所以 P29 不写 PASS，NPTCR 保持 `0/96`。

#### LLM-PROBE-AUDIT-001 复现、修复与生产验证

- deployed `6a6695e8` 的 signed-in `/enterprise/llm` health Test 会真实调用外部 provider，但 `test_llm_model()` 没有 audit writer；`/enterprise/audit` 无对应事件，前端也只消费 agent-bound legacy audit log。pre-fix bounded verdict 为 MiniMax success `7623ms`、DeepSeek 一次 `HTTP 402 Insufficient Balance`、GLM success `7575ms`；没有重试 DeepSeek、充值、换 credential 或修改模型配置。
- 最小共享修复在 provider effect 前写并 commit `llm_model.test_started`；provider success/failure 后写 `llm_model.test_completed`。两者共用生成的 probe/request ID，只持久化 provider、model、max_tokens、phase、success、latency 或 exception type。started audit 不可用时 HTTP 503 且禁止 provider call；effect 后 terminal audit persistence 失败则返回 `retryable=false` typed result，保留 started evidence 并禁止自动重试。
- `/enterprise/audit` 复用 selected-tenant server authority；frontend 并行读取 canonical security audit 与 legacy operational audit，合并排序后展示 action/event/severity/resource。没有 schema、migration、dependency、feature flag 或持久配置。
- RED：正确 Python 3.12 venv 下 backend 5 failures、frontend 2 failures；GREEN：focused backend 6、selected-tenant API file 22、frontend adjacent 34。full gates：backend **8443 passed, 2 skipped, 1 warning**；frontend **154 files / 1149 tests**；i18n 3995/3995、9 node tests、Ruff/format、production build/budgets、24 architecture tests、manifest validate 与 diff check 全绿。
- exact `cc6e726218bd491120f942edfa91e51d2d167ff4` 已 push；首次部署因手工错误扩展 short SHA 且脚本未 fail-fast，三个空上传 deployment `446bb56e…` / `771d44b3…` / `7f139625…` 均立即 `FAILED`，未替换运行实例。恢复后以 `git rev-parse HEAD`、`set -euo pipefail` 和 archive 内容检查重新上传；backend `f619e4a9…`、backend-api `7edd592d…`、frontend `beb9cd36…` 均 `SUCCESS` 并绑定 exact full SHA，health/HTTP 通过。
- post-fix 只点击 GLM Test 一次；probe `a0f1be98-27bd-4d69-9bde-247b57c6b16c` 在 `05:21:32` started、`05:21:36` completed，`zhipu/glm-5.3`、`max_tokens=16`、`success=true`、`latency_ms=3411`。audit hard reload 后 started/completed 各一、同 probe ID 恰出现两次、无 raw API key、无第二次 provider call。
- immutable evidence：`evidence/cc6e726218bd491120f942edfa91e51d2d167ff4/LLM-PROBE-AUDIT-001-production-verification.md`。finding 推进为 `Verified`；P29 四角色/双遍/fault/negative 与 P33 三模型 frozen compatibility tasks 仍未完成，NPTCR 保持 `0/96`。

#### AUDIT-DEFAULT-DISCLOSURE-001 复现、修复与生产验证

- exact deployed `cc6e7262` 的 signed-in `/enterprise/audit` 默认合并 400 条记录；DOM 量化为 `session_id=110`、`job_id=94`、`issues=94`、`reason=41`、`agent_name=77`、raw `Insufficient Balance=90`。无需 operator reason 或展开即可读到用户 recovery note、Session/job identity 与 raw provider payload。
- live API trace 证明 legacy list 返回完整 `details/user/ip`，canonical list 返回完整 `details/ip/user-agent/hash/execution identity`；raw details 还参与 admin search 并进入 CSV。export/chain 直接使用 home tenant，未复用 platform-admin selected tenant resolution/pinning。
- 最小共享修复保留 action、actor/resource、hash identity 与明确 model/runtime summary fields；server response、CSV 与 frontend consumer 均用 exact key allowlist，raw details 不再参与 admin search。canonical DB row/hash input 不改写；list/export/chain 共用 `resolve_and_pin_tenant_scope()`。没有 schema、migration、dependency、feature flag 或生产数据变更。
- RED：backend 4、frontend 1；GREEN：backend adjacent 30、frontend module 3。full gates：backend **8448 passed, 2 skipped, 1 warning**；frontend **154 files / 1149 tests**；i18n 3995/3995、9 node tests、Ruff/format、production build/budgets、35 architecture tests、manifest validate 与 diff check 全绿。
- exact `b23e94210e7e9523bafc3b591b35db8fc2762224` 已 push；backend `03d0919e…`、backend-api `b0bb7ca3…`、frontend `0dd299d8…` 均 `SUCCESS` 并绑定 full SHA；health `ok`、RLS strict、runtime bus no error、frontend HTTP 200。
- production hard reload 后仍有 400 条记录；GLM probe ID 恰两次且 provider/model/success 可读，六类 raw/default disclosure counts 全部为 0。跨用户 Session hard navigation 仍收敛到 truthful not-found，无 workbench/artifact/body。
- immutable evidence：`evidence/b23e94210e7e9523bafc3b591b35db8fc2762224/AUDIT-DEFAULT-DISCLOSURE-001-production-verification.md`。finding 为 `Verified`；单一 platform-admin 身份不能替代 P29 四角色/双遍/完整 fault-negative evidence，NPTCR 保持 `0/96`。

#### TOOL-ARTIFACT-SETTLEMENT-001 复现证据

- production application `2cee9f3ec09c7191ed4eda3c70a7c01206341b89`；Session `b3962147-07cd-4223-8f23-f00193d7735c` / RuntimeTask `76a32f8e-f5d8-5a63-b02a-e591598321e9`。
- canonical sequence `304` 为 `tool_call.started`，`305` 为 `effect_started`；受治理 `write_file` 已把 `workspace/WEEKEND-RC-P01-MAIN-PASS-1.md` 从 size `1914` / SHA prefix `52313b…` 改为 size `1508` / SHA prefix `ffdb3f…`，但没有 matching `tool_call.completed` / `tool_result.completed`。
- production log `2026-08-30T15:15:26Z` 记录 PostgreSQL `ForeignKeyViolationError`：`ChatArtifact.message_id=32e6d45a-6bfd-5f9c-920b-14f7db5c98eb` 没有对应 `ChatMessage`；事务回滚 canonical settlement，但 kernel 在 sequence `308` 仍准备 provider round six。
- 后续 sequence `309/310` 是独立的 Zhipu HTTP 429 / code `1302` typed terminal；rate limit 不是 FK 根因，也不能把缺失 receipt 变成 retry-safe。
- signed-in reload 如实显示 `失败`、0 running、0 waiting、0 delivered artifacts、Work Ledger 1 completed / 2 open；没有自动 replay，但用户仍无法从预期 artifact surface 消费已写文件。
- immutable FAIL evidence：`evidence/2cee9f3ec09c7191ed4eda3c70a7c01206341b89/P01-MAIN-fault-rate-limit-artifact-settlement.md`；该文件不进入 NPTCR。

#### TOOL-ARTIFACT-SETTLEMENT-001 修复候选与验证

- 无 production DDL。canonical terminal path 使用 invocation-derived deterministic message ID，并在同一事务内先创建 tenant/agent/principal/session-bound compatibility `ChatMessage`，再创建 `ChatArtifact`、`tool_call.completed`、`tool_result.completed` 与 outbox；无 artifact 时删除 provisional owner，settled replay 不重新读取已删除 workspace source。
- terminal settlement 异常会在独立恢复事务把 invocation 标成 `needs_reconciliation`，保留原 evidence 且不生成语义 tool result；typed `ToolLifecyclePersistenceError` 同时终止串行、并行和 pre-effect callback 旁路，RuntimeTask 不生成 assistant prose、禁止 automatic retry 并保留 exact file-change facts。
- frontend Session V2 serializer/reducer 投影 `message_id`；canonical tool pair 以该 ID 取代 compatibility row，同时消费 canonical artifact parts，避免重复卡片或附件丢失。compatibility anchor 不持久化 governed args 或 raw result，只持久化 provider-visible projection。
- production-shaped RED：真实 PostgreSQL 精确复现 FK failure 且 invocation 停在 `effect_started`；并行 pre-effect fence 测试证明旧分支会吞 typed failure 并继续 provider。GREEN：真实 PG 覆盖 FK/anchor/artifact/V2/outbox/idempotent replay/quarantine/legacy ordering，kernel 串并行均 hard-stop。
- final local gates：核心交叉 **330 passed**；完整 backend **8428 passed, 2 skipped, 1 warning**；frontend **1143 passed**、production build 和 AgentDetail/vendor bundle budgets 通过；Ruff、format、`git diff --check`、manifest `valid=true` / denominator `96` / hash `d320edce…` 均通过。
- application commit `c37fefc56b92e658bfb64a3e79d685249a2a3add` 已 push；Railway backend `62e4ef56-7e6b-456e-a505-fea90fd286a0`、backend-api `307f0df7-6ae0-4c57-817e-f9ca07fd59fc`、frontend `db6b605d-7b8b-40ea-8da8-247259db29f8` 均 `SUCCESS` 且 message 绑定该 exact commit。公共 backend health `status=ok` / `runtime_control_bus.last_error=null`，frontend HTTP 200；这些只关闭部署原子。
- 新部署后的 signed-in reload 保持旧故障 run 为 `失败`、0 running、0 waiting、0 artifacts，没有自动重放不确定 write effect。
- fresh normal revalidation：owner action-time 确认后只发送一次 `D3-SETTLEMENT-C37-8K4P`；Session `0731ec15-c662-4552-9500-3f68f1094f11` / RuntimeTask `c124e51f-c09e-5b0d-9265-38b48ae0db27` 在 GLM-5.3 下 `completed`。canonical invocation 恰为 `write_file` 一次、`read_file` 一次，两个 span 均 `status=ok`，两个 invocation 均 `effect_committed`、无 recovery owner。
- write canonical order 为 sequence `121 started → 122 effect_started → 123 tool_call.completed → 124 tool_result.completed`，下一 provider round 到 `127 result_commit.prepared` 才开始；read 为 `167 → 168 → 169 → 170`，下一 round 到 `173 prepared` 才开始。write terminal pair 共用 message ID `07afe8cd-ff96-5c03-b0f1-e54ca9c12462`；对应 ChatArtifact `be17c252-8a97-4782-ae3e-17e05d2f3519`、ChatMessage owner、目标 path/snapshot 均恰一行；terminal outbox 均 `published`、attempts 1、no error。invocation/event/run reconciliation 计数全为 0。
- artifact snapshot 为 77 B，三行正文无尾随换行，content SHA-256 `2c3f309736338d6185614a50e56875de7fc1092cd239c765b7df1661f7ec07e6` 与期望字节完全相等；canonical read tool-result event `24dabf4f…` 的 529 B provider-visible wrapper `contains_expected=true`，完整包含同一 77 B 字节。normal UI 显示精确 final、一个文件、一个 session artifact、0 running/0 waiting；hard reload 后仍为同一 Session/run/tool pair/artifact，无自动 replay；普通用户从「打开」入口成功消费保存快照预览。
- 机械取证使用 Railway backend 内 `asyncpg` readonly transaction + tenant `set_config`，仅显式 tenant/session SELECT 并 rollback；无 credential 输出、DDL、DB 写或 RLS 绕过。探针窗口 backend/backend-api 对 `ForeignKeyViolationError`、`ToolLifecyclePersistenceError`、`needs_reconciliation`、`tool_lifecycle_persistence_failed`、`chat_artifacts_message_id_fkey` 过滤均为 0。immutable bounded evidence：`evidence/c37fefc56b92e658bfb64a3e79d685249a2a3add/TOOL-ARTIFACT-SETTLEMENT-001-normal-revalidation.md`。
- 上述 `c37fefc5` 证据在当时只关闭 normal/reload/Consumption 子路径；它本身不证明 supported recovery，也不把 P01-MAIN/PJ-02/PJ-04 写成 PASS。后续 `3482b57a` 证据单独关闭该恢复门。
- `3482b57a` recovery candidate 统一使用 `SessionToolInvocation.result_event_id IS NULL + effect_state in {effect_started, needs_reconciliation} + recovery_owner non-null + terminal RuntimeTask` 的 exact predicate；fresh turn、branch、central run admission、抢先 admitted-input worker recovery、Session workbench 与管理员队列共用该事实源。operator acknowledgement 只追加 `tool_call.reconciled` / `recovery_action.reconciled`、清除 operational hold 并停止旧 NR task；invocation 仍保留 unknown state，绝不制造成功/失败 tool result 或重放旧 provider round。
- failing-first 回归实现前 4 项失败；实现后后端定向 **310 passed**、完整 backend **8438 passed, 2 skipped, 1 warning**，frontend **1145 passed**、i18n、production build/bundle budget 全绿；Ruff/format、diff check、18 条 Weekend/atomic tests 与 manifest validator 均通过。结构门曾因 AgentChatSection 2439>2400 失败，未放宽阈值，提取独立 recovery/feedback surface 后降至 2392 行并通过。
- application commit `3482b57a383d3c5bd33a5bcf813b87c6fab23339` 已 push；Railway backend `7c196980-34c6-4846-bf25-0397b7b55c0e`、backend-api `8e7545b8-9b6c-4b32-a77d-48883191728a`、frontend `6f6bd18c-1681-4049-ac20-6660a3f84fc3` 均 `SUCCESS` 且 message 绑定该 exact commit。
- production read-only precheck：旧 D2 Session hard reload 后显示 unknown-effect alert，generic `重试本轮` 消失，composer/发送 disabled，0 running/0 waiting；管理员队列把 run `76a32f8e…` 提升到首项，只显示必填 evidence note 与 disabled acknowledgement，没有 generic resolve/archive/retry。该 pre-action checkpoint 见 `evidence/3482b57a383d3c5bd33a5bcf813b87c6fab23339/TOOL-ARTIFACT-SETTLEMENT-001-recovery-admission-precheck.md`。
- production supported recovery：只对 `76a32f8e…` 填入已核验 workspace 文件事实并点击 acknowledgement 一次。目标 invocation `1dcbdf47…` 仍为 `needs_reconciliation`、`result_event_id=null`，`recovery_owner` 清空，receipt 指向 sequence `312 tool_call.reconciled`；sequence `313 recovery_action.reconciled` 与前者 outbox 均 `published` / attempts 1。旧 RuntimeTask 保持 `failed`，没有制造 `tool_result`、artifact 或第二个旧 run。
- fresh-turn/no-replay proof：同一 Session 新 input `ad602cdc…` 只绑定独立 run `f8cdd9ac…` 的唯一 round，0 tool invocation，sequence `375` 逐字为 `D4_RECOVERY_OK`，sequence `385/386` 正常终局。Session hard reload 为 prompt/final 各一、0 blocker/running/waiting/Stop；Workspace 原文件与 marker/两个验收字段各一；管理员目标 row 0、error 0。完整证据见同目录 `TOOL-ARTIFACT-SETTLEMENT-001-recovery-verification.md`。
- 因 normal path、failure hold、operator evidence action、canonical no-result reconciliation、no-replay 与 fresh-turn release 均已在 exact deployed code 上成立，本 finding 推进为 `Verified`。authority-negative、cleanup 与完整 P01-MAIN/PJ-02/PJ-04 双遍仍 open，故 Journey 不写 PASS，NPTCR 保持 `0/96`。

#### SESSION-RETRY-INPUT-001 复现证据

- production application commit `d0c9fffd1ca4995ddea6d367e04e206e973560d5`；失败源 Session `d1a2c63f-7082-424d-a9f3-a3330398e371` / run `ff9536bd-39fa-5bf3-bd02-f07aa6fb0e81`，edit retry branch Session `ef9d6498-f4dc-49c1-a566-6446e220f0ef` / run `03419d5f-6166-479d-ad02-d929759c57df`。
- 源 run 在 3-step plan、3 todos、受治理 `write_file` + `read_file` 后于 final 前收到 typed `provider_error`；目标文件已正确生成。点击产品唯一一次“重试本轮”后，branch run 无 tool calls、Work Ledger 为空、无新 artifact effect，却返回“看起来这条消息只有「1」”并被 terminalized 为 `completed`。
- branch canonical transcript seq `1` 是完整 1,300+ 字符 P01-MAIN prompt，含 marker `P01-MAIN-P1-CEDAR-734`、固定表格与受治理写读要求；branch lineage 为 `mode=edit`、source/root `d1a2c63f…`、anchor `b0004973…`、`copied_event_ids=[]`。
- signed-in operator workbench 的 `hive.session_semantic_history_receipt.v1` 合法返回 `status=empty`、`event_count=1`、`message_count=0`，说明 fresh edit branch 没有应继承的 prefix；但 model request seal 的 `bound_input_ids=[]`，且 runtime summary `used_tools=[]`。因此错误位于 current-run input admission/binding，不是 `SESSION-CONTEXT-001` 的跨轮 history 复发。
- current checkout wiring proof：正常 create/start Session routes 调 `submit_live_human_input()`，后者创建 Session V2 command/input、运行 Hook admission 并由 `session_input_dispatch` 用确定性 run ID 启动 `append_user_message=False` 的 RuntimeTask；branch route 却直接调用 `start_web_chat_run(..., append_user_message=True)`。kernel provider loop 只从 canonical history 与 `round_input_bind()` 获取 user messages，不消费 `RuntimeTask.prompt` 作为普通当前输入，所以这一旁路会发送无当前输入的 provider request。
- Attempt 1 与 retry 均保持 `FAIL`；文件没有被 retry 重写，故未发生重复 effect，但“无 effect + 错 final + completed”本身是 P1 假成功。NPTCR 仍为 `0/96`。

#### SESSION-RETRY-INPUT-001 修复候选与本地验证

- live-entry candidate 只改变 branch API 的 run admission：`edit`、`insert_before`、`insert_after`、`reply`、`side_question` 统一调用 `submit_live_human_input(requested_kind="start_turn")`；input ID 和 idempotency key 由已创建 branch Session 与 mode 确定性派生。若 admission 不产生 run，branch receipt 保存 typed input/admission/dispatch 状态；不伪造 completed。
- `regenerate` 不代表一条新 HumanInput，继续以 `append_user_message=false` 启动并消费 branch 已复制的 canonical user prefix；专项测试阻止重复 checkpoint。
- production-shaped RED：API entry 测试命中 legacy `start_web_chat_run()` 旁路；真实 PostgreSQL live API 测试在 branch Session 下找不到 `SessionTurnInput`。GREEN：同一测试证明 1,300+ 字符 Unicode prompt 字节忠实成为唯一 round-one user message，`SessionModelResult.bound_input_ids_json` 精确包含该 input ID。
- 定向 branch/history/accepted-prompt-first **43 passed**；加 Session V2 input control 与 Weekend/atomic gates 的 cross-domain **139 passed**；完整 backend **8419 passed, 2 skipped, 1 warning**。Ruff check 全仓通过，三条变更代码/测试的 Ruff format check 通过，manifest `valid=true` / denominator `96` / hash `d320edce…`，`git diff --check` 通过。全仓 format check 报告 43 个不属于本次 diff 的既有未格式化文件，按 scope preservation 未修改。
- immutable production failure evidence：`evidence/d0c9fffd1ca4995ddea6d367e04e206e973560d5/P01-MAIN-fault-provider-overload-retry-input-loss.md`。
- production application `2cee9f3ec09c7191ed4eda3c70a7c01206341b89` 的 supported retry 建立 Session `b3962147-07cd-4223-8f23-f00193d7735c` / run `76a32f8e-f5d8-5a63-b02a-e591598321e9`；round one 的 `bound_input_ids` 精确为非空 `1fd5cc5b-8378-5629-8cdc-98fd8250f27f`，GLM 消费完整 prompt 并创建 3 个 todos。随后暴露的 FK settlement P1 与 provider 429 是新的独立断点，因此本 finding 推进为 `Verified`，但 Journey 仍为 `FAIL`、NPTCR 仍为 `0/96`。

### 已验证、关闭门待补

| ID | 状态 | Severity | Journey | 最早错误状态 | 当前根因边界 | 下一动作 |
|---|---|---:|---|---|---|---|
| SESSION-CONTEXT-001 | Verified | P1 | P01-MAIN / P02-STREAM | 同一 Session 第二轮 provider request 未获得上一轮 user/assistant 语义历史，模型明确称“这是本会话我收到的第一条消息” | exact commit `d0c9fffd` 已把 tenant/agent/session-bound canonical transcript、committed model seal、settled tool results 与 anchored legacy rows 接入 live runtime；无固定消息窗口、无不可用 silent fallback，并覆盖 rewind/branch/current-run ownership | 保持 `Verified`，完成完整 P01-MAIN/P02-STREAM signed-in 双遍及 production fault/recovery、authority-negative 后才可按关闭合同推进 `Closed`；无需再改同一根因代码 |

#### SESSION-CONTEXT-001 复现证据

- production application：`eb61d468221aa22a4f22c1d96353baadef3b51e6`；实验 Session：`59257e7a-960b-459a-9652-2ff39be117ee`。
- 第一轮 run `2fa2f887-b76e-556c-99c8-3a814c37f27b` 正确生成 No-Go 判断及可观察恢复证据；第二轮 run `58b222f2-b52b-5cb1-b5a1-f657ced4222a` 在相同 Session 审计上一轮时否认存在上一轮。
- `GET .../transcript?limit=1000&schema_version=2` 返回 sequence `1..635`，包含两组 `human_input.accepted`、`assistant_text.snapshot`、`run.completed`；第一轮 prompt 和完整 assistant snapshot 均可从 canonical payload 读取。
- `GET .../messages` 返回 10 行，全部为 `system`（model route、memory degradation、context window、provider ledger），user/assistant 为 0。
- current checkout wiring：`web_chat_runtime._load_runtime_context()` 查询 `ChatMessage`；`web_chat_run_orchestrator` 把该 history 转为 `state.conversation`；Session V2 当前输入另由 `bind_round_inputs()` 注入。因此第二轮只有本轮 input，旧轮语义历史未进入 provider request。
- 这不是“Memory degraded”本身：Memory 事件明确说明可继续，且同一 Session 原始对话历史属于 Session lifecycle，不应依赖 Personal/Agent Memory 检索。
- 本次证据只建立 P1 与最早断点，不是最终根因修复设计，也不构成 P01/P02 PASS。

#### SESSION-CONTEXT-001 修复与生产复验证据

- application commit：`d0c9fffd1ca4995ddea6d367e04e206e973560d5`；Railway backend `ce0bdbf4-c8b6-4cd3-bbe2-77e74a75ca2e`、backend-api `ef4f7c81-b8cb-44d8-bbd7-37499e1765fb`、frontend `f6932ba1-9f7e-4b61-8b38-54ae709ba278` 均为 `SUCCESS` 且 message 绑定该 exact commit。
- fresh production Session：`3ce68041-ccc4-4d4e-b729-ec9ace46d222`。P01 probe run `71cffdb6-ef6b-53fa-9a63-ea57ac98349f` 的用户输入含唯一 marker `HIVE-CANONICAL-Q7M4-83NP`，assistant 只输出 `ACK-FIRST`，因此没有把 marker 复制到 assistant history。
- P02 probe run `40c3e678-0ca9-59f8-8abd-e65ef64a4cf9` 的当前输入不含 marker，却正确输出 `HIVE-CANONICAL-Q7M4-83NP NO_TOOL`；这证明真实 provider path 消费了上一轮用户语义及“未要求工具”的上下文。
- 通过受支持的 signed-in operator workbench 读取 P02 `hive.session_semantic_history_receipt.v1`：`status=complete`、`truth_source=chat_transcript_events+session_model_results`、`message_count=2`、`user_checkpoints=1`、`committed_provider_messages=1`、`settled_tool_results=0`、`mechanical_message_limit_applied=false`、`held_items=[]`，并在 `excluded_current_run_input_ids` 中排除 P02 当前输入。P01 receipt 为 typed `empty`，符合 fresh Session 预期。
- 以上关闭了已复现根因的 production 语义回归，但没有覆盖 P01-MAIN 的开放任务/deliverable，也没有覆盖 P02-STREAM 的 Markdown streaming、terminal/reload/duplicate/flicker，更没有完成 production fault/recovery 和 authority-negative；因此 finding 仅为 `Verified`，两条 Journey 均不写 PASS，NPTCR 保持 `0/96`。

### 尚待 fresh reproduction

| ID | 状态 | Severity | Journey | 观察/假设 | 下一证明动作 |
|---|---|---:|---|---|---|
| UI-CMD-001 | Observed | P2 candidate | PJ-03 | `/skill` 与 `/agent` 可能返回目标 subview，但 Agent extensions/selector 未消费目标，仍停在默认 catalog | signed-in UI 分别输入命令，记录 URL、selected tab、目标对象和 reload |
| UI-CMD-002 | Observed | P2 candidate | PJ-03 | `/workflow` 可能只切换 tab，没有打开指定 draft/preview | signed-in fresh draft 逐字段复现，追踪 `ui_action → route → consumer` |
| UI-CMD-003 | Observed | P2 candidate | PJ-03 | `/context`、`/usage`、`/permissions` 可能缺少目标 panel，最终只显示 “Command completed” 或内部 ID | 逐命令复现 success/failure/cancel/reload，确认可读 panel 是否存在 |
| KNOWLEDGE-UI-001 | Observed | P1/P2 candidate | PJ-09/PJ-10/PJ-11 | Agent Knowledge 消费 `entries + pages`，可能把 Agent Memory、Personal KB、Company KB 混成一个不诚实状态 | 从 employee Agent Detail 逐层核对来源、owner、authority 和空/拒绝/不可用状态 |

以上均为候选，未完成 fresh reproduction 前不得修改代码或宣称根因。

## 外部前置条件，不归类为产品 finding

| ID | 状态 | 历史事实 | 允许的当前动作 |
|---|---|---|---|
| BLOCKER-MODEL-001 | BLOCKED_PRECONDITION | MiniMax 与 GLM bounded production probe 成功；DeepSeek exact binding 已确认，但唯一 live probe 返回 `HTTP 402 Insufficient Balance` | 保留 typed blocker且不重试；充值、billing 或 credential change 需 action-time 授权，恢复后仍须执行 P33 frozen compatibility task |
| BLOCKER-BRIDGE-001 | BLOCKED_PRECONDITION | Hive Connect daemon running，但 `hive-connect status` fresh 返回 `401 Invalid bridge token`，UI linked `0` / offline | 保留 blocker；re-login/token replacement 需 action-time 授权 |

外部 blocker 必须在执行窗重新验证；恢复后回到真实语义路径，不以旧失败永久阻断。

## 严重度

| 级别 | 定义 |
|---|---|
| P0 | 越权、跨租户泄漏、数据破坏、不可逆错误、全局不可用 |
| P1 | 核心旅程阻断、永久非终态、假成功、证据丢失、不可恢复 |
| P2 | 外部测试者可见且显著破坏理解或信任 |
| P3 | 不阻断任务的小型一致性或美观问题；不能自动延期 |

## Finding 关闭合同

每个 finding 必须链接：冻结 journey、最早错误状态、live-entry wiring proof、production-shaped failing regression、最小共享根因修复、focused/cross-domain/full/真 PG/build gates、exact commit、三服务部署、signed-in pass 1/2、fault/recovery、authority negative 和 rollback。

禁止只隐藏 UI、字符串猜语义、放宽断言、用 fake pin 孤儿路径、在失败 provider 上盲重试，或把一个能力的证据外推给另一个能力。
