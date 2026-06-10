# RLS 迁移 — 阶段 0 穷举发现（权威 evidence + 阶段 1-3 执行计划）

> 状态：**阶段 0 完成**。205 处 bare session 全量静态穷举（5 份 inventory：`.ultra/rls-stage0/inventory-{A..E}.md`）+ 关键断言亲验 + 动态验证机制确认。
> 上游：方案框架 `docs/rls-enforcement-migration-plan.md`（4 阶段 + D1-D4）。本文档是基于穷举证据对该方案的**精确化与校正**。

## 0. 一句话结论

地基（GUC 注入、`tenant_scoped_session`、`enter_rls_bypass`、Testcontainers 非 owner 角色测试设施）**全部就位**；这场仗是「把 205 处 bare session 接到 GUC 上 + 解一个 chicken-and-egg + 翻一个角色开关」。方案原以为「真正要改的只是少数」——**穷举推翻了这个判断：几乎全部 bare session 都 fail-closed**。

## 1. 数字校正

| 来源 | 数字 | 说明 |
|------|------|------|
| 审计脚本注释 | 37 | 旧估计 |
| 方案文档 | 184 | 当时上界 |
| **本轮 grep 实测** | **206**（排除 database.py 定义）| 穷举对象 |
| **穷举分类后** | **205 处分析**（A41+B43+C40+D33+E48）| 1 处边界排除 |

**fail-closed 实测分布（远超「少数」）**：A 41/41 · B 37/43 · C ~33/40 · D 28/33 · E 45/48 ≈ **~184/205 会 fail-closed**。只有 ~21 处纯全局表（`system_settings`、`capability_policies`、`audit_logs`、`tenants` 等无 policy 表）能逃。

## 2. accessor 三层分类（驱动阶段划分）

穷举的核心产出不是「206→少数」，而是**按「何时崩」分三层**：

### 层 1 — Day-one breaker（翻角色立刻崩，~85 处）
查**已有 policy 的 9 ENABLE 表**（agents/users/llm_models/skills/tools/plaza_posts/org_departments/org_members/config_revisions）或 **7 FORCE 表**（workflow_*/coordination_*）。这些是**阶段 1 的工作量**，不依赖加列。代表：
- 🔴 **runtime bootstrap**：`invoker.py:208/256/921`、`web_chat_runtime.py:605`（查 agents/users/llm_models 拿 tenant）— **make-or-break，翻角色后无 agent 能执行**（亲验 invoker.py:208 → `agent=None` → `tenant_resolution_error` → kernel early-exit）。
- `resource_discovery.py`（7 处查 tools）、`mcp_server_service.py:767`、`memory_service.py:183/689/736`、`agent_manager.py:385`、`org_sync_service.py:171`、Group E ~35 处 tool/skill 解析、Group A ~15 处 daemon 查 Agent/LLMModel。

### 层 2 — Stage-2-gated（加列后才崩，~115 处）
查 **agent-scoped 表（有 agent_id 无 tenant_id，今天无 policy）**：`chat_sessions`/`chat_messages`/`runtime_tasks`/`triggers`/`channel_configs`/`notifications`/`pending_reply_contexts`/`agent_tools`/`agent_capability_installs`/`agent_activity_logs`。**阶段 2 给这些加 tenant_id + policy 后**，其 accessor 才需迁移。这是迁移的**主要爆炸半径**，但被 gate 在阶段 2。

### 层 3 — 合法跨租户（~21 处）
seeders（启动播种全局工具/模板）、scripts（运维）、daemon 全租户扫描（trigger tick、heartbeat sweep、evolution cleanup）。→ `enter_rls_bypass(reason=)` 审计化，**不是** fail-closed 待修，而是显式化今天的 owner-bypass。

## 3. 设计决议（穷举新增，待落地）

### DD-A — 共享 tenant-resolution helper（chicken-and-egg 解法，头号设计项）
**问题**（4 组共识）：~40 处只有 `agent_id`，要查 `agents` 表才能拿 `tenant_id` 去 scope，但查 agents 本身翻角色后 fail-closed → 死锁。
**解法**：建一个 sanctioned helper，用 `enter_rls_bypass` 包一个**窄的单行 agent→tenant 查询**（按主键，只返回该 agent 一行 tenant_id，无跨租户暴露面）：
```python
async def resolve_tenant_for_agent(agent_id) -> uuid.UUID | None:
    async with async_session() as db:
        async with enter_rls_bypass(db, reason=f"tenant resolution for agent {agent_id}") as bdb:
            return (await bdb.execute(select(Agent.tenant_id).where(Agent.id == agent_id))).scalar_one_or_none()
```
然后 caller：`tid = await resolve_tenant_for_agent(aid); async with tenant_scoped_session(tid) as db: ...`。
**例外**：`invoker._resolve_runtime_config` / governance `_request_approval` 等**本来就要读整个 agent 行**的，直接 `enter_rls_bypass` 包现有查询，不走 helper（省一次往返）。

### DD-B — 阶段 1/2 按层 1/层 2 重新划分（而非方案的「先迁所有 request-scoped」）
- **阶段 1** = 层 1（day-one breaker，~85）+ DD-A helper + 独立安全 bug。**不依赖加列，独立可部署**。
- **阶段 2** = 加 tenant_id 列到 agent-scoped 表 + policy + 回填 + 层 2 迁移（~115）。
- **阶段 3** = 翻角色（层 1+2 全绿后）。
理由：层 2 占多数但被 gate 在加列，硬塞进阶段 1 会卡死。

### DD-C — 阶段 2 scope 扩展（穷举新发现的无 policy 租户表）
方案的阶段 2 表清单要补：
- **有 tenant_id 但漏 policy**：`agent_plan_requests`、`agent_plan_recommendations`（Plan-Mode 数据当前跨租户可读，加 policy 即可，无需加列）。
- **agent-scoped 待加列**：`agent_tools`、`agent_capability_installs`、`agent_activity_logs`、`pending_reply_contexts`、`runtime_tasks`、`gateway_messages`。
- **待 owner 裁决**：`audit_logs`/`audit.py AuditLog`（agent_id nullable 无 tenant_id）是否故意无 policy（否则 webhook 限流审计写 fail-closed）。

## 4. 独立安全 bug（亲验后分级 — 修正 subagent 过度警报）

| Bug | 位置 | 亲验定性 | 处置 |
|-----|------|----------|------|
| 🔴 **配额 fail-OPEN** | `quota_guard.py:35` `if not user: return` | **真 bug**：翻角色后 user 不可见 → 配额静默不强制 | 阶段 1 修：threading `user.tenant_id` + `tenant_scoped_session` |
| 🟠 治理空结果 | `governance_resolver.py:62` `_check_capability` | 有 `tenant_id` 参数 → 迁 `tenant_scoped_session(tenant_id)` 即修；`_request_approval`(:83)/`_resolve_security_zone`(:53) **已 fail-safe**（空→deny/restricted） | 阶段 1 迁移即修，加 deny-on-empty 断言 |
| 🟡 凭证读脆弱 | `email.py:21`、`image_upload.py:39`（`Tool.name==` 无 tenant filter） | **降级**：`tools (name,tenant_id)` 唯一+全局工具单行，当前不泄漏；多租户同名是 `MultipleResultsFound` 崩溃非读别租户；翻角色后只见全局行反而安全 | 加显式 `Tool.tenant_id.is_(None)` 表意图，非 P0 |
| ⚠️ feishu 目录软失败 | `feishu_users.py:115/130/164` | subagent 报 try/except 让 `_tenant_id=None` → OrgMember/User 跨租户查 | **待亲验**，修 IM 渠道批时核实 |

## 5. 测试策略（机制已锚定，照模板复制）

`tests/integration/test_rls_tenant_isolation.py` 已用 Testcontainers 证明核心机制（非 owner 角色 + 空 GUC = fail-closed；设 GUC = 只见本租户；owner = 绕过）。基础设施 `tests/integration/conftest.py`：`app_user_engine`/`app_user_sessionmaker`（RLS 真生效的唯一连接）、`owner_engine`。
**阶段 1 每处迁移**照 `test_non_owner_with_tenant_guc_sees_only_own_rows`：用 `app_user_sessionmaker` 调迁移后 accessor，断言①设了 GUC→只见本租户②空 GUC（旧 bare 行为）→fail-closed。Functional-core 判定逻辑（如「空结果=deny」）单测无 mock。

## 6. 阶段 1 实现顺序（TDD，每步独立部署）

1. **DD-A helper** `resolve_tenant_for_agent` + Testcontainers 测试（bypass 窄读正确）。
2. **runtime bootstrap**（make-or-break）：`invoker.py:208/256/921` + `web_chat_runtime.py:605` → `enter_rls_bypass` 包 agent 读 / threading request.tenant_id。测试：翻角色后 agent 仍能 bootstrap。
3. **配额 fail-open 修复**：`quota_guard.py` threading tenant。
4. **治理迁移**：`governance_resolver._check_capability` → `tenant_scoped_session(tenant_id)` + deny-on-empty 断言。
5. **层 1 批量迁移**（~80 处剩余 day-one breaker）：按 inventory 的 `migration_target`，subagent 并行分文件迁，每处钉测试。`tools/resolver.py:31`（工具执行租户解析总枢纽）优先。
6. 全量回归 + `audit_rls_coverage` + push + Railway 部署（阶段 1 不翻角色，行为不变，安全增量）。

**阶段 3 翻角色 = owner 确认门**（不可逆，影子穷举 100% 绿后）。回滚 = `DATABASE_URL` 切回 owner 角色（秒级）。

## 7. 阶段 1 实施笔记（地基已落地）

**已落地地基（Testcontainers app_user 角色测试证明翻角色后 bootstrap 工作）：**
- `services/tenant_resolver.resolve_tenant_for_agent`（DD-A helper）+ `test_tenant_resolver.py`
- runtime bootstrap：`invoker._resolve_runtime_config` / `_resolve_current_user_name` / `_resolve_agent_smart_model_routing` + `web_chat_runtime._load_runtime_context`（make-or-break）+ `test_runtime_bootstrap_rls.py`
- `quota_guard.check_user_token_quota`（fail-OPEN 修复）
- `governance_resolver`：`_check_capability`→`tenant_scoped_session`；`_resolve_security_zone`/`_request_approval`/`_resolve_mcp_tool_mode`→`enter_rls_bypass`

**迁移三模式（批量迁移按 inventory 的 migration_target 套用）：**
1. tenant_id 在 scope（current_user/param/loaded-entity）→ `async with tenant_scoped_session(tid) as db:`
2. 只有 agent_id → `tid = await resolve_tenant_for_agent(aid)` 再 `tenant_scoped_session(tid)`；或读整行 agent 的 bootstrap → `async with async_session() as db, enter_rls_bypass(db, reason=...):`
3. 全租户扫描（daemon sweep / seeder / script）→ `enter_rls_bypass(db, reason=...)`；daemon 遍历 agents → 循环内 `set_current_tenant(agent.tenant_id)` + `tenant_scoped_session()`

**mock-session 单测适配（批量迁移必处理，否则全量回归红）：**
- `enter_rls_bypass`/`tenant_scoped_session` 会在 session 上多发 `SET LOCAL app.current_tenant_id`。
- fake session 的 `execute` 若用 result 序列（`pop(0)`）→ 加 `if "app.current_tenant_id" in str(stmt): return <empty>`，GUC 语句不消耗业务 result。
- 测试 mock 了 `async_session` 但代码改用 `tenant_scoped_session` → 补 `monkeypatch.setattr(".../tenant_scoped_session", lambda *a, **k: fake_session)`。
- 断言「第一条 SQL」→ 过滤掉含 `app.current_tenant_id` 的 GUC 语句再取业务查询。

**已知项（非阻塞，可观测性 backlog）：** `enter_rls_bypass` 的 finally 在 yield 块事务已 aborted 时，`SET LOCAL` 恢复会抛 `InFailedSQLTransactionError` 掩盖原始异常（调试 feature_flags 缺表时被它误导过）。建议 finally 的 SET LOCAL 包 try/except 记日志不掩盖。
