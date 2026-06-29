# External Capability Trust Gate Plan

日期：2026-06-26
状态：设计计划，待实现
范围：外部 Skill、MCP Server、Plugin 的发现、检验、审批、安装、激活、撤销与审计
上位文档：
- `docs/cc-tooling-alignment-and-plugin-system.md`
- `docs/agent-extension-surface-skill-mcp.md`
- `docs/ccplus-v1-deep-verification-reconciliation-2026-06-24.md`
- `docs/org-agent-asset-rights-model.md`

## 0. 决策摘要

Hive 不需要做一个独立的 Skill 广场。Hive 需要做的是一个统一的外部能力安装门：

```text
External Capability Trust Gate
```

用户和 agent 可以从 GitHub、skills.sh、ClawHub、Smithery、MCP server registry、私有 repo、本地上传、plugin source 等地方发现和拉取能力，但这些来源本身不直接成为运行时信任根。Hive 的信任对象必须是经过检验、审批、固定 hash、可回放审计的安装快照。

核心原则：

```text
允许收集和下载
禁止未经检验的激活
禁止未经隔离的 install-time code
禁止外部来源直接改变 agent 运行面
```

因此，“安装”必须拆成三层：

| 层 | 含义 | 是否允许未审批 |
|---|---|---|
| `materialize` | 从外部来源拉取、解包、运行必要 installer，产出待审包 | 可以，但必须在 sandbox / quarantine |
| `approve` | 检验来源、内容、权限、脚本、依赖、凭据、风险等级 | 必须 |
| `activate` | 写入 agent workspace / tenant connector / plugin assignment，让运行时可见 | 只有通过 Trust Gate 后允许 |

## 1. 术语

| 术语 | 定义 |
|---|---|
| External Capability | 外部能力总称，包括 Skill、MCP Server、Plugin。 |
| Source | 能力来自哪里，例如 GitHub URL、skills.sh ref、ClawHub slug、Smithery server id、plugin source。Source 是证据，不是信任。 |
| Materialized Package | 在隔离环境里拉取或生成出的原始包。可能包含 `SKILL.md`、scripts、templates、assets、MCP manifest、plugin manifest、依赖锁等。 |
| Quarantine / Staging | 未获批准前的隔离区。这里可以存证、分析、展示，但不能进入运行时。 |
| Trust Review | 平台生成的审查报告，包含 static scan、source provenance、permission analysis、dependency scan、sandbox smoke test、人工审批记录。 |
| Trusted Snapshot | 通过审查后的不可变快照，带 `content_sha256`、source ref、review id、approver、approved_at。后续安装以 snapshot 为准，不以外部 latest 为准。 |
| Activation | 将可信快照应用到 agent 或 tenant，使运行时可见。 |
| Revocation | 撤销某个 trusted snapshot 或 connector/plugin install，阻止新 agent 使用，并可选禁用已安装实例。 |

不要把这个机制叫“信任源”。“信任源”容易暗示某个外部站点永远可信。更准确的命名是：

```text
Trust Gate = 审查门
Trusted Snapshot = 审查通过的可信快照
Source = 可追溯来源
```

## 2. 为什么必须统一

当前 Skill、MCP、Plugin 已经有各自的安装路径，但缺统一安装门：

- Skill：已有 `SkillGuard` 和统一的 `skill-marketplace` 发现/审查入口；外部 Skill 仍必须通过受治理的 active `skills/` 写入路径。
- MCP：已有 server-first 管理、per-agent assignment、tool policy，但导入、凭据、连接测试、工具暴露、撤销记录需要统一到外部能力总账。
- Plugin：`pack.yaml` / `TenantInstalledPlugin` / `AgentPluginAssignment` 已经形成插件系统方向，但远程 source 仍应在签名、hash、sandbox materializer、lockfile provenance 未完成时 fail-closed。

问题不是“能不能装”。问题是：

```text
装之前是否知道它来自哪里
是否完整读过它会带来什么
是否在隔离环境里 materialize
是否把安装时代码和运行时代码分开
是否有人工/策略审批
是否能撤销和回放
```

同一服务器里混入恶意脚本的风险是平台级风险。外部 Skill 不是普通 Markdown；它可以携带脚本、模板、依赖、工具提示、网络调用说明，甚至诱导 agent 执行危险命令。MCP 和 Plugin 更高风险，因为它们可能直接引入外部工具面、凭据面、hook 或 dependency。

## 3. 统一产品流程

用户视角必须清楚记录三件事：

1. 这个能力从哪里来。
2. Hive 如何检验它。
3. 它如何被安装并进入运行时。

### 3.1 发现

允许的发现来源：

| 类型 | 示例 | 备注 |
|---|---|---|
| GitHub URL | `https://github.com/org/repo/tree/main/skills/foo` | 必须解析 owner/repo/ref/path。推荐 pin commit/tag。 |
| skills.sh ref | `owner/repo@skill` | 只能在 sandbox materializer 中运行 `npx skills add`。 |
| ClawHub slug | `market-research-agent` | ClawHub 是 source registry，不是 Hive trust root。 |
| MCP registry | Smithery server id、GitHub MCP server repo、private endpoint | 必须记录 server identity、transport、auth mode、tool/resource list。 |
| Plugin source | builtin/local/git/url/npm/pip | 远程 source 必须有 signature/hash/sandbox materializer；未达标时只识别不激活。 |
| Local upload | ZIP / folder | 仍必须走相同 scan 和 approval。 |

发现阶段输出：

```json
{
  "source_kind": "github|skills_sh|clawhub|mcp_registry|plugin_source|local_upload",
  "source_ref": "...",
  "resolved_ref": "commit/tag/version/server_id",
  "display_name": "...",
  "requested_by": "...",
  "target_scope": "tenant|agent",
  "target_agent_ids": []
}
```

### 3.2 Materialize

Materialize 是唯一允许执行 install-time 命令的阶段。

规则：

- 必须使用 ephemeral sandbox。
- 不继承后端宿主机 secrets。
- 默认网络 deny。
- 需要网络时使用 allowlist，例如 `github.com`、`api.github.com`、`registry.npmjs.org`、`skills.sh`、指定 MCP registry。
- 只回收声明的输出目录，不回收整个 HOME。
- 输出只能进入 quarantine/staging。
- 不允许 materialize 阶段直接写 active `skills/`、tenant MCP assignment、plugin assignment。

Skill materializer 示例：

```text
npx skills add owner/repo@skill -y
```

运行要求：

```text
runtime=node24
network_policy=allowlist:github.com,api.github.com,registry.npmjs.org,skills.sh
HOME=<ephemeral sandbox home>
output=$HOME/.agents/skills/<skill>
```

MCP materializer 示例：

```text
resolve server manifest -> fetch metadata -> test transport -> list tools/resources in sandbox or controlled probe
```

Plugin materializer 示例：

```text
fetch source -> validate manifest -> resolve dependencies -> generate lockfile -> sandbox unpack/build if needed
```

### 3.3 Trust Review

Trust Review 是统一审批门。所有外部能力都必须输出同一类 review record。

最低检查项：

| 检查 | Skill | MCP | Plugin |
|---|---:|---:|---:|
| Source provenance | 必须 | 必须 | 必须 |
| Content hash / lockfile | 必须 | 必须 | 必须 |
| Path / archive safety | 必须 | 可选 | 必须 |
| Secret scan | 必须 | 必须 | 必须 |
| Script / command scan | 必须 | 可选 | 必须 |
| Dependency scan | 有依赖时必须 | server package 有依赖时必须 | 必须 |
| Network endpoint analysis | 必须 | 必须 | 必须 |
| Credential scope analysis | 必须 | 必须 | 必须 |
| Tool/resource surface diff | declared tools | exposed MCP tools/resources | owned/required/optional tools/hooks/MCP/skills |
| Sandbox smoke test | 脚本存在时必须 | 必须 | 必须 |
| Human/admin approval | medium+ 必须 | 必须 | 必须 |

风险等级：

| 等级 | 含义 | 默认动作 |
|---|---|---|
| LOW | 纯说明/模板，trusted source，无脚本或脚本只读且 sandbox pass | 用户确认可激活 |
| MEDIUM | 写 workspace、调用已知 API、包含脚本但范围清楚 | 用户确认或管理员确认 |
| HIGH | 需要凭据、外部发送、生产系统、支付/金融/邮件等敏感面 | 管理员确认 |
| EXTREME | credential theft、exfiltration、混淆执行、root、写 memory/soul、逃逸路径、未知 binary | 不激活，只 quarantine |

`EXTREME` 不应开放普通用户 override。它可以进入安全审计，但不能进入 active runtime。

### 3.4 Approval

审批不是一句“同意安装”。审批必须绑定 review record 和目标范围：

```json
{
  "review_id": "...",
  "decision": "approved|rejected|needs_changes",
  "approved_scope": {
    "tenant_id": "...",
    "agent_ids": ["..."],
    "allowed_capabilities": ["file_read", "file_write"],
    "network_allowlist": ["api.example.com"],
    "credential_handles": ["cred://tenant/foo"]
  },
  "approver_id": "...",
  "approval_reason": "...",
  "expires_at": null
}
```

### 3.5 Activation

Activation 只接受 approved Trusted Snapshot。

Skill activation：

```text
TrustedSkillSnapshot -> agent workspace skills/<folder>/...
```

MCP activation：

```text
TrustedMCPInstall -> MCPServer row -> AgentMCPServerAssignment -> per-tool policy
```

Plugin activation：

```text
TrustedPluginSnapshot -> TenantInstalledPlugin -> AgentPluginAssignment -> hook/dependency/tool visibility registration
```

Activation 必须记录：

- source ref
- resolved ref / content hash
- review id
- approver
- target tenant / agents
- files written or server/plugin records created
- rollback / revoke ref

## 4. 三类能力的具体规则

### 4.1 Skill

Skill 是能力胶囊，不是普通 Markdown。

允许包含：

- `SKILL.md`
- `references/`
- `templates/`
- `assets/`
- `scripts/`
- `evals/`
- `agents/`
- workflow/subagent/component references

但规则是：

```text
packaging is not execution
```

安装 Skill 不等于执行脚本。`load_skill` 只加载指导上下文。任何 `scripts/` 后续执行都必须通过 governed code execution provider。

必须统一改造的入口：

- `/skills/import-from-url`
- `/skills/clawhub/install`
- `/agents/{agent_id}/files/import-from-url`
- `/agents/{agent_id}/files/import-from-clawhub`
- `/agents/{agent_id}/files/import-skill`
- `create_digital_employee.external_skill_refs`
- `execute_code` 收割 `$HOME/.agents/skills`

目标行为：

```text
旧：fetch -> SkillGuard -> write active skills/
新：fetch/materialize -> stage -> Trust Review -> approval -> write active skills/
```

`SkillGuard` 继续保留，但降级为 Trust Review 的一个 static scanner，不再作为唯一门。

### 4.2 MCP

MCP 是连接资产，风险重点不只是包内容，而是：

- server endpoint 是否可信
- transport 是否安全
- auth mode 是否合规
- 是否要求 token passthrough
- 是否暴露过宽 tool/resource surface
- agent 是否只能使用被 assignment 允许的 server
- per-tool policy 是否被 runtime 强制执行

MCP install 必须记录：

```json
{
  "server_key": "...",
  "server_url": "...",
  "transport": "stdio|http|sse",
  "auth_mode": "none|oauth|api_key|managed_secret",
  "credential_policy": "brokered_handle_only",
  "tools": [],
  "resources": [],
  "default_tool_mode": "deny|approval|auto",
  "review_id": "...",
  "content_or_manifest_hash": "..."
}
```

MCP 的 Trust Review 必须包括：

- 禁 token passthrough。
- 禁 URL userinfo。
- 凭据必须经 secrets provider / brokered credential handle。
- 工具名必须 canonical 化，防撞名。
- 默认 tool mode 推荐 `deny` 或 `approval`，管理员显式放开。
- 列出所有 tools/resources，安装前显示 diff。
- agent 自助安装也必须创建 server-first record，不允许只创建 legacy tool rows。

### 4.3 Plugin

Plugin 是 Skill 和 MCP 之上的组合单元。它可以包含：

- tools
- skills
- MCP servers
- hooks
- agents/subagents
- workflow definitions
- dependencies
- credential requirements
- sandbox requirements

Plugin 的风险最高，因为它可能同时改变工具面、hook、MCP、skill、凭据和依赖。

规则：

- `pack.yaml` / plugin manifest 是 install/composition 真相源，不是 tool schema 真相源。
- tool executable schema 仍由 `@tool(ToolMeta)` 拥有。
- 远程 plugin source 在 signature/hash/sandbox materializer 未完成前 fail-closed。
- hooks 只能引用平台 allowlist handler。
- PRE_TOOL_USE enforce hook 必须管理员显式审批。
- dependencies 必须 pinned，并写入 lockfile。
- plugin activation 只改变可见性和组合，不绕过 runtime governance。

### 4.4 CC / Codex 插件兼容目标

这一层要单独写清楚，因为“能读格式”和“能无缝执行全部能力”不是一回事。

当前结论：

1. Hive 当前插件系统和 CC 插件系统不一致，不能直接宣称“已经兼容并可安装 CC 全部能力”。
2. Hive 当前插件系统和 Codex 插件系统也不一致，不能把 Codex `.codex-plugin/plugin.json` 当成 Hive `pack.yaml` 直接落库。
3. 可以把 CC/Codex 插件都纳入同一个 Trust Gate，通过 adapter 转成 Hive 的 normalized manifest，再按组件逐项激活。
4. 兼容目标应写成 capability compatibility level，而不是二元“支持/不支持”。

#### 4.4.1 CC 插件真实形态

CC 插件是一个本地 CLI 语义的组合容器，通常由 marketplace 发现、物化、缓存，再加载插件目录。

CC marketplace 支持多种 marketplace source：

- `url`
- `github`
- `git`
- `npm`
- `file`
- `directory`
- `settings`

CC marketplace entry 里的 plugin source 又支持：

- marketplace 内相对路径
- `npm`
- `pip`
- `url` / git URL
- `github`
- `git-subdir`

CC plugin manifest 不是只包含 Skill。它可以声明：

- `commands`
- `agents`
- `skills`
- `hooks`
- `outputStyles`
- `mcpServers`
- `lspServers`
- `channels`
- `settings`
- `userConfig`
- metadata / dependencies

所以 CC plugin 的兼容必须拆开判断：

| CC component | Hive 目标映射 | 当前兼容判断 |
|---|---|---|
| `skills` / `SKILL.md` | Trusted Skill Snapshot -> agent `skills/` | 高，可通过 Trust Gate + SkillGuard 激活。 |
| `mcpServers` / `.mcp.json` / MCPB | Trusted MCP Install -> `MCPServer` + assignment + tool policy | 中高，需要 authz、tool/resource diff、默认 approval/deny。 |
| `commands` | prompt command 可降级为 Skill/command capsule；local JSX/local command 不直接执行 | 部分，不能无脑兼容。 |
| `agents` | tenant-scoped subagent definition | 部分，需要 agent schema adapter 和 governance。 |
| `hooks` | `PluginHookRegistration` allowlist handler | 低，CC raw shell hook 不能直接在 Hive 多租户服务器执行。 |
| `outputStyles` | prompt/UI style metadata | 低，可保存但默认不激活运行时行为。 |
| `lspServers` | 暂无等价 server runtime | 不激活，只能 stage/analyze。 |
| `userConfig` / `channels` | credential requirements + MCP/channel config prompts | 部分，需要凭据和 channel adapter。 |

关键边界：

- CC 插件格式可以作为输入格式兼容。
- CC 插件行为不能直接作为运行时行为兼容。
- 任何 CC raw command、raw hook、npm/pip install、LSP server 都必须先 stage/analyze，不允许在后端宿主机 install-time 执行。

#### 4.4.2 Codex 插件真实形态

Codex 插件不是 CC `plugin.json` 的完全同构版本。

Codex 默认插件 manifest 路径：

- `.codex-plugin/plugin.json`
- 也会识别 `.claude-plugin/plugin.json` 作为 alternate discoverable manifest path

Codex marketplace manifest 路径：

- `.agents/plugins/marketplace.json`
- `.agents/plugins/api_marketplace.json`
- `.claude-plugin/marketplace.json`

Codex plugin manifest 主要字段：

- `name`
- `version`
- `description`
- `keywords`
- `skills`
- `mcpServers`
- `apps`
- `hooks`
- `interface`

Codex 当前实装的 curated plugin 常见形态是：

- `skills: "./skills/"`
- `mcpServers: "./.mcp.json"` 或 inline object
- `apps: "./.app.json"`
- `interface` 提供 displayName、description、developerName、category、capabilities、logo、defaultPrompt 等 UI/model-facing metadata

因此 Codex plugin 对 Hive 的兼容路径比 CC 更窄、更接近 Skill/MCP/App connector bundle：

| Codex component | Hive 目标映射 | 当前兼容判断 |
|---|---|---|
| `skills` | Trusted Skill Snapshot | 高。 |
| `mcpServers` | Trusted MCP Install | 中高。 |
| `apps` | connector/app capability registration | 部分，取决于 Hive 是否已有对应 connector/app runtime。 |
| `hooks` | allowlisted `PluginHookRegistration` | 低到中，禁止 raw execution。 |
| `interface` | catalog/review metadata | 高，只做 metadata。 |

Codex 的 `.claude-plugin/plugin.json` 兼容入口说明它能读一部分 CC-style manifest location，但这不等于 Hive 可以跳过自己的 Trust Gate，也不等于 Codex/CC manifest 字段完全一致。

#### 4.4.3 Normalized Plugin Manifest

Hive 不应该为 CC 和 Codex 各建一套运行时。应新增 adapter，把外部格式统一成一个 internal normalized manifest：

```text
NormalizedExternalPluginManifest
  manifest_format: hive_pack | cc_plugin | codex_plugin
  source_kind: cc_marketplace | codex_marketplace | openai_plugin | github | git | url | npm | pip | local_upload | builtin | local
  source_ref
  resolved_ref
  content_sha256
  display_name
  version
  components:
    skills[]
    mcp_servers[]
    commands[]
    agents[]
    hooks[]
    apps[]
    lsp_servers[]
    output_styles[]
    dependencies[]
    credential_requirements[]
  unsupported_components[]
  compatibility_level: L0 | L1 | L2 | L3 | L4
  adapter_report_json
```

Adapter 列表：

| Adapter | 输入 | 输出 |
|---|---|---|
| `HivePackAdapter` | `pack.yaml` | normalized manifest |
| `CCPluginAdapter` | `.claude-plugin/plugin.json` + `marketplace.json` | normalized manifest |
| `CodexPluginAdapter` | `.codex-plugin/plugin.json` / `.claude-plugin/plugin.json` + Codex marketplace | normalized manifest |
| `SkillSourceAdapter` | GitHub URL / ClawHub / skills.sh | normalized Skill package |
| `MCPSourceAdapter` | Smithery / MCP registry / URL / manifest | normalized MCP install |

所有 adapter 只能产出 staged artifact 和 review report；不能直接 active install。

#### 4.4.4 Compatibility Levels

| Level | 定义 | 可对外承诺 |
|---|---|---|
| L0 Discover | 只识别 marketplace/plugin metadata，列出能力和来源 | “可发现、可审查”。 |
| L1 Stage | 能 materialize、hash、scan、生成 review | “可进入审批流程”。 |
| L2 Activate Compatible Subset | 只激活 Skill + MCP + metadata 等已映射子集 | “可安装兼容子集”。 |
| L3 Component Mapping | commands/agents/hooks/apps 也有 Hive 映射和治理 | “大部分能力可运行”。 |
| L4 Behavioral Parity | 与 CC/Codex 原运行时行为等价 | “完全兼容”。短期不承诺。 |

Hive 对 CC plugin 的初始目标应是 L2，不是 L4。

Hive 对 Codex plugin 的初始目标可以是 L2 到 L3：

- Skill/MCP/interface 先完整支持。
- apps 只在 Hive 有同名 connector runtime 时激活。
- hooks 只允许 allowlist handler。

#### 4.4.5 安装流程

用户选择 CC/Codex 插件时，流程必须是：

```text
select marketplace/source
-> materialize in quarantine
-> detect manifest_format
-> run adapter
-> build compatibility report
-> scan files/dependencies/scripts/hooks
-> smoke test compatible components
-> human/admin approval
-> activate mapped components only
```

UI 必须明确显示：

- 来源：哪个 marketplace、哪个 repo、哪个 ref、哪个 plugin id。
- 格式：CC plugin / Codex plugin / Hive pack / plain Skill。
- 兼容等级：L0-L4。
- 将被激活的组件：skills、MCP servers、apps、hooks 等。
- 不会被激活的组件：例如 raw hooks、LSP、local JSX commands。
- 需要人工配置的凭据。
- sandbox smoke test 结果。

#### 4.4.6 市场/来源策略

不需要自建 Skill 广场，但需要支持多来源：

| 能力类型 | 目标来源 | 安全策略 |
|---|---|---|
| MCP | Smithery / MCP registry / GitHub / private endpoint | server-first record + authz + tool/resource diff + default approval/deny。 |
| Skill | ClawHub + skills.sh + GitHub URL + local upload | materialize 后统一 SkillGuard + review + approved snapshot。 |
| Plugin | Hive pack + OpenAI/Codex plugin + CC plugin marketplace | adapter normalize + component compatibility report + Trust Gate。 |

市场不是 trust root。市场只回答“哪里有东西”；Hive Trust Gate 才回答“这个东西是否能装、装哪些部分、用什么权限运行”。

## 5. 数据模型提案

### 5.1 `external_capability_reviews`

统一 review 表，Skill/MCP/Plugin 共用。

```text
id
tenant_id
requested_by_user_id
requested_by_agent_id
capability_kind              skill | mcp_server | plugin
source_kind                  github | skills_sh | clawhub | smithery | mcp_registry | cc_marketplace | codex_marketplace | openai_plugin | plugin_source | local_upload
source_ref
resolved_ref
display_name
manifest_format              skill_package | mcp_manifest | hive_pack | cc_plugin | codex_plugin | unknown
status                       staged | analyzing | needs_review | approved | rejected | quarantined | active | revoked
risk_level                   low | medium | high | extreme
decision                     pending | approved | rejected
content_sha256
lockfile_json
materialization_json
scan_report_json
permission_report_json
compatibility_report_json
components_json
unsupported_components_json
adapter_report_json
smoke_test_report_json
review_report_json
approval_json
created_at
updated_at
approved_at
revoked_at
```

### 5.2 `trusted_capability_snapshots`

通过 review 的不可变快照。

```text
id
tenant_id
review_id
capability_kind
manifest_format
snapshot_key
version
content_sha256
source_ref
resolved_ref
artifact_path
manifest_json
policy_json
components_json
compatibility_level
created_at
revoked_at
```

### 5.3 现有表如何接入

| 现有表/服务 | 变化 |
|---|---|
| `Skill` / `SkillFile` | tenant registry skill 应由 trusted snapshot 创建或更新。 |
| agent workspace `skills/` | 只能由 approved snapshot 激活写入。 |
| `AgentCapabilityInstall` | 可保留为 agent 维度 install readiness/read model，但应挂 `review_id` / `snapshot_id`。 |
| `MCPServer` / `AgentMCPServerAssignment` | 由 approved MCP review 创建/启用。 |
| `TenantInstalledPlugin` / `AgentPluginAssignment` | 由 approved plugin snapshot 创建/启用。 |
| `PluginHookRegistration` | 只接受 approved plugin snapshot 里的 allowlisted hook。 |

## 6. API 提案

统一 API：

```text
POST /enterprise/external-capabilities/stage
GET  /enterprise/external-capabilities/reviews
GET  /enterprise/external-capabilities/reviews/{review_id}
POST /enterprise/external-capabilities/reviews/{review_id}/analyze
POST /enterprise/external-capabilities/reviews/{review_id}/approve
POST /enterprise/external-capabilities/reviews/{review_id}/reject
POST /enterprise/external-capabilities/reviews/{review_id}/activate
POST /enterprise/external-capabilities/reviews/{review_id}/revoke
```

Agent 入口可以保留，但必须调用统一服务：

```text
POST /agents/{agent_id}/skills/import-from-url
POST /agents/{agent_id}/mcp-servers/import
POST /agents/{agent_id}/plugins/assign
```

这些 agent API 的行为应该是：

```text
如果没有 approved snapshot -> 返回 review_required + review_id
如果已有 approved snapshot -> 激活到该 agent
```

## 7. 实施切口

### Step 1：Trust Gate 服务骨架

新增：

```text
backend/app/services/external_capability_trust_gate.py
backend/app/models/external_capability_review.py
backend/app/api/external_capabilities.py
backend/tests/services/test_external_capability_trust_gate.py
backend/tests/api/test_external_capabilities_api.py
```

实现：

- stage review record
- write quarantine artifact
- run `SkillGuard` as one scanner
- compute `content_sha256`
- approve/reject state machine
- active install 只允许 approved review

### Step 2：Skill 入口改造

把所有 Skill import/harvest 路径改成：

```text
materialize -> stage -> analyze -> approve -> activate
```

先保留现有 UI，但后端返回：

```json
{
  "status": "review_required",
  "review_id": "...",
  "risk_level": "medium",
  "summary": "Skill contains scripts and needs approval before activation."
}
```

### Step 3：MCP 入口改造

把 MCP import、agent self-service import、registry import 统一到 review：

- source metadata
- auth mode
- server URL
- tools/resources diff
- default tool mode
- credential policy
- smoke test

### Step 4：Plugin 入口改造

远程 plugin source：

- 仍允许 stage/analyze。
- 不满足 signature/hash/sandbox materializer 前不允许 activate。

内置/本地 plugin：

- 必须过 manifest validator、dependency lock、hook allowlist、admin approval。

CC/Codex plugin source：

- 先 detect manifest path：`.claude-plugin/plugin.json`、`.codex-plugin/plugin.json`、`pack.yaml`。
- 通过对应 adapter 生成 normalized manifest。
- 默认只允许 L2 activation：Skill + MCP + safe metadata。
- raw hooks、LSP、local commands、install-time package scripts 默认进入 unsupported_components。
- 管理员审批时必须看到 compatibility report。

### Step 5：UI / Control Plane

不做 Skill 广场，只做审批队列：

```text
Workspace Settings -> External Capability Reviews
```

列表字段：

- kind
- name
- source
- requested by
- target agent(s)
- risk level
- status
- findings count
- created at
- approve/reject/activate/revoke

详情页必须展示：

- 从哪里找到
- 拉取/解析出了哪些文件或工具
- 检查了什么
- 风险是什么
- 凭据/网络/文件/命令权限
- 最终怎么安装
- 回滚/撤销路径

## 8. 测试和验收

文档变更不需要 TDD。实现时必须 TDD。

后端测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_external_capability_trust_gate.py \
  tests/api/test_external_capabilities_api.py \
  tests/services/test_skill_guard.py \
  tests/services/test_skill_installation.py \
  tests/services/test_command_tooling.py \
  tests/tools/test_hr_handler.py \
  tests/services/test_mcp_server_service.py \
  tests/api/test_mcp_servers_api.py \
  tests/services/test_plugin_install_service.py -q
```

必须覆盖：

- unsafe Skill package 只能进入 `quarantined`，不能 activate。
- clean Skill package 需要 approval 后才能写 agent `skills/`。
- `npx skills add` 只能在 sandbox materializer 中运行。
- `execute_code` 发现 `$HOME/.agents/skills` 不再直接 active install，而是创建 review。
- MCP import 必须列出 tool/resource surface，默认不自动全放开。
- MCP token passthrough / URL userinfo 被拒绝。
- plugin remote source 未签名/未锁定时 fail-closed。
- plugin hook 只能使用 allowlist handler。
- CC plugin adapter 必须把 skills/MCP 映射为可激活组件，把 raw hooks/LSP/local commands 标记为 unsupported。
- Codex plugin adapter 必须识别 `.codex-plugin/plugin.json` 和 `.claude-plugin/plugin.json`，并把 `skills`/`mcpServers`/`apps`/`interface` 写入 compatibility report。
- compatibility level L2 不得声称 full behavioral parity。
- revoked snapshot 不能被新 agent 激活。
- snapshot update 必须重新 review。

## 9. 完成定义

这项工作不能只做到“UI 上能安装”。完成定义必须是：

1. Skill、MCP、Plugin 三类外部能力都走同一 Trust Gate。
2. 所有外部来源都有 source provenance。
3. 所有 materialized artifact 都有 content hash。
4. 所有 active install 都能追到 review/approval。
5. 未审批能力不能改变 agent runtime surface。
6. scripts/hooks/dependencies 不会绕过 sandbox 和 governance。
7. revoke 能阻止新激活，并能禁用指定 agent 的现有激活。
8. UI 能向用户解释：从哪里来、检验了什么、如何安装、怎么撤销。
9. CC/Codex plugin 都能进入 stage/analyze；能激活的组件必须有 compatibility level 和 adapter report。
10. 不支持的 CC/Codex 子能力必须显式显示为 unsupported，而不是静默忽略。
11. targeted backend + frontend tests 通过。
12. 生产部署前跑一次 dry-run sweep，列出所有 legacy installed Skill/MCP/Plugin 的 backfill review status。

## 10. 非目标

- 不做公开 Skill 广场。
- 不自己运营第三方市场。
- 不把 ClawHub / skills.sh / GitHub 当 trust root。
- 不把 CC marketplace、Codex/OpenAI curated marketplace 当 trust root。
- 不承诺一开始就做到 CC plugin L4 behavioral parity。
- 不让 plugin manifest 重定义 tool executable schema。
- 不允许 install-time command 在后端宿主机裸跑。
- 不允许通过“用户坚持安装”绕过 EXTREME 风险。
