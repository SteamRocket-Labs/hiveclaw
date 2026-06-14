# CC 工具/扩展/Runtime 全栈对标 + Hive 插件系统设计

> 状态：v1.3 设计定稿 + **Step 0-1 已实施落地**（2026-06-14，全量 4407 passed / 0 failed，证据见 §8 实施日志）。Step 2-11 待实施。
> 方法：Workflow `cc-tooling-alignment-audit`（12 agents / 177万 token / 327 工具调用 / 27min）——
> 8 维度并行深读 CC 源码(`/Users/rocky243/Context Engineering/claude-code-org`)+ Hive 源码，
> 综合统一方案，再过 3 道对抗 critic（漏链路 / 残留债 / 自创概念检测）。所有论断带 file:line。
> 吸收并取代 `docs/capability-pack-consolidation.md`（web_search/SearXNG 专项详情仍见该文 §3）。
> 纪律：一次改完零债（[[feedback_no_mvp_finish_completely]]）+ AI-Native L1/L2/L3 + 北极星。

---

## 0. 缘起与北极星

对标 Claude Code 工具体系起于一个局部问题（pack 该不该收敛），但 owner 定调拔高为**全栈对标**：
工具定义、工具调用、懒加载、插件、skill、MCP、subagent、workflow runtime 全部对齐 CC 基线，
**把半成品的 pack 升级成真正可扩展的插件系统**，全链路零债——同时保留 Hive 特色。

**北极星（目标形态）：**

| 层 | 定义 | 对标 |
|----|------|------|
| **CORE** | **通用底座**：无需安装外部连接、所有 agent turn-1 应可见的基础能力（含需治理的 `write_file`/`run_command`/`start_workflow`——治理不改变它属 CORE） | CC `getAllBaseTools()`(`src/tools.ts:193`) |
| **插件系统** | **安装型能力包**：领域工具/skills/MCP servers/凭据/沙箱/治理元数据，可打包·分发·多租户安装+凭据隔离 | CC `plugin.json` + `marketplace.json`(`schemas.ts:884/1293`) |
| **Governance** | **call-time 执行边界**(security zone→capability gate→approval→preflight)，正交于 CORE/插件归属——CORE 工具也受治理 | Hive delta |
| **skill / MCP / subagent / workflow** | 各自对标 CC 对应子系统，作为插件可组合的 component | CC 各 loader |

**判据（修正自初稿——治理不是分界线）**：工具"无需安装外部连接、所有 agent 都该 turn-1 可见"→ CORE（即使需治理，如 `write_file`/`run_command`）；"需安装领域包 / provider key / MCP server"→ 插件。**治理是 call-time 边界，不决定一个工具属 CORE 还是插件。**

**核心洞察（本次对标最大发现）**：Hive 的 pack 成员有**三个漂移的真相源**——
`RUNTIME_TOOL_GROUPS`(静态 dataclass，编译进二进制) + `@tool(ToolMeta.pack=)`(decorator 每工具声明) +
`packs/*/pack.yaml`(已是近 `plugin.json` 的清单格式，但 `catalog_reader.py:1-5` **故意注释"不参与 runtime"**)。
**插件系统不是从零造，而是接通一个已建好的死清单(pack.yaml) + 泛化一个已跑通的多租户安装原语(MCPServer)。**

---

## 0.x 当前稿修正（实施前必读，与正文冲突以本节为准）

第二轮 owner/Codex 核对（命令 §8，已复跑确认：manifest pack 仅 2 个、coordination 8/8 + plan_mode 3/3 全 overlap CORE）逼出 7 条修正：

1. **CORE 判据不以"是否治理"为准** — 治理是 call-time boundary。`write_file`/`run_command`/`start_workflow` 都是 CORE 却都需治理/plan gate。CORE/Plugin/Governance 三者正交（见 §0 北极星表）。
2. **仅 `web_search` 进 CORE；`firecrawl_fetch`/`xcrawl_scrape` 保持 provider plugin** — 后两者需 provider key、无 key 不可用，不进 CORE（web_search 有 SearXNG/DDG 无 key 兜底，可进）。
3. **`pack.yaml` = install/composition/credential/governance/distribution 真相源；`@tool` decorator 仍是 executable schema 真相源** — runtime 用 `TenantInstalledPlugin`/`AgentPluginAssignment` 决定哪些 manifest-declared tools **可见**。**绝不让 manifest 变成第二套工具 schema。**
4. **plugin manifest 的 tools 拆三字段** `owns`(本包定义) / `requires_core`(依赖的 CORE 工具，如 DR 依赖 web_search/web_fetch) / `optional_providers`(有 key 才解锁，如 firecrawl/xcrawl)。**`CORE∩pack.tools=∅` 断言只对 `owns`**，`requires_core` 允许引用 CORE。
5. **ToolSearch 对齐 CC 机制但实现必须 provider-neutral** — 写成"provider-neutral deferred schema delta"；`tool_reference` 只是 Anthropic fast path，其他 provider 走 Hive 自己的 schema expansion/event path（守 L3 模型平等）。
6. **MCP `mcp__server__tool` 迁移必须带 alias/backfill/collision audit** — 旧 `Tool.name`、历史 transcripts、skills 里的 declared tools、`AgentTool` rows 都要 alias/backfill，**不能直接 rename**。
7. **fan-out 选 B（修正版，owner 已定）** — **不新增/不暴露 `fanout_subagents` 这个可执行工具**；现有 `fanout_subagents` 字符串仅作 `_SUBAGENT_BASE_EXCLUDED_TOOLS` recursion guard(`subagent.py:109`)，**必须保留**直到有等价 guard + 测试替代；同样**不删** `SubagentJob`/`SubagentBudget`(`workflow_launch.py` 活机件)。fan-out 收敛为 workflow `fanout_step`(确定性，DR 已走)+ 并行 `spawn_subagent`(临时，接 `run_in_background`)，对齐 CC 两条路径。

---

## 1. 八维度对标矩阵

| 维度 | CC 做法 | Hive 现状 | 处置 |
|------|---------|-----------|------|
| **工具契约** | `Tool<I,O>` 富接口：`isReadOnly/isConcurrencySafe/isDestructive` 是 `(input)=>bool` 谓词；`validateInput`(告知模型失败原因) 与 `checkPermissions`(权限门) 分离；`mapToolResultToToolResultBlockParam` 回 typed block(image/pdf/text)；per-tool `maxResultSizeChars`(Read=∞)；`buildTool` fail-closed(`Tool.ts:362-792`) | `ToolMeta` frozen dataclass：`read_only/parallel_safe` **静态布尔**；`governance` 三值字符串；`adapt_and_call` 强制 `str()` **纯字符串结果(无多模态)**；全局 50KB 落盘；`registry.py:80-126` **第二份硬编码名单**与 decorator 漂移 | **converge** |
| **调用·组装·治理·并行** | 扁平 `getAllBaseTools→getTools(deny)→assembleToolPool(内置前缀+MCP+uniqBy)`；三态 allow/ask/deny + safety bypass；连续只读批并发(10)/非安全串行(`tools.ts:193-345`) | 三层 CORE(41)/pack(9)/MCP；**治理四级串行管线** plan→governance→preflight→timeout 全 fail-closed；**done_event 屏障并行**(safe 只等前序 unsafe，优于 CC 静态分批)；Semaphore(10)(`engine.py:3175`) | **converge** |
| **懒加载(defer)** | 单一 request-time defer：`ToolSearchTool`+`tool_reference`(服务端展开 schema)；三模式 tst/tst-auto(token 10%)/standard；turn-1 枚举 `<available-deferred-tools>`+`select:` 直选(`toolSearch.ts:172`) | **两套并行** pack 激活 + tool_search；defer 静态全有全无；文本端(`workspace.py`含CORE)与 schema 端(`invoker.py:662`跳CORE)**各算各的**；turn-1 不枚举只泛化引导 | **converge** |
| **插件系统** | 完整一等：单 `plugin.json` 组合 commands/agents/skills/hooks/mcpServers/lspServers/userConfig；`marketplace.json` source-addressable(github/git/npm/pip/url/local)；后台 reconciler diff→clone→refresh；userConfig enable-time 凭据(`schemas.ts:884-1367`) | **无统一插件系统**，三碎片：`RUNTIME_TOOL_GROUPS`(仅工具名) + `pack.yaml`(近 plugin.json 但声明不参与 runtime) + MCP import(唯一真租户安装，但只装单 server) | **converge** |
| **Skill** | 渐进 listing(name+desc,≤1%窗口,**动态 system-reminder 非缓存前缀**)→invoke 注入整段 body 替换；六来源；创作 100% 人在环(`disableModelInvocation:true`) | 渐进加载对齐 + `is_system` 永不截断；**catalog 错放 frozen prefix**(`prompt_builder.py:268`→跨会话 cache 击穿)；`save_skill`+`skill_distiller`+`skill_curator` = 自进化 delta；19 字段 frontmatter 过半死字段 | **converge** |
| **MCP** | per-tool 扁平 `mcp__server__tool` 规范前缀；deny 按全限定名+server 通配；`McpAuthTool` OAuth 热替换；`resources/*` 一等 primitive；8 transport(`client.ts:1768`) | DB 行**扁平原名暴露(无前缀→撞名)**；`resolve_agent_mcp_tool_mode` auto/approval/deny 真多租户治理(delta)；`mcp_authz` 禁 token passthrough；无 OAuth；list/read 实为 DB 自省非协议；仅 HTTP+SSE | **converge** |
| **Subagent** | ONE `AgentTool`(wire 'Task')；markdown body 即整段 prompt 替换；`ALL_AGENT_DISALLOWED_TOOLS` 递归守卫；`run_in_background` 在 schema+120s auto；`TaskOutput/TaskStop` | `spawn_subagent`(worker,sync) vs `delegate_to_agent`(peer,async)；3 type 忠实移植 CC；治理继承(每 subagent tool 走 governance)；max_depth=2；**async 基建全通但 LLM 不可达**(schema 缺 `run_in_background`) | **converge** |
| **Workflow** | `WorkflowTool` = 命令式 JS 脚本编排，feature-flag；run 进 Task ledger；per-leaf `canUseTool`(`tools.ts:129`) | 编排单元 = **可序列化结构化数据**(pydantic,零代码面)；四阶段 schema→compiler→admission→engine；run IS RuntimeTask+leaf journal；生命周期+trigger 融合(hash pin)+live resume | **keep_delta** |

---

## 2. 三分类：对齐 / 真 delta / 该砍

**① 对齐 CC 基线（补 Hive 缺失）：** CORE 扁平底座 · 单 plugin 清单组合多 component · source-addressable marketplace ·
MCP `mcp__server__tool` 规范前缀防撞名 · MCP `resources/*` 一等 primitive · MCP 标准 OAuth2 ·
skill catalog 动态注入(非缓存前缀) · subagent `run_in_background` 进 schema · 结果 typed content block 回灌 ·
per-tool 落盘预算(Read=∞) · ToolSearch token-阈值 auto 模式 · tool `is_destructive` 一等标志。

**② 真 Hive delta（CC 无、企业必需，保留并强化）：** 多租户+RLS 安装隔离(`TenantInstalledPlugin` 镜像 `MCPServer`) ·
凭据隔离(`SECRETS_MASTER_KEY` 加密，绝不 passthrough) · 清单内携带 governance 元数据 · 治理四级串行管线 ·
MCP per-agent auto/approval/deny+override · 并行 done_event 屏障 · `save_skill`/`distiller`/`curator` 自进化 ·
subagent 持久定义+scope chain+memory+evolution · subagent 治理继承 · workflow 结构化数据编排(零代码面) ·
subagent completion signal/wake 持久化(PG Signal；worker 本身需在 Step 8 补 RuntimeTask/SubagentRun 级跨重启恢复后才算完成) ·
`invocation_spans` 审计+`decision_trace`+`action_preflight`。

**③ 该退役/收敛的自创面（净减法，⚠️ 但见 §5.1 核实纪律——部分 deep-dive 判定有误，禁盲删）：**
退役 `coordination_pack`/`plan_mode_pack` 这类 runtime group entry（成员全在 CORE，**不是删工具**） ·
脚踏两船工具清出 plugin `owns`，改进 `requires_core`/`optional_providers` · `infer_from_tools` 布尔双语义 ·
skill frontmatter 死字段 · skill `declared_packs`(已退化 no-op) ·
`WorkflowStep.phase` 死列 · `catalog_reader.py:1-5` severance 注释 · `mcp_server:*` 伪 pack。

---

## 3. Hive 插件系统设计（本次重点）

**本质**：把 `pack.yaml`（已是近 `plugin.json` 的清单，但被故意切断 runtime）升为**安装/组合/凭据/治理/可见性真相源**（**非工具 schema 真相源**——`@tool` 仍是 executable schema 真相源；**kernel tool surface 由 resolver 合并 CORE + installed plugin + MCP assignment + requested expansion**），
并把 MCP import 已验证的多租户安装范式（`MCPServer` 行 + RLS + 凭据隔离 + per-agent assignment/override + `mcp_authz` 治理，
`mcp_server.py:1-126`）**泛化**为通用 `TenantInstalledPlugin`。对标 CC 单清单组合 + source-addressable 安装，
每层叠 Hive 多租户 RLS + 凭据隔离 + 治理审计（CC 单用户无此层 = 真 delta）。

| 维度 | 设计 |
|------|------|
| **打包** | `pack.yaml` = **install/composition/credential/governance/distribution 真相源**（**非工具 schema 真相源**——`@tool` decorator 仍是 executable schema 唯一来源，manifest 绝不重定义 schema）。`tools` 拆三字段 `owns`(本包定义)/`requires_core`(依赖的 CORE 工具，如 DR 的 web_search/web_fetch)/`optional_providers`(有 key 才解锁，如 firecrawl/xcrawl)。**v1 补 `agents`/`hooks`/`dependencies`，并同步纳入 source-addressable install model**；但三者是治理化入口：`hooks` 只允许声明式绑定平台 allowlist handler，`dependencies` 只允许 resolver+lockfile+allowlist 的闭包，`source` 由安装入口/registry 记录 provenance（不信任 manifest 自报）。v1 内置/本地 source 可用；远程 source 只进入 policy/provenance/validator 框架，缺少 signature verifier + sandbox materializer 时一律 fail-closed。manifest 出现 raw import/shell/webhook handler、unpinned dependency、disallowed source ref 即 fail-closed。**删 `catalog_reader.py:1-5` "不参与 runtime" 注释**——让它驱动安装/可见性 |
| **分发** | 内置 marketplace = 复用 `pack_service.get_pack_catalog`(对标 `marketplace.json`)。source model 覆盖**内置/本地/远程**，但 v1 可执行范围限定为内置/本地源：路径校验、blocklist、hash/provenance lockfile 必须完成。远程 git/url/npm/pip 源在 v1 只做结构化识别、policy 校验和 fail-closed 错误；实际启用必须等 content hash/signature verification + lockfile provenance + sandbox materialization 基础设施达标，任何 install-time binary/script 必须走 code_execution provider 沙箱。ClawHub(`search.py:272`)降为同一安装 API 下的一个外部源，去特权；在远程源门未达标前也按 fail-closed 处理 |
| **安装** | 新建 `TenantInstalledPlugin` + `AgentPluginAssignment` 两表，**严格镜像 `MCPServer` 范式**：`tenant_id` 强制 + RLS ENABLE/FORCE + `UniqueConstraint(tenant_id, plugin_key)` + per-agent enable/override。`POST /enterprise/plugins/install`(镜像 `mcp_servers.py:79`)→持久化→解析 `credential_requirements` 驱动 enable-time 凭据提示写 `encrypted_tool_config`。**用安装记录替代 `pack_policy_service` on/off SystemSetting** |
| **多租户治理** | day-1 硬不变量：①安装记录 `tenant_id`+RLS，owner 连接也不旁路；②凭据必经 `SECRETS_MASTER_KEY` 加密(否则=Web3 跨租户泄漏同类，见 MEMORY 铁证)；③call-time 治理不变(仍走四级管线，安装只决定"可见"不决定"可执行无门")；④清单 governance block 成治理硬输入；⑤plugin hook 若修改 `PRE_TOOL_USE` args，最终 args 必须重跑 tool schema/capability/ActionPreflight/approval，hook 只能收窄或阻断，不能绕过治理 |
| **与 skill/MCP 关系** | 插件是**上层组合单元**，skill/MCP/hooks/dependencies 是其 component，source 是安装来源/provenance。清单 `skills` 字段安装时 feed `skill_seeder`+自进化轴；`mcp_servers` 字段安装时调 `import_mcp_for_agent_and_register` 落 `MCPServer` 行(MCP 仍是一等原语，插件是其上层批量封装)；`agents` 字段安装为 tenant-scope subagent 定义；`hooks` 字段只写 tenant-scoped `PluginHookRegistration` 并经 `HookRegistrationSpec`/allowlist handler 注册；`dependencies` 字段由 resolver 展开成 tenant install graph + lockfile；install source model 记录 pinned provenance（不信任 manifest 自报） |
| **从 pack 迁移** | 三源收敛：①先砍死概念(§5.1 核实后)；②`pack.yaml` 成 **install/composition 权威(非 schema 权威)**，`RUNTIME_TOOL_GROUPS` 退化为内置系统 pack 的生成式 fallback，`@tool(ToolMeta.pack=)` 改为对清单**校验**(非重复定义 schema)；③runtime 可见性由 `TenantInstalledPlugin`/`AgentPluginAssignment` 决定；④`audit.py` startup 审计清单/decorator 分歧 fail；⑤`pack_policy_service` 迁安装记录 |

**完成判据（诚实底线）**：一个租户安装的 `pack.yaml` **真改变 kernel turn-1 暴露的工具集**，经 `invoke_agent` 集成测试证明，
非 catalog/UI 渲染——否则 = 又一个 built-but-unwired 死清单（`catalog_reader.py:1-5` 同类病复发，见 [[feedback_green_tests_dont_mean_done]]）。

---

## 4. 全链路改造序列（Step 0-11，单 PR 完整交付）

| Step | 子系统 | 核心改动 | 依赖 |
|------|--------|---------|------|
| **0** ✅ | cleanup | 退役 `coordination_pack`/`plan_mode_pack` 这类 CORE-only runtime group entry（不是删工具）；脚踏两船工具清出 plugin `owns`；**核实 cut 清单引用点(§5.1)**——`fanout_subagents` recursion guard / `SubagentJob`·`Budget` / `FALLBACK_EXECUTOR_NAME` 经核实都是活的，**禁盲删**；加 startup 断言 `CORE∩pack.owns=∅`(只对 owns，`requires_core` 允许引用 CORE)。**已实施，证据见 §8。** | — |
| **1** ✅ | tool-contract | 消除 `read_only/parallel_safe` 双定义(删 `registry.py:80-163` 静态名单，单源 decorator)；`ToolMeta` 增 `destructive:bool`+`max_result_chars:int\|None`；`read_file/read_document` 设 ∞ | 0 |
| **2** | tool-contract | 修 critical：扩展现有 `result_envelope.py` 或新建 `ToolContentEnvelope(text+blocks)`（避免与 error/fallback envelope 命名碰撞），`adapt_and_call` 透传 typed content block(image/pdf)；`read_document`(PDF)/`read_file`(图)首接；保留纯字符串默认 | 1 |
| **3** | lazy-loading | 文本端(`workspace.py`)与 schema 端(`invoker.py`)统一单一"query→可发现工具名"函数；补 turn-1 deferred 清单+`select:` 直选；token-阈值 auto 模式；loaded-tool state 必须进入 compaction/replay/prompt-cache 稳定排序/invocation span。**实现 provider-neutral**：Hive 自己的 schema expansion/event path 为主，`tool_reference` 仅作 Anthropic fast path(守 L3) | 0,4 |
| **4** | plugin-system | 删 `catalog_reader.py:1-5` severance 注释；`PackManifest` 加 `agents`/`hooks`/`dependencies` 字段；manifest validator fail-closed 校验：hook handler 必须来自平台 allowlist、dependency 必须 pinned、dependency source ref 必须 admin-allowed，远程 source ref 在 signature/sandbox 基础设施未达标时结构化拒绝，禁止 raw shell/import/webhook handler；工具收集从清单读；`RUNTIME_TOOL_GROUPS` 退化 fallback；`audit.py` startup 分歧 fail | 0,1 |
| **5** | plugin-system | 新建 `TenantInstalledPlugin`+`AgentPluginAssignment`(镜像 MCPServer RLS)+`PluginHookRegistration`+dependency lock/install graph+source policy/provenance；`POST /enterprise/plugins/install\|list\|uninstall`；安装时先 resolver/validate/lock，再落安装记录；内置/本地 source v1 可安装，远程 source v1 只可被 policy 识别并 fail-closed；`pack_policy` 迁安装记录；**仅 `web_search` 进 CORE**(有 SearXNG/DDG 无 key 兜底)；**`firecrawl_fetch`/`xcrawl_scrape` 留 provider plugin**(需 key，进 `optional_providers`) | 4 |
| **6** | mcp | 新增 `mcp_naming.py`(`mcp__server__tool` 前缀+反解)；**命名迁移必须同时审计** `Tool.name` 列宽(当前 `String(100)` `tool.py:25`，长名易超)、provider tool-name 约束、确定性 slug、旧名 alias、历史 transcript、skills declared tools、`AgentTool`/MCP override rows——**禁止直接拼长名后 rename**；统一双执行路径(`FALLBACK_EXECUTOR_NAME` 是活兜底，先核实)；收敛 `mcp_server:*` 伪 pack 到 assignment | 0,5 |
| **7** | mcp | `MCPClient` 加 `resources/list`+`resources/read`(blob 落 artifacts)；DB 自省工具更名 `list_mcp_tools/inspect_mcp_tool`；新建 `mcp_oauth.py` 标准 OAuth2(加密存凭据，守 mcp_authz)；填 `auth_status` | 6 |
| **8** | subagent | schema 暴露 `run_in_background`，接通 completion consume tool/prompt、parent wake、tenant context、depth recursion guard、budget trace；补 RuntimeTask/SubagentRun 级 durable run recovery（当前 PG Signal/wake 持久，但 `asyncio.create_task` worker 本身未跨重启）；加 `check_subagent`；governance 按 type 分级(read-only explorer/critic 轻于 worker)；fan-out 决断(见 §6) | 0 |
| **9** | skill | catalog 移出 frozen prefix→动态 suffix(修 cache 击穿)；删死字段；distiller 晋升硬门改 `evolution_ledger` 外部 eval(非 LLM 自评)；`allowed_tools` 接 scoped 治理引导 | 4 |
| **10** | workflow | preview/start_workflow 确认仅留 CORE；`office_workflow_examples` 接 platform-template seeder 或删(⚠️核实)；修 `runtime_task` 注释+`phase` 死列；文档化"结构化数据 over 脚本"为显式防御决策 | 0,5 |
| **11** | docs | 修 CLAUDE.md 文档漂移(packs.py→runtime_tool_groups.py+pack.yaml)；记录插件安装生命周期 | 4,5,6,7 |

---

### 4.1 零债务实施面锁定（每个 Step 合并前必须覆盖）

**v1 component 范围（governed inclusion 边界）**：manifest 收 `tools`/`skills`/`mcp_servers`/`agents`/`hooks`/`dependencies`，source-addressable install model 收内置/本地/远程 source provenance（不信任 manifest 自报）；`hooks`/`dependencies`/远程 source 不是自由扩展面，必须先过声明式校验、tenant/RLS 安装记录、allowlist/blocklist、lockfile/provenance、审计和 runtime governance 重放，任何不满足治理模型的字段 fail-closed。v1 内置/本地 source 完整可用；远程 source 的 policy/provenance/validator 框架完整，但实际启用被 signature verifier + sandbox materializer 安全门挡住（见 §6 决策 3/7）。以下 14 面每个 Step 合并前必须覆盖：

1. **Schema truth**：`@tool` executable schema、aliases、provider schema 转换、typed result blocks。
2. **Visibility truth**：CORE、installed plugin、MCP assignment、requested deferred expansion 统一进单一 resolver。
3. **Install truth**：`pack.yaml` manifest、`TenantInstalledPlugin`、`AgentPluginAssignment`、credential requirements、existing-tenant backfill。
4. **Governance truth**：`CAPABILITY_MAP`、`Tool.category`、`ToolMeta` governance/destructive、ActionPreflight、approval、plan gate。
5. **Runtime truth**：kernel tool assembly、parallel safety、timeouts、result size/artifacts、`invocation_spans`、compaction/replay loaded-tool state。
6. **MCP truth**：`mcp__server__tool` naming、slug/length limits、old-name aliases、`resources/list`/`resources/read`、OAuth、DB inspect tool rename。
7. **Skill truth**：dynamic catalog injection、`load_skill` body injection、frontmatter dead fields、skill-declared tools validation。
8. **Subagent truth**：schema `run_in_background`、parent wake、completion consume、recursion guard、tenant propagation、durable run recovery target。
9. **Hooks truth**：manifest hooks 只能是声明式 `HookRegistrationSpec`，handler 来自平台 allowlist；tenant/agent/plugin scope 必须落库；`PRE_TOOL_USE` enforce hook 需 admin approval，`modified_args` 重跑 schema/capability/ActionPreflight/approval；hook timeout、failure mode、recursion guard、span/audit 全覆盖；禁止插件提供 raw Python/import path/shell/webhook handler。
10. **Dependency/source truth**：dependency resolver、version/source pin、lockfile、cycle detection、install order、uninstall protection、tenant dependency graph、source allowlist/blocklist、content hash/provenance、路径遍历校验；内置/本地 source v1 可安装，远程 source v1 只完成 policy/validator/provenance 并 fail-closed，直到 signature verification + sandbox materialization 基础设施达标；禁止 transitive 远程 fetch 绕过 source policy。
11. **Workflow truth**：CORE source tools、structured runtime as Hive delta、replay/hash pin、`wait_signal`、workflow templates/examples lifecycle。
12. **Product surface**：backend APIs、frontend Workspace/Agent extension UI、i18n en/zh、legacy packs API retirement。
13. **Deployment truth**：alembic migration、create_all/bootstrap path、entrypoint imports、RLS ENABLE/FORCE、backfill dry-run/apply、rollback notes。
14. **Tests**：unit + integration + Testcontainers RLS + `invoke_agent` tool-surface test + frontend API/UI tests + migration/backfill tests。

---

## 5. ⚠️ 对抗验证：执行前必须纳入的修正

三道 critic 把方案从"看起来完整"逼到"真能落地"。**critic 3（自创概念检测）判 PASS**——方案是净减法，
唯一新结构 `TenantInstalledPlugin` 是克隆已验证的 `MCPServer`，非自创。但 critic 1/2 抓出必须纳入的硬伤：

### 5.1 deep-dive 误判，禁止盲删（已核实，铁证）

Step 0/§2③ 的 cut_invented "死代码"列表**不可盲信**，Step 0 执行时每条必须先 grep 引用点。已核实两条 deep-dive **判错**：

- **`fanout_subagents` 不是幻影死常量**——核实：它是 `_SUBAGENT_BASE_EXCLUDED_TOOLS` 的成员(`agents/subagent.py:113`)，
  在 `:434` 构建 subagent 排除集 `excluded = (*_SUBAGENT_BASE_EXCLUDED_TOOLS, *spec.excluded_tools)`。
  它是**防御性排除**(禁 subagent 递归 fan-out)。删它移除递归爆炸防御 → **保留**(或与 fan-out 决断一并定，见 §6.1)。
- **`FALLBACK_EXECUTOR_NAME` 不是死常量**——核实：`runtime.py:15` 定义，`runtime.py:51` `try_execute` 真实引用
  `executor = self._executors.get(FALLBACK_EXECUTOR_NAME)`。是**活兜底路径** → 删前必须确认无任何地方注册 fallback executor。

同理 critic 2 标记 `SubagentJob`/`SubagentBudget`/`office_workflow_examples` 的"死/孤儿"判定与代码不符——
**Step 0/10 删除前逐一核实引用，否则编译断裂或删活路径。**

### 5.2 必补的遗漏链路（critic 1，15 条）—— 漏一条即生产崩或留债

**critical 五条（漏即 fail-closed 崩 / 跨租户泄漏 / 治理绕过）：**
1. **`TenantInstalledPlugin` 多租户地基**：Step 5 只规划 alembic migration 不够；fresh/unversioned DB 会经 `create_all`/alembic bootstrap/stamp 路径跳过普通 migration 语义(`entrypoint.sh:38` 手动 import 一批 model 后 `create_all`)——必须同补：`app.models.import_all_models()` + `entrypoint.sh` 手动 import 覆盖 + `db_bootstrap.py` RLS 表清单(`RLS_TENANT_TABLES`/`RLS_FORCED_TENANT_TABLES`，`db_bootstrap.py:28`) + entrypoint ALTER patch。**漏 RLS = Web3 跨租户事故同类**([[project_rls_preauth_login_outage]])。
2. **`CAPABILITY_MAP` 注册**：Step 0/1 改工具 → STRICT_CAPABILITY_MAPPING 下漏注册 = 真实 tenant invocation **fail-closed 拒**(`capability_gate.py`)。
3. **DB `Tool.category` 列 vs `ToolMeta.pack`**：前端真正分组依据是 DB `Tool.category` 列**不是** pack——改 pack 必须同步 category。
4. **Plugin hook 治理链**：`runtime/hooks.py` 的 `PRE_TOOL_USE` 已在工具执行前生效，`HookResult` 可 `block`/`modified_args`；manifest 支持 hooks 就必须同步补 `PluginHookRegistration`、allowlist handler catalog、admin approval、timeout/failure mode、recursion guard、span/audit，并对 hook 修改后的最终 args 重跑 schema/capability/ActionPreflight/approval。漏任一项 = 租户逻辑进入治理关键路径却无治理。
5. **Dependency/source 解析链**：dependencies 和 source 必须同补 resolver、pinned lockfile、cycle/source/allowlist 校验、content hash/provenance、uninstall protection、路径遍历校验；内置/本地 source v1 完整可用。远程 source 必须先被 policy/validator 结构化拒绝，直到 signature verification + sandbox materialization 达标；漏 resolver/lock/source policy = 一个插件安装可扩散成未审计代码安装。

**structural/cosmetic 十条：** 前端插件安装/管理 UI(`WorkspaceToolsSection.tsx` 加第四视图，否则=`feedback_surface_not_plumbing` 病) ·
遗留 `app/api/packs.py` 仍 wired 在 `main.py:580` 全程未提 · `pack_policy_service` 级联消费者 · `skill_seeder` pack→skill 落地 ·
deep_research 还有 `routing_reminder` 硬编码(不止 leaf_presets) · i18n 双语键(en+zh) · 前端 extensions API 关系 ·
**`ToolResultEnvelope` 命名碰撞**(`result_envelope.py` 已有内容，Step 2 改名或扩展现有) ·
lazy-loaded tool state 的 compaction/replay/prompt-cache 保持 · 7+ 个 pack 测试会断需迁移。

### 5.3 backfill / 语义翻转防灰度断（critic 2，critical）

Step 5 把 `is_pack_enabled` 的**"缺省=启用"静默翻成"未安装=禁用"**，且对 **7 个无 `pack.yaml` 的活 pack 无 backfill** =
灰度即全平台静默断能力（RLS flip 全员 401 / Web3 跨租户停机同类病）。**必须**：迁移前为所有现存活 pack 生成 `pack.yaml` +
为所有现有租户 backfill `TenantInstalledPlugin` 安装记录(保持现状可用)，再翻语义。这是"一次改完"的必含项，非"later"。

---

## 6. 决策已定（owner 拍板归档，呼应 critic 2：不悬置成分期）

1. **fan-out = B(修正版)**：**不新增/不暴露 `fanout_subagents` 可执行工具**；现有 `fanout_subagents` 字符串仅作 `_SUBAGENT_BASE_EXCLUDED_TOOLS` recursion guard(`subagent.py:109`)，**保留**直到有等价 guard+测试替代；**不删** `SubagentJob`/`SubagentBudget`(`workflow_launch.py` 活机件)。fan-out 收敛为 workflow `fanout_step`(确定性，DR 已走)+ 并行 `spawn_subagent`(临时，接 `run_in_background`)，对齐 CC 两条路径；worker 本身的跨重启恢复在 Step 8 作为 durable run recovery 完成项。
2. **web_search 进 CORE**：与 web_fetch 同层，无 key 走 SearXNG/DDG 兜底；`firecrawl`/`xcrawl` 不随之进 CORE(留 provider plugin 的 `optional_providers`)。
3. **插件远程源体量决策**：v1 纳入 source model，但拆成两层：①零债必做的框架层：source policy/provenance schema、validator、lockfile、allowlist/blocklist、路径校验、结构化 fail-closed 错误，内置/本地 source v1 完整可用；②远程 git/url/npm/pip 的实际启用层：必须等 content hash/signature verification + sandbox materialization 基础设施达标，install-time binary/script 走 code_execution provider 沙箱。v1 不声称远程源可用；远程源在安全门达标前一律 fail-closed。
4. **subagent governance 分级**：read-only explorer/critic 降轻 zone(对齐 CC frictionless 侦察)，仅 edit-capable worker 保 sensitive。
5. **`ToolMeta.governance` 细化**：先拆 `destructive` 观察，过度拆分前等真实治理需求。
6. **插件 vs MCP 安装入口**：MCP 单 server 直装 + 插件批量安装两入口并存，文档化两入口关系防再分叉。
7. **v1 manifest component 范围（governed inclusion）**：v1 manifest 收 `tools`/`skills`/`mcp_servers`/`agents`/`hooks`/`dependencies`，并同步开启 source-addressable install model；`hooks`/`dependencies`/`source` 只作为受治理入口开启：`hooks` 必须声明式绑定平台 allowlist handler，tenant 只配置 event/matcher/mode/治理参数，不能提交任意代码、shell、import path、webhook；`dependencies` 必须经 resolver 生成 pinned lockfile，带 cycle/source/allowlist 校验和 uninstall 保护；`source` 必须由安装入口/registry 写入 provenance，内置/本地 source v1 可安装，远程 source 在 signature verifier + sandbox materializer 未达标前只允许被识别并 fail-closed。任何 raw handler、unpinned dependency、disallowed/transitive source 一律 fail-closed。理由：CC 单用户 shell-hook 的安全模型在多租户下不成立，但 v1 可以纳入，前提是它们先被建模成治理对象，而不是自由执行面；远程供应链平台不和本轮工具体系整改强绑成同一个必须完成的体量。

---

## 7. 风险

- **DR 耦合**：web_search 改动波及 `deep_research/leaf_presets.py` + `routing_reminder.py`，验工具激活链不破。
- **三源坍缩半迁移**：`pack.yaml` 升唯一真相源若只迁部分 pack = 半迁移技术债，必须全量(含 backfill §5.3)。
- **多模态回灌的 provider 中立**：typed content `blocks` 必须是 provider-中立 content block 抽象(守 L3 模型平等)，Anthropic/OpenAI/Gemini 仅在 adapter 层映射。
- **回归面**：pack 重命名/删除波及 `pack_policy_service`/`capability_gate`/`skill_seeder`/前端/测试，全量回归 + Testcontainers 真 PG 验 RLS。
- **Subagent durable 过度承诺**：PG completion Signal/wake 已可持久，但后台 worker 当前若仍只靠 `asyncio.create_task`，进程重启会丢未完成执行；Step 8 必须补 DB-backed run record + resume/terminal reconciliation。
- **Plugin hooks = 治理路径插入点**：`PRE_TOOL_USE` 当前可 `block`/`modified_args`，若允许租户 manifest 绑定任意 handler 就等于把租户逻辑放进工具执行前置链；v1 只能 allowlist handler + 声明式 matcher/mode + admin approval + args 重验 + span/audit。
- **远程源/dependency = 任意代码安装放大器**：dependency 闭包和远程 source 未经 lockfile/source policy/路径校验/审批/沙箱会把一个插件安装扩散成未审计代码安装；v1 必须把 resolver、provenance、uninstall protection、内置/本地 source 校验做完，远程 source 在 signature/sandbox materialization 基础设施完成前保持 fail-closed，禁止文档或 UI 暗示已可用。

---

## 8. 实施日志（Implementation Log）

每个 Step 落地后在此记录：实际改动、与设计稿的偏差/发现、验证证据。纪律 = 一次改完零债（[[feedback_no_mvp_finish_completely]]）+ 每 Step 一 commit + 附证据（[[feedback_green_tests_dont_mean_done]]）。

### Step 0 — cleanup（✅ 2026-06-14，零行为变更）

**改动文件：**
- `app/tools/runtime_tool_groups.py`：退役 `coordination_pack`(8 工具)、`plan_mode_pack`(3 工具) 两个 CORE-only group entry；`web_pack` 清出 `web_fetch`；`office_pack` 清出 `read_file`/`list_files`/`send_channel_file`（四个脚踏两船工具）。**工具一个未删**——全部仍 turn-1 可见（CORE）。
- `app/tools/handlers/subagent.py`：删 `spawn_subagent` 悬空的 `pack="coordination_pack"` decorator（pack 已退役）；顺手修 pre-existing 类型标注噪音 `base_filters: list[Any]`（消除 Pyright `ColumnElement vs BinaryExpression` 误报）。
- `app/tools/handlers/search.py`：删 `web_fetch` 冗余的 `pack="web_pack"` decorator（web_fetch 是 CORE = 脚踏两船）。
- `app/tools/audit.py`：① 新增 `assert_core_pack_disjoint()` 硬不变量（`CORE ∩ RUNTIME_TOOL_GROUPS.tools = ∅`，违反 raise）；② `audit_tool_coverage` 新增 `covered_by_core` discovery path（修既有缺陷：CORE 工具 turn-1 可见、schema 即发现信号，本不该被判 functional orphan——退役 CORE-only pack 会暴露此缺陷）。
- `app/main.py`：startup 调用 `assert_core_pack_disjoint()`，**不包在吞异常的 try 里**（违反必须 crash startup，fail-fast，部署期暴露漂移而非生产）。
- 测试：改写 `test_coordination_pack_remains_catalog_for_source_capabilities`→`test_coordination_pack_retired_source_capabilities_stay_core`、`test_request_plan_mode_in_plan_mode_pack`→`test_request_plan_mode_is_core_plan_mode_pack_retired`（两者原 pin 了要退役的 pack）；更新 `test_coverage_as_dict_schema`（新增 `covered_by_core` 键）；新建 `tests/tools/test_core_pack_disjoint.py`（6 测试）。

**发现与修正（与设计稿的偏差）：**
1. **§5.1 cut 清单全核实为活机件，禁删**：`fanout_subagents`(`subagent.py:113` recursion guard)、`SubagentJob`/`SubagentBudget`(`workflow_launch.py` 活引用)、`FALLBACK_EXECUTOR_NAME`(`runtime.py:51` `try_execute` 活兜底) 全部保留——deep-dive 的"死代码"判定是错的（critic 2 已预警）。
2. **pack.yaml manifest 确实存在**（纠正设计稿初判"无 pack.yaml"）：在 `<repo>/packs/` 与 `<backend>/packs/` **两套副本**，仅 `office_pack`/`deep_research_pack` 两包有 manifest，`find_pack_dirs` 爬祖先 `packs/` 目录。这把 Step 4-5 从"从零创建 manifest"修正为"清理+升级现有 manifest（且两套副本须同步）"。
3. **断言边界 = 只管 RUNTIME_TOOL_GROUPS（runtime 真相源），不管 manifest**：manifest(`catalog_reader.py:3`"不参与 runtime")合理地把 CORE 工具列为工作流依赖（office 的 read_file、DR 的 web_fetch）——这是 Step 4 才拆 owns/requires_core 的输入，不是 Step 0 的脚踏两船。断言纳入 manifest *owns* 留待 Step 4。

**零行为变更论证：** turn-1 always 工具 = `_ALWAYS_INCLUDE_CORE`(=`CORE_TOOL_NAMES`，本 Step 未改一字)。退役的 pack 成员全是 CORE（已 turn-1 可见）；脚踏两船工具是 CORE（清出 pack 不影响其 turn-1 可见性）。`RUNTIME_TOOL_GROUPS` 仅供 `tool_search` 懒发现 schema，删的两 pack 成员本就 turn-1 可见、无需经 `tool_search` 发现 → agent 实际工具集不变。

**验证证据：**
```
$ pytest tests/tools/test_core_pack_disjoint.py tests/services/test_agent_tools_core_surface.py \
         tests/tools/test_request_plan_mode.py tests/tools/test_audit.py -q
27 passed

$ pytest tests -q          # 全量后端回归
4399 passed, 7 skipped, 4 warnings in 58.51s   # 0 failed

$ ruff check <改动文件>     # All checks passed!
```
注：`search.py` 全文件 format-dirty 是 pre-existing（紧凑 `@tool(ToolMeta(` 风格 vs ruff 展开式，298→358 行），本 Step 只删 1 行(`git diff --stat` = 1 deletion)，按 scope-discipline 不触发存量 churn。

### Step 1 — tool-contract（✅ 2026-06-14，单源分类 + destructive + per-tool 落盘）

**改动文件：**
- `app/tools/decorator.py`：`ToolMeta` 加 `destructive: bool`（CC isDestructive 对标）+ `max_result_chars: int | None`（CC per-tool maxResultSizeChars 对标）；加常量 `RESULT_CHARS_UNLIMITED = 0`。
- `app/tools/collector.py`：`CollectedTools` 加 `destructive_names` + `result_char_limits`，collect 时从 ToolMeta 收集。
- `app/tools/registry.py`：**删 `_STATIC_READ_ONLY_TOOL_NAMES` + `_STATIC_PARALLEL_SAFE_TOOL_NAMES` 两份硬编码双定义名单**；`_LazyToolNameSet` 简化为纯 decorator 单源（去静态合并）；加 `is_destructive_tool()` + `result_char_limit_for_tool()`。
- 15 个工具 decorator 设 `max_result_chars=RESULT_CHARS_UNLIMITED`（search/communication/filesystem/skills/triggers）；5 个破坏性工具设 `destructive=True`（delete_file / retire_memory / feishu_doc_delete / feishu_base_record_delete / feishu_calendar_delete）。
- `app/kernel/engine.py`：**删硬编码 `_EVICTION_EXEMPT_TOOLS` 集合**，新增 `_resolve_eviction_threshold()`（per-tool 落盘阈值，单源 `ToolMeta.max_result_chars`），`_maybe_evict_tool_result` + microcompact 用它；新增 `_is_concurrency_safe_tool()`（parallel_safe ∧ ¬destructive），4 处并发判断改用它（破坏性工具永不并发）。
- 测试：新建 `tests/tools/test_tool_contract.py`（8 测试）。

**消除的双定义/硬编码（单源化）：**
1. **read_only/parallel_safe**：`registry._STATIC_*` 名单 ✗ → 纯 `@tool` decorator（collector）单源。验证：静态名单是 decorator 名单的真子集（static-only 差集 = ∅），删除行为保持（READ_ONLY size 46 不变）。
2. **落盘豁免**：`engine._EVICTION_EXEMPT_TOOLS` 硬编码 18 工具集 ✗ → `ToolMeta.max_result_chars=∞` decorator 单源。验证：旧集的 15 个 alive 工具与新标注的 15 个**完全一致**；死条目 `get_task`/`list_tasks`（已不存在的工具）随之清除。

**destructive 消费（§6 决策 5"先观察"）：** 一等标志 → collector 收集 → registry 查询 → engine 并发防御（`_is_concurrency_safe_tool` 确保破坏性工具即使误标 parallel_safe 也不并发，对标 CC destructive 不并发）。不进 DB（read_only/parallel_safe 同为纯运行时元数据，Tool model 无这些列）。

**验证证据：**
```
$ pytest tests/tools/test_tool_contract.py tests/tools/ -q
317 passed

$ pytest tests -q          # 全量后端回归
4407 passed, 7 skipped, 4 warnings   # 0 failed (= Step0 的 4399 + 8 新测试)

$ ruff check <核心文件>     # All checks passed!
```
注：`char_limits` 实际 16 项（15 主名 + `bing_search` alias 继承 web_search 的 ToolMeta，合理）。`memory.py:642/673`、`engine.py` 多处 Pyright ✘ 是 pre-existing 类型 narrowing 噪音（运行时正常，全量绿），非本 Step 引入，未纳入。
