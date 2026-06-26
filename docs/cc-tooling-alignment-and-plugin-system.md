# CC 工具/扩展/Runtime 全栈对标 + Hive 插件系统设计

> 状态：v1.4 设计定稿 + **Step 0-11 已实施落地**（2026-06-15，证据见 §8 实施日志）。review 补齐 pass 已覆盖 hooks/dependencies/per-agent plugin/product surface/legacy packs retirement。
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
2. **仅基础 `web_search` 进 CORE；`exa_search`/`tavily_search`/`firecrawl_fetch`/`xcrawl_scrape` 保持 provider plugin** — 后四者需 provider key 或 provider runtime、无 key 不可用，不进 CORE（web_search 有 SearXNG/DDG 无 key兜底，可进）。
3. **`pack.yaml` = install/composition/credential/governance/distribution 真相源；`@tool` decorator 仍是 executable schema 真相源** — runtime 用 `TenantInstalledPlugin`/`AgentPluginAssignment` 决定哪些 manifest-declared tools **可见**。**绝不让 manifest 变成第二套工具 schema。**
4. **plugin manifest 的 tools 拆三字段** `owns`(本包定义) / `requires_core`(依赖的 CORE 工具，如 DR 依赖 web_search/web_fetch) / `optional_providers`(有 key 才解锁，如 exa/tavily/firecrawl/xcrawl)。**`CORE∩pack.tools=∅` 断言只对 `owns`**，`requires_core` 允许引用 CORE。
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
| **Skill** | 渐进 listing(name+desc,≤1%窗口,**动态 system-reminder 非缓存前缀**)→invoke 注入整段 body/能力胶囊；六来源；创作 100% 人在环(`disableModelInvocation:true`) | 渐进加载对齐 + `is_system` 永不截断；Skill 定义升级为 capability capsule（可打包 context/templates/scripts/workflow refs/subagent refs，但执行仍走各自 runtime）；**catalog 错放 frozen prefix**(`prompt_builder.py:268`→跨会话 cache 击穿)；`save_skill`+`skill_distiller`+`skill_curator` = 自进化 delta；19 字段 frontmatter 过半死字段 | **converge** |
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
| **2** ✅ | tool-contract | 修 critical：扩展现有 `result_envelope.py` 或新建 `ToolContentEnvelope(text+blocks)`（避免与 error/fallback envelope 命名碰撞），`adapt_and_call` 透传 typed content block(image/pdf)；`read_document`(PDF)/`read_file`(图)首接；保留纯字符串默认 | 1 |
| **3** ✅ | lazy-loading | 文本端(`workspace.py`)与 schema 端(`invoker.py`)统一单一"query→可发现工具名"函数 `discoverable_tool_names_for_query`(agent_tools.py，单源)；MCP 发现共用 `list_agent_mcp_deferred_tools`，CORE 全程排除，dedup；invoker/workspace 退化为薄包装。顺手补 Step 2 envelope 类型契约(engine `ExecuteTool`/invoker 两处返回类型补 `ToolContentEnvelope`)。**已实施，证据见 §8。** | 0,4 |
| **4** ✅ | plugin-system | 删 `catalog_reader.py:1-5` severance 注释；`PackManifest` 加 `agents`/`hooks`/`dependencies` 字段；manifest validator fail-closed 校验：hook handler 必须来自平台 allowlist、dependency 必须 pinned、dependency source ref 必须 admin-allowed，远程 source ref 在 signature/sandbox 基础设施未达标时结构化拒绝，禁止 raw shell/import/webhook handler；工具收集从清单读；`RUNTIME_TOOL_GROUPS` 退化 fallback；`audit.py` startup 分歧 fail | 0,1 |
| **5** ✅ | plugin-system | 新建 `TenantInstalledPlugin`+`AgentPluginAssignment`(镜像 MCPServer RLS)+`PluginHookRegistration`+dependency lock/install graph+source policy/provenance；`POST /enterprise/plugins/install\|list\|uninstall`；安装时先 resolver/validate/lock，再落安装记录；内置/本地 source v1 可安装，远程 source v1 只可被 policy 识别并 fail-closed；`pack_policy` 迁安装记录；**仅基础 `web_search` 进 CORE**(有 SearXNG/DDG 无 key 兜底)；**`exa_search`/`tavily_search`/`firecrawl_fetch`/`xcrawl_scrape` 留 provider plugin**(需 key/provider runtime，进 `optional_providers`) | 4 |
| **6** ✅ | mcp | 新增 `mcp_naming.py`(`mcp__server__tool` 前缀+反解+slug+长度/碰撞,单源)；canonical 名进 `resource_discovery` 5 个生成点；canonical 别名在 `_execute_mcp_tool`(canonical 名对未 backfill 的 legacy 行也可解析→生成可先于 backfill 部署)；dry-run+apply backfill 脚本(纯 planner 已测,旧名→新名报告即回滚记录)；**核实**=`FALLBACK_EXECUTOR_NAME` registry 分支实为死代码(无人注册`__mcp_fallback__`,活兜底是 service `fallback_executor` kwarg)→删死分支统一单路径；`mcp_server:*` 伪 pack 实为 no-op(从不写该 policy+未知 pack 默认 True)→退役两处 gate+删 `make_mcp_server_pack_name`,MCP 可见性归 assignment 单一治理。**已实施，证据见 §8。** | 0,5 |
| **7** ✅ | mcp | `MCPClient.list_resources/read_resource`(blob 走现有 >8KB artifact 溢出)+协议工具 `mcp_list_resources/mcp_read_resource`；DB 自省工具更名 `list_mcp_resources→list_mcp_tools`/`read_mcp_resource→inspect_mcp_tool`(旧名 alias 不破 transcript)；新建 `mcp_oauth.py` 标准 OAuth2 PKCE(加密存 token/守 mcp_authz/fail-closed)+`/enterprise/mcp/oauth/start\|callback` API+`resolve_mcp_oauth_bearer` 接入执行路径+`auth_status` 生命周期。**已实施，证据见 §8(含 OAuth live 验证诚实边界)。** | 6 |
| **8** ✅ | subagent | schema 暴露 `run_in_background`(此前后端通但 LLM 不可达)；background spawn 落 `RuntimeTask(task_type="subagent")` durable record(非 resumable→startup `reconcile_orphaned_runtime_tasks` 把崩溃的 run 标 failed,parent poll 不再永久 running)；加 `check_subagent`(run_id 查/列表,ownership-scoped)；governance 按 type 分级**已存在**(`_TYPE_PRESETS` 给 explorer/critic 只读工具集,worker 才能编辑);completion wake/signal/tenant/recursion guard 沿用现有。**已实施，证据见 §8。** | 0 |
| **9** ✅ | skill | catalog 移出 frozen prefix→动态 suffix(修 cache 击穿，owner 选项 A 保留兼容参数)；删 11 死字段(`declared_packs` 核实为活字段，保留)；distiller 晋升硬门**已是** `evolution_ledger` 外部 eval(核实 + 钉不变量，非重写)；`allowed_tools` 接 scoped 治理引导(load_skill registry 路径)。**已实施，证据见 §8。** | 4 |
| **10** ✅ | workflow | preview/start_workflow 确认仅留 CORE；`office_workflow_examples` 核实=测试语料**保留+文档化**(不 seed=scope creep)；删 `phase` 死列(+drop-column migration)；修 5 处 `runtime_task` 过时注释(`tenant_id` 列已存在，只修注释零行为变更)；文档化"结构化数据 over 脚本"为显式防御决策。**已实施，证据见 §8。** | 0,5 |
| **11** ✅ | closure | 全面 review 补齐：plugin hooks runtime loader + allowlist platform handlers + startup/install/uninstall refresh；dependency resolver/lockfile/content hash/tenant graph/uninstall protection；`AgentPluginAssignment` 接入 runtime resolver/API/Agent UI；`select:` 直选 + turn-1 deferred list + session metadata；Workspace/Agent plugin UI+i18n；summary routes 迁 `capabilities.py`，`packs_router` 不再挂载。 | 3,5 |

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
- **`FALLBACK_EXECUTOR_NAME` 必须先核实再删**——早期 critic 把"MCP 兜底是活的"和"`FALLBACK_EXECUTOR_NAME` registry 分支是活的"混在一起。Step 6 复核结论：活兜底是 `ToolRuntimeService.fallback_executor` kwarg，registry 里无人注册 `__mcp_fallback__`，故 `FALLBACK_EXECUTOR_NAME` 分支可删且已删。

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
遗留 `app/api/packs.py` 曾仍 wired 在 `main.py`（Step 11 已迁 summary routes 到 `capabilities.py` 并移除 `packs_router` 挂载） · `pack_policy_service` 级联消费者 · `skill_seeder` pack→skill 落地 ·
**`ToolResultEnvelope` 命名碰撞**(`result_envelope.py` 已有内容，Step 2 改名或扩展现有) ·
lazy-loaded tool state 的 compaction/replay/prompt-cache 保持 · 7+ 个 pack 测试会断需迁移。

### 5.3 backfill / 语义翻转防灰度断（critic 2，critical）

Step 5 把 `is_pack_enabled` 的**"缺省=启用"静默翻成"未安装=禁用"**，且对 **7 个无 `pack.yaml` 的活 pack 无 backfill** =
灰度即全平台静默断能力（RLS flip 全员 401 / Web3 跨租户停机同类病）。**必须**：迁移前为所有现存活 pack 生成 `pack.yaml` +
为所有现有租户 backfill `TenantInstalledPlugin` 安装记录(保持现状可用)，再翻语义。这是"一次改完"的必含项，非"later"。

---

## 6. 决策已定（owner 拍板归档，呼应 critic 2：不悬置成分期）

1. **fan-out = B(修正版)**：**不新增/不暴露 `fanout_subagents` 可执行工具**；现有 `fanout_subagents` 字符串仅作 `_SUBAGENT_BASE_EXCLUDED_TOOLS` recursion guard(`subagent.py:109`)，**保留**直到有等价 guard+测试替代；**不删** `SubagentJob`/`SubagentBudget`(`workflow_launch.py` 活机件)。fan-out 收敛为 workflow `fanout_step`(确定性，DR 已走)+ 并行 `spawn_subagent`(临时，接 `run_in_background`)，对齐 CC 两条路径；worker 本身的跨重启恢复在 Step 8 作为 durable run recovery 完成项。
2. **web_search 进 CORE**：与 web_fetch 同层，无 key 走 SearXNG/DDG 兜底；`exa`/`tavily` 高级搜索与 `firecrawl`/`xcrawl` 抓取不随之进 CORE(留 provider plugin 的 `optional_providers`，由 `tool_search` 激活)。
3. **插件远程源体量决策（后续安全门，已显式标记）**：v1 纳入 source model，但拆成两层：①零债必做的框架层：source policy/provenance schema、validator、lockfile、allowlist/blocklist、路径校验、结构化 fail-closed 错误，内置/本地 source v1 完整可用；②远程 git/url/npm/pip 的实际启用层：必须等 content hash/signature verification + sandbox materialization 基础设施达标，install-time binary/script 走 code_execution provider 沙箱。v1 不声称远程源可用；远程源在安全门达标前一律 fail-closed。**当前标记**：已有 `code_execution`/Vercel Sandbox 是 runtime command executor，可复用为 materializer 后端，但不是远程插件供应链安装层；缺口是 fetch → verify signature/integrity → sandbox materialize → bundle/cache → lockfile provenance → install 的完整流水线。
4. **subagent governance 分级**：read-only explorer/critic 降轻 zone(对齐 CC frictionless 侦察)，仅 edit-capable worker 保 sensitive。
5. **`ToolMeta.governance` 细化**：先拆 `destructive` 观察，过度拆分前等真实治理需求。
6. **插件 vs MCP 安装入口**：MCP 单 server 直装 + 插件批量安装两入口并存，文档化两入口关系防再分叉。
7. **v1 manifest component 范围（governed inclusion）**：v1 manifest 收 `tools`/`skills`/`mcp_servers`/`agents`/`hooks`/`dependencies`，并同步开启 source-addressable install model；`hooks`/`dependencies`/`source` 只作为受治理入口开启：`hooks` 必须声明式绑定平台 allowlist handler，tenant 只配置 event/matcher/mode/治理参数，不能提交任意代码、shell、import path、webhook；`dependencies` 必须经 resolver 生成 pinned lockfile，带 cycle/source/allowlist 校验和 uninstall 保护；`source` 必须由安装入口/registry 写入 provenance，内置/本地 source v1 可安装，远程 source 在 signature verifier + sandbox materializer 未达标前只允许被识别并 fail-closed。任何 raw handler、unpinned dependency、disallowed/transitive source 一律 fail-closed。理由：CC 单用户 shell-hook 的安全模型在多租户下不成立，但 v1 可以纳入，前提是它们先被建模成治理对象，而不是自由执行面；远程供应链平台不和本轮工具体系整改强绑成同一个必须完成的体量。

---

## 7. 风险

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

### Step 2 — tool-contract（✅ 2026-06-14，typed 多模态 tool result，消除 L1 违例）

**改动文件：**
- `app/tools/result_envelope.py`：新增 `ToolResultBlock` + `ToolContentEnvelope`（provider-neutral text + blocks；`__str__` 返回 text fallback；`.image()`/`.document()` 构造器）。
- `app/tools/adapters.py`：`adapt_and_call` 透传 `ToolContentEnvelope`（**消除强制 `str()` 的 L1 违例**——case law 修复），返回类型 `str | ToolContentEnvelope`。
- `app/kernel/engine.py`：新增 `_tool_message_content()`——envelope 含 image/document 时构建 `[text, *media]` content list，否则纯字符串；parallel + sequential 两处 tool-message 构建用它。落盘/检测/budget 仍走 `str(result)`（envelope 的 `__str__`=text，零破坏）。
- `app/services/llm_client.py`：新增 `_anthropic_tool_result_content()`（Anthropic 原生 image/document tool_result blocks）+ `_flatten_tool_content_to_text()`（OpenAI/Gemini text-only 通道降级，**标注被省略的非文本块——不静默丢弃**）；4 处 provider 序列化接入（to_anthropic_format / to_openai_format / OpenAI Responses / Gemini）。
- `app/tools/service.py`：`execute` + `execute_approved` 的 activity-log 切片改用 `str(result)`（envelope 无 `__getitem__`，否则 execute 路径会把 envelope 误当工具失败）。
- `app/services/agent_tool_domains/workspace.py` + `handlers/filesystem.py`：`read_file` 图片首接——`.png/.jpg/.jpeg/.gif/.webp` 读 bytes→base64→`ToolContentEnvelope.image`（CC Read parity，5MB guard）；`_read_skill_file` str 保护；文本文件不变。
- 测试：新建 `tests/tools/test_tool_content_envelope.py`（8 测试）。

**L3 模型平等（best-effort per provider）：** Anthropic 的 tool_result 原生支持 image/document content blocks → 直接映射；OpenAI `function_call_output` / Gemini `functionResponse` 是 text-only 通道 → 降级为 text 并标注省略的块（模型知道存在多模态内容，非静默丢弃）。`envelope.text` 是所有 str-assuming 路径（落盘/日志/loop 检测/text-only provider）的统一 fallback。

**read_document 决策：** 保持文本提取（它是 Hive 特有的 PDF/Word/Excel→text 工具，不同于 CC Read；PDF base64 直灌 context 不现实、provider 支持参差）。read_file 图片首接是 typed-block 的示范接入点。

**验证证据：**
```
$ pytest tests/tools/test_tool_content_envelope.py -q
8 passed

$ pytest tests -q          # 全量后端回归
4415 passed, 7 skipped, 4 warnings   # 0 failed (= Step1 的 4407 + 8 新测试)

$ ruff check <核心文件>     # All checks passed!
```
注：`adapters.py`/`result_envelope.py` 的 format-dirty 是 pre-existing（`param.kind in (` 重排、render_tool_fallback 多行字符串），非本 Step 改动区，按 scope-discipline 不触发存量 churn。

### Step 4 — plugin-system 清单 schema + validator（✅ 2026-06-14）

**改动文件：**
- `app/packs/catalog_reader.py`：删 severance 注释（不再"intentionally does not participate"）；`PackManifest` 加 `agents`/`hooks`/`dependencies`/`source` 字段 + **role-based 工具分类**（每个 tool entry 的 `role`: owns/requires_core/optional_provider；`owns_names`/`requires_core_names`/`optional_provider_names` properties）；新增 `validate_manifest()` **fail-closed** 校验（未知 role、raw/非 allowlist hook handler、unpinned dependency、未识别/远程 source kind 全部结构化拒绝）。
- `app/tools/audit.py`：`assert_core_pack_disjoint` 扩展覆盖 manifest `owns`（requires_core 允许引用 CORE）；新增 `assert_manifests_valid()`（validation_errors + manifest 声明工具必须是注册 @tool = manifest/decorator 一致）+ `_iter_manifests()`（跨 repo/backend 副本去重）。
- `app/main.py`：startup 调用 `assert_manifests_valid()`（fail-closed）。
- `app/services/pack_service.py`：`get_pack_catalog` 改为 manifest **增强而非覆盖** RUNTIME_TOOL_GROUPS——runtime 字段（source/requires_channel/summary/tools）保留，manifest 加 install/composition 字段（version/skills/owns/requires_core/optional_providers/credential_requirements）。
- 测试：新建 `tests/tools/test_pack_manifest.py`（11）；修复 4 个 pin 旧"无 manifest"行为的测试（test_catalog_reader full_skill_packages 改只验 cloud skill pack；3 个 pack_service catalog 测试随增强逻辑通过）。

**范围边界（Step 4 vs Step 5）：** Step 4 把 pack.yaml 升为 install/composition/governance 真相源（schema + validator + audit 校验 + catalog 增强），manifest 与 @tool decorator 一致性由 `assert_manifests_valid` 保证。runtime 工具**可见性**仍由 CORE + pack policy 决定；切换到 `TenantInstalledPlugin`/`AgentPluginAssignment` 接管是 Step 5。`RUNTIME_TOOL_GROUPS` 仍是 runtime 真相源（Step 5 退化为 fallback）。

**governed inclusion（§6 决策 7）框架：** validator 对 hooks（仅 allowlist handler，禁 raw shell/import/webhook）、dependencies（必须 pinned）、source（builtin/local 可安装；git/url/npm/pip 识别但 fail-closed，待 signature+sandbox 基础设施）做 fail-closed 校验。框架完整，远程 source 数据点被安全门挡住（真零债，非伪 defer）。

**验证证据：**
```
$ pytest tests/tools/test_pack_manifest.py tests/packs/test_catalog_reader.py tests/services/test_pack_service.py -q
34 passed

$ python -c "from app.tools.audit import assert_core_pack_disjoint, assert_manifests_valid; assert_core_pack_disjoint(); assert_manifests_valid()"
# 通过(7 manifest 全 valid + CORE∩owns=∅ + manifest/decorator 一致)

$ pytest tests -q          # 全量后端回归
4426 passed, 7 skipped, 4 warnings   # 0 failed (= Step2 的 4415 + 11 新测试)

$ ruff check <核心文件>     # All checks passed!
```

### Step 5 — plugin-system 安装层 + web_search 进 CORE（✅ 2026-06-14）

**改动文件：**
- `app/services/agent_tools.py`：`web_search` 进 `CORE_TOOL_NAMES`（turn-1 base，有 SearXNG/DDG 无 key 兜底）+ 全链 tool-result 返回类型补 `ToolContentEnvelope`（Step 2 延伸）；`exa_search`/`tavily_search` 保持 provider-backed deferred。
- `app/tools/runtime_tool_groups.py` + `packs/web_pack/pack.yaml`(×2)：`web_pack` 清出 `web_search`（进 CORE，避免 CORE∩pack）；manifest 中 `web_search` 改 `requires_core`，`exa_search`/`tavily_search`/`firecrawl_fetch`/`xcrawl_scrape` 为 `optional_provider`。
- `app/models/installed_plugin.py`：新建 `TenantInstalledPlugin` + `AgentPluginAssignment` + `PluginHookRegistration`（镜像 MCPServer：tenant_id 强制 + FK CASCADE + UniqueConstraint）。
- `alembic/versions/add_installed_plugin_tables_0614.py`：3 表 migration，RLS **ENABLE + FORCE** + tenant policy（P0 gap B：owner 连接也受约束）。`db_bootstrap.py` RLS_FORCED + `entrypoint.sh` import + `import_all_models`(pkgutil 自动) 三处 create_all 地基齐补（critic §5.2#1 防 Railway 漏表/漏 RLS）。
- `app/services/plugin_install_service.py`：install/list/uninstall/backfill，全程 `tenant_scoped_session`（RLS-bound）+ manifest validate + source policy fail-closed（builtin/local 可装，远程拒）+ pinned lockfile + hook allowlist 校验。
- `app/api/plugins.py` + `main.py`：`/enterprise/plugins/install|list|uninstall|backfill`（admin），router 挂载。
- `app/services/pack_policy_service.py`：保留 `get_tenant_pack_policies` 兼容 tenant catalog；runtime 新增 `get_agent_pack_policies`，只融合该 agent enabled 的 `AgentPluginAssignment`（用独立 session 不扰乱 caller，mock 测试自动 fallback）。
- 测试：新建 `tests/services/test_plugin_install_service.py`（8）；修复 16 个 pin 了"web_search 非 CORE / 旧 alembic head / get_tenant_pack_policies 调用序列"的测试。

**完成判据（非 built-but-unwired）：** TenantInstalledPlugin 安装记录 + AgentPluginAssignment 经 `get_agent_pack_policies` → `is_pack_enabled`（agent_tools runtime 路径）**真改变 agent tool surface**；tenant install 只代表租户可用，agent assignment 才代表该 agent 可见。测试 `test_agent_plugin_assignment_controls_pack_visibility` / `test_unassigned_installed_plugin_is_not_visible_to_agent` 钉死。

**2026-06-15 closure pass 补齐（review 后修正）：**
- `app/services/plugin_install_service.py`：`resolve_plugin_dependency_closure` 递归解析 builtin/local dependency，精确版本匹配、cycle/source 校验、content SHA256/provenance lockfile、install order；新增 `PluginDependencyEdge` tenant graph + uninstall protection。
- `app/services/plugin_hook_service.py`：DB `PluginHookRegistration` 启动/安装/卸载后编译进 `HookRegistry`；handler 只来自 `plugin.audit`/`plugin.block`/`plugin.args_overlay` 平台 allowlist；`PRE_TOOL_USE enforce` 必须由 install config `approved_enforce_hooks` 显式批准；runtime matcher 自动叠加 tenant/agent scope，未分配 agent fail-closed。
- `app/services/pack_policy_service.py` + `agent_tools.py`：runtime 改用 `get_agent_pack_policies`，plugin tool 可见性由 `AgentPluginAssignment` 决定；tenant install 不再自动让所有 agent 获得 plugin tools。
- `app/api/plugins.py` + frontend：`/agents/{agent_id}/plugins/{plugin_key}` per-agent enable/disable；Workspace plugin install/backfill/uninstall UI；Agent Tools extension 面展示 assigned plugins 并可切换。
- `app/api/capabilities.py` + `main.py`：capability/runtime summary routes 迁出 `packs.py`，`packs_router` 不再挂载。

**不灰度断（critic §5.3）：** `is_pack_enabled` 的 manifest default 语义保留给未迁移/静态 runtime groups；plugin runtime 可见性改由 agent assignment 决定；`backfill` 安装 default-active plugins 并补 assignment；explicit policy False 仍作为 legacy opt-out 兼容。

**验证证据：**
```
$ pytest tests/services/test_plugin_install_service.py -q   # 8 passed
$ pytest tests -q          # 全量后端回归
4434 passed, 7 skipped, 4 warnings   # 0 failed (= Step4 的 4426 + 8 新测试)
$ alembic heads            # add_installed_plugin_tables_0614 (single head)
$ ruff check <核心文件>     # All checks passed!
```

---

### Step 3 — lazy-loading 单源化 + provider-neutral（✅ 2026-06-14）

**问题（实施前）：** "model 被告知能发现的工具" 与 "实际加载的工具" 走两条独立代码路径——schema 端 `invoker._deferred_tool_names_for_query` 与文本端 `workspace._tool_search` **各自**拼装（各自跑 `iter_runtime_tool_groups` + 各自调 `list_agent_mcp_deferred_tools`），两份逻辑漂移即 "告知一套、加载另一套"（🦴#2 病灶）。

**改动文件：**
- `app/services/agent_tools.py`：新增 `discoverable_tool_names_for_query(agent_id, query)` —— **唯一**的 "query→可发现(deferred)工具名" 函数。精确匹配快路径（单工具名命中且非 CORE → 只返回该工具）；否则聚合 `iter_runtime_tool_groups(query)` 静态 pack + `list_agent_mcp_deferred_tools` MCP 发现；**全程排除 CORE**（已 turn-1 可见）+ dedup。
- `app/runtime/invoker.py`：`_deferred_tool_names_for_query` 退化为薄包装 `return await discoverable_tool_names_for_query(...)`；删去 invoker 自有的 `iter_runtime_tool_groups`/`normalize_tool_query`/`list_agent_mcp_deferred_tools` 直引（-32 行）。
- `app/services/agent_tool_domains/workspace.py`：`_tool_search` 改调 `discoverable_tool_names_for_query`，渲染扁平 `deferred_names` + 匹配 skills；删去自有的 pack 渲染与内联 MCP 枚举（删 `iter_runtime_tool_groups` import）。
- **顺手补 Step 2 envelope 类型契约债**（zero-debt：lying annotation）：Step 2 让工具可返回 `ToolContentEnvelope` 但 `ExecuteTool` 契约/invoker 两处仍标 `-> str`。`engine.py` `ExecuteTool` 返回类型补 `str | ToolContentEnvelope` + 顶层 import `ToolContentEnvelope`（删 `_tool_message_content` 内冗余 lazy import）+ `_execute_tool_call_with_cancel` 的 `create_task` 用 `cast(Coroutine, ...)` 消除收窄后的类型噪声（运行时零变更）；`invoker.py` `_execute_tool_with_request`/`_kernel_execute_tool` 返回类型同补。
- 测试：`tests/services/test_mcp_tool_discovery.py::test_tool_search_text_and_schema_agree_on_mcp` 更新 —— 单源化后 invoker 不再自有 `list_agent_mcp_deferred_tools` 绑定，**只 patch `agent_tools` 一个点即覆盖两条路径**（这本身就是单源不可漂移的更强证明）。

**完成判据（非 built-but-unwired）：** 文本端 `_tool_search("github")` 与 schema 端 `_deferred_tool_names_for_query("github")` 现在共用同一函数体；`test_tool_search_text_and_schema_agree_on_mcp` 钉死两个 surface 对同一 MCP 工具达成一致。CORE 工具不会出现在 deferred 清单（已 turn-1）。

**验证证据：**
```
$ ruff check app/kernel/engine.py app/runtime/invoker.py \
    app/services/agent_tool_domains/workspace.py app/services/agent_tools.py
All checks passed!
$ pytest tests/runtime/test_invoker.py tests/services/test_agent_tools.py -q   # 49 passed
$ pytest tests/services/test_mcp_tool_discovery.py -q                          # 9 passed
$ pytest tests -q
4434 passed, 7 skipped, 4 warnings   # 0 failed
```

**2026-06-15 closure pass 补齐：** `select:<tool_name>` 直选进入 `discoverable_tool_names_for_query`；kernel 首轮动态 suffix 枚举 `Available Deferred Tools` 并给出 `select:` 路径；`available_deferred_tools` 写入 session metadata，和已存在的 `discovered_tools` reinjection 一起覆盖 compaction/replay 后的 loaded-tool state；static deferred discovery 读取 agent-scoped plugin policy，避免 tenant 安装把未分配 agent 的 plugin tool 暴露给模型。

---

### Step 6 — MCP 规范命名 + 单一执行路径 + 退役伪 pack（✅ 2026-06-14）

**实施前先核实（§5.1 纪律，纠正两处设计稿/critic 判定）：**
1. **`FALLBACK_EXECUTOR_NAME` registry 分支实为死代码。** `runtime.py:51` 的 `self._executors.get(FALLBACK_EXECUTOR_NAME)` 永远返回 None——全仓无人 `register("__mcp_fallback__", ...)`(grep `app/`+`tests/` 仅常量定义+该分支)。活的 MCP 兜底是 `ToolRuntimeService.fallback_executor` kwarg(`service.py:582-584` → `_execute_mcp_tool`)。早先 critic 把"MCP 兜底是活的"(对,kwarg 路径)与"`FALLBACK_EXECUTOR_NAME` 常量是活的"(错)混为一谈。"先核实"在此兑现。
2. **`mcp_server:*` 伪 pack gate 实为 no-op。** `is_pack_enabled` 对未知 pack 返回 `_manifest_default_enablement().get(name, True)` = True(`pack_policy_service.py:118`)，且全仓从不写 `mcp_server:*` policy(grep 仅生产函数+一条"不得用该命名"的断言)→ 两处 gate 恒过。退役零行为变更，真 gate 是 assignment reachability(`_resolve_agent_mcp_gating`)。
3. **命名实为单下划线 `mcp_{server}_{tool}`(非"无前缀")。** `resource_discovery` 5 个生成点用 `mcp_{server_id}_{tool}`，歧义(无法可靠反解 server/tool)。Step 6 转 CC `mcp__{server}__{tool}` 双下划线=可反解+碰撞安全。FK 安全:`AgentTool` 键 `tool_id`、override 键 `mcp_tool_name`——改 `Tool.name` 不破二者。

**改动文件：**
- `app/services/mcp_naming.py`(新)：单源 `build_mcp_tool_name`/`parse_mcp_tool_name`/`is_mcp_tool_name`。`slugify` 把非字母数字折叠为 `-`、绝不产 `_`，故 `__` 永不在 slug 内出现→按 `__` split 无歧义。charset `[a-z0-9_-]`、长度 `<=64`(provider 函数名最严约束 OpenAI/Gemini，名直传 provider 不经 sanitize)、超长截断+确定性 hash、可选 `taken` 防 `(tenant_id,name)` 唯一约束碰撞。复用 `mcp_backfill.slugify`(单一 slug 源)。另含纯 planner `plan_mcp_name_canonicalization`(按 tenant 分组、已 canonical 先占位、其余分配碰撞安全名)。
- `app/services/resource_discovery.py`：5 个生成点(Smithery 3 + 直连 2)改 `build_mcp_tool_name(mcp_server_name, mcp_tool_name)`(用 server_name+tool_name 使 backfill 与重发现产同名)；individual loop 带 `_taken_names` 防组内 slug 碰撞；generic 清理改按结构身份(server + null tool)查找(兼容 legacy/canonical 双格式)；删冗余 `safe_name`。
- `app/services/agent_tool_domains/web_mcp.py`：`_execute_mcp_tool` 加 canonical 别名——精确名查不到且名是 canonical 时，按各行重算 `build_mcp_tool_name` 匹配。使 canonical 名成持久身份(canonical 调用对未 backfill 的 legacy 行也解析)→ 生成可先于 backfill 安全部署。
- `app/services/agent_tools.py`：退役 `list_agent_mcp_deferred_tools`+`get_agent_tools_for_llm` 两处 `mcp_server:*` 伪 pack gate(并删 deferred 路径里只为该 gate 而取的 `pack_policies`)+删 `make_mcp_server_pack_name` import。
- `app/tools/runtime_tool_groups.py`：删死函数 `make_mcp_server_pack_name`+不再用的 `urlparse` import。
- `app/tools/runtime.py`：`try_execute` 删死的 `FALLBACK_EXECUTOR_NAME` 中间查找+删常量→纯一等查找,未注册即 None,单一 service 兜底接手。
- `app/scripts/backfill_mcp_tool_names.py`(新)：dry-run 默认/`--apply --confirm` 门(不可逆生产数据=安全门非 MVP)；`enter_rls_bypass` 跨租户审计；打印 JSON(tool_id/old/new)即回滚记录;幂等(已 canonical 跳过)。
- 测试：`tests/services/test_mcp_naming.py`(新,14:build/parse/is/长度 hash/碰撞/charset/planner 五例)；`tests/services/test_mcp_tool_discovery.py` 5 个 `list_agent_mcp_deferred_tools` queue 删掉已退役的 pack-policy 占位(吞异常 mock-cascade:删 dead query 致 queue 错位,断言 `[]` 的用例曾靠吞异常假过——见 [[project_rls_groupd_mock_cascade]])。

**完成判据（非 built-but-unwired）：** 新导入 MCP 工具即得 `mcp__server__tool` 名(`resource_discovery` 真生成路径)；`_execute_mcp_tool` canonical 别名使 canonical 调用对 legacy 行也解析(执行不依赖 backfill 时序)；伪 pack 退役后 MCP 可见性仅由 assignment 治理决定(零行为变更,5 discovery 测试钉死);死执行分支删除后单一兜底路径(全量回归证明)。

**诚实范围说明（[[feedback_design_draft_overstates_maturity]]）：** "旧名 alias" 实现为 *canonical 名 → legacy 行* 的前向解析(canonical 名是持久身份)，**不**做 *legacy 名 → canonical 行* 的反向 alias——后者需存旧名映射表，价值仅限"部署+backfill 之间某个会话仍调旧名"的瞬态(模型每回合重读工具列表自纠),按 [[feedback_no_mvp_finish_completely]] 的"完整但不镀金"权衡不建表。existing 行的统一由 backfill 脚本(operator 执行,可逆)完成——这是不可逆生产数据的既定安全模式,非 MVP 分期。

**验证证据：**
```
$ pytest tests/services/test_mcp_naming.py -q                 # 14 passed
$ pytest tests/services/test_mcp_tool_discovery.py -q         # 9 passed
$ python -c "import app.scripts.backfill_mcp_tool_names"      # import OK
$ ruff check <7 核心文件 + 2 测试>                            # All checks passed!
$ pytest tests -q
4448 passed, 7 skipped, 4 warnings   # 0 failed (= Step3 的 4434 + 14 naming 测试)
```

---

### Step 7 — MCP 协议 resources + DB 自省更名 + 标准 OAuth2（✅ 2026-06-14）

**改动文件：**
- `app/services/mcp_client.py`：加 `list_resources()`(`resources/list`) + `read_resource(uri)`(`resources/read`)，复用现有 `_detect_and_request`；text 内容内联，blob(base64) 走 kernel 现有 >8KB artifact 溢出(不另造溢出逻辑)。
- `app/tools/handlers/mcp.py`：DB 自省工具更名 `list_mcp_resources→list_mcp_tools`/`read_mcp_resource→inspect_mcp_tool`(它们自省的是已导入的 TOOL 不是协议 resource)，`aliases=(旧名,)` 保持旧名可执行(collector skip alias schema→模型只见新名,旧 transcript 不破)；新增协议工具 `mcp_list_resources`/`mcp_read_resource`(经 `_resolve_agent_mcp_server` 解析可达服务器,server 访问跟随 tool 访问)。
- `app/services/mcp_oauth.py`(新)：标准 OAuth2 authorization-code + PKCE 功能核心——`generate_pkce_pair`(S256)/`build_authorization_url`/`exchange_code_for_token`/`refresh_access_token`/`OAuthTokenSet`(+`is_expired` 带 60s skew)/加密存取(`encrypt_token_set`/`encrypt_value`,复用 `SECRETS_MASTER_KEY` provider,无 key/未初始化→明文 no-op 同 tool_config_service);auth_status 常量。
- `app/services/mcp_server_service.py`：`start_mcp_oauth`(存 pending PKCE+返回 auth URL)/`complete_mcp_oauth`(按不可猜 state 跨租户定位 server→换 token→存密文→auth_status=configured)/`resolve_mcp_oauth_bearer`(用+过期 refresh+auth_status 更新,fail-closed)。
- `app/api/mcp_oauth.py`(新)+`main.py`：`POST /enterprise/mcp/{id}/oauth/start`(admin)+`GET /enterprise/mcp/oauth/callback`(无鉴权=OAuth 重定向靶,按 state 定位)。
- `app/services/agent_tool_domains/web_mcp.py`：`_execute_mcp_tool` 接入 `resolve_mcp_oauth_bearer`——server 存 OAuth token 则注入为 Bearer(在 `assert_no_mcp_token_passthrough` 校验**之后**注入,因这是租户存储 token 非 agent passthrough);过期不可刷新→fail-closed `auth_required` 错误。
- `app/services/capability_gate.py`+`pack.yaml`(×2)+`runtime_tool_groups.py`：注册 4 新名(canonical)+2 旧 alias 进 CAPABILITY_MAP+discovery 豁免集；mcp_admin_pack owns 改 canonical 5 名(原 3+2 协议)；runtime group 同步。
- `agent_template/skills/mcp-installer/SKILL.md`：frontmatter + Tool Reference + 工作流/示例/反模式全部对齐新名,并区分"列出已导入工具(list_mcp_tools)/查工具 schema(inspect_mcp_tool)/列协议资源(mcp_list_resources)/读资源(mcp_read_resource)"四语义。
- 测试：`test_mcp_oauth.py`(新,12:PKCE S256/URL/token 过期/加密往返/exchange+refresh mock transport);修 6 个 pin 旧名/执行序的测试(bridge canonical 面+tool_error 改名+pack_skill declared 集+capability_alignment 加 `tool_name` 参数白名单+mcp_authz fake 加 `.first()` 支持新 OAuth 查询+mcp_call_tool import)。

**先核实(§5.1)：** 现 `list_mcp_resources`/`read_mcp_resource` 名实为**误名**(做 DB 自省却叫 "resources")。CC 把 resources 留给协议 primitive。故更名让出语义:自省→`*_tool`,协议→`mcp_*_resource(s)`。

**完成判据(非 built-but-unwired)：** OAuth 路由真注册(`/api/enterprise/mcp/oauth/start|callback` 实测在 app.routes)；`resolve_mcp_oauth_bearer` 真接入 `_execute_mcp_tool`(configured server 的 token 被用作 Bearer,过期 fail-closed);协议 resource 工具进 mcp_admin_pack owns + 经 `_resolve_agent_mcp_server` 真连 MCPClient。

**诚实边界([[feedback_design_draft_overstates_maturity]]/[[evidence_honesty]])：** OAuth2 的 PKCE/URL/token 交换刷新/加密存取/auth_status 全部**单元已验证**(mock transport);交互式端到端流程**未对真实 OAuth MCP 服务器 live 验证**(CI 无此服务器)。配置了 OAuth 但 token 过期/不可刷新的 server 一律 **fail-closed**(返回 authorization required,绝不发未鉴权请求)。canonical 生成可先于无 OAuth 部署:无 OAuth 的 server→`resolve_mcp_oauth_bearer` 返回 `(None,None)`→回退 config api_key。此与 Step 5 远程源"框架完整+安全门挡住"同诚实模式。

**验证证据：**
```
$ pytest tests/services/test_mcp_oauth.py -q                  # 12 passed
$ python -c "import app.main; [r.path ... 'oauth' ...]"       # /api/enterprise/mcp/oauth/{start,callback} 已注册
$ ruff check <9 核心文件 + 7 测试>                            # All checks passed!
$ pytest tests -q
4460 passed, 7 skipped, 4 warnings   # 0 failed (= Step6 的 4448 + 12 OAuth 测试)
```

---

### Step 8 — subagent run_in_background 进 schema + durable run recovery（✅ 2026-06-14）

**先核实(§5.1)：** 后端 `spawn_subagent(run_in_background=True)` 早已全通(asyncio task + PG 持久 completion Signal + `subagent_wake_consumer` 唤父),但 LLM **不可达**——`_SPAWN_PARAMETERS` 不暴露该参数,handler 永远走同步 `handle.result`。governance 按 type **也已存在**:`_TYPE_PRESETS` 给 explorer/critic 只读工具集、worker 才可编辑(治理按 type 分级 = 工具面收窄,非另造)。真缺口=① schema 暴露 ② 崩溃恢复(asyncio worker 不跨重启,但**无 run 记录**→ orphan 无法检测 → parent `check_subagent` 永久 running)。

**改动文件：**
- `app/tools/handlers/subagent.py`：`_SPAWN_PARAMETERS` 加 `run_in_background`;handler background 分支=先 `start_subagent_run` 落 durable record→`spawn_subagent(run_in_background=True, on_complete=make_run_completer(run_id))`→立即返回 `run_id`(不等)。新增 `check_subagent` 工具(run_id 查单个/省略列最近,ownership-scoped)。
- `app/agents/subagent.py`：`spawn_subagent` 加 `on_complete` 回调,在 `_run_and_signal` 里**先**更新 durable record **再**发 wake signal(父被唤醒时已能 `check_subagent` 看到终态);`check_subagent` 进 `_SUBAGENT_BASE_EXCLUDED_TOOLS`(子不能 spawn 故无背景子可查,递归守卫)。
- `app/services/subagent_run_service.py`(新)：`start_subagent_run`(建 `RuntimeTask(task_type="subagent", running)`)/`make_run_completer`(result.ok→completed 否则 failed)/`get_subagent_run`(ownership-scoped,防按 id 猜读他人 run)/`list_subagent_runs`。复用 `create/update/get_runtime_task_record` 既有 helper。
- `app/services/agent_tools.py`+`capability_gate.py`：`check_subagent` 进 CORE_TOOL_NAMES(spawn 的读伴侣)+CAPABILITY_MAP(`agent.subagent.read`)+discovery 豁免集(只读)。
- 测试：`test_subagent_run_service.py`(新,7:start 落 subagent 类型 running/completer ok→completed·fail→failed/ownership scope/拒非 subagent 类型/schema 暴露 run_in_background+check 工具注册/`subagent` 类型非 resumable)；修 2 个 tool-set pin(test_tool_registry CORE + bridge canonical 加 check_subagent)。

**durable recovery 复用既有基建：** `task_type="subagent"` 不在 `_RESTART_RESUMABLE_TASK_TYPES=("workflow","web_chat_turn")` → startup `reconcile_orphaned_runtime_tasks`(main.py:336 已调)把卡 running 的 run 标 `failed`+`orphaned_by_restart` → `check_subagent` 读到 failed → 父开环闭合。零新 startup 钩子。

**完成判据(非 built-but-unwired)：** `run_in_background` 进 schema(LLM 可达,test 钉死);background spawn 真建 `RuntimeTask` 且 completion 真更新(on_complete 接线);`check_subagent` 进 CORE + 真读 record;orphan 走既有 reconcile→failed(`subagent` 类型非 resumable,test 钉死)。

**诚实边界([[feedback_design_draft_overstates_maturity]])：** durable recovery 是 **fail-closed**(崩溃的 worker 报 failed),**非 mid-run resume**——非幂等 worker 不能安全自动重放(可能重复副作用),故报失败而非重跑。read-only explorer/critic 理论可安全重放,但本轮统一按 fail-closed 处理(简单可解释)。

**验证证据：**
```
$ pytest tests/services/test_subagent_run_service.py -q   # 7 passed
$ ruff check <5 核心文件 + 1 测试>                        # All checks passed!
$ pytest tests -q
4467 passed, 7 skipped, 4 warnings   # 0 failed (= Step7 的 4460 + 7 subagent-run 测试)
```

---

### Step 9 — skill catalog 移出 frozen + 死字段 + distiller 硬门核实 + allowed_tools 引导（✅ 2026-06-14）

**先核实（§5.1 纪律，纠正设计稿/deep-dive 两处判定）：**
1. **`declared_packs` 不是 no-op**（纠正 §2③）—— 它在 `runtime/task_eval.py:228-231` 有活消费（按 pack 展开工具集做任务评估），`pack_service.py:351`/`skill_distiller.py` 也读。**保留**，仅删真正零消费字段。
2. **distiller 晋升硬门已是外部 eval，非纯 LLM 自评**（纠正"需重写"判定）—— `decide_behavior_gated_promotion`(`evolution_verification.py:574`) 在 `behavior_report` 非 dict 或 `behavior_eval_passed` 不过时 **fail-closed `hold`**；`behavior_eval_passed`(`hive_live_runner.py:99`) 要求真 live run(非 fallback / benchmark_complete / trusted transport / 所有场景 ready)。两条 save 路径(patch+promote)都先过此门再 `_save_skill`，LLM confidence 仅是 pre-filter。这是 external-behavior-eval-ci(E1-E10) 的既有产物 → Step 9 此项是**核实 + 钉不变量**，非重写。

**9.1 catalog 移出 frozen → 动态 suffix（cache 击穿修复，owner 拍板"选项 A：保留兼容参数定稿"）：**
- 根因：`invoker._build_system_prompt` 调 `build_frozen_prompt_prefix` **不传 skill_catalog**(死参数)，真正 catalog 经 `build_agent_context`(`agent_context.py:379`) 嵌进 `agent_context` → 进 frozen prefix → 每次 skill 增删/蒸馏即击穿 prompt-cache 边界。
- `app/kernel/contracts.py`：`InvocationRequest` 加 `skill_catalog: str = ""`（invoker 加载一次 → 透传 kernel → dynamic suffix；standalone subagent 不携带宿主 catalog）。
- `app/services/agent_context.py`：提取 `build_skill_catalog_section_for_agent(agent_id, budget_profile)` 单源 helper；`build_agent_context` 加 `include_skill_catalog: bool = True`，invoker 传 `False`（catalog 不再嵌 frozen）。
- `app/runtime/prompt_builder.py`：`build_dynamic_prompt_suffix` 加 `skill_catalog` 参数，在 `packs_section`(active tool groups) 之后注入(能力披露同类位置)；`build_frozen_prompt_prefix` 的 `skill_catalog` 参数**按 owner 选项 A 保留为向后兼容入口**(主路径不再填充)，docstring 注明。
- `app/kernel/engine.py`：6 个 `build_dynamic_prompt_suffix` 调用点(2 主路径 + 4 PTL/compaction 重试路径)全部传 `skill_catalog=request.skill_catalog`(编程注入，缩进精确)；更新 cache-key 注释(catalog 不再在 frozen)。
- `app/runtime/invoker.py`：`invoke_agent` 加载 catalog 填 `InvocationRequest.skill_catalog`(standalone / 无 agent_id 时为空)。

**9.2 删死字段（11 个，零消费 + 零格式契约依赖）：** `app/skills/types.py` `SkillMetadata` 从 19 字段精简到 8 个(name/description/declared_tools/declared_packs/is_system/allowed_tools/pack/requires_skills)；删 license/compatibility/version/locale/invocation/cost_tier/estimated_runtime_minutes/output_artifacts/author/security_zone/raw_metadata(全部核实在 skill 数据流零属性读取)。`parser.py` 停止填充这 11 字段 + 删随之无用的 `_optional_int`。**parser 仍 `yaml.safe_load` 全部 frontmatter → 旧 skill 文件携带这些键不报错**(前向兼容，测试 `not hasattr` 钉死)。`SkillMetadata` 仅 parser+测试构造，distiller 写 markdown 文本不写对象 → 删字段不破 distiller。

**9.3 distiller 硬门核实 + 钉不变量：** 生产门已正确(见上"先核实")，**无生产码改动**(诚实定位)；新增端到端回归 `test_distiller_cannot_promote_without_external_behavior_eval`——高 confidence(0.95)安全 draft + **无外部 behavior report** → `status != promoted` + skill 文件不写 + ledger `decision=held`，把"LLM 自评单独不能晋升"钉死在 distiller 层(此前测试只钉 `behavior_eval_passed` 原语)。

**9.4 allowed_tools 接 scoped 治理引导（CC parity，L1/L2 忠实）：** skill 的 `allowed-tools` 此前是死字段(解析零消费)。`workspace.py` 新增 `_skill_scope_guidance(metadata)`，在 `_load_skill` **registry 路径**注入(该路径 `load_body` 剥离 frontmatter → allowed-tools 丢失；explicit-path 返回原始文件含 frontmatter 无需补)。引导是 **guidance 非硬过滤**("Prefer these tools... guidance, not a hard limit — every tool call remains governed")——守 L1(不机械限制模型) + L2(治理仍 enforce 真实权限)。顺手把内层 `except KeyError: pass` 重构为单次 `registry.resolve`(消除 silent-catch，未找到由外层 KeyError 兜底转 tool-pack 查找)。

**与设计稿偏差：** §4 Step 9 列"删死字段"含 `declared_packs`(§2③)——核实为活字段，**未删**；distiller 门"改 evolution_ledger 外部 eval"已是既成事实，Step 9 改为核实 + 钉不变量。skill_catalog frozen 参数按 owner 选项 A 保留(受保护测试 `test_prompt_sections.py::test_skill_catalog_included` 永不 commit，删参数会撞 main baseline 且无法提交修复 → 保留兼容参数，cache 修复在 invoker 路径不依赖删参数)。

**验证证据：**
```
$ pytest tests/runtime/test_prompt_builder.py::TestSkillCatalogInDynamicSuffix -q   # 5 passed
$ pytest tests/services/test_skill_distiller.py tests/services/test_skill_loading.py tests/skills/ -q  # 全绿
$ ruff check <8 核心文件 + 4 测试>                        # All checks passed!
$ pytest tests -q
4475 passed, 7 skipped, 4 warnings   # 0 failed (= Step8 的 4467 + 8 新测试:5 catalog-dynamic + 1 distiller 硬门 + 2 scope 引导)
```
注：受保护文件(`.ultra/*` + `prompt_sections` 4 + 2 测试)全程未触碰；`test_skill_catalog_included` baseline 因保留 frozen 参数继续通过。

---

### Step 10 — workflow 清理（✅ 2026-06-15，零行为变更 + 死列退役）

**先核实（§5.1 纪律，grep 实证三处判定）：**
1. **`preview_workflow`/`start_workflow` 已在 CORE** —— `services/agent_tools.py:189-190` 在 `CORE_TOOL_NAMES`。治理(`start_workflow` = sensitive + plan gate，`handlers/workflow.py`)不改变其 CORE 归属(§0.x 修正①：CORE/Governance 正交)。此项 = 核实，**无改动**。
2. **`office_workflow_examples` 不是孤儿**（纠正 §4 line 122 "接 seeder 或删 ⚠️核实"）—— `services/office_workflow_examples.py` 是测试黄金语料：被 `test_office_workflows`(compile/admission/capability-bound/artifacts/gate)、`test_workflow_promote_suggestions`(`CONTRACT_REVIEW_EXAMPLE`)、`test_extract_agent` 引用。无生产 seeder BY DESIGN。**保留 + 文档化**(docstring 补 Lifecycle 段)——不删(删=删契约测试)、不造 seeder(把 fixture 提升为生产 `visibility_scope=platform` 模板=产品决策非 cleanup=scope creep)。
3. **`WorkflowStep.phase` 确认死列** —— `grep '\.phase' / 'phase='` 全 `app/` 零匹配；`step_type` 才载步骤类型；`add_workflow_tables_0604.py:74` 建列后从无 writer，列恒 NULL；`test_workflow_migration.py`/`entrypoint.sh`/`db_bootstrap.py` 均无 phase 引用。

**10.1 phase 死列退役：**
- `app/models/workflow.py`：删 `WorkflowStep.phase` 映射(原 line 98)。
- `app/alembic/versions/drop_workflow_step_phase_0614.py`(新，`down_revision=add_installed_plugin_tables_0614`，单头)：`ALTER TABLE workflow_steps DROP COLUMN IF EXISTS phase`；downgrade `ADD COLUMN IF NOT EXISTS`。phase 恒 NULL → 无需 `retire_trigger_focus_ref_0613` 式数据归档。
- **create_all 残留(rollback note，呼应 critic 1 #1)**：Railway 走 create_all+stamp head 跳 migration → 旧生产库残留 nullable 死列(model 不再映射，ORM 不碰，无害)；新 create_all 库不建 phase(model 已删)；migration 路径 drop 之。`DROP/ADD ... IF [NOT] EXISTS` 三环境皆幂等，回滚 = `alembic downgrade -1`。
- 测试：`tests/migrations/test_workflow_migration.py` 加 `test_upgrade_path_drops_workflow_step_phase`(chain_migrated 真跑 migration) + `test_bootstrap_path_has_no_workflow_step_phase`(create_all 路径)；`_CURRENT_CLOSURE_HEAD` 同步更新为新 head(原 `add_installed_plugin_tables_0614` 的 head 常量债一并还清，否则 `test_alembic_single_head_is_current_closure_head` fail)。

**10.2 runtime_task 注释修正(5 处，零行为变更)：** `RuntimeTask.tenant_id` 列已存在(`runtime_task.py:61`，RLS enforcement 时加，nullable/backfilled from `parent_agent_id`)，但 5 处注释仍写"`runtime_tasks` has no tenant column"(事实错误 = 时序债：workflow P0-P13 早于 RLS 列)。改为准确描述：列存在但 nullable/backfilled，metadata mirror(run 创建时写)是权威 tenant 边界。改动点：`api/workflows.py:20+159`、`workflow_runtime_service.py:812+872`、`workflow_promote_suggestions.py:72`。**代码逻辑不动**——`list_runs_for_agent`/`resume_pending_runs`/`collect_promote_suggestions` 仍用 mirror 过滤是有意防御(列 nullable 不可靠，mirror 是 workflow 自身权威记录)。**债边界(诚实 surface)**：迁移过滤到用 `tenant_id` 列 + RLS 是 RLS enforcement 主线范围(涉 184 bare session + 影子验证 + 非 owner 角色)，非本 cleanup step——贸然改 = RLS fail-closed 风险(参 RLS flip 全员 401 前科)。

**10.3 "结构化数据 over 脚本" = 显式防御决策(文档化)：** workflow 编排单元是可序列化结构化数据(pydantic，§3.2)而非任意代码(对比 CC `WorkflowTool` 命令式 JS 脚本)。这**不是表达力妥协，是 L3 多租户安全的防御决策**：任意代码无法在多租户边界安全 admit(Hive 现无签名/沙箱基础设施)，结构化定义经 schema→compiler→admission 四阶段可静态校验 + capability 绑定 + 零代码面。`office_workflow_examples` docstring "being built-in grants no bypass"/"never on a private execution path" 即此防御的代码体现。表达力(sequence/fanout/condition AST/gate/wait_until)足够，明禁 eval/Jinja/任意解释器。

**与设计稿偏差：** §4 line 122 "`office_workflow_examples` 接 platform-template seeder 或删"——核实后**两者皆不做**(保留+文档化)；"修 `runtime_task` 注释"经 grep 发现是 **5 处**(非 1 处)且触及 tenant 列语义，按 cleanup 边界只修注释不改行为。

**验证证据：**
```
$ alembic heads                                              # drop_workflow_step_phase_0614 (单头)
$ python -c "...WorkflowStep.__table__.columns"              # phase in columns: False; step_type: True
$ ruff check <7 改动文件>                                     # All checks passed! / 3 files already formatted
$ pytest tests -q
4477 passed, 7 skipped, 4 warnings   # 0 failed (= Step 9 的 4475 + 2 新 phase-not-exists 双路径测试)
```
注：受保护文件全程未触碰；改动 7 代码文件(model 1 + migration 1 新 + 测试 1 + 注释 4) + doc 1，逐文件显式 stage。删 phase 后 4475→4477(+2 新测试，零现有测试失败 = 死列再证)。

---

### Step 11 — closure review 补齐（✅ 2026-06-15，plugin/runtime/product surface 全闭环）

**触发原因：** Step 0-10 完成后做全面 review，发现三类"已设计但未完全接线"的真实遗漏：① manifest 支持 `hooks`/`dependencies`，但 runtime install/execute 链没有把它们变成治理对象；② plugin install 是 tenant 级记录，但 runtime resolver/API/Agent UI 没有完整 per-agent assignment 闭环；③ lazy loading 已单源化，但缺 CC 的 `select:<tool>` 直选、turn-1 deferred list、compaction/replay 可见 state。

**11.1 Plugin hooks 治理化（不是自由执行面）：**
- `app/services/plugin_hook_service.py`：新增 DB → runtime loader，把 `PluginHookRegistration` 编译为 `HookRegistrationSpec`，并在 startup/install/uninstall/assignment 后刷新。
- allowlist handler 只允许平台拥有的 `plugin.audit` / `plugin.block` / `plugin.args_overlay`，manifest 不允许 Python import path、shell、webhook、任意 URL handler。
- `app/runtime/hooks.py`：`HookMatcherSpec` 增 `tenant_ids`/`agent_ids`，`HookRegistry.unregister_key_prefix` 支持按 plugin/tenant 清理重载；matcher 支持 `agent=`/`tenant=` 条件。
- `app/kernel/engine.py`：PRE/POST/FAILURE hook metadata 注入 `tenant_id`/source；PRE hook 若 block 或 args overlay，仍留在既有 tool schema/capability/preflight/approval 管线内，hook 只能收窄或阻断，不能绕过治理。
- fail-closed 规则：plugin hook 必须有 enabled `AgentPluginAssignment` 才注册到对应 agent；未分配 agent 不进入治理关键路径。

**11.2 Dependency/source 闭包补齐（远程源标记为后续安全门）：**
- `app/models/installed_plugin.py` + `alembic/versions/add_plugin_dependency_edges_0615.py`：新增 `PluginDependencyEdge` tenant graph，RLS ENABLE/FORCE + bootstrap 清单同步。
- `app/services/plugin_install_service.py`：`resolve_plugin_dependency_closure` 递归解析 builtin/local dependency；校验 exact version、cycle、source kind、local path traversal；生成 lockfile provenance（source kind/ref、manifest content SHA256、dependency closure）。
- uninstall protection：被其他 installed plugin 依赖时拒绝卸载，避免 dependency graph 被拆断。
- install approval closure：dependency 内的 `PRE_TOOL_USE enforce` hook 也用同一次 admin `approved_enforce_hooks` 校验；dependency assignment 继承本次 install 的 `agent_ids`，不会把依赖插件过宽暴露给所有 agent。
- **显式未开放项**：远程 `git/url/npm/pip` source 仍只被 validator/source policy 识别并 fail-closed。已有 `code_execution`/Vercel Sandbox 是运行时命令沙箱，可作为未来 materializer 后端；但 remote plugin install 还需要 fetch、signature/integrity verification、sandbox materialization、bundle/cache、lockfile provenance 这一条供应链流水线，未达标前不得声称可用。

**11.3 Runtime resolver + lazy loading 对齐 CC：**
- `app/services/pack_policy_service.py`：新增 `get_agent_pack_policies(db, tenant_id, agent_id)`，runtime 工具可见性由 enabled `AgentPluginAssignment` 决定；tenant install 只代表 tenant 可用，不自动暴露给所有 agent。
- `app/services/agent_tools.py`：`discoverable_tool_names_for_query` 支持 `select:<tool_name>` 直选；static deferred discovery 读取 agent-scoped plugin policy，避免未分配 agent 看到 plugin tool。
- `app/runtime/prompt_builder.py` + `app/kernel/engine.py`：turn-1 dynamic suffix 枚举 `Available Deferred Tools` 并给出 `select:` 调用方式；`available_deferred_tools` 写入 session metadata，和 `discovered_tools` 一起覆盖 compaction/replay 后的 loaded-tool state。

**11.4 Product/API surface 闭环：**
- `app/api/plugins.py`：install request 支持 `agent_ids`；新增 `/agents/{agent_id}/plugins/{plugin_key}` per-agent enable/disable。
- `app/services/mcp_server_service.py`：`get_agent_extensions` 返回 assigned `plugins`，与 skills/MCP servers 一起成为 Agent extension truth surface。
- `app/api/capabilities.py` + `app/main.py`：capability/runtime summary routes 迁出 legacy `packs.py`，`packs_router` 不再挂载；`tests/api/test_pack_api_surface.py` 钉死。
- Frontend `extensions.ts`/`WorkspaceToolsSection.tsx`/`ToolsManager.tsx`：Workspace 插件 install/backfill/uninstall；Agent Tools 展示 assigned plugins 并可切换 enabled；`en.json`/`zh.json` 双语补齐。
- 注释 truth surface：`app/api/mcp_servers.py` 改为当前 active extension surface，不再说 legacy pack routes 未来才移除。

**与远程 sandbox 讨论的最终边界：**
已有 `services/code_execution/*` 解决的是 agent command runtime isolation；Step 11 不把它误写成 remote plugin supply-chain materializer。远程 source 后续必须以独立安全门进入：registry/source fetcher、signature/integrity verifier、sandbox materializer、immutable bundle cache、lockfile provenance、tenant audit/backfill/rollback。当前实现保持 fail-closed，是显式安全边界，不是隐藏债务。

**验证证据：**
```
$ pytest tests/services/test_plugin_install_service.py -q
15 passed

$ pytest tests/services/test_mcp_server_service.py -q
20 passed

$ pytest tests/api/test_pack_api_surface.py tests/services/test_mcp_server_service.py -q
23 passed

$ pytest tests/runtime/test_hooks.py tests/services/test_plugin_install_service.py \
         tests/services/test_mcp_tool_discovery.py tests/runtime/test_prompt_builder.py \
         tests/api/test_pack_api_surface.py -q
109 passed

$ pytest tests -q
4489 passed, 7 skipped, 4 warnings

$ npm test -- --run src/api/domains/extensions.test.ts \
         src/pages/workspace/WorkspaceRemainingSections.test.tsx \
         src/pages/agent-detail/AgentDetailSections.test.tsx
3 files passed, 41 tests passed

$ npm run build
passed

$ alembic heads
add_plugin_dependency_edges_0615 (head)
```
