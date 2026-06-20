# Web Data Source Layer 调研与改造方案

| 字段 | 内容 |
|------|------|
| 状态 | Discussion draft |
| 日期 | 2026-06-20 |
| 范围 | AI 联网资料抓取、数据 API、行业数据源、社媒/新闻/科研资料源的统一路由层 |
| 非范围 | 自动下单、登录态/交互式浏览器操作、绕过平台授权的账号操作、违反站点条款的批量爬取、把第三方框架直接变成 Hive runtime 真相源 |

## 0. 结论

没有一个开源框架能直接满足 Hive 要的完整形态：金融/行业数据 API、新闻科研、社交媒体、网页抓取、结构化抽取、凭证治理、source ledger、tenant 权限、agent tool surface 全部统一。

但这不是从零重做网页抓取。当前 Hive 已经有 `web_pack` 抓取链：`web_search` / `web_fetch` 是 CORE，`exa_search` / `tavily_search` / `firecrawl_fetch` / `xcrawl_scrape` 是 provider-backed 升级能力。新的 layer 应该包在现有链路外面，补的是数据源路由、API catalog、source ledger 和领域 schema，不替换已有工具。

关键工程原则：网络、渠道、connector、权限层负责拿到可信 source；`DocumentConversionService` 负责把 source 转成 Markdown artifact；Agent 优先消费这个 Markdown artifact。`web_fetch` 是 known-URL source acquisition tool，不是全平台 source-to-Markdown 转换真相源。

正确做法是做 Hive 自己的 `Web Data Source Layer`：

```
Agent tools / Skills / Deep Research
  -> Source Router
  -> Provider Registry
  -> Connector Runtime
  -> Extract / Normalize
  -> Source Ledger
  -> Domain Tools
```

现有 `web_pack` 是 Web context provider 的第一实现；第三方项目只作为 provider adapter 或执行引擎，不作为系统 source of truth。

## 1. 当前仓库事实

### 1.1 已有基础

当前 checkout 已有以下可复用基础，实施时必须复用，不能重新造一套平行抓取系统：

1. `web_pack`
   - `web_search` / `web_fetch` 是 CORE 基础能力。
   - `exa_search` / `tavily_search` / `firecrawl_fetch` / `xcrawl_scrape` 是 provider-backed 升级能力。
   - 路径：`backend/app/tools/handlers/search.py`、`backend/app/services/agent_tool_domains/web_mcp.py`、`packs/web_pack/pack.yaml`。

2. `web_search`
   - 支持 SearXNG / DuckDuckGo。
   - `auto` 模式优先 SearXNG，未配置则 DuckDuckGo。
   - SearXNG 失败会 fallback 到 DuckDuckGo。
   - 会拒绝 URL 输入，要求关键词；已知 URL 应走 `web_fetch`。
   - 不会自动路由到 Exa/Tavily，避免高级 provider key 被无意消费。

3. Advanced search provider
   - `exa_search`：Exa AI-native search，需要 Exa key；支持 search type、category verticals、内容抽取，适合研究论文、公司/人物页、金融报告、语义/source discovery。
   - `tavily_search`：Tavily real-time web access layer，需要 Tavily key；支持 topic、search_depth、freshness filters、provider answer/raw content，适合当前事实、新闻/金融/RAG 检索。
   - 二者都是 `tool_search` 发现后的升级工具，不是基础搜索替代品。

4. `web_fetch`
   - 已知 URL 直读，支持自动补 `https://`。
   - HTTP GET、follow redirects、20s timeout。
   - 禁止直接读 Feishu OpenAPI URL，提示使用 Feishu 专用工具。
   - PDF/HTML/Office 等可转换内容交给 `DocumentConversionService` 产出 Markdown artifact；坏 PDF 会返回结构化 `unreadable_pdf`，避免 `%PDF` mojibake 污染证据。
   - HTML 转换由 MarkItDown 优先处理；`trafilatura` 和 `_HTMLTextExtractor` 只作为 `DocumentConversionService` 的 legacy fallback。
   - 识别 React/Next 等 JS shell 的短空内容，触发 crawler-backed fallback。
   - `web_fetch` 自己保留 URL、网络、fallback、source metadata 边界。

5. Crawler-backed fallback
   - `firecrawl_fetch`：Firecrawl scrape v1，返回 markdown；失败时可 fallback 到 `web_fetch`。
   - `xcrawl_scrape`：XCrawl scrape，支持 `js_render`；失败时 fallback 到 `firecrawl_fetch`。
   - `web_fetch` 在 HTTP 错误、空内容、不可读 PDF、JS shell 时，会按 key 可用性升级到 Firecrawl/XCrawl。

6. Deep Research ledger
   - Deep Research 已有 `EvidenceLedger`，会写 `sources.jsonl` / `claims.jsonl`。
   - source 记录包含 `source_id`、URL、title、publisher、content、lane、query、fetch_tool，并做 evidence grade。
   - 但这是 Deep Research run-local ledger，不是跨工具共享的数据源 ledger。

7. `trafilatura`
   - `backend/pyproject.toml` 已安装 `trafilatura>=1.12.0`。
   - 2026-06-20 后，HTML 正文抽取不再由 `web_fetch` 直接拥有；`web_fetch` 获取可信 source 后交给 `DocumentConversionService`，由 MarkItDown 优先产出 Markdown artifact。
   - `trafilatura` 保留为 `DocumentConversionService` legacy HTML fallback，不新增并行 `data_source_fetch` 抓网页工具。

8. Custom API Connector
   - 已有 tenant-owned `custom_api__*` tool。
   - 凭证通过 tool config 服务端注入，LLM 参数里传 `api_key` / `token` 会被拒绝。
   - 路径：`backend/app/services/custom_api_connectors.py`、`backend/app/api/custom_api_connectors.py`、`frontend/src/pages/workspace/WorkspaceToolsSection.tsx`。

### 1.2 当前缺口

`docs/SKILLS_AND_PACKS_V2.md` 里写过 finance data layer skeleton，但当前 checkout 没有 `backend/app/finance_data/`。因此现在的实际状态是：

- 有较完整的联网搜索/抓取链。
- 有 custom API 连接器。
- 有 deep research source ledger 方向。
- 没有跨 `web_pack` / `custom_api` / deep research 共用的 `SourceArtifact` / data source ledger。
- 没有“先 API、再 search/fetch fallback”的 data source router。
- 没有把 Custom API 升级成 provider template / operation catalog。
- 没有金融、行业、科研、社媒数据源的领域 connector。
- `web_fetch` 的 HTML/PDF/Office 等正文转换应统一交给 `DocumentConversionService`；`trafilatura` 只作为 legacy HTML fallback，不新增平行抓取工具。

## 2. 可复用开源框架调研

### 2.1 网页转 LLM-ready 内容

| 项目 | 适合用法 | 许可证/边界 | Hive 判断 |
|------|----------|-------------|-----------|
| [MarkItDown](https://github.com/microsoft/markitdown) | 本地文件/stream 到 Markdown；适合 WebFetch 已抓取 HTML/PDF/Office bytes 后的 canonical conversion。 | MIT；必须用窄转换 API，不把用户 URL 直接交给 converter。 | 放在 `DocumentConversionService` 后面作为默认转换 engine，不作为 crawler/search provider。 |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | 自托管网页抓取、markdown 生成、结构化抽取、公开页面渲染 | Apache-2.0 | P0 候选。适合作为自托管 `web_reader` / `web_crawler` provider；不使用登录态 session。 |
| [Jina Reader](https://github.com/jina-ai/reader) | URL/search -> LLM-friendly markdown，轻量 reader fallback | Apache-2.0，OSS 分支不含 SaaS 存储层 | P0 候选。适合作为低成本 reader provider，不要当唯一抓取层。 |
| [Trafilatura](https://github.com/adbar/trafilatura) | 静态网页正文、metadata、评论、文本抽取 | Apache-2.0 for v1.8+，旧版本 GPLv3+ | 作为 `DocumentConversionService` legacy HTML fallback，不做新工具，不作为 `web_fetch` 主转换面。 |
| [Firecrawl](https://github.com/firecrawl/firecrawl) | search/scrape/crawl/map/batch/interact，markdown/JSON/PDF/office，MCP/skill 生态 | AGPL-3.0；托管服务可用 | 作为外部 provider 接入，不建议把 AGPL 代码嵌入闭源 SaaS 核心。 |

### 2.2 高性能抓取与服务端渲染

| 项目 | 适合用法 | Hive 判断 |
|------|----------|-----------|
| [Crawlee](https://github.com/apify/crawlee) | 大规模 crawling、Playwright/Puppeteer/Cheerio/raw HTTP、proxy rotation | P1。适合 crawler worker，不适合作为 agent 直接工具面。 |
| Scrapy | 传统大规模爬虫、pipeline、scheduler | P1/P2。适合特定站点长期抓取，不适合 LLM-ready 输出主路径。 |
| Playwright | 公开页面服务端渲染、等待 JS 加载后抽取 DOM | 只能作为 provider 内部实现，不能暴露成 agent 交互式浏览器工具，也不处理登录态任务。 |

### 2.3 AI 结构化抽取

| 项目 | 适合用法 | Hive 判断 |
|------|----------|-----------|
| [ScrapeGraphAI](https://github.com/ScrapeGraphAI/Scrapegraph-ai) | 用自然语言 prompt + graph pipeline 从网页/HTML/JSON/Markdown 抽结构化字段 | P1 候选。适合 `data_extract_structured` provider，但必须套 schema、source refs、eval。 |
| Firecrawl Extract/Agent | 结构化 JSON 和 autonomous web data gathering | Provider-backed P1；受 AGPL/托管服务边界影响。 |
| LLM 自建 extractor | 针对金融/科研/社媒字段抽取 | 必须保留。Hive 的 LLM 才是智能判断主路径，平台只做治理和验证。 |

### 2.4 Skills / MCP 借鉴

Firecrawl 已经提供 CLI skill、MCP server 和 agent onboarding。这类项目说明一个趋势：高性能联网能力正在从“库”升级成“agent capability package”。

Hive 应吸收这个形态，但执行必须走 Hive 自己的治理：

- Skill 只提供方法、schema、fallback 策略和 source-quality rubric。
- 真正执行走 `ToolRuntimeService`、connector runtime、credential config、source ledger。
- 外部 MCP 可以作为 optional provider，但不能绕过 Hive 的 capability gate 和 tenant credential。

## 3. 推荐架构

### 3.1 核心对象

```python
class DataSourceProvider:
    id: str
    category: str  # web_reader | crawler | search | api | social | finance | research
    adapter: str
    license_policy: str
    credential_requirements: list[str]
    capabilities: list[str]

class SourceArtifact:
    source_id: str
    provider_id: str
    source_type: str  # webpage | filing | api_json | pdf | social_post | paper | patent
    url: str | None
    retrieved_at: str
    content_hash: str
    raw_ref: str | None
    normalized_ref: str | None
    license_scope: str
    credential_scope: str

class DataObservation:
    observation_id: str
    entity_id: str | None
    field: str
    value: object
    unit: str | None
    as_of: str | None
    confidence: float
    source_ids: list[str]
```

### 3.2 路由层

Source Router 根据任务选择数据路径：

| 意图 | 优先路径 |
|------|----------|
| 已知 URL | `web_fetch` source acquisition -> `DocumentConversionService` Markdown artifact -> source ledger；JS/blocked case 再走 Jina/Crawl4AI/Firecrawl provider fallback |
| 普通公开资料 | `web_search` -> `web_fetch` -> Markdown artifact/source ledger |
| 高召回研究 | Exa category/search type 或 Tavily topic/search_depth -> reader/crawler -> Markdown artifact -> deep research/统一 ledger |
| 金融市场/财报/filing | finance provider registry -> official/API source first -> web fallback |
| 科研/专利/政府监管 | official API first -> web fallback |
| 社媒 | official API / approved provider first -> web fallback only for public pages and low-risk cases |
| 需要登录/交互 | 非目标；云端 Hive 不做登录态网页自动化，要求用户改用官方 API、授权 connector 或人工上传资料 |

### 3.3 Provider Registry

建议新增 `backend/app/data_sources/`，但它只能做 provider catalog / source ledger / routing facade，不能重写已有 `web_pack` 执行路径：

```
backend/app/data_sources/
├── registry.py
├── schemas.py
├── router.py
├── source_ledger.py
├── extractors/
│   ├── html_text.py          # compatibility wrapper around existing web_fetch cleaner
│   ├── document_conversion_bridge.py  # delegates fetched/vetted sources to DocumentConversionService
│   ├── llm_structured.py
│   └── scrapegraph_provider.py
├── providers/
│   ├── web_pack_provider.py  # delegates to current web_search/web_fetch/firecrawl/xcrawl
│   ├── jina_reader_provider.py
│   ├── crawl4ai_provider.py
│   ├── custom_api_provider.py
│   ├── finance/
│   ├── research/
│   └── social/
└── tools.py
```

### 3.4 Tool 面

不要给 LLM 暴露几十个 provider tool，也不要立即用一批 `data_source_*` 工具替换现有 `web_search` / `web_fetch`。第一阶段应保持现有 tool surface，新增数据源能力主要走内部 router 和领域工具：

| Tool | 用途 |
|------|------|
| `web_search` | 继续作为基础公开网页搜索入口 |
| `web_fetch` | 继续作为已知 URL source acquisition 入口，增强 cleaner/ledger，并把可转换内容交给 `DocumentConversionService` |
| `exa_search` / `tavily_search` | 继续作为高级搜索升级工具 |
| `firecrawl_fetch` / `xcrawl_scrape` | 继续作为 crawler-backed fallback |
| `custom_api__*` | 继续作为用户自定义 API tool，后续由 provider template 生成 |
| `data_source_get_ledger(...)` | 可新增，只负责回放统一证据台账 |

领域工具可以保留粗粒度：

- `finance_resolve_entity`
- `finance_get_price_history`
- `finance_search_filings`
- `finance_get_filing`
- `research_search_papers`
- `research_search_patents`
- `regulatory_search_notices`
- `social_search_posts`
- `social_get_profile`

原则：新增数据源优先加 provider adapter / provider template，不新增 LLM tool；确实需要领域能力时再加粗粒度工具。

## 4. 数据源分层

### 4.1 P0

1. Web context
   - Existing `web_search` / `web_fetch` / Firecrawl / XCrawl chain
   - Trafilatura enhancement inside `web_fetch`
   - `DocumentConversionService` / MarkItDown handoff for canonical Markdown artifacts
   - Jina Reader optional
   - Crawl4AI self-host optional
   - Firecrawl hosted optional provider

2. Finance
   - SEC official APIs / EDGAR
   - CFTC / FRED / CBOE official/public
   - FMP / Finnhub / Polygon/Massive / Twelve Data optional
   - yfinance / AKShare / Tushare / Baostock with version lock and source disclaimer
   - GLEIF / OpenCorporates / Companies House

3. Research / patents / regulatory
   - OpenAlex / Semantic Scholar / Crossref / PubMed / arXiv
   - PatentsView / USPTO / EPO
   - ClinicalTrials.gov / openFDA / Regulations.gov

4. Social/public signal
   - Hacker News official API
   - X official API
   - YouTube Data API
   - Reddit API
   - Bilibili/Douyin/TikTok official or approved developer APIs where available
   - Xiaohongshu only through official/approved/commercial data provider;不要默认做规避式爬取

### 4.2 P1

- Crawlee worker for larger scheduled crawl.
- ScrapeGraphAI provider for schema extraction.
- Commercial social listening provider adapter, such as Meltwater/Brandwatch/Data365, for customer-owned keys.

## 5. 合规与治理边界

1. API first
   - 金融、科研、监管、社媒优先 official/public API。
   - Web crawl 只作为 API 不存在、资料公开、低风险时的 fallback。

2. Tenant credentials
   - 所有 key 存 `TenantToolConfig` / encrypted tool config。
   - 不允许 LLM 参数传 token/api_key。

3. Source ledger mandatory
   - 每个数字、结论、结构化字段必须能回放到 provider、URL/API endpoint、timestamp、content hash。

4. Source acquisition vs. conversion
   - `web_fetch`、connector、API provider 负责获取可信 source 和治理边界。
   - `DocumentConversionService` 负责 source-to-Markdown artifact。
   - 不允许把用户提供的远程 URL 直接交给 MarkItDown 或其他 permissive converter。

5. License policy
   - AGPL 项目不内嵌到闭源 SaaS 核心。
   - AGPL/商业许可项目只能作为 external service、customer self-hosted provider 或明确许可后的部署项。

6. No login-state browser automation
   - 云端 Hive 不做登录态/交互式浏览器自动化。
   - Playwright 只允许作为 provider 内部公开页面渲染能力，不允许暴露为 agent 可操作浏览器。
   - 需要账号权限的数据必须走官方 API、approved provider、MCP/connector 授权，或由用户上传资料。

## 6. 改造步骤

### Step 1: 当前能力锁定和 registry seed

- 用测试锁定当前 `web_pack` 行为：SearXNG/DuckDuckGo fallback、PDF extract、JS shell fallback、Firecrawl/XCrawl fallback、Feishu OpenAPI wrong-tool guard。
- 新增 `data_sources` registry schema。
- 把现有 `web_pack` 登记为第一批 provider，不新增并行抓取实现。
- 把 API/finance/research/social provider 做成静态 seed catalog。
- 不改现有 `web_search`/`web_fetch` 主路径。

### Step 2: Source ledger 最小闭环

- 在现有 `web_fetch`、`firecrawl_fetch`、`xcrawl_scrape`、custom API test/run 外围写 `SourceArtifact`。
- 对可转为 agent-readable prose 的 source，记录 `markdown_artifact_path`、`content_hash`、conversion engine 和 source acquisition provider。
- Deep Research 可以读取统一 ledger，而不是只用自己的局部 ledger。

### Step 3: Web context provider adapter

- `web_pack_provider` 只代理当前工具链。
- `web_fetch` 抓取成功后，把 HTML/PDF/Office bytes 或 provider markdown 作为 vetted local artifact 交给 `DocumentConversionService`，不让 MarkItDown 自己抓远程 URL。
- `DocumentConversionService` 可在 MarkItDown 不可用或输出为空时使用 `trafilatura` 作为 HTML fallback。
- 可选接 Jina Reader / Crawl4AI，但它们是 fallback provider，不替代 `web_fetch`。
- Firecrawl 继续作为 optional hosted provider，不内嵌 AGPL runtime。

### Step 4: Data API catalog

- 在公司后台把 Custom API 从“手工填 endpoint”升级成“Provider Template + Operation”。
- 用户选择 provider、填 key、启用 operation。
- 特殊 API 仍允许 custom operation，但必须绑定 category、schema、source ledger。

### Step 5: 领域 pack

- `finance_data_pack`
- `research_data_pack`
- `social_signal_pack`
- 每个 pack 包含 provider templates、tool routing hints、source-quality rubric、evals。

### Step 6: 评测

每类 provider 都必须有 eval：

- 已知 URL 提取正文。
- JS-heavy 页面 fallback。
- API key server-side injection。
- 金融数字 source ledger。
- paper/patent/regulatory search 可回放。
- social API 失败时不能自动切到违规爬取。

## 7. 推荐优先级

P0 应先做：

1. 锁定并复用当前 `web_pack` 能力。
2. `data_sources` registry + source ledger schema。
3. `web_fetch` -> `DocumentConversionService` Markdown artifact handoff。
4. `custom_api` provider template 化。
5. `DocumentConversionService` 负责 HTML/PDF/Office source-to-Markdown；MarkItDown 优先，`trafilatura`/HTML parser 只作 legacy fallback。
6. Finance/research/social 三个 catalog seed，不先做所有 provider 的真实实现。
7. Jina Reader / Crawl4AI 只作为 optional provider，不作为 P0 blocker。

P1 再做：

1. Crawlee worker。
2. ScrapeGraphAI structured extractor。
3. 商业 social listening provider。

## 8. 最小验收标准

1. Agent 对同一个问题能选择 API、web search、known URL、social provider 的正确路径。
2. 所有外部数据都有 `source_id`。
3. Known URL fetch 产生 source ledger entry，并在可转换时产生 Markdown artifact path。
4. 任一报告里的关键数字能回放 provider、URL/API endpoint、retrieved_at、content_hash。
5. 缺 key 时返回可操作错误，不让 agent 猜或把 key 放进参数。
6. 社媒源不做默认规避式爬取。
7. AGPL provider 不被打进 Hive SaaS runtime 镜像核心。

## 9. 参考来源

- Firecrawl: https://github.com/firecrawl/firecrawl
- Firecrawl MCP Server: https://github.com/firecrawl/firecrawl-mcp-server
- Crawl4AI: https://github.com/unclecode/crawl4ai
- Crawlee: https://github.com/apify/crawlee
- ScrapeGraphAI: https://github.com/ScrapeGraphAI/Scrapegraph-ai
- Jina Reader: https://github.com/jina-ai/reader
- Trafilatura: https://github.com/adbar/trafilatura
