# CCPlus 治理层代码修复计划

日期：2026-06-28

状态：待实施的代码修复计划

配套架构文档：`docs/ccplus-governance-layer-architecture-2026-06-28.md`

## 目标

把当前代码里的治理链路收口到 L0-L3 产品架构：

1. **L0 平台硬护栏**：不可配置、不可绕过、fail closed。
2. **L1 公司硬规则**：企业 capability / enterprise policy。
3. **L2 扩展与组合面**：只暴露高级搜索、第三方抓取、平台自带外部集成、Plaza / 广场、PaaS connector、plugin/MCP、公开扩展接口和公司预装增值能力。
4. **L3 Session Permission Mode**：当前 session 内的 allow once / allow session / deny。

关键原则：

- Agent 基础能力默认开放，不出现在 L2 可关闭面。
- L2 只管可插拔增强能力，不管 Agent 基础能力。
- L1 不负责“关掉默认功能”，只负责定义默认功能不能越过的行为边界。
- L3 是 session-local，不是企业后台审批。
- 所有真实安全边界都必须在 call-time enforce。

## 当前代码现实

### 已经正确的部分

- `backend/app/tools/service.py` 已经把执行链路串成：
  `plan gate -> runtime context -> governance -> preflight -> execute`。
- `backend/app/tools/governance.py` 已经有 L0 fail-closed、L1 capability gate、dangerous command / destructive delete、MCP policy、L3 session permission。
- `backend/app/services/capability_gate.py` 已经有 `CAPABILITY_MAP` 和 synthetic capabilities。
- `backend/app/models/installed_plugin.py` 已经有 `TenantInstalledPlugin`、`AgentPluginAssignment`、`PluginHookRegistration`。
- `backend/app/services/pack_policy_service.py` 已经能按 agent plugin assignment 影响 runtime tool visibility。
- Web / IM 的 session permission 已经有基础闭环：Web card、IM prompt、IM 文本确认、session permission resolve。

### 需要修复的错位

1. **能力分类没有代码级单源**
   - 现在 `CORE_TOOL_NAMES`、`RUNTIME_TOOL_GROUPS`、`pack.yaml`、`CAPABILITY_MAP` 各自表达一部分事实。
   - 但没有一个 governance taxonomy 明确说明：哪些是 Agent 基础能力，哪些是 L2 默认增值项，哪些是第三方扩展，哪些只能通过 L1 行为规则治理。

2. **L2 仍然像工具开关**
   - `/enterprise/tools` 仍会展示全局 enabled toggle。
   - 这会让用户误以为可以关闭 Agent 基础能力。
   - 实际 runtime 又会通过 `_ALWAYS_INCLUDE_CORE` 自动补回 CORE tools，造成产品理解和执行事实不一致。

3. **L1 前端闭环弱**
   - 后端已有 `/enterprise/capabilities`。
   - 前端 API adapter 已有 `listCapabilityPolicies` / `upsertCapabilityPolicy`。
   - 但真实产品入口没有把 Capability Policies 做成企业硬规则管理面。

4. **Web Search 混合**
   - `web_search` 当前描述仍是 AnySearch API first + SearXNG fallback。
   - 架构口径要求基础 `web_search` 代表平台基础搜索底座，AnySearch / Exa / Tavily / Firecrawl / XCrawl 进入 L2。

5. **Office 混合**
   - `office_pack` 同时承载基础文档能力和 Office Online 增值体验。
   - 需要拆成基础 agent 文档能力与 L2 Office Online / 协作编辑增值项。

6. **L3 断点恢复还不是完整 resume**
   - 当前批准后会用 bypass profile 执行原工具。
   - 这能完成工具级重放，但不等于完整模型 loop 原地恢复。
   - 目标应是保存 permission checkpoint，批准后恢复同一个 run 或受控 continuation，让工具结果回到模型 loop 继续推理。

## 修复路线

### Step 1：新增治理能力分类单源

新增一个后端单源模块，例如：

- `backend/app/services/governance_capability_taxonomy.py`

建议数据结构：

```python
from dataclasses import dataclass
from enum import StrEnum


class GovernanceCapabilityLayer(StrEnum):
    AGENT_BASE = "agent_base"
    PLATFORM_ADDON = "platform_addon"
    EXTERNAL_EXTENSION = "external_extension"
    ENTERPRISE_POLICY_ONLY = "enterprise_policy_only"


@dataclass(frozen=True)
class GovernanceCapabilityDescriptor:
    name: str
    layer: GovernanceCapabilityLayer
    tools: tuple[str, ...]
    default_enabled: bool = True
    l2_visible: bool = False
    enterprise_toggleable: bool = False
    notes: str = ""
```

初始分类：

- `agent_base`
  - 文件、命令、代码、基础 `web_fetch` / 基础 `web_search`、session/channel delivery、agent message/delegation、async task helpers、skill、memory、work ledger、subagent/workflow source、plan helpers、trigger helpers。
- `platform_addon`
  - AnySearch、Exa、Tavily、Firecrawl、XCrawl。
  - 飞书 / Lark、Slack、Email、DingTalk、WeCom、Teams、Telegram、Discord。
  - Plaza / 广场。
  - Office Online / 在线协作编辑。
  - PaaS connector。
- `external_extension`
  - 租户安装的 plugin。
  - MCP server。
  - 第三方 skill / workflow / subagent bundle。
  - 行业能力包。
- `enterprise_policy_only`
  - destructive delete。
  - share agent / cross-agent delegation 的行为边界。
  - external channel send 的审批边界。
  - company-boundary conflict。

验收：

- 单元测试覆盖每个 `CORE_TOOL_NAMES` 成员都被分类。
- 单元测试覆盖 L2 UI 候选不包含 `agent_base`。
- 单元测试覆盖 AnySearch / Exa / Firecrawl / Plaza / Feishu / MCP 属于 L2 可见能力。

### Step 2：后端保护 Agent 基础能力不能被 L2 disable

修改点：

- `backend/app/api/tools.py`
- `backend/app/services/agent_tools.py`
- `backend/app/services/pack_policy_service.py`

行为要求：

- `agent_base` 工具不允许通过 `/tools/{tool_id}` 的 `enabled=false` 关闭。
- 如果前端或 API 请求关闭基础能力，返回明确错误：`agent_base_capability_not_toggleable`。
- `Tool.enabled` 只对 L2 add-on / extension 生效。
- `_ALWAYS_INCLUDE_CORE` 继续保留，但产品层不再暗示这些工具可关闭。

验收：

- 关闭 `send_message_to_agent`、`web_fetch`、`web_search`、`start_workflow` 返回 400。
- 关闭 `exa_search`、`plaza_create_post`、`send_feishu_message` 可进入 L2 policy / assignment 流程。
- `get_agent_tools_for_llm(core_only=True)` 始终包含基础能力。

### Step 3：重做 L2 企业后台产品面

前端修改点：

- `frontend/src/pages/workspace/WorkspaceToolsSection.tsx`
- `frontend/src/api/domains/extensions.ts`
- `frontend/src/api/domains/tools.ts`
- i18n：`frontend/src/i18n/en.json`、`frontend/src/i18n/zh.json`

后端修改点：

- `backend/app/api/plugins.py`
- `backend/app/api/tools.py`
- `backend/app/services/pack_service.py`

产品要求：

- `/enterprise/tools` 改名或重塑为 **Extensions and Add-ons**。
- 不显示 Agent 基础能力的 toggle。
- L2 面只显示：
  - 高级搜索。
  - 第三方抓取。
  - 飞书 / Slack / Email / 企业 channel integrations。
  - Plaza / 广场。
  - PaaS connector。
  - Plugin / MCP。
  - 公开扩展接口。
  - 公司预装增值能力。
- 默认增值项可以默认开启，但必须可关闭、可按 agent 分配。

验收：

- UI 中不出现 `send_message_to_agent` / `web_fetch` / `start_workflow` 的关闭开关。
- UI 中出现 `AnySearch` / `Exa` / `Firecrawl` / `Feishu` / `Plaza` / `MCP` / `PaaS connector`。
- L2 disabled extension 不会出现在 `tool_search` 可发现列表。
- stale transcript 直接调用 disabled extension 时，call-time 返回 `extension_disabled`，不落到 L3 prompt。

### Step 4：拆 Web Search

后端修改点：

- `backend/app/tools/handlers/search.py`
- `backend/app/services/agent_tools.py`
- `backend/app/tools/runtime_tool_groups.py`
- `backend/packs/web_pack/pack.yaml`
- `backend/app/services/agent_tool_domains/web_mcp.py`

目标：

- 基础 `web_search` 只代表平台基础搜索底座：SearchRNG / SearXNG。
- `web_fetch` 保持 Agent 基础能力。
- AnySearch 不再是 `web_search` 的默认优先路径。
- `anysearch_*` 全部归 L2 默认增值项。
- Exa / Tavily / Firecrawl / XCrawl 继续归 L2 provider-backed extension。

验收：

- 无 AnySearch 配置时，`web_search` 仍可用。
- 关闭 AnySearch 后，`web_search` 不受影响。
- 关闭 `web_pack` / advanced search add-on 后，`anysearch_*`、`exa_search`、`firecrawl_fetch` 不可发现且不可执行。
- `web_fetch` 始终可用。

### Step 5：拆 Office CLI 与 Office Online

后端修改点：

- `backend/app/tools/handlers/office.py`
- `backend/packs/office_pack/pack.yaml`
- `backend/app/services/office_document_service.py`
- `backend/app/api/office.py`
- `backend/app/services/pack_policy_service.py`

前端修改点：

- Office Workbench 相关页面。
- Enterprise L2 add-ons 页面。

目标：

- Office CLI / 文档生成、读取、转换、编辑所需的 agent 能力归基础能力或基础文档能力，不可被 L2 关闭。
- Office Online / 在线协作编辑 / 浏览器工作台归 L2 默认增值项，可企业关闭。
- 关闭 Office Online 不影响 agent 生成文档、读取文档、修改文档。

验收：

- 关闭 Office Online 后，Agent 仍可 `office_document_create` 或等价基础文档生成能力。
- 关闭 Office Online 后，Web UI 不显示在线编辑入口。
- 文档外发、分享、删除、覆盖仍走 L1 / L3 行为治理。

### Step 6：补 L1 Capability Policies 管理面

后端已有：

- `backend/app/api/capabilities.py`
- `backend/app/services/capability_gate.py`

需要补：

- 前端企业页 Capability Policies 面。
- capability definition 分组和文案。
- 默认企业硬规则模板。

建议分组：

- Agent collaboration
  - `agent.message.send`
  - `agent.subagent.spawn`
  - `agent.workflow.run`
- Workspace mutation
  - `workspace.file.write`
  - `workspace.command.execute`
  - `workspace.command.destructive_delete`
- External communication
  - `channel.message.send`
  - `channel.file.send`
  - `channel.email.send`
  - `channel.feishu.message`
- Extension / MCP
  - `agent.mcp.call`
  - `agent.tool.install`
  - `external.api.call`
- Community / Plaza
  - `plaza.post.write`

同时修正：

- `Capability '<cap>' has no capability policy configured; admin approval is required`
- 改为：
  - `No enterprise policy configured; falling through to session permission mode`

验收：

- 企业后台可设置 tenant default deny / approval / allow。
- L1 deny 优先于 L3 `bypassPermissions`。
- 缺少 policy 时进入 L3，而不是企业审批。
- 前端不再把 capability policy 混进工具开关。

### Step 7：L3 Permission Checkpoint / Resume

当前行为：

- permission required 时，run 会暂停并生成 permission request。
- 用户批准后，`resolve_session_permission()` 用 bypass profile 执行原工具。
- 这更像工具级重放，不是完整模型 loop resume。

目标行为：

- permission required 时，`RuntimeTask` 进入 `waiting_for_user`。
- 持久化 `permission_checkpoint`：
  - `permission_request_id`
  - `tool_call_id`
  - `tool_name`
  - `arguments`
  - `round_state`
  - `runtime_task_id`
  - `session_id`
  - `origin_channel`
  - `permission_profile`
- allow 后恢复同一个 run，或创建明确的 continuation run。
- 工具结果回到 model loop，模型继续下一步。
- deny 后把 denial result 回到 model loop，让模型解释或改路。
- Web 和 IM 复用同一 checkpoint。

验收：

- Web allow once 后，模型能继续下一步，而不是只显示工具结果。
- IM allow once 后，模型能继续执行并把最终结果发回原 channel。
- deny 后模型收到 denial 并给出替代方案。
- 重复 resolve、stale request、非同 session request 都被拒绝。
- process restart 后 pending permission request 仍能在 session 中恢复或明确标记 expired。

### Step 8：回归测试矩阵

必须新增或调整测试：

- Agent 基础能力不可被 L2 关闭。
- L2 disabled extension 不可发现、不可执行。
- `web_search` 不依赖 AnySearch。
- AnySearch disabled 不影响基础 web search/fetch。
- Office Online disabled 不影响基础文档生成。
- Plaza disabled 后 `plaza_create_post` 不可发现、不可执行。
- L1 hard deny 优先于 L3 bypass。
- Missing L1 policy 进入 L3 session prompt。
- Web session permission prompt + resume。
- IM session permission prompt + resume。
- Hook 修改 args 后重新走 schema / capability / preflight。

## 实施顺序建议

1. Step 1：taxonomy 单源和只读测试。
2. Step 2：保护基础能力不能被 L2 disable。
3. Step 3：L2 UI / API 重塑。
4. Step 4：拆 Web Search。
5. Step 5：拆 Office。
6. Step 6：补 L1 Capability Policies 管理面。
7. Step 7：L3 checkpoint / resume。
8. Step 8：全链路回归。

这个顺序的理由：

- 先建立分类单源，否则后续 UI 和 runtime 会继续各自判断。
- 先保护基础能力，避免企业后台继续制造错误开关。
- 先收 L2，再拆 Web / Office，减少迁移面。
- L3 checkpoint / resume 最后做，因为它触及 runtime task 生命周期，风险最大。

## 非目标

- 不删除 Agent 基础能力。
- 不把基础能力迁移成企业可关闭插件。
- 不用 L2 替代 L1 行为治理。
- 不用 L1 替代 L3 session-local consent。
- 不允许 plugin / hook 绕过 call-time governance。
