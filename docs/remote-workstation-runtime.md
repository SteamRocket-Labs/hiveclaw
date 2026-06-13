# Remote Workstation Runtime 设计探索

> **状态：探索性设计稿（brainstorm），非当前主线。2026-06-13。**
>
> 本文讨论"如果将来要给 agent 提供有状态远程工作站能力（持久浏览器、登录态 profile、人工接管）该怎么做"。它不是 canonical、不构成现在动工的依据。三个必须先承认的前提：
>
> 1. **当前 Hive agent 没有任何浏览器能力**——`tools/` 下零 browser 工具。本文不是改进现有能力，而是从零提出一个新子系统。
> 2. **尚无被验证的触发场景**——没有一个具体的、用户或 agent 当前被卡住的需求来驱动它。需求坐实之前，这是探索，不是设计定稿。
> 3. **排在 eval CI 之后**——当前工程主线是第二轮 SOTA 对标的外部行为 eval CI（Goal 1 自进化内核收尾）。按 `CLAUDE.md` 的 build order，本 runtime 属于 Goal-2 周边能力，必须排在 eval CI 之后。
>
> 价值定位：本文记录"如果要做，安全原则和最小切片长什么样"，留住已经想清楚的安全直觉，避免将来重新发明。动工前提见 §15。

## 1. 问题陈述

真问题：`code_execution` 是无状态短命令执行，结构上无法承载有状态的浏览器工作站（持久登录态、长会话、人工接管）。如果将来出现需要"持久浏览器 + 登录态"的场景，**直接把 `code_execution` 改成长生命周期会破坏它的无状态安全语义**——这是不能做的。所以那个能力需要另起一层。

强调：以上是"如果"。目前没有这样的场景被验证。下面全部是 conditional 设计，前提是需求先被坐实（§15）。

## 2. 现有事实（已核查）

当前生产接入的 `vercel_sandbox` 是 code execution provider，不是远程工作站 runtime：

- 入口：`backend/app/services/code_execution/service.py`
- provider：`backend/app/services/code_execution/vercel_provider.py`
- 行为：每次 `execute_agent_command()` 创建一个 Vercel Sandbox，上传 workspace tar，执行一个命令，回写 workspace tar，然后 `sandbox.stop()`。
- 默认网络：`HIVE_CODE_EXEC_NETWORK_POLICY=deny-all`。
- secrets 边界：`subprocess_env.py` 只允许白名单环境变量；backend host secrets 不上传 sandbox。
- 测试：`backend/tests/services/test_vercel_code_execution.py` 锁定了 fail-closed、workspace sync、network policy threaded-through、执行后 stop 的 contract。
- **浏览器能力：无。** agent 工具面没有任何 browser/CDP/noVNC 工具。

这个模型适合短命令和无状态代码执行，不适合持久浏览器 session，也不能直接改成长生命周期。

## 3. 如果要做：安全原则（核心，不可打折）

这一节是本文真正的价值所在——无论将来用什么 provider，这些边界都成立：

1. `code_execution` 保持无状态短命令语义，不被改造成长生命周期。
2. 浏览器登录态**不留在普通 agent workspace**，也不依赖 sandbox 永久保存。
3. browser profile 是**敏感凭证级 artifact**，必须加密存储、带 ACL、TTL、审计和吊销。
4. sandbox / worker 是执行环境，**Hive 是控制面和真相源**——provider 自身不是信任源。
5. 所有 browser/code/workstation tools 必须走 `ToolRuntimeService`，不绕过 governance 和 action preflight。
6. 默认网络仍是 `deny-all`，每个任务按目标域名显式 allowlist。
7. 人工登录是显式 checkpoint，不允许 agent 直接读取原始密码、OTP seed 或平台级 secrets。
8. 恢复策略要诚实：能恢复就通过 `RuntimeTask` 和 provider id 恢复，不能恢复就标 failed 并保留 profile/workspace checkpoint。

## 4. 如果要做：最小完整切片（取代"一次铺开整个 runtime"）

**不要一次定义并交付整个操作系统。** 第一个切口应该是一个**最小但完整**的垂直闭环：

- **单 provider**：Vercel Sandbox workstation。不引入第二个未验证的 provider。
- **单场景**：一次"需登录的网页任务"（hydrate profile → 操作 → checkpoint profile）。
- **完整闭环**（这部分不打折）：profile 加密存取 + lease + revoke + `deny-all` 网络 + 人工登录 checkpoint + `RuntimeTask` 恢复 + 全部 fail-closed 测试。

> **澄清纪律边界**："禁 MVP / 一次改完"指的是**做的那个切片要完整**（测试、边界、错误路径、安全做满），**不是**"一次定义整个系统"。后者是范围失控，不是工程纪律——`CLAUDE.md` 自己也写明 complexity ≥ 7 要拆、单任务 ≤ 20 文件。把"禁拆分"当成"禁 MVP"是对纪律的曲解。

明确**不在第一切片**的后续增量：第二个长期 worker provider、noVNC 长时观察、多场景工具面扩展、跨 provider 的 profile 迁移。

## 5. 如果要做：目标架构（草图）

```text
Agent loop
  -> ToolRuntimeService
  -> browser / workstation tools
  -> RemoteWorkstationService
  -> Provider:
       - vercel_sandbox_workstation   （第一切片）
       - dedicated_browser_worker     （后续增量，provider 选型待定 §8）
  -> encrypted BrowserProfileStore
  -> agent workspace artifact sync
  -> RuntimeTask + invocation spans + audit
```

待决：是否真需要一个跟 `code_execution` **平级的新 runtime**，还是 `code_execution` 的 provider 抽象加一个 stateful session 模式即可。本文未论证"必须平级"，仅论证了"不能改 `code_execution` 现有无状态路径"。见 §15。

| 层 | 职责 | 持久状态 |
|---|---|---|
| `code_execution` | 短命令、无状态脚本执行 | 只回写 workspace artifact |
| `remote_workstation`（拟） | 有状态远程工作站、浏览器、profile、人工接管 | RuntimeTask、sandbox/worker id、profile ref、workspace checkpoint |
| Browser Profile Store | 保存 cookies/localStorage/profile tarball | 加密 blob + ACL + TTL + audit metadata |
| ToolRuntimeService | 工具治理、Plan Mode、preflight、timeout、activity log | decision trace / activity log |

## 6. 数据模型（draft）

### 6.1 `remote_workstations`

```text
id uuid primary key
tenant_id uuid not null
agent_id uuid not null
user_id uuid nullable
runtime_task_id uuid nullable
provider text not null
provider_session_id text nullable
status text not null          -- starting | active | idle | checkpointing | stopped | failed | expired
purpose text not null
network_policy jsonb not null
browser_profile_id uuid nullable
workspace_ref jsonb nullable
viewer_url_encrypted text nullable
cdp_url_encrypted text nullable
lease_owner text nullable
heartbeat_at timestamptz nullable
expires_at timestamptz not null
created_at timestamptz not null
updated_at timestamptz not null
metadata_json jsonb nullable
```

### 6.2 `browser_profiles`

```text
id uuid primary key
tenant_id uuid not null
agent_id uuid not null
owner_user_id uuid nullable
domain_scope jsonb not null
provider text not null
profile_blob_ref text not null
profile_blob_sha256 text not null
encrypted_key_ref text nullable
status text not null          -- active | locked | expired | revoked | corrupted
last_checkpoint_at timestamptz nullable
expires_at timestamptz nullable
created_at timestamptz not null
updated_at timestamptz not null
metadata_json jsonb nullable
```

### 6.3 RuntimeTask

新增 task type：`remote_workstation_session`。`metadata_json` 至少包含：

```json
{
  "schema": "remote_workstation_runtime_task.v1",
  "remote_workstation_id": "...",
  "provider": "vercel_sandbox_workstation",
  "provider_session_id": "...",
  "browser_profile_id": "...",
  "resume_after_restart": true,
  "network_policy": { "allow": ["example.com"] },
  "checkpoint_refs": []
}
```

## 7. Provider（第一切片）：Vercel Sandbox Workstation

适用：一次网页自动化任务；需要真实浏览器但生命周期在数分钟到 provider 上限内；需要 microVM 隔离和网络 allowlist；任务开始 hydrate profile、结束 checkpoint profile。

能力：`AsyncSandbox.create(ports=[...], runtime=..., timeout=..., network_policy=...)`；`Sandbox.get()` 恢复 active sandbox；detached command 运行浏览器服务或 CDP bridge；`domain(port)` 暴露短期 viewer/proxy；`update_network_policy()` 在 setup / execution / cleanup 间切换网络边界；snapshot 只缓存系统依赖和浏览器二进制，不保存用户登录态。

限制：不是永久工作站；sandbox filesystem 是 ephemeral，必须导出 workspace/profile artifacts；timeout 到期前必须 checkpoint；暴露 public URL 需要 Hive signed proxy，不能直接把 provider URL 暴露为长期凭证。

## 8. Provider（后续增量）：长期 Browser Worker

适用：长期浏览器 profile；用户需要 noVNC 人工登录或长时间观察；目标站点对 headless/datacenter 环境敏感。

> **provider 选型未定。** 这里需要一个能托管持久浏览器 profile 的专门 worker，候选方案（自建 worker / 第三方 browser-as-a-service）必须先经过真实性、可用性、数据合规和成本评估，再写进设计——不在本探索稿里点名绑定任何未核实的产品。

无论选哪个，部署形态的抽象边界不变：

```text
持久 browser worker
  -> persistent volume
  -> HTTPS reverse proxy
  -> Hive-issued short-lived service token
  -> CDP endpoint
  -> noVNC viewer（经 Hive signed URL + RBAC）
  -> Hive control-plane API
```

边界：worker 自身不是信任源，profile export/import 仍回到 Hive 加密管控；worker API 只接受 Hive-issued short-lived service token；每个 profile 有 tenant/agent/user/domain scope；noVNC viewer 必须通过 Hive signed URL 和 RBAC。

## 9. Browser Profile Store

browser profile 包含 cookies、localStorage、IndexedDB、cache、扩展状态等，**等价于登录凭证**。处理规则：

1. 不写入普通 `workspace/`。
2. 不进入 LLM prompt。
3. 不通过 `read_file` / `read_document` 暴露。
4. 不作为 Vercel snapshot 的一部分长期复用。
5. 不在 provider logs 中打印。
6. 导出前压缩，压缩后计算 hash，再加密保存。
7. 加密 key 由 `SecretsProvider` / KMS 管理。
8. profile 使用前需要获取 lease，避免两个 worker 并发写同一登录态。
9. revoke 后 provider 必须删除本地副本并拒绝 hydrate。

## 10. 工具面（草图）

```text
browser_start_session      browser_open_url        browser_click
browser_type               browser_extract         browser_screenshot
browser_download           browser_wait_for        browser_request_human_login
browser_checkpoint_profile browser_stop_session    run_workstation_command
```

工具参数不能携带密码、token、cookie 明文。需要登录时走：`browser_request_human_login` → create checkpoint → issue short-lived viewer URL → user logs in → checkpoint browser profile。

外部可见动作（发帖、下单、发送消息、提交表单）必须走 `ActionPreflightService` 或更严格的 confirm-first gate。

## 11. 网络策略

默认 `deny-all`。任务启动时由 tool args / plan artifact 提供目标域名 allowlist：

```json
{ "allow": ["example.com", "*.examplecdn.com"] }
```

规则：setup 期可短暂允许 package registry，但不带 backend secrets；浏览目标站点时只允许任务域名和必要 CDN；credential brokering 只用于 API header 注入，不用于浏览器用户登录态；任何 `allow-all` 必须有 plan confirmation、TTL 和 audit reason。

## 12. 生命周期

```text
start
  -> create RuntimeTask -> create or rehydrate provider session
  -> hydrate workspace -> hydrate encrypted profile
  -> launch browser / code service -> mark active

operate
  -> run browser/code actions -> emit spans and activity logs
  -> heartbeat -> periodic checkpoint

checkpoint
  -> export workspace artifacts -> export browser profile
  -> encrypt profile blob -> update BrowserProfile row -> append audit

stop
  -> final checkpoint -> stop provider session -> mark RuntimeTask terminal
```

Crash/restart：

- provider session still active：`Sandbox.get()` 或 worker reconnect，继续 heartbeat。
- provider session gone：hydrate latest checkpoint into new session。
- profile checkpoint missing or corrupted：**fail closed and ask for re-login**。

## 13. API 面（草图）

```text
POST   /api/agents/{agent_id}/workstations
GET    /api/agents/{agent_id}/workstations/{id}
POST   /api/agents/{agent_id}/workstations/{id}/checkpoint
POST   /api/agents/{agent_id}/workstations/{id}/stop
POST   /api/agents/{agent_id}/workstations/{id}/viewer-token
GET    /api/agents/{agent_id}/browser-profiles
POST   /api/agents/{agent_id}/browser-profiles/{id}/revoke
```

viewer token：short-lived；scoped to tenant/agent/user/workstation；single-purpose；not reusable as provider token。

## 14. 测试要求（针对第一切片）

```bash
cd backend && source .venv/bin/activate
pytest tests/services/test_remote_workstation_lifecycle.py -q
pytest tests/services/test_browser_profile_store.py -q
pytest tests/tools/test_browser_tools.py -q
pytest tests/api/test_remote_workstations.py -q
pytest tests/services/test_vercel_code_execution.py -q
```

Required assertions：

- profile blob never appears under normal agent workspace.
- backend secrets are not inherited by workstation provider.
- default network policy is deny-all; explicit allowlist is threaded through provider.
- profile lease prevents concurrent checkpoint writes.
- expired viewer token cannot access noVNC/CDP proxy; revoked profile cannot be hydrated.
- provider crash does not mark task completed.
- restart can recover active session or rehydrate from checkpoint.
- final stop always attempts profile checkpoint and provider cleanup.

## 15. 待决项与前置条件

**前置条件（全部满足才考虑动工）：**

1. 出现一个**具体被验证的场景**——某个用户/agent 当前确实因为缺持久浏览器能力而被卡住，能说清是哪个工作流。
2. 当前主线 **eval CI 完成**（Goal 1 自进化内核收尾）。
3. **owner 批准**把它列入路线。

**待决项：**

- 是否真需要"平级 runtime"，还是 `code_execution` 的 provider 抽象加一个 stateful session 模式即可（§5）。
- 长期 browser worker 的 provider 选型——候选需先核实真实性/可用性/合规/成本（§8）。

**倾向（非既定决策）：** `code_execution` 保持无状态；若做，browser profile 当凭证管、放在 LLM 不可读路径外；单 Vercel provider 起步，长期 worker 作为后续增量。
