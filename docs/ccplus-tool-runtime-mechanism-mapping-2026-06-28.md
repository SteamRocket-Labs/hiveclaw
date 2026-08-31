# CCPlus 工具运行时机制映射表

日期：2026-06-28

状态：当前 checkout 源码核对后的映射草案。本文按 CC / FreeCode 风格的 runtime 入口机制，映射 Hive 当前实现状态。

关键口径：

- `Feature-gated built-ins` 不再作为独立机制。它只是 CC 自己一些实验性 / 模式门控的一等内置能力；Hive 需要时应把它们归入下面真实机制。
- `Worktree` 不纳入云端核心 runtime；后续归入可开启的 Coding 插件。
- 技术名词、工具名、函数名保留英文；解释和结论使用中文。

## 核对依据

当前源码依据：

- `backend/app/tools/collector.py`：收集 `@tool` handler，生成 OpenAI-compatible schema、执行 registry、安全 / 只读 / 并行 metadata、结果限制、alias 和 runtime tool group。
- `backend/app/tools/registry.py`：定义 canonical tool groups，并从 live `ToolMeta` 导出 `ToolSpecV1`。
- `backend/app/services/agent_tools.py`：按 agent 组装 active tools、core tools、provider-gated tools、deferred discovery、MCP-imported tools、session-expanded schemas。
- `backend/app/kernel/engine.py`：执行 model / tool loop；在 `tool_search` / MCP discovery 后扩展工具；做 tool result budget eviction；把 tool result 重新写回上下文。
- `backend/app/tools/service.py`：执行 Plan Mode blocking、plan gate、capability governance、ActionPreflight、timeout、trace / activity log、最终 handler 调用。
- `backend/app/services/mcp_client.py`：当前 MCP 执行层支持 Streamable HTTP 和 SSE 自动探测；没有证明 stdio / WS / SDK transport 完整对齐。

工具清单核对命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python - <<'PY'
from collections import defaultdict
from app.tools.collector import collect_tools

tools = collect_tools()
print("canonical_total", len(tools.schemas))
by_category = defaultdict(list)
for name, meta in sorted(tools.metadata.items()):
    if name in tools.schemas:
        by_category[meta.category].append(name)
for category, names in sorted(by_category.items()):
    print(category, len(names), ", ".join(names))
PY
```

核对结果：当前有 130 个 canonical `@tool` 工具，覆盖 filesystem、search、communication、skills、MCP、plan、workflow、subagent、work ledger、triggers、office、Feishu、email、Plaza、HR、command-parity 等类别。

## 状态标记

| 状态 | 含义 |
| --- | --- |
| 对应 | Hive 当前 callable tool / runtime surface 已有同类语义能力。 |
| 增强对应 | Hive 有 CC 同类语义能力，并额外提供治理、持久化、多租户、企业通道或审计能力。 |
| 部分对应 | Hive 覆盖部分能力，但不等价于完整 CC 机制，或交互模型不同。 |
| 缺口 | 当前核对到的 canonical tool surface 中没有对应能力。 |
| 不需要 | 当前明确不纳入 Hive parity 范围。 |

## 机制总数

移除 `Feature-gated built-ins` 独立分类后，runtime 入口机制应拆成 8 类：

| 序号 | Runtime 机制 | 本质 |
| --- | --- | --- |
| 1 | Built-in tools | runtime 自己注册的一等内置工具。 |
| 2 | MCP tools | 外部 MCP server 暴露的工具，被转换成模型可调用工具。 |
| 3 | MCP resources | MCP resource 不是普通 tool schema，通过 list/read resource 入口访问。 |
| 4 | ToolSearch / deferred tools | 工具 schema 延迟加载机制，不是业务能力本身。 |
| 5 | Agent / Subagent | 一个工具调用启动或继续另一个 query loop / worker / agent session。 |
| 6 | Skill tool / Skill command | Skill 注入上下文，也可能暴露受治理的可执行资产。 |
| 7 | User interaction / planning runtime | 计划、提问、任务板、用户确认、plan handoff。 |
| 8 | 横切治理机制 | 校验、权限、hooks、classifier、interrupt、sandbox、deny rules、审计。 |

## 1. Built-in tools

定义：runtime 自己拥有的一等工具对象。Hive 中对应 `@tool` handler，经 collector 进入 canonical schema，再由 `ToolRuntimeService` 执行。

| CC 能力 | Hive 当前对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| `Read` | `read_file`, `fs_read`, `read_document` | 增强对应 | workspace-scoped；文档读取有 `read_document` / document conversion。 |
| `Write` | `write_file`, `fs_write` | 增强对应 | 对 memory / logs / evolution / runtime / soul / task / skill 控制路径有保护。 |
| `Edit` | `edit_file` | 对应 | 有直接文件编辑工具。 |
| `Bash` | `run_command` | 部分对应 | 通过 code execution provider 和安全检查执行；不是 handler 直接 raw subprocess。 |
| 代码执行 | `execute_code` | 增强对应 | 支持 Python / Bash / Node；本地可走 OS sandbox，生产可走 Vercel Sandbox。 |
| `Grep` | `grep_search` | 对应 | workspace grep 能力存在。 |
| `Glob` | `glob_search` | 对应 | workspace glob 能力存在。 |
| `WebFetch` | `web_fetch` | 对应 | 已知 URL 抓取能力存在。 |
| `WebSearch` | `web_search`, `exa_search`, `tavily_search`, `anysearch_*`, `firecrawl_fetch`, `xcrawl_scrape` | 增强对应 | 基础 search/fetch 和高级 search/crawl/extract 都有；高级工具按 provider / deferred 进入。 |
| `TodoWrite` | `track_todo`, `record_finding`, `read_ledger` | 增强对应 | Hive Work Ledger 是明确的认知记账；写 todo 不启动执行。 |
| `Task v2` 类任务命令 | `task_create`, `task_update`, `task_list`, `task_get`, `task_output`, `task_stop` | 对应 | command-parity 工具已存在；底层按 Work Ledger / RuntimeTask 语义承接。 |
| `NotebookEdit` | 当前 canonical inventory 未发现 | 缺口 | 普通文件编辑有，但没有结构化 notebook cell edit。 |
| Workflow | `propose_dynamic_workflow`, `preview_workflow`, `start_workflow` | 增强对应 | 不再视为 feature-gated 独立机制；Hive 中是一等内置 orchestration surface。 |
| Cron / Monitor / RemoteTrigger | `set_trigger`, `update_trigger`, `list_triggers`, `cancel_trigger` | 增强对应 | 支持 cron / once / interval / poll / on_message / webhook，可绑定 workflow。 |
| WebBrowser | 当前云端 canonical inventory 未发现 | 缺口 / Coding 插件 | Hive 云端核心保留 web search/fetch/crawl/extract。浏览器 UI 自动化依赖本地浏览器状态，应通过 Local Bridge / local runner 形态进入 Coding 或 QA 插件，不进入云端核心 runtime。 |
| REPL / PowerShell | 当前 canonical inventory 未发现 | 缺口 | 若需要，应作为 optional coding pack 设计。 |
| LSP | 当前 canonical inventory 未发现 | 缺口 | LSP diagnostics / symbols 不是当前核心工具。 |
| Worktree | 当前 canonical inventory 未发现 | Coding 插件 | Worktree 是 coding workbench 能力，不进入云端核心 runtime；后续随 Coding 插件开启。 |

结论：Built-in tools 基本覆盖。NotebookEdit、LSP、WebBrowser、REPL / PowerShell、持久 terminal/process state、Worktree 都不应作为云端核心断点处理，应进入可开启的 Coding 插件；其中 Browser UI 自动化必须走本地能力接入。

## 2. MCP tools

定义：MCP server 暴露的外部工具，通过 import / discovery / execution 路径变成模型可调用工具。

| CC 能力 | Hive 当前对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| 导入 / 连接 MCP server | `import_mcp_server` 加 enterprise / agent MCP server API | 对应 | Hive 有 tenant-scoped `MCPServer`, `MCPServerTool`, agent assignment records。 |
| 列出 MCP tools | `list_mcp_tools` | 对应 | 能列出 agent 当前可达 / 已导入的 MCP 工具名。 |
| 查看 MCP schema | `inspect_mcp_tool` | 对应 | 展示 Hive-side name、MCP tool name、server、policy mode、schema。 |
| 执行 MCP tool | `call_mcp_tool` | 对应 | policy check 后转发到远端 MCP server。 |
| MCP tools 作为 deferred schema 暴露 | `tool_search` 加 DB `Tool(type="mcp")` rows | 增强对应 | 已导入 MCP tools 可通过 discovery 激活进当前 session。 |
| `mcp__server__tool` 命名空间 | Hive-side imported tool naming 加 MCP metadata | 部分对应 | 不应默认假设 Hive 完全复制 CC 的命名拼写；实际名称要以 `list_mcp_tools` / `tool_search` 为准。 |
| stdio / SSE / HTTP / WS / SDK transport 广度 | `MCPClient` 当前支持 Streamable HTTP 和 SSE | 部分对应 | model 里有 `transport` 字段，但当前执行 client 只证明 HTTP/SSE；stdio/WS/SDK 未证明完成。 |
| MCP auth / OAuth pseudo-tool | `mcp_auth_status`；MCP authz 拒绝 token passthrough / URL userinfo | 部分对应 | Hive 故意暴露 server-side/tokenless auth status，不让模型携带 OAuth token。 |

结论：MCP tools 语义上已实现，而且治理更强；但 transport 广度和精确 `mcp__server__tool` namespace parity 是部分对应。

## 3. MCP resources

定义：MCP resources 不是普通 tool schema，而是通过 list/read resource 获取的上下文 / 数据原语。

| CC 能力 | Hive 当前对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| `ListMcpResources` | `mcp_list_resources` | 对应 | 当前工具存在，并调用 MCP `resources/list`。 |
| `ReadMcpResource` | `mcp_read_resource` | 对应 | 当前工具存在，并调用 MCP `resources/read`；binary payload 会安全渲染 / spill。 |
| MCP prompts 邻近能力 | `mcp_list_prompts`, `mcp_get_prompt` | 增强对应 | 不完全等于 resource，但 Hive 当前把 MCP prompt list/get 做成一等协议工具。 |

结论：MCP resources 已作为独立 resource access layer 实现，没有混成普通 MCP tool call。

## 4. ToolSearch / deferred tools

定义：deferred tools 是 schema 加载机制。模型先知道某类工具存在，需要时再拉完整 schema。

| CC 能力 | Hive 当前对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| 工具 schema lazy loading | `tool_search` | 对应 | 搜索 deferred tool groups、imported MCP tools、skill/tool surfaces。 |
| Deferred runtime groups | `runtime_tool_groups.py` | 增强对应 | web pack、Feishu pack、MCP admin pack、Office pack、command pack 等。 |
| provider / env gated availability | `get_agent_tools_for_llm()` provider filters | 增强对应 | 没有 provider/API key 的工具会隐藏，不让模型看到不可用 schema。 |
| session 内激活 discovered tools | `SessionContext.discovered_tools`；kernel 在 `tool_search` 后扩展工具 | 对应 | discovery 后 schema 可在当前 model loop 里调用。 |
| deferred tool 是否业务能力 | 不适用 | 不需要 | `tool_search` 是基础设施，不是业务能力本身。 |

结论：Hive 已有 deferred-schema 机制，并用于一方工具 pack 和 imported MCP tools。

## 5. Agent / Subagent

定义：一个工具调用启动或继续另一个 query loop，例如 subagent、fork worker、background agent、team agent、peer employee agent。

| CC 能力 | Hive 当前对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| general-purpose subagent | `spawn_subagent` | 增强对应 | 内置 `general-purpose`, `explorer`, `critic`，支持 named definitions 和 isolation modes。 |
| fork / isolated agent loop | `spawn_subagent` 的 isolation options 和 child session | 对应 | session-local worker 走同一套 governed runtime。 |
| async / background subagent | `spawn_subagent(run_in_background=true)`, `check_subagent` | 增强对应 | 返回 run/session handle，后续可检查。 |
| 继续 child session | `send_agent_session_message` | 增强对应 | 可继续 child/team session。 |
| team agent | `team_create`, team member spawn path | 增强对应 | Team runtime 是 Hive 一等能力。 |
| peer employee delegation | `delegate_to_agent`, `send_message_to_agent`, async task helpers | 增强对应 | 这是 Hive-native A2A / employee delegation，不只是 CC local AgentTool。 |
| public A2A JSON-RPC task surface | 当前未暴露 | 部分对应 | Hive 有 durable internal A2A messaging；public JSON-RPC task interoperability 标记为 not exposed。 |

结论：Agent / Subagent 是 Hive 最强的对应层之一。Hive 不只是对齐本地 CLI，还增加了 durable session、team、治理和企业代理协作。

## 6. Skill tool / Skill command

定义：Skill 可以注入上下文 / 指令，也可能暴露可执行资产或命令。CC 里通常涉及 `SKILL.md`、allowedTools、skill hooks、forked execution。

| CC 能力 | Hive 当前对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| `SKILL.md` progressive disclosure | `load_skill` | 对应 | 加载 skill 只注入 guidance/context，不直接执行。 |
| skill references / templates / scripts / workflows / subagents | Skill capsule model 和 `load_skill` 描述 | 对应 | Hive 的 skill 概念不只是 prompt 文件。 |
| skill command / script execution | `run_skill_tool` | 部分对应 | 运行 `skill/scripts` 下脚本，并走 governed code execution。 |
| skill allowed tools | runtime policy / tool availability 加 skill guidance | 部分对应 | Hive 有工具治理，但未证明完整 CC `allowedTools` frontmatter parity。 |
| skill hooks | hook runtime 单独存在；skill frontmatter hook registration 未证明完整 | 部分对应 | 如果要对齐 CC-style skill hooks，需要单独设计 / 验证。 |
| fork skill 到 isolated agent 执行 | 当前未证明完整对应 | 缺口 | Hive 有 subagent，但未证明完整 CC SkillTool forked execution 语义。 |
| skill 创建 / 进化 | `save_skill`, `pin_skill`, SkillGuard / eval / promotion path | 增强对应 | Hive 故意把新 skill 当 candidate，而不是直接 durable activate。 |

结论：Skill progressive disclosure 已对齐；可执行 / forked skill 语义是部分对应，需要单独补齐或明确不做。

## 7. User interaction / planning runtime

定义：让模型进入计划、向用户提问、维护任务板、请求确认，再进入执行。

| CC 能力 | Hive 当前对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| `EnterPlanMode` | `request_plan_mode`, runtime `plan_mode` state | 对应 | Plan Mode 是一等 runtime boundary。 |
| `ExitPlanMode` | `exit_plan_mode` | 增强对应 | 提交 agent-authored plan 给用户确认；本身不执行计划。 |
| `AskUserQuestion` | `ask_user_question` | 对应 | 一等 clarification / interaction 工具。 |
| `TodoWrite` | `track_todo`, `record_finding`, `read_ledger` | 增强对应 | Work Ledger 有 todos、findings、dependencies、restore。 |
| `Task v2` | `task_create`, `task_update`, `task_list`, `task_get`, `task_output`, `task_stop` | 对应 | command-parity 工具存在。 |
| 用户确认 gate | plan gates、checkpoint / preflight paths | 增强对应 | Hive 区分 planning、confirmation、execution。 |
| 后台 run continuation | `RuntimeTask`, durable web chat, workflow/subagent/A2A background runs | 增强对应 | 浏览器断开不会取消 active web-chat/background work。 |

结论：计划和交互 runtime 已实现，而且因为持久化和治理边界，强于普通本地 CLI plan mode。

## 8. 横切治理机制

定义：不是能力来源，但决定工具能不能调用、如何校验、是否需要确认、如何 sandbox、如何审计。

| CC 治理点 | Hive 当前对应 | 状态 | 说明 |
| --- | --- | --- | --- |
| schema / 参数校验 | `ToolMeta.parameters`, collector schemas, handler-level checks | 对应 | Python schema dict 不是 Zod，但承担同类角色。 |
| `validateInput` 类校验 | handler validation 加 service/domain validators | 对应 | 校验分布在 handler 和 service 层。 |
| permission / capability gate | `ToolRuntimeService`, `CapabilityGate`, AgentTool/MCP assignment policies | 增强对应 | tenant / agent / tool 治理是中心化路径。 |
| plan / confirmation classifier | plan gates 和 ActionPreflight | 增强对应 | 外部可见 / 敏感 / 不可逆动作可 checkpoint 后再执行。 |
| interrupt / cancel | RuntimeTask cancellation、async task cancel、workflow/subagent controls | 部分对应 | 多种 run type 已有 cancel；精确 CC interrupt 语义还需单独 source-level comparison。 |
| sandbox | code execution providers、local OS sandbox、Vercel Sandbox | 增强对应 | tool handler 不在生产里直接 raw subprocess。 |
| deny rules | path protection、MCP authz、provider availability filters、capability policies | 增强对应 | 执行前有多层 deny surface。 |
| hooks | kernel / tool runtime hook path 已存在 | 部分对应 | external command / prompt / http / agent hook runner parity 需要专门设计 / 验证。 |
| trace / audit | InvocationSpan、tool activity logs、runtime/task/session metadata | 增强对应 | Hive 有企业级审计和 read-model 要求。 |

结论：治理层不是缺失项，反而是 Hive 的刻意增强层。剩余风险主要是精确 hook-runner parity，不是通用 permission / sandbox / audit 缺失。

## 原 `Feature-gated built-ins` 示例重新归类

因为 `Feature-gated built-ins` 不再作为核心机制，原截图里的例子应这样归类：

| 原示例 | 当前正确归类 | Hive 状态 |
| --- | --- | --- |
| `Workflow` | Built-in tools + planning/background runtime | 增强对应 |
| `Cron` | Built-in tools / triggers | 增强对应 |
| `Monitor` | poll trigger / workflow / daemon patterns | 部分对应到增强对应 |
| `RemoteTrigger` | webhook trigger / channel runtime | 部分对应到增强对应 |
| `LSP` | 如果产品化，就是 built-in tool；当前是缺口 | 缺口 |
| `REPL` | 如果产品化，就是 built-in tool；当前是缺口 | 缺口 |
| `WebBrowser` | 本地 Coding / QA 插件；云端核心不承载浏览器 UI 状态 | 缺口 / Coding 插件 |
| `PowerShell` | 如果产品化，就是 built-in tool；当前是缺口 | 缺口 |
| `Worktree` | Coding 插件 | 缺口 / Coding 插件 |

## 总结

当前 runtime 入口地图是 8 类：

1. Built-in tools：大体实现；NotebookEdit、LSP、WebBrowser、REPL / PowerShell、持久 terminal/process state、Worktree 归入可开启 Coding 插件，不作为云端核心 runtime 缺口。
2. MCP tools：已实现；transport 广度和精确 `mcp__server__tool` namespace parity 是部分对应。
3. MCP resources：已实现，并额外有 MCP prompts。
4. ToolSearch / deferred tools：已实现。
5. Agent / Subagent：已实现且增强。
6. Skill tool / Skill command：progressive disclosure 已实现；forked SkillTool、allowedTools、frontmatter hooks 是部分对应。
7. User interaction / planning runtime：已实现且增强。
8. 横切治理机制：已实现且增强；精确 external hook-runner parity 需要单独处理。

下一步不是重写总架构，而是给每个部分对应 / 缺口做决策分类：`core parity`、`optional coding pack`、`plugin/connector`、`later`、`not needed`。
