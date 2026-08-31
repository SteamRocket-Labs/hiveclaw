# Personal Knowledge Base 能力重基线与配置闭环

日期：2026-07-09
状态：当前事实复核 + 下一轮实现施工口径
适用范围：Personal KB / Knowledge LM，不替代 Agent Memory，不接管 Enterprise KB / Ontology。
当前代码快照：`512200142`

## 0. 本文要解决的问题

此前 Personal KB 文档里有两类内容混在一起：

1. 目标形态：Open Notebook / NotebookLM 级别的个人资料库、总结、索引、问答、图谱、授权、运行时注入。
2. 当前 M1 thin core：owner-scoped 导入队列、canonical Markdown、稳定切片、PostgreSQL 全文检索、轻图谱抽取、可选 vector provider 插口、授权表和前端入口。

这导致几个概念不够清楚：

- “可选 LLM 抽取实体关系断言图谱”里的“可选”到底是产品可选，还是 provider 未配置时降级。
- “取决于租户模型设置”到底使用哪个租户设置，是否会从单 agent 模型设置轮空。
- 没有 LLM / embedding / 多模态模型时，资料导入后哪些步骤还能跑，哪些必须失败或 degraded。
- 图片、音频、视频等格式如果没有多模态或专用转写 provider，系统不能伪装成已总结、已索引。
- OpenKnowledge 不是 Markdown renderer，而是 AI-native Markdown vault / LLM Wiki / agent workspace，Personal 模块需要重新吸收它的产品形态。

本文把当前真实机制、目标配置面、OpenKnowledge / Open Notebook 取舍、以及后续实现边界重新拉齐。

## 1. 当前事实结论

### 1.1 一句话结论

当前 Personal KB 是一个已经可运行的薄核，不是完整 AI-native Personal Knowledge 产品。

已经落地：

- owner-scoped Personal KB API：`/api/knowledge/personal/*`
- paste / upload / URL 导入队列
- canonical Markdown artifact
- Markdown heading-aware 切片
- `knowledge_documents` / `knowledge_segments` / `knowledge_entities` / `knowledge_assertions` / `knowledge_links` / `knowledge_index_jobs` / `knowledge_grants`
- PostgreSQL `tsvector` 全文索引
- LLM 实体 / 断言 / 关系抽取器
- optional vector provider 插口
- media provider 插口
- graph / entity / text / optional vector 融合检索
- owner / grant / agent_searchable 过滤
- Personal KB 前端入口、文库、导入任务、搜索、图谱、授权基础 UI

仍然缺：

- 租户级 Personal KB 模型配置面
- embedding provider 的后台配置和默认实例化
- media provider 的后台配置和默认实例化
- 图片 OCR / vision caption / 音频转写 / 视频转写与关键帧理解
- 前端 capability status 明示
- “无法总结 / 无法语义索引 / 无法处理多模态”的红色状态
- OpenKnowledge 式 vault/editor/wiki-link/agent co-edit 产品体验
- Open Notebook 式 transformations / notes / ask-vs-chat / context control

### 1.2 “没有 LLM 就无法索引”需要拆开说

不准确。

没有 LLM 时，当前系统仍能做：

- 导入任务入队
- 原始文件 spool
- 普通文档转 Markdown
- canonical Markdown 保存
- deterministic segmentation
- PostgreSQL `tsvector` / `plainto_tsquery` 全文检索
- ACL / grant / sensitivity / `agent_searchable` 过滤

没有 LLM 时不能做：

- LLM summary
- entity / assertion / relation extraction
- transformation / insight generation
- profile / taste / cognitive-style synthesis
- RAG answer synthesis

没有 embedding provider 时不能做：

- semantic vector search
- semantic similarity dedupe
- clustering / related notes by meaning
- embedding-based alias alignment

没有 multimodal / OCR / STT / video provider 时不能做：

- image OCR / caption / visual understanding
- audio transcription
- video speech transcription
- video keyframe / visual scene summary
- screenshot / scanned PDF semantic extraction

因此正确产品语义应该是：

> 没有模型配置时，系统可以保留基础导入和全文检索，但不能声称完成了总结、语义索引、多模态理解或 AI-native 知识提炼。

## 2. 当前资料进入后的真实机制

### 2.1 入口

当前入口在 `backend/app/api/agent_knowledge.py`：

- `POST /api/knowledge/personal/documents`：Markdown / paste 入库。
- `POST /api/knowledge/personal/imports`：multipart 文件入库。
- `POST /api/knowledge/personal/import-url`：URL 入库。
- `GET /api/knowledge/personal/import-jobs`：导入任务列表。
- `POST /api/knowledge/personal/import-jobs/{job_id}/retry`：失败或 degraded 任务重试。

所有 owner-scoped 写入都由后端从 `current_user` 解析 owner，不接受浏览器传入 `owner_user_id`。

### 2.2 队列与后台处理

`PersonalKnowledgeService` 先写入 `knowledge_index_jobs(status=queued)`，再由 BackgroundTasks / daemon 共用同一 processing path。

关键函数：

- `queue_markdown_import()`：规范化 Markdown，写 artifact，创建 queued job。
- `queue_source_bytes_import()`：文件写 spool，创建 queued job，不在请求线程转换。
- `queue_url_import()`：校验 URL，创建 queued job。
- `process_import_jobs()`：claim queued / failed / stuck running job。
- `_process_queued_import_job()`：按 queued kind 分发到 markdown / source bytes / URL ingest。

### 2.3 文档转换

普通文件路径：

```text
source bytes
  -> DocumentConversionService.convert_bytes()
  -> MarkItDown local conversion
  -> fallback legacy extractors: text/html/pdf/docx/xlsx/pptx
  -> markdown
  -> ingest_markdown()
```

当前 `DocumentConversionService` 明确写入：

- `used_ocr=False`
- `used_vision=False`

也就是说，普通文档转换现在不是多模态理解；它主要是文档到 Markdown 的本地提取。

### 2.4 Media 文件

media 文件路径：

```text
image / audio / video extension
  -> _ingest_media_bytes()
  -> media_provider.transcribe_media(...)
  -> transcript / markdown
  -> ingest_markdown()
```

但当前默认 `media_provider=None`。如果未配置 provider，系统会创建 failed document/job：

```text
unsupported_or_unconfigured:media_transcription_provider
```

这正是产品层应该显式展示的状态：不是“导入成功但没有总结”，而是“此格式需要多模态/转写 provider，当前未配置”。

### 2.5 切片

当前切片函数是 `segment_markdown()`：

```text
canonical markdown
  -> normalize
  -> split by Markdown heading
  -> preserve heading_path
  -> split content by paragraph
  -> max_segment_chars = 3600
  -> overlap_chars = 400
  -> segment_hash = sha256(heading_path + chunk)
  -> rough token_count
```

这和旧 spec 里 500-1000 token target 的愿景不完全一致。当前实现是字符数上限 + overlap，不是 token-aware splitter。

结论：

- 当前切片是 deterministic、非 LLM。
- 当前切片保留 heading path 和 stable hash。
- 当前切片还没有做到 Open Notebook 那种 content-type aware token chunking。

### 2.6 基础索引

当前基础索引是 PostgreSQL full-text：

```text
KnowledgeSegment.content
  -> to_tsvector("simple", content)
  -> KnowledgeSegment.tsv
```

搜索语句：

```text
query
  -> plainto_tsquery("simple", query)
  -> KnowledgeSegment.tsv @@ ts_query
  -> ts_rank_cd
  -> fallback: content/title ilike
  -> owner / grant / agent_searchable / sensitivity filters
```

这条基础索引不依赖 LLM，也不依赖 embedding。

### 2.7 智能图谱索引

当前 extractor 是 `PersonalKnowledgeLLMExtractor`：

```text
segment content + document metadata + source_ref
  -> tenant model resolver
  -> LLM complete(max_tokens=4096, temperature=0)
  -> strict JSON
  -> entities / assertions / links
  -> KnowledgeEntity / KnowledgeAssertion / KnowledgeLink
```

如果敏感级别在 blocklist 内，或没有可用模型，或模型输出不可用，则不能写完整图谱。服务层会把导入标成 `degraded`，并保留 warning。

当前模型解析路径借用 `memory_service._get_summary_model_config()`：

1. tenant memory config 的 `summary_model_id`
2. main conversation model
3. tenant default model / newest enabled model

但 Personal KB ingest 并不天然带入“当前 agent main model”。所以这里不应该继续依赖“单 agent 模型设置”。下一轮必须改成显式的 tenant-level Personal KB 模型配置。

### 2.8 可选向量索引

当前 `PersonalKnowledgeService.__init__()` 支持注入 `vector_provider`，但默认是 `None`。

未配置时 document/job metadata 会记录：

```json
{
  "optional_vector": {
    "enabled": false,
    "status": "disabled",
    "reason": "provider_unconfigured"
  }
}
```

配置 provider 后，ingest 会把以下输入传给 provider：

```text
title
+ heading_path
+ segment content
```

search 时 provider 只能返回 segment candidates；最终仍回到 SQL fetch，并经过 ACL 过滤后才进入结果。

结论：

- vector 是可插拔增强，不是 source of truth。
- 当前没有后台设置把 embedding model 接成默认 provider。
- 这必须补齐，否则前端不能暗示“语义搜索已可用”。

### 2.9 搜索融合

当前 `search_personal()` 的通道：

- `text`：tsvector + ilike。
- `entity`：匹配 entity canonical name / aliases。
- `graph`：从实体命中出发，经 `knowledge_links` 子图和 personalized pagerank 扩散。
- `optional_vector`：如果 vector provider 存在，融合 provider candidates。

融合方式：

- 每个通道按 rank 写 RRF。
- 加 heat / freshness boost。
- 返回 `score_trace`，包含各通道 trace 和 optional vector 状态。

## 3. “可选”的正确定义

代码里的“可选”当前有三层含义，必须拆开：

| 语义 | 当前代码含义 | 产品层应该怎么表达 |
| --- | --- | --- |
| optional vector provider | provider 可以不存在；不存在时全文/图谱仍可用 | “语义向量未启用”，不能显示为完整索引 |
| optional media provider | provider 不存在时 media 导入 failed | “当前不支持此格式处理”，不能伪装 ready |
| optional LLM extraction | LLM 失败时 degraded，基础全文仍可用 | “全文索引完成，AI 图谱/总结未完成” |

因此“可选”不是“产品上可以不做”，而是“系统必须能观测降级，并把缺失原因告诉用户和管理员”。

## 4. 后台配置闭环

### 4.1 配置必须是 tenant/company-level

Personal KB 是 user/principal scoped，但模型能力属于 tenant/company control plane。不能依赖每个 agent 的模型设置。

原因：

1. Personal KB 可以由用户直接上传，不经过某个 agent。
2. Personal KB 会被多个 agent 检索，不能绑定单个 agent 的模型。
3. embedding、OCR、STT、video provider 是后台能力，不是 agent persona。
4. 管理员需要统一成本、合规、数据出境和 provider 路由。

### 4.2 必须新增或统一的配置项

| 配置项 | 用途 | 未配置时前端状态 | 未配置时后端行为 |
| --- | --- | --- | --- |
| `personal_kb_extraction_model_id` | 实体/断言/关系抽取 | 图谱抽取不可用 | 跳过或 degraded，保留 warning |
| `personal_kb_summary_model_id` | 文档总结、source summary、note summary | 总结不可用 | 不写机械总结 |
| `personal_kb_embedding_model_id` | vector indexing/search | 语义搜索不可用 | `optional_vector.disabled` |
| `personal_kb_ocr_model_id` | 图片、扫描 PDF OCR | 图片/扫描件不可总结索引 | failed 或 degraded |
| `personal_kb_vision_model_id` | 图片理解、截图理解、视频关键帧 | 视觉理解不可用 | failed 或 degraded |
| `personal_kb_speech_to_text_model_id` | 音频/视频语音转写 | 音视频不可总结索引 | failed |
| `personal_kb_video_provider_id` | 视频下载/音轨/关键帧/字幕 | 视频处理不可用 | failed |
| `personal_kb_conversion_engine` | MarkItDown / content-core / legacy | 文档转换能力状态 | unsupported type failed |
| `personal_kb_extraction_policy` | 哪些 sensitivity 可进入 LLM | 敏感内容处理说明 | sensitive skip / blocked |
| `personal_kb_budget_policy` | 成本和队列并发 | 成本限制提示 | queued / held / failed with reason |

### 4.3 Capability Status API

需要新增一个 owner/tenant 可读的能力状态接口，例如：

```text
GET /api/knowledge/personal/capabilities
```

返回应区分：

```json
{
  "full_text_index": { "status": "ready" },
  "document_conversion": { "status": "ready", "engine": "local_markitdown" },
  "llm_extraction": { "status": "unconfigured", "required_config": "personal_kb_extraction_model_id" },
  "summarization": { "status": "unconfigured", "required_config": "personal_kb_summary_model_id" },
  "embedding": { "status": "unconfigured", "required_config": "personal_kb_embedding_model_id" },
  "image_understanding": { "status": "unconfigured", "required_config": "personal_kb_vision_model_id" },
  "audio_transcription": { "status": "unconfigured", "required_config": "personal_kb_speech_to_text_model_id" },
  "video_understanding": { "status": "unconfigured", "required_config": "personal_kb_video_provider_id" }
}
```

前端据此显示：

- 绿色：可完整处理。
- 黄色：基础索引可用，AI 增强缺失。
- 红色：此格式无法处理，提交会失败或需要管理员配置。

### 4.4 前端不应做的事

- 不应把未配置的 media 文件显示为“已索引”。
- 不应把 full-text indexed 说成“已语义索引”。
- 不应把 extracted graph failed 说成“已总结”。
- 不应只在导入失败后才告诉用户缺模型。
- 不应把配置入口藏在 agent setting 里。

## 5. OpenKnowledge 重新判断

### 5.1 修正结论

之前把 OpenKnowledge 说成 Markdown renderer 是错误判断。更准确的定义是：

> OpenKnowledge 是 AI-native Markdown vault / LLM Wiki / agent co-edit workspace。

它的官方 README 和文档强调：

- WYSIWYG Markdown / MDX editor
- macOS app / web UI / CLI
- file navigator / search / tabs / graph wiki link viewer
- Claude / Codex / Cursor / OpenCode 等 agent 通过 MCP / CLI 协作编辑
- MCP、skills、agentic search
- Git / GitHub auto-sync
- Markdown / MDX 文件作为 source of truth
- 可打开 existing codebases、wikis、Obsidian vaults

这和我们 Personal KB 的产品层非常相关。

### 5.2 OpenKnowledge 对 Hive 的价值

OpenKnowledge 更适合借鉴这些层：

| 层 | 可借鉴点 | Hive 落点 |
| --- | --- | --- |
| Vault UI | 文件树、tabs、WYSIWYG/source toggle、frontmatter、wiki links、graph view | `/knowledge` Personal workspace |
| Agent co-edit | Ask AI、Open with AI、MCP tools、agent activity | Hive agent 操作 Personal KB 的 UI/trace |
| Markdown truth | Markdown / MDX as source of truth | canonical Markdown artifact / future owner notes |
| Git/timeline | per-burst diffs、rollback、sync | 个人知识变更审计和 rollback 体验 |
| Components | Mermaid、PDF、video、audio、HTML embeds | Personal KB source preview / rich note rendering |

### 5.3 不应直接照搬的点

- OpenKnowledge 是 GPL-3.0，不能未经 legal review 直接复制代码进入 Apache 2.0 项目。
- OpenKnowledge 是 local-first file vault 体验；Hive 是 multi-tenant SaaS，需要 RLS、tenant audit、grant、cost governance。
- OpenKnowledge 的 MCP / local editor integration 不能替代 Hive 后端 ingestion pipeline。

因此采用方式应是：

1. 产品形态和交互借鉴。
2. 数据真相仍保留 Hive Knowledge Core。
3. agent co-edit 能力通过 Hive tool/runtime/governance 实现。
4. 不直接复制 GPL 代码。

## 6. Open Notebook 重新判断

Open Notebook 更适合作为 ingestion / RAG / transformations 后端机制参考。

它的公开 README / docs 强调：

- Notebook / Sources / Notes 三层 mental model。
- Sources 是输入，Notes 是用户或 AI 生成的输出。
- PDFs、videos、audio、web pages、Office docs 等 multi-modal content。
- full-text 和 vector search。
- Chat vs Ask：Chat 使用用户选择的完整上下文；Ask 走 RAG 自动检索 chunks。
- Transformations 把 source 压缩成 dense insights。
- 用户可控制 not in context / summary only / full content。
- 多 provider 模型配置，包含 LLM、embedding、speech-to-text、text-to-speech。

对 Hive 的价值：

| Open Notebook 概念 | Hive Personal KB 对应 |
| --- | --- |
| Notebook | Personal workspace / scoped collection |
| Source | `knowledge_documents` + source artifact |
| Notes | owner-authored notes / AI-generated insights |
| Transformations | summary / insight / action-item / profile candidate |
| Ask | `search_personal()` + answer synthesis |
| Chat | 选择 documents 后 full-context conversation |
| Context control | sensitivity / grant / context exposure policy |
| Provider matrix | tenant-level model capability config |

Open Notebook 对后端摄取、embedding、STT、transformations 的借鉴价值高于 OpenKnowledge；OpenKnowledge 对前端知识工作台和 agent 协作体验的借鉴价值高于 Open Notebook。

## 7. Personal 模块重新收敛后的产品边界

Personal KB 不应变成第四个产品，也不应被 Agent Memory 吞掉。

保留为同一个 `/knowledge` 工作台下的几个内部分区：

| 分区 | 目标 | 是否依赖 LLM |
| --- | --- | --- |
| 收集箱 | paste/upload/URL/chat attachment/agent output 导入与 job 状态 | 基础导入不依赖；总结和图谱依赖 |
| 文库 | documents list/detail/source preview/segments/source refs | 不依赖 |
| 搜索 | full-text / entity / graph / optional vector | full-text 不依赖；graph extraction 和 vector 依赖 |
| Ask | 基于选定 sources 或检索结果问答 | 依赖 LLM |
| Notes | owner 手写或 AI 生成 notes | 手写不依赖；AI 生成依赖 |
| Insights | summary / transformation / action items / digest | 依赖 LLM |
| 知识网 | entity/link/assertion graph | 需要 LLM extraction；可视化本身不依赖 |
| 画像/Profile | taste、偏好、工作模式、长期上下文 | 依赖 LLM synthesis + owner correction |
| 授权 | grants、agent_searchable、session grant | 不依赖 |
| 设置 | capability status、模型/embedding/media 配置入口 | 配置本身不依赖，测试连接依赖 provider |

### 7.1 暂时不做或降级的花哨能力

下一轮不应该优先做：

- 大型力导向图作为主体验。
- podcast generation。
- 复杂企业 ontology authoring。
- 每个 agent 自建 Personal KB index。
- 独立向量数据库作为 Personal M1 必需依赖。
- 自动把所有 Agent Memory 无确认写入 Personal KB。

下一轮必须先做：

- capability status。
- tenant-level Personal KB 模型配置。
- embedding provider 接线。
- media provider 接线。
- front-end unavailable/degraded 状态。
- summary / transformation 的最小闭环。
- OpenKnowledge 式文库工作台基本体验。

## 8. 下一轮实现施工边界

### 8.1 后端

新增或修改：

| 位置 | 改动 |
| --- | --- |
| `backend/app/api/agent_knowledge.py` | 新增 `/knowledge/personal/capabilities` |
| `backend/app/services/personal_knowledge_service.py` | service 初始化时从 tenant config 解析 extractor/vector/media provider |
| `backend/app/services/personal_knowledge_extractor.py` | 不再借用 generic summary resolver，改用 Personal KB extraction config |
| `backend/app/services/document_conversion.py` | 把 OCR/vision 状态写入 capability 和 conversion metadata |
| `backend/app/models/*` 或现有 config model | 增加 Personal KB capability config 持久化 |
| `backend/app/api/enterprise.py` 或 settings API | tenant admin 配置模型、embedding、media provider |
| `backend/tests/services/test_personal_knowledge_service.py` | 覆盖 unconfigured / configured provider 状态 |
| `backend/tests/api/test_agent_personal_knowledge_api.py` | 覆盖 capability endpoint 和 UI 所需 payload |

### 8.2 前端

新增或修改：

| 位置 | 改动 |
| --- | --- |
| `frontend/src/api/domains/knowledge.ts` | 增加 capability types / client |
| `frontend/src/pages/PersonalKnowledge.tsx` | 导入区读取 capability status，按格式显示 ready/degraded/unconfigured |
| `frontend/src/pages/PersonalKnowledge.css` | 增加 status strip / blocked file type / degraded job 样式 |
| `frontend/src/i18n/en.json` / `zh.json` | 增加 capability、模型缺失、多模态缺失文案 |
| 企业设置页面 | 增加 Personal KB 模型配置入口 |

### 8.3 测试红线

每项逻辑改动继续按 TDD：

1. 未配置 embedding 时，capability 返回 `unconfigured`，UI 不显示语义索引 ready。
2. 配置 embedding provider 后，ingest job metadata 记录 `optional_vector.ready`。
3. 上传 audio/video/image 且 media provider 未配置时，前端提交前提示；后端即使被直接调用也返回 failed job。
4. 配置 STT provider 后，audio 先转 transcript，再走 canonical Markdown / segmentation / tsvector。
5. 配置 extraction LLM 后，graph extraction ready；未配置时 document 为 degraded 或 graph channel unavailable。
6. 没有 LLM 时 full-text search 仍可用。
7. 没有多模态 provider 时不能创建 ready media document。
8. 管理员配置变更后 capability endpoint 实时反映。

## 9. 验证证据

当前文档基于以下本地代码事实复核：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_personal_knowledge_service.py::test_ingest_markdown_records_optional_vector_unconfigured_without_pgvector_dependency \
  tests/services/test_personal_knowledge_service.py::test_ingest_media_records_failed_job_when_provider_unconfigured \
  tests/services/test_personal_knowledge_service.py::test_ingest_audio_uses_transcription_provider_then_indexes_transcript \
  tests/services/test_personal_knowledge_service.py::test_ingest_markdown_marks_document_degraded_when_extraction_fails \
  -q
# 4 passed in 0.16s
```

当前关键代码锚点：

- `backend/app/api/agent_knowledge.py`
- `backend/app/services/personal_knowledge_service.py`
- `backend/app/services/personal_knowledge_extractor.py`
- `backend/app/services/document_conversion.py`
- `backend/app/services/memory_service.py`
- `frontend/src/pages/PersonalKnowledge.tsx`
- `frontend/src/api/domains/knowledge.ts`

外部参考源：

- OpenKnowledge GitHub: https://github.com/inkeep/open-knowledge
- OpenKnowledge docs: https://openknowledge.ai/docs
- Open Notebook GitHub: https://github.com/lfnovo/open-notebook
- Open Notebook core concepts: https://github.com/lfnovo/open-notebook/blob/main/docs/2-CORE-CONCEPTS/index.md

## 10. 当前最终口径

之后讨论 Personal KB 时采用以下口径：

1. Personal KB 当前不是空壳，已经有 M1 thin core。
2. Personal KB 当前也不是完整 OpenKnowledge / Open Notebook 级产品。
3. “索引”必须拆成 full-text / graph / vector / summary / multimodal，不再混用。
4. “可选”必须解释为 provider unconfigured 时可观测降级，不是产品功能可不做。
5. 模型配置必须是 tenant/company-level，不依赖单 agent 设置。
6. 图片、音频、视频没有对应 provider 时必须显式 blocked/failed/degraded。
7. OpenKnowledge 作为 Personal workspace / AI Obsidian 产品参考。
8. Open Notebook 作为 ingestion / RAG / transformations / provider config 机制参考。
9. 下一轮先补配置和 capability status，再补 UI 状态和 provider 接线。
10. Company KB / Enterprise Ontology 不混入 Personal KB 页面管理，只保留晋升/proposal 方向。
