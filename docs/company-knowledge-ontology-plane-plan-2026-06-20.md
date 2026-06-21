# 公司知识库 / 本体平面建设计划

日期：2026-06-20

## 1. 决策摘要

Hive 应该自己建设公司级 `Knowledge / Ontology Plane`，并把它作为公司知识、权限、审计和 Agent 写入治理的权威层。外部活跃项目可以接在下面，作为索引、图谱、检索、GraphRAG 或向量搜索 provider，但不能成为公司知识的最终真相源。

当前选型决定：第一版 graph provider 先采用 `getzep/graphiti`，并允许 `Zleap-AI/SAG` 作为平行 retrieval provider 同步接入。Graphiti 负责 temporal context graph、episode provenance、facts/relationships 增量更新和 graph/hybrid retrieval；SAG 负责 Markdown 文档语料上的 chunk / vector / event / entity / 多跳检索。Hive 仍然负责公司知识权威层、ACL、proposal/review、Platform Gate、source refs、审计、结果融合和 prompt 注入。

不要让 SAG、GraphRAG、Graphiti、Cognee、Neo4j、Weaviate、Qdrant 或任何单一外部项目承担以下权威职责：

- 租户 / 公司边界
- 对象、关系、动作本体
- source-of-truth ledger
- 权限和敏感级别策略
- Agent 提案 / 审核 / 批准流程
- 来源追溯和回滚
- prompt 注入边界
- 导出 / 迁移能力

这些系统可以帮助我们更快完成检索、图谱构建和索引，但 Hive 必须拥有公司知识的治理层。这个方向接近 Palantir Ontology 的核心经验：真正的地基不是 “RAG over docs”，而是覆盖组织现实世界的语义操作层，包括 object types、properties、link types、action types、permissions、history 和 operational actions。

## 2. 我们真正要建设什么

目标是一个可以由 Agent 在治理下持续维护的公司级活 Wiki / Ontology。

它包含多类知识：

- 公司资料：政策、SOP、组织架构、产品文档、客户笔记、销售材料、工程文档。
- Agent 产生的知识：已接受的 memory、任务发现、项目经验、workflow 输出、复盘、工具调用证据。
- 结构化本体对象：人员、部门、Agent、客户、供应商、项目、产品、服务、政策、决策、事故、技能、workflow。
- 关系：`owns`、`reports_to`、`works_on`、`depends_on`、`supersedes`、`references`、`caused_by`、`approved_by`、`delegated_to`、`governed_by`。
- 动作：提出事实、批准事实、连接对象、退役事实、合并实体、申请来源访问、发布 Wiki 页面、触发 workflow。

Memory 仍然是 Agent 私有或 Agent 作用域内的学习资产。公司知识是组织资产。Agent memory 可以晋升为公司知识，但必须经过治理候选路径，不能直接混入公司真相池。

## 3. 不可妥协的设计边界

1. Memory 不是公司知识库。
   Agent memory 可以生成公司知识候选，但不能直接成为公司事实。

2. 检索索引不是权威层。
   Vector DB、GraphRAG 输出、temporal graph engine 都是派生读模型 / 索引层。权威层必须由 Hive 保持可查询、可审计、可导出。

3. 每条晋升事实都必须有 source refs。
   公司事实必须能追溯回文档、T0/T2/T3 memory 证据、chat/session transcript、runtime task record 或管理员手写来源。

4. ACL 必须发生在 prompt 注入之前。
   检索引擎能搜 tenant 数据不够。Hive 必须在任何 prompt section 构建前，按 tenant、user、agent、role、department、project、sensitivity 和 source permission 过滤结果。

5. Agent 写入是 proposal，不是 commit。
   Agent 可以提出 assertion、link、object update、wiki edit。最终由 Platform Gate 原子提交已接受变更。

6. 外部 provider 必须可替换。
   把 “company truth” 绑定到某个检索或图谱 provider，是结构性风险。

## 4. 当前工具格局（2026-06-20 复核）

当前活跃度判断以 GitHub `pushed_at`、最新 release、license、产品适配度为主。单看 `updated_at` 不够，因为 issue/comment 也会刷新它。

| 项目 | 当前状态 | 适配度 | 建议 |
| --- | --- | --- | --- |
| `getzep/graphiti` | 活跃：2026-06-20 pushed，2026-06-08 发布 v0.29.2，Apache-2.0 | 面向 Agent 的 temporal knowledge graph；支持 episodes/provenance、temporal facts、自定义 entity/edge types、增量更新 | 第一版默认 provider，用于动态 Agent-maintained graph layer |
| `microsoft/graphrag` | 活跃：2026-06-19 pushed，2026-05-28 发布 v3.1.0，MIT | 文档语料的 batch GraphRAG、community summary、全局/局部问答基线很强 | 作为 benchmark / batch pipeline，不作为 live ontology 主层 |
| `HKUDS/LightRAG` | 活跃：2026-06-18 pushed，2026-06-14 发布 v1.5.3，MIT | 轻量图增强 RAG 算法，社区热度强 | 作为 benchmark / 可能的 retrieval provider，不作为权威层 |
| `Zleap-AI/SAG` | 新且活跃：2026-06-18 pushed，MIT，暂无稳定 release | 基于 Postgres/pgvector 的 event/entity retrieval，架构轻，贴近我们想做的多跳检索 | 可以 spike，但项目太年轻，不能直接定为 foundation |
| `topoteretes/cognee` | 活跃：2026-06-20 pushed，2026-06-18 发布 v1.1.3，Apache-2.0 | Agent memory / KG platform | 可评估为参考或 provider，但要注意和 Hive 自己 memory plane 的边界重叠 |
| `neo4j/neo4j-graphrag-python` | 活跃：2026-06-16 pushed，2026-05-27 发布 1.17.0 | Neo4j 官方 GraphRAG Python 包，图数据库生态成熟 | 如果 Neo4j 的运维和 license 被接受，是强候选 |
| `weaviate/weaviate` | 活跃：2026-06-20 pushed，2026-06-18 发布 v1.38.1，BSD-3-Clause | Vector/object DB，hybrid search，多租户，RBAC | 强生产检索 / 索引层候选 |
| `qdrant/qdrant` | 活跃：2026-06-20 pushed，2026-06-04 发布 v1.18.2，Apache-2.0 | 稳定向量库，payload filtering 成熟 | 强向量索引候选，但不够 ontology-native |
| `vespa-engine/vespa` | 活跃：2026-06-20 pushed，高频 release，Apache-2.0 | 生产级 hybrid search/ranking，支持 vectors、tensors、text、structured data | 最强严肃搜索平台候选，但运维更重 |
| `typedb/typedb` | 活跃：2026-06-19 pushed，2026-05-27 发布 3.11.5，MPL-2.0 | typed knowledge graph / reasoning 风格数据库 | 后续严格 ontology / reasoning 阶段可考虑，v1 可能过重 |
| `FalkorDB/FalkorDB` | 活跃：2026-06-20 pushed，2026-06-10 发布 v4.18.10 | 快速 graph DB，强 GraphRAG 定位 | license 风险：SSPL。除非法务明确批准，否则不作为默认 SaaS 地基 |
| `SciPhi-AI/R2R` | 不够新鲜：2025-11-07 pushed，2025-06-06 发布 v3.6.5 | 历史上不错的 RAG 产品思路 | 排除出 foundation 候选 |
| `OSU-NLP-Group/HippoRAG` | 2025-09-04 pushed，2025-02-27 发布 v1.0.0 | associative retrieval 研究基线 | 保留为算法参考，不作为地基 |

## 5. 推荐架构

### 5.1 权威层：Hive Knowledge Core

这一层应该建在 Hive 内部，使用 Hive 现有 PostgreSQL/RLS 和租户治理能力承载。

最小表 / 概念：

- `knowledge_sources`
  - Feishu wiki、上传文件、Agent memory、chat transcript、workflow 输出、手写页面、外部 connector。
- `knowledge_documents`
  - 带版本的源文档 / 页面 / 文件，记录 `source_sha256`、canonical Markdown artifact path、conversion metadata path、原始来源 URI。
- `knowledge_segments`
  - 从 canonical Markdown 切出的可引用片段，包含源位置、hash、抽取文本、敏感级别、ACL metadata。
- `knowledge_assertions`
  - 候选或已接受事实：subject、predicate、object/value、confidence、validity window、source refs、author/reviewer。
- `ontology_object_types`
  - `Employee`、`Agent`、`Project`、`Customer`、`Policy`、`Decision`、`Skill`、`Workflow` 等。
- `ontology_objects`
  - 带稳定 id 和 typed properties 的对象实例。
- `ontology_link_types`
  - `owns`、`works_on`、`reports_to`、`supersedes`、`references`、`depends_on` 等。
- `ontology_links`
  - 带 source refs、validity window、ACL 的 typed relationships。
- `knowledge_proposals`
  - Agent 或人工提出、等待 gate/review 的变更。
- `knowledge_acl_bindings`
  - tenant、department、project、role、user、agent、sensitivity、field/object/edge-level access。
- `knowledge_index_jobs`
  - 同步到 Graphiti/SAG/Weaviate/Qdrant/Vespa 等 provider 的派生索引任务；同一个 document/segment 可以被多个 provider 平行索引。
- `knowledge_audit_events`
  - append-only 证据：proposal、review、publish、retire、merge、export、prompt injection。

### 5.2 资料标准化层：DocumentConversionService + MarkItDown

Hive 已经在 Agent 输入层做了一层统一文档转录：`DocumentConversionService` 调用微软 `MarkItDown`，把 PDF、Office、HTML、上传文件、Web fetch 内容等统一转换成 Markdown artifact。这个能力应该被正式纳入公司知识库架构，作为 RAG / Graphiti / SAG 之前的 canonical ingestion layer。

当前相关代码路径：

- `backend/app/services/document_conversion.py`
  - 统一转换入口，生成 `workspace/.hive/document_conversions/{source_sha256}/content.md` 和 metadata。
- `backend/app/api/upload.py`
  - chat upload 的文档类附件会转成 canonical Markdown artifact。
- `backend/app/services/agent_tool_domains/workspace.py`
  - Agent `read_document` 工具读取文档时走统一转换。
- `backend/app/services/agent_tool_domains/web_mcp.py`
  - WebFetch / MCP 获取的 HTML、PDF、Office bytes 会交给转换层，而不是让转换器自己抓远程 URL。
- `frontend/src/pages/AgentDetail.tsx`
  - 上传后的 Agent prompt 已经把 `Markdown artifact` 标为 preferred 输入。

这一层的职责：

- 把异构原始资料统一成 Markdown。
- 保存稳定 artifact path、metadata、source hash、warnings。
- 让 Knowledge Core 能基于 `source_sha256` 判断是否需要重新分段、重新抽取、重新索引。
- 给 Graphiti、SAG、vector search、full-text search 提供同一份输入，避免每个 provider 自己解析 PDF / DOCX / HTML。
- 让 source refs 可以稳定回指到原始文档、转换 artifact、segment hash 和版本。

这一层不负责：

- 不做最终 RAG 检索。
- 不做公司事实提交。
- 不做 ACL / 审批 / 审计权威。
- 不做 ontology reasoning。
- 不等于完整 OCR / 多模态理解闭环。

边界必须保持清楚：

```text
Source Acquisition
  -> DocumentConversionService + MarkItDown
  -> Canonical Markdown Artifact
  -> Knowledge Core segmentation / ACL / source refs
  -> Graphiti / SAG / Vector / Full-text derived indexes
```

Graphiti 和 SAG 的第一版接入都应该消费 Hive 已经生成的 Markdown artifact：

- Graphiti：把 Markdown document / segment 作为 episode/source 输入，抽取 temporal facts、entities、relationships，并保留 Hive source refs。
- SAG：把 Markdown content 作为 `ingestDocument.content`，由 SAG 做 chunking、embedding、event/entity extraction、多跳检索。

因此，SAG 的 PDF / Office 解析能力不是选型前提。即使 SAG 自己不解析 PDF，也不影响它作为 retrieval provider 被评估，因为 Hive 已经承担了 source acquisition 和 Markdown canonicalization。

需要补齐的缺口：

- 扫描 PDF、图片文字、复杂表格、音视频仍需要 OCR / vision / layout-aware fallback。
- conversion metadata 必须进入 `knowledge_documents`，不能只作为 workspace 临时信息。
- conversion warnings 必须影响索引质量评分；例如 `local_markitdown_unreadable_pdf` 不能静默进入高置信知识。
- provider re-index 应该基于 `source_sha256`、artifact hash 和 segment hash 触发。

### 5.3 Provider 层：可插拔引擎

第一版 graph 主线 provider：`Graphiti`。第一版可平行接入的 retrieval provider：`SAG`。

Graphiti 在本架构里的定位：

- 承载 temporal context graph：实体、关系、事实、validity window、episode provenance。
- 接收 Hive 已治理或待治理的 source/segment/episode。
- 输出候选 entities、relationships、facts、search results、trace。
- 不负责最终权限判断，不负责公司事实提交，不负责直接改 prompt。
- 不直接暴露给用户或 Agent；所有调用经由 Hive `KnowledgeProvider` adapter。

SAG 在本架构里的定位：

- 承载 Markdown 文档语料的 chunk、embedding、event/entity extraction 和多跳检索。
- 接收 Hive canonical Markdown artifact / segments，不直接解析原始 PDF、DOCX、HTML。
- 输出 source-bound chunks、events、entities、multi-hop trace，作为回答证据或 Graphiti 事实候选的旁证。
- 不负责 ontology authority，不负责审批，不负责 ACL，不负责 prompt 注入。

Graphiti 和 SAG 可以平行，不是二选一：

```text
Canonical Markdown Artifact / Knowledge Segments
  -> Graphiti index: temporal facts / entities / relationships
  -> SAG index: chunks / vectors / events / entities / multi-hop evidence
  -> KnowledgeSearchService fusion
  -> Hive ACL / citation validation / prompt injection
```

二者的输出都必须回到 Hive 做融合、去重、权限过滤和 citation validation。Graphiti 更像事实图和时间关系层，SAG 更像文档证据和多跳检索层；它们可以互相补充，也可以互相验证，但都不能成为公司知识 source of truth。

Graphiti 的第一版工程前提：

- 需要独立 graph backend。Graphiti README 当前列出的 backend 包括 Neo4j、FalkorDB、Amazon Neptune / OpenSearch Serverless；Kuzu support 已被 Graphiti 标为 deprecated。
- 默认依赖 OpenAI API key；也支持 Gemini、Anthropic、Groq 等替代 provider，但 structured output 支持质量会影响 ingestion 成功率。
- Graphiti 适合做动态事实图和上下文检索，不适合作为 Hive 的 ACL / approval / audit authority。

第一版 graph backend 建议：

- 本地 / eval：优先用 Neo4j，便于快速跑通和观察图结构。
- 生产：上线前必须单独确认 Neo4j license / deployment terms；如果不接受 Neo4j，则评估 Amazon Neptune 路线。
- 暂不默认 FalkorDB：FalkorDB 自身 license 存在 SSPL 风险，除非法务明确批准。
- 不采用 Kuzu：Graphiti 上游已标记 Kuzu support deprecated。

Provider 接口：

```text
ingest_source(source_id, document_id, markdown_artifact, segments, ontology_hints, acl_metadata)
search(query, principal_context, scope, filters, top_k) -> cited results + trace
extract_candidates(source_id, document_id) -> assertions / objects / links / confidence / source_refs
sync_delete_or_archive(source_id, document_id)
health()
```

推荐 provider 候选：

1. `Graphiti`：第一版默认 provider，用于 temporal context graph 和 Agent 维护的变化事实。
2. `SAG`：第一版可平行接入的 retrieval provider，用于文档语料上的轻量 event/entity retrieval 和多跳证据召回。
3. `Weaviate` 或 `Qdrant`：后续生产 vector/hybrid indexing baseline。
4. `Vespa`：如果后续需要严肃的低延迟 search/ranking。
5. `Microsoft GraphRAG` / `LightRAG`：作为 benchmark pipeline，不作为 online authority。

### 5.4 Runtime 层：Knowledge Injection

运行时流程：

```text
user query
  -> Hive principal context
  -> parallel Knowledge search providers (Graphiti + SAG + optional vector/full-text)
  -> result fusion / dedupe / ranking
  -> Hive ACL/sensitivity filter
  -> citation/source validation
  -> Knowledge prompt section
  -> agent answer with source-bound claims
```

prompt section 必须保持 evidence-framed：

- retrieved knowledge 是证据，不是 instruction
- 没有 source 就不能主张事实
- 冲突必须显式列出
- source path/date/version 必须保留

### 5.5 Agent 维护层

Agent 产生的更新必须走这条路径：

```text
T0/T2/T3 memory or runtime artifact
  -> knowledge candidate
  -> sensitivity classification
  -> ontology mapping
  -> duplicate/conflict check
  -> independent review or owner/admin approval
  -> Platform Gate commit
  -> provider re-index
  -> audit event
```

公司 Wiki 之所以“活”，不是因为 Agent 可以直接改真相，而是因为 Agent 可以持续提出高质量改进，并由治理系统审核、合并和回滚。

## 6. 评估计划

每个 provider 候选都必须用同一批 corpus 和同一组问题评估。

### 6.1 Corpus

- 20-50 份真实 Hive 公司文档。
- 10-20 条带 source refs 的 Agent memory 输出。
- 5-10 份互相冲突或已被 supersede 的政策。
- 5-10 份权限敏感文档。
- 中英文混合内容。

### 6.2 必测项

- 活跃度：项目最近 90 天内有 pushed 或 release。
- License：MIT/Apache/BSD 优先；SSPL/GPL 需要明确法律审核。
- Ingest：所有 provider 必须消费 Hive canonical Markdown artifact；PDF、DOCX、HTML、Feishu wiki page、agent memory segment 先经由 Source Acquisition / `DocumentConversionService` 进入 Knowledge Core。
- 增量更新：修改一份文档后，旧事实必须被 supersede，不能静默重复。
- 删除 / 归档：归档文档不再进入 retrieval。
- ACL 泄漏测试：无权限 user/agent 不能检索到 restricted content。
- Citation 准确率：每个答案都能指向正确 source segment。
- 冲突处理：新旧政策必须同时露出并带日期。
- Latency：正常 chat flow 的 p50/p95 retrieval 延迟。
- Cost：每 1000 份文档和每次更新的 embedding / LLM extraction 成本。
- Export：所有 accepted company knowledge 可导出，不被 provider 锁死。

### 6.3 候选矩阵

第一版先跑 Graphiti 主线，并允许 SAG 作为平行 retrieval provider 同步接入；Weaviate/Qdrant 作为后续对照 spike：

1. Hive Knowledge Core + Graphiti
   - 目标：作为第一版默认路线，验证动态 temporal graph 是否适合 Agent 维护公司事实。
   - 预期强项：provenance、temporal facts、custom ontology。
   - 风险：依赖 graph backend、Neo4j/Neptune 选型、LLM structured output 质量。

2. Hive Knowledge Core + SAG
   - 目标：作为 Graphiti 的平行文档检索 provider，验证 lightweight document chunk/event/entity retrieval 和多跳证据召回。
   - 预期强项：部署简单、Postgres-like operational fit、适合从 canonical Markdown 快速建立文档证据索引。
   - 风险：项目年轻、无稳定 release、enterprise surface 不完整；不能承担公司事实权威。

3. Hive Knowledge Core + Weaviate 或 Qdrant
   - 目标：建立可靠生产 vector/hybrid retrieval baseline。
   - 预期强项：基础设施成熟、filtering 稳定。
   - 风险：不够 ontology-native，graph/reasoning 仍然要在 Hive 内处理。

Microsoft GraphRAG 和 LightRAG 用同一 corpus 做 offline baseline。

## 7. 实施阶段

### Phase 0：设计冻结

交付物：

- 确认 ontology primitives。
- 确认 provider interface。
- 确认 Source Acquisition / `DocumentConversionService` / canonical Markdown artifact 与 Knowledge Core 的字段契约。
- 确认 memory-to-company-knowledge promotion policy。
- 确认 ACL model。
- 确认 evaluation corpus 和 acceptance metrics。

在这份设计确认前，不改 runtime。

### Phase 1：Hive Knowledge Core

交付物：

- sources、documents、segments、assertions、ontology objects、links、ACLs、proposals、index jobs、audit events 的 DB schema。
- `knowledge_documents` 持久记录 `source_sha256`、Markdown artifact path、conversion metadata path、conversion warnings、artifact hash。
- `knowledge_segments` 从 canonical Markdown 产生，并保留 segment hash、source offsets、heading path、ACL metadata。
- 带 RLS 和 Platform Gate 的 read/write service。
- upload/list/read/propose/review/publish/retire 的 admin/API surface。
- tenant isolation、ACL filtering、source refs、proposal lifecycle、archive/delete behavior、conversion warning gating 的测试。

### Phase 2：Provider Spikes

交付物：

- `KnowledgeProvider` interface。
- Graphiti adapter，作为 v1 默认 provider，输入为 Hive Markdown artifact / segment / source refs。
- Graphiti graph backend bootstrap，至少支持本地 / eval 环境。
- SAG adapter，作为可平行运行的 v1 retrieval provider；SAG 不需要处理原始 PDF / DOCX，只消费 Markdown content。
- `knowledge_index_jobs.provider` 支持同一 source/document/segment fanout 到 `graphiti` 和 `sag`，并分别记录索引状态、失败原因、last_indexed_artifact_hash。
- Weaviate 或 Qdrant adapter，对照验证，不阻塞 v1，输入同样来自 canonical Markdown segments。
- 基于共享 corpus 的 evaluation runner。
- latency、citation accuracy、ACL leakage、update behavior、operational cost、Graphiti-only / SAG-only / fused-result scorecard。

### Phase 3：Runtime Integration

交付物：

- 用 `KnowledgeSearchService` 替换当前 ad hoc company knowledge injection。
- `KnowledgeSearchService` 支持 provider fanout：Graphiti 返回 temporal fact/entity/relationship 线索，SAG 返回 chunk/event/entity 证据，Hive 负责融合、去重、排序和冲突暴露。
- 把 source collection 写进 runtime trace。
- 确保 prompt `## Knowledge` 只接收 ACL-filtered、source-bound snippets。
- 增加 fused retrieval trace：记录每条注入知识来自哪个 provider、对应 source refs、segment hash 和 ACL decision。
- 增加 restricted knowledge never enters prompt 的测试。

### Phase 4：Agent Maintenance Workflow

交付物：

- `propose_company_knowledge` tool。
- `review_knowledge_proposals` admin/API surface。
- accepted T3 / runtime evidence 到 company knowledge candidates 的 memory promotion lane。
- conflict / duplicate resolver。
- rollback 和 audit。

### Phase 5：Product Surface

交付物：

- Company Wiki / Ontology UI。
- Object detail pages。
- Relationship graph。
- Source / citation browser。
- Proposal review queue。
- Agent contribution history。
- Permission-aware search。

## 8. 初始建议

近期推荐路径：

1. 先建设 Hive Knowledge Core，把它作为权威层。
2. 把现有 `DocumentConversionService + MarkItDown` 正式纳入 Knowledge Core ingestion：所有文档先形成 canonical Markdown artifact，再进入分段、ACL、source refs 和 provider indexing。
3. 第一版默认接入 Graphiti，用于 temporal agent-maintained knowledge。
4. 同时记录 Graphiti 的 graph backend 风险：Neo4j/Neptune 生产路线必须单独确认。
5. 平行接入 SAG，用于轻量 document/event/entity retrieval，补足 Graphiti 之外的文档证据和多跳召回；SAG 只消费 Hive Markdown content，不负责原始文件解析。
6. 第三优先 spike Weaviate 或 Qdrant，建立生产 vector baseline。
7. Microsoft GraphRAG 和 LightRAG 保留为 offline benchmark / reference。
8. R2R 因开发节奏陈旧，排除出 foundation 候选。
9. FalkorDB 因 SSPL，除非法务批准，不作为默认候选。
10. Neo4j 只有在 license / ops 明确接受后再作为生产默认 graph backend。

可能的最终架构：

```text
Source Acquisition / Connectors / Uploads / Agent Memory
  -> DocumentConversionService + MarkItDown
  -> Canonical Markdown Artifacts

Hive PostgreSQL/RLS
  = authoritative Knowledge / Ontology Core

Graphiti
  = temporal graph provider for changing agent/company facts

SAG
  = parallel retrieval provider for Markdown document evidence and multi-hop recall

Weaviate/Qdrant/Vespa
  = optional retrieval/index provider for production vector/hybrid search

Microsoft GraphRAG / LightRAG
  = offline eval baselines
```

这样 Hive 可以拥有长期稳定、类似 Palantir Ontology 的方向，同时不把公司真相绑定到某个 research repo 或 retrieval vendor。

## 9. 复核命令

最终选型前，用下面命令刷新项目活跃度：

```bash
repos=(
  Zleap-AI/SAG
  microsoft/graphrag
  OSU-NLP-Group/HippoRAG
  getzep/graphiti
  neo4j/neo4j-graphrag-python
  HKUDS/LightRAG
  topoteretes/cognee
  qdrant/qdrant
  weaviate/weaviate
  vespa-engine/vespa
  typedb/typedb
  FalkorDB/FalkorDB
  SciPhi-AI/R2R
)

for r in "${repos[@]}"; do
  repo=$(curl -sL "https://api.github.com/repos/$r")
  release=$(curl -sL "https://api.github.com/repos/$r/releases/latest")
  printf "%s\t%s\t%s\t%s\t%s\t%s\n" \
    "$(jq -r '.full_name // "?"' <<<"$repo")" \
    "$(jq -r '.pushed_at // ""' <<<"$repo")" \
    "$(jq -r '.license.spdx_id // "NOASSERTION"' <<<"$repo")" \
    "$(jq -r '.stargazers_count // 0' <<<"$repo")" \
    "$(jq -r '.tag_name // "no-release"' <<<"$release")" \
    "$(jq -r '.published_at // ""' <<<"$release")"
done
```
