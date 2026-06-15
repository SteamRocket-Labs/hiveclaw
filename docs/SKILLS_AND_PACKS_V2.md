# Skills & Capability Packs v2 — 设计提案

| 字段 | 内容 |
|------|------|
| **状态** | Proposal / Implementation Checkpoint v0.5 |
| **日期** | 2026-05-02 |
| **作者** | Hive Engineering |
| **范围** | Skill 系统重构 + Capability Pack catalog 升级 + 三个内容能力包（deep-research / finance / office）+ finance data layer + finance analysis layer |
| **依赖** | `agentskills.io` 开放标准（**兼容**而非全盘采纳） |
| **预计工期** | 10–14 周（6 个 v1 stage + 1 个推迟的 runtime 迁移 stage） |

---

## Revision Log

| 版本 | 日期 | 主要变更 |
|------|------|---------|
| v0.1 | 2026-05-02 | 初稿。基于 4 份外部研究 fork 报告。被实证反馈指出 6 处错估 |
| **v0.2** | 2026-05-02 | **基于代码实证订正**。详见下方 6 处订正 |
| **v0.3** | 2026-05-02 | **决策收敛版**：拆分 office stage、frontmatter 改 flat 示例、MCP credential 改多租户 secret、finance 改 data-first、补验收标准和微服务边界 |
| **v0.4** | 2026-05-02 | **finance 分析层补全版**：基于 FinceptTerminal 当前分析模块，把数据之上的二级研究、一级尽调、投行模型、组合风险、宏观/地缘、量化实验分层落到 Hive workflow / 子 agent / 可复算模型 |
| **v0.5** | 2026-05-02 | **实施检查点**：已落地 Parser v2、skill resources API、依赖 skill 加载、pack.yaml catalog reader、三包 manifest、office package skeleton、finance data/source ledger、finance analysis DCF/workflow skeleton |

### v0.5 已落地范围

| Stage | 状态 | 已落地文件/能力 |
|-------|------|----------------|
| Stage 1 Parser v2 | 已完成基础实现 | `backend/app/skills/parser.py` 改 PyYAML tolerant parser；`types.py` 补 v2 metadata；`loader.py` 补 `list_resources()` / `read_resource()`；`registry.py` 补 dependency body loading |
| Stage 2 Pack manifest catalog | 已完成 catalog 旁路 | `backend/app/packs/catalog_reader.py`；根目录 `packs/deep_research_pack` / `packs/finance_pack` / `packs/office_pack`；`get_pack_catalog()` 展示 manifest pack，runtime 仍由 `@tool(ToolMeta.pack)` 决定 |
| Stage 3a Office package skeleton | 已完成目录契约 | docx/xlsx/pptx/pdf 增加 `references/` `templates/` `evals/`；新增 `weekly-report-generator` / `meeting-minutes` / `pitch-deck-generator` SOP package |
| Stage 5 Finance foundation skeleton | 已完成可测试骨架 | `backend/app/finance_data/schemas.py` 提供 entity/source ledger；`backend/app/finance_analysis` 提供 research packet、DCF calculator、IC memo artifact、workflow spec |

未在 v0.5 内宣称完成：真实外部金融 connector、OAuth 外部账户工具、deep-research 工具 handler、finance tool handler 注册、Stage 6 runtime pack 迁移。

### v0.1 → v0.2 订正记录

| # | v0.1 错估 | v0.2 订正 | 证据 |
|---|----------|----------|------|
| 1 | "现有 skill 全部是单 SKILL.md" | pdf-generator 已有 `scripts/`；skill-creator 由 `services/skill_creator_content.py` 动态生成；docx/xlsx/pptx/find-skills/skill-vetter 5 个仍是单文件 | `ls backend/app/templates/skills/pdf-generator/` 实测 |
| 2 | "Stage 0 是低风险纯重构" | Stage 0 是**中风险**。collector 用 `@tool(ToolMeta(... pack=...))` 收集，runtime 改造牵动 collector / pack_service / runtime/invoker / capability_gate / tool_seeder / UI / 测试。**runtime 改造推迟到 Stage 6 单独立项**，前 5 个 stage 不动收集机制 | `backend/app/tools/collector.py:124-142` |
| 3 | "deep-research 必须先做，finance/office 不能平行" | **协议先定，实施可并行**。deep-research SOP 协议先定，但 office 文档 package 化和 finance 数据 adapter 完全可平行打底 | 业务依赖分析：office 生成 PPT 不依赖 5-phase；finance 数据拉取不依赖 orchestrator |
| 4 | 工具命名用 dotted name（`finance.get_price_history`） | **全部改 underscore**：`finance_get_price_history` / `office_read_docx`。OpenAI function name 规范是 `[a-zA-Z0-9_-]`，dotted name 进 LLM API 会报错 | `backend/app/tools/collector.py:54-63` 直接把 `meta.name` 塞进 OpenAI function schema |
| 5 | "`metadata.hive.version` 强制 semver" | **全部 optional + tolerant 解析**。strict YAML validator 跨平台风险真实，硬约束应放 pack.yaml / DB，不放 frontmatter | agentskills.io spec 公开 spec 偏简单 key-value |
| 6 | Office pack 先做 ~15 个 atomic tool | **先 package 化现有 4 个 skill，不堆 atomic tool**。`pyproject.toml:28-41` 已装 pdfplumber / python-docx / openpyxl / python-pptx / reportlab / pypdf / xlsxwriter，核心库不缺。Anthropic 范式本身是"教 LLM 写代码"，堆 atomic tool 反范式。外部账户工具（gmail/outlook/imap）单独做 | `backend/pyproject.toml:28-41` 实测 |

### v0.2 → v0.3 决策记录

| # | 决策 | v0.3 写法 | 依据 |
|---|------|----------|------|
| 1 | Stage 3 拆分 | `3a office package 化` 与 `3b office external accounts` 分开交付 | 文档 package 能快速交付；OAuth / 外部账户 / 回调不应拖住 office 基础能力 |
| 2 | frontmatter 示例 | 对外示例使用 `metadata` 下的 flat string keys（如 `hive.version`），parser 兼容 nested fallback | agentskills.io 的 `metadata` 是 key-value mapping；flat 更容易跨 client |
| 3 | MCP credential | `pack.yaml` 只声明 `credential_requirements`；真实密钥来自 tenant-scoped encrypted tool config，不使用平台全局 env | Hive 是多租户平台，不能把客户凭证写到全局环境变量 |
| 4 | 付费数据源 | 默认只启用公开/免费源；付费源走后台配置，按 tenant / org 自带 key 或商业合同启用 | 与现有 web_search tool config 思路一致 |
| 5 | 验收标准 | 每个 pack 必须有 eval：citation、source attribution、文件可渲染、金融数字可追溯 | 能力包必须可验证，不只看 prompt 叙事 |
| 6 | finance 方向 | **数据第一**：覆盖美股 / 港股 / A 股的一二级数据源；workflow / 子 agent 在数据层之后 | 金融机构客户首先看数据覆盖、来源可信和可追溯 |
| 7 | OpenBB / Fincept 边界 | OpenBB / Fincept 只借鉴架构与 provider 思路；OpenBB MCP 不作为 SaaS 默认核心依赖 | OpenBB Platform / MCP 为 AGPL，闭源 SaaS 默认绑定风险高 |
| 8 | finance 微服务 | 允许把 finance data layer 拆成独立 `finance-data-service`，但先以清晰接口落地 | 数据依赖重、credential 多、license/合规边界独立，适合服务化 |

### v0.3 → v0.4 分析层订正

| # | 订正 | v0.4 写法 | 依据 |
|---|------|----------|------|
| 1 | finance 不能只做数据工具 | 新增 `finance_analysis` 分层：可复算模型 + workflow 状态 + 报告/模型交付物 | FinceptTerminal 的 `Analytics/README.md` 显示它的核心价值在 CFA 分析模块，不只是 data fetcher |
| 2 | 不把每个分析函数都暴露成 LLM tool | 工具仍控制在 12 个以内；DCF、comps、research packet 是粗粒度入口，细分计算放 analysis engine | FinceptTerminal 有 80+ analytics modules，逐个 tool 化会污染 prompt surface |
| 3 | 二级/一级/组合/宏观/量化要分层 | P0 先做报告级、非交易能力；P1 做组合风险和投行扩展；实盘交易/HFT/RL 延后 | Hive 是多租户云端平台，客户首先要可信研究交付，不是自动下单 |
| 4 | subagent 不是泛化“多 agent” | 每个子 agent 绑定一类金融证据和产物：filing reader、statement analyst、valuation、risk、IC memo writer | FinceptTerminal 的 agent/persona 很多，但 Hive 应产品化为可审计 workflow |

---

## 0. 执行摘要

业界已经收敛到一个开放 skill 标准（[agentskills.io](https://agentskills.io)，30+ 平台采用），Hive 应当**兼容**而不是**全盘采纳**：

- frontmatter 字段全部 **optional + tolerant**，跨平台 skill 可加载
- 强约束（版本 / 依赖 / 治理）放 `pack.yaml` 和 DB
- 现有 `@tool(ToolMeta(... pack=...))` 收集机制是 runtime 真相源，pack.yaml 是 catalog 旁路读取层

Hive 当前 skill 系统在文件级看似不薄（pdf-generator 已有 scripts/，每个 SKILL.md 100-200 行带语义标签），但有四个真实债务：

1. **Parser 是手写正则**，只识别 5 个字段，挡死 spec 扩展
2. **业务覆盖薄**：办公型只有 docx/xlsx/pptx/pdf 4 个生成器（且其中 3 个仍是单 SKILL.md），行业型为零
3. **缺 `references/` `templates/` `evals/` 子目录生态**（除 pdf-generator 外）
4. **Pack 是代码硬编码**（短期 catalog 旁路 OK，长期需要 manifest）

升级路径：

| Stage | 内容 | 周期 | 风险 |
|-------|------|------|------|
| 1. Parser v2 + 宽容解析 | PyYAML 替正则，desc 200→1024，解析 flat `metadata["hive.*"]` + nested fallback，全 optional | 1 周 | 低 |
| 2. Pack manifest catalog 旁路 | `packs/*/pack.yaml` 作为元数据读取层；runtime 仍走 @tool decorator | 1 周 | 低 |
| 3a. Office package 化 | 现有 4 个 skill 升级为完整 package + 3 个新 SOP | 1-2 周 | 低-中 |
| 3b. Office external accounts | gmail/outlook/imap/gcal/onedrive/gdrive 等外部账户工具 | 1-2 周 | 中 |
| 4. Deep-research pack | 5-phase SOP + 8-10 高价值工具 + 独立 citation agent | 2-3 周 | 中 |
| 5a. Finance data foundation | A 股 / 港股 / 美股一二级 connector + entity master + source ledger；OpenBB 仅可选外接 | 3-4 周 | 高 |
| 5b. Finance analysis workflows | 二级深度、一级尽调、IPO pipeline、估值模型、组合风险 review；workflow + 特定子 agent + 可复算模型 | 2-3 周 | 中-高 |
| **6. Pack runtime 迁移**（**单独立项**） | `@tool decorator + ToolMeta.pack` → `pack.yaml` 完全切换。牵动 collector / capability_gate / tool_seeder / UI / 测试 | 后续 | 高 |

**总周期**：v1（Stage 1-5b 完成）10-14 周。Stage 6 是清理债务，独立排期。

**关键架构决策**：
- v1 阶段 **runtime 不动**，pack.yaml 是 catalog 旁路而非真相源
- 三包并行：协议先定，实施可同步
- 工具命名全 underscore（OpenAI function name 兼容）
- frontmatter 兼容多平台（optional + tolerant），强约束在 pack.yaml/DB
- finance pack 的第一优先级是数据源覆盖、实体对齐和数字可追溯；workflow / 子 agent 在数据底座之后
- finance 的第二优先级是“数据之上的可交付分析”：二级研究、一级尽调、投行模型、组合风险、宏观/地缘、量化实验；不要把每个计算函数都做成 LLM tool
- 付费数据源和 MCP 凭证全部 tenant-scoped，不使用平台全局 env

---

## 1. 现状诊断（基于后端实测）

### 1.1 Skill 系统当前形态

| 维度 | 现状 | 实测 |
|------|------|------|
| Skill 文件本身 | 质量很高 | 每个 SKILL.md 100–200 行，有 `<role>` `<when_to_use>` `<workflows>` `<examples>` `<anti_patterns>` `<success_criteria>`。比 Anthropic 公开的 docx/xlsx skill 还严谨 |
| Skill 数量 | 6 个内置 + 1 个动态 | docx-generator / xlsx-processor / pptx-generator / pdf-generator / find-skills / skill-vetter（meta）+ skill-creator（动态生成于 `services/skill_creator_content.py`）|
| **业务覆盖** | **薄**：办公型仅 4 个生成器，行业型为零 | 完全没覆盖：会议纪要、周报、邮件起草、合同审查、行业研究、估值建模、一级尽调、财报会跟踪…… |
| Skill 文件夹生态 | **不均匀**：pdf-generator 已有 `scripts/`；其它 5 个仍是单 SKILL.md | docx/xlsx/pptx/find-skills/skill-vetter 都缺 `references/` `templates/` `evals/` |
| Frontmatter parser（`backend/app/skills/parser.py`） | 手写正则，只识别 5 字段（`name` / `description` / `tools` / `packs` / `is_system`） | SKILL.md 里写的 `license:` `metadata.version:` `metadata.category:` 全部被解析器丢弃；`description` 强制截断到 200 字符 |
| Skill loader（`backend/app/skills/loader.py`） | **已经支持文件夹结构** | 能读 `skills/foo/SKILL.md`，但 parser 跟不上 spec 扩展，子目录（`scripts/` `references/` `assets/`）没有显式加载语义 |
| Catalog 渲染（`backend/app/skills/registry.py`） | 三级 budget-aware 降级 | 与 Claude Code 对齐，这部分挺好 |

### 1.2 Tool Pack 当前形态

`backend/app/tools/packs.py` 是 5 个 Python `frozen dataclass`：

```python
TOOL_PACKS: tuple[ToolPackSpec, ...] = (
    ToolPackSpec(name="web_pack", ...),
    ToolPackSpec(name="feishu_pack", ...),
    ToolPackSpec(name="plaza_pack", ...),
    ToolPackSpec(name="email_pack", ...),
    ToolPackSpec(name="mcp_admin_pack", ...),
)
```

**关键事实**：runtime 真正的 pack 归属来自 `@tool(ToolMeta(... pack=...))` 装饰器，由 `backend/app/tools/collector.py:124-142` 在 `collect_tools()` 里收集成 `pack_tool_groups`：

```python
# collector.py:140-142
if meta.pack and is_canonical:
    pack_groups.setdefault(meta.pack, []).append(name)
```

**含义**：要把 pack 从代码硬编码改成 `pack.yaml` 是大工程，会牵动：
- `backend/app/tools/collector.py`（收集逻辑）
- `backend/app/services/pack_service.py`（生命周期）
- `backend/app/runtime/invoker.py`（pack 激活）
- `backend/app/tools/governance.py` + `backend/app/services/capability_gate.py`
- `backend/app/services/tool_seeder.py`（DB 注入）
- 管理 UI（`frontend/src/api/domains/`）
- 全部 pack 相关测试

**v0.2 的关键决策**：v1 阶段 **runtime 不动**，pack.yaml 仅作为 catalog 旁路读取层（UI 展示、客户分发、文档导出用），运行时 pack 归属仍走装饰器。runtime 迁移单独立项（Stage 6）。

### 1.3 用户对"4 个 skill 太薄"的真实诊断

经过实证，"薄"指三件事：

1. **数量薄**：业务覆盖只有"生成文档"4 类，行业型为零
2. **形态不均**：pdf-generator 已经有 `scripts/`，但 docx/xlsx/pptx 还是单文件；缺 `references/` `templates/` `evals/`
3. **架构薄**：parser 5 字段、pack 代码硬编码 → spec 升级被基础设施挡死

---

## 2. 行业标准研究

### 2.1 Skill 标准已收敛到 agentskills.io

[agentskills.io](https://agentskills.io) 是 Anthropic 起草的开放标准，已被 30+ 平台采纳，包括：

> Claude / Claude Code / Gemini CLI / OpenAI Codex / OpenCode / Cursor / GitHub Copilot / VS Code / Goose / OpenHands / Letta / Roo Code / Spring AI / Snowflake Cortex / Databricks Genie ……

来源：[agentskills.io overview](https://agentskills.io)

**核心 schema 极简**：

```yaml
---
# 必填
name: pdf-processing            # 1-64 字符，小写+数字+连字符
description: Extract PDF text, fill forms. Use when handling PDFs.  # 1-1024 字符

# 可选
license: Apache-2.0
compatibility: Requires Python 3.10+
allowed-tools: Bash(python:*) Read Write
metadata:                       # freeform key-value
  author: example-org
  version: "1.0"
---
```

来源：[agentskills.io specification](https://agentskills.io/specification)

**文件夹结构**：

```
skill-name/
├── SKILL.md          # 必需，<500 行 / <5000 tokens
├── scripts/          # 可执行代码 agent 直接跑
├── references/       # 按需加载的参考文档
└── assets/           # 模板、图片、查找表
```

**渐进披露三级**：~100 tokens 的 metadata 全部 skill 都常驻 → 完整 SKILL.md 在激活时加载 → `scripts/` `references/` `assets/` 仅当 SKILL.md 指引 agent 读时才加载。

**Hive 的兼容策略**：采纳目录结构与渐进披露，但 frontmatter 字段处理**优先兼容性**：

- 标准字段（`name` `description` `license` `compatibility` `allowed-tools` `metadata`）：原样支持
- Hive 私有字段（flat `metadata["hive.*"]`，兼容 nested `metadata.hive.*` fallback）：写入 + 读取，但**全部 optional**，**缺失不报错**
- 真正的强约束（版本管理、依赖关系、治理策略）：在 `pack.yaml` 和 DB 里强制

这样跨 Claude Code / OpenCode / Gemini CLI 加载 Hive skill 不会因 strict validator 报错。

### 2.2 Deep Research SOTA 已收敛

Anthropic / OpenAI / Gemini / Perplexity / xAI 五个商业产品 + 10 个开源项目，**所有 SOTA 都用同一个拓扑**：

> **Plan → Execute → Synthesize**，其中 Execute 阶段是 **orchestrator + 并行 subagent**，Synthesize 之后是**独立的 citation 后处理 agent**。

| 关键发现 | 数据 | 来源 |
|---------|------|------|
| 并行 subagent vs 单 agent 长循环 | **90% 时间节省** | [Anthropic engineering](https://www.anthropic.com/engineering/multi-agent-research-system) |
| Code-action vs JSON tool calls | GAIA 上 **55.15% vs 33%**（+22 pp）| [HF Open Deep Research](https://huggingface.co/blog/open-deep-research) |
| Inline citation 幻觉率 | **3-13% URL 幻觉，5-18% 不可解析** | [PIES 论文 arXiv 2601.22984](https://arxiv.org/abs/2601.22984) |
| Outline-first vs 纯 iterative | STORM / Anthropic / 大多数 OSS 用 outline-first | [STORM](https://github.com/stanford-oval/storm) |

**关键洞察**：Hive 现有 `delegate_to_agent` + 4 层记忆 + kernel **已经实现 Anthropic blueprint 的 artifact pattern**：

> "subagents can create outputs that persist independently...rather than requiring subagents to communicate everything through the lead agent" — [Anthropic engineering](https://www.anthropic.com/engineering/multi-agent-research-system)

不需要重写架构。

**三大反模式**：
1. ❌ **Inline-while-writing 引用**（用专门 CitationAgent 做后处理）
2. ❌ **单 agent 跑 200 round**（用 orchestrator + workers）
3. ❌ **跳过 Phase 0 澄清**（OpenAI/Anthropic 都强制澄清意图）

### 2.3 Finance — 数据第一，不把 OpenBB 作为默认底座

**7 个开源金融 agent 项目**调研结果：

| 项目 | 价值 |
|------|------|
| [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | **Persona pipeline**：19 投资人格 → 4 分析 agent → Risk → PM |
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | **两步报告**：先编译结构化数据，后写叙事 |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | provider / schema / tool discovery 设计值得借鉴；**AGPL，不能作为闭源 SaaS 默认核心依赖** |
| [FinanceToolkit](https://github.com/JerBouma/FinanceToolkit) | 150+ 估值/风险计算 |
| [edgartools](https://github.com/dgunning/edgartools) | SEC filings + XBRL native + 自带 MCP，零成本 |

**关键决策**：finance pack 不应先做一堆分析 prompt，也不应默认绑定 OpenBB MCP。正确顺序是：

1. **数据源 connector + entity master + source ledger 先行**
2. 二级分析和一级尽调 workflow 复用这套数据底座
3. OpenBB / Fincept 只借鉴 provider 架构和 workflow 思路；如客户自带 OpenBB 商业许可或自托管环境，可作为 optional external MCP

**云端数据栈**（按市场 × 一级/二级拆分）：

| 市场 | 二级数据（行情 / 财报 / 估值） | 一级数据（IPO / 融资 / M&A / 工商） | 付费扩展 |
|------|-----------------------------|----------------------------------|----------|
| **美股** | `yfinance`、SEC 10-K/10-Q、FRED / CBOE / CFTC 公开源 | SEC EDGAR S-1 / F-1 / 424B / Form D / 8-K；`edgartools` 优先 | Crunchbase / PrivCo / PitchBook / Capital IQ / Mergermarket |
| **港股** | `yfinance` + `akshare` 兜底；后续可接 EODHD / HKEX market data | HKEX 披露易、Application Proof、PHIP、聆讯后资料集、公告 | Wind 港股 / Bloomberg ECM / Mergermarket |
| **A 股** | `akshare`（版本锁定）、Tushare / Baostock 兜底、巨潮财报公告 | 巨潮招股书 / 定增 / 重组、证监会发行监管、上交所 / 深交所 / 北交所 IPO 状态、企查查/天眼查 API | Wind / iFinD / Choice / 清科私募通 / 投中 CVSource / IT 桔子 |
| **合规/主数据** | GLEIF、OpenCorporates、OFAC / EU / UN sanctions | 工商注册号、LEI、CIK、上市代码、外部源 ID 映射 | World-Check / Dow Jones / 商业 KYC |
| **分析计算** | FinanceToolkit、statsmodels、quantstats、内部估值函数 | Cap table、PME、precedent transactions、IPO pipeline 状态机 | 客户自带模型 / 内部 Excel 模板 |

**故意推迟到 EE/desktop**：Wind / Bloomberg / Choice / iFinD（终端依赖，云端无 API）。

**FinceptTerminal research 结论迁移**：Fincept 在二级市场数据源很多，但一级市场几乎空白；Hive 的差异化不应是再做一个行情终端，而是做**实体驱动 + 关系驱动**的数据层：Company / People / Fund / LP / Deal 统一到 Entity Master，再服务 DD、IC memo、IPO pipeline 和二级深度研究。

### 2.4 Office — Anthropic 范式：skill 驱动 code_exec

**关键发现**：Anthropic 的 [docx/pptx/xlsx/pdf skills](https://github.com/anthropics/skills) **不是工具堆，而是 playbook 教 LLM 写 Python 跑库**：

> "SKILL.md is a playbook, not a tool wrapper. It tells Claude 'do X using library Y, here are the gotchas, run this script when done'. The actual work is done by Claude writing Python code that imports the library, executed via shell tools."

**例子**（Anthropic pptx skill 的 killer pattern）：

> 多轮 QA loop：generate → render to PNG → "hunt for issues (overlapping elements, text overflow, contrast, alignment gaps)" → fix → re-verify
> "Assume there are problems. Your job is to find them. Fresh eyes via subagents catch what creators miss."

来源：[anthropics/skills/pptx/SKILL.md](https://github.com/anthropics/skills/blob/main/skills/pptx/SKILL.md)

**Hive 已具备的基础**（实测 `backend/pyproject.toml:28-41`）：

```
pdfplumber>=0.11.0
python-docx>=1.1.0
openpyxl>=3.1.0
python-pptx>=1.0.0
reportlab>=4.0.0
pypdf>=4.0.0
xlsxwriter>=3.x
```

**所以 office pack 第一性任务不是堆 atomic tool**，而是：
1. 把现有 4 个生成器 skill 升级为完整 package（带 `references/` `scripts/` `templates/` `evals/`）
2. 单独做**外部账户工具**（gmail / outlook / imap / gcal），这些是真的缺，且需要 governance

---

## 3. Skill Spec v2 — 完整字段定义

### 3.1 两层架构（关键拆分）

当前 Hive 把 skill 和 pack 混在一起，必须拆开：

| 层 | 概念 | 格式 | 范畴 | 强约束位置 |
|----|------|------|------|----------|
| **Skill** | 给 agent 读的 SOP | `SKILL.md`（agentskills.io 兼容） | 程序性知识 / 一段端到端 workflow | 文件夹形状（loader 校验） |
| **Capability Pack** | 代码级工具 + 技能 + MCP server 的打包 | `pack.yaml`（catalog 旁路） + `@tool` 装饰器（runtime 真相源） | 一个领域的能力交付 | DB 字段 + pack.yaml schema |

### 3.2 SKILL.md frontmatter schema — **全部 optional + tolerant**

**Standard 字段**（直接采纳 agentskills.io）：

| 字段 | 类型 | 必需 | 约束 |
|------|------|------|------|
| `name` | string | **是** | 1–64 字符，`[a-z0-9-]+`，无前后/连续连字符，**必须匹配父目录名** |
| `description` | string | **是** | 1–1024 字符，第三人称，描述"做什么 + 何时用" |
| `license` | string | 否 | 许可证名或路径 |
| `compatibility` | string | 否 | ≤500 字符，环境要求说明 |
| `allowed-tools` | string | 否 | 空格分隔，咨询性，**不是授权**，实际授权走 `governance_resolver` |
| `metadata` | mapping | 否 | freeform key-value |

**Hive 私有字段**（公开示例使用 `metadata` 下的 flat string keys，parser 兼容 nested fallback，**全部 optional**）：

| 字段 | 类型 | 必需 | 用途 | 缺失时 |
|------|------|------|------|--------|
| `metadata["hive.version"]` | semver string | 否 | `"1.0.0"`，UI 展示和 marketplace 用 | parser 不报错；DB 默认 `0.0.0`；强约束在发布流水线里 |
| `metadata["hive.pack"]` | string | 否 | 引用哪个 pack 提供工具（hint） | parser 不报错；runtime 真相源仍是 `@tool` decorator |
| `metadata["hive.requires_skills"]` | comma string | 否 | skill→skill 依赖 | parser 不报错；激活时尝试加载，缺则 warn |
| `metadata["hive.locale"]` | enum | 否 | `cloud` \| `desktop` \| `hybrid`。默认 `cloud` | 缺则视为 cloud |
| `metadata["hive.invocation"]` | enum | 否 | `auto` \| `manual` \| `both`。默认 `both` | 缺则视为 both |
| `metadata["hive.persona_lock"]` | bool string | 否 | true 时 skill 临时压过 `soul.md` | 缺则视为 false |
| `metadata["hive.cost_tier"]` | enum | 否 | `low` \| `mid` \| `high` → quota_guard 启发 | 缺则不传 hint |
| `metadata["hive.estimated_runtime_minutes"]` | number string | 否 | UI hint | 缺则不显示 |
| `metadata["hive.output_artifacts"]` | comma string | 否 | glob 模式，workspace 注册产物 | 缺则不注册 |
| `metadata["hive.author"]` | string | 否 | — | — |
| `metadata["hive.security_zone"]` | enum | 否 | `public` \| `restricted`。可比 agent 默认更严 | 缺则继承 agent 默认 |

**为什么全部 optional**：
- 跨平台兼容性：strict YAML validator（Claude Code / Gemini CLI 部分版本）对深嵌套和未知字段挑刺
- 真正的强约束放 `pack.yaml` 和 DB 字段（pack 必须有 version；skill 在 marketplace 上架时由发布流水线校验）
- frontmatter 是 hint 层，不是 source of truth

**parser 容错原则**：
- 任何 `metadata["hive.*"]` / nested `metadata.hive.*` 字段缺失或类型错误 → 用默认值 + WARN 日志，不抛异常
- `name` `description` 缺失 → 抛异常（这两个是真的必需）
- 顶级未知字段（如 `themes`、`audience`）→ 保留在 `metadata` dict，不报错

### 3.3 文件夹结构

```
secondary-equity-deep-dive/         # 目录名 = name 字段
├── SKILL.md                        # 必需，≤500 行 / ≤5000 tokens
├── checklist.md                    # 可选，复制即用的清单
├── references/                     # 按需加载（一层深，不嵌套）
│   ├── sell-side-format.md
│   ├── valuation-methods.md
│   └── disclosure-language.md
├── templates/                      # 输出模板（Hive 约定，agentskills.io 标准把这放 assets/）
│   ├── deep-dive-report.md
│   └── valuation-model.xlsx
├── assets/                         # 静态资源
│   ├── industry-tags.json
│   └── *.png
├── scripts/                        # 可执行 Python（agent 通过 code_exec 跑）
│   ├── compute_dcf.py
│   └── render_report.py
└── evals/                          # 评估脚本 + 黄金样本（可选）
    ├── eval.yaml
    └── samples/
```

**约束**：所有 SKILL.md 内的资源引用必须**只下一层**，不嵌套。否则 Claude 会"部分读取" — 来自 [Anthropic best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)。

### 3.4 渐进披露三级

| 级别 | 内容 | 何时进 context |
|------|------|---------------|
| **L1 Catalog** | `name + description`（~100 tokens 每 skill） | 所有 skill 常驻 system prompt |
| **L2 Body** | 完整 SKILL.md（≤5k tokens） | `load_skill` 调用时进入；本会话不重读（与 Claude Code 一致） |
| **L3 Resources** | `references/` `templates/` `assets/` `scripts/` | agent 通过 `read_file` 按需读取 |

### 3.5 完整 SKILL.md 示例

```yaml
---
name: secondary-equity-deep-dive
description: |
  Conduct a sell-side-analyst-grade equity research deep dive on a single listed
  company, covering business model, industry positioning, competitive moat,
  financial-statement analysis, valuation (DCF + comparables), catalysts, and
  risk factors. Output is a structured Markdown report plus an Excel valuation
  model. Use when the user asks for "公司深度报告", "二级研究", "deep dive on
  <ticker>", "equity research report on X", or any sell-side-style equity
  analysis with valuation.
license: Proprietary
compatibility: |
  Requires Hive >= 1.8.0 and finance_pack installed.
allowed-tools: web_search web_fetch read_file write_file
metadata:
  hive.version: "1.0.0"
  hive.pack: finance_pack
  hive.requires_skills: "industry-research,dcf-valuation"
  hive.locale: cloud
  hive.invocation: both
  hive.cost_tier: high
  hive.estimated_runtime_minutes: "30"
  hive.output_artifacts: "reports/{ticker}-deep-dive-{date}.md,reports/{ticker}-valuation-{date}.xlsx"
  hive.author: Hive Finance Research Team
  hive.security_zone: restricted
---

# Secondary Equity Deep-Dive — Sell-Side Analyst Workflow

[完整 SOP body，≤500 行...]
```

**重申**：上面 `metadata["hive.*"]` 全部字段缺失都不会让 parser 报错；缺 `version` 默认 `0.0.0`，缺 `pack` runtime 不会因此拒载。Hive parser 可以兼容 nested `metadata: { hive: ... }`，但对外示例统一 flat keys。

---

## 4. Capability Pack — `pack.yaml` Manifest（catalog 旁路）

### 4.1 pack.yaml 的角色

**v1 阶段**：pack.yaml 是 **catalog 旁路读取层**，不是 runtime 真相源。具体：

| 用途 | 来源 |
|------|------|
| UI 展示 pack 信息 | pack.yaml |
| 客户/合作伙伴分发 | pack.yaml |
| 文档导出 / marketplace | pack.yaml |
| **运行时 pack 归属** | `@tool(ToolMeta(... pack=...))` 装饰器（**不变**） |
| **运行时 governance** | `backend/app/tools/governance.py` + `backend/app/services/capability_gate.py`（**不变**） |
| **运行时工具注册** | `collector.py` 收集 `@tool` 装饰器（**不变**） |

这意味着 v1 的 pack.yaml 主要是**信息层**：版本、描述、SOP 列表、数据源声明、credential requirements、可选 MCP server。不接管 runtime。

### 4.2 完整 pack.yaml 示例

```yaml
# packs/finance_pack/pack.yaml
name: finance_pack
version: "1.0.0"
description: |
  金融研究能力包：一二级投研、估值、财报、监管披露
license: Proprietary
author: Hive Finance Team

# === Tools 声明（catalog 旁路，runtime 真相源仍是 @tool 装饰器） ===
# v1 阶段这部分是文档性的，UI 用来展示 pack 包含哪些工具
tools:
  - name: finance_get_company_overview
    locale: cloud
    governance:
      security_zone: public
  - name: finance_compute_dcf
    locale: cloud
    governance:
      security_zone: public
  - name: finance_search_filings
    locale: cloud
    governance:
      security_zone: public
  - name: finance_wind_terminal_query   # 留给桌面端
    locale: desktop
    governance:
      security_zone: restricted
      requires_credential: wind_license

# === Skills（pack 内部包含的 skill 文件夹路径） ===
skills:
  - skills/secondary-equity-deep-dive
  - skills/dcf-valuation
  - skills/comps-valuation
  - skills/primary-market-due-diligence
  - skills/portfolio-risk-review
  - skills/industry-research
  - skills/primary-market-due-diligence
  - skills/earnings-call-analysis

# === 数据源声明（catalog + governance；真实调用仍由 connector/tool 实现） ===
data_sources:
  public_default:
    - sec_edgar
    - hkexnews
    - cninfo
    - akshare
    - yfinance
    - gleif
    - sanctions_lists
  paid_optional:
    - fmp
    - polygon
    - crunchbase
    - qichacha
    - tianyancha
    - wind
    - pitchbook

# === 可选 MCP server（不作为 SaaS 默认核心依赖） ===
mcp_servers:
  - name: openbb_optional
    enabled_by_default: false
    license_note: AGPL/commercial license required if Hive hosts or modifies OpenBB Platform/MCP
    transport: stdio
    command: openbb-mcp
    args: ["--default-categories", "admin", "--tool-discovery"]
    credential_scope: tenant

  - name: edgartools_optional
    enabled_by_default: false
    transport: stdio
    command: uvx
    args: ["--from", "edgartools[ai]", "edgartools-mcp"]
    credential_scope: tenant

# === 多租户凭证声明 ===
# 注意：这里不允许写平台全局 env。env_name 只是 vendor SDK/MCP 子进程需要的变量名；
# 值必须来自 tenant-scoped encrypted tool config，并在单次调用/子进程启动时临时注入。
credential_requirements:
  - key: fmp_api_key
    display_name: FMP API Key
    scope: tenant
    storage: encrypted_tool_config
    injected_as: per_invocation_env
    env_name: FMP_API_KEY
  - key: edgar_identity
    display_name: SEC EDGAR User-Agent identity
    scope: tenant
    storage: encrypted_tool_config
    injected_as: per_invocation_env
    env_name: EDGAR_IDENTITY

# === Pack 级激活规则 ===
activation:
  required_capabilities: [finance_data_access]
  default_state: inactive

# === 沙箱依赖（office pack 用得多，finance 用得少） ===
sandbox_requirements:
  pip_packages:
    - akshare>=1.18.59,<1.20      # 版本锁定（实测维护频繁）
    - tushare>=1.4.0
    - yfinance>=0.2.40
    - financetoolkit>=2.0
    - edgartools>=5.30
    - stockstats>=0.6
```

**重要**：所有工具名是 underscore 形式（`finance_get_price_history`，不是 `finance.get_price_history`）。原因：`backend/app/tools/collector.py:54-63` 把 `meta.name` 直接塞进 OpenAI function calling schema，OpenAI function name 规范是 `[a-zA-Z0-9_-]`，dotted name 会被 LLM API 拒。Pack 的命名空间分组通过元数据字段表达，不通过工具名前缀。

### 4.3 与现有 packs.py 的关系

**v1 阶段**：

```
runtime（真相源）：
  @tool(ToolMeta(name="finance_get_price_history", pack="finance_pack", ...))
  ↓
  collector.collect_tools() → pack_tool_groups["finance_pack"] = [...]
  ↓
  governance / capability_gate / tool_seeder 用这个

catalog（旁路）：
  packs/finance_pack/pack.yaml  ← UI / 文档 / marketplace 读这个
  ↓
  PackCatalogReader.discover()
```

**Stage 6（迁移，单独立项）**：

```
runtime + catalog 都从 pack.yaml 读：
  packs/finance_pack/pack.yaml （唯一 source of truth）
  ↓
  PackRegistry.discover() → ToolMeta（动态 + 静态混合）
  ↓
  collector / governance / tool_seeder 改造
```

Stage 6 单独立项是因为：collector / pack_service / runtime/invoker / capability_gate / tool_seeder / UI / 测试 都要动，影响面大。v1 阶段把 pack.yaml 作为旁路读取，先把内容能力补上，runtime 改造延后。

---

## 5. 三个能力包的依赖关系（协议先定，实施可并行）

### 5.1 业务依赖矩阵

| 能力包 | 依赖于 | 不依赖 | 协议先定 | 实施可并行 |
|--------|-------|--------|---------|-----------|
| **deep-research** | parser v2、pack catalog | — | 5-phase 协议、citation 后处理 | — |
| **office package** | parser v2、pack catalog | deep-research 协议（office 不做研究 SOP）| 现有 4 个 skill 的 package 化模式 | ✓ 可与 deep-research 并行 |
| **office external accounts** | tenant credential / OAuth / governance | deep-research 协议 | 外部账户工具治理协议 | ✓ 可与 deep-research 并行，但不阻塞 office package |
| **finance data layer** | parser v2、pack catalog、connector 抽象、entity master | deep-research 协议 | 三市场一二级数据源矩阵、tenant credential 规则 | ✓ 可与 deep-research 并行 |
| **finance analysis workflow** | finance data layer；研究 SOP 部分依赖 deep-research 协议；输出部分消费 office 工具 | — | research packet schema、analysis result schema、source ledger、子 agent 分工、deterministic calculator 边界 | 等 deep-research 协议和 finance data layer 定稿 |

**结论**：v0.1 文档说"必须串行"是错的。正确说法是：

- **Stage 1-2（parser v2 + pack catalog）必须先做** — 这是基础设施
- **Stage 3a / 4 / 5a 可重叠**：
  - office package 化 = deep-research 协议无关，**可并行**
  - finance data layer（connector / entity master / source ledger）= deep-research 协议无关，**可并行**
  - office external accounts = 可并行，但 credential / OAuth 风险单独管理
  - finance analysis workflow（二级深度 / 一级尽调 / IPO pipeline / 组合风险）= 等 deep-research 协议和 finance data layer 定稿后做

### 5.2 修订后的依赖图

```
                Stage 1: parser v2
                        ↓
                Stage 2: pack manifest catalog
                        ↓
            ┌───────────┼───────────┐
            ↓           ↓           ↓
    Stage 3a:office Stage 4:    Stage 5a:
    package 化       deep-      finance
    (并行)          research    data layer
                    pack        (并行)
                    协议
                        ↓
    Stage 3b:office external accounts
    (并行，但不阻塞 3a)
                        ↓
                    Stage 5b:
                    finance analysis
                    workflow
                    (依赖 stage 4 协议 + stage 5a 数据层)
```

---

## 6. 三包内容矩阵

### 6.1 deep-research-pack — 8-10 高价值工具（不是 14）

#### 工具清单（cloud locale，underscore 命名）

| 类目 | 工具 | 后端实现 | 状态 |
|------|------|---------|------|
| Search | `web_search` | SearXNG / DuckDuckGo 基础搜索；Exa/Tavily 走 `exa_search` / `tavily_search` deferred provider | ✓ 已有 |
| Search | `academic_search` | Semantic Scholar API + arXiv API | 🆕 新建 |
| Search | `news_search` | Tavily news mode | 🆕 新建 |
| Fetch | `web_fetch` | Firecrawl scrape | ✓ 已有 |
| Fetch | `web_extract` | Firecrawl extract / Exa contents | 🆕 新建 |
| PDF | `read_pdf` | pdfplumber + pypdf | 🆕 新建 |
| Quality | `evaluate_source_credibility` | LLM 启发式 | 🆕 新建 |
| Outline | `write_outline` | filesystem 包装 | 🆕 新建 |
| Delegate | `delegate_research_subtopic` | 包 `delegate_to_agent` | 🆕 新建 |
| Citation | `compile_citations` | **专门后处理 agent**，验证每个 URL | 🆕 新建（关键）|

**总计 10 个**，其中 2 个已有，8 个新建。`web_crawl`、`web_screenshot`、`update_outline_section_status`、`compose_report` 等可选项**不在 v1**，等需求验证。

#### Skill 清单（v1 起步）

| Skill | 用途 | v1 包含 |
|------|------|--------|
| `industry-research` | 行业研究报告 5-phase SOP | ✓ |
| `topic-deep-dive` | 通用主题深度研究 | ✓ |
| `competitor-analysis` | 竞品对比 | 后续 |
| `literature-review` | 学术文献综述 | 后续 |
| `news-analysis` | 新闻事件深度分析 | 后续 |

#### 核心 SOP：`industry-research/SKILL.md`

5 phase（与 SOTA 收敛）：

```
Phase 0 — Clarify intent (5 min)        ← 不澄清不开工
   ↓
Phase 1 — Outline (10 min, sequential)
   - 3-5 broad web_search
   - academic_search 5 篇综述
   - 建 outline → 用户审批
   ↓
Phase 2 — Parallel subtopic research (20-40 min, parallel)
   - 每节 delegate_research_subtopic
   - subagent 独立 30-round 上限
   - 写 findings-{section}.md
   - 上限 5 个并行（Anthropic empirical cap）
   ↓
Phase 3 — Synthesis (10 min, sequential)
   - 主 agent 读所有 findings 去重
   - 冲突时 primary > academic > industry > SEO
   - 写 report-draft.md
   ↓
Phase 4 — Citation pass (5 min, delegated)
   - compile_citations agent 后处理
   - 验证每个 URL 可解析
   - 标记 [CITATION-NEEDED]
   ↓
Phase 5 — QA (5 min)
```

#### 与 4 层记忆的集成

| 层 | 在研究流程中的角色 |
|----|------------------|
| **T0 behavior** | 完整对话 + 工具调用 = 研究 transcript（审计用）|
| **T2 learnings** | 每次 run 的事实抽取，跨 run 累积领域知识 |
| **T3 memory** | 受平台 cadence 的 Heartbeat 与 governed T3 append 沉淀后的稳定先验 → `knowledge.md` `strategies.md` |
| **soul.md** | 研究风格固化（"偏好原始资料、紧凑表格、必含风险段"）|

**关键决策**：不发明独立的 "research notebook"，直接用 T2。

#### 三大反模式

1. ❌ Inline-while-writing 引用（PIES 论文测出 3-13% URL 幻觉率）
2. ❌ 单 agent 跑 200 round（用 orchestrator + workers）
3. ❌ 跳过 Phase 0 澄清

### 6.2 finance-pack — data-first + analysis-layer v1（美股 / 港股 / A 股，一二级覆盖）

#### v1 优先级：数据源整合先于分析工作流

金融客户第一关不是 prompt 漂亮，而是三件事：

1. **数据覆盖**：美股 / 港股 / A 股的一二级资源要可用
2. **数据可信**：每个数字能追溯到 source URL / filing / provider / timestamp
3. **数据治理**：免费源默认可用，付费源由客户在后台配置 tenant/org credential

因此 finance pack v1 先建数据底座：

```
Finance Data Layer
├── connector/          # 每个外部源一个 connector
├── entity_master/      # Company / Person / Fund / LP / Deal 多源 ID 映射
├── source_ledger/      # 每个字段的来源、抓取时间、license / credential scope
├── normalized_schema/  # Filing / Security / Financials / FundingRound / IPOEvent
└── tools/              # LLM 可调用的少量稳定工具
```

#### 三市场 × 一二级数据源矩阵

| 市场 | 二级 P0 | 一级 P0 | 付费/客户自带扩展 |
|------|--------|--------|------------------|
| **美股** | `yfinance`、SEC 10-K/10-Q、13F/Form 4、FRED/CBOE/CFTC | SEC EDGAR S-1/F-1/424B/Form D/8-K；`edgartools` 解析招股书 / Form D / XBRL | Crunchbase、PrivCo、PitchBook、Capital IQ、Mergermarket、Polygon/FMP |
| **港股** | `yfinance` + `akshare` 兜底；EODHD 可选 | HKEX 披露易、Application Proof、PHIP、上市申请进度、公告 PDF | Wind 港股、Bloomberg ECM、Mergermarket、EODHD |
| **A 股** | `akshare`（版本锁）、Tushare/Baostock 兜底、巨潮财报公告 | 巨潮招股书/定增/重组、证监会发行监管、上交所/深交所/北交所 IPO 状态、企查查/天眼查 API | Wind、iFinD、Choice、清科私募通、投中 CVSource、IT 桔子 |
| **横切合规** | GLEIF、OpenCorporates、OFAC/EU/UN sanctions | 工商注册号、LEI、CIK、股票代码、外部源 ID 映射 | World-Check、Dow Jones、客户内部 KYC |

**原则**：OpenBB MCP 不进入默认 P0，因为它是 AGPL/commercial-license 边界；可以作为客户自托管 / 自带商业许可的 optional MCP。

#### v1 工具清单（12 个以内，underscore 命名）

| 类目 | 工具 | 后端 | 优先级 |
|------|------|------|--------|
| Entity | `finance_resolve_entity(query, region)` | entity master + GLEIF/OpenCorporates/交易所 ID | P0 |
| Source | `finance_get_source_ledger(entity_id, field)` | source ledger | P0 |
| 市场数据 | `finance_get_price_history(symbol, market, start, end, freq)` | yfinance / akshare / Tushare / EODHD optional | P0 |
| 财报 | `finance_get_financial_statements(entity_id, market, period)` | edgartools / SEC / cninfo / HKEX filings | P0 |
| 披露 | `finance_search_filings(entity_id, market, form_type, date_range)` | SEC / HKEX / cninfo / 交易所 | P0 |
| 披露 | `finance_get_filing(filing_id, extract_tables)` | edgartools / PDF parser / HTML parser | P0 |
| 一级 | `finance_get_ipo_pipeline(market, status, date_range)` | SEC / HKEX / CSRC / SSE / SZSE / BSE | P0 |
| 一级 | `finance_get_funding_rounds(entity_id, market)` | SEC Form D / IT 桔子 optional / Crunchbase optional | P1 |
| 工商/KYC | `finance_get_company_registry(entity_id, region)` | OpenCorporates / GLEIF / 企查查 optional | P1 |
| 估值 | `finance_compute_dcf(financials, assumptions)` | internal model / FinanceToolkit | P0 |
| 估值 | `finance_build_comps(entity_id, peer_set, metric)` | market data + filings + industry tags | P1 |
| 报告底稿 | `finance_compile_research_packet(entity_id, workflow)` | orchestrator，打包 source ledger + tables | P0 |

**总计 P0=8 个，加 P1 共 12 个**。工具数量不再继续膨胀；新增数据源优先进入 connector，而不是新增 LLM tool。

#### Skill / workflow 清单（v1 起步）

| Skill | 依赖 | v1 包含 |
|------|------|--------|
| `secondary-equity-deep-dive` | deep-research 协议 + finance data layer | ✓ |
| `dcf-valuation` | 估值工具 + source ledger | ✓ |
| `comps-valuation` | peer set + multiples + filings | ✓ |
| `ipo-pipeline-monitor` | SEC / HKEX / CSRC / 交易所 connector | ✓ |
| `primary-market-due-diligence` | entity master + registry/KYC + funding rounds | ✓（轻量版） |
| `ic-memo-generator` | finance research packet + office templates | ✓（模板版） |
| `portfolio-risk-review` | holdings + prices + factor/risk metrics | P1 |
| `mna-deal-analysis` | deal database + filings + valuation models | P1 |
| `macro-regime-brief` | economics + rates + policy + geopolitics sources | P1 |
| `earnings-call-analysis` | 财报 + 新闻 + transcript 源 | 后续 |
| `industry-mapping` | deep-research 协议 + comps | 后续 |
| `alpha-factor-lab` | factor data + backtest engine + model eval | 后续 / gated |

#### 数据搞定之后，finance analysis layer 应该提供什么

FinceptTerminal 的当前形态说明一件事：金融能力的客户价值不在“能查行情”，而在数据之上能稳定产出可复算、可追溯、可交付的分析。Hive 不应该照搬它的桌面终端 UI，也不应该把 80+ analytics module 全变成 LLM tool；应该抽象成一层 `finance_analysis`：

```
finance_data
  -> normalized tables + source ledger + entity master
finance_analysis
  -> models / calculators / workflow state / assumptions / artifacts
finance_workflows
  -> subagents + report writer + reviewer + office export
```

推荐能力矩阵：

| 能力域 | FinceptTerminal 对应能力 | Hive v1/P1 形态 | 交付物 |
|--------|--------------------------|-----------------|--------|
| **二级 equity research** | Equity Research、financial statement analysis、DCF、multiples、technicals、news/sentiment | `secondary-equity-deep-dive` + `dcf-valuation` + `comps-valuation` | Markdown/PDF 深度报告 + Excel 估值模型 + source ledger |
| **一级 / 投行 / PEVC** | M&A analytics、startup valuation、LBO、fairness opinion、deal database、SEC filing scanner | `primary-market-due-diligence` + `ipo-pipeline-monitor` + `mna-deal-analysis` | IC memo、IPO pipeline、deal teardown、LBO/VC method 模型 |
| **组合与风险** | Portfolio analytics、optimization、VaR/CVaR、stress test、factor exposure | `portfolio-risk-review` | 组合风险报告、暴露拆解、再平衡建议、stress scenario |
| **宏观 / 地缘 / 新闻事件** | economics modules、news correlation、instability score、prediction-market overlay、geopolitics agents | `macro-regime-brief` + deep-research | 市场 regime brief、风险地图、行业/供应链影响分析 |
| **量化 / alpha 实验** | AI Quant Lab、factor discovery、backtesting、technical nodes、pairs/regime detection | `alpha-factor-lab`（后续 gated） | factor tear sheet、backtest report、signal diagnostics |
| **交易执行 / broker / HFT / RL** | algo trading、live signals、HFT、RL trading、order nodes | 不进默认 cloud v1 | Enterprise/Desktop 或客户自担风险环境 |

P0 应先做“研究交付能力”，不是“交易机器人能力”：

1. **Report-grade secondary research**：公司识别、filing 阅读、财务趋势、行业/竞争、估值、催化剂、风险、结论。
2. **Primary-market diligence**：公司/人/基金/LP/deal 实体图谱、融资/IPO/招股书、工商/KYC、可比交易、IC memo。
3. **Reproducible valuation**：DCF、trading comps、precedent transactions、sensitivity、football field；每个假设有来源或人工输入标记。
4. **Portfolio/risk review**：持仓暴露、集中度、回撤、VaR/CVaR、stress test、factor exposure、再平衡建议。
5. **Macro/geopolitical overlay**：利率、通胀、汇率、政策、贸易、供应链、地缘事件对公司/行业/组合的影响。

P1/P2 才扩展到：

- M&A deal model：accretion/dilution、sources & uses、synergy valuation、earnout/CVR/exchange ratio。
- Startup/VC valuation：Berkus、Scorecard、VC method、First Chicago、risk-factor summation。
- Quant research：factor discovery、IC/IR、walk-forward backtest、regime detection、pairs trading、technical signals。
- Derivatives/fixed income：options Greeks、bond duration/convexity、yield curve、credit spread analysis。

明确不做的 P0：

- 不默认支持自动下单、实盘交易、broker order、HFT、RL trading。
- 不把 FinceptTerminal 的 AGPL/商业许可代码作为 Hive SaaS 内置依赖。
- 不让 LLM 直接“算所有东西”；关键数值模型必须是 deterministic calculator，可重跑、可测试、可审计。

#### `finance_analysis` 内部接口（不额外污染 LLM tool surface）

```python
class FinanceAnalysisEngine:
    def build_research_packet(entity_id: str, workflow: str) -> ResearchPacket: ...
    def run_dcf(packet: ResearchPacket, assumptions: DcfAssumptions) -> ValuationResult: ...
    def run_comps(packet: ResearchPacket, peer_set: PeerSet, metrics: list[str]) -> CompsResult: ...
    def analyze_financial_quality(packet: ResearchPacket) -> QualityResult: ...
    def analyze_portfolio_risk(holdings: list[Holding], scenario: RiskScenario) -> RiskResult: ...
    def build_ic_memo(packet: ResearchPacket, analysis_results: list[AnalysisResult]) -> ArtifactBundle: ...
```

LLM tool 保持粗粒度：

- `finance_compile_research_packet(...)`
- `finance_compute_dcf(...)`
- `finance_build_comps(...)`

其它 statement quality、risk、portfolio、M&A、startup valuation 先作为 workflow 内部 calculator / sub-step；等客户场景稳定后再决定是否升为 tool。

#### 成熟项目模式怎么借鉴

| 模式 | 来源 | Hive 用法 |
|------|------|----------|
| Persona pipeline | ai-hedge-fund | 多个投资视角独立分析：growth / value / risk / accounting / legal signals → PM 汇总 |
| 两步报告 | FinRobot | 先 `compile_research_packet` 生成结构化底稿，再由 writer agent 写报告 |
| Provider schema | OpenBB | 学 provider / category / tool discovery 设计，**不绑定 AGPL 代码** |
| Analysis library | FinceptTerminal | 学“CFA/投行模型库 + workflow 节点”的分层，Hive 内部重写 deterministic calculators |
| Agent workflow | Fincept / agno | 借鉴“workflow + 特定子 agent”组织方式，Hive 内部用现有 agent/delegate 能力实现 |

推荐子 agent 拆分：

```
Lead Finance Analyst
├── Data Compiler Agent        # 拉 connector，生成 source ledger
├── Filing Reader Agent        # 读 SEC/HKEX/巨潮/招股书
├── Statement Analyst Agent     # 财务趋势、质量、会计异常、银行/金融机构特殊口径
├── Market Comps Agent         # peer set + multiples
├── Valuation Agent            # DCF / comps / sensitivity
├── Primary Market DD Agent     # 融资、IPO、工商/KYC、股权结构、deal facts
├── Portfolio Risk Agent        # 持仓暴露、VaR/CVaR、stress、factor exposure
├── Macro & Geopolitics Agent   # 利率、政策、供应链、地缘事件 overlay
├── Risk & Compliance Agent    # KYC / sanctions / legal / related-party
└── IC Memo Writer Agent       # 输出投委会材料 / 二级深度报告
```

每个子 agent 的输出必须是结构化 finding，而不是散文：

```json
{
  "claim": "Revenue growth decelerated for three consecutive quarters",
  "evidence": [{"source_id": "filing:sec:10q:2025q3", "field": "revenue", "periods": ["2025Q1", "2025Q2", "2025Q3"]}],
  "calculation_id": "statement_trend:v1",
  "confidence": "high",
  "risk": "medium",
  "artifact_refs": ["tables/revenue_trend.csv", "charts/revenue_growth.png"]
}
```

#### FinceptTerminal 取证摘要（只借鉴，不复制）

| 证据 | 说明 | Hive 吸收方式 |
|------|------|---------------|
| `fincept-qt/scripts/Analytics/README.md` | 明确列出 equity valuation、portfolio、derivatives、economics、financial analysis、alternative investment、ML/backtesting 等分析大类 | 作为 finance analysis capability map |
| `fincept-qt/src/services/equity/EquityResearchModels.h` | company fundamentals、financial statements、technicals、peers、news、sentiment 已经是 equity research 数据形状 | 作为 `ResearchPacket` schema 参考 |
| `fincept-qt/src/services/ma_analytics/MAAnalyticsTypes.h` | DCF、LBO、merger、deals、startup valuation、fairness opinion、Monte Carlo、deal comparison | 作为一级/投行 P1 workflow 参考 |
| `fincept-qt/src/services/ai_quant_lab/AIQuantLabTypes.h` | factor discovery、model library、backtesting、RL、online/meta learning、feature engineering、factor evaluation | 作为后续 `alpha-factor-lab`，不进 P0 |
| `fincept-qt/src/services/workflow/nodes/AnalyticsNodes.cpp` | technical indicators、backtest、portfolio optimization、performance、risk、factor、pairs、regime detection 节点化 | 作为 Hive workflow DAG / deterministic node 参考 |
| `fincept-qt/scripts/agents/finagent_core/README.md` | persona isolation、config-driven agent、memory/storage/knowledge 分离 | 作为 Hive 子 agent 配置和租户隔离参考 |

#### 可拆微服务：finance-data-service

如果 finance 依赖继续变重，建议在 Stage 5a 把数据层拆成独立服务，而不是塞进主 backend：

| 维度 | 主 backend 内置 | 独立 `finance-data-service` |
|------|----------------|-----------------------------|
| 启动速度 | 更简单 | 依赖隔离更好 |
| 多租户凭证 | 能复用现有 secrets_provider | 需要服务间 credential token |
| PDF/OCR/爬虫依赖 | 会污染主 backend image | 隔离最干净 |
| 合规 / license 边界 | 容易混 | 更清晰 |
| 推荐 | Stage 5a 先定义接口，可先内置实现 | 一旦接 OCR/商业数据源/高频采集，就拆服务 |

接口建议：

```http
POST /v1/entities/resolve
POST /v1/filings/search
POST /v1/filings/extract
POST /v1/market/prices
POST /v1/ipo/pipeline
POST /v1/research-packets/compile
GET  /v1/sources/{source_id}/ledger
```

#### 故意推迟到 Enterprise / Desktop

Wind / Bloomberg / Choice / iFinD 终端、PitchBook/Preqin 深度订阅、World-Check/Dow Jones KYC — 这些都不进默认 SaaS 数据源。只有客户后台配置凭证、或者客户合同覆盖数据成本时启用。

### 6.3 office-pack — 先 package 化现有 skill，不堆 atomic tool

#### 设计原则（修订自 v0.1）

```
office pack v1 = 3a: 现有 4 个 skill 升级为完整 package
                     + 3 个新 SOP skill（weekly-report / meeting-minutes / pitch-deck）
                 3b: 外部账户工具（gmail/outlook/imap/gcal 等，单独交付）
```

**不做**：office_read_docx / office_write_docx 这种 atomic tool wrapper。Hive 已有 `code_exec` + `python-docx`，直接让 LLM 写代码，符合 Anthropic 范式。

#### 第一步：升级现有 4 个 skill 为完整 package

**docx-generator/**

```
docx-generator/
├── SKILL.md              # 已有，需更新 frontmatter
├── references/           # 🆕 新建
│   ├── docx-cookbook.md  # python-docx 常用模式（表格、样式、图片插入）
│   ├── ooxml-basics.md   # 编辑现有文档（unpack/edit/pack）
│   └── style-guide.md    # 中英文混排、字体fallback、PageBreak
├── scripts/              # 🆕 新建
│   ├── unpack.py         # OOXML 解包（仿 Anthropic）
│   ├── pack.py           # OOXML 重打包
│   └── recalc.py         # 验证文档合法性
├── templates/            # 🆕 新建
│   ├── corporate-letter.docx     # docxtpl 模板
│   ├── meeting-summary.docx
│   └── proposal.docx
└── evals/                # 🆕 新建
    ├── eval.yaml
    └── samples/
        ├── input-1.json
        └── expected-1.docx
```

**xlsx-processor/** — 同样升级（加 references/ scripts/ templates/ evals/）

- references：openpyxl 公式与样式、xlsxwriter 性能差异、pandas 集成
- scripts：recalc.py（检查 #REF! / #DIV/0!）、convert_csv_to_xlsx.py
- templates：财务模型、数据透视样板、看板模板
- evals：黄金样本

**pptx-generator/** — 同样升级

- references：python-pptx 布局 API、占位符规则、母版（master）使用
- **scripts**：thumbnail.py（**关键** — Anthropic QA loop 渲染→PNG→检查）、outline_to_pptx.py
- templates：5 主题（steel / parchment / neon / editorial / corporate）每主题一份 master.pptx

**pdf-generator/** — 已有 scripts/，补 references/ + evals/

- references：reportlab Flowable 模式、pypdf 操作、unicode 注意（如 Anthropic 提示的"never use Unicode subscript/superscript"）
- evals：黄金样本

#### 第二步：新增外部账户工具（这些是真的缺）

| 工具 | 后端 | governance |
|------|------|----------|
| `office_gmail_send` / `office_gmail_search` / `office_gmail_thread` | Google API Python client | restricted + OAuth |
| `office_outlook_send` / `office_outlook_search` | msgraph-sdk-python | restricted + OAuth |
| `office_gcal_create_event` / `office_gcal_list_events` / `office_gcal_update_event` | Google Calendar API | restricted + OAuth |
| `office_outlook_calendar_create` / `office_outlook_calendar_list` | Graph `/me/calendar/events` | restricted + OAuth |
| `office_imap_search` / `office_imap_fetch` / `office_smtp_send` | stdlib `imaplib` + `smtplib` | restricted + 凭证 |
| `office_onedrive_upload` / `office_onedrive_list` | Graph `/me/drive` | restricted + OAuth |
| `office_gdrive_upload` / `office_gdrive_list` | Drive API | restricted + OAuth |

约 12-15 个工具，都是**真的缺**且**需要 governance**（外部账户、凭证、回调）。

不做的：`office_read_docx` / `office_write_docx` / `office_thumbnail` 等。这些走 skill + code_exec。

#### 第三步：新增 3 个 SOP skill

##### `weekly-report-generator/`

```
weekly-report-generator/
├── SKILL.md
├── references/
│   ├── tone-guide.md
│   └── data-sources.md           # Jira / Linear / GitHub / Feishu Tasks 接入指南
├── scripts/
│   ├── collect_signals.py        # 拉去过 7 天活动
│   └── render.py                 # docxtpl 渲染
├── templates/
│   ├── exec-summary.docx         # docxtpl 模板
│   ├── team-update.docx
│   └── ic-update.md              # IM-style markdown
├── assets/
│   └── examples/
│       └── 2026-w17-example.docx
└── evals/
    ├── eval.yaml
    └── samples/
```

##### `meeting-minutes/`

```
meeting-minutes/
├── SKILL.md
├── references/
│   ├── transcription-providers.md  # Whisper / Parakeet / whisper.cpp
│   ├── speaker-diarization.md
│   └── extraction-rubric.md
├── scripts/
│   ├── transcribe.py
│   ├── diarize.py
│   └── extract_decisions.py
├── templates/
│   ├── minutes.md
│   └── minutes.docx
└── assets/
    └── examples/
        └── q1-planning-2026.md
```

参考项目：[Meetily](https://github.com/Zackriya-Solutions/meetily) / [meeting-transcriber](https://github.com/jfcostello/meeting-transcriber)

##### `pitch-deck-generator/`

```
pitch-deck-generator/
├── SKILL.md
├── references/
│   ├── deck-structures.md          # 10 种 deck 形态
│   ├── slide-design-principles.md  # 60-30-10 配色 / 不要文字墙
│   └── fonts-colors.md
├── scripts/
│   ├── outline.py                  # brief → 12-slide outline
│   ├── render.py                   # outline JSON + theme → .pptx
│   └── thumbnail.py                # 每页 → PNG（QA loop 关键）
├── templates/
│   └── themes/                     # 5 主题
│       ├── steel/
│       ├── parchment/
│       ├── neon/
│       ├── editorial/
│       └── corporate/
└── assets/
    └── examples/
        └── series-a-deck-2026.pptx
```

**Killer pattern — 多轮 QA loop**（采纳 Anthropic）：

```
1. outline.py → 12-slide JSON
2. 用户确认 outline
3. render.py → .pptx
4. thumbnail.py → 每页 PNG
5. 主 agent 检查每张 PNG（文字溢出 / 低对比 / 对齐错位 / 字体不一致）
6. 直接用 python-pptx 修
7. 改过的页重新 thumbnail 验证
```

#### 沙箱依赖（已经具备）

`backend/pyproject.toml:28-41` 已装：

```
pdfplumber>=0.11.0
python-docx>=1.1.0
openpyxl>=3.1.0
python-pptx>=1.0.0
reportlab>=4.0.0
pypdf>=4.0.0
xlsxwriter>=3.x
```

需要补的：
- `markitdown`（Microsoft，万能转 markdown）
- `docxtpl`（DOCX 模板填充）
- `pypandoc` + LibreOffice headless（高保真转换）
- `weasyprint`（HTML→PDF）

加到 `pyproject.toml` 即可。

#### 应该补的 SOP（按真实场景，v2 后续）

| Skill | 用途 |
|------|------|
| `email-drafter` | 多语种 / 多场景邮件起草 |
| `contract-review` | 合同 PDF → 风险点 + 修订建议 |
| `data-pivot` | 多源 Excel → 透视表 + 图 |
| `kpi-dashboard` | 数据汇总 → 仪表盘 |
| `internal-comm` | 公告 / 状态更新 / 事故报告 |

---

## 7. 后端改造路径

### 7.1 v1 阶段（Stage 1-5b）真正的 file-level 改动

| 文件 | 改动 | 工时 | 风险 |
|------|------|------|------|
| `backend/app/skills/parser.py` | 手写正则 → **PyYAML**；description 200→1024;解析 `license` `compatibility` `allowed-tools` flat `metadata["hive.*"]` + nested fallback；**全部 optional + tolerant**；保留旧字段读法做 backward compat | 1 天 | 低 |
| `backend/app/skills/types.py` | `SkillMetadata` 加 `version` `pack` `requires_skills` `locale` `invocation` `cost_tier` 等可选字段；缺失值用默认 | 0.5 天 | 低 |
| `backend/app/skills/loader.py` | 文件夹结构识别 `scripts/` `references/` `templates/` `assets/` `evals/`；提供 `list_resources(skill_name)` / `read_resource(skill_name, path)` API | 1 天 | 低 |
| `backend/app/skills/registry.py` | catalog 渲染已对齐 Claude Code，加 `requires_skills` 解析在激活时尝试连带加载（缺则 warn）| 0.5 天 | 低 |
| **新增** `backend/app/packs/catalog_reader.py` | **新建**：扫描 `packs/*/pack.yaml`，提供 `list_packs()` / `get_pack(name)` API（**只读，不影响 runtime**）| 1 天 | 低 |
| `backend/app/templates/skills/docx-generator/` etc. | 4 个现有 skill 升级为完整 package（加 references/ scripts/ templates/ evals/）| 4-5 天 | 低 |
| **新增** `backend/app/tools/handlers/research/*.py` | 8-10 个 deep research 工具 | 1 周 | 中 |
| **新增** `backend/app/finance_data/schemas.py` | `EntityMaster` / `Security` / `Filing` / `FundingRound` / `IPOEvent` / `SourceLedger` 统一 schema | 2-3 天 | 中 |
| **新增** `backend/app/finance_data/connectors/*.py` | SEC / HKEX / cninfo / akshare / yfinance / GLEIF / sanctions connectors；付费源只声明 adapter 接口 | 1.5-2 周 | 高 |
| **新增** `backend/app/finance_data/entity_master.py` | 多源 ID 映射、company/person/fund/deal 归一化 | 1 周 | 高 |
| **新增** `backend/app/finance_analysis/schemas.py` | `ResearchPacket` / `AnalysisResult` / `AssumptionSet` / `ArtifactBundle` / `ValuationResult` / `RiskResult` schema | 2 天 | 中 |
| **新增** `backend/app/finance_analysis/engine.py` | 粗粒度 orchestration：research packet → calculator → artifact；不直接访问外部 API，只消费 `finance_data` | 3-4 天 | 中 |
| **新增** `backend/app/finance_analysis/calculators/*.py` | DCF、trading comps、financial quality、portfolio risk、sensitivity 等 deterministic calculators | 1 周 | 中 |
| **新增** `backend/app/finance_analysis/workflows/*.py` | 二级深度、一级尽调、IPO pipeline、组合风险 review 的 workflow state machine / subagent contract | 1 周 | 中-高 |
| **新增** `backend/app/tools/handlers/finance/*.py` | 12 个以内 finance tool，全部调用 finance data layer，不直接访问外部 API | 1 周 | 中-高 |
| **新增** `backend/app/tools/handlers/office_external/*.py` | 12-15 个外部账户工具（gmail/outlook/imap/gcal 等）| 1 周 | 中（OAuth + 凭证）|
| **新增** `packs/*/pack.yaml` | 8 个 pack manifest（5 个旧的迁移 + 3 个新）| 2 天 | 低 |
| **可选新增** `services/finance-data-service/` | 当 PDF/OCR/爬虫/付费源依赖过重时拆成独立 FastAPI 微服务 | 1-2 周 | 中-高 |
| `pyproject.toml` | 加 markitdown / docxtpl / pypandoc / weasyprint | 0.5 天 | 低 |

**v1 不动的文件**（Stage 6 才动）：
- `backend/app/tools/collector.py`
- `backend/app/tools/packs.py`
- `backend/app/tools/governance.py` 的 pack 相关逻辑
- `backend/app/services/pack_service.py`
- `backend/app/services/tool_seeder.py`
- `backend/app/runtime/invoker.py` 的 pack 激活路径

**总 v1 工时**：约 10-12 周（与 6 个 v1 stage 总周期 10-14 周匹配，留 1-2 周缓冲）。

### 7.2 关键代码示例

#### parser.py 改造前后对比

**前**（手写正则，5 字段）：

```python
# 50 行手写循环 if/elif 解析 5 个字段
class SkillParser:
    FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
    def parse_content(self, content, ...):
        ...
```

**后**（PyYAML，宽容解析）：

```python
import yaml
import logging

logger = logging.getLogger(__name__)

class SkillParser:
    def parse_content(self, content, *, path, relative_path, default_name=None):
        match = self.FRONTMATTER_PATTERN.match(content.strip())
        if not match:
            return ParsedSkill(...)  # 没 frontmatter，body=全部内容

        try:
            frontmatter = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as e:
            logger.warning(f"Skill {relative_path} has invalid YAML frontmatter: {e}")
            frontmatter = {}

        # 标准字段（必需）
        name = frontmatter.get("name") or default_name or path.stem
        description = (frontmatter.get("description") or "")[:1024]  # 1024 上限

        # 标准字段（可选）
        license_field = frontmatter.get("license")
        compatibility = frontmatter.get("compatibility")
        allowed_tools = (frontmatter.get("allowed-tools") or "").split()

        # Hive 私有字段：优先 flat keys，兼容 nested metadata.hive fallback
        metadata = frontmatter.get("metadata") or {}
        nested_hive = metadata.get("hive") if isinstance(metadata.get("hive"), dict) else {}

        def hive_value(key: str, default=None):
            return metadata.get(f"hive.{key}", nested_hive.get(key, default))

        version = hive_value("version", "0.0.0")  # 默认值，缺失不报错
        pack = hive_value("pack")
        requires_raw = hive_value("requires_skills", "")
        if isinstance(requires_raw, str):
            requires_skills = tuple(x.strip() for x in requires_raw.split(",") if x.strip())
        else:
            requires_skills = tuple(requires_raw or [])
        locale = hive_value("locale", "cloud")
        invocation = hive_value("invocation", "both")
        cost_tier = hive_value("cost_tier")
        # ... 其它字段，全部带默认值

        return ParsedSkill(
            metadata=SkillMetadata(
                name=name,
                description=description,
                license=license_field,
                compatibility=compatibility,
                allowed_tools=tuple(allowed_tools),
                version=version,
                pack=pack,
                requires_skills=requires_skills,
                locale=locale,
                invocation=invocation,
                cost_tier=cost_tier,
                # ...
            ),
            body=match.group(2).strip(),
            file_path=path,
            relative_path=relative_path,
        )
```

#### catalog_reader.py 新增（不动 runtime）

```python
# backend/app/packs/catalog_reader.py（新文件）
import yaml
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True, slots=True)
class PackManifest:
    name: str
    version: str
    description: str
    license: Optional[str]
    author: Optional[str]
    tools: tuple                # 文档性，runtime 仍走 @tool decorator
    skills: tuple               # 相对路径
    data_sources: dict
    mcp_servers: tuple
    credential_requirements: tuple
    activation: dict
    sandbox_requirements: dict


class PackCatalogReader:
    """Read-only catalog reader for packs/*/pack.yaml.

    v1 阶段这是旁路读取层，UI / 文档 / marketplace 用。
    Runtime pack 归属仍来自 @tool(ToolMeta(... pack=...))。
    """

    def __init__(self, packs_dir: Path):
        self.packs_dir = packs_dir
        self._manifests: dict[str, PackManifest] = {}

    def discover(self):
        if not self.packs_dir.exists():
            return
        for pack_dir in sorted(self.packs_dir.iterdir()):
            manifest_file = pack_dir / "pack.yaml"
            if not manifest_file.exists():
                continue
            try:
                data = yaml.safe_load(manifest_file.read_text())
                manifest = PackManifest(...)
                self._manifests[manifest.name] = manifest
            except Exception as e:
                logger.error(f"Failed to load pack manifest {pack_dir}: {e}")

    def list_packs(self) -> tuple[PackManifest, ...]:
        return tuple(self._manifests.values())
```

**关键**：这个新文件**不修改** `collector.py` `packs.py` `pack_service.py`。Runtime 行为完全不变。

---

## 8. 渐进交付路径（6+1 stage）

| Stage | 内容 | 周期 | 风险 | 并行可能 |
|-------|------|------|------|----------|
| **1. Parser v2** | PyYAML 替正则；desc 200→1024；解析 flat `metadata["hive.*"]` + nested fallback；types.py 加字段；loader 加子目录支持；registry 加 requires_skills（缺则 warn） | 1 周 | 低 | 串行（基础设施）|
| **2. Pack manifest catalog** | `packs/*/pack.yaml` 文件 + `PackCatalogReader` 新文件；UI 加"Pack 详情"页面；**不动 collector / pack_service / runtime** | 1 周 | 低 | 串行（基础设施）|
| **3a. Office package 化** | 现有 4 个 skill 升级为完整 package（references/scripts/templates/evals）；加 markitdown/docxtpl/pypandoc/weasyprint；新增 3 个 SOP skill（weekly-report / meeting-minutes / pitch-deck） | 1-2 周 | 低-中 | **可与 Stage 4/5a 并行** |
| **3b. Office external accounts** | 新增 12-15 个外部账户工具（gmail/outlook/imap/gcal/onedrive/gdrive）；tenant credential + OAuth + audit | 1-2 周 | 中（外部 API + OAuth）| **可与 Stage 4/5a 并行；不阻塞 3a** |
| **4. Deep-research pack** | 8-10 个高价值工具实现 + 5-phase orchestrator SOP（`industry-research` `topic-deep-dive`）+ 独立 `compile_citations` agent + 与 4 层记忆集成 | 2-3 周 | 中（PIES 失败模式）| **可与 Stage 3a/5a 并行** |
| **5a. Finance data foundation** | 美股/港股/A股一二级 connector + entity master + source ledger + tenant credential policy；OpenBB optional only | 3-4 周 | 高 | **可与 Stage 3a/4 并行** |
| **5b. Finance analysis workflows** | `secondary-equity-deep-dive` + `dcf-valuation` + `comps-valuation` + `ipo-pipeline-monitor` + `primary-market-due-diligence` + `portfolio-risk-review` + `ic-memo-generator` | 2-3 周 | 中-高 | 必须在 Stage 4 协议和 Stage 5a 数据层定稿后 |
| **6. Pack runtime 迁移**（**单独立项**） | `@tool(ToolMeta(... pack=...))` → `pack.yaml` 完全切换；改 collector / pack_service / runtime/invoker / capability_gate / tool_seeder / UI / 测试 | 后续单独排期 | 高 | 串行 |

**总 v1 周期**：10-14 周（Stage 1-5b 完成）。

**Stage 6 单独立项**的理由：
- 牵动文件多（7+ 文件 + 全部 pack 测试）
- 不阻塞内容能力交付（Stage 1-5b 用 catalog 旁路就能跑）
- 收益是工程清洁度（客户/合作伙伴可发布 pack），不是用户感知功能
- 留出窗口观察 v1 的 pack.yaml 设计是否合适，再做 runtime 切换

---

## 9. 待决策的开放问题

Stage 1 开工前需要确认：

| # | 问题 | v0.4 推荐方案 | 理由 |
|---|------|--------------|------|
| 1 | 版本字段是否强制 semver？ | **Optional**，缺失默认 `0.0.0` + warn。**强约束放发布流水线**（marketplace 上架时校验） | 跨平台兼容；本地开发不应被版本格式卡住 |
| 2 | pack manifest 用 YAML 还是 JSON？ | **YAML** | 与现有 `extensions_config.json` 哲学一致，更人类可读 |
| 3 | skill→skill 依赖怎么解析？ | **激活时尝试加载所有依赖 skill 的 body，缺则 warn 但不阻塞** | 显式声明 + 自动加载更自然；缺失不阻塞符合 tolerant 原则 |
| 4 | locale 路由现在做到什么程度？ | **只在 frontmatter 加 hint 字段**，不做路由实装；所有工具 v1 都是 cloud locale | 不浪费时间提前实装 |
| 5 | body 重读策略？ | **跟 Claude Code，一会话一次性进 context，不重读** | 省 token，可预测 |
| 6 | 工具命名约定？ | **全部 underscore**：`finance_get_price_history` / `office_gmail_send`；命名空间通过元数据字段表达，不通过工具名前缀 | OpenAI function name 规范 `[a-zA-Z0-9_-]`，dotted name 进 LLM API 报错 |
| 7 | `allowed-tools` 语义？ | **当 hint，不当授权** | 真授权仍走 `governance_resolver`；`allowed-tools` 提供给 kernel 的发现提示 |
| 8 | pack.yaml 在 v1 是 source of truth 还是 catalog 旁路？ | **catalog 旁路**。runtime 仍走 `@tool` 装饰器。Stage 6 才迁移 | 改 runtime 要动 7+ 文件 + 测试，独立立项 |
| 9 | MCP / 付费源凭证怎么放？ | **tenant-scoped encrypted tool config**；pack.yaml 只声明 `credential_requirements`，不写平台全局 env | Hive 是多租户平台；客户 key / 付费源不能进入系统全局环境变量 |
| 10 | finance data layer 是否拆微服务？ | **接口先服务化，部署可先内置**；一旦 OCR/爬虫/付费源依赖变重，拆 `finance-data-service` | 先保证交付速度，保留清晰边界 |
| 11 | OpenBB MCP 是否作为默认数据层？ | **否**。OpenBB 只做 optional external MCP 或架构参考 | OpenBB Platform / MCP 是 AGPL；闭源 SaaS 默认绑定有 license 风险 |
| 12 | 金融数据覆盖优先级？ | **A 股 / 港股 / 美股的一二级 P0 覆盖优先于分析模型数量** | 金融机构客户先看数据完整性、来源可信和可追溯 |
| 13 | 数据之后的分析层怎么划边界？ | **新增 `finance_analysis` 内部层**：deterministic calculators + workflow state + artifact bundle；LLM tool 保持粗粒度 | 可复算、可测试、可审计；避免 80+ 分析函数污染 prompt surface |
| 14 | 交易/回测/HFT/RL 是否进 cloud v1？ | **不进默认 P0**。先做研究交付；回测/alpha lab 后续 gated；自动下单/实盘交易留 Enterprise/Desktop | 多租户云端默认做交易执行风险高，客户价值先在研究和尽调 |

---

## 10. 验收标准（每个 pack 必须可验证）

| Pack | 必须通过的 eval | 最低通过线 |
|------|----------------|------------|
| **skill parser / catalog** | fixture 覆盖 flat metadata、nested fallback、未知字段保留、invalid YAML tolerant、folder resources | parser 单测全绿；旧 skill 不回归 |
| **pack catalog** | 读取 `pack.yaml`、展示工具/skill/MCP/credential requirements；runtime 行为不变 | pack API/UI 可显示；collector output 不变 |
| **office package** | docx/xlsx/pptx/pdf golden samples；文件能打开；渲染截图检查文字溢出/低对比/布局错位 | 每类至少 3 个样本通过 |
| **office external accounts** | OAuth / IMAP / SMTP / calendar mock；tenant A/B credential 隔离；audit log 记录 | 跨租户不能读到对方凭证或数据 |
| **deep-research** | source ledger 完整；citation URL 可解析；引用和报告段落能对应；subagent findings 可复现 | citation 可解析率 ≥95%，所有关键结论有来源 |
| **finance data layer** | 每个市场至少 1 个端到端样本：A 股、港股、美股；每个字段有 source ledger | 价格/财报/filing/IPO pipeline 样本全通；数字可追溯 |
| **finance analysis workflow** | 二级深度、一级尽调、IPO pipeline、组合风险、IC memo golden tasks | 报告内所有关键数字有来源；估值/风险模型可重算；subagent findings 有结构化 evidence |

**finance 第一条验收原则**：任何金融数字如果不能回答“来自哪个数据源、哪份 filing、哪个 URL、哪个抓取时间”，就不能进入正式报告，只能标为 `[UNVERIFIED]`。

## 附录 A：完整 SKILL.md 示例（可上线）

```yaml
---
name: secondary-equity-deep-dive
description: |
  Conduct a sell-side-analyst-grade equity research deep dive on a single listed
  company, covering business model, industry positioning, competitive moat,
  financial-statement analysis, valuation (DCF + comparables), catalysts, and
  risk factors. Output is a structured Markdown report plus an Excel valuation
  model. Use when the user asks for "公司深度报告", "二级研究", "deep dive on
  <ticker>", "equity research report on X", or any sell-side-style equity
  analysis with valuation.
license: Proprietary
compatibility: |
  Requires Hive >= 1.8.0 and finance_pack installed.
allowed-tools: web_search web_fetch read_file write_file
metadata:
  hive.version: "1.0.0"
  hive.pack: finance_pack
  hive.requires_skills: "industry-research,dcf-valuation"
  hive.locale: cloud
  hive.invocation: both
  hive.cost_tier: high
  hive.estimated_runtime_minutes: "30"
  hive.output_artifacts: "reports/{ticker}-deep-dive-{date}.md,reports/{ticker}-valuation-{date}.xlsx"
  hive.author: Hive Finance Research Team
  hive.security_zone: restricted
---

# Secondary Equity Deep-Dive — Sell-Side Analyst Workflow

## When to use this skill
[...]

## Workflow（13 步）
- [ ] 1. Company identification and ticker resolution
- [ ] 2. Catalyst scan (last 30d news + next 90d earnings)
- [ ] 3. Financial statements (annual + last 4 quarters)
- [ ] 4. 5-year trend analysis
- [ ] 5. Filing deep read (10-K / annual report)
- [ ] 6. Industry positioning (delegate to industry-research skill)
- [ ] 7. Earnings call review
- [ ] 8. DCF valuation (delegate to dcf-valuation skill)
- [ ] 9. Comparables valuation
- [ ] 10. Catalyst map (forward 12m)
- [ ] 11. Risk assessment
- [ ] 12. Report generation (FinRobot two-step pattern)
- [ ] 13. T2 distillation

## References
- [references/sell-side-format.md](references/sell-side-format.md)
- [references/disclosure-language.md](references/disclosure-language.md)
- [references/valuation-methods.md](references/valuation-methods.md)

## Templates
- [templates/deep-dive-report.md](templates/deep-dive-report.md)
- [templates/valuation-model.xlsx](templates/valuation-model.xlsx)

## Anti-patterns
- ❌ Skip Phase 0 clarification
- ❌ Inline citations while writing — use compile_citations post-processing
- ❌ Single agent at 200 rounds — use orchestrator + delegate_to_agent
- ❌ Skip CSRC disclosure language for A-share reports
```

文件夹：

```
secondary-equity-deep-dive/
├── SKILL.md
├── checklist.md
├── references/
│   ├── sell-side-format.md
│   ├── disclosure-language.md
│   └── valuation-methods.md
├── templates/
│   ├── deep-dive-report.md
│   └── valuation-model.xlsx
├── assets/
│   └── industry-tags.json
├── scripts/
│   ├── compile_financials.py
│   └── render_report.py
└── evals/
    ├── eval.yaml
    └── samples/
```

---

## 附录 B：完整 pack.yaml 示例（可上线）

```yaml
# packs/finance_pack/pack.yaml
name: finance_pack
version: "1.0.0"
description: |
  Finance research toolkit covering primary (PE/VC) and secondary (sell-side)
  workflows, with valuation, financial statement, and regulatory disclosure
  tools. Cloud-first; desktop terminals (Wind/Bloomberg/Choice/iFinD) deferred
  to Enterprise Edition.
license: Proprietary
author: Hive Finance Team

tools:
  # Market Data
  - name: finance_get_price_history
    locale: cloud
    governance:
      security_zone: public

  # Fundamentals
  - name: finance_get_company_overview
    locale: cloud
    governance:
      security_zone: public

  - name: finance_get_income_statement
    locale: cloud
    governance:
      security_zone: public

  # ... 其余 v1 工具，最多 12 个 ...

  # Valuation
  - name: finance_compute_dcf
    locale: cloud
    governance:
      security_zone: public

  # CN A-share
  - name: finance_get_cn_quote
    locale: cloud
    governance:
      security_zone: public

  # Desktop-only (留给桌面端 Stage 后续)
  - name: finance_wind_terminal_query
    locale: desktop
    governance:
      security_zone: restricted
      requires_credential: wind_license

skills:
  - skills/secondary-equity-deep-dive
  - skills/dcf-valuation

mcp_servers:
  - name: openbb_optional
    enabled_by_default: false
    license_note: AGPL/commercial license required if Hive hosts or modifies OpenBB Platform/MCP
    transport: stdio
    command: openbb-mcp
    args: ["--default-categories", "admin", "--tool-discovery"]
    credential_scope: tenant

  - name: edgartools_optional
    enabled_by_default: false
    transport: stdio
    command: uvx
    args: ["--from", "edgartools[ai]", "edgartools-mcp"]
    credential_scope: tenant

data_sources:
  public_default:
    - sec_edgar
    - hkexnews
    - cninfo
    - akshare
    - yfinance
    - gleif
  paid_optional:
    - fmp
    - polygon
    - crunchbase
    - qichacha
    - tianyancha
    - wind
    - pitchbook

credential_requirements:
  - key: fmp_api_key
    scope: tenant
    storage: encrypted_tool_config
    injected_as: per_invocation_env
    env_name: FMP_API_KEY
  - key: edgar_identity
    scope: tenant
    storage: encrypted_tool_config
    injected_as: per_invocation_env
    env_name: EDGAR_IDENTITY

activation:
  required_capabilities: [finance_data_access]
  default_state: inactive

sandbox_requirements:
  pip_packages:
    - akshare>=1.18.59,<1.20      # 版本锁定（实测维护频繁）
    - tushare>=1.4.0
    - yfinance>=0.2.40
    - financetoolkit>=2.0
    - edgartools>=5.30
    - stockstats>=0.6
```

**重申**：这份 pack.yaml 在 v1 阶段是 **catalog 旁路读取层**，UI 用来展示，**不接管 runtime**。runtime 行为完全保持现状（`@tool(ToolMeta(... pack="finance_pack"))` 装饰器）。`env_name` 不是平台全局环境变量，只是 vendor SDK/MCP 子进程的注入目标；值必须来自 tenant-scoped encrypted tool config。

---

## 附录 C：引用源完整列表

### Skill 标准

- [agentskills.io overview](https://agentskills.io)
- [agentskills.io specification](https://agentskills.io/specification)
- [code.claude.com Claude Code skills](https://code.claude.com/docs/en/skills)
- [platform.claude.com Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
- [platform.claude.com Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- [anthropics/skills repo](https://github.com/anthropics/skills)
- [opencode.ai skills doc](https://opencode.ai/docs/skills/)
- [geminicli.com extensions reference](https://geminicli.com/docs/extensions/reference/)
- [modelcontextprotocol.io 2025-06-18 tools spec](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [docs.composio.dev custom tools](https://docs.composio.dev/docs/custom-tools)

### Deep Research

- [Anthropic — How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
- [OpenAI — Introducing deep research](https://openai.com/index/introducing-deep-research/)
- [Google — Deep Research Max](https://blog.google/innovation-and-ai/models-and-research/gemini-models/next-generation-gemini-deep-research/)
- [Perplexity — Introducing Deep Research](https://www.perplexity.ai/hub/blog/introducing-perplexity-deep-research)
- [HuggingFace — Open Deep Research](https://huggingface.co/blog/open-deep-research)
- [PIES paper (arXiv 2601.22984)](https://arxiv.org/abs/2601.22984)
- [assafelovic/gpt-researcher](https://github.com/assafelovic/gpt-researcher)
- [stanford-oval/storm](https://github.com/stanford-oval/storm)
- [langchain-ai/open_deep_research](https://github.com/langchain-ai/open_deep_research)
- [Tavily docs](https://docs.tavily.com/welcome)
- [Exa Answer API](https://exa.ai/docs/reference/answer)

### Finance

- [OpenBB license change to AGPL](https://openbb.co/blog/license-change-openbb-platform-goes-agpl/)
- [openbb-mcp-server PyPI](https://pypi.org/project/openbb-mcp-server/)
- [SEC API Overview](https://www.sec.gov/file/api-overview)
- [HKEX Application Proof / PHIP notes](https://www2.hkexnews.hk/New-Listings/Application-Proof-and-PHIP/Explanatory-Notes/Main-Board?sc_lang=en)
- [EdgarTools 424B prospectus parser](https://www.edgartools.io/parse-sec-424b-prospectus-filings-with-python/)
- [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT)
- [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot)
- [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)
- [OpenBB](https://github.com/OpenBB-finance/OpenBB)
- [AKShare](https://github.com/akfamily/akshare)
- [Tushare](https://tushare.pro)
- [yfinance](https://github.com/ranaroussi/yfinance)
- [edgartools](https://github.com/dgunning/edgartools)
- [FinanceToolkit](https://github.com/JerBouma/FinanceToolkit)
- [stockstats](https://github.com/jealous/stockstats)
- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

本地 FinceptTerminal 取证（只作为架构/能力参考，不复制代码）：

- `/Users/rocky243/testing-project/FinceptTerminal/fincept-qt/scripts/Analytics/README.md`
- `/Users/rocky243/testing-project/FinceptTerminal/fincept-qt/src/services/equity/EquityResearchModels.h`
- `/Users/rocky243/testing-project/FinceptTerminal/fincept-qt/src/services/ma_analytics/MAAnalyticsTypes.h`
- `/Users/rocky243/testing-project/FinceptTerminal/fincept-qt/src/services/portfolio/PortfolioAnalyticsService.h`
- `/Users/rocky243/testing-project/FinceptTerminal/fincept-qt/src/services/ai_quant_lab/AIQuantLabTypes.h`
- `/Users/rocky243/testing-project/FinceptTerminal/fincept-qt/src/services/workflow/nodes/AnalyticsNodes.cpp`
- `/Users/rocky243/testing-project/FinceptTerminal/fincept-qt/scripts/agents/finagent_core/README.md`

### Office

- [anthropics/skills/docx](https://github.com/anthropics/skills/blob/main/skills/docx/SKILL.md)
- [anthropics/skills/pptx](https://github.com/anthropics/skills/blob/main/skills/pptx/SKILL.md)
- [anthropics/skills/xlsx](https://github.com/anthropics/skills/blob/main/skills/xlsx/SKILL.md)
- [anthropics/skills/pdf](https://github.com/anthropics/skills/blob/main/skills/pdf/SKILL.md)
- [python-docx](https://github.com/python-openxml/python-docx)
- [python-pptx](https://github.com/scanny/python-pptx)
- [openpyxl](https://openpyxl.readthedocs.io/)
- [docxtpl](https://docxtpl.readthedocs.io/)
- [markitdown (Microsoft)](https://github.com/microsoft/markitdown)
- [pdfplumber](https://github.com/jsvine/pdfplumber)
- [Microsoft Graph: Use the API](https://learn.microsoft.com/en-us/graph/use-the-api)
- [msgraph-sdk-python](https://github.com/microsoftgraph/msgraph-sdk-python)
- [Google API Python client](https://github.com/googleapis/google-api-python-client)
- [Meetily](https://github.com/Zackriya-Solutions/meetily)
- [txt2pptx](https://github.com/blackbyte7/txt2pptx)

### 完整研究报告

本文件总结自 4 份独立研究报告，并补充 FinceptTerminal 本地取证：

- `/tmp/hive-research-skills.md` — Skill 标准对比（8 平台）
- `/tmp/hive-research-deep-research.md` — Deep Research SOTA 调研
- `/tmp/hive-research-finance.md` — Finance pack 设计
- `/tmp/hive-research-office.md` — Office pack 设计
- `/Users/rocky243/testing-project/FinceptTerminal/.ultra/research/02-primary-market-modules.md` — 一级市场模块和工作流
- `/Users/rocky243/testing-project/FinceptTerminal/.ultra/research/03-data-sources.md` — 一级市场数据源采购梯度
- `/Users/rocky243/testing-project/FinceptTerminal/.ultra/research/04-data-layer-inventory.md` — 4 市场 × 一二级数据矩阵
- `/Users/rocky243/testing-project/FinceptTerminal/.ultra/research/05-open-source-ecosystem.md` — 开源金融生态与 license 边界
- `/Users/rocky243/testing-project/FinceptTerminal/.ultra/research/07-data-collection-stack.md` — 数据采集层组合方案
