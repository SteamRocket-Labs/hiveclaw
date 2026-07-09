# Agent-Native 清债优先综合落地方案（CC 报告复核后独立版）

日期：2026-07-09
当前 Hive HEAD：`9614e099564b8ba7bc6669366cdee007f1546821`
被 review 文档：`docs/final-atomic-review-2026-07-09.md`
本方案定位：综合 CC 报告、当前 checkout 取证、前一版断点总览，重新写一份可落地、可测试、可被 CC 反审的施工方案。

## 0. 总原则

本方案把工作拆成两个互不混淆的部分：

1. **第一部分：当前债务收尾。**
   只处理已经存在、已经半接、已经进入主链、或者当前文档/代码已经承诺的能力。目标是让现有 Agent-native 底座干净、闭环、可维护。
2. **第二部分：新能力建设。**
   公司知识库和飞书权限系统都属于新系统建设。它们可以先做接口边界和设计冻结，但不能在第一部分未完成前开工实现。

第一部分完成前，禁止借“架构升级”名义推进 Company KB、飞书权限系统、全新企业治理产品面或大规模 Dynamic activation 重构。否则旧债没清完，新系统会压在不干净的地基上。

## 1. CC 报告 review 结论

### 1.1 直接采纳

| CC 报告点 | 当前复核结论 | 处理方式 |
|---|---|---|
| `tenant_id=None` 导致治理/RLS 互锁 | 成立。`resolve_tenant_for_agent` 调用面很广，background runtime 一旦拿到 None，后续治理和 RLS 会出现“看似权限拒绝，实际租户前置条件缺失”的混乱。 | 第一部分 P0 |
| `/steer` 孤儿别名 | 成立。`session_command_runtime.execute_session_command()` 支持 `turn_steer` 和 `steer`，但 `command_registry` 只注册 `turn_steer`。 | 第一部分 P0 |
| workspace rewind UI 半接 | 成立。后端 `rewind_mode=conversation|workspace|both` 和 `confirm_workspace_restore` 存在，前端 checkpoint selector 仍主要走对话 rewind。 | 第一部分 P0 |
| T3 two-plane 与 legacy flat T3 双轨 | 成立。prompt 和新读面偏 two-plane，但 `auto_dream._T3_FILES`、`reference_index._T3_FILENAMES`、部分 view 仍引用 legacy 文件名。 | 第一部分 P1 |
| `codex_optimization_ledger` 本机路径脆弱 | 成立。控制面 payload 不应携带 `/Users/rocky243/...` 这种本机绝对路径作为可运行事实。 | 第一部分 P1 |
| STRICT capability mapping 是新工具上线风险 | 成立，但不是当前 drift。它是机制性清债点：必须有测试和启动审计防止新工具未注册就上线。 | 第一部分 P1 |

### 1.2 需要修正

| CC 报告点 | 修正意见 |
|---|---|
| “系统核心健康，距终局只差 P0/P1” | 这个判断对单 Agent 主链基本成立，但对整个 agent-native 系统过乐观。Company KB、飞书权限、统一 PermissionDecision、完整 Workbench V2 仍不是“清债后自然完成”的状态。 |
| C1 修复只写“daemon 短路” | 不够。需要一个可复用的 Runtime Tenant Admission 入口，所有 background runtime 都先产出结构化 precondition 结果，不能各自手写短路。 |
| `promotion_router.py` 直接算死代码 | 先按 orphan suspect 处理。清理前必须用 graph/import/test 证明没有 runtime consumer，再决定 retire 还是接线。 |
| `activation_feedback.jsonl` / `decay_signal` 直接算孤儿 | 不应简单删除。当前已有 writer 和测试，问题更像“sidecar 读模型和保留策略未闭合”，应按 telemetry loop cleanup 处理。 |
| Dynamic activation M1-M9 放进 P2 | 方向可以保留，但不应进入第一部分。第一部分只修当前 Dynamic 注入已经造成的脏点，不做新 scoring 方程大改。 |
| `PermissionDecision` / `ContextAssemblyDecision` 最小 envelope 放进第一部分 | 不采纳。它们是第二部分治理、Company KB、飞书权限系统的架构对象，第一部分不应先造壳。 |
| M-6 assembler 排序丢弃 raw score 推到第二部分 | 不采纳。`MemoryRetriever._apply_activation()` 已按 `activation_raw_score` 排序，但 `MemoryAssembler.assemble()` 又按 clamp 后的 `item.score` 重排，属于现役路径 bug，应进入第一部分。 |

### 1.3 明确不采纳为第一部分

| 项 | 原因 |
|---|---|
| Company KB runtime provider | 这是新功能，不是清债。先冻结设计，不实现。 |
| 飞书权限系统完整搭建 | 这是新治理系统，不是当前债务收尾。第一部分只核现有 Feishu channel/config 权限不要污染现有治理。 |
| 全量 Workbench V2 重构 | UI 当前债务要修，但不在第一部分做全新产品大改。先闭合现有半接控制链和可见状态。 |
| `save_to_kb` / `save_to_personal_kb` | 只有当当前 UI/tool 已承诺这个入口时才算第一部分债务；否则归第二部分或 Personal KB 下一施工包。 |

## 2. 第一部分：当前债务收尾

第一部分的目标是：现有系统不再有“写了不能读、后端有前端没有、文档说有代码没有、权限/RLS 报错不可解释、旧轨和新轨同时写”的问题。

### 2.1 验收门

第一部分结束时必须同时满足：

1. 所有 background runtime 在 tenant 解析失败时产生结构化 `blocked_precondition`，不会继续进入 ToolRuntimeService、RLS 写入或 LLM run。
2. `/steer` 从 registry、API、runtime、前端 slash command 到测试全链一致。
3. workspace rewind 的 `conversation`、`workspace`、`both` 三种模式在 UI 可选，workspace restore 有确认动作和结果展示。
4. T3 legacy flat files 不再作为新写入目标。保留 legacy 只能是 import/compat/quarantine。
5. 控制面 payload 不再包含本机绝对路径或不可移植 source refs。
6. capability mapping 新工具 drift 由测试/启动审计拦住，不靠人工记忆。
7. 当前文档和代码声明一致：没有“heartbeat 40 rounds”这类只写在文档里的契约。
8. Memory assembler 不再丢弃 retriever 的 `activation_raw_score` 排序。
9. 第一部分不引入 Company KB runtime，不引入 Feishu 权限新系统，也不预造 `PermissionDecision` / `ContextAssemblyDecision` envelope。

## 3. 施工包 A：Runtime Tenant Admission 与 RLS/治理互锁清理

### 3.1 问题

当前 `resolve_tenant_for_agent()` 是被大量调用的底层 helper。它可以返回 `None`，而很多 runtime 入口拿到 `None` 后继续进入 `tenant_scoped_session(None)`、预算、治理或 RuntimeTask 写入。这会让真实错误变成权限拒绝、RLS 空结果、审计缺失或后台重复重试。

### 3.2 落地方案

新增一个明确的 runtime 前置检查层，而不是让每个 daemon 自己猜。

建议文件：

```text
backend/app/services/runtime_tenant_admission.py
```

核心接口：

```python
@dataclass(frozen=True)
class RuntimeTenantAdmission:
    ok: bool
    tenant_id: uuid.UUID | None
    status: Literal["allowed", "blocked_precondition"]
    reason_code: str
    message: str
    agent_id: uuid.UUID | None
    source: str


async def admit_agent_runtime_tenant(
    agent_id: uuid.UUID | str | None,
    *,
    source: str,
) -> RuntimeTenantAdmission:
    ...
```

使用规则：

1. `ok=True` 才能进入 `tenant_scoped_session(tenant_id)`。
2. `ok=False` 必须返回 `blocked_precondition`，原因只能是前置条件，不允许伪装成 tool permission denied。
3. 已创建 RuntimeTask 的路径，要把 task 标记为 `blocked_precondition` 或 `needs_reconciliation`，并写入 `metadata_json.reason_code`。
4. 尚未创建 RuntimeTask 的路径，写结构化 daemon log 和 operator-visible liveness event。
5. 不允许 background mutating path 使用 `tenant_scoped_session(None)`。
6. 增加 session 层物理防线：`tenant_scoped_session` 保持默认兼容行为，但新增显式 `require_tenant=True` 或等价 guard。background mutating path 必须使用该 guard。admission 是策略入口，session guard 是最后防线。

### 3.3 需要接入的当前入口

优先改这些真实入口：

| 文件 | 函数/路径 | 动作 |
|---|---|---|
| `backend/app/services/trigger_daemon.py` | `_create_trigger_runtime_task()`、`_invoke_agent_for_triggers()`、resume loader | tenant admission 失败时不创建 mutating run |
| `backend/app/services/heartbeat.py` | `_execute_heartbeat()`、`_touch_last_heartbeat()` | tenant admission 失败时标记 heartbeat skip，不进 memory maintenance |
| `backend/app/services/runtime_task_service.py` | `create_runtime_task_record()` | parent_agent_id 存在但 tenant 解析失败时禁止写 NULL-tenant runtime task |
| `backend/app/services/subagent_wake_consumer.py` | `drain_subagent_completion_wakes()` | signal tenant 缺失时 structured skip |
| `backend/app/services/agent_tools.py` | deferred tools / channel file sender 相关 tenant resolve | 保留读路径 fail-closed，但要返回结构化原因 |
| `backend/app/tools/resolver.py` | `ToolRuntimeResolver.resolve()` | tenant None 不进入可变工具执行 |
| `backend/app/database.py` | `tenant_scoped_session()` | 增加 opt-in tenant-required guard，禁止 mutating runtime 无租户继续执行 |

### 3.4 测试

先写失败测试，再改实现。

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_runtime_tenant_admission.py \
  tests/services/test_trigger_daemon.py \
  tests/services/test_heartbeat.py \
  tests/services/test_runtime_task_service.py \
  tests/architecture/test_rls_tenant_write_contracts.py \
  -q
```

新增断言：

1. `resolve_tenant_for_agent()` 返回 None 时，trigger 不调用 `invoke_agent()`。
2. heartbeat 不创建 NULL-tenant ChatSession / RuntimeTask。
3. `create_runtime_task_record(parent_agent_id=<agent>)` 不能写 NULL tenant。
4. 用户可见事件区分 `blocked_precondition` 和 `permission_denied`。
5. mutating runtime 使用 `tenant_scoped_session(..., require_tenant=True)` 或等价 guard 时，tenant 缺失会抛出结构化 precondition error。

### 3.5 实装证据（2026-07-09）

状态：已完成并准备独立提交。

实装范围：

1. 新增 `backend/app/runtime/tenant_admission.py`：统一 `RuntimeTenantAdmission`、`RuntimeTenantPreconditionError` 和 blocked precondition metadata。
2. 新增 `backend/app/services/runtime_tenant_admission.py`：统一 `admit_agent_runtime_tenant()`，把 agent tenant 缺失归类为 `blocked_precondition`，不是 permission denied。
3. `backend/app/database.py::tenant_scoped_session()` 增加 opt-in `require_tenant=True` / `source` 物理防线；默认行为保持 fail-closed 兼容。
4. `backend/app/services/runtime_task_service.py::create_runtime_task_record()` 在 `parent_agent_id` 存在但 tenant 缺失时拒绝写 NULL-tenant `RuntimeTask`。
5. `backend/app/services/trigger_daemon.py::_invoke_agent_for_triggers()` 在 workflow/LLM/session 前先做 tenant admission；已存在 runtime task 时标记 skipped，并写 `precondition_status=blocked_precondition`。
6. `backend/app/services/heartbeat.py::_execute_heartbeat()` 在 tenant 缺失时不抢 lease、不进 session，直接标记 heartbeat runtime task 为 blocked precondition。
7. `backend/app/tools/resolver.py::ToolRuntimeResolver.resolve()` 不再容忍 missing tenant，不再继续打开 workspace 或构造 tool context。

红测证据：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_runtime_tenant_admission.py tests/test_database_tenant_scoped_session.py tests/services/test_runtime_task_service.py::test_create_runtime_task_record_blocks_parent_agent_without_tenant tests/services/test_trigger_daemon.py::test_invoke_trigger_blocks_when_agent_tenant_missing tests/services/test_heartbeat.py::test_execute_heartbeat_blocks_when_agent_tenant_missing -q
```

红测失败点：缺少 `app.services.runtime_tenant_admission` / `app.runtime.tenant_admission`，以及 trigger/heartbeat 会继续进入 session/lease。

绿测证据：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_runtime_tenant_admission.py tests/test_database_tenant_scoped_session.py tests/services/test_trigger_daemon.py tests/services/test_heartbeat.py tests/services/test_runtime_task_service.py tests/tools/test_resolver.py tests/architecture/test_rls_tenant_write_contracts.py -q
```

结果：

```text
87 passed, 3 warnings
```

静态检查：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/runtime/tenant_admission.py app/services/runtime_tenant_admission.py app/database.py app/services/runtime_task_service.py app/services/trigger_daemon.py app/services/heartbeat.py app/tools/resolver.py tests/services/test_runtime_tenant_admission.py tests/test_database_tenant_scoped_session.py tests/services/test_runtime_task_service.py tests/services/test_trigger_daemon.py tests/services/test_heartbeat.py tests/tools/test_resolver.py
```

结果：

```text
All checks passed!
```

## 4. 施工包 B：Slash Command 收口

### 4.1 `/steer` 修复

当前状态：

| 层 | 状态 |
|---|---|
| runtime | `execute_session_command()` 支持 `turn_steer` 和 `steer` |
| registry | 只注册 `turn_steer` |
| API dispatch | 先查 registry，所以 `/steer` 会 404 |

落地：

1. 在 `backend/app/services/command_registry.py::build_default_command_registry()` 注册 `steer` alias。
2. 确认 `canonical_name` 指向 `turn_steer` 或两个名字共享同一 runtime command。
3. 前端 slash parser 不再出现 runtime 支持但 schema 不暴露的命令。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/api/test_cc_codex_parity_api.py \
  tests/services/test_session_command_runtime.py \
  -q
```

前端：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/agent-detail/slashCommand.test.ts \
  src/pages/agent-detail/CommandPalette.test.tsx \
  src/pages/agent-detail/SlashCommandMenu.test.tsx
```

### 4.2 Slash command 文档/实现一致性

同包处理：

1. `/loop` self-pace 文案若实现已存在，删掉 `not yet available`。
2. `advanced_plan` / `verify_plan` 若是 internal-only，文档和 schema 明确来源限制。
3. `permissions/config` 如果云端只读是刻意设计，文档写清楚，不与 CC 本地 CLI 可写语义混用。

### 4.3 实装证据（2026-07-09）

状态：已完成并准备独立提交。

实装范围：

1. `backend/app/services/command_registry.py`：`turn_steer` 增加 user alias `steer`，user command index 暴露 `/steer`，canonical 仍为 `turn_steer`。
2. `frontend/src/pages/agent-detail/slashCommand.ts`：放开 `/steer ...` 解析，继续禁止 internal `/turn_steer`。
3. `backend/app/services/command_registry.py` 与 `backend/app/api/commands.py`：`/loop` 文案从 “not yet available” 改为已实装 self-paced `schedule_wakeup` 模式。
4. `backend/tests/api/test_cc_codex_parity_api.py`：补 `/steer` API index/schema 断言，并修正 session index fake DB 以匹配当前 ownership 校验。
5. `frontend/src/pages/agent-detail/slashCommand.test.ts`：补 `/steer` 解析断言。
6. `backend/tests/api/test_commands_loop.py`：补 `/loop` schema 不再包含 “not yet available” 的断言。

红测证据：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/api/test_cc_codex_parity_api.py::test_commands_api_lists_compact_index_and_schema -q

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run src/pages/agent-detail/slashCommand.test.ts
```

红测失败点：user command index 不含 `steer`；frontend `/steer ...` 被 internal-only filter 返回 null。

绿测证据：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/api/test_cc_codex_parity_api.py tests/api/test_commands_loop.py tests/services/test_session_command_runtime.py -q
```

结果：

```text
59 passed, 3 warnings
```

前端：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run src/pages/agent-detail/slashCommand.test.ts src/pages/agent-detail/CommandPalette.test.tsx src/pages/agent-detail/SlashCommandMenu.test.tsx
```

结果：

```text
Test Files  3 passed (3)
Tests  21 passed (21)
```

静态检查：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/services/command_registry.py app/api/commands.py tests/api/test_cc_codex_parity_api.py tests/api/test_commands_loop.py
```

结果：

```text
All checks passed!
```

## 5. 施工包 C：Workspace Rewind UI 闭环

### 5.1 问题

后端 `backend/app/services/session_command_runtime.py::execute_session_command()` 已支持：

```text
mode = conversation | workspace | both
confirm_workspace_restore = true
```

前端 `AgentDetail.tsx::handleSessionCommandUiAction()` 能识别 `install_workspace_snapshot` 和 `install_active_projection_with_workspace`，但 checkpoint selector 当前没有把 mode 选择和二次确认作为用户操作闭合。

### 5.2 落地

改动位置：

| 文件 | 改动 |
|---|---|
| `frontend/src/pages/agent-detail/AgentChatSection.tsx` | `SessionCommandControlPanel` 的 checkpoint selector 增加 mode segmented control：Conversation / Workspace / Both |
| `frontend/src/pages/AgentDetail.tsx` | 执行 checkpoint rewind 时带上 `mode` |
| `frontend/src/pages/AgentDetail.tsx` | 收到 `workspace_restore_requires_confirmation` 后打开确认卡，而不是只打开 permissions panel |
| `frontend/src/pages/agent-detail/sessionCommandResult.ts` 或同等 helper | normalize workspace restore result |
| `frontend/src/pages/agent-detail/AgentDetailSections.test.tsx` | 加 workspace/both 模式选择、确认重发、结果展示测试 |

后端保持：

| 文件 | 动作 |
|---|---|
| `backend/app/services/session_command_runtime.py` | 保留 fail-closed：无 snapshot 返回 `not_supported`；无确认返回 `workspace_restore_requires_confirmation` |
| `backend/tests/services/test_session_command_runtime.py` | 增加 both mode 的前后端契约 payload 断言 |

验收：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_session_workspace_snapshot.py \
  tests/services/test_session_command_runtime.py \
  -q

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/sessionCommandResult.test.ts
```

### 5.3 实装证据（2026-07-09）

状态：已完成并准备独立提交。

实装范围：

1. `backend/app/services/session_command_runtime.py`：workspace rewind 缺少确认时不再复用 `open_permissions_menu`，而是返回专用 `confirm_workspace_restore` UI action，携带 `checkpoint_event_id` 和 `requested_mode`。
2. `frontend/src/pages/agent-detail/AgentChatSection.tsx`：`SessionCommandControlPanel` 增加 Conversation / Workspace / Both mode 控件；checkpoint rewind 统一通过 `buildSessionRewindCommandArgs()` 传递显式 `mode`。
3. `frontend/src/pages/AgentDetail.tsx`：收到 `confirm_workspace_restore` 后打开 workspace restore 确认卡；确认按钮重新发送 `rewind`，并带 `confirm_workspace_restore=true`。
4. `frontend/src/index.css`：补齐 mode segmented control 和确认按钮样式，保持 Session TUI 的紧凑控制面。
5. 测试覆盖后端确认 action、前端 mode payload、确认卡渲染和 typed session result 格式化。

红测证据：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_session_command_runtime.py -q

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/sessionCommandResult.test.ts
```

红测失败点：

```text
backend: expected confirm_workspace_restore, got open_permissions_menu
frontend: missing session-rewind-mode-* controls; buildSessionRewindCommandArgs is not a function; missing session-workspace-restore-confirm-action
```

绿测证据：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_session_command_runtime.py -q
```

结果：

```text
27 passed, 3 warnings
```

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/agent-detail/sessionCommandResult.test.ts
```

结果：

```text
Test Files 2 passed (2)
Tests 100 passed (100)
```

构建与静态检查：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/services/session_command_runtime.py tests/services/test_session_command_runtime.py

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run build
```

结果：

```text
All checks passed!
frontend build: tsc && vite build completed successfully
```

## 6. 施工包 D：Memory/T3 双轨清理

### 6.1 问题

当前两套路径共存：

1. 新 two-plane：`memory/self/self.md`、`memory/profiles/*.md`、`memory/knowledge/*.md`、`memory/milestones/*.md`
2. 旧 flat T3：`memory/t3/episodes.md`、`memory/t3/user.md`、`memory/t3/worker.md`、`memory/t3/capabilities.md`

这会导致 prompt、dream、reference index、evolution view 对“真相路径”的理解不一致。

### 6.2 落地

| 文件 | 改动 |
|---|---|
| `backend/app/services/auto_dream.py` | `_T3_FILES` 改为 two-plane source；旧路径只能作为 legacy import |
| `backend/app/memory/reference_index.py` | legacy flat 文件只作为 migration source，不作为 active index source |
| `backend/app/memory/t3_platform_gate.py` | `LEGACY_T3_FILES` 明确只允许 quarantine/import，不允许 accepted write |
| `backend/app/services/agent_evolution_view.py` | `_empty_view()` 的路径描述改为 two-plane |
| `docs/memory-vault-path-contract-2026-06-23.md` | 修正文档，使旧 flat T3 标注为 legacy |

### 6.3 迁移策略

1. 先跑 dry-run，报告每个 agent 的 legacy flat T3 是否存在。
2. 若存在，移动到 `memory/.archive/legacy_t3/` 或通过既有迁移工具转换到 two-plane。
3. 不直接删除原始文件，保留 reversible quarantine。
4. 新写入路径只能是 two-plane。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/memory/test_t3_platform_gate.py \
  tests/runtime/test_t0_to_t2_session_close.py \
  tests/services/test_agent_evolution_view.py \
  tests/services/test_heartbeat.py \
  -q
```

### 6.4 实装证据（2026-07-09）

状态：已完成并准备独立提交。

实装范围：

1. `backend/app/memory/reference_index.py`：移除 active scan `memory/t3/{episodes,user,worker,capabilities}.md` 的 `_t3_reference_rows` 路径；reference index 只从 two-plane profile/knowledge/milestone、explicit overlay、T2/T2.5 packages 派生。
2. `backend/app/services/auto_dream.py`：`_T3_FILES` 改为 two-plane manifest：`memory/self/self.md`、`memory/profiles/*.md`、`memory/knowledge/<slug>.md`、`memory/milestones/<slug>.md`。
3. `backend/app/services/agent_evolution_view.py`：path contract 与 `t3_targets` 切到 two-plane；legacy flat T3 只进入 `legacy_audit.detected_legacy_files`。
4. `docs/memory-vault-path-contract-2026-06-23.md`：把 legacy flat T3 从 accepted ownership 表移到 read-only migration/quarantine 语义。
5. `backend/app/memory/t3_platform_gate.py::LEGACY_T3_FILES` 保留为唯一 app 层 legacy flat T3 常量，用于 migration/quarantine 命名，不作为 accepted write target。

红测证据：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_t2_retention.py::test_legacy_flat_t3_blocks_do_not_count_as_active_referrers \
  tests/services/test_dream_phase6.py::TestT3ReadWriteBoundary::test_dream_t3_file_manifest_uses_two_plane_paths \
  tests/services/test_agent_evolution_view_v2.py::test_agent_evolution_view_v2_uses_unified_memory_and_skill_paths \
  tests/api/test_agent_evolution_api.py::test_get_agent_evolution_returns_structured_view -q
```

红测失败点：

```text
legacy flat T3 ref still counted as active referrer
auto_dream._T3_FILES still listed t3/episodes.md, t3/user.md, t3/worker.md, t3/capabilities.md
agent_evolution_view path_contract still exposed t3_capabilities=memory/t3/capabilities.md
```

绿测证据：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_t2_retention.py tests/memory/test_c8_derived_tables.py tests/memory/test_source_ref_system.py tests/memory/test_retrieval_pipeline.py tests/memory/test_two_plane_migration.py tests/services/test_dream_phase6.py tests/services/test_agent_evolution_view_v2.py tests/api/test_agent_evolution_api.py tests/scripts/test_rebuild_reference_index.py -q
```

结果：

```text
78 passed
```

Prompt/path 合同回归：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_dream_template.py tests/runtime/test_t3_prompt_contracts.py tests/runtime/test_memory_section.py tests/memory/test_t3_file_boundary.py tests/memory/test_t3_gate_four_planes.py -q
```

结果：

```text
41 passed, 4 warnings
```

静态检查：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/memory/reference_index.py app/services/auto_dream.py app/services/agent_evolution_view.py tests/memory/test_t2_retention.py tests/services/test_dream_phase6.py tests/services/test_agent_evolution_view_v2.py tests/api/test_agent_evolution_api.py
```

结果：

```text
All checks passed!
```

## 7. 施工包 E：控制面脆弱点与孤儿清理

### 7.1 `codex_optimization_ledger` 本机路径

改动：

| 文件 | 改动 |
|---|---|
| `backend/app/runtime/codex_optimization_ledger.py` | 移除 `/Users/rocky243/...` 绝对路径；改成相对 source family 或文档 ref |
| `backend/tests/runtime/test_runtime_context_composition.py` | 断言 ledger 不含本机绝对路径 |

### 7.2 heartbeat round budget 文档/实现不一致

二选一，不允许继续两边不一致：

1. 如果 heartbeat 应该 40 rounds：在 heartbeat 调用 `invoke_agent()` 或直接 runtime config 里显式覆盖。
2. 如果 heartbeat 已经不走普通 tool loop：删除文档里 “heartbeat override max_tool_rounds=40” 的承诺。

### 7.3 `promotion_router.py`

处理方式：

1. 用 graph/import 搜索确认 runtime 没有 consumer。
2. 如果只剩测试或类型引用，进入退役路径。
3. 如果实际应该接入 Skill/Memory promotion，则补真实 entry point 和测试。
4. 不允许继续保留“看起来完整但无人调用”的子系统。

### 7.4 activation feedback sidecar

不直接删除。改为闭环处理：

1. 若 `activation_feedback.jsonl` 是保留的 sidecar，则补 read model / retention / UI 或调试出口。
2. 若不是保留面，则迁移现有 writer 到 T0/event summary，然后退役 sidecar。
3. `decay_signal` 必须有 consumer 或从 payload 中删除。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/runtime/test_runtime_context_composition.py \
  tests/services/test_session_feedback.py \
  tests/tools/test_audit.py \
  -q
```

## 8. 施工包 F：现有 Personal KB 与 Dynamic 注入清债

第一部分只清现有 Personal KB 和 Dynamic 注入的债，不开发 Company KB。

### 8.1 必须清的现有债

| 债务 | 动作 |
|---|---|
| Personal KB grant 和 runtime search 已存在 | 保证 owner/grant/agent 三种读路径测试齐全 |
| `PersonalKnowledgeCandidateProvider` 只处理 personal scope | 保持现状，并明确 Company scope 是第二部分 |
| KB hint / runtime activation 可见性 | `Context usage` 或 Workbench 中至少能看出 Personal KB 是否参与候选 |
| 上传/URL/import job 状态 | 失败原因要能在 UI/API 中解释，不静默失败 |

### 8.2 不在第一部分做

| 不做 | 原因 |
|---|---|
| `search_company_kb` | Company KB 是第二部分新功能 |
| Personal -> Company proposal | 依赖 Company KB review plane |
| Agent 主动 `save_to_kb` | 除非当前产品入口已承诺，否则不是清债 |
| 全新 QKV scoring 方程 | 属于新设计落地，不是当前债务收尾 |

### 8.3 M-6：Memory assembler 丢弃 raw score 排序

这是第一部分债务，不是第二部分新 scoring 方程。

当前路径：

```text
MemoryRetriever._apply_activation()
  -> writes metadata.activation_raw_score
  -> returns sorted(... activation_raw_score ...)

MemoryAssembler.assemble()
  -> sorted(items, key=lambda i: i.score, reverse=True)
  -> i.score 是 clamp 后的显示分数，多个高相关项会饱和到 1.0
  -> retriever 的 raw score 排序被覆盖
```

修复原则：

1. 不改 ActivationScorer 方程。
2. 不引入新的 Dynamic activation 架构。
3. 只让 assembler 的排序键尊重 `metadata.activation_raw_score`，fallback 到 `item.score`。
4. 增加回归测试，构造两个 `score=1.0` 但 `activation_raw_score` 不同的 item，断言 assembler 输出按 raw score 排序。

改动位置：

| 文件 | 改动 |
|---|---|
| `backend/app/memory/assembler.py` | `sorted_items` key 改为 `_memory_item_sort_score(item)` |
| `backend/tests/memory/test_assembler.py` 或现有 assembler 测试文件 | 新增 raw score tie-break regression |

建议测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_assembler.py -q
```

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_personal_knowledge_service.py \
  tests/tools/test_personal_knowledge_tool.py \
  tests/runtime/test_personal_knowledge_activation.py \
  tests/runtime/test_personal_knowledge_provider.py \
  tests/integration/test_personal_knowledge_cross_owner.py \
  -q
```

## 9. 第一部分总验证

第一部分施工完成后至少跑：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_runtime_tenant_admission.py \
  tests/services/test_trigger_daemon.py \
  tests/services/test_heartbeat.py \
  tests/services/test_runtime_task_service.py \
  tests/services/test_session_command_runtime.py \
  tests/services/test_session_workspace_snapshot.py \
  tests/memory/test_assembler.py \
  tests/runtime/test_runtime_context_composition.py \
  tests/runtime/test_personal_knowledge_activation.py \
  tests/tools/test_audit.py \
  tests/architecture/test_rls_tenant_write_contracts.py \
  -q

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/sessionCommandResult.test.ts \
  src/pages/agent-detail/slashCommand.test.ts \
  src/pages/agent-detail/CommandPalette.test.tsx \
  src/pages/session-workbench/timelineModel.test.ts

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run build
```

最终再跑完整后端：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q
```

## 10. 第二部分：冻结设计，但不立刻实现

第二部分只有在第一部分验收通过后才开工。

### 10.0 第二部分上游设计索引

第二部分开工前必须先读并锁定这两份历史设计，避免 Company KB、飞书权限和统一治理再次分叉成平行系统：

| 文档 | 负责问题 | 第二部分约束 |
|---|---|---|
| `docs/company-knowledge-base-spec-2026-07-07.md` | Company KB 总体规格：组织真相层、Knowledge Core 复用、ontology primitives、proposal/review/publish/retire、ACL/source refs/audit/rollback、飞书/企业文档权限兼容契约 | Company KB 不能做成 Personal KB 扩容版，也不能做成旧 `/enterprise/knowledge-base/*` 文件树；必须走 governed authority plane |
| `docs/agent-permission-governance-spec-2026-07-07.md` | Agent 权限治理与知识授权：User/Owner、Agent delegation、resource ACL、sensitivity、session/task/A2A 目的、公司策略的交集；飞书式 Creator/Owner/Collaborator/分享/export/proposal 权限映射 | 不新增一套“飞书权限表”作为平行真相；Feishu connector 只能做 Permission Mapping Adapter，Runtime 最终只认 Hive 统一 `PermissionDecision` |

这两份文档是第二部分的上游设计面，不改变第一部分清债边界：第一部分仍不创建 `PermissionDecision` / `ContextAssemblyDecision` envelope，不实现 Company KB runtime，也不接飞书权限同步。

### 10.1 公司知识库

上游文档：`docs/company-knowledge-base-spec-2026-07-07.md`。

第二部分目标：

```text
Company KB = tenant/company authority plane
```

它不是 Personal KB 的简单扩展，也不是旧 `/enterprise/knowledge-base/*` 文件树。

必须包含：

1. Company knowledge object / link / assertion / proposal / version / retirement。
2. Proposal -> review -> publish -> retire -> rollback。
3. ACL：org、department、team、role、agent、user。
4. `search_company_kb` 只读工具。
5. `propose_company_kb_update` 候选工具。
6. Dynamic injection 只能注入 authorized company knowledge。
7. Workbench 展示 source refs、review status、policy refs。

第一部分可以做的唯一准备：`RuntimeTenantAdmission` 的结构化结果保留未来可被 `PermissionDecision` / `ContextAssemblyDecision` 吸收的字段语义；不设计、不创建、也不固定这两个 envelope。

### 10.2 飞书权限系统

上游文档：`docs/agent-permission-governance-spec-2026-07-07.md`；其中 `docs/company-knowledge-base-spec-2026-07-07.md` 的 3.8 节定义了 Company KB 与飞书/企业文档权限映射的兼容契约。

当前已存在的 Feishu 事实面：

| 事实面 | 当前状态 |
|---|---|
| per-agent bot config | `channel_configs` 和 `_get_feishu_app_credentials(agent_id)` |
| tenant-level channel config | `TenantChannelConfig` 和 `/tenant-channels/{channel_type}` |
| org sync | `feishu_org_sync` tenant setting |
| Feishu/Lark region | `platform_region=feishu_cn|lark_global` |
| card approval | `feishu_card_callback()` |
| 前端权限 JSON | `ChannelConfig.tsx` 有 basic/full permission preset |

第二部分要做的不是再堆一个 Feishu 面板，而是把飞书身份和权限接入统一企业治理：

1. Feishu user/open_id/user_id/union_id 与 Hive User/OrgMember 的稳定映射。
2. Department/role/group/chat/document 权限进入 PermissionDecision。
3. Bot scopes 和 app scopes 成为可审计能力，不只是 JSON copy。
4. Feishu 消息动作：read/send/file/card approval 都通过同一治理解释。
5. Feishu/Lark region split 继续保留一个 `feishu` channel type，不新增 channel enum。
6. 企业知识库可从 Feishu doc/wiki/import proposal 进入，但必须走 Company KB review。

### 10.3 两个 envelope 的第二部分落点

`PermissionDecision` 和 `ContextAssemblyDecision` 都属于第二部分，但它们不是第二部分末尾的补丁，而是第二部分开工后的第一个基础施工包。

第二部分内部顺序必须是：

1. **Part 2-A：PermissionDecision。**
   先定义统一权限判定返回值和最小服务入口。它吸收第一部分的 `RuntimeTenantAdmission` 结果，但不被 `RuntimeTenantAdmission` 反向决定。Company KB、Feishu adapter、Personal -> Company proposal、A2A 读取、tool 外发、Dynamic injection 都只能消费这个统一判定结果。
2. **Part 2-B：ContextAssemblyDecision。**
   在权限判定可用后，再定义上下文装配返回值：候选来源、ACL 过滤结果、sensitivity stripping、ranking/budget、source refs、why included/why excluded、prompt manifest。它不能绕过 `PermissionDecision`。
3. **Part 2-C：Company KB runtime。**
   复用 `docs/company-knowledge-base-spec-2026-07-07.md`，实现 company knowledge object/proposal/review/publish/retire/search/inject。所有 read/inject/export/propose 都必须先拿到 `PermissionDecision`，进入 prompt 前必须生成 `ContextAssemblyDecision`。
4. **Part 2-D：Feishu 权限映射。**
   Feishu/Lark connector 只做 Permission Mapping Adapter，把 user/department/group/chat/document/app/bot 权限映射到 Hive grants/bindings/source_acl_snapshot/org policy；runtime 不直接查询飞书权限。
5. **Part 2-E：Workbench 与审计闭环。**
   UI 展示 permission trace、context manifest、source refs、review status、policy refs、denial reason 和可申请权限入口。

因此：

```text
第一部分完成
  -> Part 2-A PermissionDecision
  -> Part 2-B ContextAssemblyDecision
  -> Part 2-C Company KB runtime
  -> Part 2-D Feishu Permission Mapping Adapter
  -> Part 2-E Workbench/audit
```

禁止顺序：

```text
Company KB / Feishu adapter 先各自实现权限判断
  -> 后补 PermissionDecision
```

这种顺序会重新制造平行权限和上下文注入断点。

## 11. 第一部分与第二部分的接口边界

第一部分允许创建的未来兼容接口只有一个：

| 接口 | 第一部分用途 | 第二部分复用 |
|---|---|---|
| `RuntimeTenantAdmission` | 解决 tenant=None/RLS/daemon 互锁 | Company KB/Feishu background jobs 共用 |

`PermissionDecision` 和 `ContextAssemblyDecision` 不在第一部分创建。第一部分只要求 `RuntimeTenantAdmission` 的结构化结果未来能被它们吸收，不提前固定 shape。

第一部分不允许做：

1. Company KB DB schema。
2. Company KB tool。
3. Feishu department permission model。
4. Feishu document/wiki ingest。
5. Personal -> Company promotion。
6. 全新 Dynamic activation scoring。
7. `PermissionDecision` / `ContextAssemblyDecision` envelope。

## 12. 给 CC 的 review 问题

CC 反审这份方案时，建议只问这些问题：

1. 第一部分里还有没有“已经存在但未闭合”的债务遗漏？
2. 哪些条目其实是第二部分新功能，不该混进第一部分？
3. `RuntimeTenantAdmission` 是否足够覆盖 C1，还是必须更靠近 `tenant_scoped_session()` 做硬防线？
4. workspace rewind UI 是补齐，还是应该砍掉 workspace mode 并退役后端路径？
5. T3 legacy flat 文件是保留 compatibility，还是必须完成一次 fleet migration？
6. activation feedback sidecar 是接 read model，还是退役？
7. STRICT capability mapping 是否需要 CI hard gate，还是 startup audit 足够？

当前吸收 CC 反馈后的回答：

1. 已补 M-6 assembler raw-score 排序丢失。
2. 已把 `PermissionDecision` / `ContextAssemblyDecision` 从第一部分移出。
3. 采用双保险：RuntimeTenantAdmission 作为策略入口，`tenant_scoped_session` opt-in tenant-required guard 作为物理防线。
4. workspace rewind 倾向补齐，不砍；若产品决定砍，必须连后端路径和快照写入一起退役。
5. T3 legacy flat 需要一次 fleet migration，旧路径降级为 quarantine/import compatibility。
6. activation sidecar 先证明有无 consumer；无 consumer 则退役 writer 到 T0/event summary，不为没人读的 JSONL 补产品面。
7. STRICT capability mapping 应加 CI hard gate，startup audit 只能作为运行时补充。

## 13. 最终判定

CC 报告最有价值的是把当前债务打到了具体文件和具体断点上。但它的问题是把“当前债务收尾”和“后续新系统设计”混在了一起，并且对 Company KB、飞书权限系统、Workbench V2 的新开发边界没有拆清。

本方案的核心修正是：

```text
先清债，后新建。

第一部分只修当前系统已经存在的断点：
tenant/RLS admission、slash command、workspace rewind、T3 双轨、控制面脆弱点、assembler raw-score 排序、Personal KB/Dynamic 已有闭环。

第二部分再做新系统：
Company KB、飞书权限系统、企业治理扩展、Personal -> Company promotion。
```

只有第一部分全部验收通过，第二部分才有干净地基。
