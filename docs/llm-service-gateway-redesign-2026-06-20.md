# AI Gateway Redesign: Model Gateway + Connector Gateway

| 字段 | 内容 |
|------|------|
| 状态 | Discussion draft |
| 日期 | 2026-06-20；更新：2026-07-14 |
| 范围 | Company Settings 的 AI 模型接入体验、LiteLLM Gateway 接入、直连模型服务、模型目录同步、`llm_models` 兼容迁移；OpenConnector 作为连接器 Gateway 的插件化接入、Agent Detail 个性化安装、连接身份归属、OpenConnector action 同步与执行 |
| 非范围 | 替换 Kernel、删除 provider-native adapter、把 LiteLLM 作为唯一 LLM backend、改变 agent/runtime 的模型引用语义；把所有连接器重写成 Hive 自研 connector runtime、让 Agent/LLM 直接持有 OAuth/API key、绕过 Hive Platform Gate 直接调用 OpenConnector |

## 0. 结论

Hive 不应该把所有模型调用都收敛成 LiteLLM / OpenAI-compatible 一条路。正确方向是把 LLM 接入拆成两条并行 lane：

```text
Hive Kernel / Runtime
  -> existing LLMClient contract
    -> Native Direct Lane
       -> OpenAI direct
       -> Anthropic direct
       -> Gemini direct
       -> DeepSeek direct
    -> Gateway Lane
       -> LiteLLM Gateway (default recommended aggregator)
```

产品上，LiteLLM Gateway 是默认推荐入口，负责大多数长尾模型、统一 key、路由、预算、fallback 和 catalog。OpenAI、Anthropic、Gemini、DeepSeek 是一等直连服务，负责保留 Hive 已经实现的 provider-native 优化。

后台页面也要跟着拆：用户先配置“模型服务账号”，再从该服务的模型目录中发布模型到 Hive 的 runtime model pool。当前“一条模型记录同时承载 provider、api key、base url、模型能力和高级推理参数”的表单必须退到兼容层。

同一轮 redesign 还应该引入第二个 Gateway：**OpenConnector 作为连接器 Gateway**。LiteLLM 管模型服务；OpenConnector 管 SaaS / 业务系统连接器。两者都是 Gateway，但产品边界不同：

```text
AI Gateway Plane
  -> Model Gateway
     -> LiteLLM Gateway
     -> provider-native direct services
     -> published llm_models runtime pool
  -> Connector Gateway
     -> OpenConnector plugin
     -> provider/app catalog
     -> action catalog
     -> user / agent / tenant connection identities
     -> agent-scoped action assignments
```

OpenConnector 不应该被当成“又一个普通 MCP server”直接暴露给 Agent。它在 Hive 里应该表现为一个 **plugin-contributed connector capability source**：公司先安装 OpenConnector 插件并配置 Gateway；Agent Detail 再按 Agent 个性化选择 app/provider/action 和连接身份。执行时 Hive 后端解析连接身份并调用 OpenConnector，LLM 不接触 token、OAuth credential、runtime token 或 connection alias。

## 1. 当前仓库事实

### 1.1 已有 provider-native 能力

当前 `backend/app/services/llm_client.py` 已经不是薄 OpenAI proxy。它维护 `ProviderSpec` registry，并把 provider capability 显式建模：

1. `ProviderSpec.protocol` 区分 `openai_compatible`、`openai_responses`、`anthropic`、`gemini`。
2. Anthropic 直连使用 `AnthropicClient`，包含 Messages API、thinking block、thinking signature、interleaved thinking beta header、native tool_result block 映射。
3. Gemini 直连使用 `GeminiClient`，包含 native `generateContent` / `streamGenerateContent` payload、systemInstruction、functionDeclarations、usage normalization。
4. OpenAI 新模型可按 model prefix 切到 Responses API。
5. DeepSeek 虽然是 OpenAI-compatible protocol，但已有独立 `reasoning_strategy="deepseek_thinking"`、大输出上限、temperature omit、reasoning preservation 等策略。
6. `services/llm_reasoning.py` 是 provider-specific reasoning/thinking translation layer，不能被 Gateway 抹平。
7. `create_llm_client()` 已经是 runtime 的统一 seam；最低风险的 Gateway 接入方式是新增 provider/adapter，而不是替换 Kernel。

这些能力属于 Hive 的 AI-native 质量边界。Gateway 化不能让它们丢失。

### 1.2 当前后台接入页面的问题

当前 `frontend/src/pages/workspace/WorkspaceLlmSection.tsx` 直接让管理员填写：

- provider
- model
- label
- base_url
- api_key
- supports_vision
- max_output_tokens
- max_input_tokens
- temperature
- reasoning_mode / effort / budget / display / preservation
- provider_options JSON

后端 `LLMModel` 也把 `api_key_encrypted`、`base_url`、`provider`、`model`、runtime 参数放在同一条模型记录上。

这导致四个问题：

1. **服务账号和模型实例混在一起**：同一个 OpenRouter / LiteLLM key 下添加 20 个模型，需要重复理解和维护同一组连接信息。
2. **新手路径过载**：接入模型时暴露了太多 runtime tuning 参数，管理员必须理解 provider 内部差异。
3. **catalog 缺失**：系统只给静态 recommended models，不是从上游服务发现可用模型。
4. **provider-native 与 gateway 语义混淆**：如果一个 Claude 模型来自 LiteLLM Gateway，它不能被 Hive 当成 Anthropic native runtime，否则 thinking signature/cache/tool-result 语义会失真。

### 1.3 当前连接器/插件事实

当前仓库已有可以承接 OpenConnector 的基础面：

1. `TenantInstalledPlugin` / `AgentPluginAssignment` 已经能表达“租户安装插件、Agent 个性化启用插件”。
2. Agent Detail 的 `ToolsManager` 已经展示插件并提供 Agent 级启用/禁用。
3. `Tool` / `AgentTool` 已经能表达“某个 Agent 拥有哪些工具/action”。
4. `TenantToolConfig` + `AgentTool.config` 已经支持租户/Agent 层级的工具配置，但它不是完整的连接身份模型，不能单独表达 user-owned OAuth、agent-owned OAuth、tenant service account 之间的权限差异。
5. `connector_acl.py` 已经提供 connector 内容进入 prompt 前的本地 ACL mirror，这个边界应继续保留，不能因为 OpenConnector 自带策略就绕过 Hive prompt ingress filter。
6. MCP server-first 表已经存在，但 OpenConnector 的 MCP 入口是 discovery-oriented 工具集；如果只按普通 MCP server 接入，Hive 难以对每个 SaaS action 做精确授权、审批和审计。

因此，OpenConnector 的落点不是替代这些表，而是作为插件和连接器 Gateway，向 Hive 同步 provider/action，再落到 Hive 的 agent-scoped tool/action assignment。

## 2. 设计硬边界

### 2.1 LiteLLM 是推荐入口，不是唯一 backend

LiteLLM Gateway 在 Hive 里是一个 **模型服务连接类型**，不是 Hive LLM backend 的替代品。

允许：

- 使用 LiteLLM 管理长尾 provider、模型路由、统一 key、spend tracking、fallback。
- 从 LiteLLM `/v1/models` / `/model/info` 同步模型目录。
- 把 LiteLLM 目录里的模型发布成 Hive runtime model。
- 默认把 LiteLLM 放在“推荐接入”首位。

禁止：

- 把 OpenAI、Anthropic、Gemini、DeepSeek 直连调用全部改成 LiteLLM。
- 让 `provider="anthropic"` 的模型实际走 LiteLLM Gateway。
- 让 `provider="gemini"` 的模型实际走 OpenAI-compatible wrapper。
- 把 LiteLLM 的 OpenAI-compatible 输出当成 provider-native thinking/cache/tool-result 的等价替代。

### 2.2 Native provider fidelity 优先级高于统一接口便利性

直连服务必须保留为一等路径：

| 服务 | Runtime provider | 保留原因 |
|------|------------------|----------|
| OpenAI Direct | `openai` / `openai-response` | Responses API、reasoning/text verbosity、未来官方新能力 |
| Anthropic Direct | `anthropic` | native Messages API、thinking signature、cache_control、native multimodal tool_result |
| Gemini Direct | `gemini` | native `generateContent`、Gemini usage metadata、functionResponse、cached content accounting |
| DeepSeek Direct | `deepseek` | V4 thinking controls、大输出上限、reasoning_content/tool-call 组合、temperature omit |
| LiteLLM Gateway | `litellm_gateway` | 长尾模型聚合、路由、预算、fallback、catalog |

### 2.3 Gateway 模型不能伪装成 native 模型

从 LiteLLM 添加的 `anthropic/claude-*`、`gemini/*`、`deepseek/*` 模型，在 Hive runtime 里应该是：

```json
{
  "provider": "litellm_gateway",
  "model": "anthropic/claude-sonnet-4-5",
  "source_connection_type": "litellm_gateway",
  "upstream_provider_family": "anthropic"
}
```

而不是：

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "base_url": "https://litellm.example.com/v1"
}
```

原因：后者会误触发 Anthropic native adapter 和 reasoning/signature/cache 逻辑，但 Gateway 通道只保证 OpenAI-compatible 层面的兼容。

UI 可以按 upstream family 显示 “Anthropic / Claude”，但 runtime provider 必须诚实。

### 2.4 `llm_models` 继续是 runtime model pool

现有 agent、memory、eval、workflow、subagent 都引用 `llm_models.id`。这条 contract 不应在第一版重构里打破。

新结构应该是：

```text
llm_service_connections
  -> llm_service_catalog_models
    -> llm_models
      -> Agent.primary_model_id / fallback_model_id / default_model_id
```

`llm_models` 从“用户手填模型账号”转成“已发布给 Hive runtime 使用的模型快照”。

### 2.5 Key ownership and operating modes

LiteLLM Gateway 本身不是一种用户必须拥有的账号。它是一层 proxy/gateway。Hive 可以用它承载不同的 key ownership 模式。

必须区分四种模式：

| 模式 | 用户是否需要官方 provider key | 用户在 Hive 填什么 | Hive/LiteLLM 持有什么 | Runtime path |
|------|-------------------------------|--------------------|------------------------|--------------|
| Hive Managed Gateway | 不需要 | 不填 provider key；只使用 Hive 开通的模型额度或 Hive API key | Hive 持有上游 provider keys；Hive 为租户生成 LiteLLM virtual key | Hive -> LiteLLM -> provider |
| BYOK Direct | 需要，且 provider 是 OpenAI / Anthropic / Gemini / DeepSeek | 官方 provider key | Hive 保存该 provider key；不进入 LiteLLM | Hive -> native direct adapter -> provider |
| BYOK Gateway Relay | 需要，且 provider 是 MiniMax / Qwen / Moonshot / Groq / Together 等长尾 provider | 官方 provider key，例如 MiniMax API key | Hive 保存/托管该 provider key，并把它配置进 Hive 部署的 LiteLLM；Hive runtime 使用 LiteLLM virtual key | Hive -> LiteLLM -> provider |
| Self-hosted Gateway | 用户已有自己的 LiteLLM 或兼容 Gateway | Gateway base URL + gateway virtual key | Hive 只保存用户的 gateway endpoint/key；不接触上游 provider key | Hive -> user gateway -> provider |

This means:

1. “用户申请 Hive key”属于 **Hive Managed Gateway**。用户拿到的是 Hive 权限或 Hive API key，背后映射到 Hive 内部的 LiteLLM virtual key 和预算。
2. “用户填 Gemini key”属于 **BYOK Direct**，应该走 Gemini native adapter，不经过 LiteLLM。
3. “用户填 MiniMax key”属于 **BYOK Gateway Relay**。用户填的是 MiniMax 官方 key，不是 LiteLLM key；Hive 后台把这个 key 接入我们部署的 LiteLLM，再由 Hive runtime 通过 LiteLLM virtual key 调用。
4. “用户填 LiteLLM URL + key”只属于 **Self-hosted Gateway** 高级场景。不要把它当成普通用户的默认路径。

Hive UI 应该把这四种模式做成明确入口，而不是只给一个含糊的 “LiteLLM API Key” 表单。

### 2.6 OpenConnector 是连接器 Gateway，不是模型 Gateway

OpenConnector 在 Hive 中负责：

- SaaS/provider catalog。
- OAuth2、API key、custom credential 的连接流程。
- Action schema、required scopes、provider permissions。
- Action allow/block policy。
- Run logs 和连接 profile。
- MCP/HTTP/OpenAPI access surfaces。

OpenConnector 不负责：

- 模型调用和 `llm_models` 发布。
- 替换 LiteLLM 或 provider-native LLM adapters。
- 替换 Hive Platform Gate、approval、audit、connector ACL prompt ingress filter。
- 给 LLM 暴露 provider token、OpenConnector admin token、runtime token 或 raw connection alias。

### 2.7 OpenConnector 必须插件化安装

OpenConnector 在产品里是一个插件：

```text
TenantInstalledPlugin(plugin_key="open_connector")
  -> ConnectorGateway(type="open_connector")
  -> synced ConnectorProvider / ConnectorAction catalog
  -> AgentPluginAssignment(agent_id, plugin)
  -> AgentConnectorActionAssignment(agent_id, action, mode)
```

插件开关只表示 Agent 可以看到 OpenConnector 贡献的能力面；它不等于授权全部 providers/actions。Agent 必须进一步选择具体 app/provider/action。

禁止：

- Agent 启用 `open_connector` 插件后自动获得 1000+ provider actions。
- 把 OpenConnector MCP `execute_action` 暴露成一个无限制超级工具。
- 让前端把 OpenConnector runtime/admin token 下载到浏览器。
- 让 LLM 在 tool arguments 中指定 alias、token、API key、OAuth state。

允许：

- Workspace admin 安装 OpenConnector 插件并配置 Gateway。
- Workspace admin 设置 provider/action allowlist。
- User 在 Agent Detail 为自己的 user-agent 连接自己的 OAuth/API key。
- Agent owner/admin 为 Agent 配置 agent-owned service account。
- Workspace admin 配置 tenant service account，并显式授权给 Agent/action。

### 2.8 连接身份归属必须一等建模

团队协作下，同一个 action 可能使用不同身份执行：

| 连接身份 | Owner | 典型场景 | Runtime 约束 |
|----------|-------|----------|--------------|
| User-owned connection | `user_id` | 用户让自己的 user-agent 读写自己的 Gmail、Calendar、GitHub | 必须有当前 user context；后台解析该用户的 OpenConnector alias |
| Agent-owned connection | `agent_id` | 一个长期运行的 digital employee 使用专属邮箱、专属 GitHub bot、专属 Slack app | 可以用于 patrol/trigger/autonomous run；必须由 agent owner/admin 授权 |
| Tenant service account | `tenant_id` | 公司共享 Notion workspace、support inbox、CRM、analytics | 必须由 Workspace admin 配置，并在 action assignment 上显式允许 |

`TenantToolConfig` 可以继续用于非 OAuth 的工具配置，但 OpenConnector 需要独立 `connector_connections`，否则无法正确区分“当前用户账号”和“Agent 专属账号”。

## 3. 目标产品形态

### 3.1 页面层级

Company Settings / Models 页面改成三层：

```text
模型服务
  Hive Managed Gateway    Recommended
  LiteLLM Gateway          Recommended
  OpenAI Direct
  Anthropic Direct
  Gemini Direct
  DeepSeek Direct
  Custom OpenAI-compatible Advanced

服务详情
  API Key / Base URL / Enabled / Test / Sync Catalog
  Catalog health / last synced / model count
  Published model count

模型目录
  Search model ID or name
  Filters: reasoning / vision / tools / embedding / rerank / free / multimodal
  Group by upstream provider family
  Add one model / Add group / Refresh

Hive 模型池
  Enabled runtime models
  Default / fallback / memory / eval assignment surfaces
  Advanced settings drawer
```

### 3.2 管理员主路径

首次接入推荐路径：

1. 进入 Models。
2. 如果租户没有任何 provider key，默认看到 Hive Managed Gateway，标记 Recommended。
3. 用户开通 Hive 模型额度或管理员为租户启用 Hive Managed Gateway。
4. Hive 在内部生成/绑定 LiteLLM virtual key 和预算。
5. 打开模型目录弹窗。
6. 搜索/过滤模型，添加需要的模型到 Hive model pool。
7. 选择默认模型。

直连高保真路径：

1. 选择 Anthropic Direct / Gemini Direct / OpenAI Direct / DeepSeek Direct。
2. 输入官方 API key，Base URL 默认隐藏或只在高级设置里展示。
3. 点击 Test Connection。
4. 使用官方 recommended models 或 provider catalog。
5. 添加模型到 Hive model pool。

长尾 BYOK Gateway Relay 路径：

1. 选择 MiniMax / Qwen / Moonshot / Groq / Together 等长尾 provider。
2. 输入该 provider 的官方 API key。
3. Hive 将该 provider key 写入 Hive 管理的 LiteLLM 配置/数据库。
4. Hive 为该租户生成或复用 LiteLLM virtual key。
5. 用户在 Hive 里从该 provider 的 catalog 发布模型。
6. Runtime 统一走 `provider="litellm_gateway"`，但 UI 显示 upstream provider family。

自托管 Gateway 路径：

1. 选择 Self-hosted LiteLLM Gateway 或 Custom OpenAI-compatible Gateway。
2. 输入 gateway base URL 和 gateway virtual key。
3. Hive 不接触上游 provider key，只把该 gateway 当成外部 OpenAI-compatible endpoint。

### 3.3 高级参数的展示位置

新增服务和添加模型主流程中，不展示所有高级 runtime 参数。

高级参数应该进入“模型详情 / 高级设置抽屉”：

- max input tokens
- max output tokens
- temperature
- reasoning mode
- reasoning effort
- reasoning budget
- preserve reasoning
- text verbosity
- provider_options JSON

抽屉应根据 runtime provider 控制可见项：

- `anthropic` 显示 Anthropic thinking/adaptive/budget/preserve。
- `openai` / `openai-response` 显示 reasoning effort 和 text verbosity。
- `gemini` 默认少展示 reasoning knobs，优先展示 context/capability。
- `deepseek` 显示 thinking enabled/disabled、effort high/max，并明确 thinking 时 temperature 不生效。
- `litellm_gateway` 默认只展示安全通用项；provider-native knobs 不默认开放。

### 3.4 OpenConnector 管理员主路径

Workspace / Company Settings 中新增 OpenConnector 插件管理：

```text
Extensions & Add-ons
  OpenConnector                 Recommended Connector Gateway
    Gateway runtime
      Base URL
      Runtime token
      Admin token
      Test gateway
    Catalog
      Sync providers
      Sync actions
      Provider/action allowlist
    OAuth clients
      Provider OAuth app config
      Redirect/callback health
    Connections
      Tenant service accounts
      Recent failures
      Run logs link / summary
```

管理员主路径：

1. 安装 `open_connector` 插件。
2. 配置 OpenConnector runtime URL、runtime token、admin token。
3. 测试 `/v1/health`、`/v1/providers`、`/v1/actions`。
4. 同步 provider/action catalog 到 Hive。
5. 配置 provider OAuth client 或 API key credential requirements。
6. 设置 tenant-level action allowlist/blocklist。
7. 可选：配置 tenant service account，并显式授权给某些 Agent/action。

### 3.5 Agent Detail 个性化安装路径

Agent Detail / Extensions 中，OpenConnector 插件应展开为 app/action 选择面：

```text
OpenConnector
  Plugin: On / Off

  Apps
    Gmail
      Connection
        Use my account
        Connect agent account
        Use workspace service account
      Actions
        gmail.search_messages      auto
        gmail.send_message         approval
        gmail.delete_message       deny

    GitHub
      Connection
        Use my GitHub
      Actions
        github.get_current_user    auto
        github.create_issue        approval
```

前端只负责发起连接和展示连接状态；实际 token、alias、runtime token 都留在后端和 OpenConnector runtime。OAuth 流程：

1. 用户点击 Connect。
2. Hive 后端创建 pending connection，生成服务端 alias。
3. Hive 调 OpenConnector `/api/oauth/authorizations`，传入 `service` 和 `connectionName`。
4. Hive 把 `authorizationUrl` 返回前端。
5. 用户在 provider 页面授权。
6. OpenConnector callback 完成后，Hive 刷新 `/api/connections`，保存 profile/status/alias mapping。
7. Agent action assignment 引用 Hive connection record，而不是直接保存 alias 到前端状态。

## 4. 数据模型设计

### 4.1 `llm_service_connections`

```python
class LLMServiceConnection:
    id: UUID
    tenant_id: UUID
    service_type: Literal[
        "hive_managed_gateway",
        "litellm_gateway",
        "openai_direct",
        "anthropic_direct",
        "gemini_direct",
        "deepseek_direct",
        "custom_openai_compatible",
    ]
    ownership_mode: Literal[
        "hive_managed",
        "byok_direct",
        "byok_gateway_relay",
        "self_hosted_gateway",
    ]
    display_name: str
    base_url: str | None
    api_key_encrypted: str | None
    credential_role: Literal[
        "hive_internal_virtual_key",
        "tenant_provider_key",
        "tenant_gateway_virtual_key",
        "none",
    ]
    enabled: bool
    recommended: bool
    health_status: Literal["unknown", "ok", "warning", "error"]
    health_message: str | None
    last_tested_at: datetime | None
    last_synced_at: datetime | None
    catalog_model_count: int
    provider_options: dict | None
    created_at: datetime
    updated_at: datetime
```

Notes:

1. `recommended=True` 可用于 Hive Managed Gateway 和 LiteLLM Gateway 默认卡片，不代表强制使用。
2. `api_key_encrypted` 仍由 Hive secrets provider 管理，不暴露给 LLM。
3. `ownership_mode="hive_managed"` 时，用户不提供 provider key；Hive 使用平台上游 key，并为租户生成内部 LiteLLM virtual key。
4. `ownership_mode="byok_direct"` 时，`api_key_encrypted` 是用户提供的官方 provider key，runtime 不经过 LiteLLM。
5. `ownership_mode="byok_gateway_relay"` 时，`api_key_encrypted` 是用户提供的长尾 provider key；Hive 将它接入 Hive 管理的 LiteLLM，runtime 使用内部 virtual key 调用。
6. `ownership_mode="self_hosted_gateway"` 时，`api_key_encrypted` 是用户自己的 gateway virtual key，Hive 不接触上游 provider key。
7. Direct service 的 `base_url` 可以为空，默认从 `ProviderSpec.default_base_url` 推导。
8. Custom OpenAI-compatible 是高级逃生口，不应该和官方直连服务混在一起。

### 4.2 `llm_service_catalog_models`

```python
class LLMServiceCatalogModel:
    id: UUID
    tenant_id: UUID
    connection_id: UUID
    upstream_model_id: str
    display_name: str
    upstream_provider_family: str | None
    description: str | None
    capabilities: dict
    context_window: int | None
    max_output_tokens: int | None
    pricing_metadata: dict | None
    raw_metadata: dict | None
    last_seen_at: datetime
    first_seen_at: datetime
    retired_at: datetime | None
```

`capabilities` should be normalized:

```json
{
  "reasoning": true,
  "vision": true,
  "tools": true,
  "embedding": false,
  "rerank": false,
  "web": false,
  "free": false,
  "image_generation": false
}
```

This is catalog/display metadata, not a provider-native runtime guarantee.

### 4.3 `llm_models` compatibility extension

Add nullable references while keeping old fields:

```python
class LLMModel:
    service_connection_id: UUID | None
    catalog_model_id: UUID | None
    source_kind: Literal["legacy_manual", "service_catalog", "manual_gateway"]
    upstream_provider_family: str | None
```

Existing fields remain authoritative for runtime:

- `provider`
- `model`
- `api_key_encrypted`
- `base_url`
- `supports_vision`
- `max_input_tokens`
- `max_output_tokens`
- reasoning settings

For service-backed models, those fields become a snapshot copied from the connection/catalog at publish time. Runtime remains stable even if catalog later changes.

### 4.4 Connector Gateway models

OpenConnector 使用独立连接器模型，不复用 `llm_service_connections`。

```python
class ConnectorGateway:
    id: UUID
    tenant_id: UUID
    plugin_id: UUID
    gateway_type: Literal["open_connector"]
    display_name: str
    base_url: str
    runtime_token_encrypted: str
    admin_token_encrypted: str | None
    status: Literal["unknown", "ok", "warning", "error", "disabled"]
    health_message: str | None
    last_tested_at: datetime | None
    last_synced_at: datetime | None
    provider_count: int
    action_count: int
    config_json: dict
    created_at: datetime
    updated_at: datetime
```

```python
class ConnectorProvider:
    id: UUID
    tenant_id: UUID
    gateway_id: UUID
    provider_key: str      # gmail, github, slack
    display_name: str
    auth_types: list[str]  # no_auth, api_key, oauth2, custom_credential
    required_scopes: list[str]
    raw_metadata: dict
    status: Literal["available", "disabled", "retired"]
    last_seen_at: datetime
```

```python
class ConnectorAction:
    id: UUID
    tenant_id: UUID
    gateway_id: UUID
    provider_key: str
    action_id: str         # github.get_current_user
    display_name: str
    description: str
    input_schema: dict
    output_schema: dict | None
    required_scopes: list[str]
    provider_permissions: list[str]
    raw_metadata: dict
    status: Literal["available", "disabled", "retired"]
    last_seen_at: datetime
```

```python
class ConnectorConnection:
    id: UUID
    tenant_id: UUID
    gateway_id: UUID
    provider_key: str
    owner_type: Literal["user", "agent", "tenant"]
    owner_id: UUID
    connection_alias_encrypted: str
    auth_type: Literal["no_auth", "api_key", "oauth2", "custom_credential"]
    status: Literal["pending", "connected", "needs_reconnect", "revoked", "error"]
    profile_json: dict     # accountId/displayName/grantedScopes only; never raw tokens
    created_by_user_id: UUID | None
    last_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime
```

```python
class AgentConnectorActionAssignment:
    id: UUID
    tenant_id: UUID
    agent_id: UUID
    action_id: UUID
    enabled: bool
    mode: Literal["auto", "approval", "deny"]
    connection_policy: dict
    created_at: datetime
    updated_at: datetime
```

Example `connection_policy`:

```json
{
  "allowed_owner_types": ["user", "agent"],
  "preferred_owner_type": "user",
  "fallback_owner_type": "agent",
  "allow_tenant_service_account": false,
  "required_connection_id": null
}
```

OpenConnector actions may also be projected into existing `Tool(type="open_connector")` rows for runtime tool discovery, but the authoritative connector identity and action assignment live in connector-specific tables.

## 5. Provider/runtime mapping

### 5.1 Direct service mapping

| Service connection | Published `LLMModel.provider` | Published `base_url` | Client |
|--------------------|-------------------------------|-----------------------|--------|
| OpenAI Direct | `openai` or `openai-response` | OpenAI default or override | `OpenAICompatibleClient` / `OpenAIResponsesClient` |
| Anthropic Direct | `anthropic` | Anthropic default or override | `AnthropicClient` |
| Gemini Direct | `gemini` | Gemini default or override | `GeminiClient` |
| DeepSeek Direct | `deepseek` | DeepSeek default or override | `OpenAICompatibleClient(provider="deepseek")` |

### 5.2 LiteLLM Gateway mapping

| Mode | Catalog source | Published `LLMModel.provider` | Published `LLMModel.model` | Published `base_url` |
|------|----------------|-------------------------------|-----------------------------|----------------------|
| Hive Managed Gateway | Hive-managed LiteLLM `/v1/models` | `litellm_gateway` | model id from LiteLLM | internal LiteLLM base URL |
| BYOK Gateway Relay | Hive-managed LiteLLM `/v1/models` after tenant provider key registration | `litellm_gateway` | model id from LiteLLM | internal LiteLLM base URL |
| Self-hosted Gateway | User gateway `/v1/models` | `litellm_gateway` | model id from user gateway | user gateway base URL |

`ProviderSpec` needs a new entry:

```python
ProviderSpec(
    provider="litellm_gateway",
    display_name="LiteLLM Gateway",
    protocol="openai_compatible",
    default_base_url=None,
    default_max_tokens=8192,
    max_input_tokens=128000,
    reasoning_strategy="none",
)
```

The UI may display upstream family from catalog, but runtime uses `OpenAICompatibleClient` with `provider="litellm_gateway"`.

The credential used by runtime is different by mode:

1. Hive Managed Gateway: Hive uses an internal LiteLLM virtual key scoped to the tenant/team.
2. BYOK Gateway Relay: Hive still uses an internal LiteLLM virtual key, but the upstream provider credential belongs to the tenant and is registered into LiteLLM by Hive.
3. Self-hosted Gateway: Hive uses the user's gateway virtual key directly.

### 5.3 Why not publish Gateway Claude as `anthropic`

This is forbidden because native Anthropic path uses:

- Anthropic Messages API request shape
- `x-api-key` headers
- `anthropic-version` / beta headers
- thinking block + signature replay
- native `tool_result` blocks
- cache_control-aware content blocks

LiteLLM Gateway only guarantees Gateway contract, not all of those native semantics.

### 5.4 OpenConnector runtime mapping

OpenConnector action execution is server-side only:

```text
LLM tool call
  -> Hive ToolRuntimeService
  -> OpenConnector action handler
  -> Platform Gate / approval / AgentConnectorActionAssignment
  -> resolve ConnectorConnection from current user / agent / tenant policy
  -> call OpenConnector POST /v1/actions/{action_id}
       Authorization: Bearer <runtime_token>
       x-oo-connector-alias: <server-resolved alias>
       body: {"input": {...}}
  -> register connector source items / ACL metadata where returned
  -> return redacted result to model
```

The LLM-visible schema includes only action input fields. It must not include:

- OpenConnector runtime token.
- OpenConnector admin token.
- Provider OAuth tokens.
- Provider API keys.
- `connection_alias`.
- `owner_id`.

If a model tries to pass credential-like keys such as `api_key`, `access_token`, `authorization`, `bearer_token`, `connection_alias`, or `x-oo-connector-alias` as action arguments, the handler must reject them before calling OpenConnector.

## 6. API surface

### 6.1 New endpoints

```text
GET    /enterprise/llm-services
POST   /enterprise/llm-services
PUT    /enterprise/llm-services/{service_id}
DELETE /enterprise/llm-services/{service_id}

POST   /enterprise/llm-services/{service_id}/test
POST   /enterprise/llm-services/{service_id}/sync-catalog
GET    /enterprise/llm-services/{service_id}/catalog

POST   /enterprise/llm-services/{service_id}/publish-models
```

### 6.1.1 Connector Gateway endpoints

```text
GET    /enterprise/connector-gateways
POST   /enterprise/connector-gateways/open-connector
PUT    /enterprise/connector-gateways/{gateway_id}
DELETE /enterprise/connector-gateways/{gateway_id}

POST   /enterprise/connector-gateways/{gateway_id}/test
POST   /enterprise/connector-gateways/{gateway_id}/sync-catalog
GET    /enterprise/connector-gateways/{gateway_id}/providers
GET    /enterprise/connector-gateways/{gateway_id}/actions

GET    /enterprise/connector-connections
POST   /enterprise/connector-connections/{provider_key}/api-key
POST   /enterprise/connector-connections/{provider_key}/oauth/start
POST   /enterprise/connector-connections/{connection_id}/verify
DELETE /enterprise/connector-connections/{connection_id}

GET    /agents/{agent_id}/connector-actions
PUT    /agents/{agent_id}/connector-actions/{action_id}
GET    /agents/{agent_id}/connector-connections
POST   /agents/{agent_id}/connector-connections/{provider_key}/oauth/start
POST   /agents/{agent_id}/connector-connections/{provider_key}/api-key
```

Agent-scoped routes must check agent manage access. Enterprise routes require workspace admin. User-owned connection routes require the current authenticated user and cannot be delegated through arbitrary natural-language confirmation.

### 6.2 Keep existing endpoints

Keep these for runtime compatibility and existing UI consumers:

```text
GET    /enterprise/llm-providers
GET    /enterprise/llm-models
POST   /enterprise/llm-models
PUT    /enterprise/llm-models/{model_id}
DELETE /enterprise/llm-models/{model_id}
PUT    /enterprise/llm-models/default
POST   /enterprise/llm-test
```

But the new UI should prefer service endpoints for creation. Direct `POST /llm-models` becomes advanced/manual fallback.

### 6.3 Test connection behavior

Service test should not always require a model chosen by user.

| Service type | Test method |
|--------------|-------------|
| Hive Managed Gateway | Verify tenant has enabled Hive quota/internal virtual key, GET internal LiteLLM `/v1/models`, then optional small completion |
| BYOK Gateway Relay | Probe provider key registration through Hive-managed LiteLLM, GET `/v1/models`, then optional small completion |
| Self-hosted LiteLLM Gateway | GET user gateway `/v1/models`, then optional small completion against selected/default model |
| OpenAI Direct | small completion against recommended model or user-selected model |
| Anthropic Direct | small Messages API request through `AnthropicClient` |
| Gemini Direct | small `generateContent` request through `GeminiClient` |
| DeepSeek Direct | small chat completion through `OpenAICompatibleClient(provider="deepseek")` |

Errors must be surfaced as service-level health, not hidden as generic form failure.

### 6.4 OpenConnector connection behavior

Gateway test:

1. `GET /v1/health` with runtime token.
2. `GET /v1/providers`.
3. `GET /v1/actions`.
4. If admin token is configured, `GET /api/connections` for admin surface readiness.

Provider connection setup:

1. API key/custom credential: Hive accepts secret once, validates user/admin authority, forwards to OpenConnector `/api/connections/:service`, then stores only Hive connection row + OpenConnector alias/profile/status.
2. OAuth: Hive starts OpenConnector authorization with a server-generated `connectionName`, returns only `authorizationUrl` to the browser, and later verifies profile/status through OpenConnector.
3. No-auth provider: Hive creates a virtual `ConnectorConnection` with `owner_type="tenant"` or `owner_type="agent"` depending on the assignment path.

Execution connection resolution:

1. If action policy requires user-owned connection, a runtime without current user context returns `needs_connection` instead of falling back silently.
2. If action policy allows agent-owned connection, autonomous tasks may use it after explicit assignment.
3. Tenant service account is never default fallback unless `allow_tenant_service_account=true` on that action assignment.

## 7. Catalog sync rules

### 7.1 LiteLLM Gateway

Use in order:

1. Hive Managed Gateway and BYOK Gateway Relay use Hive-managed LiteLLM.
2. Self-hosted Gateway uses the user's supplied Gateway base URL.
3. `/v1/models` for available model IDs.
4. `/model/info` when available for richer metadata.
5. Existing LiteLLM cost map fields when returned.

If `/model/info` is unavailable, sync should still succeed with basic model IDs and `raw_metadata`.

### 7.2 Direct providers

Direct provider catalog can start from `ProviderSpec.recommended_models` plus optional official model list integration later.

Do not block the redesign on perfect live catalog support for every provider. The key improvement is the service/model separation and direct/gateway runtime boundary.

### 7.3 Manual model addition

Every service should allow manual model ID addition:

1. User enters model ID.
2. Hive probes it through that service's runtime path.
3. On success, Hive creates a catalog row with `source="manual_probe"`.
4. User can publish it to `llm_models`.

This is required for private LiteLLM aliases, new models, and custom OpenAI-compatible endpoints.

### 7.4 OpenConnector catalog sync

Sync sources:

1. `GET /v1/providers` for provider/app catalog.
2. `GET /v1/actions` and `GET /v1/actions?service=<service>` for action catalog.
3. `GET /v1/actions/:actionId` for one-action detail.
4. `GET /api/actions/:actionId/agent.md` for agent-readable guidance/evidence, stored as admin/operator evidence and not blindly injected into prompt.

Sync rules:

1. `action_id` is stable identity.
2. `provider_key + action_id` must be unique per tenant/gateway.
3. Required scopes and provider permissions are catalog metadata; execution still checks actual connection profile where available.
4. Missing action on next sync marks `retired`, not hard-deleted.
5. Schema changes update metadata fingerprint and may require admin review before runtime exposure.

## 8. Migration strategy

### 8.1 Backfill existing records

Existing `llm_models` should be preserved. Migration groups models by:

```text
tenant_id + provider + base_url + api_key_encrypted
```

For each group:

1. Create an `llm_service_connections` row.
2. Infer `service_type`:
   - `provider=anthropic` -> `anthropic_direct`
   - `provider=gemini` -> `gemini_direct`
   - `provider=deepseek` -> `deepseek_direct`
   - `provider=openai` / `openai-response` -> `openai_direct`
   - unknown OpenAI-compatible provider -> `custom_openai_compatible`
3. Attach existing `llm_models.service_connection_id`.
4. Mark existing models as `source_kind="legacy_manual"`.

No agent model references are rewritten.

### 8.2 Default service seed

For tenants with no models:

1. Show LiteLLM Gateway as recommended unconfigured card.
2. Show OpenAI / Anthropic / Gemini / DeepSeek direct cards as unconfigured.
3. Do not auto-create fake DB rows until a user saves a service.

For tenants with existing models:

1. Show inferred direct/custom services.
2. Keep all existing models in the Published Models list.
3. Allow users to sync catalog for each inferred service.

### 8.3 Deletion semantics

Deleting a service should be blocked if it has published models referenced by agents, unless user explicitly force-deletes and accepts the same consequences as deleting model rows today.

Safer default:

1. Disable service.
2. Keep published models visible but unhealthy.
3. Agents fail with clear configuration error if selected model is disabled/unhealthy.

### 8.4 Connector migration and deletion semantics

No existing Hive connector data should be silently migrated into OpenConnector. Existing MCP/custom API connectors remain as-is.

Initial OpenConnector rollout:

1. Seed `open_connector` as an installable plugin projection.
2. Admin installs plugin and creates `ConnectorGateway`.
3. Catalog sync creates `ConnectorProvider` and `ConnectorAction`.
4. Agent Detail action selection creates `AgentConnectorActionAssignment`.
5. User/agent/admin connection flow creates `ConnectorConnection`.

Deleting an OpenConnector gateway should be blocked while it has active connections or enabled agent action assignments. Safer default:

1. Disable gateway.
2. Mark actions unavailable.
3. Keep connection records for audit/profile without exposing secrets.
4. Runtime returns typed configuration error.

Deleting a connection should not delete action assignments. It should make affected assignments show `needs_connection`.

## 9. Frontend design brief

### 9.1 Primary user action

The primary action is: configure a model service once, then publish one or more models from its catalog to Hive.

### 9.2 Layout

Use a two-pane operational layout instead of a large add-model form:

```text
┌──────────────────────────────┬──────────────────────────────────────────────┐
│ Model Services               │ Selected Service                             │
│ Search services              │ Header: LiteLLM Gateway  Recommended  Enabled│
│                              │ API Key / Base URL / Test / Sync             │
│ LiteLLM Gateway              │ Catalog status                               │
│ OpenAI Direct                │ Published models from this service           │
│ Anthropic Direct             │                                              │
│ Gemini Direct                │                                              │
│ DeepSeek Direct              │                                              │
└──────────────────────────────┴──────────────────────────────────────────────┘
```

Catalog opens as a modal or drawer:

```text
Search model ID or name
[All] [Reasoning] [Vision] [Tools] [Embedding] [Rerank] [Free]

anthropic (12)
  Claude Sonnet ...      badges      +
  Claude Opus ...        badges      +

google (33)
  Gemini ...             badges      +
```

### 9.3 States

Required states:

1. No service configured.
2. Service configured, never tested.
3. Service test failed.
4. Service healthy, catalog never synced.
5. Catalog sync loading.
6. Catalog sync partial failure.
7. Catalog empty.
8. Catalog has retired models.
9. Publishing duplicate model.
10. Published model used by agents.
11. Service disabled while models remain published.

### 9.4 Copy rules

Use “模型服务” for connections and “Hive 模型池” for published runtime models.

Avoid calling LiteLLM models “Anthropic direct” or “Gemini direct” even if upstream family is Anthropic/Gemini. Use copy like:

```text
来源：LiteLLM Gateway / upstream: Anthropic
```

Direct models can say:

```text
来源：Anthropic Direct
```

### 9.5 Connector UI layout

Company Settings / Tools keeps tenant-wide installation and policy:

```text
┌──────────────────────────────┬──────────────────────────────────────────────┐
│ Extensions & Add-ons         │ OpenConnector                                │
│ Search plugins               │ Gateway status: OK                           │
│                              │ Runtime URL / Test / Sync catalog            │
│ OpenConnector                │ Providers                                    │
│ MCP Servers                  │ Actions                                      │
│ Custom APIs                  │ Tenant service accounts                      │
└──────────────────────────────┴──────────────────────────────────────────────┘
```

Agent Detail / Extensions keeps personalization:

```text
OpenConnector
  Plugin enabled
  Apps
    Gmail        connected as: Example Owner / Agent Inbox / none
      search_messages      auto
      send_message         approval
      delete_message       deny
    GitHub       connected as: github-user
      get_current_user     auto
      create_issue         approval
```

Required connector states:

1. Gateway not installed.
2. Gateway installed but unhealthy.
3. Catalog never synced.
4. Provider available but no connection.
5. User-owned connection connected.
6. Agent-owned connection connected.
7. Tenant service account available but not allowed for this Agent.
8. Action disabled by workspace policy.
9. Action requires approval.
10. OAuth expired / reconnect required.
11. Connection exists but required scopes are missing.

## 10. Implementation plan

### 10.1 Backend

1. Add models and migration:
   - `LLMServiceConnection`
   - `LLMServiceCatalogModel`
   - nullable references on `LLMModel`
2. Add schemas:
   - service create/update/out
   - catalog model out
   - publish request/out
3. Add service layer:
   - `llm_service_connections.py`
   - connection test
   - catalog sync
   - publish models
4. Add `litellm_gateway` `ProviderSpec`.
5. Keep `create_llm_client()` contract unchanged.
6. Backfill existing `llm_models`.

Connector Gateway backend:

1. Add models and migration:
   - `ConnectorGateway`
   - `ConnectorProvider`
   - `ConnectorAction`
   - `ConnectorConnection`
   - `AgentConnectorActionAssignment`
2. Add OpenConnector client:
   - runtime API calls
   - admin API calls
   - redaction and error normalization
3. Add plugin seed/materialization:
   - `open_connector` installed plugin projection
   - agent plugin assignment compatibility
4. Add catalog sync:
   - providers
   - actions
   - schema fingerprint/review status
5. Add connection flows:
   - API key/custom credential setup
   - OAuth start/callback verification
   - profile/status sync
6. Add runtime handler:
   - `Tool(type="open_connector")` or equivalent dynamic runtime registration
   - credential argument rejection
   - server-side alias resolution
   - approval/gate integration
   - connector ACL source registration

### 10.2 Frontend

1. Replace `WorkspaceLlmSection` form-first UI with service-first UI.
2. Add service list/search.
3. Add service detail editor.
4. Add catalog modal/drawer.
5. Add published model pool list.
6. Move advanced runtime knobs into model detail drawer.
7. Update i18n `en.json` / `zh.json`.

Connector Gateway frontend:

1. Add OpenConnector plugin management panel in Workspace Tools / Extensions.
2. Add gateway config/test/sync UI.
3. Add provider/action catalog browser.
4. Add tenant service account connection UI.
5. Extend Agent Detail `ToolsManager` to expand plugin-contributed apps/actions.
6. Add user-owned and agent-owned connection flows.
7. Add action policy controls: auto / approval / deny.
8. Add `needs_connection`, `reconnect`, `missing_scopes`, `disabled_by_policy` states.

### 10.3 Compatibility

1. Existing agents keep their `primary_model_id` / `fallback_model_id`.
2. `GET /enterprise/llm-models` response remains compatible.
3. Existing memory/eval model dropdowns continue reading published `llm_models`.
4. Direct manual creation remains available behind an advanced action.

## 11. Test plan

Documentation-only work does not require TDD. Implementation must be TDD-first.

### 11.1 Backend red tests

Before implementation, add failing tests for:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest tests/services/test_llm_service_connections.py -q
pytest tests/api/test_enterprise_llm_services.py -q
pytest tests/migrations/test_llm_service_connection_migration.py -q
pytest tests/services/test_llm_client_gateway_provider.py -q
```

Required cases:

1. `litellm_gateway` creates an OpenAI-compatible client and does not route as Anthropic/Gemini.
2. Anthropic Direct still creates `AnthropicClient`.
3. Gemini Direct still creates `GeminiClient`.
4. DeepSeek Direct still applies DeepSeek reasoning kwargs and temperature omission.
5. Hive Managed Gateway can publish a model without a tenant-provided provider key.
6. BYOK Direct stores a tenant official provider key and does not register it into LiteLLM.
7. BYOK Gateway Relay stores a tenant long-tail provider key and registers/probes it through Hive-managed LiteLLM.
8. Self-hosted Gateway stores a user gateway virtual key and never asks for upstream provider keys.
9. Existing `llm_models` backfill into service connections without changing model IDs.
10. Agent `primary_model_id` remains valid after migration.
11. Service deletion is blocked while referenced published models exist.
12. LiteLLM catalog sync handles `/v1/models` only and `/model/info` enriched responses.
13. Manual model publish probes through the selected service provider.

Connector Gateway implementation must add failing tests first:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest tests/services/test_open_connector_gateway.py -q
pytest tests/services/test_open_connector_identity_resolution.py -q
pytest tests/api/test_open_connector_connections.py -q
pytest tests/services/test_open_connector_action_runtime.py -q
```

Required connector cases:

1. Agent cannot execute OpenConnector action when `open_connector` plugin assignment is disabled.
2. Agent cannot execute action that is not assigned to that Agent.
3. Agent assigned `github.get_current_user` does not receive other GitHub actions by implication.
4. User-owned connection requires current user context and never falls back to tenant service account silently.
5. Agent-owned connection can be used by autonomous task only when action assignment allows it.
6. Tenant service account requires explicit assignment policy.
7. LLM-supplied `api_key`, `access_token`, `authorization`, `connection_alias`, and `x-oo-connector-alias` arguments are rejected.
8. Runtime sends OpenConnector alias only from server-resolved `ConnectorConnection`.
9. OAuth start stores a pending connection and returns only `authorizationUrl`.
10. API key setup forwards secret once and stores only masked/encrypted alias/profile metadata in Hive.
11. Catalog sync retires missing actions without deleting existing assignments.
12. OpenConnector run result registers connector source/ACL metadata before prompt ingress.

### 11.2 Frontend red tests

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- WorkspaceLlmSection
```

Required cases:

1. Hive Managed Gateway is shown as the no-key recommended path.
2. LiteLLM Gateway/Self-hosted Gateway is shown as an advanced gateway path.
3. Direct services are shown separately from Gateway.
4. Adding a LiteLLM catalog model posts `provider=litellm_gateway`.
5. Adding Anthropic Direct posts `provider=anthropic`.
6. MiniMax BYOK asks for MiniMax official API key, not LiteLLM key.
7. Self-hosted Gateway asks for Gateway base URL and Gateway virtual key.
8. Advanced reasoning controls are hidden in the main add flow.
9. Advanced drawer shows provider-specific controls only for matching runtime provider.
10. Catalog filters by reasoning/vision/tools/free.
11. Duplicate published model shows idempotent/clear feedback.

Connector Gateway frontend tests:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- ToolsManager
npm test -- WorkspaceToolsSection
```

Required connector UI cases:

1. Workspace Tools shows OpenConnector as a plugin-backed connector Gateway.
2. Agent Detail shows OpenConnector plugin as expandable apps/actions, not only a global toggle.
3. Enabling plugin does not visually enable all actions.
4. Provider with no connection shows `Connect`.
5. User-owned connection option starts OAuth/API key flow from Agent Detail.
6. Tenant service account is visible only as an allowed option, not default fallback.
7. Action policy selector persists auto/approval/deny.
8. Missing scopes/reconnect states are shown without exposing raw token or alias.

### 11.3 Regression suite

After implementation:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_llm_client_streaming.py \
  tests/services/test_llm_reasoning_adapter.py \
  tests/services/test_llm_client_from_config.py \
  tests/services/test_llm_client_token_limits.py \
  tests/kernel/test_engine.py -q
```

And frontend:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run build
```

## 12. Non-goals

1. Do not replace Hive Kernel.
2. Do not remove existing `LLMClient` subclasses.
3. Do not route all direct services through LiteLLM.
4. Do not claim Gateway models have native provider fidelity.
5. Do not require perfect live catalog for every direct provider in the first implementation.
6. Do not migrate agent model references away from `llm_models.id` in this redesign.
7. Do not expose raw `provider_options` JSON in the primary user flow.
8. Do not replace all existing MCP/custom API connectors with OpenConnector in one migration.
9. Do not expose OpenConnector admin/runtime tokens to browser code or LLM tool arguments.
10. Do not make OpenConnector plugin enablement equivalent to all-action authorization.

## 13. Open questions

Resolved by discussion:

1. Hive should support both centralized managed usage and BYOK usage.
2. No-key users use Hive Managed Gateway; Hive owns upstream provider keys and exposes Hive quota/API access.
3. OpenAI / Anthropic / Gemini / DeepSeek official keys use BYOK Direct and preserve native adapters.
4. Long-tail official provider keys, such as MiniMax, use BYOK Gateway Relay through Hive-managed LiteLLM.
5. User-supplied LiteLLM URL + virtual key is only the Self-hosted Gateway advanced path.
6. OpenConnector should be delivered as a plugin-backed connector Gateway.
7. OpenConnector plugin install belongs to Workspace/Company Settings; app/action personalization belongs to Agent Detail.
8. OpenConnector connection identity must support user-owned, agent-owned, and tenant service-account connections.

Remaining open questions:

1. Should direct services support multiple API keys per tenant for quota separation, or one official connection per provider in the first implementation?
2. Should published Gateway models be grouped in Agent model dropdown by upstream family or by source service?
3. Should LiteLLM spend/budget data be mirrored into Hive observability, or linked out to LiteLLM admin UI for the first pass?
4. Should catalog sync be synchronous on click only, or also scheduled background refresh?
5. Should production OpenConnector runtime be one runtime per tenant, or a shared runtime only after tenant isolation is externally proven?
6. Should OAuth callback be proxied through Hive for uniform audit, or can OpenConnector callback be public with Hive polling verification?
7. Should OpenConnector action metadata review reuse MCP metadata trust tables or have connector-specific review tables?

## 14. Proposed first implementation boundary

The complete first implementation should include:

1. Service connection schema and migration with backfill.
2. `ownership_mode` support for Hive Managed Gateway, BYOK Direct, BYOK Gateway Relay, and Self-hosted Gateway.
3. LiteLLM Gateway provider spec and connection test.
4. OpenAI / Anthropic / Gemini / DeepSeek direct service cards.
5. LiteLLM `/v1/models` catalog sync.
6. Manual model addition for all service types.
7. Publish selected catalog models into existing `llm_models`.
8. New service-first UI with catalog modal and published model pool.
9. Advanced model settings drawer.
10. Backend/frontend regression coverage listed above.

Connector Gateway first implementation should include:

1. `open_connector` plugin seed and install path.
2. `ConnectorGateway` + catalog/action/connection/assignment schema and migration.
3. OpenConnector runtime/admin client.
4. Gateway test and provider/action catalog sync.
5. Agent Detail app/action selection under OpenConnector plugin.
6. User-owned, agent-owned, and tenant service-account connection flow.
7. Runtime action handler with server-side alias resolution and credential argument rejection.
8. Approval/gate integration for action mode.
9. Connector source/ACL registration before prompt ingress.
10. Backend/frontend tests listed above.

It should not ship as a UI-only facelift. The product problem is data-shape and runtime-boundary driven; a front-end-only implementation would keep the current duplicated service/account model hidden under a nicer shell.

For connector Gateway, it also should not ship as “just import OpenConnector MCP server”. That would create a powerful generic execution surface without Hive-native per-action ownership, approval, and audit semantics.
