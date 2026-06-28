# AnySearch Web Search Fallback 设计方案

Status: superseded by CCPlus L2 boundary on 2026-06-28
Date: 2026-06-20  
Scope: `web_search` provider design, tool definition wording, and agent prompt contract.

## 1. 结论

本文件保留为 2026-06-20 的历史方案记录；其中“AnySearch 作为基础 `web_search` 主 provider”的方向已在 2026-06-28 被 CCPlus L2 治理边界取代。

当前结论：

- 基础 `web_search` 是 `agent_base` CORE 能力，只代表平台基础搜索底座。
- AnySearch、Exa、Tavily、Firecrawl、XCrawl 属于 L2 provider-backed add-on / extension。
- AnySearch 不再作为基础 `web_search` 的默认优先路径；legacy `search_engine=anysearch` 会被归一到 core auto，不执行 AnySearch。
- AnySearch 通过 `anysearch_get_sub_domains`、`anysearch_search`、`anysearch_batch_search`、`anysearch_extract` 进入 `web_pack`。

当前目标链路是：

```text
web_search(auto)
  -> SearXNG JSON API when configured
  -> structured provider-unavailable result when no CORE provider is configured
```

Agent 面仍然先看到基础工具 `web_search`；当基础结果不足时，通过 `tool_search` 发现 L2 advanced search tools。AnySearch key 轮询、quota、错误重试属于 AnySearch L2 adapter 的内部行为，不进入 CORE `web_search` prompt。

补充决策：AnySearch 官方还暴露 `POST https://api.anysearch.com/mcp` JSON-RPC 2.0 endpoint。Hive 不把它做成用户可见的“外部 MCP server 配置”，也不要求用户在后台手动 import MCP。Hive 在后端内置一个 AnySearch MCP adapter，并把其能力封装成原生 read-only tools，作为 `web_pack` 的垂直数据搜索层：

```text
web_search(auto)
  -> basic search provider chain

web_pack / AnySearch MCP vertical layer
  -> anysearch_get_sub_domains
  -> anysearch_search
  -> anysearch_batch_search
  -> anysearch_extract
```

这样普通查询仍从 `web_search` 开始；金融、社交、科研、法律/监管、健康/生物、公司信息、安全/IP、代码文档等需要更精准数据源的查询，通过 `tool_search` 发现 AnySearch MCP vertical tools。

## 2. 改造前代码事实

改造前 repo 里 `web_search` 的基础 provider 定义分散在以下位置：

- `backend/app/services/agent_tool_domains/web_mcp.py`
  - `_fallback_search_result()` 当前调用 `_search_duckduckgo()`，并返回 `web_search:duckduckgo`。
  - `_web_search()` 的 `search_engine` 当前只接受 `auto`、`searxng`、`duckduckgo`。
  - `_search_searxng()` 使用 `GET {SEARXNG_URL}/search`，参数包含 `q`、`format=json`、`language`、`categories=general`，并解析 top-level `results`。
  - `_search_duckduckgo()` 当前抓取 `https://html.duckduckgo.com/html/`，再用 regex 解析 HTML。
- `backend/app/tools/handlers/search.py`
  - `web_search` 的 ToolMeta 说明当前写的是 SearXNG JSON API + DuckDuckGo HTML fallback。
  - config schema 当前的 `search_engine` enum 是 `auto`、`searxng`、`duckduckgo`。
- `backend/app/config.py`
  - 当前搜索相关配置只有 `TAVILY_API_KEY`、`EXA_API_KEY`、`SEARXNG_URL`、`FIRECRAWL_API_KEY`、`XCRAWL_API_KEY`。
  - `SEARXNG_URL` 是 env-backed setting，从 `.env` / `../.env` 读取；后续设计不应再把它作为主路径选择条件，只把它当 AnySearch 不可用时的 fallback provider 配置。
  - 还没有 AnySearch 相关配置。
- Prompt / skill surface
  - `backend/app/runtime/prompt_sections/tools.py` 要求 Agent 先使用 CORE `web_search` 做基础联网查询，再用 `tool_search` 找 Exa/Tavily 等高级工具。
  - `backend/app/runtime/prompt_sections/executing_actions.py` 目前只泛化描述 search/fetch/extract，不绑定具体 provider。
  - `backend/app/tools/runtime_tool_groups.py` 把高级 Web pack 定义成 Exa/Tavily/Firecrawl/XCrawl 等扩展能力。
  - `backend/app/templates/system_skills/web-research/SKILL.md` 当前明确写了 `web_search` 是 SearXNG when configured, DuckDuckGo fallback。

因此本次实现的核心是替换基础 `web_search` fallback，不是新建一个 Agent 直接调用的高级 search tool。

Implemented code paths:

- `backend/app/config.py`
- `backend/app/services/agent_tool_domains/web_mcp.py`
- `backend/app/tools/handlers/search.py`
- `backend/app/templates/system_skills/web-research/SKILL.md`
- `backend/app/services/agent_tools.py`

## 3. AnySearch 接口事实

AnySearch 官方 docs 当前给出的核心形态：

- API Base URL: `https://api.anysearch.com`
- Search endpoint: `POST /v1/search`
- Authentication:
  - `/v1/*` 支持 optional API key。
  - 无 API key 时使用 anonymous tier，按 IP 和免费额度限流。
  - 有 API key 时请求头为 `Authorization: Bearer <API_KEY>`。
  - 无效、禁用、过期 key 返回 `401` 或 `403`，不会静默降级成 anonymous。
- Request body:
  - `query`: required。
  - `max_results`: default 10, range 1-100。
  - `domain`: optional。
  - `tag`: optional。
  - `content_types`: optional, for example `["web"]`。
  - `zone`: optional, `cn` or `intl`。
  - `language`: optional。
  - `params`: optional pass-through object。
- Error shape:
  - `400`: invalid request。
  - `401`: invalid key / invalid auth header。
  - `402`: daily free quota exhausted / quota exhausted。
  - `403`: expired key / disabled account / capability not enabled。
  - `429`: rate limit exceeded。
  - `500` / `502` / `503` / `504`: transient provider failure。
- Pricing page currently lists a free plan with 1,000 requests/day and 20 QPS per key.

AnySearch official skill / agent spec additionally defines:

- MCP endpoint: `POST https://api.anysearch.com/mcp`
- Protocol: JSON-RPC 2.0
- Method: `tools/call`
- Auth: `Authorization: Bearer <API_KEY>` optional at provider level, but Hive production defaults to configured keys and only permits anonymous mode when `anysearch_allow_anonymous` is explicitly enabled for dev/eval.
- MCP tools:
  - `get_sub_domains`: discover vertical sub-domain directory and required params.
  - `search`: general search or vertical search. Vertical mode requires `domain + sub_domain`, and required `sub_domain_params` must be passed even when values are empty strings.
  - `batch_search`: execute 2-5 query objects for hybrid or multi-domain coverage.
  - `extract`: fetch full HTML page content as Markdown, capped by provider.
- Available domains currently include `general`, `resource`, `social_media`, `finance`, `academic`, `legal`, `health`, `business`, `security`, `ip`, `code`, `energy`, `environment`, `agriculture`, `travel`, `film`, and `gaming`.

Observed live anonymous response shape on 2026-06-20:

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "results": [
      {
        "title": "...",
        "url": "...",
        "snippet": "...",
        "content": "..."
      }
    ],
    "metadata": {}
  }
}
```

The implementation should accept both the documented top-level `results` shape and the observed nested `data.results` shape.

Docs:

- https://www.anysearch.com/docs#search-api
- https://anysearch.com/pricing

## 4. Provider 策略

### 4.1 `auto` 模式

`auto` 应该变成：

1. 基础 `web_search(auto)` 优先使用 CORE provider，不根据 AnySearch API key 切换主路径。
2. 如果 SearXNG 没有配置，基础 `web_search` 返回结构化 provider-unavailable。
3. AnySearch key pool、quota、rate limit、transient failure 只影响 L2 `anysearch_*` tools。
4. Direct DuckDuckGo HTML 只保留为 `duckduckgo_legacy` manual/debug compatibility path。

这意味着 `SEARXNG_URL` 配置状态决定 CORE `web_search` 是否有基础 provider；AnySearch 配置状态不改变 CORE `web_search` 的 provider selection。

### 4.2 SearXNG 仍需保留

保留 SearXNG 的原因：

- 它是开源聚合搜索引擎，搜索源可配置，包含 DuckDuckGo 等 engine。
- 它符合当前治理判断：不要用 L2 provider 替代或移除 CORE provider。
- 它能作为自建、可控、低成本的基础搜索源。
- 它是 CORE `web_search` 当前的基础 provider。

### 4.3 Direct DuckDuckGo 的处理

建议不再作为 `auto` fallback。

可选策略：

- Preferred: 保留为 `duckduckgo_legacy`，仅供手工调试或兼容旧配置，不在默认 enum 文案里推荐。
- Stricter: 完全移出 `web_search` provider chain。

当前采用 Preferred，降低迁移风险，同时把默认路径从 direct DuckDuckGo HTML 迁到 SearXNG core provider；AnySearch 不进入默认路径。

## 5. AnySearch 多 key 轮询设计

### 5.1 配置

新增配置建议：

```text
ANYSEARCH_API_KEYS=key1,key2,key3
ANYSEARCH_DEFAULT_ZONE=intl
ANYSEARCH_DEFAULT_CONTENT_TYPES=web
ANYSEARCH_TIMEOUT_SECONDS=12
```

如果后续支持公司后台配置，则后台配置优先级应为：

```text
tenant/user configured key pool
  -> platform global key pool
  -> structured L2 provider error
```

API key 必须存储在服务端的加密 secret/config surface，不能作为 Agent tool args，也不能进入 Agent prompt。

Anonymous AnySearch 不建议作为 production 默认路径。它可以作为 dev/eval 手工开关，但 product 默认判断应保持简单：AnySearch L2 tools 有 key 才调用 AnySearch；CORE `web_search` 不因 AnySearch key presence 改变 provider。

### 5.2 轮询

多 worker 环境不能用进程内 index。建议用 Redis 原子计数：

```text
INCR web_search:anysearch:key_index:{scope}
selected_key = keys[index % len(keys)]
```

`scope` 建议：

```text
tenant:{tenant_id}
user:{user_id}
global
```

日志里不能记录完整 key。只允许记录 key fingerprint，例如 SHA-256 前 8-12 位。

### 5.3 错误处理

错误处理应区分 permanent、quota、rate limit 和 transient：

| Status | Meaning | Action |
| --- | --- | --- |
| 200 | Success | Parse results |
| 400 | Bad request | Do not retry same payload; return structured error |
| 401 | Invalid key/auth | Mark key invalid, try next key |
| 402 | Quota exhausted | Mark key exhausted until reset/TTL, try next key |
| 403 | Expired/disabled/not enabled | Mark key unavailable, try next key |
| 429 | Rate limited | Respect `Retry-After` / reset header if present; otherwise cooldown, try next key |
| 5xx | Provider failure | Try next key; if no AnySearch key is currently callable, return structured L2 provider error and ask the agent to use CORE `web_search` separately if useful |

重要限制：如果多个 API key 属于同一个 AnySearch account，provider 可能按 account 维度限额，而不是按 key 维度限额。平台可以支持用户填多个 key 做 pool，但不能承诺“10 个 key 一定等于 1 万次/天”。这个判断必须写进后台 UI 和文档，避免误导用户或鼓励绕过服务条款。

## 6. Tool 定义修改

### 6.1 工具名

保留现有工具名：

```text
web_search
```

不要新增 `anysearch_search` 作为 Agent 默认工具。AnySearch 是基础 `web_search` 的 provider，不是 Agent 的新认知入口。

补充：这里的“不新增默认工具”只适用于 turn-1 core tool surface。AnySearch MCP vertical layer 需要新增原生 Hive tools，但它们必须挂在 `web_pack`，通过 `tool_search` 或 web-research Skill 的升级路径发现，不进入 core 默认工具面。

新增工具：

```text
anysearch_get_sub_domains
anysearch_search
anysearch_batch_search
anysearch_extract
```

这些工具共用 `web_search` 的 AnySearch key pool 和 tenant-aware tool config，不单独暴露 API key，也不暴露通用 `call_mcp_tool` 给 Agent 使用。Agent 看到的是 Hive 原生 tool schema，不需要理解 MCP server import。

### 6.2 ToolMeta description

2026-06-28 后的当前描述应保持为：

```text
Basic internet search for public information using Hive's built-in basic provider chain
(SearXNG when configured, with legacy HTML fallback only for manual/debug use).
```

### 6.3 Config schema

2026-06-28 后，基础 `web_search` 的 `search_engine` 只能保留 CORE provider options：

```json
{
  "enum": ["auto", "searxng", "duckduckgo_legacy"],
  "default": "auto"
}
```

AnySearch provider config 不再暴露在 CORE `web_search` schema 中。AnySearch API keys、zone、content types、domain/tag/params 属于 L2 AnySearch adapter / `web_pack` 的配置域。

### 6.4 Result format

`web_search` 返回给 Agent 的内容应保持轻量：

```text
Search results for "<query>" via the CORE provider:
1. Title
   URL
   Snippet
```

AnySearch L2 search result 也不应该把 provider 的长 `content` 字段完整塞进 search result。最多保留短摘要；需要全文时让 Agent 后续调用 `web_fetch` 或在 AnySearch workflow 中对已知 URL 调用 `anysearch_extract`。

## 7. Agent Prompt 修改

Prompt 修改原则：

- Agent 不需要知道 key 轮询。
- Agent 不需要选择 AnySearch key。
- Agent 不需要在普通搜索失败时要求用户提供 AnySearch key。
- Agent 不需要根据 provider 配置判断搜索路径；平台只让 CORE `web_search` 表达基础搜索，provider-backed 增强搜索通过 `tool_search` 发现。
- Agent 只需要知道：`web_search` 是基础联网搜索；如果基础搜索不够，再通过 `tool_search` 找 Exa/Tavily/Firecrawl/XCrawl 等高级工具。

### 7.1 必改位置

`backend/app/templates/system_skills/web-research/SKILL.md`

当前：

```text
web_search
- Basic public web search using built-in no-key providers: SearXNG when configured, DuckDuckGo fallback.
```

建议改成：

```text
web_search
- Basic public web search using Hive's built-in basic provider chain: SearXNG when configured, with legacy HTML fallback only for manual/debug use.
```

`backend/app/tools/handlers/search.py`

ToolMeta description 和 config schema 是最重要的 prompt surface，因为它直接进入工具说明。

### 7.2 可保持泛化的位置

`backend/app/runtime/prompt_sections/tools.py`

这段目前要求 Agent 先用 CORE `web_search`，再升级到 `tool_search`，方向是正确的。建议只做 provider-neutral 微调，不写 AnySearch 细节。

`backend/app/runtime/prompt_sections/executing_actions.py`

这里可以继续保持 “search/fetch/extract” 的抽象描述。不要把 provider 内部实现写进全局行动规则。

`backend/app/tools/runtime_tool_groups.py`

这里描述的是 advanced web pack，重点是 AnySearch、Exa、Tavily、Firecrawl、XCrawl 等 L2 provider-backed 能力。AnySearch 不属于基础 fallback。

## 8. 测试计划

后续实现必须先写测试，再写实现。建议新增或扩展以下测试：

```text
backend/tests/services/test_web_mcp_resilience.py
backend/tests/tools/test_search_provider_tool_definitions.py
backend/tests/services/test_prompt_contracts.py
```

Red phase 测试项：

1. `test_web_search_auto_uses_searxng_even_when_anysearch_key_is_configured`
2. `test_web_search_no_anysearch_key_uses_searxng_fallback`
3. `test_web_search_legacy_anysearch_config_uses_basic_provider_not_anysearch`
4. `test_anysearch_key_pool_round_robin_uses_next_key`
5. `test_anysearch_401_skips_bad_key_and_tries_next`
6. `test_anysearch_402_marks_key_exhausted_and_tries_next`
7. `test_anysearch_429_respects_retry_after`
8. `test_anysearch_all_keys_temporarily_unavailable_returns_l2_provider_error`
9. `test_anysearch_parses_nested_data_results_live_shape`
10. `test_web_search_tool_definition_keeps_anysearch_out_of_core_primary_path`
11. `test_web_research_skill_documents_anysearch_as_l2_vertical_workflow`
11. `test_anysearch_mcp_get_sub_domains_calls_official_json_rpc_endpoint`
12. `test_anysearch_mcp_search_forwards_vertical_domain_arguments`
13. `test_anysearch_mcp_batch_search_forwards_queries`
14. `test_anysearch_mcp_extract_forwards_url`
15. `test_anysearch_mcp_requires_configured_key_unless_anonymous_is_enabled`
16. `test_anysearch_vertical_tools_expose_mcp_search_surface`
17. `test_web_research_skill_documents_anysearch_vertical_workflow`
18. `test_policy_pack_names_include_anysearch_vertical_tools`

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_web_mcp_resilience.py tests/tools/test_search_provider_tool_definitions.py tests/services/test_prompt_contracts.py -q
ruff check app/services/agent_tool_domains/web_mcp.py app/tools/handlers/search.py tests/services/test_web_mcp_resilience.py tests/tools/test_search_provider_tool_definitions.py tests/services/test_prompt_contracts.py
```

## 9. 实施顺序

1. Keep AnySearch provider behavior tests on the L2 `anysearch_*` adapter.
2. Do not add AnySearch config fields to CORE `web_search` schema.
3. Keep `_search_anysearch()` as an internal helper for L2 AnySearch adapter tests, not as the CORE `web_search` provider.
4. Keep `auto` selection on CORE provider only.
5. Return structured provider-unavailable when CORE provider is not configured; use `tool_search` to discover L2 providers.
6. Keep direct DuckDuckGo only as `duckduckgo_legacy` manual/debug compatibility path.
7. Keep `web_search` ToolMeta and config schema provider-neutral and CORE-only.
8. Keep `web-research` system skill wording aligned with L2 escalation.
9. Keep AnySearch MCP adapter and `web_pack` vertical tools as L2 add-ons.
10. Update `web-research` system skill wording for vertical workflows.
11. Run targeted backend tests and lint.
12. After user confirmation, deploy backend to product and eval environments.

## 10. Open Decisions

1. Direct DuckDuckGo should be hidden as `duckduckgo_legacy`, or removed completely?
2. Tenant-provided AnySearch keys should override platform keys or append after platform keys?
3. Should anonymous AnySearch be disabled entirely in production, or retained as a dev/eval-only explicit switch?
4. Should company后台 expose AnySearch as a generic secret pool first, or wait for a broader data-source credential UI?

Recommended defaults:

```text
duckduckgo_legacy: keep but remove from auto
tenant keys: override platform keys
anonymous AnySearch: dev/eval explicit switch only, not product default
credential UI: start with AnySearch-specific config, then later generalize to data-source secret pool
```
