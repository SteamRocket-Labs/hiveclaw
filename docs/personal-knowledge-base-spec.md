# Hive 个人知识库（Person Wiki / 终极知识库）— 实施规格

版本：v1.0（2026-07-03）
状态：**设计已闭环，据此实施。** 递进架构与三项前置拍板见 `knowledge-pyramid-agent-person-org-2026-07-03.md`；本轮五岔路拍板记录见 §1；讨论过程见对应设计图景 artifact（讨论稿，非实施依据）。

上游文档：

- `docs/knowledge-pyramid-agent-person-org-2026-07-03.md` — 金字塔三级架构 + 三项拍板（自持薄核 / 画像双读面 / M1 直投先行）
- `docs/memory-system-spec.md` v1.2 — Agent 层（第一级）规格；本文是其 person-scope 对应物
- `docs/company-knowledge-ontology-plane-plan-2026-06-20.md` — 组织层设计；本文的表族是其 schema 家族的 person-scope 子集

---

## 0. 一页纲领

> 施工契约：2026-07-08 起，Personal KB 的“全部完成”验收以
> `docs/personal-knowledge-base-completion-contract-2026-07-08.md`
> 为当前执行清单。本文仍是产品/架构 spec；完成契约负责逐项记录实现证据、
> 测试结果和 commit。

- **定位**：金字塔第二级。一个人**所有经手信息的归宿**（终极知识库）+ 名下 agents 的知识聚合层（M2）。
- **核心原则**：一份真相（canonical MD）、一次索引（person scope）、全员访问（受治理工具）。agents **不各自建索引**，获得的是检索访问。
- **两平面在 person scope 的复现**：画像（收敛 → 常驻注入，M2）‖ 语料/知识网（成网 → 工具检索，M1）。
- **与 agent 记忆的边界**：互不接管。agent 自有 SQLite 索引、`plane_read` 读面、七道工序原样不动；个人库是第二套系统，靠 refs 与 agent 记忆互指。

**硬约束（实施不变量，验收自检 §8）**：索引层可整层重建 / LLM 步骤 L1 完整视野 / ACL 过滤在注入前 / 同表族 scope 列 + RLS day one / 内容零复制（sha256 一份真相）/ 库真相独立于 agent 生命周期。

---

## 1. 决策记录（全部已拍板）

| # | 决策 | 定案 | 出处 |
|---|---|---|---|
| P1 | 底座 | 自持薄核（06-20 schema 轻治理子集）+ HippoRAG 配方内化 + SAG 仅陪练 spike | pyramid §9-1 |
| P2 | 画像消费者 | ①投影回 agent + ②owner 自阅面同一 pass；③组织后置 | pyramid §9-2 |
| P3 | 批次次序 | M1 直投先行；M2 聚合复用其薄核 | pyramid §9-3 |
| D1 | KB 提示行 | **要**：invoke 时轻召回 top-3 标题+id（≤200 tok，不注正文），空结果不注入 | 本轮 ① |
| D2 | 聊天附件 | **默认入库** + 上传时单条可排除；入库后可归档 | 本轮 ② |
| D3 | agent 检索权限 | M1 最简版：owner 全部 agent 可检全库；每文档「禁止 agent 检索」开关 + 敏感度等级；细粒度矩阵待 ACL 硬化 | 本轮 ③ |
| D4 | 知识网可视化 | M1 简版（实体列表 + 邻居 + 关联段落）；力导向图 M2 | 本轮 ④ |
| D5 | 表落位 | 直接用 `knowledge_*` 表族 + `scope` 列；person→org 晋升 = scope 翻转 + proposals 过门，零搬家；RLS day one | 本轮 ⑤ |

---

## 2. 架构总览

```
输入                          个人知识核（person scope）                消费
──────                        ────────────────────────                ──────
直投(上传/URL/粘贴) ──┐        真相层  canonical MD artifact           ┌─→ 全部 agents:
聊天附件顺流(D2) ────┼──→     管线层  异步 index_jobs（六步 §4）  ──→─┤    search_personal_kb 工具
agent 晋升(M2) ──────┘        索引层  PG 薄核（六表 §3）               │    + 画像常驻切片(M2)
                                                                      ├─→ owner Web UI（§7）
                              索引层整层可删可重建 ←─ 真相层            └─→ org proposals（后置）
```

- **「大量上传如何被所有 agent 索引」**：索引只发生一次；N 份文件 = N 个异步 job 入队，逐 hash 幂等处理，完成即对全部 agent 同时可检索。
- 分形定律复现：侧写常驻 ‖ 知识检索（memory-system-spec §4.2 的 person-scope 版本）。

---

## 3. 存储与 schema（D5）

### 3.1 表族（六表，全带 `tenant_id` + `scope_type('person'|'team'|'org')` + `scope_id`，RLS day one）

| 表 | 关键列 | 约束/说明 |
|---|---|---|
| `knowledge_documents` | `source_sha256` · `artifact_path` · `origin(upload\|url\|paste\|agent:<id>)` · `title` · `status` · `sensitivity` · `agent_searchable(bool, 默认 true)` | `unique(tenant_id, scope, source_sha256)` —— 去重锚点；同文件多处出现 = 一行 + 多条引用边 |
| `knowledge_segments` | `document_id` · `position` · `content` · `seg_hash` · `embedding vector` · `tsv tsvector` | 检索落点单位；refs 指到段；tsv 为生成列 |
| `knowledge_entities` | `canonical_name` · `type(自由串)` · `aliases text[]` · `merged_into(nullable)` | person 层不做硬类型化；合并留墓碑可回滚 |
| `knowledge_assertions` | `subject_entity_id` · `predicate` · `object_text/object_entity_id` · `segment_refs[]` · `confidence` | HippoRAG 式三元组/事件；证据必指段 |
| `knowledge_links` | `from_kind/from_id` · `to_kind/to_id`（entity\|segment\|document）· `relation(自由串)` · `source_refs[]` | 图检索走这里；org 层升 typed |
| `knowledge_index_jobs` | `document_id` · `stage` · `status` · `artifact_hash` · `error` · `retries` | 逐 hash 幂等；管线可观测；重跑入口 |

- 迁移纪律：单 Alembic head；RLS policy 随建表同批。person 内 owner 校验在 service 层（`check_person_scope`，对标 `check_agent_access`），RLS 管租户隔离。
- **pgvector 前置核实（M0）**：生产 PG 启用待核实；不可用则走官方镜像/扩展启用路径解决，**不引入第二存储**。嵌入走租户模型配置新增 embedding 用途（模型平等，无硬编码 vendor）；租户无 embedding 配置 → 管线可观测降级（全文 + 图两通道），配置后可重跑补齐。

### 3.2 真相层路径契约（person root）

```
{AGENT_DATA_DIR}/persons/<user_id>/kb/
  artifacts/<source_sha256>/content.md + meta.json   ← 直投与入库附件的库内真相
  notes/<note_id>.md                                  ← 粘贴笔记
```

- **库真相独立于 agent 生命周期**：聊天附件入库时，artifact 复制/硬链一份进 person root（sha256 保证至多一份）——agent 或其 workspace 被删不伤库。
- M2 指针型条目（agent 概念页）真相留 agent workspace；**agent 删除时冻结快照进 person root**（quarantine 语义），条目标 `frozen`。
- 本节即 person root 的权威路径契约；不归 agent memory 的 `hygiene.py` 接管。

---

## 4. 摄取管线（六步，异步 job 驱动）

| 步 | 动作 | 关键契约 |
|---|---|---|
| 1 转换 | `DocumentConversionService` + MarkItDown → canonical MD + `source_sha256`（**复用**） | URL/文本/文件统一入口 |
| 2 建档 | `documents` upsert by `(scope, sha256)` | 已存在 → 只加引用边，秒完成 |
| 3 切段 | 结构感知（标题/段落），500–1000 tok，CJK-aware 估算（复用运行时契约） | `seg_hash` 稳定 |
| 4 抽取 | 逐段 LLM 抽实体 + 断言 + 关系；prompt 借鉴 HippoRAG/SAG 公开配方；输出 schema 化 | **L1 完整段落视野**；失败 → job error 可观测重试 |
| 5 对齐 | 别名规整匹配 + embedding 相似候选 → 同义边；合并写 `merged_into` 墓碑 | 错合可回滚 |
| 6 入索引 | embedding + tsv + 图边落库；job 完成 → 可检索 | 完成事件可通知 UI |

- **幂等**：`artifact_hash` 不变不重跑；改版才重索引（06-20 re-index 语义）。
- **批量是常态**：并发闸 + LLM 成本 telemetry；进度/失败/花费在收集箱逐项可见。
- **机械兜底只在失败路径**：抽取彻底失败的文档可标 `degraded`（仅向量+全文可检，无实体/图），可观测、可重跑——不静默、不作主路径。
- 复用底座：LLM client（重试/过载 fallback）、转换层、工具治理、>50KB 结果溢出 artifacts 规则。

### 4.1 聊天附件顺流（D2）

丢给任何 agent 的文档附件默认注册入库（转换 artifact 本就生成，增量成本≈建档+索引）；上传气泡提供单条「不入库」排除；入库后可在文库归档。origin 记 `agent:<id>` 会话来源，refs 双向：会话 T0 事件 ↔ 库 doc id。

---

## 5. 检索服务与运行时组装

### 5.1 `KnowledgeSearchService`（person scope）

```
query → 实体链接（match entities/aliases）
      → 并行通道：① pgvector ANN ② tsv 全文 ③ PPR 图扩散（从命中实体出发，多跳）
      → ④ 引用热度/新鲜度 boost
      → RRF 分数融合
      → 敏感度 / agent_searchable / ACL 过滤（返回之前，06-20 §3.4 定律）
      → top-k 段落 + 证据 refs（doc 标题 · sha256 · seg_hash · artifact path）+ 各通道 trace
```

- PPR 复用 `relation_graph.py` 模式：邻接从 PG 加载子图，Python 内存计算（person 图万级节点量级）。
- trace 随结果返回：owner UI 直接展示（透明性 = 调试面）；agent 侧进入 tool result envelope。

### 5.2 Agent 工具

- `search_personal_kb(query, top_k?, filters?)`：只读检索；**注册 CAPABILITY_MAP + tool governance**（历史坑：不注册则真租户调用被拒）；结果走 envelope，>50KB 溢出 artifacts。
- M2 增补：`save_to_kb`（agent 将交付物显式存库，受治理）。

### 5.3 上下文组装（读侧双轨，D1）

| 内容 | 通道 | 位置 |
|---|---|---|
| 画像切片（M2） | **常驻注入**，按 agent ACL 过滤 | memory 侧写区（frozen prefix 附近） |
| KB 提示行（D1） | invoke 时轻召回（向量+全文两通道，跳过 PPR 省成本），top-3 标题+id，≤200 tok，空结果不注入 | **动态后缀区——不得破坏 prompt cache 锚点** |
| 语料正文 | agent 按需调 `search_personal_kb`（渐进披露，与 Skill/tool_search 同哲学） | tool round |

---

## 6. 文档关联四则（规范性规则）

| 场景 | 规则 | 机制 |
|---|---|---|
| 聊天里丢给 agent 的文件 | 库里一份真相，会话里只是引用 | sha256 去重：3 个 agent 收到同一文件 = 1 行文档 + 3 条引用边（D2 默认入库） |
| agent workspace 工作产物 | **默认不入库**（中间产物是噪音） | M1 owner 手动「存入知识库」；M2 `save_to_kb` |
| agent `knowledge/` 概念页 | 真相留 agent MD；库里是指针型条目 + 抽取的实体/边 | M2 注册；跨 agent 同名懒合并（共存带 provenance，高热/冲突才 LLM 合并）；agent 删除 → 冻结快照 |
| agent 采用了库内容 | 引用流入其 T2 证据链，`t2-` ↔ 库 doc id 互指 | 引用计数回流 = 热度/retention + 文库「被谁使用」视图 |

一句话：**agent 里是工作现场，库里是归档真相；内容永不复制两份。**

---

## 7. 前端 IA（Workspace 顶层入口「知识库」，`/knowledge`）

当前前端主入口应跟 `Agent圈` / `任务 / 自动化` / `Bridge` 同级，位于 `frontend/src/pages/layout/AppSidebar.tsx` 的 `workspaceNavItems`。`/workspace/knowledge` 只作为历史设计稿兼容路径，进入后重定向到 `/knowledge`；不要把 Personal KB 的主入口藏在某个 Agent Detail 里。

| 模块 | 批次 | 内容 |
|---|---|---|
| 收集箱 | M1 | Markdown / notes 直投已落地；拖拽/URL/批量投喂必须走后端统一摄取能力后再打开，不能先做前端假入口。逐项管线状态（转换→抽取→已入网/失败重试/degraded）+ LLM 成本；单条排除入库（D2） |
| 文库 + 详情 | M1 | 来源徽章（上传/链接/粘贴/来自 Agent X）· 实体 chips · 引用者；详情 = MD 预览（实体高亮）+ 证据链回源 + 操作（重建索引/敏感度/agent 检索开关（D3）/归档） |
| 全局搜索 | M1 | 与 agent 同一检索 API；展示融合 trace（哪条通道、什么分数） |
| 知识网 | M1 简版 / M2 图 | M1：实体列表 + 邻居 + 关联段落（纯数据视图）；M2：力导向可视化（D4） |
| 画像 | M2 | 「Hive 眼中的你」：应然/实然两栏 · 每条带证据 · 可纠正（= 最强反例下调信号）· 投影范围设置 |

- API surface：当前 owner-scoped M1 使用 `/api/knowledge/personal/`（paste · documents list/get · search）；agent 视角保留 `/api/agents/{agent_id}/knowledge/personal/` 作为消费/调试入口。未来 upload / ingest_url / jobs / flags 仍应落在后端统一摄取能力上，scope 参数默认 person，org 后置复用。
- Enterprise KB：Personal KB 页面只允许出现只读入口或晋升/proposal 入口；企业库的写入、publish、retire、Ontology 管理留在公司控制台。
- 删除语义：默认归档（索引清除、真相保留）；硬删真相走确认门。
- i18n `en.json`/`zh.json` 双更；样式遵循 `.impeccable.md` tokens。

---

## 8. 治理与不变量（每次改动自检）

1. 索引层（PG 六表 + embedding + 图）可整层删除并从真相层全量重建。
2. L1：抽取/对齐/合并由 LLM 完整视野完成；机械处理只作失败路径的可观测降级（`degraded` 标记 + 可重跑）。
3. 敏感度 / `agent_searchable` / ACL 过滤发生在结果返回与 prompt 注入之前。
4. 同表族 + scope 列 + RLS day one；person→org 晋升 = scope 翻转过 `knowledge_proposals` 门，零数据搬家。
5. agent 自有索引与个人库互不接管，各自可从各自真相重建。
6. 内容零复制：sha256 一份真相、多处引用边。
7. 库真相独立于 agent 生命周期（复制/硬链入 person root；指针条目冻结快照）。
8. 新工具注册 CAPABILITY_MAP + tool governance，无旁路执行。
9. KB 提示行只进动态后缀，不破坏 prompt cache 锚点。
10. 外部检索/图 provider（含 SAG）任何情况下不成为真相源，随时可拔。

---

## 9. 实施计划（每批一次完整 pass，零 MVP 债）

- **M0 前置**：agent 层生产迁移 `migrate_memory_two_planes --apply` 执行 + 吃真数据；pgvector 生产可用性核实；租户 embedding 模型配置面确认。
- **M1 直投闭环（一次完整 pass）**：六表 + RLS 迁移 → 摄取管线（含 degraded 路径与重跑）→ 检索服务 + `search_personal_kb` + 提示行 → 前端四模块（收集箱/文库/搜索/知识网简版）→ 聊天附件顺流 + 去重 → i18n → 测试（红测先行：管线幂等 · sha256 去重 · ACL/敏感度过滤 · 整层重建 · 工具治理 · 提示行 cache 锚点稳定；集成测试跑真 PG + RLS，沿用仓库惯例）。
- **M2 聚合批次（独立完整 pass，非延期债——其养分依赖 agents 在新两平面结构上的真实积累）**：概念页指针注册 + 懒合并 → 画像收敛管线（spec §3.3 机制复用）+ 双读面 + ACL 投影 → `save_to_kb` → agent 贡献视角 → 力导向图。
- **SAG 陪练对打（M1 检索服务成型后）**：同一批语料 + 同一组问题；指标 = recall@k · citation accuracy · ACL leakage(=0 硬门) · latency · cost；对打记档进 eval 纪律——输了即弃，赢了以 provider 身份挂接（不变量 10）。
- **效果验收纪律**：不在无靶状态下宣称效果；M1 验收 = 红测绿 + 真数据摄取记档 + 对打 scorecard。

---

## 修订记录

- **v1.0（2026-07-03）**：初版。承接 pyramid §9 三项拍板（P1-P3）+ 本轮五岔路拍板（D1-D5）；定 schema 六表、person root 路径契约、六步管线、检索融合与读侧双轨、文档关联四则、前端 IA、十条不变量、M0/M1/M2 批次与 SAG 对打方案。
