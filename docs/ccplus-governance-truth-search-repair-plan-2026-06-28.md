# CCPlus 治理与 Truth Search 整合修复方案

日期：2026-06-28

状态：自查修复闭合稿。2026-06-28 已把 Truth Search 从旧 `knowledge_inject.py` prompt helper 迁到 `TruthSearchService`，并接入当前 runtime context assembly；旧 helper 已退役删除。

## 0. 2026-06-28 自查实装证据

| 自查断点 | 实装收口 | 代码证据 | 验证证据 |
| --- | --- | --- | --- |
| Truth Search 仍有旧 `knowledge_inject.py` 旁路 | 删除旧 helper，`runtime/invoker.py` 的 `fetch_relevant_knowledge()` 只作为测试兼容 seam，内部调用 `TruthSearchService.search()` + `render_prompt_context()` | `backend/app/runtime/invoker.py`、`backend/app/services/truth_search_service.py`、已删除 `backend/app/services/knowledge_inject.py` | `pytest tests/services/test_truth_search_service.py tests/services/test_connector_acl.py tests/runtime/test_invoker.py -q` 已包含在扩大集合：`116 passed, 4 warnings` |
| 检索结果不是 source-bound evidence | `TruthEvidencePackV1` 增加 `snippets`，Truth Search 输出 citations/source refs/snippets，prompt context 只渲染证据，不授予权限 | `backend/app/runtime/ccplus_contracts.py`、`backend/app/services/truth_search_service.py` | `pytest tests/services/test_truth_search_service.py -q` 通过；全量后端 `pytest tests -q`：`5320 passed, 2 skipped, 4 warnings` |
| Connector ACL mirror 与 prompt assembly 可能分叉 | `TruthSearchService.search()` 统一调用 `filter_connector_results_for_prompt()`，不可见 source 不进入 prompt context | `backend/app/services/truth_search_service.py`、`backend/tests/services/test_connector_acl.py` | `pytest tests/services/test_connector_acl.py::test_truth_search_prompt_context_applies_connector_acl_mirror -q` 通过 |
| 文档仍保留旧路径描述而代码已删除旧路径 | 本文档和落地总方案同步改成“已退役旧 helper / 已统一 TruthSearchService” | 本文档、`docs/ccplus-tool-call-governance-closure-landing-plan-2026-06-28.md` | `rg -n "knowledge_inject|test_knowledge_inject" backend/app backend/tests frontend/src` 无匹配 |

## 结论

当前方向是对的，但需要把修复拆成两条互相绑定的主线：

1. **本身的 CCPlus runtime / governance 修复**：把 CC 该有的 runtime 机制对应齐，消除工具、Skill、MCP、Hook、Plan/L3、L2 扩展面的断点。
2. **Truth Search 与治理结合**：Truth Search 不能只是 prompt 前置检索，也不能绕过治理。它必须成为 source-bound、ACL-filtered、可审计的治理证据层，并服务于 L1/L3/preflight/回答引用。

核心原则：

- CC baseline 先对齐机制语义，不盲目复制每个实验性工具名。
- Coding-only 能力不要塞进核心 runtime，应打包成可开启的 **Coding 插件**。
- Governance 不能替代模型判断；平台负责权限、证据、引用、去重、回滚、审计和原子提交。
- Truth Search 输出是证据，不是 instruction，不是权限真相源，也不是公司知识 authority。
- Hive Knowledge Core 才是公司知识、Ontology、ACL、audit、rollback、export 的 authority；Graphiti / SAG / vector / OpenViking 只能是 provider 或 derived read model。

## 1. 缺口分类

### 1.1 放入 Coding 插件的能力

这些能力主要服务 coding 场景，后续应作为可开启插件进入 L2 extension/composition surface，而不是核心 runtime baseline：

| 能力 | 推荐归属 | 说明 |
| --- | --- | --- |
| LSP diagnostics / symbols | Coding 插件 | 需要 IDE/LSP server/file watcher substrate，不应污染默认企业 agent runtime。 |
| NotebookEdit / notebook embed mode | Coding 插件 | 偏 Jupyter / notebook workspace adapter；核心只保留普通文件和文档能力。 |
| REPL | Coding 插件 | 需要持久 interpreter/session state。 |
| PowerShell | Coding 插件 | Windows / shell-specific coding surface。 |
| Persistent terminal/process manager | Coding 插件 | `run_command` 已覆盖 bounded command；持久 terminal state 是 coding workbench 能力。 |
| Browser UI automation | 本地 Coding / QA 插件 | 依赖本机浏览器、cookie、窗口和用户环境；Hive 云端核心无法直接承载这类状态，应通过 Local Bridge / local runner 作为可开启插件接入。核心保留 web_search/web_fetch/crawl。 |
| Worktree | Coding 插件 | Worktree 是 coding workbench 能力，不进入云端核心 runtime；后续随 Coding 插件开启。 |

### 1.2 必须进入核心修复的断点

这些不是 coding-only，应该作为 CCPlus runtime / governance core 修复：

| 断点 | 为什么必须修 | 推荐处理 |
| --- | --- | --- |
| Skill `allowedTools` / frontmatter hooks / forked SkillTool 语义不完整 | Skill 是 CC session-middle parity 的核心机制 | 用 Hive subagent + governed tool profile 实现 SkillTool fork；frontmatter 只声明，不直接执行 raw code。 |
| Hook runner parity 不完整 | Hooks 是横切治理机制，不是插件小功能 | 建声明式 hook registration + platform allowlist；hook 修改 args 后重跑 schema/capability/L1/L3/preflight。 |
| L3 permission checkpoint/resume 不是完整 loop resume | 当前批准后更像工具级重放，不能完全恢复模型 loop | 持久化 permission checkpoint，allow/deny 后把结果回灌原 run 或明确 continuation run。 |
| L2 仍像工具开关 | 容易误导用户，以为 CORE 能力可关闭 | L2 改成 Extensions and Add-ons，只管理扩展可见性，不管理 CORE。 |
| 缺少治理能力分类单源 | `CORE_TOOL_NAMES`、`RUNTIME_TOOL_GROUPS`、`CAPABILITY_MAP`、pack metadata 各说各话 | 新增 `governance_capability_taxonomy.py`，成为 L2 UI / runtime / tests 的分类 authority。 |
| Web Search 混合 | `web_search` 不应语义上依赖 AnySearch | 基础 `web_search` 保持 core；AnySearch/Exa/Tavily/Firecrawl/XCrawl 进入 L2 add-on。 |
| Office 混合 | 文档生成/转换和 Office Online 协作编辑是不同层 | 基础文档能力保留 core；Office Online / browser workbench 作为 L2 add-on。 |
| MCP 命名和 transport 断点 | MCP 是核心 extension mechanism | 先提供稳定 Hive-side tool naming + alias；HTTP/SSE core，stdio/WS/SDK 按 connector/local bridge/coding plugin 分层。 |
| Truth Search 仍是 ad hoc injection | 已修复：旧 `knowledge_inject.py` 删除，runtime context assembly 统一走 `TruthSearchService`；剩余工作是把 evidence pack 进一步接入 preflight/DecisionTrace 深水区 | 保持 `TruthSearchService` 为唯一 prompt evidence adapter，后续扩展 provider fusion / trace / citation UI。 |

## 2. 本身修复主线

### Step A：建立治理能力分类单源

新增后端单源：

```text
backend/app/services/governance_capability_taxonomy.py
```

分类建议：

- `agent_base`：文件、基础命令/代码、基础 web_search/web_fetch、session/channel delivery、agent message/delegation、async helper、skill load/discovery、memory、work ledger、plan helpers、trigger helpers、subagent/workflow source capability。
- `platform_addon`：AnySearch、Exa、Tavily、Firecrawl、XCrawl、Feishu/Lark、Slack、Email、DingTalk、WeCom、Teams、Telegram、Discord、Plaza、Office Online、PaaS connector。
- `external_extension`：tenant-installed plugin、MCP server、第三方 skill/workflow/subagent bundle、行业能力包。
- `enterprise_policy_only`：destructive delete、share agent、cross-agent delegation、external channel send、company-boundary conflict。

验收：

- 每个 `CORE_TOOL_NAMES` 都有分类。
- L2 UI 候选不包含 `agent_base`。
- L2 disabled extension 不可发现、不可执行。

### Step B：保护 CORE 能力不被 L2 关闭

修复目标：

- `/enterprise/tools` 不再暗示 CORE tool 是企业可关闭能力。
- API 层拒绝对 `agent_base` 做 `enabled=false`。
- `_ALWAYS_INCLUDE_CORE` 保持，但不再作为“后台关了又偷偷补回来”的产品错位。

验收：

- 关闭 `web_fetch`、`web_search`、`send_message_to_agent`、`start_workflow` 返回明确错误。
- 关闭 `exa_search`、`plaza_create_post`、`send_feishu_message` 走 L2 add-on / assignment 流程。

### Step C：重塑 L2 产品面

产品面改成：

```text
Extensions and Add-ons
```

只展示：

- 高级搜索 / 第三方抓取。
- 企业 channel integrations。
- Plaza / 广场。
- Office Online / 在线协作编辑。
- PaaS connector。
- Plugin / MCP。
- 公司预装增值能力。
- Hook allowlist / registration / provenance。

不展示：

- 默认文件工具。
- 基础 web_fetch/web_search。
- Work Ledger。
- Plan helpers。
- Session messaging。
- Subagent/workflow source capabilities。

### Step D：补 L1 Capability Policies 管理面

后端已有 `capability_gate.py` 和 `/enterprise/capabilities` 基础，产品上要补完整企业硬规则管理面。

建议分组：

- Agent collaboration：`agent.message.send`、`agent.subagent.spawn`、`agent.workflow.run`
- Workspace mutation：`workspace.file.write`、`workspace.command.execute`、`workspace.command.destructive_delete`
- External communication：`channel.message.send`、`channel.file.send`、`channel.email.send`、`channel.feishu.message`
- Extension / MCP：`agent.mcp.call`、`agent.tool.install`、`external.api.call`
- Community / Plaza：`plaza.post.write`
- Knowledge / Truth：`knowledge.search`、`knowledge.propose`、`knowledge.publish`、`knowledge.inject`

必须修正一个语义：

```text
缺少 L1 policy 不是企业审批。
缺少 L1 policy 应下落到 L3 session permission。
```

### Step E：补 L3 Permission Checkpoint / Resume

当前批准后工具级重放还不够。目标是：

```text
permission required
  -> RuntimeTask waiting_for_user
  -> 持久化 permission_checkpoint
  -> allow/deny
  -> 工具结果或 deny result 回灌 model loop
  -> 同一 run resume 或明确 continuation run
```

checkpoint 至少记录：

- `permission_request_id`
- `tool_call_id`
- `tool_name`
- `arguments`
- `round_state`
- `runtime_task_id`
- `session_id`
- `origin_channel`
- `permission_profile`
- `knowledge_refs`（如果 preflight 使用了 Truth Search）

验收：

- Web allow once 后，模型继续下一步，而不是只显示工具结果。
- IM allow once 后，模型继续执行并回到原 channel。
- deny 后模型收到 denial result 并给替代方案。
- restart 后 pending request 能恢复或过期。

### Step F：补 Skill / Hook parity

Skill 修复方向：

- `load_skill` 继续只注入 context。
- `run_skill_tool` 继续走 governed execution。
- `allowedTools` frontmatter 映射成 tool profile / allowed capability view，不让 skill 自己扩大权限。
- Skill fork 用 `spawn_subagent` 承接，生成 isolated skill worker。
- Skill hooks 只允许声明式 registration，经 plugin trust gate、admin approval、allowlist 后进入 runtime。

Hook 修复方向：

- hook 可 observe / block / narrow / rewrite args。
- hook 不可扩大权限，不可绕过 L0/L1/L3。
- hook 修改 args 后必须重跑 schema validation、capability mapping、L1 policy、L3 permission、preflight。
- raw shell / raw Python / arbitrary webhook hook handler 默认禁止；只能走平台 allowlist 或受信 plugin handler。

## 3. Truth Search 与治理结合

### 3.1 Truth Search 的正确定位

Truth Search 不是：

- 不是 `web_search`。
- 不是普通 RAG prompt stuffing。
- 不是 company truth authority。
- 不是权限判断的最终来源。
- 不是可以绕过 L0/L1/L3 的工具。

Truth Search 应该是：

- 基于 Hive Knowledge Core 的 source-bound retrieval。
- 在 prompt 注入前完成 principal / ACL / sensitivity 过滤。
- 给模型提供证据，不给模型提供不可审计指令。
- 给 ActionPreflight / L1 policy explanation / decision trace 提供可引用证据。
- 把每条注入或用于决策的 knowledge result 写入 trace。

### 3.2 目标架构

```text
Source Acquisition / Connectors / Uploads / Agent Memory / Workflow Artifacts
  -> DocumentConversionService + MarkItDown
  -> Canonical Markdown Artifact
  -> Knowledge Core segmentation / ACL / source refs
  -> Graphiti / SAG / Vector / Full-text derived indexes
  -> TruthSearchService fusion
  -> ACL + sensitivity filter
  -> citation validation
  -> prompt injection / ActionPreflight evidence / decision trace
```

权威边界：

- Hive Knowledge Core：公司知识、Ontology、ACL、audit、rollback、export 的 authority。
- Graphiti：temporal facts/entities/relationships provider。
- SAG：Markdown chunks/events/entities/multi-hop evidence provider。
- OpenViking / vector / full-text：可替换检索 provider。
- `TruthSearchService`：融合、去重、排序、冲突暴露、ACL/citation validation。

### 3.3 与 L0-L3 的结合点

| 治理层 | Truth Search 结合方式 | 不允许做什么 |
| --- | --- | --- |
| L0 平台硬护栏 | 强制 source refs、ACL metadata、tenant boundary、citation hash；provider failure 按风险 fail closed 或降级 | 不允许 provider 自己决定 ACL / prompt injection。 |
| L1 公司硬规则 | capability policy 可以引用 Knowledge Core 中的公司政策、SOP、审批规则；preflight 解释可带 citation | 不允许检索结果覆盖显式 enterprise deny。 |
| L2 扩展组合面 | Graphiti/SAG/vector provider 可以作为 add-on/provider 安装；Knowledge Core 不可被当成可关闭插件 | 不允许关闭 provider 后破坏 Knowledge Core authority。 |
| L3 Session Permission | 当工具调用需要用户确认时，permission prompt 可以展示 source-bound policy evidence | 不允许把普通 L3 prompt 送到企业后台，也不允许 Truth Search 自动替用户同意。 |
| Hooks | hook 可请求 Truth Search 作为证据输入或阻断依据 | 不允许 hook 用未过滤结果扩权或注入不可审计 instruction。 |
| Preflight | `ActionPreflight` 可调用 `TruthSearchService` 查相关政策、收件人边界、敏感内容、历史冲突 | 不允许 preflight 因检索失败而静默放行高风险动作。 |

### 3.4 已替换当前 ad hoc knowledge injection

当前 runtime prompt assembly 不再调用 `backend/app/services/knowledge_inject.py`；该文件已删除。`backend/app/runtime/invoker.py` 保留同名 `fetch_relevant_knowledge()` 只是为了兼容既有测试 monkeypatch seam，真实实现已经调用 `TruthSearchService`。

当前落地接口为：

```text
TruthSearchService.search(
  query,
  tenant_id,
  current_user_id,
  agent_id,
  source_collector,
)
```

返回结构必须包含：

- `result_id`
- `source_id`
- `document_id`
- `segment_id`
- `source_ref`
- `source_sha256` / `artifact_hash` / `segment_hash`
- `provider`
- `provider_trace`
- `acl_decision`
- `sensitivity`
- `validity_window`
- `superseded_by`
- `confidence`
- `conflict_refs`
- `rendered_snippet`

每次使用必须写入：

- `InvocationSpan`
- `DecisionTrace`
- session transcript / T0 event
- 如果用于 prompt injection，写入 prompt manifest 的 knowledge refs

### 3.5 Truth Search 对工具治理的具体作用

1. **外部通信前置证据**
   - `send_email`、`send_feishu_message`、`send_channel_message` 等外发前，preflight 可以查公司外发政策、客户资料、敏感规则。
   - 结果只作为证据；L1 policy 仍是硬规则。

2. **MCP / plugin 调用证据**
   - 调用敏感 MCP tool 前，preflight 可查该 connector/provider 的 provenance、公司安装策略、使用 SOP。
   - 如果 extension disabled，直接 L2/call-time deny，不进入 L3 prompt。

3. **Skill / workflow 建议**
   - Skill load 或 workflow proposal 可用 Truth Search 检索相关 SOP 和已有 approved workflow。
   - 不能把检索到的 SOP 当成强制 system instruction；只能 evidence-framed 注入。

4. **Plan Mode**
   - Plan Mode 中可以用 Truth Search 支持计划依据，但计划必须 agent-authored。
   - 平台不能把 classifier/tool args/system skeleton 冒充成计划。

5. **Knowledge proposal**
   - Agent 不能直接 commit company truth。
   - Agent 只能 `propose_company_knowledge`，附 source refs、conflict refs、confidence、ontology mapping。
   - Platform Gate / admin review 决定 publish / retire / rollback。

## 4. 推荐实施顺序

### Phase 0：冻结边界

交付物：

- 更新工具机制映射表，把 coding plugin boundary 标成正式决定。
- 在治理修复计划里加入 Truth Search / Knowledge Core 结合章节。
- 明确 Knowledge Core 是 authority，provider 不是 authority。

### Phase 1：治理 taxonomy 和 L2 收口

交付物：

- `governance_capability_taxonomy.py`
- CORE 工具不可被 L2 disable。
- L2 UI/API 只展示 Extensions and Add-ons。
- 单元测试覆盖 CORE/L2 分类。

### Phase 2：Web / Office / MCP 边界拆分

交付物：

- `web_search` 基础化，AnySearch/provider search 进入 L2。
- Office CLI / document capability 与 Office Online 拆分。
- MCP imported tool naming / alias / transport support 策略落地。

### Phase 3：L1 + L3 完整闭环

交付物：

- Capability Policies 管理面。
- missing L1 policy 下落 L3。
- L3 permission checkpoint/resume。
- Web/IM 同语义 permission prompt。

### Phase 4：Truth Search 服务化（本轮已完成 prompt assembly 主路径）

交付物：

- `TruthSearchService`
- `KnowledgeProvider` interface
- 已替换并删除 `knowledge_inject.py` 的 ad hoc prompt injection
- source refs / ACL / citation / conflict / superseded trace
- restricted knowledge never enters prompt 测试

### Phase 5：Truth Search 进入 preflight / trace

交付物：

- `ActionPreflight` 支持 source-bound governance evidence。
- 外部通信、敏感 MCP、plugin action、company-boundary conflict 写入 knowledge refs。
- decision trace / invocation span 展示 evidence。

### Phase 6：Skill / Hook parity

交付物：

- Skill `allowedTools` 映射到 governed tool profile。
- Skill forked worker 走 subagent runtime。
- Declarative hook registration + allowlist + revalidation。

### Phase 7：Coding 插件

交付物：

- Coding plugin packaging boundary。
- LSP adapter。
- Notebook adapter。
- Persistent terminal/REPL adapter。
- Browser automation / QA adapter，只通过 Local Bridge / local runner 接入本地浏览器状态。
- Worktree adapter，作为 Coding 插件的一部分，不作为云端核心 runtime 能力。
- 所有 coding plugin tools 通过 L2 install/assignment 进入，再走 L0/L1/L3。

## 5. 测试矩阵

必须覆盖：

- CORE 基础能力不可被 L2 关闭。
- L2 disabled extension 不可发现、不可执行。
- `web_search` 不依赖 AnySearch。
- AnySearch disabled 不影响基础 `web_search` / `web_fetch`。
- Office Online disabled 不影响基础文档生成 / 读取 / 转换。
- L1 hard deny 优先于 L3 bypass。
- missing L1 policy 进入 L3 session prompt。
- Web permission allow/deny 后恢复模型 loop。
- IM permission allow/deny 后恢复原 channel continuation。
- Hook 修改 args 后重跑 schema / capability / L1 / L3 / preflight。
- Restricted knowledge 永不进入 prompt。
- Truth Search provider failure 不得导致高风险动作静默放行。
- Prompt manifest / InvocationSpan / DecisionTrace 记录 knowledge refs。
- Conflicting / superseded company knowledge 必须显式暴露，不得只注入旧事实。
- Agent 提出的 company knowledge 只能进入 proposal，不得直接 commit。

## 6. 当前判断

不需要推翻现有治理文档。它们的主线正确：

- L0/L1/L2/L3 分层是对的。
- L2 不应是 CORE tool switch 是对的。
- Web Search / Office 拆分是对的。
- L3 checkpoint/resume 是必须补的。

需要补强的是：

- 把 CC runtime 机制缺口和治理修复计划合并成同一张 gap ledger。
- 把 Truth Search 从 ad hoc prompt injection 升级成 Knowledge Core-backed governance evidence layer。
- 把 coding-only 能力明确移入 Coding 插件，不要让核心 runtime 背过多 IDE/terminal/notebook 复杂度。
- 把 Skill/Hook/MCP 这些非 coding-only 的机制断点作为核心修复项处理。
