# Personal Knowledge Base 落地实施计划

日期：2026-07-07
状态：M1 owner-scoped Personal KB 入口已落地；upload / URL 摄取仍以统一后端摄取能力为后续入口，不做前端假功能
上游文档：

- `docs/personal-knowledge-base-spec.md`
- `docs/knowledge-pyramid-agent-person-org-2026-07-03.md`
- `docs/agent-permission-governance-spec-2026-07-07.md`
- `docs/ccplus-runtime-activation-weight-design-2026-07-04.md`
- `docs/company-knowledge-ontology-plane-plan-2026-06-20.md`

## 0. 当前结论

Personal Knowledge Base 不另起主库。M1 采用当前 PostgreSQL 作为主存储，PostgreSQL `tsvector` / GIN 作为全文检索，canonical Markdown artifact 作为可回溯源文件。当前 Personal M1 不把 `pgvector` 作为必需依赖，避免本地 `postgres:15-alpine`、Railway Postgres 或新租户 bootstrap 因扩展缺失而不可启动；向量能力保留为后续统一检索层或企业 KB 的可插拔增强。

```text
source file / url / paste / agent artifact
  -> canonical Markdown artifact
  -> knowledge_documents
  -> knowledge_segments
  -> entities / assertions / links
  -> tsvector + relation graph + grant filter
  -> KnowledgeSearchService
  -> Attention Control candidate lane
  -> search_personal_kb / context injection
```

这不是重型企业 Ontology，也不是独立向量数据库。它是一个薄 Knowledge Core，先解决个人知识可入库、可检索、可授权、可被 Agent 使用、可晋升为公司 proposal。

## 1. 当前代码事实

当前代码里已经有 Runtime 接入点，但还没有知识库实体实现：

1. `backend/app/runtime/retrieval/kb_candidates.py` 定义了 `KnowledgeCandidateProvider`、`KnowledgeACLContext`、`gather_knowledge_base_candidates()`，默认 provider 返回空结果。
2. `backend/app/runtime/activation_candidates.py` 定义了 `ActivationCandidate`、`ActivationScore`、`ActivationHardMask`。
3. `backend/app/runtime/activation_router.py` 已有 hard mask、sensitivity mask、ACL mask、multi-head score。
4. `backend/app/memory/relation_graph.py` 已有纯 Python `personalized_pagerank()`，可复用为轻量 PPR 扩散。
5. 后端原先没有 `knowledge_documents`、`knowledge_segments`、`knowledge_entities`、`knowledge_assertions`、`knowledge_links`、`knowledge_index_jobs`、`knowledge_grants` 的模型或迁移。

因此本轮实施不是改现有表，而是新增 Knowledge Core 数据面，并挂接 Runtime 的 KB candidate seam。

## 2. 技术选择

### 2.1 数据库

采用：

```text
Primary DB: PostgreSQL
Full-text: PostgreSQL tsvector + GIN
Vector: Personal M1 不强依赖；企业 KB / 统一检索层可后续接入 pgvector 或 provider index
Job/cache: Redis optional
Artifact: AGENT_DATA_DIR/persons/<user_id>/kb 或未来对象存储
```

不采用：

```text
独立 Qdrant / Weaviate / Milvus 作为 M1 必需依赖
Neo4j / Graphiti 作为 Personal KB source of truth
SAG 作为生产主索引
```

理由：

1. Personal KB 的难点是 owner、agent、grant、sensitivity、source refs、audit，不是单纯向量相似度。
2. 当前 Hive 已经以 PostgreSQL/RLS 为权限真相层，Personal KB 需要和 tenant/user/agent/session 在同一事务边界内。
3. `tsvector` / GIN 足够支撑 M1 的个人规模，并能与 SQL ACL、全文检索、job 状态、审计自然融合。
4. `pgvector` 或独立向量库可以在 Company KB 或大规模场景作为 provider index，不应成为 Personal M1 source of truth。

### 2.2 HRPP/OIG 配方内化

本文把用户提到的 HRPP/OIG 先落成内部工程配方名。实际实现含义固定为：

```text
Hybrid Retrieval
  + PPR relation expansion
  + Open Information Graph / Ontology-lite extraction
```

具体表现：

1. Hybrid Retrieval：全文、实体别名、关系扩散、热度/新鲜度、多头分数融合；向量只作为可选 provider lane。
2. PPR：从 query 命中的 entity/document/segment 出发，通过 links/assertions 做轻量 Personalized PageRank。
3. OIG：不是完整企业 Ontology，而是对个人文档抽取 entity、assertion、link，形成可重建的轻量信息图。

名称以后可以统一，但实现边界不要漂移。

### 2.3 SAG 陪练

SAG 不进入生产 source of truth。它作为陪练和 benchmark：

```text
same corpus
same query set
same source refs expectation
same ACL leakage requirement
compare Hive native retrieval vs SAG trace
```

评估指标：

1. recall@k
2. MRR / nDCG
3. citation accuracy
4. ACL leakage = 0
5. p50/p95 latency
6. token cost
7. entity/event extraction quality
8. multi-hop question hit rate

## 3. 数据模型

### 3.1 Scope

Personal KB 使用统一 `knowledge_*` 表，但 scope 固定为 person。

```text
scope_type = "person"
scope_id = owner_user_id
tenant_id = tenant_id
```

这为未来 team/org 复用同一 Knowledge Core 做准备。

### 3.2 knowledge_documents

```text
id uuid pk
tenant_id uuid not null
scope_type text not null              -- person / team / org
scope_id uuid not null                -- person 时为 owner_user_id
owner_user_id uuid not null
created_by_user_id uuid not null
source_type text not null             -- upload / url / paste / agent_artifact / chat_attachment
source_uri text null
source_sha256 text not null
artifact_path text not null
artifact_hash text not null
title text not null
summary text null
status text not null                  -- uploaded / indexing / ready / degraded / archived / failed
sensitivity text not null             -- PL0 / PL1 / PL2 / PL3 / PL4
agent_searchable boolean not null default true
default_agent_scope text not null     -- owner_agents / explicit_grants / none
source_acl_snapshot jsonb not null default '{}'
metadata jsonb not null default '{}'
created_at timestamptz not null
updated_at timestamptz not null
```

约束：

```text
unique(tenant_id, scope_type, scope_id, source_sha256)
index(tenant_id, scope_type, scope_id, status)
index(owner_user_id, agent_searchable)
```

### 3.3 knowledge_segments

```text
id uuid pk
tenant_id uuid not null
document_id uuid not null
scope_type text not null
scope_id uuid not null
owner_user_id uuid not null
position integer not null
heading_path_json jsonb not null default '[]'
content text not null
segment_hash text not null
tsv tsvector null
token_count integer not null
segment_metadata_json jsonb not null default '{}'
created_at timestamptz not null
updated_at timestamptz not null
```

约束：

```text
unique(document_id, position)
unique(document_id, segment_hash)
index(tenant_id, scope_type, scope_id)
GIN(tsv)
```

### 3.4 knowledge_entities

```text
id uuid pk
tenant_id uuid not null
scope_type text not null
scope_id uuid not null
canonical_name text not null
entity_type text not null             -- free-form in person scope
aliases text[] not null default '{}'
description text null
merged_into uuid null
source_refs text[] not null default '{}'
confidence numeric not null
metadata jsonb not null default '{}'
created_at timestamptz not null
updated_at timestamptz not null
```

Person scope 不强制企业级类型。可以允许 `person`、`project`、`topic`、`preference`、`tool`、`decision`、`document` 等轻类型。

### 3.5 knowledge_assertions

```text
id uuid pk
tenant_id uuid not null
scope_type text not null
scope_id uuid not null
subject_entity_id uuid null
subject_text text not null
predicate text not null
object_entity_id uuid null
object_text text not null
segment_refs text[] not null
confidence numeric not null
valid_from timestamptz null
valid_until timestamptz null
status text not null                  -- active / superseded / contested / archived
metadata jsonb not null default '{}'
created_at timestamptz not null
updated_at timestamptz not null
```

### 3.6 knowledge_links

```text
id uuid pk
tenant_id uuid not null
scope_type text not null
scope_id uuid not null
from_kind text not null               -- document / segment / entity / assertion
from_id uuid not null
to_kind text not null
to_id uuid not null
relation text not null                -- mentions / supports / contradicts / depends_on / similar_to / about
source_refs text[] not null
weight numeric not null default 1.0
metadata jsonb not null default '{}'
created_at timestamptz not null
```

### 3.7 knowledge_grants

该表来自权限治理文档，是 Personal KB M1 必需项。

```text
id uuid pk
tenant_id uuid not null
resource_type text not null           -- document / collection / segment
resource_id uuid not null
grantee_type text not null            -- user / agent / session / collaboration_group
grantee_id text not null
actions text[] not null               -- search / read / cite / inject / write / propose / export
sensitivity_ceiling text not null
purpose text null
allowed_session_id uuid null
expires_at timestamptz null
revoked_at timestamptz null
created_by_user_id uuid not null
audit_reason text not null
metadata jsonb not null default '{}'
created_at timestamptz not null
updated_at timestamptz not null
```

### 3.8 knowledge_index_jobs

```text
id uuid pk
tenant_id uuid not null
document_id uuid not null
stage text not null                   -- convert / segment / embed / extract / link / ready
status text not null                  -- queued / running / succeeded / failed / retrying
artifact_hash text not null
attempt_count integer not null default 0
error_code text null
error_message text null
started_at timestamptz null
finished_at timestamptz null
metadata jsonb not null default '{}'
created_at timestamptz not null
updated_at timestamptz not null
```

### 3.9 与飞书权限映射的兼容契约

Personal KB M1 必须直接兼容飞书式权限模型，避免后续接入飞书文档/知识库时重构。

| 飞书概念 | Personal KB 字段/机制 | 约束 |
| --- | --- | --- |
| Creator | `created_by_user_id` | 记录谁导入/创建，不代表永久 manage 权限。 |
| Owner | `owner_user_id` | Personal scope 的最终责任主体。 |
| Collaborator | `knowledge_grants` | 用户、Agent、session、协作组都通过 grant 表表达。 |
| Read/Edit/Manage | `actions[]` | 不把 read、inject、export 混成一个权限。 |
| App/Bot 权限 | `grantee_type=agent` | Agent 只能作为被委托主体。 |
| Link sharing / 临时访问 | `grantee_type=session` + `expires_at` | 默认只支持会话级临时授权；公开链接后置。 |
| 外部分享设置 | `source_acl_snapshot` | 保存来源权限快照，不自动放大 Hive 内权限。 |
| 复制/下载/打印 | `action=export` | export 必须单独授权。 |
| 评论/建议 | `action=propose` | propose 只产生候选，不直接改真相。 |
| 文档密级 | `sensitivity` + `sensitivity_ceiling` | 密级先 hard mask，再参与检索排序。 |
| 所有权转移 | update `owner_user_id` + audit | creator/source refs 不随 owner 转移丢失。 |

后续接飞书时只能新增 adapter：

```text
Feishu document permission
  -> source_acl_snapshot
  -> knowledge_grants
  -> sensitivity / export policy
  -> KnowledgeSearchService permission filter
```

禁止路径：

```text
Feishu permission
  -> runtime direct allow
  -> prompt injection bypass
  -> separate shadow ACL table
```

也就是说，Personal KB 的权限内核就是未来飞书权限映射的落点，不需要再重做一套关系层。

## 4. Artifact 存储

Person root：

```text
{AGENT_DATA_DIR}/persons/<owner_user_id>/kb/
  artifacts/<source_sha256>/
    content.md
    meta.json
    extraction.json
  notes/<note_id>.md
```

规则：

1. DB 是查询和治理真相；artifact 是可回溯正文和重建来源。
2. `content.md` 是 canonical Markdown，不保存原始二进制。
3. 原始上传文件若需要保留，放 object storage 或 workspace artifact，不作为检索主输入。
4. DB row 必须记录 `artifact_path`、`artifact_hash`、`source_sha256`。
5. 删除默认是 archive/de-index，不物理删除 artifact，除非执行合规删除流程。

## 5. 切片规则

### 5.1 切片顺序

```text
canonical Markdown
  -> block normalize
  -> heading sections
  -> retrieval segments
  -> full-text index input
  -> optional semantic index input
  -> entity/assertion/link extraction
```

### 5.2 Section 规则

1. 按 Markdown heading 切 section，保留 `heading_path`。
2. 无 heading 的文档按段落聚合成 synthetic section。
3. 表格、代码块、列表优先保持完整。
4. 图片、附件、二进制内容只写 caption/metadata/source ref，不直接进入 segment 正文。

### 5.3 Segment 规则

建议参数：

```text
target_segment_tokens = 650
min_segment_tokens = 220
max_segment_tokens = 900
overlap_tokens = 100
large_table_policy = summarize_with_ref
large_code_policy = preserve_if_under_limit_else_ref
```

规则：

1. section 小于 900 tokens，直接作为一个 segment。
2. section 超过 900 tokens，按语义段落切到 500-800 tokens。
3. overlap 只跨相邻 segment，不能跨 heading 大段落。
4. 不为了固定 token 数切断一句话、表格行、代码块。
5. 每个 segment 必须可追溯到 `content.md` 的 heading/path/source hash。

### 5.4 Full-text 与可选语义索引输入

全文索引输入不等于 segment 裸正文。Personal M1 的默认索引输入是 PostgreSQL `tsvector`，可选 semantic/vector provider 只能消费同一份规范化输入，不能成为新的 source of truth。

```text
index_text =
  title
  + heading_path
  + detected_entities / aliases
  + key assertions
  + segment content
```

原因：

1. 个人知识很多是短笔记或上下文依赖内容，裸正文语义不足。
2. heading_path 能显著提高召回方向。
3. entity/alias 能减少“同义词搜不到”的问题。

### 5.5 Hash

```text
source_sha256 = 原始输入规范化后的 hash
artifact_hash = canonical Markdown content hash
segment_hash = segment content hash
grant decision = sensitivity + grant + principal stack runtime 判定
```

Hash 用于：

1. 去重。
2. re-index 判定。
3. optional provider index 同步。
4. source refs 证据链。

## 6. 写入链路

### 6.1 输入入口

M1 支持：

1. 上传文件。
2. 粘贴文本。
3. URL 导入。
4. Agent artifact 手动保存。
5. 聊天附件入库。

Agent 自动把自己的记忆写入 Personal KB 不作为默认行为，必须走 proposal 或 Owner 确认。

### 6.2 Pipeline

```text
ingest request
  -> resolve principal / owner
  -> source acquisition
  -> canonical Markdown conversion
  -> document upsert by scope + source_sha256
  -> create index job
  -> segmentation
  -> tsvector refresh
  -> LLM extraction: entities / assertions / links
  -> relation materialization
  -> status ready / degraded / failed
  -> audit event
```

### 6.3 Degraded 状态

部分失败不应全盘失败。

```text
conversion ok + full-text refresh failed -> degraded_text_index_missing
conversion ok + extraction failed -> degraded_graph_missing
conversion failed -> failed_conversion
ACL missing -> failed_policy
```

检索时 degraded 文档可通过可用 index 参与，但 tool result 要带 warning。

## 7. 搜索链路

### 7.1 KnowledgeSearchService

新增统一服务：

```text
KnowledgeSearchService.search(
  query,
  scope_type,
  scope_id,
  principal_stack,
  agent_context,
  action,
  top_k,
  filters,
) -> KnowledgeSearchResult
```

### 7.2 检索头

```text
lexical_head: PostgreSQL FTS / title / heading / aliases
optional_vector_head: provider index / pgvector ANN, only when explicitly enabled
entity_head: entity canonical_name / aliases
relation_head: PPR over knowledge_links
freshness_head: updated_at / explicit boost
authority_head: source_refs / confidence / status
permission_head: hard mask
```

### 7.3 融合规则

先 hard mask，再排序。

```text
candidates = lexical ∪ optional_vector ∪ entity
visible = apply_scope_acl_sensitivity_grants(candidates)
expanded = relation_ppr_expand(visible)
ranked = RRF(lexical, optional_vector, entity, expanded)
ranked = apply_boosts(ranked, freshness, authority, usage_heat)
return top_k with refs and trace
```

拒绝项不能把标题或摘要泄露给模型。debug trace 只给 owner/admin UI。

### 7.4 Tool result

`search_personal_kb` 返回：

```json
{
  "results": [
    {
      "document_id": "...",
      "segment_id": "...",
      "title": "...",
      "snippet": "...",
      "source_ref": "kb://person/<owner>/<doc>#seg=<id>",
      "score": 0.83,
      "score_trace": {
        "lexical": 0.2,
        "optional_vector": 0.0,
        "entity": 0.4,
        "relation": 0.1
      }
    }
  ],
  "trace_id": "...",
  "warnings": []
}
```

超过 50KB 的结果必须落 artifact，tool result 只返回引用。

## 8. Attention Control 接入

Personal KB 是 Attention Control 的 candidate lane，不是独立 prompt 注入系统。

```text
ActivationQuery
  -> gather_knowledge_base_candidates(scope=personal)
  -> KnowledgeCandidateProvider.search()
  -> ActivationCandidate(kind=knowledge_base)
  -> ActivationRouter hard mask / score
  -> selected retrieval_context
  -> prompt assembly manifest
```

要求：

1. Personal KB 不常驻 frozen prefix。
2. Personal KB 只进入 dynamic suffix / retrieval context。
3. 每个 selected candidate 必须有 `source_refs`。
4. 每个 suppressed candidate 必须有 reason，但不给模型正文。
5. prompt manifest 记录 selected/suppressed counts、score heads、permission reason。

## 9. API 与工具

### 9.1 Backend API

建议新增：

```text
POST   /api/knowledge/documents/upload
POST   /api/knowledge/documents/paste
POST   /api/knowledge/documents/ingest-url
GET    /api/knowledge/documents
GET    /api/knowledge/documents/{id}
PATCH  /api/knowledge/documents/{id}
POST   /api/knowledge/search
GET    /api/knowledge/jobs/{id}
POST   /api/knowledge/grants
DELETE /api/knowledge/grants/{id}
POST   /api/knowledge/proposals
```

所有 API 必须显式带 scope，M1 默认 `person`。

### 9.2 Agent Tool

新增工具：

```text
search_personal_kb(query, top_k?, filters?)
save_to_personal_kb(source_ref | text | artifact_path, title?, sensitivity?, agent_searchable?)
propose_personal_kb_update(document_id?, patch | note)
```

M1 可先只开放 `search_personal_kb` 和用户确认后的 `save_to_personal_kb`。

### 9.3 Tool Governance

工具必须经过：

```text
CAPABILITY_MAP
ToolRuntimeService.execute()
PermissionDecision
KnowledgeSearchService permission filter
Audit event
```

不得从 tool handler 直接查表绕过权限。

## 10. 前端 IA

入口：

```text
/knowledge
```

`/workspace/knowledge` 作为历史设计稿兼容路径重定向到 `/knowledge`，不是新的产品面。

M1 页面：

1. Inbox：当前实装 Markdown / notes 直投；上传、URL、Agent artifact 必须以后端统一摄取能力为真相打开，不能先做前端假入口。
2. Library：文档列表、状态、敏感度、agent_searchable。
3. Search：全局检索，展示 trace。
4. Document Detail：MD 预览、segments、entities、refs、被哪些 Agent 使用。
5. Permissions：授权给哪些 Agent、session grant、过期时间。
6. Image/Media：后置，可先只保留 metadata。

## 11. 测试矩阵

### 11.1 数据与迁移

1. Alembic 单 head。
2. `knowledge_documents` unique 去重。
3. RLS 下跨 tenant 不可见。
4. `agent_searchable=false` 不进入 Agent search。
5. PL4 写入被拒绝或剥离。

### 11.2 切片

1. heading_path 保留。
2. 表格不被中间截断。
3. 大 section 分段并有 overlap。
4. segment hash 稳定。
5. artifact_hash 改变触发 re-index。

### 11.3 搜索

1. FTS 命中标题/heading。
2. optional vector provider 命中语义相似段落时，只能作为加分通道，不能绕过 ACL。
3. alias 命中 entity。
4. relation PPR 扩散命中相邻证据。
5. RRF 融合顺序稳定。

### 11.4 权限

1. Owner 自己的 Agent 可搜允许文档。
2. Owner 公共 Agent 无 grant 不可搜。
3. session grant 过期后不可搜。
4. PL3 不跨 owner。
5. denied candidate 不泄露正文。

### 11.5 Runtime

1. `gather_knowledge_base_candidates()` 接入 provider 后返回 `ActivationCandidate`。
2. Activation Router suppresses sensitivity denied candidates。
3. prompt manifest 记录 selected KB candidate。
4. dynamic suffix 包含 refs，不包含无权限内容。

## 12. 原子施工项

这不是分阶段交付，而是同一轮完整落地里的原子项顺序。

1. 新增 Alembic：`knowledge_documents`。
2. 新增 Alembic：`knowledge_segments` + `tsv` GIN index。
3. 新增 Alembic：`knowledge_entities`。
4. 新增 Alembic：`knowledge_assertions`。
5. 新增 Alembic：`knowledge_links`。
6. 新增 Alembic：`knowledge_grants`。
7. 新增 Alembic：`knowledge_index_jobs`。
8. 新增 SQLAlchemy models。
9. 新增 RLS policies。
10. 新增 artifact path resolver。
11. 新增 canonical Markdown ingestion service。
12. 新增 segmentation service。
13. 新增 optional embedding job boundary，不作为 Personal M1 启动依赖。
14. 新增 extraction prompt/service。
15. 新增 KnowledgeSearchService。
16. 新增 permission decision integration。
17. 新增 PersonalKnowledgeCandidateProvider。
18. 接入 `gather_knowledge_base_candidates()`。
19. 新增 `search_personal_kb` tool。
20. 新增 upload/paste/url APIs。
21. 新增 document list/detail/search APIs。
22. 新增 grants APIs。
23. 新增 frontend workspace knowledge entry。
24. 新增 document detail + permission panel。
25. 新增 SAG benchmark harness。
26. 新增 tests：schema/RLS。
27. 新增 tests：segmentation。
28. 新增 tests：search/ranking。
29. 新增 tests：permission/ACL。
30. 新增 tests：runtime Attention Control integration。

## 13. 验收口径

Personal KB M1 完成必须满足：

1. 用户可以上传/粘贴/URL 入库。
2. 文档转换成 canonical MD artifact。
3. 文档切片、全文、实体、关系索引可重建；向量通道如果开启，也必须从 canonical artifact 重建。
4. Owner 的 Agent 可以通过 `search_personal_kb` 搜索。
5. 权限过滤在 DB search、tool result、prompt injection 三处一致。
6. Attention Control 能把 Personal KB 作为 candidate lane，而不是绕开 Router。
7. denied content 不泄露。
8. 每条注入内容都有 source ref。
9. SAG 陪练 scorecard 可跑。
10. 当前系统没有另起 shadow truth store。

### 13.1 M1 闭环缺口修复清单（2026-07-08）

本节作为当前修复轮的准入和验收口径。以下条目属于 M1 完成条件，不得再以 M2 后置名义挂账；每完成一项必须更新本节证据并单独 commit。

| 原子项 | 当前判定 | 必须完成的实装边界 | 证据要求 |
| --- | --- | --- | --- |
| M1-G1 多跳 PPR 检索 | 未闭环 | `search_personal()` 的 graph channel 必须从命中实体出发构建 person-scope 子图，复用 `personalized_pagerank()` 做多跳扩散；结果仍经过 document/segment ACL 过滤并返回 `score_trace.channels.graph`。 | 单测覆盖二跳命中；Personal KB service 测试通过。 |
| M1-G2 异步 ingestion job | 未闭环 | upload / URL / paste 入口先创建 `knowledge_index_jobs(status=queued)` 并返回 job；后台 worker 负责转换、切片、抽取、索引、失败重试和状态推进。同步 helper 只能作为 worker 内部执行器或测试便利路径。 | API 测试证明入口不阻塞抽取；service 测试覆盖 queued/running/ready/failed/degraded/retry；现有导入测试通过。 |
| M1-G3 optional vector boundary | 未闭环 | Personal M1 不强依赖 pgvector，但必须有可插拔 vector provider 边界：未配置时可观测降级；配置后从 canonical segment 输入生成/查询 vector lane，且不成为 source of truth。 | 单测覆盖无 provider 降级和 provider 命中融合；迁移仍不强制 `CREATE EXTENSION vector`。 |
| M1-G4 extractor 独立测试 | 未闭环 | `PersonalKnowledgeLLMExtractor` 的 JSON 提取、schema 解析、敏感级别跳过、空响应/坏 JSON 失败路径必须有独立测试文件。 | 新增 `test_personal_knowledge_extractor.py` 并通过。 |
| M1-G5 SAG benchmark scorecard | 未闭环 | 新增 Hive-native vs SAG-trace 的轻量 benchmark harness；SAG 只作为陪练输入/对照，不进入生产 truth 或检索 provider。输出 recall@k、citation accuracy、ACL leakage、latency、cost 的 scorecard。 | 新增 scorecard 单测和可执行脚本/模块；文档记录命令与通过结果。 |

## 14. 实施证据

### 14.0 M1 闭环缺口修复证据日志（2026-07-08）

| 原子项 | 状态 | Commit | 证据 |
| --- | --- | --- | --- |
| M1-G1 多跳 PPR 检索 | 待修复 | - | - |
| M1-G2 异步 ingestion job | 待修复 | - | - |
| M1-G3 optional vector boundary | 待修复 | - | - |
| M1-G4 extractor 独立测试 | 待修复 | - | - |
| M1-G5 SAG benchmark scorecard | 待修复 | - | - |

### 14.1 数据面、迁移、RLS

已落地：

1. `backend/app/models/knowledge.py` 新增 7 张核心表的 ORM 模型：
   `knowledge_documents`、`knowledge_segments`、`knowledge_entities`、`knowledge_assertions`、`knowledge_links`、`knowledge_index_jobs`、`knowledge_grants`。
2. `backend/alembic/versions/personal_knowledge_core_0707.py` 新增对应迁移，`down_revision = external_extension_catalog_entries_0707`，当前 Alembic head 为 `personal_knowledge_core_0707`。
3. `backend/app/db_bootstrap.py` 将 7 张表加入 `RLS_FORCED_TENANT_TABLES`，fresh `Base.metadata.create_all` bootstrap 与正常 Alembic 路径都不会绕过 owner-role RLS。
4. Personal M1 使用 PostgreSQL 原生 `tsvector` / GIN：`knowledge_segments.tsv` + `ix_knowledge_segments_tsv_gin`。未引入 `pgvector` Python 依赖，也未创建 `vector` 扩展。

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/models/test_knowledge_records.py \
  tests/migrations/test_personal_knowledge_core_migration.py \
  tests/services/test_audit_rls_coverage.py::test_force_all_tenant_rls_migration_covers_bootstrap_force_tables \
  tests/test_alembic_bootstrap.py::test_personal_knowledge_tables_are_forced_rls_on_fresh_bootstrap_path -q
# 10 passed in 0.26s

ruff check app/models/knowledge.py app/db_bootstrap.py \
  tests/models/test_knowledge_records.py \
  tests/migrations/test_personal_knowledge_core_migration.py \
  tests/services/test_audit_rls_coverage.py \
  tests/test_alembic_bootstrap.py
# All checks passed!

alembic heads
# personal_knowledge_core_0707 (head)
```

### 14.2 Ingest / Search 服务层

已落地：

1. `backend/app/services/personal_knowledge_service.py` 新增 `PersonalKnowledgeService`。
2. `personal_knowledge_artifact_path()` 固定 person-scope artifact 路径：
   `AGENT_DATA_DIR/persons/<owner_user_id>/kb/documents/<sha-prefix>/<source_sha256>.md`。
3. `segment_markdown()` 使用稳定切片规则，保留 heading path、segment hash 和 rough token count。
4. `ingest_markdown()` 写 canonical Markdown artifact，按 `tenant_id + scope_type + scope_id + source_sha256` upsert `knowledge_documents`，重建 `knowledge_segments`，写 `knowledge_index_jobs`，并刷新 `KnowledgeSegment.tsv = to_tsvector('simple', content)`。
5. `build_personal_knowledge_search_statement()` 使用 person scope、`agent_searchable`、`status=ready`、PostgreSQL FTS 和 grant ACL。Owner 自己的查询不要求 grant；非 owner 的 user/agent 必须命中 `knowledge_grants` 的 scope/document 授权。
6. `search_personal()` 返回 `KnowledgeSearchHit`，包含 `document_id`、`segment_id`、`snippet`、`source_ref`、`score`、`heading_path` 和 metadata。

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_personal_knowledge_service.py -q
# 6 passed in 0.13s

ruff check app/services/personal_knowledge_service.py tests/services/test_personal_knowledge_service.py
# All checks passed!
```

### 14.3 Tool / Runtime Attention Control 接入

已落地：

1. `backend/app/tools/handlers/knowledge.py` 新增 `search_personal_kb`，通过 `ToolExecutionRequest.context` 读取 `agent_id/user_id/tenant_id`，再由 `Agent.owner_user_id/sponsor_user_id/creator_id` 解析 owner scope；不接受模型传入 owner。
2. `backend/app/tools/collector.py` 将 `app.tools.handlers.knowledge` 加入 `HANDLER_MODULES`，`search_personal_kb` 进入 OpenAI tool schema、seed list、execution registry、read-only set、parallel-safe set。
3. `backend/app/runtime/retrieval/personal_knowledge_provider.py` 新增 `PersonalKnowledgeCandidateProvider`，把 `KnowledgeSearchHit` 映射为 `KnowledgeCandidateRecord`。
4. `backend/app/runtime/retrieval/kb_candidates.py` 在 `KnowledgeCandidateRecord -> ActivationCandidate` 转换时写入 `acl_scope = record.scope`，避免 Personal KB candidate 被 Activation Router 误判为 company scope。
5. `backend/app/runtime/invoker.py` 新增 `_record_knowledge_activation_for_request()`，在 ActivationQuery 构建后执行 Personal KB gather -> Activation Router -> RuntimeAssemblyState 记录。插件 hook 可加上下文，但 Personal KB 的 Q/gather/route 是 runtime native 路径。

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_personal_knowledge_provider.py \
  tests/tools/test_personal_knowledge_tool.py \
  tests/runtime/test_personal_knowledge_activation.py -q
# 5 passed, 4 warnings in 0.23s

pytest tests/runtime/test_kb_candidates.py -q
# 3 passed, 4 warnings in 0.10s

ruff check app/runtime/retrieval/personal_knowledge_provider.py \
  app/runtime/retrieval/kb_candidates.py \
  app/tools/handlers/knowledge.py \
  app/tools/collector.py app/tools/registry.py app/runtime/invoker.py \
  tests/runtime/test_personal_knowledge_provider.py \
  tests/tools/test_personal_knowledge_tool.py \
  tests/runtime/test_personal_knowledge_activation.py \
  tests/runtime/test_kb_candidates.py
# All checks passed!
```

### 14.4 Personal KB API 入口

已落地：

1. `backend/app/api/agent_knowledge.py` 在既有 `/agents/{agent_id}/knowledge` router 下新增 Personal KB thin-kernel API：
   - `GET /personal/documents`
   - `POST /personal/documents`
   - `GET /personal/search?q=...`
   - `GET /personal/documents/{document_id}`
2. API 不接受客户端传入 owner。owner scope 统一从 `Agent.owner_user_id -> sponsor_user_id -> creator_id` 解析。
3. 写入端只允许当前用户等于 Personal KB owner；非 owner 即使拥有 agent manage/use 权限也不能写 person scope。
4. list/detail/search 复用 service 层 owner-or-grant ACL，和 `search_personal_kb` tool / runtime candidate lane 使用同一套 `knowledge_grants` 判定。
5. `PersonalKnowledgeService` 新增 `list_personal_documents()` 与 `get_personal_document()`，用于前端文库、详情、段落 evidence 展示。

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_personal_knowledge_service.py tests/api/test_agent_personal_knowledge_api.py -q
# 13 passed in 0.29s

ruff check app/api/agent_knowledge.py app/services/personal_knowledge_service.py \
  tests/services/test_personal_knowledge_service.py \
  tests/api/test_agent_personal_knowledge_api.py
# All checks passed!
```

### 14.5 Workspace 顶层 Personal KB 入口（2026-07-08 修正）

已落地：

1. 后端新增 owner-scoped thin API，主语义仍由 `PersonalKnowledgeService` 和 Knowledge Core 表决定：
   - `GET /api/knowledge/personal/documents`
   - `POST /api/knowledge/personal/documents`
   - `GET /api/knowledge/personal/search`
   - `GET /api/knowledge/personal/documents/{document_id}`
2. agent-scoped API 保留为 Agent 消费/调试视角，不再承担 Personal KB 主入口：
   - `GET /api/agents/{agent_id}/knowledge/personal/documents`
   - `POST /api/agents/{agent_id}/knowledge/personal/documents`
   - `GET /api/agents/{agent_id}/knowledge/personal/search`
   - `GET /api/agents/{agent_id}/knowledge/personal/documents/{document_id}`
3. `frontend/src/api/domains/knowledge.ts` 现在同时提供两组 client：
   - workspace/owner-scoped：
     - `myPersonalDocuments()`
     - `myPersonalIngest()`
     - `myPersonalSearch()`
     - `myPersonalDocument()`
   - agent-scoped：
   - `personalDocuments()`
   - `personalIngest()`
   - `personalSearch()`
   - `personalDocument()`
4. `frontend/src/pages/layout/AppSidebar.tsx` 在 workspace 顶层 nav 中加入 `知识库`，位置与 `Agent圈`、`任务 / 自动化`、`Bridge` 同级；路由为 `/knowledge`。
5. `frontend/src/App.tsx` 新增 `/knowledge` 页面，并把历史设计稿路径 `/workspace/knowledge` 重定向到 `/knowledge`。
6. `frontend/src/pages/PersonalKnowledge.tsx` / `.css` 新增 Owner 级 Personal KB 工作台。当前实装能力：
   - owner 粘贴 Markdown / notes 入库；
   - document list；
   - query search；
   - document detail + segment evidence；
   - source refs 可见；
   - `知识网` / `画像` / `授权` 作为同一 Personal Knowledge plane 内部入口呈现，不新增第 4 个产品；
   - `企业库（只读）` 只作为只读/晋升方向入口，不在 Personal 页面管理 Company KB。
7. UI 不传 `owner_user_id`；owner 由后端 `current_user` 解析。agent-scoped 旧入口仍由后端从 agent 解析 owner。这与权限 spec 和 A2A 预留入口保持一致。
8. `frontend/src/i18n/en.json` 与 `frontend/src/i18n/zh.json` 已补齐文案。

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/api/test_agent_personal_knowledge_api.py -q
# 5 passed in 0.48s

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- src/api/domains/knowledge.test.ts src/pages/layout/LayoutSections.test.tsx src/pages/PersonalKnowledge.test.tsx
# Test Files  3 passed (3)
# Tests  18 passed (18)
```

红测证据：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/api/test_agent_personal_knowledge_api.py -q
# initially failed: AttributeError: module 'app.api.agent_knowledge' has no attribute 'personal_router'

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- src/api/domains/knowledge.test.ts src/pages/layout/LayoutSections.test.tsx src/pages/PersonalKnowledge.test.tsx
# initially failed: myPersonalDocuments is not a function; Cannot find module './PersonalKnowledge'; AppSidebar missing Knowledge
```

### 14.6 组合回归与断点检查

本轮回归覆盖：

1. 插件系统 hook 修复点：custom tool executor 不再重复触发内部 runtime hook。
2. Personal KB schema / migration / RLS。
3. Personal KB ingest / search / list / detail service。
4. Personal KB API。
5. `search_personal_kb` tool。
6. Runtime Attention Control candidate lane。
7. Activation Router 的 personal scope 记录。
8. 前端 Workspace 顶层 Knowledge 入口、owner-scoped Personal KB API 与 UI。
9. Agent Detail 的 Personal KB 子视图仍保留，作为 agent 消费/调试入口，不再是主入口。

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/models/test_knowledge_records.py \
  tests/migrations/test_personal_knowledge_core_migration.py \
  tests/services/test_personal_knowledge_service.py \
  tests/api/test_agent_personal_knowledge_api.py \
  tests/runtime/test_personal_knowledge_provider.py \
  tests/tools/test_personal_knowledge_tool.py \
  tests/runtime/test_personal_knowledge_activation.py \
  tests/runtime/test_kb_candidates.py \
  tests/services/test_audit_rls_coverage.py::test_force_all_tenant_rls_migration_covers_bootstrap_force_tables \
  tests/test_alembic_bootstrap.py::test_personal_knowledge_tables_are_forced_rls_on_fresh_bootstrap_path \
  tests/runtime/test_invoker.py::test_custom_tool_executor_disables_inner_runtime_hooks \
  tests/services/test_agent_message_runtime.py::test_build_agent_message_tool_executor_persists_tool_calls -q
# 35 passed, 4 warnings in 0.53s

ruff check app/models/knowledge.py app/db_bootstrap.py \
  app/services/personal_knowledge_service.py app/api/agent_knowledge.py \
  app/runtime/retrieval/personal_knowledge_provider.py \
  app/runtime/retrieval/kb_candidates.py app/tools/handlers/knowledge.py \
  app/tools/collector.py app/tools/registry.py app/runtime/invoker.py app/main.py \
  app/services/agent_tool_domains/messaging.py \
  tests/models/test_knowledge_records.py \
  tests/migrations/test_personal_knowledge_core_migration.py \
  tests/services/test_personal_knowledge_service.py \
  tests/api/test_agent_personal_knowledge_api.py \
  tests/runtime/test_personal_knowledge_provider.py \
  tests/tools/test_personal_knowledge_tool.py \
  tests/runtime/test_personal_knowledge_activation.py \
  tests/runtime/test_kb_candidates.py \
  tests/runtime/test_invoker.py tests/services/test_agent_message_runtime.py
# All checks passed!

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- src/api/domains/knowledge.test.ts src/pages/layout/LayoutSections.test.tsx src/pages/PersonalKnowledge.test.tsx src/pages/agent-detail/AgentKnowledgeSection.test.tsx
# Test Files  4 passed (4)
# Tests  22 passed (22)

npm run build
# tsc && vite build
# ✓ built in 2.60s
```

结论：

1. Personal KB 没有绕开 Attention Control：runtime candidate lane、tool search、API search 均从同一 Knowledge Core 表读取，并受 owner/grant 约束。
2. Personal KB 没有引入第 4 个产品：顶层 `知识库` 是 Personal Knowledge / Knowledge LM 的主入口；persona/profile/taste 仍属于该入口内部 plane。
3. 权限入口已预留且可执行：`knowledge_grants` 对 user/agent + scope/document 建模；owner 不需要 grant，非 owner 必须 grant。
4. 当前 M1 没有向量库硬依赖：Personal 先用 PostgreSQL `tsvector` / GIN 和 canonical Markdown artifact，后续企业 KB 可在同一 schema 上扩 Ontology / vector provider。
5. Company KB 不在 Personal KB 页面内管理；Personal 页面只保留只读/晋升方向入口，避免后续飞书式权限映射时重构。
