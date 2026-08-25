# 企业知识库：两条准入路径的分离与切片→展示→授权链条

> 建档 2026-08-24
> 代码基线：`de66ac4e`（当前 HEAD）
> 状态：**owner 已授权施工（2026-08-24）**，含生产 `CREATE EXTENSION pg_trgm` + GIN 索引 DDL
> 本文只写 Company KB 的准入、切片、展示、授权与检索。Agent Memory 的 T0/T2/T3 蒸馏不在范围内。

---

## 0. 一页结论

当前实现把**两条本质不同的准入路径塞进了同一个界面**，并且把「个人内容的发起动作」放进了「公司治理的场所」。这不是文案问题，是场所与角色错配。

| | 路径 1：自下而上（提升） | 路径 2：自上而下（导入） |
|---|---|---|
| 谁发起 | **员工本人**（内容的 owner） | **公司管理员**（org_admin） |
| 内容从哪来 | 员工自己的 Personal KB / Agent 产出 | 公司制度文件、合同、手册 |
| 发起场所 | **员工工作区**（`/knowledge`） | **公司后台**（`/enterprise/knowledge`） |
| 后端入口 | `POST /promotion-intakes/personal` | `POST /source-contracts` + `POST /imports` |
| 前端是否已接 | ✅ 已接（但放错了地方） | ❌ **完全没接** |
| 语义 | 「我建议公司采纳这条」 | 「这是公司的正式资料」 |

**核心错误**：路径 1 的发起动作（`个人知识提交`）被放在了公司后台。管理员看到的应该是「谁提交了什么、我批不批」，而不是「我从我的个人知识里挑一条提交」。管理员的个人知识跟公司知识库没有任何关系。

---

## 1. 现状真相（全部来自当前代码，非推断）

### 1.1 受众：公司后台只有管理员能进

`frontend/src/guards.tsx:28`

```js
const allowed = ['org_admin', 'platform_admin'];
if (!allowed.includes(user.role)) return <Navigate to="/dashboard" replace />;
```

`/enterprise/*` 全部包在 `WorkspaceGuard` 里。**普通员工进不来。**

### 1.2 两个 surface 是分开的，而按钮把人踢出去了

```
/enterprise/knowledge  → WorkspaceLayout   （公司后台外壳）
/knowledge/company     → AppLayout         （员工工作区外壳）
/knowledge             → AppLayout         （个人知识）
```

`CompanyKnowledgeControlPlane.tsx:964` 的 `<Link to="/knowledge/company">` 与 `:967` 的 `<Link to="/knowledge">` 会把管理员从公司后台弹进员工工作区，侧边栏整体更换。这就是「跳转到前面」的成因。

### 1.3 后端两条路径的真实差异

**路径 1（提升）**：`POST /promotion-intakes/personal` → 从 Personal KB 条目产出 evidence + 候选 → 走 proposal → review → publish。

**路径 2（直接导入）**：`POST /imports` → `process_import_job()` 产出：

```
CompanyKnowledgeEvidence   （证据）
KnowledgeDocument          （文档）
segment_markdown(markdown) → KnowledgeSegment[]   （切片）
job.status = "completed"
```

**注意：路径 2 到此为止，不自动创建 Proposal。** 必须有人再调 `POST /proposals` 才能进入审核。这个设计是对的（证据 ≠ 已发布知识），但**前端两步都没有界面**。

### 1.4 切片：真的做了

`segment_markdown()` 按 Markdown 标题层级切，产出 `position` / `segment_hash` / `heading_path` / `content` / `token_count`，落 `knowledge_segments` 表。在 import（`company_knowledge_service.py:944`）与 materialize（`:1674`）两处都调用。

`KnowledgeSegment` 有唯一约束 `(tenant_id, document_id, position)` 和 `(tenant_id, document_id, segment_hash)`。

### 1.5 授权粒度：**不是切片级**

`ResourcePermission`（`app/models/security_audit.py:43`）+ `_matches_resource()`（`company_knowledge_permissions.py:215`）支持三种匹配：

| 粒度 | resource_type | 含义 |
|---|---|---|
| 精确 | 具体资源 id/key | 单个 publication |
| 命名空间 | `company_knowledge_namespace` | 一个 namespace 下全部 |
| 租户 | `company_knowledge_scope` | 全公司 |

外加两个收窄维度：`sensitivity_ceiling`（PL1–PL4）、`conditions.field_refs`。

**切片不是授权对象。** 切片是检索单位，授权发生在它所属的 publication 上。

> 这一点与 owner 的假设不同，但**当前设计是对的**，不建议改：
> 授权对象必须是「一个人能读懂并签字负责的东西」。一份文件可以，「第 7 个切片」不行。
> 切片是可重建的派生物（改了切片算法就全变），把权限挂在派生物上等于把授权绑在实现细节上。

### 1.6 检索：切片是检索单位，publication 是授权单位

`company_knowledge_gateway.py:591 search()`：

```python
ts_query = func.plainto_tsquery("simple", query)
score = ts_rank_cd(KnowledgeSegment.tsv, ts_query)
... .join(KnowledgeSegment, KnowledgeSegment.document_id == KnowledgeDocument.id)
    .where(or_(
        KnowledgeSegment.tsv.op("@@")(ts_query),      # FTS
        KnowledgeSegment.content.ilike(like_query),   # 子串兜底
        KnowledgeDocument.title.ilike(like_query),
    ))
# 逐条对 publication 做 discover / search 两次权限判定
```

**中文现状**：`simple` 配置不分词，一段中文变成一个 token。查 `报销标准` 只有当切片里存在被空格/标点界定的完全相同的 token 才命中 FTS，`ts_rank_cd` 因此几乎恒为 0，排序退化成按 `published_at`。
**但 ILIKE 兜底让完全匹配的子串仍能搜到** —— 所以现状是「中文能搜到精确短语，但没有排序、没有分词、没有近义」。不是完全不可用，是不可靠。

零命中确认：`jieba` / `zhparser` / `to_tsvector('chinese'` / `Vector(` 全部为 0；`pgvector` 只出现在一句注释里。

---

## 2. 目标设计

### 2.1 一句话原则

> **场所由「谁负责这件事」决定，不由「内容将来去哪」决定。**

员工对自己的内容负责 → 提交动作在员工工作区。
管理员对公司知识负责 → 审核、导入、授权在公司后台。

### 2.2 三个场所的职责重划

| 场所 | 路由 | 谁能进 | 职责 |
|---|---|---|---|
| **个人知识** | `/knowledge` | 所有员工 | 上传、整理自己的资料；**发起「建议公司采纳」** |
| **公司知识库（阅读）** | `/knowledge/company` | 所有员工（受权限过滤） | 检索、阅读、引用已发布的公司知识 |
| **公司后台 · 公司知识** | `/enterprise/knowledge` | org_admin / platform_admin | **导入公司资料**、审核提交、发布、授权、生命周期 |

### 2.3 公司后台四个 lane 的重排

现有 lane（`ControlPlane` line 939-942）：`intake` / `review` / `access` / `lifecycle`。保留骨架，改内容：

| lane | 现在是什么 | 应该是什么 |
|---|---|---|
| **接收** `intake` | ❌ 管理员从**自己的**个人知识挑一条提交 + 恢复归档 | ✅ **导入公司资料**（路径 2 全流程）+ 恢复归档 |
| **审核与发布** `review` | 提案列表 | ✅ 不变，但**提案要显示来源路径**（提升 / 导入），两类审核信息不同 |
| **权限** `access` | 授权列表 | ✅ 不变，补上「按 namespace 批量授权」与「授权影响预览」 |
| **知识库与模型** `lifecycle` | publication 生命周期 | ✅ 不变 |

**关键改动：把「个人知识提交」整块从公司后台移到员工工作区的 `/knowledge` 页。** 公司后台的接收 lane 只显示「**收到的**提交」，不显示「发起提交」。

### 2.4 路径 1：自下而上（提升）

```
员工在 /knowledge 选中自己的条目
   → 点「建议公司采纳」，填 目标 namespace / 信息级别 / 理由
   → POST /promotion-intakes/personal          ← 已存在
   → 后端产出 evidence + 候选
   → 管理员在 /enterprise/knowledge「接收」lane 看到一条待处理提交
   → 创建 proposal → review → publish          ← 已存在
```

前端要做：把提交表单从 ControlPlane 搬到 PersonalKnowledge；ControlPlane 接收 lane 改为只读的「收到的提交」列表。

### 2.5 路径 2：自上而下（导入）—— 这条前端要从零建

```
管理员在 /enterprise/knowledge「接收」lane
  ① 声明来源（一次性）    POST /source-contracts
       来源类型、责任人、ACL 快照、保留策略
  ② 上传/指向资料         POST /imports         → 202 + job_id
  ③ 观察处理进度          GET  /import-jobs/{id}
       后端：抽正文 → segment_markdown() 切片 → 落 evidence + document + segments
  ④ 从导入结果发起提案     POST /proposals       ← 现在这一步前端也没有
  ⑤ 审核 → 发布           已存在
```

**②→④ 之间是当前最大的断点**：后端全通，前端一步都没有。

### 2.6 切片 → 展示 → 授权的完整链条

这是 owner 问的核心，逐环说清：

**① 切片何时产生**
导入完成时（`process_import_job`）与提案 materialize 时（`materialize`）各产生一次。切片是**文档的派生物**，随文档版本走。

**② 切片展示给谁**

| 阶段 | 谁看得到切片 | 看到什么 | 目的 |
|---|---|---|---|
| 导入后、发布前 | 仅管理员 | 切片预览：标题层级 + 每片正文 + token 数 | **确认切得对不对**——这是导入质量的唯一可视化手段 |
| 审核中 | 审核者 | 切片 + 与现有知识的冲突对照 | 判断能否发布 |
| 发布后 | 有权限的员工/Agent | 检索命中的**单个切片**，带 `heading_path` 面包屑与出处链接 | 精确定位，不用读整篇 |

**③ 授权基于什么建立**

不基于切片，基于 **publication**（= 一份文件的一个版本）。授权时选：

```
授权对象：  ○ 单份文件（publication）
           ● 一个 namespace 下全部（推荐默认）
           ○ 全公司
授权给：    用户 / 部门 / 角色 / Agent
动作：      discover / search / read / cite
信息级别上限：PL1 public → PL4 credential
（可选）字段收窄：conditions.field_refs
有效期：    可设 expires_at
```

**④ 检索时两者如何对上**（现有实现，无需改）

```
按切片检索命中  →  取切片所属 publication  →  对该 publication 判权限
   ↓ 允许                                        ↓ 拒绝
返回该切片 + 出处                            整条丢弃，且不泄漏存在性
```

`gateway.search()` 已经是 `discover` + `search` 两段判定，同租户无权者拿不到任何侧信道。**这一层是对的，不动。**

---

## 3. 中英文检索（2026-08-24 已实证，原建议已撤回）

### 3.1 生产实测：中文 FTS 确实完全失效

在生产 PG 上直接跑（只读）：

| 测试 | 结果 |
|---|---|
| `to_tsvector('simple','公司报销标准与差旅制度')` | `'公司报销标准与差旅制度':1` — **整句一个 token** |
| `to_tsvector('simple','company expense and travel policy')` | 5 个 token — 英文正常 |
| `to_tsvector(...) @@ plainto_tsquery('simple','报销标准')` | **`False`** — 子串明明存在也搜不到 |
| `'公司报销标准…' ILIKE '%报销标准%'` | `True` — 只有它在兜底 |

生产环境：PostgreSQL **18.6**，encoding `UTF8`，collate `en_US.utf8`。

### 3.2 原方案 A（zhparser / pg_jieba）作废

`pg_available_extensions` 共 47 项，**`zhparser`、`pg_jieba`、`pgroonga`、`pg_bigm` 全部不存在**。
`pg_ts_parser` 只有 `default`；`pg_ts_config` 30 种语言，**无 `chinese`**。

要装就得自建/替换 Railway 的 PG 镜像 —— 代价远超收益。**方案 A 排除。**

### 3.3 原方案 B（应用侧分词）也不再推荐

镜像里有两个现成扩展，比自己写分词更好：

| 扩展 | 版本 | 状态 |
|---|---|---|
| `pg_trgm` | 1.6 | 可安装，未装 |
| `vector`（pgvector） | 0.8.6 | 可安装，未装 |

角色：`postgres`（superuser）存在；应用连接用的 `app_rls` 无 superuser、无法 `CREATE EXTENSION`。

### 3.4 新方案：pg_trgm（本地 PG 18 实测验证）

**中文相似度 —— `word_similarity` 是关键函数**

对「短查询 vs 长文档」，`similarity()` 会被文档长度稀释（中文命中仅 0.10），但 `word_similarity()` 干净利落：

| 切片内容 | `similarity` | **`word_similarity`** |
|---|---|---|
| 报销标准调整通知：自本月起住宿费上限调整为 600 元 | 0.138 | **0.800** |
| 差旅报销标准：交通费按实报销，住宿费每晚上限 500 元 | 0.097 | **0.600** |
| 年假申请流程：提前三个工作日在系统提交 | 0.000 | **0.000** |
| 员工手册第三章：考勤与请假管理规定 | 0.000 | **0.000** |

查询词 `报销标准`。相关 0.6–0.8、不相关 0.0，**区分干净且绝对值可用常规阈值**。

**英文同样有效**（一套机制覆盖中英文）：
`word_similarity('expense policy', 'company expense and travel policy')` = **0.577**；不相关查询 `annual leave` = 0.158。

**GIN trgm 索引让现有 ILIKE 零改动提速**

现有查询已经在用 `KnowledgeSegment.content.ilike('%q%')` 兜底。5 万行切片上实测：

| | 计划 | Buffers |
|---|---|---|
| 无索引 | Seq Scan，扫 50,001 行，cost 1395 | 770 |
| `CREATE INDEX ... USING gin (content gin_trgm_ops)` | Bitmap Index Scan，cost 88 | **16** |

**约 48× 提升，且查询语句一行都不用改。**

### 3.5 落地步骤

| 序 | 动作 | 需要什么 |
|---|---|---|
| 1 | `CREATE EXTENSION pg_trgm;` | **生产 DDL，需 owner 授权 + `postgres` superuser 连接** |
| 2 | `knowledge_segments.content` 与 `knowledge_documents.title` 上建 GIN trgm 索引（`CONCURRENTLY`，避免锁表） | 同上 |
| 3 | `gateway.search()` 排序改用 `word_similarity` 而非恒为 0 的 `ts_rank_cd`；保留 FTS 分支给英文（互补不冲突） | 代码改动 |
| 4 | 用路径 2 导入的真实中文制度文件建召回验收集，量出改善幅度 | 依赖 §4 第 1 项 |

**注意**：`similarity` 系列的默认阈值 `set_limit` 是 0.3，对中文 `similarity()` 偏高。若用 `word_similarity` 则不受影响（实测 0.6–0.8）。阈值应基于真实语料标定，不要拍脑袋。

### 3.6 语义检索（延后，但路已通）

`vector` 0.8.6 就在可用扩展列表里 —— 将来上 embedding 检索**不需要换镜像**。
但建议先做完 pg_trgm 并量出召回率再评估：trgm 解决「精确/近似字面匹配 + 排序」，向量解决「同义不同词」，是两个问题。

## 4. 施工顺序

按「解除阻塞」排，不按技术难度：

| 序 | 内容 | 依赖 | 是否需 owner 决策 |
|---|---|---|---|
| **1** | **路径 2 前端从零建**：source-contract 声明 → 上传 → job 进度 → 切片预览 → 发起提案 | 无（后端全通） | 否 |
| **2** | **把「个人知识提交」搬到 `/knowledge`**；后台接收 lane 改为只读「收到的提交」 | 无 | 否 |
| **3** | **修跳转**：后台内不再 `<Link>` 到 `/knowledge/*`；公司知识库阅读页改为后台内嵌视图或新窗口打开 | 无 | 需定：内嵌还是新窗口 |
| **4** | **装 `pg_trgm` + GIN 索引 + `word_similarity` 排序** + 召回验收集 | 需 1 提供真实语料 | 需定：授权生产 DDL |
| **5** | 授权界面补「按 namespace 批量」与「授权影响预览」 | 需 1 有真实数据 | 否 |

**1 和 2 可并行**，都不需要你决策。

---

## 5. 置信度声明

**≥95% 的部分（代码实证，可复核）**

- 受众判定：`guards.tsx:28`
- 两个 surface 分离与跳转成因：`App.tsx:146/165` + `ControlPlane.tsx:964/967`
- 路径 2 前端完全未接：`companyKnowledge.ts` 中 `imports` / `source-contracts` 零命中
- 切片真实存在且接线：`segment_markdown()` 在 `:944` / `:1674`
- 授权粒度非切片级：`_matches_resource()` 三种匹配 + `field_refs`
- 检索实现与中文缺陷：`gateway.py:591`，`jieba`/`zhparser`/`Vector(` 零命中
- 直接导入不自动建 proposal：`process_import_job` 内无 `Proposal(`
- **生产 PG 扩展清单**：47 项可用，无 `zhparser`/`pg_jieba`；有 `pg_trgm` 1.6 与 `vector` 0.8.6；`postgres` superuser 存在
- **中文 FTS 失效**：生产实跑 `to_tsvector('simple', …) @@ plainto_tsquery(…)` = `False`
- **pg_trgm 中文有效**：本地 PG 18 实跑 `word_similarity` 相关 0.6–0.8 / 不相关 0.0；英文 0.577 / 0.158
- **GIN trgm 加速 ILIKE**：5 万行实测 Buffers 770 → 16

**不到 95%、需要验证的部分**

- **切片预览的粒度是否够管理员判断质量** —— 需要真实文件跑一遍才知道，目前是设计假设
- **是否存在其它调用 `POST /imports` 的客户端**（如 Agent 工具侧）—— 我只查了前端，若 Agent 侧已在用，路径 2 的语义要重新对齐

**明确的设计取舍（非事实，是主张）**

- 授权保持在 publication 层、不下沉到切片 —— 理由见 §1.5
- ~~中文分词优先应用侧~~ —— **已撤回**。实证发现镜像无 CJK 分词扩展但有 `pg_trgm`，且 `word_similarity` 中英文双有效、现有 ILIKE 零改动提速 48×，优于自写分词

---

## 6. 待 owner 决定

| # | 决定项 | 我的建议 |
|---|---|---|
| 1 | 是否授权在生产执行 `CREATE EXTENSION pg_trgm` + 建 GIN 索引 | **授权**。这是本方案唯一的生产 DDL；索引用 `CONCURRENTLY` 建，不锁表，可随时 `DROP INDEX` 回滚 |
| 2 | 后台里「打开公司知识库」是内嵌视图还是新窗口 | **后台内嵌只读视图**。管理员需要的是「员工搜到的是什么样」，不该被弹出后台 |
| 3 | 授权默认粒度 | **namespace**。逐份文件授权在几十份之后就不可维护 |
| 4 | 是否现在就上向量检索 | **否**。`vector` 0.8.6 已在可用列表、路已通，但先用 pg_trgm 量出召回率再评估 |


---

## 7. 下个 session 从这里开始

> **owner 已于 2026-08-24 授权本方案施工**，包含唯一的生产 DDL（`CREATE EXTENSION pg_trgm` + 两个 GIN 索引）。
> 无需再确认，直接按下列顺序开工。

### 7.1 开工顺序（1 与 2 可并行，都不阻塞）

| 序 | 任务 | 关键文件 | 阻塞项 |
|---|---|---|---|
| **1** | 路径 2 前端从零建：source-contract 声明 → 上传 → job 进度 → **切片预览** → 发起提案 | `frontend/src/api/domains/companyKnowledge.ts`（加 `imports` / `source-contracts` / `proposals` 创建）、`CompanyKnowledgeControlPlane.tsx` intake lane | 无（后端全通） |
| **2** | 「个人知识提交」从后台搬到 `/knowledge`；后台 intake lane 改为只读「收到的提交」 | `PersonalKnowledge.tsx`、`CompanyKnowledgeControlPlane.tsx` | 无 |
| **3** | 修跳转：后台内不再 `<Link to="/knowledge/*">`，改后台内嵌只读视图 | `CompanyKnowledgeControlPlane.tsx:964/967` | 无 |
| **4** | pg_trgm：装扩展 → GIN 索引（`CONCURRENTLY`）→ `search()` 排序改 `word_similarity` | alembic migration + `company_knowledge_gateway.py:591` | 已授权 |
| **5** | 授权界面补 namespace 批量授权 + 授权影响预览 | `CompanyKnowledgeControlPlane.tsx` access lane | 需 1 产出真实数据 |

### 7.2 已验证的事实（不要重新调查）

| 事实 | 出处 |
|---|---|
| `/enterprise/*` 只有 `org_admin` / `platform_admin` 能进 | `frontend/src/guards.tsx:28` |
| 后台与员工工作区是两个 surface，按钮跨 surface | `App.tsx:146/165`、`ControlPlane.tsx:964/967` |
| 前端**完全没接** `POST /imports` 与 `POST /source-contracts` | `companyKnowledge.ts` 零命中 |
| `POST /imports` 产出 evidence + document + 切片，**不自动建 proposal** | `process_import_job` 内无 `Proposal(` |
| 切片真实存在：`segment_markdown()` | `company_knowledge_service.py:944` / `:1674` |
| 授权粒度是 publication / namespace / tenant，**不是切片** | `company_knowledge_permissions.py:215` |
| 检索：切片是检索单位，publication 是授权单位，`discover`+`search` 两段判定 | `company_knowledge_gateway.py:591` |
| 生产 PG **18.6**，47 个可用扩展 | 生产实测 |
| **无** `zhparser` / `pg_jieba` / `pgroonga` / `pg_bigm`；`pg_ts_config` 无 `chinese` | 生产实测 |
| **有** `pg_trgm` 1.6、`vector` 0.8.6，均未安装；`postgres` superuser 存在 | 生产实测 |
| 中文 FTS 完全失效：`to_tsvector('simple',…) @@ plainto_tsquery(…)` = `False` | 生产实测 |
| `word_similarity` 中文有效：相关 0.6–0.8 / 不相关 0.0；英文 0.577 / 0.158 | 本地 PG 18 实测 |
| GIN trgm 索引让现有 ILIKE 零改动提速：Buffers 770 → 16 | 本地 PG 18 实测（5 万行） |

### 7.3 施工时要注意的坑

- **索引必须 `CONCURRENTLY`**：`knowledge_segments` 生产行数未知但随导入增长，普通 `CREATE INDEX` 会锁表。alembic 里用 `CONCURRENTLY` 需要 `autocommit_block()`。
- **`set_limit` 默认 0.3 对中文 `similarity()` 偏高**，但用 `word_similarity` 不受影响（实测 0.6–0.8）。阈值要用真实语料标定，不要拍脑袋。
- **`CREATE EXTENSION` 要 superuser**：应用连接的 `app_rls` 不是 superuser。migration 在部署时以什么角色执行需先确认。
- **FTS 分支保留**：`word_similarity` 与现有 `tsv` FTS 互补，不要删掉 FTS —— 英文路径靠它。

### 7.4 仍未验证（施工中需确认）

- **切片预览的粒度是否够管理员判断质量** —— 设计假设，需真实文件跑一遍
- **是否存在其它客户端在调 `POST /imports`**（如 Agent 工具侧）—— 只查了前端
- **migration 执行角色能否 `CREATE EXTENSION`** —— 见 §7.3

### 7.5 与 P0 的关系

本方案与 `docs/wip/production-remediation-plan-2026-08-23.md` 的 P2（B3 Company KB 首次真实数据验收）是同一件事的两面：
本方案建的是**入口**，B3 验的是**跑通一遍**。路径 2 前端建好后，B3 才第一次具备可执行条件（此前根本没有添加入口）。

P0-A（LLM provider 充值）与本方案无依赖关系，独立挂起。
