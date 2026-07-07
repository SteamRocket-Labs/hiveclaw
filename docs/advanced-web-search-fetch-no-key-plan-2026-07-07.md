# Advanced Web Search / Fetch No-Key 方案重基线

| 字段 | 内容 |
|------|------|
| 状态 | Discussion plan / docs-only |
| 日期 | 2026-07-07 |
| 范围 | `web_search` / `web_fetch` 基础工具与高级 Web Search / Web Fetch provider 的 no-key 默认接入、工具分层、skill 路由、治理边界 |
| 非范围 | 登录态浏览器自动化、绕过站点授权的批量抓取、把第三方 MCP 直接暴露为未治理 runtime、生产数据迁移 |
| 相关旧文档 | `docs/web-data-source-layer-plan-2026-06-20.md`, `docs/anysearch-web-search-fallback-plan-2026-06-20.md` |

## 0. 结论

本轮重新确认后，Hive 的 Web 能力不应继续用“所有高级工具堆在一起”的方式描述。正确形态是：

```text
CORE layer
  -> web_search  基础公开搜索，当前主 provider 是 SearXNG
  -> web_fetch   已知 URL 轻量读取和 DocumentConversionService 转换

Advanced no-key layer
  -> advanced_web_search  高级搜索路由器
  -> advanced_web_fetch   高级抓取/抽取路由器

Provider adapters
  -> AnySearch / Exa / Tavily / Firecrawl / Jina 等 no-key-capable provider
  -> XCrawl / keyed crawl-map-research 等 optional keyed add-on
```

关键修正：

1. **SearXNG 不属于高级 no-key layer。** SearXNG 已经是 `web_search` 的 CORE provider。再把它放进高级 no-key layer 是重复。
2. **AnySearch 应改成默认 no-key。** 官方 MCP README 和官方 skill 都声明 API key optional；无 key 时 anonymous access 可用，只是限额更低。当前 Hive 代码把 AnySearch MCP anonymous 设成 opt-in，这是和官方 skill 不一致的旧实现。
3. **XCrawl 按 keyed add-on 保留。** 有 key 才展示/可用；无 key 不进入默认 no-key 链。
4. **Exa、Tavily、Firecrawl 均应默认 no-key，但边界不同。**
   - Exa MCP 默认支持 `web_search_exa` / `web_fetch_exa` no-key；Exa Agent tools 需要认证。
   - Tavily keyless 只覆盖 Search / Extract；Crawl / Map / Research 需要 key。
   - Firecrawl keyless 只覆盖 Search / Scrape / Interact；Crawl / Map / Agent / Extract 需要 key。
5. **ScrapeGraphAI 不进入默认 no-key hosted provider。** ScrapeGraphAI 托管 v2 API 要 `SGAI-APIKEY`；开源库可本地/自托管运行，但应作为本地 structured extraction adapter 另案评估。

推荐产品形态：保留 `web_search` / `web_fetch` 两个基础工具，同时新增两个高级入口 `advanced_web_search` / `advanced_web_fetch` 做 provider 路由。Provider-specific tools 可以保留作诊断、override、兼容和测试 surface，但不应成为默认心智模型。

## 1. 当前代码事实

### 1.1 `web_search` 是 CORE，不是高级 provider 混合器

当前实现位于：

- `backend/app/services/agent_tool_domains/web_mcp.py::_web_search`
- `backend/app/tools/handlers/search.py::web_search`

当前行为：

- `search_engine=auto` 时，如果有 `SEARXNG_URL`，使用 SearXNG。
- legacy `search_engine=anysearch` 会被归一为 `auto`，不会调用 AnySearch。
- 没有 CORE provider 时返回结构化 `provider_unavailable`。
- `duckduckgo_legacy` 只作为兼容/调试路径，不应作为推荐默认能力。

因此，SearXNG 的正确位置是：

```text
CORE web_search provider chain
  -> SearXNG when configured
  -> provider_unavailable if no CORE provider
  -> duckduckgo_legacy only when explicitly selected
```

不应写成：

```text
Advanced no-key layer
  -> SearXNG
```

### 1.2 AnySearch 当前 Hive 行为与官方行为不一致

当前实现位于：

- `backend/app/services/agent_tool_domains/web_mcp.py::_call_anysearch_mcp_tool`
- `backend/app/services/agent_tool_domains/web_mcp.py::_anysearch_search`
- `backend/app/services/agent_tool_domains/web_mcp.py::_anysearch_batch_search`
- `backend/app/services/agent_tool_domains/web_mcp.py::_anysearch_extract`
- `backend/app/services/agent_tool_domains/web_mcp.py::_anysearch_get_sub_domains`

当前 Hive MCP path 行为：

```text
if no AnySearch keys and anysearch_allow_anonymous is false:
  return provider_not_configured
```

这意味着当前 AnySearch MCP 工具默认不是 no-key。它只有在 `anysearch_allow_anonymous=true` 时才允许 anonymous。

但官方 AnySearch MCP README 和 AnySearch skill 的行为是：

```text
API key optional
no key -> anonymous access with lower rate limits
has key -> Authorization: Bearer <key>
```

因此要改的不是产品判断，而是 Hive adapter 的默认行为。

### 1.3 XCrawl 当前是 keyed add-on

当前实现位于：

- `backend/app/services/agent_tool_domains/web_mcp.py::_xcrawl_scrape`
- `backend/app/services/agent_tools.py::_provider_available_tools`

当前行为：

- 无 `XCRAWL_API_KEY` 时，`xcrawl_scrape` 返回 `provider_not_configured`。
- provider availability 只有在 `xcrawl_key` 存在时才加入 `xcrawl_scrape`。

这与本轮最终决策一致：XCrawl 保留，但只在配置 key 后展示/使用。

## 2. 官方定义重核

### 2.1 SearXNG

官方定义：

- free internet metasearch engine
- aggregates results from many search services
- self-hostable
- no user tracking / no profiling

官方来源：

- https://docs.searxng.org/

Hive 定位：

| 项 | 判断 |
|----|------|
| no-key | 对 Hive 来说是 no provider API key，但需要一个配置好的 SearXNG instance URL |
| 工具层 | CORE `web_search` |
| 不应做 | 不应重复进入 Advanced no-key layer |

### 2.2 AnySearch

官方定义：

- unified real-time search MCP server
- general web search
- vertical domain search
- parallel batch search
- URL content extraction
- anonymous access without API key, lower rate limits

官方来源：

- https://github.com/anysearch-ai/anysearch-mcp-server
- https://github.com/anysearch-ai/anysearch-skill
- https://www.anysearch.com/docs

官方 skill 的关键路由规则：

- 默认推荐 vertical path。
- 对 finance、academic、travel、health、code、legal、gaming、film、business、security、ip、energy、environment、agriculture、resource、social_media 等 domain，先 `get_sub_domains`，再 search。
- required `sub_domain_params` 必须传齐；没有值也传空字符串。
- `extract` 是已知 URL 读取，不是 keyword search。
- API key optional；无 key 继续 anonymous access。

Hive 定位：

| 项 | 判断 |
|----|------|
| no-key | 默认 no-key anonymous |
| 工具层 | Advanced no-key search/fetch provider |
| 最适合场景 | vertical/domain search、public social discovery、finance/academic/legal/health/security/code 等结构化搜索、hybrid batch search |
| 需要修正 | 当前 Hive MCP adapter 不应默认拒绝 no-key |

### 2.3 Exa

官方定义：

- Exa MCP connects AI assistants to Exa search capabilities.
- Server URL: `https://mcp.exa.ai/mcp`
- Default tools include `web_search_exa` and `web_fetch_exa`。
- API key 用于突破 free plan limits 和生产使用。
- Exa Agent tools 需要 OAuth 或 API key。

官方来源：

- https://exa.ai/docs/reference/exa-mcp
- https://exa.ai/mcp

Hive 定位：

| 项 | 判断 |
|----|------|
| no-key | Exa MCP default search/fetch no-key |
| 工具层 | Advanced no-key search/fetch provider |
| 最适合场景 | semantic/source discovery、company/person/research-paper/source-rich search、LLM-ready extracted result text |
| 不应默认放入 | Exa Agent async research/list-building/enrichment tools，除非认证和费用治理已做完 |
| 需要补齐 | 当前 Hive 主要有 `exa_search`；应补 `exa_fetch` 或纳入 `advanced_web_fetch` |

### 2.4 Tavily

官方定义：

- AI search engine optimized for agent consumption.
- Keyless Search and Extract: no account, no API key, no configuration.
- Direct API keyless header: `X-Tavily-Access-Mode: keyless`。
- Remote MCP no-key exposes `tavily-search` and `tavily-extract`。
- Crawl / Map / Research 需要 API key。

官方来源：

- https://docs.tavily.com/documentation/keyless
- https://docs.tavily.com/agents

Hive 定位：

| 项 | 判断 |
|----|------|
| no-key | Search / Extract 默认 no-key |
| 工具层 | Advanced no-key search/fetch provider |
| 最适合场景 | current factual retrieval、news/finance/current web、RAG-friendly results、freshness filters |
| 不应默认放入 | Crawl / Map / Research |
| 需要补齐 | 当前 Hive 主要有 `tavily_search`；应补 `tavily_extract` 或纳入 `advanced_web_fetch` |

### 2.5 Firecrawl

官方定义：

- context API to search, scrape, and interact with the web at scale。
- keyless free tier supports search, scrape, interact。
- Hosted MCP no-key endpoint: `https://mcp.firecrawl.dev/v2/mcp`。
- Crawl / Map / Agent / Extract still need key。
- Firecrawl skill 路由强调：search first, scrape when URL is known, interact only when page needs clicks/forms/login-like navigation.

官方来源：

- https://www.firecrawl.dev/
- https://www.firecrawl.dev/blog/firecrawl-keyless-launch
- https://github.com/firecrawl/firecrawl-mcp-server
- https://www.firecrawl.dev/agent-onboarding/SKILL.md

Hive 定位：

| 项 | 判断 |
|----|------|
| no-key | Search / Scrape / Interact no-key，但 Hive 默认只应接 search/scrape |
| 工具层 | Advanced no-key search/fetch provider |
| 最适合场景 | search result with page content、known URL rendered scrape、JS-heavy page scrape |
| 需要治理 | `interact` 是网页操作能力，不应混入普通 fetch 默认链；需要 ActionPreflight / auth boundary |
| 需要补齐 | 当前 Hive 有 `firecrawl_fetch`；应补 `firecrawl_search` 或纳入 `advanced_web_search` |

### 2.6 XCrawl

当前依据：

- Hive 当前已有 `xcrawl_scrape` adapter。
- 当前实现需要 `XCRAWL_API_KEY`。
- 用户本轮明确选择方案 A：保留；有 key 才放进工具面；无 key 不放。

Hive 定位：

| 项 | 判断 |
|----|------|
| no-key | 不作为 no-key provider |
| 工具层 | Optional keyed add-on |
| 最适合场景 | hard JS-rendered/proxy/device/locale scrape case |
| 默认可见性 | 仅有 key 时展示 |

### 2.7 ScrapeGraphAI

官方定义：

- 托管 v2 API：Scrape / Extract / Search / Crawl / Monitor / History。
- 所有托管 v2 API request 都需要 `SGAI-APIKEY` header。
- 开源 Python library 可用 LLM + graph logic 构建 scraping pipeline，可读取网站和本地文档。

官方来源：

- https://docs.scrapegraphai.com/api-reference/introduction
- https://github.com/ScrapeGraphAI/Scrapegraph-ai

Hive 定位：

| 项 | 判断 |
|----|------|
| hosted no-key | 否 |
| local/self-host no-key | 可以作为本地/自托管 extraction adapter 评估 |
| 工具层 | 不进默认 Advanced no-key search/fetch |
| 最适合场景 | 后续 structured extraction / schema extraction adapter，不是默认 search/fetch provider |

### 2.8 Jina Reader / Search

官方定义：

- `r.jina.ai` converts URL to LLM-friendly text。
- `s.jina.ai` can be used as SERP API and returns top results with URLs and contents。
- 不带 API key 有 IP-based rate limits；API key 用于更高限额。

官方来源：

- https://jina.ai/reader/

Hive 定位：

| 项 | 判断 |
|----|------|
| no-key | 可作为 no-key candidate |
| 工具层 | Optional Advanced no-key fallback |
| 最适合场景 | simple reader fallback、search+content low-friction fallback |
| 落地建议 | P1 引入，先不阻塞本轮 Exa/Tavily/Firecrawl/AnySearch/XCrawl 收口 |

## 3. 最终分层

### 3.1 CORE tools

| Tool | 职责 | Provider |
|------|------|----------|
| `web_search` | 基础公开搜索；正常 lookup 起点 | SearXNG when configured；`duckduckgo_legacy` only manual/debug |
| `web_fetch` | 已知 URL 轻量读取；文档转换；基础 source acquisition | Direct HTTP + `DocumentConversionService` |

CORE layer 的原则：

- 不根据 Exa/Tavily/Firecrawl/AnySearch 是否有 key 改变基础工具语义。
- `web_search` 不调用 AnySearch。
- `web_fetch` 不变成 crawler API wrapper；它只在失败/空内容/JS shell 时升级。

### 3.2 Advanced no-key tools

建议新增两个主入口：

| Tool | 职责 | Provider 路由 |
|------|------|---------------|
| `advanced_web_search` | 高级搜索路由器 | AnySearch / Exa / Tavily / Firecrawl Search / optional Jina Search |
| `advanced_web_fetch` | 高级抓取/抽取路由器 | Firecrawl Scrape / Tavily Extract / Exa Fetch / AnySearch Extract / optional Jina Reader / XCrawl if keyed |

Provider-specific tools 可以继续存在：

- `anysearch_get_sub_domains`
- `anysearch_search`
- `anysearch_batch_search`
- `anysearch_extract`
- `exa_search`
- `tavily_search`
- `firecrawl_fetch`
- `xcrawl_scrape`

但 skill 和 runtime prompt 应把它们描述为 provider adapter / override surface，而不是让模型从一堆 provider 名字里自由猜。

### 3.3 Optional keyed add-ons

| Provider / capability | 默认状态 | 原因 |
|-----------------------|----------|------|
| `xcrawl_scrape` | 有 key 才展示 | 当前实现和用户决策均为 keyed add-on |
| Tavily Crawl / Map / Research | keyed only | 官方明确 keyless 不覆盖 |
| Firecrawl Crawl / Map / Agent / Extract | keyed only | 官方明确 keyless 不覆盖 |
| Exa Agent tools | authenticated only | 官方明确 Agent tools require authentication |
| ScrapeGraphAI hosted API | keyed only | 官方 v2 API 要 `SGAI-APIKEY` |

## 4. 场景调用规则

### 4.1 基础查询

```text
User asks simple factual lookup
  -> web_search
  -> web_fetch selected authoritative URLs
```

例子：

- 公司官网、文档页、普通新闻、基本事实查询。

### 4.2 基础搜索弱、太宽、太旧或冲突

```text
web_search weak/sparse/stale/contradictory
  -> advanced_web_search
```

Router 判断：

| 场景 | 优先 provider |
|------|---------------|
| vertical/domain-specific, finance/social/academic/legal/health/security/code | AnySearch |
| semantic source discovery, company/person/research-paper/source-rich search | Exa |
| latest/current/news/factual/RAG-friendly answer | Tavily |
| 需要 search 结果直接带 full-page markdown | Firecrawl Search |
| lightweight search+content fallback | Jina Search, if introduced |

### 4.3 已知 URL 读取

```text
User provides URL
  -> web_fetch
  -> if empty/incomplete/JS shell/blocked:
       advanced_web_fetch
```

Router 判断：

| 场景 | 优先 provider |
|------|---------------|
| known URL needs rendered scrape | Firecrawl Scrape |
| known URL needs clean extraction / RAG content | Tavily Extract |
| known URL via Exa MCP fetch | Exa Fetch |
| AnySearch vertical workflow already selected URL | AnySearch Extract |
| hard JS/proxy/device/locale | XCrawl only if key configured |
| lightweight markdown reader fallback | Jina Reader, if introduced |

### 4.4 AnySearch vertical workflow

```text
advanced_web_search detects vertical domain
  -> anysearch_get_sub_domains(domain or domains)
  -> select sub_domain
  -> pass all required sub_domain_params
  -> anysearch_search or anysearch_batch_search
  -> web_fetch / anysearch_extract / advanced_web_fetch for selected URLs
```

规则：

- 不能跳过 `get_sub_domains`。
- 不能 invent `sub_domain`。
- required params 必须传齐；无值传空字符串。
- `anysearch_extract` 只用于 URL，不用于 keyword search。

### 4.5 Firecrawl workflow

```text
Need discovery
  -> firecrawl_search

Have URL
  -> firecrawl_fetch / scrape

Need page actions
  -> not default web fetch
  -> future interact tool with ActionPreflight and explicit boundary
```

Firecrawl `interact` 不应悄悄进入默认 fetch fallback。它涉及 click / form / navigation，虽然官方 keyless 支持，但 Hive 需要单独的 action boundary。

## 5. MCP vs API 接入原则

### 5.1 默认原则

| 情况 | 推荐接入 |
|------|----------|
| 官方 remote MCP 已明确 no-key，工具 schema 稳定，能力本身就是 agent tool | MCP first |
| 需要低延迟、严格参数控制、精确错误分类、已有 direct API adapter | API first |
| 需要两者兼容 | no-key MCP default + keyed direct API override |

### 5.2 Provider 判断

| Provider | 默认接入 | keyed override |
|----------|----------|----------------|
| AnySearch | MCP adapter first | Authorization header |
| Exa | MCP no-key first | direct API or keyed MCP |
| Tavily | Direct API keyless header first; MCP also acceptable | Authorization Bearer |
| Firecrawl | Direct API for existing `firecrawl_fetch`; MCP optional for future discovery | Authorization Bearer or keyed MCP URL |
| Jina | Direct URL/API | API key header for higher limits |
| XCrawl | Direct API only | required |
| ScrapeGraphAI hosted | Direct API only | required |

原因：

- Hive 已经有 `ToolRuntimeService`、capability gate、tool schema、config/secrets、fallback/error rendering。直接 import 外部 MCP server 为用户可见工具会绕开 Hive 的一部分治理语义。
- 对 AnySearch/Exa 这类 MCP-native provider，可以后端内置 MCP adapter，把它们封装成 Hive 原生 tool，而不是要求用户手动 import MCP。
- 对 Tavily/Firecrawl 这种 direct API 已足够清晰且 keyless header/无 header 行为明确的 provider，直接 API 更容易做错误分类和测试。

## 6. 代码落点

### 6.1 Runtime / provider adapter

文件：

- `backend/app/services/agent_tool_domains/web_mcp.py`

改动：

1. AnySearch:
   - `_call_anysearch_mcp_tool()` 默认允许 anonymous。
   - 移除 “anonymous only for dev/eval” 的提示。
   - 保留 explicit `auth_mode=api_key` 或 future policy 时的强制 key 行为。
   - 处理 quota/rate limit 时提示可配置 key，但不把 no-key 当未配置。

2. Exa:
   - 保留 no-key MCP search fallback。
   - 补 Exa fetch path，调用 `web_fetch_exa`，或先纳入 `advanced_web_fetch` 内部 provider。
   - 明确 Exa Agent tools 不进入默认 no-key。

3. Tavily:
   - `tavily_search` 无 key 时使用 `X-Tavily-Access-Mode: keyless`。
   - 补 `tavily_extract` 或纳入 `advanced_web_fetch`。
   - Crawl / Map / Research 不进入 no-key availability。

4. Firecrawl:
   - `firecrawl_fetch` 无 key 时不加 `Authorization`。
   - 补 `firecrawl_search` 或纳入 `advanced_web_search`。
   - `interact` 暂不进入默认工具面；未来单独接 action-gated tool。

5. XCrawl:
   - 保持 key required。
   - 保持无 key 不展示。

6. Advanced routers:
   - 新增 `_advanced_web_search(arguments)`。
   - 新增 `_advanced_web_fetch(arguments)`。
   - Router 只负责 provider selection、fallback、error synthesis，不替代 provider adapters。

### 6.2 Tool definitions

文件：

- `backend/app/tools/handlers/search.py`

改动：

1. 新增 `advanced_web_search` tool meta。
2. 新增 `advanced_web_fetch` tool meta。
3. 修正 provider-specific tool descriptions：
   - `web_search`: 明确 CORE/SearXNG，不提 AnySearch 是主 fallback。
   - `anysearch_*`: 明确 no-key anonymous default + vertical schema workflow。
   - `exa_search`: 明确 Exa MCP default no-key；key 只是提升限额/生产控制。
   - `tavily_search`: 明确 Search keyless；Extract 另有 fetch path。
   - `firecrawl_fetch`: 明确 Scrape keyless；不要说 crawler/search 混用。
   - `xcrawl_scrape`: 明确 keyed add-on。

### 6.3 Availability / discoverability

文件：

- `backend/app/services/agent_tools.py`

改动：

1. `_provider_available_tools()`：
   - AnySearch tools 默认可用，除非显式 `auth_mode=api_key` 且没有 key。
   - Exa/Tavily/Firecrawl no-key tools 默认可用。
   - XCrawl 仍 `if xcrawl_key` 才 available。
2. `_filter_unavailable_tools()`：
   - 不应把 no-key-capable provider 当作缺 key 后隐藏。
3. tool_search surface：
   - 默认发现 `advanced_web_search` / `advanced_web_fetch`。
   - provider-specific tools 可以作为 advanced/override 被发现。

### 6.4 Governance taxonomy

文件：

- `backend/app/services/governance_capability_taxonomy.py`

改动：

1. `web_search` / `web_fetch` 保持 CORE。
2. `advanced_web_search` / `advanced_web_fetch` 属于 `web_pack` L2 extension。
3. AnySearch / Exa / Tavily / Firecrawl no-key provider tools 属于 `external.web.search` 或 `external.web.read`。
4. XCrawl 属于 `external.web.read` + keyed add-on wording。
5. 不把 ScrapeGraphAI hosted API 加进 default web_pack。

### 6.5 Skill

文件：

- `backend/app/templates/system_skills/web-research/SKILL.md`

改动：

1. 重写为 “CORE first, advanced router second, provider override third”。
2. 明确 SearXNG 只属于 CORE `web_search`。
3. 明确 AnySearch no-key default，但 vertical workflow 要先 `get_sub_domains`。
4. 明确 Tavily Search/Extract keyless，Crawl/Map/Research keyed only。
5. 明确 Firecrawl Search/Scrape keyless，Interact future action-gated，Crawl/Map/Agent/Extract keyed only。
6. 明确 XCrawl keyed add-on，有 key 才出现。
7. 增加 anti-pattern：
   - 不要把 SearXNG 列入高级 no-key layer。
   - 不要用 crawler fetch 做 keyword search。
   - 不要用 `anysearch_extract` 做搜索。
   - 不要把 provider auth failure 误报为 “没有联网能力”。

### 6.6 Tests

文件：

- `backend/tests/services/test_web_mcp_resilience.py`
- `backend/tests/services/test_agent_tools.py`
- `backend/tests/tools/test_search_provider_tool_definitions.py`

新增/调整测试：

1. `web_search`:
   - `search_engine=anysearch` 仍归一到 CORE auto。
   - SearXNG 仍只属于 CORE。

2. AnySearch:
   - 无 key 默认调用 MCP，不返回 provider_not_configured。
   - 有 key 时带 `Authorization: Bearer`。
   - 显式 `auth_mode=api_key` 且无 key 时才不可用。
   - skill/definition 中不再写 “anonymous for dev/eval only”。

3. Exa:
   - 无 key 使用 MCP no-key search。
   - 有 key 使用 configured direct/API path 或 keyed MCP path。
   - Exa fetch path 可被 `advanced_web_fetch` 调用。

4. Tavily:
   - 无 key search/extract 使用 `X-Tavily-Access-Mode: keyless`。
   - 有 key 使用 Bearer。
   - Crawl/Map/Research 不作为 no-key available。

5. Firecrawl:
   - 无 key scrape/search 不加 Authorization。
   - 有 key加 Authorization。
   - Crawl/Map/Agent/Extract 不作为 no-key available。

6. XCrawl:
   - 无 key 不出现在 provider available tools。
   - 无 key 直接调用时仍 `provider_not_configured`。
   - 有 key 才 available。

7. Router:
   - `advanced_web_search` vertical domain routes to AnySearch。
   - current/news query routes to Tavily。
   - semantic/company/person/research-paper routes to Exa。
   - search-with-content route can use Firecrawl Search。
   - `advanced_web_fetch` JS/blocked known URL routes to Firecrawl first, XCrawl only when keyed。

验证命令：

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_web_mcp_resilience.py \
  tests/services/test_agent_tools.py \
  tests/tools/test_search_provider_tool_definitions.py -q

ruff check app/services/agent_tool_domains/web_mcp.py \
  app/services/agent_tools.py \
  app/services/governance_capability_taxonomy.py \
  app/tools/handlers/search.py \
  tests/services/test_web_mcp_resilience.py \
  tests/services/test_agent_tools.py \
  tests/tools/test_search_provider_tool_definitions.py
```

### 6.7 Frontend / UI 适配

结论：需要改前端，但不应大改成第二套路由系统。后端仍是工具可用性、provider routing、auth mode、config schema、taxonomy 的真相源；前端只做展示、配置和治理操作的适配。

当前前端相关路径：

- `frontend/src/pages/workspace/WorkspaceToolsSection.tsx`
- `frontend/src/pages/workspace/WorkspaceToolsSection.css`
- `frontend/src/pages/workspace/WorkspaceToolsSection.test.tsx`
- `frontend/src/api/domains/tools.ts`
- `frontend/src/api/domains/enterprise.ts`

当前 UI 事实：

- Workspace Tools 页通过 `toolsApi.listCatalog()` 读取 `/tools` catalog。
- `isExtensionOrAddonTool()` 依赖后端返回的 `governance_taxonomy.l2_visible` 和 `enterprise_toggleable` 判断是否展示在 Extensions & Add-ons。
- 工具卡片展示 `display_name`、`description`、`config_schema.fields`、`enabled`、`is_default` 和 governance status。
- capability 归属来自 `enterpriseApi.listCapabilityDefinitions()`，再用 `resolveWorkspaceToolCapability()` 按 tool name 匹配。
- 前端目前只硬编码了 `GLOBAL_CATEGORY_CONFIG_SCHEMAS.agentbay`；web provider 的 key/config 不应再新增一套前端硬编码配置。

因此后端落地时必须给前端足够 metadata：

```json
{
  "governance_taxonomy": {
    "name": "web_pack",
    "layer": "platform_addon",
    "l2_visible": true,
    "enterprise_toggleable": true,
    "default_enabled": true,
    "source": "taxonomy"
  },
  "config_schema": {
    "fields": [
      {
        "key": "auth_mode",
        "label": "Auth mode",
        "type": "select",
        "options": [
          { "value": "auto", "label": "Auto (keyless by default, key upgrades limits)" },
          { "value": "keyless", "label": "Keyless" },
          { "value": "api_key", "label": "API key required" }
        ],
        "default": "auto"
      }
    ]
  }
}
```

前端必须做的 UI 适配：

1. **Web Pack 分组说明**
   - 在 Extensions & Add-ons 的 `web_pack` category 头部展示一句说明：
     `Advanced web research defaults to no-key providers. API keys are optional upgrades unless a tool is marked keyed-only.`
   - 中文 copy：
     `高级 Web Research 默认使用免 key provider；API key 只是提升限额/生产控制，除非工具标记为 keyed-only。`

2. **No-key / Optional key / Keyed-only badge**
   - 从后端 `config_schema` 或新增 `provider_auth` metadata 渲染状态。
   - 建议状态：
     - `No key by default`
     - `Optional key`
     - `Key required`
   - AnySearch、Exa、Tavily、Firecrawl 的 no-key surface 显示 `No key by default` 或 `Optional key`。
   - XCrawl 显示 `Key required`，无 key 时不应作为默认工具出现；如果管理员打开详情页，应看到它是 keyed add-on。

3. **Advanced router 优先展示**
   - 如果新增 `advanced_web_search` / `advanced_web_fetch`，UI 应在 `web_pack` 分组里把这两个工具排在 provider-specific tools 前面。
   - Provider-specific tools 仍展示，但 copy 应表达为 override/diagnostic/provider adapter。
   - 不要让用户以为必须逐个配置 Exa/Tavily/Firecrawl key 才能使用高级 Web Research。

4. **配置面 copy**
   - `auth_mode=auto` label 应统一为：
     `Auto (keyless by default; API key upgrades limits)`
   - `api_key` placeholder/description 应统一写成 optional upgrade，不要写成 required。
   - XCrawl 的 `api_key` 才写 required。

5. **隐藏/不可用状态**
   - 无 key 的 XCrawl 不应被 `agent_tools._provider_available_tools()` 返回，也就不会进入 agent discoverable tool surface。
   - 如果 catalog 里仍存在 `xcrawl_scrape` builtin row，Workspace UI 可以展示为 `Key required`，但不能暗示它默认可运行。
   - 更好的做法是后端在 catalog payload 增加 `availability` metadata，例如：

```json
{
  "availability": {
    "available": false,
    "reason": "missing_required_api_key",
    "keyless_supported": false,
    "credential_optional": false
  }
}
```

6. **MCP import UI 不作为默认路径**
   - Exa / AnySearch / Firecrawl 官方 MCP 可以 no-key，但 Hive 默认高级 web research 不应要求用户去 “Add MCP Server” 手动 import。
   - Workspace 的 MCP Servers tab 保留给外部自定义 MCP；内置 web providers 通过 native tools / backend adapters 展示。

7. **i18n**
   - 如果新增可见 UI 文案，必须同步 `frontend/src/i18n/locales/en.json` 和 `frontend/src/i18n/locales/zh.json`。
   - 不能只在 TSX 里用英文 fallback 当最终文案。

前端不应做的事：

- 不在前端硬编码 AnySearch/Exa/Tavily/Firecrawl/XCrawl 的路由逻辑。
- 不在前端探测 env/API key。
- 不让用户把 provider key 粘进聊天或普通文本。
- 不把官方 remote MCP onboarding 当成 Hive 默认接入流程。

前端测试：

```bash
cd frontend
npm run test -- WorkspaceToolsSection
npm run build
```

建议新增/调整测试：

- `isExtensionOrAddonTool()` 能正确展示 `advanced_web_search` / `advanced_web_fetch`。
- `resolveWorkspaceToolCapability()` 能把新工具映射到 `external.web.search` / `external.web.read`。
- Workspace tools 渲染 no-key / optional key / keyed-only badge。
- XCrawl key-required copy 不会和 no-key provider 混淆。
- `web_pack` 分组中 advanced routers 排在 provider-specific tools 前面。

## 7. 迁移策略

### 7.1 不破坏现有工具名

短期保留 provider-specific tools：

- `anysearch_*`
- `exa_search`
- `tavily_search`
- `firecrawl_fetch`
- `xcrawl_scrape`

原因：

- 测试和旧 prompt 已依赖。
- tool_search 仍可精准发现 provider。
- 便于排查 provider-specific failure。

但默认 skill 路由应推：

```text
web_search / web_fetch
  -> advanced_web_search / advanced_web_fetch
  -> provider-specific override
```

### 7.2 AnySearch anonymous 默认迁移

旧行为：

```text
no key + anysearch_allow_anonymous=false
  -> provider_not_configured
```

新行为：

```text
no key
  -> anonymous access

auth_mode=api_key + no key
  -> provider_not_configured
```

如果企业租户不允许 anonymous external provider，后续应通过 governance policy 控制，而不是 hardcode 到 provider adapter。

### 7.3 XCrawl keyed add-on

保持现状：

```text
no XCRAWL_API_KEY
  -> no xcrawl_scrape in available tools

has XCRAWL_API_KEY
  -> xcrawl_scrape available
```

`advanced_web_fetch` router 也必须遵守这个规则。无 key 时不能把 XCrawl 写进 fallback plan。

## 8. 最终目标状态

### 8.1 Agent 看到的心智模型

```text
For normal lookup:
  use web_search

For known URL:
  use web_fetch

When core is insufficient:
  use advanced_web_search or advanced_web_fetch

Only when provider-specific control is needed:
  use AnySearch / Exa / Tavily / Firecrawl / XCrawl specific tools
```

### 8.2 系统默认 no-key provider

| Provider | Search | Fetch / Extract | Notes |
|----------|--------|-----------------|-------|
| SearXNG | CORE `web_search` | No | Needs configured instance URL; not L2 |
| AnySearch | Yes | Yes | Anonymous by default; key optional |
| Exa MCP | Yes | Yes | Agent tools need auth |
| Tavily | Yes | Yes | Crawl/Map/Research need key |
| Firecrawl | Yes | Scrape yes | Interact no-key but action-gated later; Crawl/Map/Agent/Extract need key |
| Jina | Candidate | Candidate | P1 fallback candidate |
| XCrawl | No | Keyed only | Optional add-on |
| ScrapeGraphAI hosted | Keyed only | Keyed only | Not default no-key |

### 8.3 Acceptance criteria

本轮完整落地后，必须满足：

1. 文档、tool definitions、skill、taxonomy 对 SearXNG 的定位一致：CORE only。
2. AnySearch 官方 no-key 行为和 Hive adapter 行为一致：无 key 默认 anonymous。
3. Exa/Tavily/Firecrawl no-key surface 与官方边界一致，不把 keyed-only 子能力误放默认层。
4. XCrawl 无 key 时不展示、不被 router 选中。
5. `web_search` / `web_fetch` 基础语义不被高级 provider 污染。
6. `advanced_web_search` / `advanced_web_fetch` 给模型一个清晰高级入口，减少 provider 自由猜测。
7. 所有行为均有测试覆盖，并通过 targeted pytest + ruff。

## 9. 一次性实施顺序

这不是分阶段 MVP；实现时按一个完整 PR/patch 收口：

1. Red tests:
   - 写 AnySearch no-key、XCrawl keyed-only、Exa/Tavily/Firecrawl no-key boundary、advanced router routing tests。
2. Green implementation:
   - 修 `web_mcp.py` provider adapter。
   - 加 `advanced_web_search` / `advanced_web_fetch` handler。
   - 修 availability。
   - 修 taxonomy。
   - 修 skill。
3. Refactor:
   - 提取 provider auth-mode helper，避免 Exa/Tavily/Firecrawl/AnySearch 各写一套判断。
   - 保持 provider-specific adapters 小而清晰。
4. Verification:
   - targeted pytest。
   - targeted ruff。
   - diff review：确认没有 unrelated refactor。

## 10. 不做事项

- 不把 SearXNG 从 CORE 移到 advanced layer。
- 不把 AnySearch 重新塞回 `web_search` 主 provider。
- 不把 Firecrawl Interact 当普通 fetch fallback。
- 不把 Tavily Crawl/Map/Research、Firecrawl Crawl/Map/Agent/Extract、Exa Agent tools 放进 no-key 默认层。
- 不把 XCrawl 无 key 时展示给 agent。
- 不把 ScrapeGraphAI hosted API 当 no-key provider。
- 不要求用户手动 import 外部 MCP 才能用默认高级 web research。
