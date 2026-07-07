# External Capability Trust Gate Plan

日期：2026-06-26
更新：2026-07-07，按 Pack 退役后的 Extension surface 重写
状态：设计计划，待实现
范围：外部 Plugin/Extension 的发现、检验、审批、安装、激活、撤销与审计；Skill、MCP Server、Subagent、Hook 是 Plugin components，也保留单组件快捷导入入口
上位文档：
- `docs/agent-extension-surface-skill-mcp.md`
- `docs/session-tui-collaboration-provenance-root-cause-and-repair-plan-2026-07-02.md`
- `docs/ccplus-v1-deep-verification-reconciliation-2026-06-24.md`
- `docs/org-agent-asset-rights-model.md`
- `docs/cc-tooling-alignment-and-plugin-system.md`（历史参考，不再作为 Pack 主线依据）

## 0. 决策摘要

Hive 不需要做一个独立的 Skill 广场，也不应把已经退役的 Hive Pack 重新变成新的安装核心。Hive 需要做的是一个自己的 Plugin / Extension 安装系统，并在它前面放一个统一的外部能力安装门：

```text
External Capability Trust Gate
```

用户和 agent 可以从 GitHub、skills.sh、ClawHub、Smithery、MCP server registry、私有 repo、本地上传、CC plugin marketplace、Codex/OpenAI plugin marketplace 等地方发现和拉取能力，但这些来源本身不直接成为运行时信任根。Hive 的信任对象必须是经过检验、审批、固定 hash、可回放审计的 Plugin/Extension Snapshot。

2026-07-07 的系统边界：

- 用户和产品面使用 `Extensions`、`Skills`、`MCP Servers`、`Plugins`。
- `/agents/{agent_id}/extensions` 是 agent 维度 extension 状态的公共真相面，返回 `skills`、`mcp_servers`、`plugins`。
- MCP 已经是 server-first surface，不再是 pack-derived MCP grouping。
- Pack 作为模型 runtime 核心能力开关已经退役；不能把 `pack.yaml` 作为新 Trust Gate 的主抽象。
- 旧 `pack.yaml`、`TenantInstalledPlugin`、`AgentPluginAssignment` 只可作为 legacy compatibility / migration projection 处理，不能作为未来外部插件格式的目标模型。
- 长期 canonical install unit 是 Hive 自己的 `Plugin/Extension`；Skill、MCP、Subagent/Agent、Hook、App、Command 都是 component。
- 单独安装 Skill 或 MCP 只是 UX shortcut：底层应创建 one-component Plugin/Extension snapshot，而不是创建一套平行安装体系。
- CC `project` scope 映射到 Hive 的 employee / agent activation；CC `user` / `managed` global scope 映射到 user / enterprise catalog availability，不等于所有 agent 自动激活。
- 企业后台“安装”默认含义应是把 approved snapshot 放入 workspace catalog / managed catalog；只有显式 `mandatory` 或 `auto_activate_by_policy` 策略才会进入 agent runtime。

当前代码现实必须分清：

- 已有：`SkillGuard` 静态扫描、Skill active installer、MCP server-first 管理、MCP tool policy、legacy plugin install/projection、`/agents/{agent_id}/extensions` read model、`ToolRuntimeService` / `CapabilityGate` / `ActionPreflightService` 运行时治理、CC plugin normalized adapter、`external_capability_reviews` / `external_capability_snapshots` Trust Gate substrate、`admission_class` / `governance_projection_json` 落库、enterprise review stage / approve API、外部 Skill URL / ClawHub / skills.sh / code execution sandbox / MCP prompt -> Skill 入口的 Trust Gate staging、standalone MCP import 的 Trust Gate staging、approved Skill snapshot -> existing Skill runtime activation。
- 未完成：`external_extension_catalog_entries`、materializer sandbox、Codex adapter、MCP/plugin approved snapshot -> existing runtime projection、Agent Detail / Workspace Settings 的最终统一 IA。
- 因此本文档是目标计划和落地契约，不得把规划中的 Trust Gate 误读为当前已经全量上线的代码事实。

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
| `approve` | 检验来源、内容、权限、脚本、依赖、凭据，并产出准入判定和治理投影 | 必须 |
| `activate` | 写入 agent workspace / tenant connector / plugin assignment，让运行时可见 | 只有通过 Trust Gate 后允许 |

## 1. 术语

| 术语 | 定义 |
|---|---|
| External Capability | 外部能力入口总称，包括 Skill、MCP Server、Plugin、Subagent、Hook 等；底层统一归一成 Plugin/Extension Snapshot。 |
| Plugin / Extension | Hive 长期唯一的外部能力安装单元。它不直接等同于 CC/Codex plugin 格式，而是 Hive 的 canonical package，内部包含多个 component。 |
| Component | Plugin 内部的可激活子能力，例如 Skill、MCP server、Subagent/Agent、Hook、App/Connector、Command、Workflow、metadata。 |
| Source | 能力来自哪里，例如 GitHub URL、skills.sh ref、ClawHub slug、Smithery server id、plugin source。Source 是证据，不是信任。 |
| Materialized Package | 在隔离环境里拉取或生成出的原始包。可能包含 `SKILL.md`、scripts、templates、assets、MCP manifest、plugin manifest、依赖锁等。 |
| Quarantine / Staging | 未获批准前的隔离区。这里可以存证、分析、展示，但不能进入运行时。 |
| Trust Review | 平台生成的审查报告，包含 static scan、source provenance、permission analysis、dependency scan、sandbox smoke test、人工审批记录。 |
| Trusted Snapshot | 通过审查后的不可变快照，带 `content_sha256`、source ref、review id、approver、approved_at。后续安装以 snapshot 为准，不以外部 latest 为准。 |
| Catalog Listing | 将 approved snapshot 放入 platform / workspace / user 可发现目录。Catalog listing 只是可见和可选，不进入模型 prompt、tool surface、hook runtime。 |
| Activation | 将可信快照或其中 selected components 应用到 agent，使运行时可见。 |
| Capability Factor | 从 agent 自产 Skill、Subagent、Workflow pattern、工具使用经验中提炼出的可复用能力因子。它是“可入库候选”，不是 runtime activation。 |
| Promotion Proposal | 将某个 agent-local factor 提交到 workspace / enterprise catalog 的提案，必须带 source refs、审计报告、风险分析、复用理由和回滚路径。 |
| Revocation | 撤销某个 trusted snapshot 或 connector/plugin install，阻止新 agent 使用，并可选禁用已安装实例。 |

不要把这个机制叫“信任源”。“信任源”容易暗示某个外部站点永远可信。更准确的命名是：

```text
Trust Gate = 审查门
Trusted Snapshot = 审查通过的可信快照
Source = 可追溯来源
```

## 2. 为什么必须统一

当前 Skill、MCP、Plugin 已经有各自的安装路径，但缺统一安装门。更新后的目标不是长期维护三套安装系统，而是收敛为：

```text
External source
  -> materialize
  -> normalize into Hive Plugin/Extension manifest
  -> review snapshot
  -> activate selected components
```

现有路径如何理解：

- Skill：已有 `SkillGuard` 和统一的 `skill-marketplace` 发现/审查入口；外部 Skill 仍必须通过受治理的 active `skills/` 写入路径。
- MCP：已有 server-first 管理、per-agent assignment、tool policy，但导入、凭据、连接测试、工具暴露、撤销记录需要统一到外部能力总账。
- Plugin：当前公共面已经作为 agent extension 的 `plugins` 返回；历史 `TenantInstalledPlugin` / `AgentPluginAssignment` 仍在代码里承载部分兼容安装记录，但新计划不能继续以 `pack.yaml` 为中心。远程 plugin source 应进入统一 Trust Gate，先审查、再按兼容组件激活。

因此，Skill.sh / ClawHub / GitHub Skill 的安装不是另一套产品线；它们应被标准化成：

```text
Hive Plugin/Extension
  components.skills[] = [...]
```

Smithery / MCP registry / MCP URL 的安装也不是另一套产品线；它们应被标准化成：

```text
Hive Plugin/Extension
  components.mcp_servers[] = [...]
```

问题不是“能不能装”。问题是：

```text
装之前是否知道它来自哪里
是否完整读过它会带来什么
是否在隔离环境里 materialize
是否把安装时代码和运行时代码分开
是否有人工/策略审批
是否能撤销和回放
```

同一服务器里混入恶意脚本是平台级安全问题。外部 Skill 不是普通 Markdown；它可以携带脚本、模板、依赖、工具提示、网络调用说明，甚至诱导 agent 执行危险命令。MCP 和 Plugin 需要更强准入要求，因为它们可能直接引入外部工具面、凭据面、hook 或 dependency。

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
| Plugin source | CC marketplace、Codex/OpenAI marketplace、GitHub/git/url/npm/local upload/private registry | 远程 source 必须先 materialize、hash、scan、生成 compatibility report；未达标时只 stage/analyze，不激活。 |
| Legacy pack manifest | 历史 `pack.yaml` / installed-plugin record | 只用于兼容迁移和审计，不作为新外部插件生态格式。 |
| Local upload | ZIP / folder | 仍必须走相同 scan 和 approval。 |

发现阶段输出：

```json
{
  "source_kind": "github|skills_sh|clawhub|mcp_registry|cc_marketplace|codex_marketplace|openai_plugin|plugin_source|legacy_pack|local_upload",
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
| Human/admin approval | 由准入判定决定 | 必须 | 必须 |

### 3.3.1 准入判定：不是第二套运行时治理

这里不能再新建一套和现有对话框治理 / 后台治理并列的“风险系统”。Hive 已经有运行时治理链路：

- `ToolRuntimeService` 是工具执行咽喉。
- `CapabilityPolicy` / `CapabilityGate` 决定 tenant / agent 维度能力是否允许、拒绝或需要审批。
- `GuardPolicy` 是 tenant 级 Guard 配置，由后台管理并同步到客户端执行。
- MCP 工具仍走 MCP server policy：`deny`、`approval`、`auto`。
- `ActionPreflightService` 负责具体动作的运行时边界：敏感度、可逆性、外部可见性、公司边界、是否需要 ASK / REFUSE / ESCALATE。
- Plan confirmation / approval / audit 继续处理不可逆、高影响或需要人工确认的动作。

因此 Trust Gate 的准入判定只回答安装前问题：

```text
这个外部 artifact 是否可以成为 trusted snapshot？
如果可以，它允许哪些 component 被激活？
激活时要生成哪些现有治理配置或约束？
```

它不回答运行时问题：

```text
这个 agent 此刻能不能调用某个 tool？
这次 tool call 的参数、收件人、文件、凭据、外部副作用是否允许？
这次动作是否需要用户确认、企业审批或 checkpoint？
```

这些运行时问题继续交给现有治理系统。

准入判定的对象是 `snapshot + selected components + target activation scope` 的组合。同一个 plugin 只启用纯说明 Skill 时，准入结果可能是 `metadata_only`；同一个 plugin 如果启用 MCP server、hook、credential、外部发送能力，则可能变成 `admin_scoped`；同一个 snapshot 安装到个人测试 agent 和安装到生产财务/邮件 agent，准入投影也可以不同。

准入判定输出的是 `admission_class` 和 `governance_projection`，不是新的 runtime policy engine：

| admission_class | 含义 | 安装/激活动作 | 运行时怎么治理 |
|---|---|---|---|
| `metadata_only` | 纯说明、模板、只读 Skill guidance；没有脚本、hook、MCP、credential、外部写动作 | 可进入 approved snapshot；owner/user 可按 scope 激活 | 不新增 tool 权限；后续仍只走已有 read-only / Skill loading 语义 |
| `governed_runtime` | 会新增已知 runtime surface，例如 workspace 写入、已知 API、MCP tools，但权限面清楚 | 可审批激活；必须生成 component-level activation 和默认 policy | 投影到现有 `CapabilityPolicy`、MCP policy、credential handle、ActionPreflight；每次调用仍被现有治理拦截 |
| `admin_scoped` | 需要凭据、外部发送、生产系统、支付/金融/邮件、hook、install-time script、subagent 权限扩张等 | 需要管理员审批后才可 activation；默认 tool/MCP mode 应为 `approval` 或 `deny` | 继续走现有 approval、MCP policy、GuardPolicy、ActionPreflight，不因为通过 Trust Gate 而自动放行 |
| `blocked` | credential theft、exfiltration、混淆执行、root、逃逸路径、未知 binary、越权写 memory/soul 等 | 不允许 activation，只能 quarantine / security review | 不进入运行时，因此现有治理不会看到它 |

Trust 的准确语义是：某个不可变 snapshot 的来源、内容和 component mapping 已经通过准入，可以在批准的 scope 内被激活。它不代表“后续所有运行时行为都免治理”。已经 trust 过的能力，激活后必须继续 follow 当前现有治理逻辑。

重新 review 只在这些情况下发生：

- 外部 source 更新了 commit / tag / tarball / npm version / MCPB。
- 启用的 component 集合变化，例如从只启用 Skill 改成启用 MCP / hook。
- activation scope 扩大，例如从单 agent 变成 workspace catalog / mandatory policy。
- credential requirement、network endpoint、tool/resource surface、dependency、script 发生变化。
- snapshot 被 revoke、policy 被更新、或企业后台要求重新审查。

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

Catalog availability 和 runtime activation 必须分离：

```text
Approved Snapshot
  -> Catalog Listing       # platform / workspace / user 可发现、可安装
  -> Agent Activation      # 只有被 employee/agent 显式启用后才进入运行面
```

企业后台预置插件时，默认只创建 `Catalog Listing`，不自动把插件注入所有 agent。只有管理员显式选择 `mandatory` 或 `auto_activate_by_policy`，并且策略命中对应 agent role / team / tag / template 时，才允许自动创建 agent activation。

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
TrustedPluginSnapshot
  -> Extension plugin install record
  -> Agent extension assignment
  -> mapped Skill / MCP / app / hook activation
  -> legacy installed-plugin projection only while the old plugin table still exists
```

Activation 必须记录：

- source ref
- resolved ref / content hash
- review id
- approver
- target tenant / agents
- files written or server/plugin records created
- rollback / revoke ref

### 3.6 Agent 自产能力与因子入库

外部能力入口只解决“从外部拿能力”的治理；Hive 还必须解决另一条更重要的路径：

```text
agent 在工作中自己生成 Skill / Subagent / Workflow pattern / tool usage pattern
  -> 是否只属于这个 agent
  -> 是否值得沉淀成个人或企业可复用能力
  -> 是否进入公司后台 catalog
```

核心原则：

```text
Agent 可以自动生成候选
平台可以自动审计候选
企业 catalog 入库默认不能隐式同步
企业可复用发布必须走 proposal-based promotion
```

也就是说，agent 自产能力和公司后台之间不是“自动同步关系”，而是“候选 -> 审计 -> 提案 -> 审批/策略 -> catalog”的关系。

完整路径：

```text
Agent runtime evidence
  -> Agent-local candidate
  -> Capability Factor Intake
  -> Automated Audit Report
  -> Promotion Proposal
  -> Enterprise Review / Policy Decision
  -> Trusted Snapshot
  -> Workspace Catalog Listing
  -> Other Agent Activation
```

三层对象必须分开：

| 层 | 含义 | 是否进入公司后台 | 是否进入其他 agent runtime |
|---|---|---:|---:|
| Agent-local candidate | 某个 agent 自己生成或演化出的 Skill/Subagent 草稿、补丁、定义或经验模式 | 可以记录索引和审计元数据 | 否 |
| Capability Factor | 从候选中提炼出的可复用因子，带证据、风险、复用评分、适用边界 | 是，进入 factor intake / review queue | 否 |
| Trusted Snapshot / Catalog Listing | 通过 Trust Gate 和企业审批后的可安装能力快照 | 是，进入 workspace catalog | 只有被 agent activation 后才进入 |

自产 Skill 的当前落点应映射为：

```text
evolution/skill_candidates/<candidate_id>/
  -> Capability Factor(kind=skill_candidate)
  -> Promotion Proposal
  -> Trusted Skill Component Snapshot
  -> Workspace Catalog
```

自产 Subagent 的当前落点应映射为：

```text
subagent pending proposal / agent definition
  -> Capability Factor(kind=subagent_candidate)
  -> Promotion Proposal
  -> Trusted Subagent Component Snapshot
  -> Workspace Catalog
```

自动审计可以做：

- source_refs 完整性检查。
- 生成来源分类：user requested、agent self-evolved、T3/memory-derived、runtime evidence-derived。
- secret scan / path scan / dependency scan。
- 权限面分析：tools、MCP servers、filesystem、network、credentials、external-visible actions。
- 适用范围分析：只适合当前 agent、适合某类 role、适合整个 workspace。
- 敏感信息检查：客户名、个人数据、公司机密、credential、内部 URL。
- 去重和冲突检查：是否已有类似 Skill/Subagent/Plugin。
- smoke test / eval report / regression report。
- 复用评分：reusability、novelty、success_count、failure_count、volatility。

自动审计不能默认做：

- 直接发布到 enterprise catalog。
- 直接让其他 agent 可见。
- 直接写入 tenant-level Subagent library。
- 直接把 agent 私有经验升级为公司标准流程。
- 绕过 owner/admin 审批使用敏感凭据、外部工具或 hook。

自进化范围必须先收窄：

```text
Self-evolution v1 scope = Hive/agent 自己生成或明确 fork 的能力
External capability scope = 使用、审查、激活、反馈、提案；默认不自动改写
```

外部 Skill / Plugin 即使通过 Trust Gate，也不应该默认进入自进化 patch chain。原因：

- provenance 不属于 Hive 原生作者链，直接改写会破坏来源和 license 边界。
- 外部能力可能有 upstream 更新、签名、版本约束；本地自改会变成 fork。
- agent 的成功使用证据只能证明“适合复用”，不能证明“允许改写并再分发”。
- 外部能力常带脚本、MCP、hook、credential requirements，自进化改写会扩大风险面。

因此外部能力默认策略：

| 对象 | 是否可被 agent 使用 | 是否进入 self-evolution | 是否可进入公司库 |
|---|---:|---:|---:|
| Hive/agent 自己生成的 Skill | 是 | 是，按 authoring contract 和 eval gate | 是，走 factor promotion |
| Hive/agent 自己生成的 Subagent | 是 | 是，按 proposal/approval gate | 是，走 factor promotion |
| 外部 Skill / CC plugin / Codex plugin | 是，Trust Gate 通过后可 agent-scoped activation | 否，默认只允许 usage feedback 和 patch suggestion | 可以，但必须走 external catalog promotion |
| 外部能力的本地 fork | 是，作为新 Hive-authored fork 审查 | 是，fork 之后按 Hive-authored 能力处理 | 可以，必须标明 upstream 和 fork diff |

外部能力如果确实需要“进化”，必须先显式 fork：

```text
External Snapshot
  -> Fork Proposal
  -> license / attribution / upstream diff review
  -> Hive-authored Fork Snapshot
  -> self-evolution eligible after approval
```

没有 fork 的外部能力，只记录：

- 使用次数。
- 成功/失败反馈。
- 哪些 agent / role 适用。
- 是否建议发布到 workspace catalog。
- 是否建议向 upstream 提 patch 或在 Hive 内 fork。

默认决策矩阵：

| Candidate 类型 | 自动审计 | 自动 agent-local 激活 | 自动进入公司后台 | 默认企业发布 |
|---|---:|---:|---:|---:|
| 用户明确要求当前 agent 创建的 Skill | 是 | 可在当前 agent 范围内按 owner policy 激活 | 仅进入 factor intake | 否，需要 proposal approval |
| Agent 从重复工作中自我生成的 Skill 候选 | 是 | 不直接激活，先 trial / eval | 仅进入 factor intake | 否，需要 proposal approval |
| Subagent proposal | 是 | 只在当前 agent owner 批准后应用 | 仅进入 factor intake | 否，需要 proposal approval |
| 外部 Skill / Plugin 通过 Trust Gate | 是 | 可按 approved scope 激活到当前 agent | 只记录 external usage factor | 否，需要 external catalog promotion |
| 外部 Skill / Plugin 的 Hive fork | 是 | 按 fork 后 authoring contract 决定 | 可进入 factor intake | 否，需要 proposal approval |
| 纯提示/模板类 `metadata_only` 能力 | 是 | 可按 tenant policy 自动 trial | 可自动创建 proposal | 默认不 publish |
| 含 MCP、凭据、外部动作、hook、脚本的 `admin_scoped` 能力 | 是 | 否 | 仅进入 admin-scoped review queue | 否，必须管理员审批 |

企业后台应该看到的不是“所有 agent 草稿”，而是筛选后的入库队列：

```text
Workspace Settings -> Capability Factor Intake
  -> Skill candidates
  -> Subagent candidates
  -> Workflow patterns
  -> Tool/MCP usage patterns
  -> Rejected / Duplicate / Sensitive
```

每个 factor 必须展示：

- originating agent / owner / session。
- source_refs / trace_refs / evidence refs。
- candidate artifact hash。
- authoring contract：LLM authored / platform scaffold / human approved。
- declared tools / MCP / credentials / network。
- admission class / governance projection。
- reusability / novelty / volatility。
- suggested catalog scope：personal、workspace、role-specific、team-specific。
- automatic audit findings。
- proposed activation policy。

这就是“因子入库”的准确含义：先把可复用能力因子进入企业可审计队列，而不是把 runnable Skill/Subagent 直接塞进公司 runtime。入库的是 factor/proposal；真正可安装的是后续 approved snapshot。

### 3.7 外部 Skill 的公司库收录机制

外部 Skill / Plugin 过检之后，默认结果是：

```text
Trusted External Snapshot
  -> Agent-scoped Activation
```

它不会直接变成：

```text
Workspace Catalog Listing
```

因为“允许某个 agent 使用”和“公司认可并推荐给其他 agent 使用”是两次不同决策。

外部能力有三种落点：

| 落点 | 含义 | 触发方式 |
|---|---|---|
| Agent activation only | 只给当前 agent / owner 使用 | owner 安装，Trust Gate 通过，approval scope 仅覆盖该 agent。 |
| Workspace catalog proposal | 推荐企业收录，但尚未发布 | owner 手动提交、agent 使用效果触发、管理员从 review 中提名。 |
| Workspace catalog listing | 企业已收录，可被其他 agent 发现/安装 | enterprise approval 通过，生成 catalog entry。 |

外部能力进入公司库必须走独立 promotion：

```text
Agent uses external skill successfully
  -> External Usage Factor
  -> Catalog Promotion Proposal
  -> Enterprise Review
  -> Approved Trusted Snapshot
  -> Workspace Catalog Listing
```

Promotion review 必须额外检查：

- license / redistribution / attribution。
- upstream repo / marketplace reputation。
- pinned version / content hash / signature。
- 是否需要 fork，还是仅引用 upstream snapshot。
- 是否含公司敏感配置或 agent 私有修改。
- 是否带脚本、MCP、hook、credential、network egress。
- 是否已有同类公司内置能力。
- 使用证据：哪个 agent 使用、多少次、成功率、失败记录、owner 推荐理由。

外部能力被收录到 workspace catalog 后，仍然不自动进入所有 agent runtime。它只变成：

```text
Workspace provided / approved_available
```

后续其他 agent 还需要：

```text
install to employee
  or try in chat
  or auto_activate_by_policy selector match
```

如果公司决定长期维护该外部能力的改版，应创建 Hive fork：

```text
External upstream snapshot
  -> Hive fork snapshot
  -> company-owned version policy
  -> self-evolution eligible only after fork approval
```

Fork 后必须保留：

- upstream source_ref。
- upstream version/hash。
- fork diff。
- license/attribution report。
- company approver。
- rollback to upstream snapshot 的策略。

## 4. Plugin Components 的具体规则

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

Plugin 是 Skill、MCP、Subagent/Agent、Hook 等 component 之上的组合单元。它是 Hive 的唯一长期外部能力安装单元。

这意味着：

- Skill 可以继续作为用户看得懂的一类能力存在。
- MCP Server 可以继续作为 server-first runtime object 存在。
- Subagent/Agent definition 可以继续作为 delegation runtime 的输入存在。
- Hook 可以继续作为 lifecycle interception runtime 的输入存在。
- 但安装、审查、来源追踪、hash、审批、撤销、兼容报告都应归到 Plugin/Extension Snapshot。

Plugin 可以包含：

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

- Plugin manifest 是外部能力的 composition / metadata / credential requirements / component declaration，不是 Hive tool schema 真相源。
- Hive 内置 tool executable schema 仍由 `@tool(ToolMeta)` 和 governance taxonomy 拥有。
- CC/Codex/OpenAI plugin manifest 只能通过 adapter 进入 normalized extension manifest。
- 历史 `pack.yaml` 只能作为 legacy import source；新生态不再要求开发者写 Hive pack。
- 远程 plugin source 在 signature/hash/sandbox materializer 未完成前 fail-closed。
- hooks 只能引用平台 allowlist handler。
- PRE_TOOL_USE enforce hook 必须管理员显式审批。
- dependencies 必须 pinned，并写入 lockfile。
- plugin activation 只激活通过兼容映射的组件，不绕过 runtime governance。

### 4.3.1 Skill 与 Subagent 的边界

不要把 “Skill 可以携带相关文件” 误读成 “Skill 是所有东西的父容器”。

更准确的模型是：

```text
Plugin/Extension
  -> skills[]
  -> mcp_servers[]
  -> agents_or_subagents[]
  -> hooks[]
  -> apps/connectors[]
  -> commands[]
  -> workflows[]
```

CC 的实现也更接近这个模型：

- Plugin manifest 有独立 `agents` 字段。
- Plugin manifest 有独立 `skills` 字段。
- Plugin agent frontmatter 可以声明 `skills`，表示这个 agent 预载/关联哪些 skills。
- Plugin agent 不允许自己偷偷声明 `hooks` / `mcpServers` / `permissionMode` 这类会扩大权限的字段；这些必须在 plugin install-time 边界处理。

所以 Hive 可以允许一个 Skill package 目录里带 `agents/`、`evals/`、`scripts/`、workflow references 等辅助材料，但 canonical install model 不应变成 “Skill 包住 Subagent”。应该是 “Plugin 包住 Skill 和 Subagent，Skill 可以引用或建议使用 Subagent”。

### 4.4 CC / Codex 插件兼容目标

这一层要单独写清楚，因为“能读格式”和“能无缝执行全部能力”不是一回事。

当前结论：

1. Hive 当前插件系统和 CC 插件系统不一致，不能直接宣称“已经兼容并可安装 CC 全部能力”。
2. Hive 当前插件系统和 Codex 插件系统也不一致，不能把 Codex `.codex-plugin/plugin.json` 当成 Hive legacy pack 或旧 installed-plugin record 直接落库。
3. 可以把 CC/Codex 插件都纳入同一个 Trust Gate，通过 adapter 转成 Hive 的 normalized manifest，再按组件逐项激活。
4. 兼容目标应写成 capability compatibility level，而不是二元“支持/不支持”。
5. Hive 的目标不是复制 CC/Codex 的 plugin runtime，而是用自己的 Plugin/Extension 系统兼容它们的 manifest 和 component。

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
| `hooks` | `external_extension_hook_registrations` / allowlist handler；迁移期可投影到 `PluginHookRegistration` | 低，CC raw shell hook 不能直接在 Hive 多租户服务器执行。 |
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
| `hooks` | allowlisted extension hook registration；迁移期可投影到 `PluginHookRegistration` | 低到中，禁止 raw execution。 |
| `interface` | catalog/review metadata | 高，只做 metadata。 |

Codex 的 `.claude-plugin/plugin.json` 兼容入口说明它能读一部分 CC-style manifest location，但这不等于 Hive 可以跳过自己的 Trust Gate，也不等于 Codex/CC manifest 字段完全一致。

#### 4.4.3 Normalized Plugin Manifest

Hive 不应该为 CC 和 Codex 各建一套运行时。应新增 adapter，把外部格式统一成一个 internal normalized manifest：

```text
NormalizedExternalPluginManifest
  manifest_format: hive_extension_manifest | cc_plugin | codex_plugin | openai_plugin | legacy_pack_manifest
  source_kind: cc_marketplace | codex_marketplace | openai_plugin | github | git | url | npm | pip | local_upload | private_registry | legacy_pack | builtin | local
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
| `HiveExtensionManifestAdapter` | Hive-native extension manifest, if we add one | normalized manifest |
| `CCPluginAdapter` | `.claude-plugin/plugin.json` + `marketplace.json` | normalized manifest |
| `CodexPluginAdapter` | `.codex-plugin/plugin.json` / `.claude-plugin/plugin.json` + Codex marketplace | normalized manifest |
| `LegacyPackAdapter` | 历史 `pack.yaml` | migration-only normalized manifest，不作为新生态目标 |
| `SkillSourceAdapter` | GitHub URL / ClawHub / skills.sh | normalized Skill package |
| `MCPSourceAdapter` | Smithery / MCP registry / URL / manifest | normalized MCP install |

所有 adapter 只能产出 staged artifact 和 review report；不能直接 active install。

#### 4.4.3.1 Legacy Pack / Direct Import 债务分级

不能把“旧 package/pack 还在”简单等同于必须马上清掉的阻断性技术债。更准确的分级是：

| 现象 | 债务类型 | 是否阻断当前落地 | 处理方式 |
|---|---|---:|---|
| `pack.yaml`、`TenantInstalledPlugin`、`AgentPluginAssignment` 仍作为兼容记录存在 | 迁移债 | 否 | 保留为 migration-compatible backing store，通过 `LegacyPackAdapter` backfill 到 review/snapshot/catalog/activation。 |
| runtime 代码通过 capability-group facade 读取 legacy pack policy backing store | 迁移债 | 否 | 允许暂存，但产品面和新 API 不能暴露 pack/package 字段。 |
| 旧 pack/package 仍作为新能力入口、统一 UI 入口或 `/agents/{agent_id}/extensions` 的产品概念 | 运行时债 | 是 | 必须收口到 Plugin/Extension Catalog + Activation，不得继续宣传 pack。 |
| URL / ClawHub / HR external skill import 经 SkillGuard 后直接写 active skill package | 入口一致性债 | 是 | 改为 `SkillSourceAdapter -> stage/review/snapshot -> activation`，保留现有入口但返回 `review_required` 或投影到 Trust Gate。 |
| `npx skills add` 或 sandbox HOME 产物被直接装入 active `skills/` | 入口一致性债 | 是 | 改为 materializer sandbox 产出 staged artifact，禁止未经 approved snapshot 的 active install。 |

当前代码证据支持这个分级：

- `backend/app/services/capability_group_policy_service.py` 已声明 persisted storage 仍是 legacy pack policy，但 runtime callers 应依赖 capability groups。
- `backend/app/services/agent_tools.py` 的 `is_pack_enabled` 只是 compatibility shim；MCP discovery / reachability 已明确不靠 legacy `mcp_server:*` pseudo-pack。
- `backend/app/services/mcp_server_service.py` 的 extension surface 声明 public DTO 不携带 `pack` / `pack_name`，但仍会读取 `TenantInstalledPlugin` / `AgentPluginAssignment` 作为兼容 plugin assignment。
- `backend/app/api/files.py` 的 `/import-from-url` 和 `/import-from-clawhub` 仍会调用 `install_active_skill_package`，而 `backend/app/services/skill_installation.py` 会在 SkillGuard 通过后直接写入 `workspace/skills/<folder>`。这正是必须迁移的 direct import bypass。

因此迁移原则是：

```text
不要为了清债断掉现有能力。

旧 pack/package compatibility
  -> LegacyPackAdapter
  -> review/snapshot backfill
  -> catalog / activation projection

旧 direct skill import
  -> SkillSourceAdapter
  -> materialize in sandbox
  -> Trust Gate review
  -> trusted snapshot
  -> /agents/{agent_id}/extensions activation
  -> existing Skill runtime
```

最终判断标准不是“代码里还有没有 pack 字样”，而是：

1. 新外部能力入口是否必须经过 Trust Gate。
2. `/agents/{agent_id}/extensions` 是否不暴露 legacy pack/package 字段。
3. 旧兼容记录是否只能作为 migration projection / audit evidence。
4. active runtime 是否只由 approved snapshot + agent activation 决定。

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
- 格式：CC plugin / Codex plugin / OpenAI plugin / Hive extension manifest / legacy pack / plain Skill。
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
| Plugin | OpenAI/Codex plugin + CC plugin marketplace + GitHub/private plugin manifest + legacy pack migration source | adapter normalize + component compatibility report + Trust Gate。 |

市场不是 trust root。市场只回答“哪里有东西”；Hive Trust Gate 才回答“这个东西是否能装、装哪些部分、用什么权限运行”。

#### 4.4.7 Scope 与治理模型：Catalog != Activation

推荐采用：

```text
Enterprise / User Catalog
  -> employee chooses / admin assigns
  -> Agent Activation
```

不要采用：

```text
Enterprise install
  -> every agent runtime gets every plugin
```

原因是 global plugin 在本地 CLI 里通常意味着“当前用户或当前项目加载时可见”，但 Hive 是多 agent、多租户、长期运行的服务器系统。后台安装如果直接等于所有 employee runtime activation，会带来三个问题：

- prompt / tool / hook surface 膨胀，很多 agent 会加载自己永远不用的能力。
- 权限面扩大，一个原本只应具备基础能力的 agent 可能被动暴露 `admin_scoped` MCP、hook、credential requirement。
- 回滚复杂，一次 enterprise install 影响所有正在运行或未来恢复的 agent session。

更稳的映射：

| 外部概念 | Hive 概念 | 是否进入 agent runtime |
|---|---|---|
| CC `project` plugin | Employee / Agent Activation | 是，只对该 agent 或该 project-like employee workspace 生效。 |
| CC `user` global plugin | User catalog entry / personal library | 否，除非用户选择安装到某个 agent。 |
| CC `managed` global plugin | Enterprise managed catalog entry | 否，除非管理员策略要求激活。 |
| Codex `由 OpenAI 提供` | Platform curated catalog | 否，只是平台预置可发现来源。 |
| Codex `由你的工作空间提供` | Workspace approved catalog | 否，只是企业已审批、可安装。 |
| Codex `个人` | User personal catalog | 否，只是个人已保存或已审批入口。 |
| `Try in chat` | Session-scoped temporary activation | 只在当前 session / sandbox 内生效，不写 durable agent assignment。 |

完整治理链路应拆成四层：

```text
Source Registry
  -> Trust Review / Trusted Snapshot
  -> Catalog Listing
  -> Agent Activation
```

各层职责：

| 层 | 负责什么 | 关键规则 |
|---|---|---|
| Source Registry | 记录 OpenAI/Codex、CC marketplace、ClawHub、skills.sh、Smithery、GitHub、private registry 等来源 | source 只可发现，不可信任。 |
| Trusted Snapshot | 固定 hash、review report、compatibility report、approval | 没有 snapshot 不允许 catalog listing 或 activation。 |
| Catalog Listing | 决定谁能看到、谁能申请、是否推荐、是否可安装 | 进入 catalog 不等于进入 runtime。 |
| Agent Activation | 决定某个 employee 实际启用哪些 components | 只有 activation 才能影响 prompt/tools/MCP/hooks/subagents。 |

Catalog policy 建议使用这些状态：

| Policy | 含义 | 默认使用场景 |
|---|---|---|
| `blocked` | 企业禁止安装 | 恶意、违规、越权或不符合公司策略。 |
| `requestable` | 用户/agent 可提交审批请求 | 来自公开 marketplace、GitHub、npm 等未审批来源。 |
| `approved_available` | 已审查，workspace/user catalog 可见 | 企业常规预置插件。 |
| `recommended` | 已审查并被推荐，但不自动激活 | 常用办公、代码、文档类能力。 |
| `auto_activate_by_policy` | 对命中条件的 agent 自动激活 | 例如某类工程 employee 默认启用 GitHub MCP。 |
| `mandatory` | 强制激活，不允许 agent owner 禁用 | 只能用于审计、安全、合规等极少数基础能力。 |

`auto_activate_by_policy` 必须有明确 selector：

```text
agent_template_id
agent_role
department_id
team_id
tenant_id
tags[]
owner_user_id
environment
```

反膨胀规则：

- Catalog listing 不进入 agent prompt。
- Catalog listing 不注册 MCP tools。
- Catalog listing 不注册 hooks。
- Catalog listing 不创建 subagent runtime。
- Agent activation 必须 component-level，可只启用一个 plugin 里的 `skills[]` 或 `mcp_servers[]` 子集。
- 每个 agent 的 `/agents/{agent_id}/extensions` 只返回 active / available / recommended 的差异视图，不把 enterprise catalog 全量塞进 runtime context。
- `Try in chat` 默认是 temporary activation；结束 session 后清理，除非用户显式转为 durable install。

这和 CC 的兼容关系是：

```text
CC plugin format                 -> adapter input
CC managed/user/project scopes   -> Hive catalog/activation policy
CC local runtime loading         -> Hive governed activation runtime
```

也就是说，Hive 可以对开发者说“CC plugin manifest 可以进入 Hive 审查和安装流程”，但不能说“CC global install 会被原样复制成所有 agent 自动加载”。Hive 的 global 应该解释为 availability global，而不是 runtime global。

## 5. 数据模型提案

### 5.1 `external_capability_reviews`

统一 review 表，所有 entrypoint 共用。

```text
id
tenant_id
requested_by_user_id
requested_by_agent_id
capability_kind              plugin
entrypoint_kind              skill | mcp_server | plugin | subagent | hook | app | command | workflow
source_kind                  github | skills_sh | clawhub | smithery | mcp_registry | cc_marketplace | codex_marketplace | openai_plugin | plugin_source | private_registry | legacy_pack | local_upload
source_ref
resolved_ref
display_name
manifest_format              skill_package | mcp_manifest | hive_extension_manifest | cc_plugin | codex_plugin | openai_plugin | legacy_pack_manifest | unknown
status                       staged | analyzing | needs_review | approved | rejected | quarantined | active | revoked
admission_class              metadata_only | governed_runtime | admin_scoped | blocked
governance_projection_json
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
entrypoint_kind
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
| `Skill` / `SkillFile` | tenant registry skill 应由 trusted plugin snapshot 的 `skills[]` component 创建或更新。 |
| agent workspace `skills/` | 只能由 approved snapshot 激活写入。 |
| `AgentCapabilityInstall` | 可保留为 agent 维度 install readiness/read model，但应挂 `review_id` / `snapshot_id`。 |
| `MCPServer` / `AgentMCPServerAssignment` | 由 trusted plugin snapshot 的 `mcp_servers[]` component 创建/启用。 |
| `/agents/{agent_id}/extensions` | 必须成为 Skill/MCP/Plugin/Subagent 等 component 激活后的统一读模型。 |
| `TenantInstalledPlugin` / `AgentPluginAssignment` | 仅作为当前 legacy plugin projection；新 Trust Gate 不应继续把它们当 canonical plugin install model。迁移期可以由 approved plugin snapshot 生成兼容投影。 |
| `external_extension_hook_registrations` | 只接受 approved plugin snapshot 里的 allowlisted hook；迁移期如果仍投影到 `PluginHookRegistration`，应记录为 legacy debt。 |

目标新增表应避免 `pack` 命名：

```text
external_extension_catalog_entries
external_extension_installs
external_extension_assignments
external_extension_activations
external_extension_components
external_extension_hook_registrations
capability_factors
capability_factor_reviews
capability_promotion_proposals
```

这些表才是 Trust Gate 之后的 canonical plugin/extension install surface；历史 plugin/pack 表只做兼容读取和 backfill。

### 5.4 Catalog 与 Activation 数据边界

需要显式增加 catalog 层，避免把 enterprise install 和 agent runtime activation 混在一张表里：

```text
external_extension_catalog_entries
  id
  tenant_id
  snapshot_id
  catalog_scope                  platform | workspace | user
  owner_user_id                  nullable
  policy                         blocked | requestable | approved_available | recommended | auto_activate_by_policy | mandatory
  selector_json                  agent_template_id / role / team / tags / owner / environment
  display_metadata_json
  created_by_user_id
  created_at
  updated_at

external_extension_activations
  id
  tenant_id
  agent_id
  snapshot_id
  catalog_entry_id               nullable for direct approved activation
  activation_scope               agent | session
  activated_components_json
  permission_overrides_json
  credential_binding_refs_json
  status                         active | disabled | revoked | expired
  activated_by_user_id
  activated_at
  expires_at
  revoked_at
```

`external_extension_assignments` 可以作为 `external_extension_activations` 的旧名或 read model，但 canonical 语义应是 activation。Catalog entry 是“可以用”，activation 是“正在用”。

对当前 legacy 表的迁移：

```text
TenantInstalledPlugin
  -> external_extension_catalog_entries(policy=approved_available, catalog_scope=workspace)

AgentPluginAssignment(enabled=true)
  -> external_extension_activations(activation_scope=agent)
```

这个迁移只做兼容投影，不把 legacy pack 恢复为新模型。

### 5.5 自产能力因子表

自产 Skill/Subagent 不应该直接写入 `external_extension_catalog_entries`。先进入 factor / proposal：

```text
capability_factors
  id
  tenant_id
  originating_agent_id
  originating_user_id
  factor_kind                    skill_candidate | subagent_candidate | workflow_pattern | tool_usage_pattern | mcp_usage_pattern | external_usage_factor | external_fork_candidate
  source_refs_json
  trace_refs_json
  artifact_ref
  artifact_sha256
  upstream_source_ref
  upstream_content_sha256
  license_report_json
  display_name
  summary
  authoring_contract_json
  declared_components_json
  declared_permissions_json
  sensitivity_report_json
  reuse_score_json               reusability / novelty / success_count / failure_count / volatility
  suggested_scope                personal | workspace | role | team | agent_template
  status                         captured | analyzing | needs_review | proposed | rejected | promoted | archived
  created_at
  updated_at

capability_factor_reviews
  id
  tenant_id
  factor_id
  automated_report_json
  admission_report_json
  findings_json
  dedupe_report_json
  eval_report_json
  decision                      pending | hold | propose | reject
  created_at
  updated_at

capability_promotion_proposals
  id
  tenant_id
  factor_id
  review_id
  proposed_snapshot_kind        skill | subagent | plugin | workflow
  proposed_catalog_scope        workspace | user | platform
  proposed_activation_policy    requestable | approved_available | recommended | auto_activate_by_policy | mandatory
  proposed_selector_json
  approver_id
  decision                      pending | approved | rejected | needs_changes
  decision_reason
  resulting_snapshot_id
  created_at
  decided_at
```

状态机：

```text
captured
  -> analyzing
  -> needs_review
  -> proposed
  -> approved promotion
  -> trusted snapshot
  -> catalog listing
```

`promoted` 只表示 factor 已经生成 approved snapshot 或 catalog entry；不表示已经进入任何 agent runtime。

## 6. API 提案

统一 API：

```text
POST /enterprise/external-capabilities/stage
GET  /enterprise/external-capabilities/reviews
GET  /enterprise/external-capabilities/reviews/{review_id}
POST /enterprise/external-capabilities/reviews/{review_id}/analyze
POST /enterprise/external-capabilities/reviews/{review_id}/approve
POST /enterprise/external-capabilities/reviews/{review_id}/reject
POST /enterprise/external-capabilities/reviews/{review_id}/publish-to-catalog
POST /enterprise/external-capabilities/reviews/{review_id}/revoke
GET  /enterprise/external-capabilities/catalog
POST /enterprise/external-capabilities/catalog/{catalog_entry_id}/policy
POST /agents/{agent_id}/extensions/{catalog_entry_id}/activate
POST /agents/{agent_id}/extensions/{activation_id}/disable
POST /agents/{agent_id}/extensions/{catalog_entry_id}/try
GET  /agents/{agent_id}/capability-factors
POST /agents/{agent_id}/capability-factors/{factor_id}/request-promotion
GET  /enterprise/capability-factors
GET  /enterprise/capability-factors/{factor_id}
POST /enterprise/capability-factors/{factor_id}/analyze
POST /enterprise/capability-factors/{factor_id}/propose
POST /enterprise/capability-promotion-proposals/{proposal_id}/approve
POST /enterprise/capability-promotion-proposals/{proposal_id}/reject
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
如果已有 approved snapshot 但未进 catalog -> 管理员可 publish-to-catalog 或 direct activate
如果已有 catalog entry -> 激活到该 agent 或创建 session-scoped try activation
```

这些入口不能创建平行安装真相。它们都只是不同 entrypoint：

```text
skill import      -> capability_kind=plugin, entrypoint_kind=skill, components.skills[]
mcp import        -> capability_kind=plugin, entrypoint_kind=mcp_server, components.mcp_servers[]
plugin import     -> capability_kind=plugin, entrypoint_kind=plugin, multiple components
subagent import   -> capability_kind=plugin, entrypoint_kind=subagent, components.agents[]
```

## 7. 实施切口

### 7.0 六步实施主线

这一轮的完整落地路径必须按六步推进，不能跳过 CC 语义，也不能先做一个 Hive 自己想象出来的 plugin runtime。

| 步骤 | 目标 | 核心产物 | 不能做的事 |
|---|---|---|---|
| 1. CC 语义对齐 | 先对齐 CC `/plugin`、marketplace、manifest、scope、enable/reload、context composition | `CCPluginAdapter` contract、scope mapping、component mapping、context snapshot dry-run tests | 不能先改 runtime；不能把 CC global install 解释成所有 agent 自动激活 |
| 2. Trust Gate 准入门 | 建立外部来源进入 Hive 前的统一门槛 | materializer sandbox、review record、`admission_class`、`governance_projection_json`、trusted snapshot | 不能做第二套运行时治理；不能让 source/marketplace 成为 trust root |
| 3. Component adapters 与入口收口 | Skill、MCP、CC/Codex Plugin、Subagent 都先 normalize 成 Hive component | `SkillSourceAdapter`、`MCPSourceAdapter`、`CCPluginAdapter`、`CodexPluginAdapter`、`LegacyPackAdapter` | 不能让 URL/ClawHub/skills.sh/MCP/plugin source 直接写 active surface |
| 4. Catalog / Activation / Runtime projection | Catalog 只表示可发现，Activation 才进入某个 agent/session | catalog entry、agent activation、session try activation、existing runtime surface projection | 不能把后台安装等同于所有 agent runtime 注入 |
| 5. 前后端产品面收敛 | Agent Detail 和 Workspace Settings 都围绕 Extensions / Catalog / Reviews / Factors 展开 | Agent Detail `能力 / Extensions`、Workspace `Marketplaces`、`External Reviews`、`Extension Catalog`、`Capability Factor Intake` | 不能继续让用户在 Tools/Skills/Subagents 三个旧入口里猜状态 |
| 6. Legacy migration 与生产验收 | 旧 pack/direct import 只保留迁移兼容，所有新入口走 Trust Gate | backfill report、legacy projection、dry-run sweep、targeted backend/frontend tests | 不能为了证明新系统正确而硬删旧能力；不能留下新的 Trust Gate bypass |

这六步与下面 Round 1-6 是同一条主线：

```text
Round 1 = 步骤 1：CC 对齐和 contract freeze
Round 2 = 步骤 2-3：Trust Gate substrate + adapters
Round 3 = 步骤 4-5 的 agent 侧：activation/read model/runtime projection + Agent Detail
Round 4 = 步骤 5 的 workspace 侧：Marketplace / External Reviews / Catalog
Round 5 = 步骤 5 的自进化侧：Capability Factor Intake
Round 6 = 步骤 6：legacy migration、runtime 收口、生产 dry-run
```

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
  "admission_class": "admin_scoped",
  "summary": "Skill contains scripts; activation requires admin approval and existing runtime governance projection."
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

Hive-native plugin：

- 不再以 Hive pack 为目标格式。
- 如需要 Hive 自有格式，应定义 `hive_extension_manifest`，字段贴近 normalized manifest，而不是复活 `pack.yaml`。
- 必须过 manifest validator、dependency lock、hook allowlist、admin approval。

CC/Codex plugin source：

- 先 detect manifest path：`.claude-plugin/plugin.json`、`.codex-plugin/plugin.json`，以及 marketplace manifest。
- 通过对应 adapter 生成 normalized manifest。
- 默认只允许可映射 component 进入 L2：Skill、MCP、Subagent/agent definition、safe metadata。
- commands 先进入 command compatibility report；只有能转成 Hive 既有 command/tool 调用且不执行任意 shell 的 command 才可激活。
- raw hooks 默认只生成 report；只有 allowlisted governed handler 才能激活。
- LSP、output styles、channels、install-time package scripts 默认进入 `unsupported_components`。
- 管理员审批时必须看到 compatibility report。

Legacy pack source：

- 只能从已存在的 `pack.yaml` / installed-plugin record backfill 成 review/snapshot。
- 不作为新 marketplace、用户上传、开发者文档的推荐格式。
- 不允许把 legacy pack 重新变成 runtime core gate。

### Step 5：UI / Control Plane

不做开放式 Skill 广场，但要做企业可控的目录和审批队列：

```text
Workspace Settings -> External Capability Reviews
Workspace Settings -> Extension Catalog
Workspace Settings -> Capability Factor Intake
Agent Detail -> Extensions
```

前端信息架构应遵循：

```text
企业后台负责治理和预置目录
Agent Detail 负责单个 employee 的实际安装和启用
```

不要把插件入口直接放成一个所有 agent 之上的全局首页。那会把“可发现目录”误读成“全局 runtime install”。Hive 的核心对象是 employee / agent，因此主要安装入口应在 Agent Detail。

当前 Agent Detail 里的 `tools`、`skills`、`subagents` 不应长期平行存在。目标应收敛为：

```text
Agent Detail -> 能力 / Extensions
  -> 已安装
  -> 可安装 / 插件库
  -> 审批中
  -> 自产候选
```

`已安装` 内部分组：

```text
MCP
Skills
Plugins / Bundles
Subagents
```

`可安装 / 插件库` 内部分来源：

```text
Platform provided
Workspace provided
Personal
External source
```

其中 `External source` 是提交 review 的入口，不是直接安装入口：

```text
GitHub URL
skills.sh
ClawHub
Smithery / MCP registry
CC marketplace
Codex/OpenAI marketplace
local upload
```

`自产候选` 显示这个 agent 自己产生的能力因子：

```text
Skill candidates
Subagent proposals
Workflow patterns
Tool/MCP usage patterns
```

Agent owner 在这里可以：

- 只在当前 agent 内试用。
- 申请进入 workspace review。
- 丢弃或归档候选。
- 查看自动审计报告。

但不能从这里绕过 enterprise approval 直接发布到 workspace catalog。

列表字段：

- kind
- name
- source
- requested by
- target agent(s)
- admission class / governance projection
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

Extension Catalog 必须分 scope tab，类似 Codex 插件页但语义更明确：

```text
Platform provided     # Hive / OpenAI / Codex curated source，经 Hive Trust Gate 固定 snapshot
Workspace provided    # 企业后台已审批并发布到 workspace catalog
Personal              # 用户个人提交并通过审批或仅自己可见的 catalog entry
```

每个 catalog item 展示：

- policy：requestable / approved_available / recommended / auto_activate_by_policy / mandatory / blocked
- compatibility level：L0-L4
- active agents count
- components：skills / MCP / apps / hooks / agents / commands / workflows
- required credentials
- admission class / governance projection
- last reviewed snapshot hash
- actions：request review、publish、install to employee、try in chat、disable、revoke

Agent Detail 的 Extensions 页面必须区分：

```text
Installed on this employee
Recommended for this employee
Available from workspace
Available from platform
Personal
```

默认只把 `Installed on this employee` 注入运行时；其他列表只作为可安装目录。

### Step 6：Legacy 收口与生产验收

实现：

- `TenantInstalledPlugin` / `AgentPluginAssignment` 只作为 legacy projection / migration backing store。
- `pack.yaml` 只由 `LegacyPackAdapter` 读取并生成 migration report，不作为新生态 manifest。
- direct import 入口没有 approved snapshot 时返回 `review_required`，不能写 active `skills/` / MCP assignment / plugin assignment。
- 跑生产 dry-run sweep：列出现有 Skill/MCP/Plugin/Extension 的来源、hash、backfill status、unknown/unsafe/legacy 项。
- targeted backend + frontend tests 全部通过后，才允许正式迁移。

## 8. 分轮落地计划

这不是一个单点功能。目标是把 Hive 的 Plugin 系统完整收敛为：

```text
CC-compatible discovery / manifest semantics
  -> Hive Trust Gate
  -> Hive Plugin/Extension Snapshot
  -> Catalog / Factor / Activation
  -> Agent runtime
```

每一轮都必须同时交付：

- 后端数据模型 / API / runtime。
- 前端入口 / 审批面 / Agent Detail 适配。
- 测试和迁移。
- 明确的 legacy bypass 收口。

### Round 1：全面对齐 CC Plugin 语义，冻结 Hive 目标契约

目标：

```text
先把“我们要兼容什么”讲清楚，再写实现。
```

这一轮不追求最终 activation 全链路上线，但必须交付一个可测试的 CC plugin adapter / context composition 契约。否则后续 Trust Gate、Catalog、Agent Detail 都会继续摇摆。

原子任务：

1. Source-check CC plugin baseline。
   - 逐项核对 CC marketplace source、plugin source、manifest fields、install scope、enabled state、managed policy、project/user/global 行为。
   - 明确 CC `project` -> Hive employee / agent activation。
   - 明确 CC `user` / `managed` -> Hive user / enterprise catalog availability。
   - 明确 CC local runtime 行为哪些不能照搬到 Hive 服务器。

2. Source-check Codex/OpenAI plugin baseline。
   - 核对 Codex plugin manifest、skills、mcpServers、apps、hooks、interface。
   - 明确 Codex UI 中 platform/workspace/personal 的 Hive 映射。
   - 明确 Codex `.claude-plugin/plugin.json` 兼容入口不等于 CC full behavioral parity。

3. 形成 Hive normalized manifest contract。
   - 定义 `NormalizedExternalPluginManifest`。
   - 定义 components：skills、mcp_servers、subagents、hooks、apps、commands、workflows。
   - 定义 unsupported_components 和 compatibility_level L0-L4。
   - 定义 `LegacyPackAdapter` 只能 migration-only。

4. 形成 scope contract。
   - Catalog availability != Agent activation。
   - Enterprise install 默认 publish-to-catalog，不自动进入所有 agent runtime。
   - `mandatory` / `auto_activate_by_policy` 必须有 selector。
   - `try in chat` 是 session-scoped activation。

5. 形成产品 IA contract。
   - Agent Detail 收敛为 `能力 / Extensions`。
   - 内部分为 `已安装`、`可安装 / 插件库`、`审批中`、`自产候选`。
   - Workspace Settings 分为 `Marketplaces`、`External Reviews`、`Capability Factor Intake`、`Extension Catalog`。

6. 形成自进化边界 contract。
   - Self-evolution v1 只针对 Hive/agent 自己生成的 Skill/Subagent 和 approved fork。
   - 外部 Skill/Plugin 默认不进入 self-evolution patch chain。
   - 外部能力可以使用、反馈、推荐、fork，但不能被隐式改写和再分发。

#### Round 1.1：CC Plugin 源码事实，作为实现基线

本轮实现必须先对齐 FreeCode/CC 的真实结构，而不是想象一个“plugin runtime”。

| CC 源码 | 事实 | 对 Hive 的约束 |
|---|---|---|
| `/Users/rocky243/vc-saas/free-code-main/src/types/plugin.ts:48` | `LoadedPlugin` 由 `manifest`、`path`、`source`、`enabled`、`commandsPath(s)`、`agentsPath(s)`、`skillsPath(s)`、`hooksConfig`、`mcpServers`、`settings` 等组成 | Hive adapter 的输入必须以 component container 处理，不允许把 plugin 当成单一 Skill 或单一 MCP |
| `/Users/rocky243/vc-saas/free-code-main/src/utils/plugins/schemas.ts:884` | `PluginManifestSchema` 合并 metadata、hooks、commands、agents、skills、outputStyles、channels、mcpServers、lspServers、settings、userConfig | Hive normalized manifest 必须覆盖这些字段，并把暂不支持的字段明确放进 `unsupported_components` |
| `/Users/rocky243/vc-saas/free-code-main/src/utils/plugins/pluginLoader.ts:1348` | `createPluginFromPath` 读取 `.claude-plugin/plugin.json`，并自动探测 `commands/`、`agents/`、`skills/`、`hooks/` 等目录 | Hive `CCPluginAdapter` 也要支持 manifest 声明和目录约定两种 component source |
| `/Users/rocky243/vc-saas/free-code-main/src/utils/plugins/mcpPluginIntegration.ts:132` | plugin MCP 可以来自 `.mcp.json`、manifest `mcpServers`、JSON path、MCPB path/URL、inline config | Hive 必须把 MCP 作为 plugin-provided MCP component 审核，不是重写 MCP runtime |
| `/Users/rocky243/vc-saas/free-code-main/src/utils/plugins/installedPluginsManager.ts:800` | `user` / `managed` scope 对当前 project relevant，`project` / `local` 需要 projectPath 命中 | Hive 不能把 enterprise/user availability 解释成所有 agent 自动 runtime 注入 |
| `/Users/rocky243/vc-saas/free-code-main/src/utils/plugins/refresh.ts:59` | `/reload-plugins` 刷新 commands、agents、hooks、plugin MCP reconnect key、AppState plugins | Hive 的“reload/activation refresh”只能刷新 active component projection，不能创建新 runtime |
| `/Users/rocky243/vc-saas/free-code-main/src/utils/messages/systemInit.ts:53` | system init 输出 `tools`、`mcp_servers`、`slash_commands`、`agents`、`skills`、`plugins` | Hive context composition 也要分 surface 组装，而不是把 plugin 整包塞进 prompt |

#### Round 1.2：CC -> Hive 字段级映射

| CC 字段 / 目录 | Hive normalized component | Hive runtime surface | Round 1 支持级别 |
|---|---|---|---|
| `.claude-plugin/plugin.json` metadata | `NormalizedExternalPluginManifest.metadata` | catalog/review/read model metadata | L2：解析、hash、provenance、report |
| `commands/` / manifest `commands` | `components.commands[]` | Hive command registry / slash command read model；执行仍走既有 tool/runtime | L1/L2：先展示和 compatibility report；不执行任意 shell |
| `skills/` / manifest `skills` | `components.skills[]` | existing Skill loader / `load_skill`；scripts 仍走 `run_skill_tool` 或 governed code execution | L2：可 stage/analyze；approve 后进入 Skill catalog/activation |
| `agents/` / manifest `agents` | `components.subagents[]` | existing subagent definition store / `spawn_subagent` / `delegate_to_agent` | L2：映射为 subagent definition，不映射为数字员工 employee |
| `hooks/hooks.json` / manifest `hooks` | `components.hooks[]` | existing hook dispatcher / `PluginHookRegistration` 等价新投影 | L1：只生成 report；approve 需要 allowlist handler |
| `.mcp.json` / manifest `mcpServers` / MCPB | `components.mcp_servers[]` | existing MCP registry/client/tool runtime + credential handles | L2：review 后投影到现有 MCP server/assignment |
| `userConfig` | `activation_config_schema` + `credential_requirements[]` | activation prompt / secrets provider / credential handle | L2：敏感值不得进 manifest/report/API 明文 |
| manifest `settings` | `components.settings[]` | allowlisted agent setting overlay | L1：仅允许白名单字段，默认不生效 |
| `outputStyles` | `unsupported_components.output_styles[]` | 无 Hive runtime surface | L0：记录但不激活 |
| `lspServers` | `unsupported_components.lsp_servers[]` | 无 Hive server-side LSP runtime | L0：记录但不激活 |
| `channels` | `unsupported_components.channels[]` 或后续 MCP channel adapter | Hive channel runtime 尚未定义 plugin channel binding | L0/L1：记录并标注需要单独设计 |

#### Round 1.3：CC Scope -> Hive Scope 映射

| CC scope / state | CC 含义 | Hive 映射 | 是否改变 agent runtime |
|---|---|---|---:|
| `user` install | 对当前用户所有 relevant project 可见 | user-level catalog availability / personal plugin source | 否，除非某个 agent 显式 activation |
| `managed` install | 组织托管安装，对 project relevant | enterprise catalog policy：mandatory/default/recommended/blocked | 只有 mandatory/default 且 selector 命中才改变 |
| `project` install | 当前 project 启用 | agent/employee activation | 是，只影响该 agent |
| `local` install | 当前本地 project/dev 环境 | dev/local draft 或 session-scoped try activation | 生产默认否 |
| `enabledPlugins` | 启用列表 | `external_extension_activations` / session try activation | 取决于 activation scope |
| disabled plugin | 安装但不启用 | catalog entry / disabled activation | 否 |
| `/reload-plugins` | 刷新 active component state | 重新 resolve agent/session active component projection | 只刷新可见面，不改 runtime 语义 |

关键解释：

```text
CC project == Hive agent / employee activation
CC user/managed == Hive availability / catalog policy
Hive enterprise install != all agents runtime install
```

#### Round 1.4：Hive Context Composition 目标形态

Hive 不能把 plugin 整包塞进 prompt，也不能另起 runtime。每次 agent invocation 前应组装一个 `AgentExtensionContextSnapshot`，把 active components 分发到已有 surface：

```text
AgentExtensionContextSnapshot
  agent_id
  session_id?
  enabled_plugins[]              # name/source/hash/version, for init/debug/audit
  active_components[]
    - kind                       # skill | mcp_server | subagent | hook | command | app | workflow
    - component_id
    - snapshot_id
    - source_ref
    - content_sha256
    - runtime_surface            # load_skill | tool_search_mcp | subagent_definition | hook_registry | command_registry
    - activation_scope           # agent | session | enterprise_policy
    - permission_requirements[]
    - credential_handles[]
  unsupported_components[]
  audit_refs[]
```

组装路径：

```text
external_extension_catalog_entries
  + trusted_capability_snapshots
  + external_extension_activations
  + session-scoped try activations
  + legacy projection
    -> AgentExtensionContextSnapshot
    -> existing runtime surfaces
```

具体投影：

| Snapshot component | 投影目标 | 上下文里如何出现 |
|---|---|---|
| Skill | Workspace/approved Skill registry | skills catalog 只出现名称/描述；正文仍由 `load_skill` progressive disclosure 加载 |
| MCP server | Tenant MCP server + Agent MCP assignment | MCP tools 默认 deferred，通过 `tool_search` 发现；`always_load` 才进入当前 tool schema |
| Subagent | Agent-scoped subagent definition projection | subagent listing 出现 definition；执行仍走 `spawn_subagent` / `delegate_to_agent` |
| Hook | Hook registration projection | hook registry 只注册 approved + allowlisted handler |
| Command | Command registry projection | slash command list 出现 command；执行必须转成既有 command/tool 调用 |
| Plugin metadata | init/debug/audit metadata | system/init 或 read model 展示 name/source/hash，不直接提供执行权 |

这和 CC `buildSystemInitMessage` 的组成关系一致：`tools`、`mcp_servers`、`slash_commands`、`agents`、`skills`、`plugins` 分开进入上下文，plugin 本身只是 provenance/debug/audit 元数据。

#### Round 1.5：第一步真实实施切口

第一步不是改 runtime，而是实现一个可测试的 CC plugin adapter 和 context composer dry-run。

新增文件：

```text
backend/app/services/plugin_adapters/__init__.py
backend/app/services/plugin_adapters/types.py
backend/app/services/plugin_adapters/cc_plugin_adapter.py
backend/app/services/agent_extension_context.py
backend/tests/services/test_cc_plugin_adapter.py
backend/tests/services/test_agent_extension_context.py
```

TDD 原子任务：

1. `test_cc_plugin_adapter_parses_manifest_and_standard_dirs`
   - 构造含 `.claude-plugin/plugin.json`、`commands/`、`agents/`、`skills/`、`hooks/hooks.json`、`.mcp.json` 的 fixture。
   - 期望 adapter 输出 metadata、components、unsupported_components、content hashes、source refs。
   - 期望不执行任何 install script / shell command。

2. `test_cc_plugin_adapter_maps_unsupported_components_explicitly`
   - manifest 含 `lspServers`、`outputStyles`、`channels`。
   - 期望全部进入 `unsupported_components`，并带 `reason`。

3. `test_cc_plugin_adapter_maps_user_config_to_credential_requirements`
   - manifest 含 `userConfig.sensitive=true`。
   - 期望输出 credential requirement，不在 report 中暴露 secret value。

4. `test_agent_extension_context_projects_components_to_existing_surfaces`
   - 给定 approved snapshot + agent activation。
   - 期望 Skill -> `load_skill` surface、MCP -> existing MCP surface、Subagent -> existing subagent surface、Hook -> existing hook registration surface、Command -> command registry surface。
   - 期望不会创建新的 runtime executor。

5. `test_agent_extension_context_does_not_treat_enterprise_catalog_as_activation`
   - 给定 enterprise catalog entry 但无 agent activation。
   - 期望它只出现在 available/recommended，不进入 active runtime surface。

第一步完成后，才进入 Round 2 的 Trust Gate 总账和 DB migration。也就是说，Round 1 的代码产物是：

```text
CC plugin input
  -> normalized manifest
  -> component compatibility report
  -> context projection dry-run
```

不是：

```text
CC plugin input
  -> direct install
  -> direct runtime mutation
```

#### Round 1.6：CC 对齐硬约束补齐

下面这些约束是为了防止后续实现走偏。它们不是“安全增强项”，而是 CC plugin 语义对齐的组成部分：先按 CC 的 marketplace / install / enable / reload / context composition 机制建模，再把 Hive 的 Trust Gate 和企业治理加在进入 active runtime 之前。

1. Marketplace 与 `/plugin` 的存在方式。
   - CC 的 `/plugin` 是一个本地插件管理入口，负责 Discover、Installed、Marketplaces、Errors、install、enable、disable、update、validate 等操作，不是一个新的 runtime。
   - Marketplace 是 source registry + cache。它回答“哪里有插件”和“插件 manifest 是什么”，不回答“是否可信”和“是否已经进入某个 agent runtime”。
   - Hive 的 Marketplace source 只能进入发现层和 review 队列。Market entry 不能直接写 agent workspace、不能直接写 active Skill/MCP/Subagent/Hook、不能直接成为 company catalog 权威记录。

2. 云端 Git / materializer 边界。
   - CC marketplace 会使用 Git clone / pull / sparse checkout / ref pin 等机制物化 marketplace 和 plugin source。Hive 可以对齐这个语义，但 Git 只能存在于 Trust Gate materializer worker / sandbox / sidecar 中。
   - Git 不能在 Agent runtime、ToolRuntimeService、普通 web chat worker、agent workspace HOME 中执行。agent 不能通过安装 plugin 间接获得 host HOME、host secrets、SSH agent、全局 git config。
   - Materializer 必须记录 `source_ref`、`resolved_ref`、`commit_sha`、`content_sha256`、`lockfile`、`materializer_image`、`sandbox_report`、`scan_report`。
   - 如果云端 materializer 没有 Git 或 source 拉取失败，状态必须是 `materialize_failed` / `review_required`，不能 fallback 到 runtime 直接拉取。
   - 实现选择可以是：在专用 materializer 镜像内预装 Git，或使用隔离的 fetcher sidecar；不能让 agent runtime 镜像为了 plugin install 暴露通用 Git 执行面。

3. 上下文与 Tool Discovery 进入方式。
   - CC install / enable 不会把 plugin 整包塞进 prompt，也不会让 manifest 直接定义 tool executable schema。reload 后会把 active components 分别刷新到 commands、agents、skills、hooks、MCP reconnect state、plugin metadata。
   - Hive 必须对齐为 `AgentExtensionContextSnapshot`：active plugin 只产出分 surface 的 projection。Skill 进入 Skill surface，MCP 进入 MCP surface，Subagent 进入 subagent definition surface，Hook 进入 hook registry，Command 进入 command registry。
   - `tool_search` / deferred tool discovery 只能看到已经通过 activation projection 的工具或 MCP tool。未审批 marketplace entry、catalog-only entry、pending review entry 不能进入 tool discovery。

4. 与原生工具的隔离。
   - Plugin-provided command、skill、subagent 默认必须命名空间化，例如 `pluginName:commandName`、`pluginName:skillName`、`pluginName:agentName`。这用于避免覆盖原生命令、内置 Skill、内置 Subagent。
   - Plugin-provided MCP 与手动配置的 MCP 冲突时，手动配置 / 原生配置优先；plugin MCP 被 suppress 或要求用户显式处理冲突。
   - 所有 plugin component 都必须带 `source=plugin`、`plugin_id`、`snapshot_id`、`component_id`、`content_sha256`。Plugin metadata 可以进入 init/debug/audit read model，但 metadata 本身不授予执行权。
   - Legacy pack compatibility layer 可以继续作为迁移 backing store，但不能以 `pack` / `package` 字段出现在新 `/agents/{agent_id}/extensions` 产品面，也不能作为新外部能力的 activation authority。

5. 权限机制与 Agent 发现机制。
   - Source policy 先于 materialize：blocked marketplace / repo / URL / npm package 不得进入拉取和分析阶段。
   - Plugin policy 先于 enable：blocked plugin、blocked dependency、blocked transitive dependency 不得进入 activation。
   - Plugin agent / subagent frontmatter 中会扩大权限的字段必须降权处理：`permissionMode`、`hooks`、`mcpServers` 不能被第三方 plugin agent 静默继承。它们只能进入 compatibility report，由 Trust Gate / admin policy 显式批准并投影到对应 surface。
   - Plugin command 的 `allowed-tools`、shell interpolation、argument expansion、model/effort 等字段必须进入 command compatibility report。无法转换成 Hive 既有 governed command/tool 调用的命令，不得激活。
   - `userConfig` / secret / credential 字段必须映射为 credential requirements 和 credential handles。敏感值不得写入 manifest report、prompt、API response、audit 文本或 snapshot 明文。
   - Agent 发现 plugin 能力的唯一方式是 active projection：slash command list、Skill list、Subagent list、MCP deferred tools、hook lifecycle registry。Catalog/recommended/requestable 只是可安装目录，不是 runtime discovery。

6. 更新机制。
   - 必须区分三件事：marketplace refresh、snapshot update review、agent activation reload。
   - Marketplace refresh 只更新 source cache 和 available versions，不改变 approved snapshot，也不改变任何 agent runtime。
   - Snapshot update 必须重新 materialize、hash、scan、compatibility report、approve。不能因为同名 plugin 已审批过，就自动信任新 commit / 新 tarball / 新 npm version。
   - Agent activation reload 只重新 resolve 已批准 snapshot 到 active component projection，等价于 CC `/reload-plugins` 的 component refresh 语义；它不是 runtime 重构，也不是重新审批。
   - Update 后旧 snapshot 必须可追溯、可撤销、可回滚；revoke 新 snapshot 不应破坏旧 snapshot 的审计证据。

补充 TDD 原子任务：

6. `test_cc_plugin_adapter_namespaces_commands_skills_and_agents`
   - 构造 command、skill、agent 与原生名称冲突的 plugin fixture。
   - 期望输出 `pluginName:*` 命名空间，并保留原始名称用于 audit。

7. `test_cc_plugin_adapter_ignores_plugin_agent_permission_escalation_fields`
   - plugin agent frontmatter 包含 `permissionMode`、`hooks`、`mcpServers`。
   - 期望这些字段进入 compatibility warning / permission requirements，不进入 active subagent definition。

8. `test_cc_plugin_adapter_requires_dependency_closure_report`
   - plugin manifest 声明 dependency / transitive dependency。
   - 期望 adapter 或 review report 输出 dependency closure；blocked dependency 会阻止 activation。

9. `test_cc_plugin_adapter_maps_sensitive_user_config_to_credential_handles`
   - plugin manifest 声明 sensitive `userConfig`。
   - 期望 report 只有 credential requirement，不包含 secret value。

10. `test_agent_extension_context_excludes_unapproved_marketplace_entries_from_tool_discovery`
    - 给定 marketplace available entry 但没有 approved snapshot / activation。
    - 期望 `tool_search`、MCP tools、skills、commands、subagents 都不可见。

11. `test_agent_extension_context_distinguishes_marketplace_refresh_snapshot_update_and_activation_reload`
    - marketplace source 发现新版本。
    - 期望只更新 available version；未重新 approve 前不改变 agent active projection。

12. `test_materializer_requires_git_in_sandbox_not_runtime`
    - 模拟 Git source materialize。
    - 期望 Git 只在 materializer boundary 执行，runtime projection 中没有 Git command execution 权限。

Round 1 完成标准：

- 文档列出 CC/Codex/Hive 的字段级映射表。
- 文档列出 CC scope 到 Hive catalog/activation 的映射表。
- 文档列出所有 unsupported CC/Codex component 的处理方式。
- 文档列出 CC marketplace、`/plugin`、Git materializer、reload/update、context composition 的 Hive 对齐语义。
- 后端有 `CCPluginAdapter` 的 fixture tests，能解析 CC manifest + standard dirs。
- 后端有 `AgentExtensionContextSnapshot` dry-run tests，证明 component 只投影到现有 runtime surfaces。
- 后端有 namespace、permission downgrade、dependency closure、credential handle、marketplace refresh vs activation reload 的 tests。
- 后续实现必须以这些 contract 为测试依据，不再沿用 `pack.yaml` 作为新生态目标。

Round 1 实装证据（2026-07-07）：

- 新增 `backend/app/services/external_capabilities/cc_plugin_adapter.py`，只负责把 CC plugin 目录归一化为 Hive component report，不写 DB、不安装 active runtime、不调用 `ToolRuntimeService`。
- 新增 `backend/app/services/external_capabilities/context_projection.py`，输出 CC-shaped context payload：`tools`、`mcp_servers`、`slash_commands`、`agents`、`skills`、`plugins`、`hooks`，并保持 native tools 与 plugin components 分离。
- 新增 `backend/tests/services/test_external_cc_plugin_adapter.py`：
  - 验证 `commands/`、`skills/`、`agents/`、`hooks/hooks.json`、`.mcp.json` 被 namespace 成 `plugin:component`。
  - 验证 plugin agent frontmatter 里的 `permissionMode`、`hooks`、`mcpServers` 被忽略并记录，不进入 runtime projection。
  - 验证 `userConfig` 转成 credential requirement，`lspServers` / `outputStyles` 标记为 unsupported。
  - 验证 manifest component path traversal 被 admission note 捕获，不读取插件根目录外文件。
- 新增 `backend/tests/services/test_agent_extension_context_projection.py`：
  - 验证 native tool names 不会和 plugin component names 混合。
  - 验证 dry-run payload 对齐 CC system init 的组件分组。
- 验证命令：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_external_cc_plugin_adapter.py \
  tests/services/test_agent_extension_context_projection.py -q
# 3 passed in 0.07s

cd backend && source .venv/bin/activate && ruff check \
  app/services/external_capabilities \
  tests/services/test_external_cc_plugin_adapter.py \
  tests/services/test_agent_extension_context_projection.py
# All checks passed!
```

### Round 2：后端 canonical substrate，建立 Trust Gate 与 Snapshot 总账

目标：

```text
让所有外部能力先进入统一后端总账，而不是继续分散写 Skill/MCP/Plugin 表。
```

原子任务：

1. 新增 canonical models / migrations。
   - `external_capability_reviews`
   - `trusted_capability_snapshots`
   - `external_extension_catalog_entries`
   - `external_extension_activations`
   - `external_extension_components`
   - `external_extension_hook_registrations`

2. 新增 Trust Gate service。
   - stage。
   - materialize。
   - analyze。
   - approve / reject。
   - publish-to-catalog。
   - activate。
   - revoke。

3. 新增 artifact/quarantine storage。
   - artifact path。
   - content hash。
   - resolved ref。
   - lockfile。
   - scan report。
   - smoke test report。

4. 新增 adapters。
   - `SkillSourceAdapter`
   - `MCPSourceAdapter`
   - `CCPluginAdapter`
   - `CodexPluginAdapter`
   - `HiveExtensionManifestAdapter`
   - `LegacyPackAdapter`

5. 收口外部 install-time execution。
   - `npx skills add` 只能在 materializer sandbox。
   - remote npm/git/url/plugin source 只能 stage/analyze。
   - raw subprocess / host HOME / host secrets 禁止进入 materializer。

6. 建立 component compatibility report。
   - Skill/MCP/interface 可先做到 L2。
   - hooks/LSP/local commands 默认 unsupported。
   - apps 只有 Hive 有 connector runtime 才可 activate。

7. 建立 backend API。
   - `/enterprise/external-capabilities/stage`
   - `/enterprise/external-capabilities/reviews`
   - `/enterprise/external-capabilities/reviews/{id}/analyze`
   - `/enterprise/external-capabilities/reviews/{id}/approve`
   - `/enterprise/external-capabilities/reviews/{id}/publish-to-catalog`
   - `/agents/{agent_id}/extensions/{catalog_entry_id}/activate`
   - `/agents/{agent_id}/extensions/{catalog_entry_id}/try`

Round 2 完成标准：

- 外部 Skill / MCP / Plugin 都能 stage -> review -> snapshot。
- 没有 approved snapshot 不能 activation。
- `Trusted Snapshot` 可以追溯 source_ref、resolved_ref、content_sha256、review_id。
- legacy direct import API 开始返回 `review_required` 或投影到 Trust Gate，不再直接写 active runtime。
- 后端 targeted tests 覆盖 state machine、hash、scan、reject、revoke。

Round 2 substrate 实装证据（2026-07-07）：

- 新增 `backend/app/models/external_capability.py`：
  - `ExternalCapabilityReview`：外部能力进入 Hive 前的 staged review 记录。
  - `ExternalCapabilitySnapshot`：approve 后产生的 approved snapshot；后续 activation 只能引用 snapshot，不引用 raw source。
- 新增迁移 `backend/alembic/versions/external_capability_trust_gate_0707.py`：
  - 创建 `external_capability_reviews` / `external_capability_snapshots`。
  - 两张表都带 `tenant_id`、索引、唯一约束和 tenant RLS policy。
- 新增 `backend/app/services/external_capabilities/trust_gate.py`：
  - `stage_external_capability_review()`：根据 normalized bundle 生成 `admission_class`、`admission_report_json`、`governance_projection_json`，但不做 activation。
  - `approve_external_capability_snapshot()`：只从 non-blocked review 生成 approved snapshot；blocked review 不能 approve。
  - `list_external_capability_reviews()`：workspace admin 可查看 review queue。
- 新增 `backend/app/api/external_capabilities.py` 并接入 `backend/app/main.py`：
  - `GET /enterprise/external-capabilities/reviews`
  - `POST /enterprise/external-capabilities/reviews`
  - `POST /enterprise/external-capabilities/reviews/{review_id}/approve`
- 新增测试：
  - `backend/tests/services/test_external_capability_trust_gate.py`
  - `backend/tests/api/test_external_capability_reviews_api.py`
- 验证命令：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_external_capability_trust_gate.py \
  tests/api/test_external_capability_reviews_api.py -q
# 5 passed in 0.31s

cd backend && source .venv/bin/activate && ruff check \
  app/models/external_capability.py \
  app/services/external_capabilities/trust_gate.py \
  app/api/external_capabilities.py \
  app/main.py \
  tests/services/test_external_capability_trust_gate.py \
  tests/api/test_external_capability_reviews_api.py \
  alembic/versions/external_capability_trust_gate_0707.py
# All checks passed!

cd backend && source .venv/bin/activate && alembic heads
# external_capability_trust_gate_0707 (head)
```

Round 2 external Skill entrypoint 收口证据（2026-07-07）：

- 新增 `backend/app/services/external_capabilities/skill_source_adapter.py`：
  - 把 external Skill package 归一化为 one-component `NormalizedExternalPluginBundle`。
  - 继续复用 `SkillGuard`，但扫描结果进入 Trust Gate `admission_report_json`。
  - `skill_guard_blocked` / `missing_skill_md` 会映射为 `admission_class=blocked`，不会写 active workspace。
- 收口入口：
  - `backend/app/api/files.py`：agent 级 GitHub URL / ClawHub import 改为 `stage_external_skill_package_review()`。
  - `backend/app/api/skills.py`：company/global registry 的 URL / ClawHub import 改为 Trust Gate review，不再直接 `_save_skill_to_db`。
  - `backend/app/tools/handlers/hr.py`：HR external skill URL、skills.sh ref、ClawHub post-commit install 改为 review_required / blocked 状态记录。
  - `backend/app/services/agent_tool_domains/code_exec.py`：sandbox HOME 里的 `.agents/skills` 只 stage review，不再 promote 到 active `skills/`。
  - `backend/app/services/mcp_prompt_trust.py` + `backend/app/tools/handlers/mcp.py`：MCP prompt `import_as_skill` 改为 review_required。
- 保留的 `install_active_skill_package()` 调用经扫描确认属于内部路径：
  - registry skill copy：`backend/app/api/files.py`、`backend/app/api/agents.py`、`backend/app/tools/handlers/hr.py`。
  - platform seeding / agent manager / reuse / skill distiller：`agent_seeder.py`、`skill_seeder.py`、`agent_manager.py`、`capability_reuse_service.py`、`skill_distiller.py`。
  - 这些路径不是新的外部 source bypass；后续 activation layer 会让 approved snapshot 也投影到同一 existing skill loader。
- 验证命令：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/api/test_files_import_idempotency.py \
  tests/api/test_skills_skill_guard.py \
  tests/tools/test_hr_handler.py::test_append_hr_creation_t0_event_records_source_attributed_creation_case \
  tests/tools/test_hr_handler.py::test_install_external_skill_from_url_stages_review_without_active_install \
  tests/tools/test_hr_handler.py::test_install_external_skill_from_skills_ref_stages_review_without_active_install \
  tests/tools/test_hr_handler.py::test_install_external_skill_from_skills_ref_fails_closed_without_sandbox \
  tests/services/test_command_tooling.py::test_execute_code_stages_sandbox_installed_skill_review_without_activation \
  tests/services/test_mcp_prompt_trust.py \
  tests/services/test_external_capability_trust_gate.py -q
# 15 passed, 3 warnings in 1.76s

cd backend && source .venv/bin/activate && ruff check \
  app/services/external_capabilities/skill_source_adapter.py \
  app/services/external_capabilities/trust_gate.py \
  app/api/files.py app/api/skills.py app/tools/handlers/hr.py \
  app/services/agent_tool_domains/code_exec.py app/services/mcp_prompt_trust.py app/tools/handlers/mcp.py \
  tests/api/test_files_import_idempotency.py tests/api/test_skills_skill_guard.py \
  tests/tools/test_hr_handler.py tests/services/test_command_tooling.py \
  tests/services/test_mcp_prompt_trust.py tests/services/test_external_capability_trust_gate.py
# All checks passed!

rg -n "install_mcp_prompt_as_skill|Installed ClawHub skill|downloaded_to_agent|hr_clawhub|agent_clawhub|SkillGuard blocked sandbox-installed skill|MCP Prompt Skill Installed" backend/app backend/tests
# no matches
```

Round 2 standalone MCP import 收口证据（2026-07-07）：

- 新增 `backend/app/services/external_capabilities/mcp_source_adapter.py`：
  - standalone MCP import 被归一化为 one-component `mcp_server` bundle。
  - 复用 `mcp_authz`：禁止 URL userinfo、OAuth/user token query、local-only transport。
  - `api_key` / `apiKey` 只转为 credential requirement，不进入 normalized manifest。
- 修改 `backend/app/api/mcp_servers.py`：
  - `POST /enterprise/mcp-servers/import` 不再调用 `import_and_register()` 直接创建 `TenantMCPServer`。
  - 入口改为 `stage_external_mcp_server_review()`，返回 `review_required` / `blocked`。
  - 既有 MCP runtime、assignment、tool policy API 保持不变；approved snapshot activation 后再投影到这些现有 surface。
- legacy plugin install 现状：
  - `backend/app/services/plugin_install_service.py` 已有 source policy fail-closed：remote source 不能作为 legacy pack 直接安装。
  - 因此本轮没有把 legacy pack install 改成 CC plugin runtime；它只保留 migration-compatible projection。
- 验证命令：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/api/test_mcp_servers_api.py::test_import_route_stages_trust_gate_review \
  tests/api/test_mcp_servers_api.py::test_import_route_maps_value_error_to_400 \
  tests/services/test_external_mcp_source_adapter.py -q
# 4 passed in 0.29s

cd backend && source .venv/bin/activate && pytest \
  tests/services/test_mcp_server_service.py::test_import_and_register_rejects_local_only_transport_before_db_query \
  tests/services/test_mcp_server_service.py::test_get_agent_extensions_has_both_keys \
  tests/services/test_mcp_server_service.py::test_get_agent_extensions_includes_installed_skill_records \
  tests/services/test_mcp_server_service.py::test_get_agent_extensions_includes_agent_plugin_assignments -q
# 4 passed in 0.15s

cd backend && source .venv/bin/activate && ruff check \
  app/services/external_capabilities/mcp_source_adapter.py \
  app/api/mcp_servers.py \
  tests/api/test_mcp_servers_api.py \
  tests/services/test_external_mcp_source_adapter.py
# All checks passed!

rg -n "import_and_register" backend/app/api/mcp_servers.py backend/tests/api/test_mcp_servers_api.py
# no matches
```

### Round 3：Agent runtime visibility / activation layer 与 Agent Detail 前端收敛

目标：

```text
让一个 employee/agent 的真实能力可见面统一从 /agents/{agent_id}/extensions 读取和管理。
```

这里的 `runtime activation` 不是重写整个 Agent runtime，也不是为了插件另造一个独立 runtime。它指的是 AgentKernel / ToolRuntimeService / MCP / Skill / Subagent 这些既有 runtime 之前的一层“可见性和激活解析层”。

这层必须对齐 CC 的 scope / enable / load 语义：

| CC 语义 | Hive 对齐方式 |
|---|---|
| `user` / `managed` install 对当前 project relevant | user / enterprise catalog availability 对当前 employee 可见，但不自动 runtime 注入。 |
| `project` install / projectSettings enabled plugin | employee / agent activation。 |
| enabled plugin 才加载 commands / skills / agents / hooks / MCP 等 component | active activation 才暴露 skills / MCP / plugins / subagents / hooks 给既有 runtime。 |
| `/reload-plugins` 之后重新加载插件可见面 | Hive 通过 activation state / session state 刷新 `/agents/{agent_id}/extensions` 和 runtime context。 |

对齐的是 CC 的语义，不是照搬 CC 的本地实现。Hive 需要多出 tenant/RLS、审批、审计、catalog policy、session-scoped try activation、component-level permissions。

不属于这一轮的范围：

- 不重写 AgentKernel LLM loop。
- 不重写 ToolRuntimeService 的治理执行。
- 不重写 web chat / RuntimeTask 调度。
- 不重写 MCP client runtime。
- 不重写 Subagent 执行引擎。

这一轮要重构的是：

```text
Catalog / Snapshot / Assignment
  -> resolve active components for this agent/session
  -> expose skills / MCP / plugins / subagents / hooks to existing governed runtimes
```

原子任务：

1. 重构 `/agents/{agent_id}/extensions`。
   - 返回 active components。
   - 返回 available/recommended catalog delta。
   - 返回 pending reviews / pending activations。
   - 返回自产候选摘要。
   - 不暴露 legacy pack 字段。

2. 实现 component-level activation。
   - 一个 plugin 可以只启用 skills。
   - 一个 plugin 可以只启用 MCP servers。
   - hooks 必须单独审批。
   - credentials 必须绑定 approved credential handle。

3. 重构 Agent Detail 前端。
   - 将 `tools`、`skills`、`subagents` 收敛为 `能力 / Extensions`。
   - 二级 tab：`已安装`、`可安装`、`审批中`、`自产候选`。
   - 已安装分组：MCP、Skills、Plugins/Bundles、Subagents。
   - 可安装分来源：Workspace、Platform、Personal、External source。

4. 实现 agent activation 操作。
   - install to employee。
   - disable。
   - revoke view。
   - try in chat。
   - tool/MCP per-tool policy。
   - credential binding prompt。

5. 保留 runtime governance。
   - MCP tool policy 仍由 `AgentMCPServerAssignment` / tool policy 等价新表控制。
   - ToolRuntimeService 不因 plugin manifest 绕过 governance。
   - hooks 只走 allowlist handler。

#### Round 3 补充：Default / Optional Skill 与 Subagent 前端模型

Round 3 Skill activation substrate 实装证据（2026-07-07）：

- 新增 `backend/app/services/external_capabilities/activation.py`：
  - `activate_external_extension_for_agent()` 只接受 `status=approved` 的 snapshot。
  - Skill component 激活时调用既有 `install_active_skill_package()`，不改 Skill loader/runtime。
  - 激活结果写入 `external_extension_activations`，保留 agent、snapshot、component type、activation result 和 user 证据。
- 修改 `backend/app/services/external_capabilities/skill_source_adapter.py`：
  - external Skill review manifest 现在携带 `metadata.files` artifact，保证 approved snapshot 可回放安装。
- 新增迁移 `backend/alembic/versions/external_extension_activation_0707.py`：
  - 创建 `external_extension_activations`，带 tenant RLS policy。
- 新增 API：
  - `POST /agents/{agent_id}/external-extensions/{snapshot_id}/activate`
  - route 先走 `check_agent_access()`，再按 agent workspace 激活 approved snapshot。
- 验证命令：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_external_capability_trust_gate.py::test_stage_external_skill_package_review_maps_skill_guard_block_to_blocked_review \
  tests/services/test_external_capability_activation.py \
  tests/api/test_external_capability_activation_api.py -q
# 3 passed in 0.37s

cd backend && source .venv/bin/activate && ruff check \
  app/models/external_capability.py \
  app/services/external_capabilities/activation.py \
  app/services/external_capabilities/skill_source_adapter.py \
  app/api/external_capabilities.py \
  tests/services/test_external_capability_activation.py \
  tests/api/test_external_capability_activation_api.py \
  tests/services/test_external_capability_trust_gate.py \
  alembic/versions/external_extension_activation_0707.py
# All checks passed!

cd backend && source .venv/bin/activate && alembic heads
# external_extension_activation_0707 (head)
```

当前实现只能算有基础，不算完善：

- Skill 侧已有 agent 已安装 Skill 读模型、preset import、URL import、ClawHub import 的入口，但它还是 Skill 单独入口，不是统一 Catalog / Activation 模型。
- Subagent 侧已有 builtin / tenant / agent 三层定义管理，也能在 Agent Detail 中生成、编辑、fork subagent definition，但 tenant definition 现在更接近“企业定义库可见”，不是“可选能力待安装”。
- 因此，如果后台直接添加 10 个 Skill 和 100 个 Subagent，目标状态不应该是它们全部进入每个 agent runtime；目标状态应该是它们进入 workspace catalog，用户在某个 employee/agent 的 `能力 / Extensions` 页面按需安装。

新的能力分类必须统一成：

| 分类 | 含义 | 是否默认进入 agent runtime | 前端位置 |
|---|---|---:|---|
| `mandatory` | 企业强制能力，通常用于合规、安全、审计 | 是，按 selector 自动激活 | `已安装`，标记为企业强制 |
| `default` | agent 模板或企业策略默认启用能力 | 是，但必须有 agent/template/policy selector | `已安装`，标记为默认启用 |
| `recommended` | 企业推荐能力 | 否 | `可安装` 的推荐区 |
| `optional` | 企业或平台预置但按需安装 | 否 | `可安装` 主列表 |
| `requestable` | marketplace 可见但未过企业审批 | 否 | `可安装`，动作是提交审核 |
| `blocked` | 企业禁用能力 | 否 | 管理端可见，agent 端只在必要时展示不可安装原因 |

Subagent 必须和 Skill 走同一个 default/optional 逻辑：

```text
Workspace / Platform / Personal Catalog
  -> catalog entry: component_kind = skill | subagent | mcp | plugin
  -> policy = mandatory | default | recommended | optional | requestable | blocked
  -> agent activation only when selected by selector or user install
  -> runtime resolver exposes active subagents/skills to existing runtime
```

Subagent 的落地规则：

1. 后台新增的 100 个 subagent 默认只是 `optional` catalog entries，不写入每个 agent 的 active subagent list。
2. 默认的 3 个 subagent 应该表达为 `default` policy，并带 selector，例如 all agents、某些 employee template、某些 department、某些 role。
3. 用户在 Agent Detail 里点击“安装到此员工”时，只创建 `agent activation`，不要复制 definition。
4. 用户点击“自定义 / Fork”时，才从 trusted snapshot 派生 agent-local definition 或 fork candidate。
5. 已激活 subagent 才能出现在 delegation/spawn subagent 可用列表；catalog-only subagent 只能出现在可安装列表。
6. 过期、撤销或被企业禁用的 subagent 必须从 active resolver 中移除，并在 Agent Detail 展示原因。

前端形态建议：

```text
Agent Detail
  -> 能力 / Extensions
     -> 已安装
        -> Skills
        -> Subagents
        -> MCP
        -> Plugins
     -> 可安装
        -> 搜索框
        -> Type filter: All / Skills / Subagents / MCP / Plugins
        -> Source filter: Platform / Workspace / Personal / Marketplace
        -> Policy filter: Default / Recommended / Optional / Requestable
        -> Category / role tags
        -> detail drawer
     -> 审批中
     -> 自产候选
```

当列表里有 10 个 skill 和 100 个 subagent 时，前端不能用“全部卡片平铺”作为唯一体验。必须支持：

- 搜索：name、description、tag、author、source。
- 类型筛选：Skill / Subagent / MCP / Plugin。
- 场景筛选：research、coding、sales、ops、finance、support、自定义 tag。
- 状态筛选：已安装、推荐、可选、需审批、被禁用。
- 详情抽屉：能力说明、权限、工具/MCP 依赖、来源、审计状态、版本、安装影响。
- 批量动作：选择多个 optional subagent 安装到当前 agent，或加入某个 employee template。
- 只显示 active runtime 数量，避免用户误以为 catalog 中 100 个 subagent 已经全部加载进当前 agent。

推荐的 UI copy：

| 场景 | 文案 |
|---|---|
| 后台预置但未安装 | `可安装` |
| 已进入当前 agent runtime | `已安装` / `已启用` |
| 企业默认开启 | `默认启用` |
| 企业强制开启 | `企业强制` |
| 需要审批 | `申请安装` |
| 已提交审核 | `审批中` |
| 只想试一次 | `在本次对话试用` |
| 想修改能力 | `Fork 自定义` |

这条规则要写进实现测试：tenant/workspace catalog visibility 不等于 agent activation。后台预置多少 Skill/Subagent，都不能默认让每个 agent 的 prompt、tool surface、subagent runtime 膨胀。

Round 3 完成标准：

- Agent Detail 不再让用户在 `Tools / Skills / Subagents` 三个入口里猜能力状态。
- 安装、试用、禁用、审批中状态都能在一个页面看清楚。
- 只有 active activation 进入 prompt/tools/MCP/hooks/subagents。
- Frontend tests 覆盖 Agent Detail IA、空态、安装、禁用、审批中、try in chat。
- Frontend tests 覆盖 100 个 optional subagents 的搜索、筛选、详情抽屉、安装状态，不把未安装 catalog entries 显示为 active。

### Round 4：Marketplace source 管理与外部提交审核闭环

目标：

```text
把 Marketplace 做成发现层，把公司 Catalog 做成权威层。
```

原子任务：

1. 新增 Workspace Settings -> Marketplaces。
   - 添加 marketplace source。
   - 支持 GitHub repo source。
   - 支持 CC marketplace repo。
   - 支持 Codex/OpenAI marketplace source。
   - 支持公司自建 marketplace repo。
   - 支持 enable/disable/sync/status。

2. 定义 marketplace source schema。
   - source_kind。
   - source_ref。
   - resolved_ref。
   - sync status。
   - entries count。
   - trust policy。
   - owner/admin metadata。

3. 实现 marketplace sync。
   - 拉取 marketplace manifest。
   - 只保存 metadata 和 source refs。
   - 不 materialize runnable artifact。
   - 不进入 company catalog。
   - 不进入 agent runtime。

4. 实现用户从 marketplace 提交。
   - browse。
   - select。
   - submit for review。
   - owner reason。
   - target agent / target company catalog。

5. 实现外部 Skill/Plugin 审核闭环。
   - materialize。
   - Trust Review。
   - license / attribution。
   - upstream hash。
   - runtime governance projection。
   - approve for agent。
   - propose to company catalog。

6. 实现 Workspace Extension Catalog。
   - Platform provided。
   - Workspace provided。
   - Personal。
   - policy：requestable / approved_available / recommended / auto_activate_by_policy / mandatory / blocked。

Round 4 完成标准：

- Marketplace entry 不能直接运行。
- Marketplace entry 不能直接进公司库。
- 外部 Skill 过检后默认只 agent-scoped activation。
- 公司收录必须单独 catalog promotion。
- 管理员可以看到从 marketplace source 到 trusted snapshot 的完整证据链。

### Round 5：Capability Factor Intake 与自进化系统接入

目标：

```text
把 agent 自己长出来的能力和外部能力使用反馈统一进入“因子入库”队列。
```

原子任务：

1. 新增 factor models / APIs。
   - `capability_factors`
   - `capability_factor_reviews`
   - `capability_promotion_proposals`
   - `/agents/{agent_id}/capability-factors`
   - `/enterprise/capability-factors`
   - `/enterprise/capability-promotion-proposals/{proposal_id}/approve`

2. 接入原生 Skill 候选。
   - `evolution/skill_candidates/<candidate_id>` -> `skill_candidate` factor。
   - 记录 source_refs、artifact_sha256、authoring_contract。
   - 通过 review 后生成 Skill component snapshot。

3. 接入 Subagent proposal。
   - pending subagent proposal -> `subagent_candidate` factor。
   - owner approval 只应用到当前 agent。
   - enterprise promotion 才进入 workspace catalog。

4. 接入外部 usage factor。
   - 外部 Skill/Plugin 的成功使用证据 -> `external_usage_factor`。
   - 不进入 self-evolution。
   - 可生成 catalog promotion proposal。

5. 接入 approved fork。
   - 外部能力需要改写时，先创建 `external_fork_candidate`。
   - license / attribution / upstream diff review。
   - fork approved 后才成为 Hive-authored skill/plugin。
   - fork approved 后才可进入 self-evolution patch chain。

6. Frontend 增加自产候选入口。
   - Agent Detail -> 能力 -> 自产候选。
   - Workspace Settings -> Capability Factor Intake。
   - 支持 approve / reject / archive / promote。

Round 5 完成标准：

- 原生 Skill/Subagent 不会隐式进入公司库。
- 外部 Skill/Plugin 不会进入 self-evolution patch chain。
- 所有公司库收录都有 factor/proposal/review/snapshot 链路。
- 前端能解释：这是 agent 自己长出来的，还是外部来源推荐收录。

### Round 6：Legacy migration、runtime 收口与生产级验收

目标：

```text
把旧 pack/plugin/skill/mcp 分散路径全部收口到新 Plugin/Extension 系统。
```

原子任务：

1. Legacy pack migration。
   - `TenantInstalledPlugin` -> catalog entry projection。
   - `AgentPluginAssignment` -> activation projection。
   - `pack.yaml` -> LegacyPackAdapter migration report。
   - legacy 字段不出现在正常用户面。
   - 旧 pack compatibility layer 可以先保留为 backing store；只有它继续作为新入口、产品面或 active runtime gate 时，才按阻断性债务处理。

2. Direct import bypass 收口。
   - `/skills/import-from-url`
   - `/agents/{agent_id}/files/import-from-url`
   - `/agents/{agent_id}/files/import-from-clawhub`
   - HR/tool handler 的 external skill install
   - sandbox code execution 产出的 `npx skills add` skill package
   - 全部改成 `SkillSourceAdapter -> stage/review/snapshot -> activate`。
   - 保留现有入口和 UI affordance，但没有 approved snapshot 时返回 `review_required`，不能直接写 active `skills/`。

3. Runtime hardening。
   - Prompt assembly 只读 active activation。
   - Tool registry 不接受 plugin manifest 直接定义 executable schema。
   - Hook runtime 只接受 approved allowlist registration。
   - MCP authz 继续拒绝 token passthrough / URL userinfo。

4. Frontend cleanup。
   - 去掉旧 Tools/Skills/Subagents 分裂体验，或降级为 Extensions 内部 view。
   - Workspace Tools / Workspace Skills / Workspace Subagents 收敛到 Catalog / Reviews / Factors。
   - i18n 更新 en/zh。

5. Observability / audit。
   - 所有 stage/review/approve/activate/revoke 写 audit log。
   - 所有 materialize 记录 sandbox report。
   - 所有 activation 记录 agent/session/user/tenant。

6. Production dry-run。
   - 扫描现有 Skill/MCP/Plugin/Extension。
   - 生成 backfill review status。
   - 生成 unsafe / unknown / legacy report。
   - 确认不会自动改变现有 agent runtime。

Round 6 完成标准：

- 用户面只看到统一 Extensions/Plugin 系统。
- 后端没有新的 install path 绕过 Trust Gate。
- Legacy pack 只剩 migration/read compatibility。
- 全量 targeted backend + frontend tests 通过。
- 完成生产 dry-run sweep 后再允许正式迁移。

### 总体落地标准

整个插件系统完全落地必须同时满足：

1. CC plugin 可以被发现、stage、adapter normalize、生成 compatibility report。
2. Codex/OpenAI plugin 可以被发现、stage、adapter normalize、生成 compatibility report。
3. MCP、Skills、Plugin、Subagent 都是 Hive Plugin/Extension component。
4. Marketplace 是发现层，不是信任层。
5. Company Catalog 是企业权威层，不是 runtime 层。
6. Agent Activation 是唯一进入 agent runtime 的入口。
7. Capability Factor Intake 是原生能力和外部使用反馈进入企业库的候选层。
8. Self-evolution 不直接 patch 外部 upstream snapshot。
9. 前端完成 Agent Detail 和 Workspace Settings 双端适配。
10. 旧 pack/install path 完成迁移和收口。
11. 所有关键路径有 tests、audit、rollback、revoke。
12. 后台预置的 Skill/Subagent 先进入 catalog availability；只有 `mandatory` / `default` selector 命中或用户显式安装后，才进入 agent activation。
13. CC `/plugin` 对齐为管理入口和 component enablement，不被实现成新的 runtime。
14. Git / GitHub / marketplace source 物化只发生在 Trust Gate materializer worker / sandbox / sidecar，不发生在 Agent runtime。
15. Plugin-provided command / skill / subagent 默认命名空间化，并带 plugin/snapshot/component provenance。
16. Plugin agent 中会扩大权限的 `permissionMode` / `hooks` / `mcpServers` 默认降权进入 report，不静默进入 runtime。
17. Marketplace refresh、snapshot update review、agent activation reload 三段语义分离。
18. `userConfig` / secret / credential 只形成 credential requirement / handle，不进入 prompt、report、API response 或 snapshot 明文。

## 9. 测试和验收

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
  tests/services/test_external_plugin_adapters.py \
  tests/api/test_agent_extensions_api.py \
  tests/services/test_capability_factor_intake.py \
  tests/api/test_capability_factors_api.py -q
```

必须覆盖：

- unsafe Skill package 只能进入 `quarantined`，不能 activate。
- clean Skill package 需要 approval 后才能写 agent `skills/`。
- `npx skills add` 只能在 sandbox materializer 中运行。
- `execute_code` 发现 `$HOME/.agents/skills` 不再直接 active install，而是创建 review。
- MCP import 必须列出 tool/resource surface，默认不自动全放开。
- MCP token passthrough / URL userinfo 被拒绝。
- plugin remote source 未签名/未锁定时只能 stage/analyze，不能 activate。
- enterprise catalog listing 不得自动进入所有 agent runtime。
- CC `user` / `managed` scope 必须映射为 catalog availability，CC `project` scope 才能映射为 agent activation。
- `mandatory` / `auto_activate_by_policy` 必须带 selector，并且只激活命中的 agent。
- `optional` Skill/Subagent 只出现在 available catalog，不得出现在 active runtime resolver。
- 安装 optional Subagent 只能创建 agent activation；只有用户选择自定义时才 fork agent-local definition。
- 后台预置 100 个 optional Subagent 时，`/agents/{agent_id}/extensions` 必须返回 available count/search/filter metadata，不能把 100 个都注入 active subagents。
- `try in chat` 必须创建 session-scoped activation，不能写 durable agent assignment。
- agent 自产 Skill/Subagent 只能先进入 capability factor intake，不能直接发布到 workspace catalog。
- 自动审计可以生成 factor review / promotion proposal，但默认不能自动 approve enterprise catalog publishing。
- capability factor 必须带 source_refs、artifact_sha256、originating_agent_id、authoring_contract、admission report / governance projection。
- promoted factor 只能生成 trusted snapshot / catalog listing，不能自动进入其他 agent runtime。
- 外部 Skill / Plugin 通过 Trust Gate 后默认只能 agent-scoped activation，不能自动进入 workspace catalog。
- 外部 Skill / Plugin 默认不能进入 self-evolution patch chain；只有 approved Hive fork 可以进入。
- 外部能力 catalog promotion 必须检查 license、upstream source/hash、usage evidence、admission report、governance projection 和 attribution。
- plugin hook 只能使用 allowlist handler。
- CC plugin adapter 必须把 skills/MCP 映射为可激活组件，把 raw hooks/LSP/local commands 标记为 unsupported。
- Codex plugin adapter 必须识别 `.codex-plugin/plugin.json` 和 `.claude-plugin/plugin.json`，并把 `skills`/`mcpServers`/`apps`/`interface` 写入 compatibility report。
- compatibility level L2 不得声称 full behavioral parity。
- legacy pack adapter 只能标记为 migration-only，不能把 `pack.yaml` 输出为新生态 manifest。
- legacy pack compatibility layer 保留时，测试必须证明它不作为新外部能力入口、不出现在 `/agents/{agent_id}/extensions` 的产品字段里，也不能绕过 Trust Gate 激活新能力。
- direct skill import 兼容入口必须在无 approved snapshot 时返回 `review_required`，不得直接调用 active skill installer 写入 `skills/`。
- `/agents/{agent_id}/extensions` 必须能读出 approved snapshot 激活后的 Skill/MCP/Plugin/Subagent 等 component 状态，且不暴露 pack 字段。
- revoked snapshot 不能被新 agent 激活。
- snapshot update 必须重新 review。
- Git source / GitHub marketplace materialize 只能在 materializer sandbox / sidecar 中执行，不能在 Agent runtime 或 ToolRuntimeService 中执行。
- marketplace refresh 只能更新 source cache / available version，不能改变 approved snapshot 或 active runtime projection。
- agent activation reload 只能刷新 approved snapshot 的 active component projection，不能重新审批，也不能创建新 runtime。
- plugin command / skill / subagent 名称与原生命名冲突时必须输出 `pluginName:*` 命名空间。
- plugin agent 的 `permissionMode` / `hooks` / `mcpServers` 默认不得进入 active subagent definition。
- plugin dependency / transitive dependency 必须进入 dependency closure report；blocked dependency 阻止 activation。
- sensitive `userConfig` 只能输出 credential requirement / handle，不得在 report / API / prompt 中出现明文 secret。
- 未审批 marketplace entry / catalog-only entry / pending review entry 不得进入 `tool_search`、MCP tools、skills、commands、subagents。

前端测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- AgentExtensions AgentSkills AgentSubagents WorkspaceSubagents
```

必须覆盖：

- Agent Detail 的 `能力 / Extensions` 有 `已安装`、`可安装`、`审批中`、`自产候选`。
- `可安装` 列表支持 Skill/Subagent/MCP/Plugin 类型筛选和 Workspace/Platform/Personal/Marketplace 来源筛选。
- 100 个 optional Subagent 可搜索、筛选、打开详情抽屉，未安装项不会显示在 `已安装`。
- `mandatory` / `default` / `recommended` / `optional` / `requestable` / `blocked` 状态文案清晰且互斥。
- 安装 optional Subagent 后只改变当前 agent 的 active state，不影响其他 agent。
- Marketplace 页面必须区分 `发现来源`、`待审核`、`已批准 snapshot`、`已安装到当前 agent`，不能把 marketplace entry 显示成可直接运行。
- Agent Detail 的能力详情必须展示 source、resolved ref、snapshot hash、permission requirements、credential requirements、unsupported components、last review status。

## 10. 完成定义

这项工作不能只做到“UI 上能安装”。完成定义必须是：

1. Skill、MCP、Plugin、Subagent、Hook 等外部能力入口都走同一 Trust Gate，并归一成 Plugin/Extension Snapshot。
2. 所有外部来源都有 source provenance。
3. 所有 materialized artifact 都有 content hash。
4. 所有 active install 都能追到 review/approval。
5. 未审批能力不能改变 agent runtime surface。
6. scripts/hooks/dependencies 不会绕过 sandbox 和 governance。
7. revoke 能阻止新激活，并能禁用指定 agent 的现有激活。
8. UI 能向用户解释：从哪里来、检验了什么、如何安装、怎么撤销。
9. CC/Codex plugin 都能进入 stage/analyze；能激活的 components 必须有 compatibility level 和 adapter report。
10. 不支持的 CC/Codex 子能力必须显式显示为 unsupported，而不是静默忽略。
11. Legacy pack / installed-plugin 只能作为 migration projection 出现在审计报告里，不能作为新安装主路径。
12. `/agents/{agent_id}/extensions` 是 agent 维度 Skill/MCP/Plugin/Subagent 等 component 状态读模型，正常用户面不出现 `pack` / `package` / `capability pack`。
13. Agent 自产 Skill/Subagent/Workflow pattern 进入 capability factor intake，而不是隐式进入 workspace catalog。
14. Capability factor promotion 有 source refs、artifact hash、自动审计报告、审批决定和 resulting snapshot 的完整链路。
15. 外部 Skill/Plugin 过检后默认只 agent-scoped activation；workspace catalog 收录必须单独 promotion。
16. Self-evolution 默认只作用于 Hive-authored / agent-authored / approved fork 能力，不直接 patch 外部 upstream snapshot。
17. targeted backend + frontend tests 通过。
18. 生产部署前跑一次 dry-run sweep，列出所有 legacy installed Skill/MCP/Plugin/Extension 的 backfill review status。
19. CC `/plugin` 的语义被完整映射为 Marketplace / Installed / Enable / Disable / Update / Reload / Validate 管理面，不新增第二套 runtime。
20. Git / GitHub / marketplace source 的拉取、checkout、cache、hash、scan 都在 materializer boundary 内完成；Agent runtime 不具备 plugin install-time Git 执行面。
21. Plugin-provided command / skill / subagent / MCP / hook 都有 namespace、source、snapshot、component、hash provenance。
22. Plugin agent 的权限扩张字段默认降权；只有 Trust Gate / admin policy 显式批准后，才投影到对应 existing runtime surface。
23. Marketplace refresh、snapshot update review、agent activation reload 分离，并分别有审计事件。
24. Secret / userConfig / credential 字段只通过 credential handle 进入 activation，不进入 prompt、report、snapshot 明文或前端响应。
25. 未审批能力不能通过 `tool_search`、MCP deferred tools、slash command list、Skill list、Subagent list、Hook registry 被 agent 发现。

## 11. 当前计划相关债务盘点（不改 CC Runtime）

先纠正边界：这套 Plugin 计划不是要改 CC runtime，也不是要重写 Hive 现有 agent runtime。

Plugin 系统只做四件事：

1. 发现：从 marketplace / GitHub / ClawHub / skills.sh / 本地上传等来源拿到 plugin 或 component。
2. 审核：在落地到 agent 之前做 provenance、manifest、脚本、依赖、MCP、hook、subagent、skill 内容检查。
3. 启用：把已通过审核的 component 映射到某个 user / workspace / agent 的 enabled state。
4. 投影：把 enabled component 挂回既有 runtime surface。

它不做下面这些事：

1. 不重写 `AgentKernel` / model loop。
2. 不替代 `ToolRuntimeService`。
3. 不重写 MCP client / MCP tool call runtime。
4. 不重写 `spawn_subagent` / `delegate_to_agent` 执行语义。
5. 不让 plugin manifest 自己定义一套新的 tool executable schema。
6. 不因为 Trust Gate 存在就切断现有 CC runtime 的行为。

正确模型是：

```text
External source / Marketplace
  -> Plugin/component adapter
  -> Trust Gate review
  -> Approved snapshot
  -> Scoped enablement
  -> Existing runtime surfaces
       - Skill loader / dynamic command
       - Existing MCP registry/client/runtime
       - Existing subagent definition / spawn runtime
       - Existing hook dispatcher
       - Existing command/tool surfaces
```

### 11.1 CC 源码对齐证据

FreeCode / CC 的 plugin 体系是 component source + enablement layer，不是第二套 runtime。

| CC 源码证据 | 观察到的行为 | Hive 对齐结论 |
|---|---|---|
| `/Users/rocky243/vc-saas/free-code-main/src/QueryEngine.ts:529` | `QueryEngine.submitMessage` 在 system init 前加载 `getSlashCommandToolSkills(getCwd())` 和 `loadAllPluginsCacheOnly()`，然后把 `tools`、`mcpClients`、`commands`、`agents`、`skills`、`plugins` 一起传给 `buildSystemInitMessage` | Plugin 影响上下文和可见 component，但不替代原有 model loop / tool loop / MCP runtime |
| `/Users/rocky243/vc-saas/free-code-main/src/commands.ts:353` | `getSkills` 合并 skill dir、plugin skills、bundled skills、builtin plugin skills | Plugin skill 是现有 Skill/Command surface 的来源之一，不是独立 runtime |
| `/Users/rocky243/vc-saas/free-code-main/src/commands.ts:476` | `getCommands` 在 base commands 中插入 dynamic skills，并保持 command enable/availability 过滤 | Dynamic skill / plugin skill 是 command list 的扩展，不是 runtime 重构 |
| `/Users/rocky243/vc-saas/free-code-main/src/commands/plugin/ManagePlugins.tsx:194` | plugin component 被展示为 `commands`、`agents`、`skills`、`hooks`、`mcpServers` | Plugin 是一组 component 的包；component 分别挂到已有 surface |
| `/Users/rocky243/vc-saas/free-code-main/src/commands/plugin/DiscoverPlugins.tsx:228` | marketplace install 写入 scoped install，安装后提示 `/reload-plugins` 激活 | 安装和激活是两步；reload 只是刷新可见 component |
| `/Users/rocky243/vc-saas/free-code-main/src/utils/plugins/refresh.ts:59` | `refreshActivePlugins` 刷新 commands、agents、hooks、plugin MCP reconnect key，并更新 AppState | reload 是 active component swap；不是重建 runtime |
| `/Users/rocky243/vc-saas/free-code-main/src/commands/clear/conversation.ts:180` | `/clear` reset MCP state 时保留 `pluginReconnectKey`，该 key 只由 `/reload-plugins` 触发变化 | Plugin MCP 通过既有 MCP reconnect/runtime 进入系统，不是单独 MCP runtime |

因此 Hive 的目标是：兼容 CC plugin 的 manifest/component 语义，同时把企业场景需要的审核、批准、撤销、catalog、agent-scoped enablement 加在进入 runtime 之前。

### 11.2 P0：真正阻断 Plugin 落地的债务

这些债务的本质是“外部能力可能绕过 plugin admission / Trust Gate，直接进入 active surface”。它们需要收口，但收口方式必须是 adapter + review + approved snapshot + existing runtime projection，不能改 runtime 语义。

| 债务 | 当前证据 | 为什么阻断 | 正确收口方式 |
|---|---|---|---|
| Direct external Skill import 仍可直接写 active `skills/` | `backend/app/api/files.py` 的 `/import-from-url`、`/import-from-clawhub` 调用 `install_active_skill_package`；HR external skill refs、ClawHub install、sandbox `npx skills add`、MCP prompt -> Skill 也有同类调用 | SkillGuard 只是静态扫描，不等于 approved snapshot；通过后会直接进入 workspace active skill surface | 外部 Skill 统一走 `SkillSourceAdapter -> materialize sandbox -> Trust Gate review -> approved snapshot -> agent enablement -> existing skill loader`；旧入口保留为兼容入口，但返回 `review_required` 或创建 pending review |
| `execute_code` sandbox HOME 产物会安装成 active skill | `backend/app/services/agent_tool_domains/code_exec.py` 会扫描 sandbox `$HOME/.agents/skills`，然后调用 active installer | 用户运行 `npx skills add` 后产物会越过 review/snapshot，直接改变当前 agent skill surface | sandbox 产物只能生成 staged artifact / review candidate，不得直接写 active `skills/`；审核通过后仍进入既有 Skill loader |
| Plugin-provided MCP component 需要 admission，不是 MCP runtime 重写 | CC plugin 可包含 `mcpServers`；Hive 现有 MCP import 也会 materialize MCP tools / assignments | 风险点不是 MCP runtime 本身，而是 plugin 附带的 MCP server 若未经过 provenance、tool diff、credential、smoke test 审核就被启用 | 新增 `MCPComponentAdapter`：只治理 plugin-provided / external package MCP component 的 review 和 enablement；通过后仍投影到现有 MCP registry/client/runtime。Standalone MCP import 若作为产品入口保留，也必须有 review mode，但不能被误写成“重写 MCP runtime” |
| Plugin-provided hook 需要 snapshot provenance | `PluginHookRegistration` 来源于 legacy installed plugin，当前 provenance/review/snapshot 不完整 | hook 会影响 session lifecycle，必须知道它来自哪个 approved snapshot 和 handler allowlist | hook component 绑定 snapshot、component id、approval、handler allowlist；执行仍走 existing hook dispatcher |
| `/enterprise/plugins/install` 仍以 capability pack / `pack.yaml` 为安装主语 | `backend/app/api/plugins.py` 调 `plugin_install_service.install_plugin`，后者 `load_manifest(plugin_key)`、写 `TenantInstalledPlugin`、sync hooks/assignments | 这个入口容易把 legacy pack 继续当新 plugin 系统；问题在 admission path，不在 runtime | `/enterprise/plugins/*` 降级为 legacy-compatible projection / migration entry；新安装走 External Capability Review / Catalog / Enablement，再投影到现有 runtime surfaces |

### 11.3 P1：产品面和读模型收敛债务

这些债务会让用户分不清“可用、已启用、审批中、自产生长候选”，但它们也是产品/read-model 层问题，不是 runtime 重构理由。

| 债务 | 当前证据 | 影响 | 收口方式 |
|---|---|---|---|
| Agent Detail 前端仍分裂为 Tools / Skills / Subagents | `frontend/src/pages/AgentDetail.tsx` 仍有 `tools`、`skills`、`subagents` tab，并分别渲染 `AgentSkillsSection` / `AgentSubagentsSection` | 用户看不出一个 employee 的真实能力状态，也无法区分 installed / available / pending / candidate | 新建 `能力 / Extensions`，按 MCP / Skills / Plugin / Subagent 展示；旧 tab 变成兼容入口或迁移后删除 |
| Workspace Settings 仍分裂为 Tools / Skills / Subagents | `frontend/src/pages/EnterpriseSettings.tsx` 仍分别挂 `WorkspaceToolsSection`、`WorkspaceSkillsSection`、`WorkspaceSubagentsSection` | 后台预置、审批、市场、能力因子四个概念分散，治理链路不可见 | 收敛为 `Extension Catalog`、`External Reviews`、`Marketplaces`、`Capability Factor Intake` |
| Subagent tenant library 和 agent enablement 混在一起 | `list_subagent_definitions` 合并 agent / tenant / builtin；Agent Detail 直接展示 merged definitions | 后台放 100 个 tenant subagent 时，agent 端容易误以为全部已安装 | tenant subagent 先进 catalog；只有 default selector 命中或用户 install 才产生 agent-scoped enablement；执行仍走 existing subagent runtime |
| Skill preset / platform skill copy 和 external import 共用 active installer | 默认 skill seeder、agent create、HR registry skill、platform skill reuse 都调用 `install_active_skill_package` | 内部可信复制和外部来源审查使用同一函数，审计上分不清 trust boundary | 拆分 `install_trusted_platform_skill` 与 `stage_external_skill_source`；所有调用点必须声明 `source_trust_class` |
| Extension read model 有两套概念 | `/agents/{agent_id}/extensions` 是 skills/MCP/plugins；`/{agent_id}/extension-registry` 是 read-only CCPlus projection | 容易出现两个“统一能力面”的真相源，前端不知道该信谁 | 保留一个 canonical `/agents/{agent_id}/extensions` product contract；ExtensionRegistryV1 作为兼容 projection，由 canonical read model 派生 |
| Capability install records 更像 telemetry，不是 authority | `AgentCapabilityInstall` 被 `/extensions` 用来展示 installed Skill records | 可能被误用为 runtime 权威状态，和 enablement state 冲突 | 将其保留为 install attempt / telemetry；产品权威状态迁移到 extension enablement/read model。这里不是替代 runtime，只是定义 UI/API authority |

### 11.4 P2：清理性债务

这些不阻塞安全闭环，但会影响产品一致性、测试清晰度和迁移完成感。

| 债务 | 当前证据 | 收口方式 |
|---|---|---|
| UI 文案还在使用 `capability pack`、`plugin key`、`web_pack` | Agent create subtitle、Workspace Tools placeholder、i18n 文案仍有 pack 语言 | 后端 read model 稳定后统一改成 Extension / Capability group / Catalog entry 文案 |
| Agent Skills UI 仍写着 “Search and install skills from ClawHub directly into this agent's workspace” | `AgentSkillsSection` 的 ClawHub modal 文案仍是 direct install 心智 | 改成 “Submit for review / Install approved snapshot / Try in chat” |
| Workspace Skills 仍是 Skill Registry + GitHub/ClawHub token 管理 | `WorkspaceSkillsSection` 管理 shared skills 和 import credentials | 迁移到 Marketplace source / External Reviews / Credential Handles |
| 旧测试仍验证分裂入口 | AgentDetail / WorkspaceTools / WorkspaceSubagents tests 仍围绕旧 section | 新增 AgentExtensions tests 后，再把旧 section tests 改成 compatibility projection tests |
| `pack` 命名仍大量存在于内部代码 | `pack_service.py`、`pack_policy_service.py`、`plugin_install_service.py` 等 | 不需要先全删；只要求正常产品面、新外部入口、read model 不再把 pack 当 canonical concept |

### 11.5 执行顺序建议

推荐按下面顺序处理，避免为了清理命名或安全入口而误伤 CC runtime：

1. 先冻结边界：Plugin = discovery / admission / catalog / enablement / projection；Runtime = 现有 Skill/MCP/Subagent/Hook/Tool 执行系统。
2. 先做 Trust Gate substrate：review、snapshot、artifact、adapter report、enablement state。
3. 先拦截 direct active install：Skill URL/ClawHub、HR external refs、sandbox `npx skills add`、MCP prompt -> Skill。
4. 再接 CC plugin component adapter：commands、agents/subagents、skills、hooks、mcpServers 分别产出 component report，不直接执行安装脚本。
5. 再做 `/agents/{agent_id}/extensions` product read model：active / available / pending / candidate 全部从 catalog + enablement + legacy projection 派生。
6. 再做前端 `能力 / Extensions`：按 MCP / Skills / Plugin / Subagent 分类展示已启用、可安装、审批中、自产生长候选。
7. 最后清理 pack 文案、旧 API 文档、legacy tests，并保留必要 migration projection。

### 11.6 禁止事项

1. 禁止把 Plugin 系统做成新的 runtime。
2. 禁止为了 Trust Gate 重写 CC tool loop / MCP client / subagent execution / hook dispatcher。
3. 禁止把 standalone MCP runtime 的存在本身判定为债务；只有“外部 plugin/package 附带 MCP component 绕过 admission 进入 active surface”才是债务。
4. 禁止把 `/agents/{agent_id}/extensions` 写成执行引擎；它是 product/read-model authority，不是 runtime authority。
5. 禁止删除 legacy pack/package 兼容层来证明新系统正确；正确做法是 adapter / projection / migration，而不是断掉现有能力。

## 12. 非目标

- 不做公开 Skill 广场。
- 不自己运营第三方市场。
- 不把 ClawHub / skills.sh / GitHub 当 trust root。
- 不把 CC marketplace、Codex/OpenAI curated marketplace 当 trust root。
- 不复活 Hive pack 作为外部插件生态格式。
- 不把 legacy `pack.yaml` 当新的 canonical manifest。
- 不承诺一开始就做到 CC plugin L4 behavioral parity。
- 不让 plugin manifest 重定义 tool executable schema。
- 不允许 install-time command 在后端宿主机裸跑。
- 不允许通过“用户坚持安装”绕过 `blocked` 准入判定。
