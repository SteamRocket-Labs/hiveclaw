# Hindsight 与 Semantica 外部基线评估：结论是不引入，以及为什么

> 日期：2026-08-17（v2 全文重写）
>
> 状态：外部基线评估已完成。**结论：两个项目都不引入。** 本文档不产生施工项，只记录评估证据与撤回记录
>
> 本轮边界：只读源码、只运行外部项目的本地验证、只写本文档；未修改 Hive 代码、数据库、Railway 配置，未部署
>
> 代码基线：Hive `0cac825b0683b23f153d260d4708fc2bafcf318a`；Semantica `94d0c3dc07109fb4e6df3027dbd571eeefc45d52`（v0.6.5, MIT）；Hindsight API `0.9.1` / `@vectorize-io/hindsight-coding-agents` `0.3.4`（MIT）
>
> **权威状态来源（本文档不重复其内容，只引用）**：
> - Agent Memory 设计权威：`docs/memory-system-spec.md` v1.2
> - Agent Memory 施工台账与终验收：`docs/memory-implementation-log-2026-07.md`
> - 全系统最新状态（含 Enterprise Knowledge 七原子闭环判定）：`docs/kimi-review-report-2026-07-24.md`
> - 语义层选型框架：`docs/company-knowledge-semantic-layer-decision-memo-2026-07-20.md`
> - Provider 契约：`docs/knowledge-substrate-plugin-architecture-2026-07-09.md`

---

## 0. 结论

| 问题 | 结论 |
|---|---|
| Hindsight 能否作为 Hive 的 memory | **不能，且没有可抄的设计**。它是 T0→T2→T3→soul 的同类竞品；其两项标志性设计（失效状态机、动态主题页）Hive 已实现且更严谨。唯一 Hive 缺失的是双时间轴，但那不需要抄 Hindsight——Hive 自己的 ontology 层已有可复用模型 |
| Semantica 能否作为企业知识库中枢 / 是否 fork | **不做中枢，不 fork**。中枢位置已由 2026-07-24 验收的 Company Knowledge 七原子闭环占据。装入 `OntologyEnginePlugin` 在技术上可行且零摩擦，但属**可选能力增强**，不补任何断点，当前不排期 |
| 本轮是否产生施工项 | **不产生**。两个域的代码与本地验收均已完工；真正的 owner 待办是生产验收（§3），不是新功能 |

**本文档 v1 曾提出三条施工建议，全部撤回或降级。撤回依据见 §4。**

---

## 1. Hindsight 评估

### 1.1 它是什么（运行时实测）

本机运行中：容器 `ghcr.io/vectorize-io/hindsight:latest`，`:8888`（另 `:9999`），后端 PostgreSQL，`/health` → `database: connected`。真实数据：bank `coding-agent::ultra-builder-pro-cli`（94 facts）、`coding-agent::hindsight-four-client-smoke`（126 facts）。

```text
bank
  ├── memories (facts) + observations
  ├── mental-models        ← 可 refresh / history / clear
  ├── knowledge-base       ← pages(kp-*) / folders(kf-*) / tree / search / export
  ├── directives
  ├── entities + entities/graph + graph
  ├── documents + chunks
  ├── audit-logs / llm-requests / operations(retry) / webhooks
  └── profile: mission（检索用 system prompt）+ disposition(skepticism/literalism/empathy)
```

单条 memory 字段：

```json
{
  "text": "...", "fact_type": "observation|world",
  "mentioned_at": "...", "occurred_start": "...", "occurred_end": "...",
  "state": "valid", "invalidation_reason": null, "invalidated_at": null,
  "proof_count": 1, "entities": "...", "tags": ["relatedPageId:kp-..."],
  "consolidated_at": "...", "document_id": "...", "chunk_id": "..."
}
```

### 1.2 为什么不引入

1. **同类竞品，非补件。** bank → memories → mental-models → knowledge-base 与 Hive T0 → T2 → T3 → soul 一一对应。引入即双事实源。
2. **违反明文法律。** CLAUDE.md「no external memory provider may become the T3 source of truth」；`memory-system-spec.md` §0 硬约束 1「MD 是唯一真相源」。
3. **企业治理不达标。** `/version` 自报 `audit_log: false`；`components.securitySchemes` 为空、`authorization` header `required: false`；路径固定 `/v1/default/banks/...`，`default` 为硬编码单 namespace，无 tenant 维度。
4. **语义模型弱于 Hive。** 见 §1.3。

### 1.3 逐项对比：Hive 的记忆语义模型强于 Hindsight

| 维度 | Hindsight | Hive（当前 checkout 实证） |
|---|---|---|
| 失效语义 | `state: valid\|invalid` + `invalidation_reason` + `invalidated_at`（单阶段） | **`retire_entry` + `_retire_md_entry`**（`t3_platform_gate.py:746`）打 `<!-- retired: by {job_id} at {ts}; reason: ... -->`；docstring 明写 *"Marking only — the gate never deletes authored content mechanically"*；实际移除只发生在带 `convergence_note` 的收敛环 full rewrite，旧版进 `AgentAssetTransaction` rollback journal；另有 `control/tombstones.jsonl` 活引用墓碑。**两阶段 + job_id 归因** |
| 状态维度 | valid / invalid 二元 | `CONSOLIDATION_MODE_VALUES` **7 种**：create / merge / supersede / reinforce / contradict / retract / noop |
| 证据强度 | `proof_count` 计数 | `SOURCE_COVERAGE_VALUES`：single_session / multi_session / explicit_user / tool_verified / weak |
| 时效分层 | 无 | `STABILITY_VALUES`：ephemeral / short_lived / evolving / stable |
| 动态主题页 | mental models，refresh = 整页重写 | **`memory/knowledge/<concept>.md` + `milestones/<milestone>.md`**（`_DYNAMIC_TARGET_RE`）；新页强制 ≥1 条 `predicate [[target]]` 关系边（`_RELATION_EDGE_RE`）；更新时**已有 Contradictions 行必须全部保留**（`_dropped_contradiction_lines`，spec §3.4）；页面规格含 Current Claim / Scope / Evidence / Contradictions / Relations 五节 |
| 双时间轴 | `mentioned_at` vs `occurred_start/end` | **memory 层缺失**（`app/memory/` 内 `occurred_at\|valid_from\|mentioned_at` grep 零命中）；但 **ontology 层已有** `CompanyOntologyObject.valid_from` / `valid_until` / `observed_at`，且 gateway 查询已按有效期过滤 |

**结论：Hindsight 无可抄之处。** 唯一缺口（memory 层双时间轴）的参考实现应取自 Hive 自己的 ontology 层，而非外部项目。

---

## 2. Semantica 评估

### 2.1 它是什么（含实测能力与边界）

MIT / `semantica-agi/semantica` / 8.2k stars / 179,425 行 Python / 349 文件 / 241 个测试文件 / PR 编号至 #978 / 有 SSRF、header injection 等真实安全修复。**不是纸面项目。**

```text
context 18,333   ingest 17,920   kg 13,125   semantic_extract 11,594
vector_store 11,263   ontology 7,927   graph_store 7,747   conflicts 4,951
cli 4,369   provenance 3,770   pipeline 3,577   core 3,604   reasoning 3,391
change_management 2,101   seed 1,146   mcp_server 626   evals 10 ← 空
```

依赖重量**不是**阻塞项：声明了 torch / opencv / librosa / spacy，但实际懒加载（`from semantica.ontology import OntologyGenerator` 可直接导入成功），`optional-dependencies` 拆分很细。

| 能力 | 实测结论 |
|---|---|
| 本体生成 | ✅ 真实。5 实体 3 关系 → 正确产出 `owl:Class` 与 `owl:ObjectProperty`，**domain/range 推断正确** |
| Datalog 推理 | ✅ 真实。两跳正确；**递归传递闭包完全正确**（3 事实 → 全部 6 条派生） |
| 本体校验 | ❌ **不可用于把关**。悬空 `subClassOf` 与循环继承（A⊑B, B⊑A）均返回 `valid: True, errors: [], warnings: []` |
| 权限模型 | ❌ 单一全局 `SEMANTICA_API_KEY`（`explorer/dependencies.py:28`），无 per-user / per-tenant ACL；多租户仅在 vector_store namespace 层 |
| LLM 抽象 | ✅ vendor-neutral：litellm / openai / groq / huggingface + `LLMProvenanceMixin` |

### 2.2 为什么不 fork

| 能力 | 难度 | Semantica | Hive 现状 |
|---|---|---|---|
| permission-aware retrieval | **最难** | ❌ 全局单 key | ✅ 逐候选 fresh 授权（HN-04C）：显式 grant/deny、runtime/delegation、source ACL snapshot、sensitivity ceiling、validity、evidence binding 在返回标题/计数/score/正文/citation 前全部校验 |
| 治理链 | **很难** | ❌ 无 | ✅ SourceContract → Source → Evidence Envelope → Proposal → append-only Review → immutable Publication → hash-chained Event → transactional Outbox |
| 多租户 | 难 | ❌ 仅 vector namespace | ✅ 27 张表 `ENABLE + FORCE ROW LEVEL SECURITY`，NOBYPASSRLS 实测 cross-tenant=0 |
| 审计溯源 | 中 | ✅ W3C PROV-O | ✅ SHA-256 previous/event hash chain + 单调 `stream_sequence` |
| 本体推理 | 中 | ✅ 实测通过 | ⚠️ engine 层无递归推理（单跳/直接关联可用） |
| ingest 连接器 | 中 | ✅ 20+ 源 | ⚠️ 部分 |

fork = 放弃 Hive 最强三项换其最强两项，净亏。附加成本：179K LOC 维护责任、77 个 open issue、上表已实测的校验缺陷全部转为 Hive 债务。

### 2.3 装入 `OntologyEnginePlugin` 是可行的，但当前不排期

技术上零摩擦：`company_ontology_gateway.py:200-290` 已承担存储访问、逐项鉴权、逐项审计、时序过滤与 facts/links 二次回绑；engine 层被有意隔离为纯语义计算。HN-04D 已验收该边界——**"engine 是可替换注入边界，provider 不可用返回不泄漏底层文本的 retryable 503/blocked receipt"**。接入不需改 gateway、不需 provider 接触 ACL 或数据库，施工面仅为新增一个实现 + DI 处可配置切换（`company_ontology_service.py:223`、`company_ontology_gateway.py:100`、`company_knowledge_indexer.py:34`）。

若未来接入，边界为：

| 方法 | 是否交给 Semantica |
|---|---|
| `query()` | ✅ 递归/多跳推理 |
| `materialize_release_projection()` / `rebuild_projection()` | ✅ |
| `simulate_action()` | ✅ 随 `query` |
| `validate_package()` / `validate_candidate()` | ❌ 保留 Hive（更严格，§2.1） |
| `capability_status()` / `resolve_fact_lineage()` | ❌ 溯源权威属 Hive Authority Plane |

**当前不排期的理由**：它不补断点。Company Ontology 的单跳与直接关联查询已闭环并通过真 PG 验收；递归多跳属能力增强。按 `knowledge-substrate-plugin-architecture-2026-07-09.md` §8.4，接入前须在同一 corpus 上通过质量/安全/成本/更新/恢复 scorecard——而该 corpus 应建立在生产真实数据上（§3）。

---

## 3. 真正的 owner 待办：生产验收（非本轮产出，仅记录）

两个域的代码与本地验收均已完工，剩余项性质相同且只有 owner 能执行：

| 域 | 待办 | 记录出处 |
|---|---|---|
| Agent Memory | 存量 agent 的 t3 四文件重组：`python -m app.scripts.migrate_memory_two_planes --apply --confirm`（dry-run 默认；先审 plan 再 apply）。**不可逆步骤** | `memory-implementation-log-2026-07.md` Part J「交付边界与 owner 待办」；`hive-sota-master-goal.md:167` 记录「生产未执行」（2026-07-03）；`final-atomic-review-2026-07-09.md:223` 列为 P1 |
| Company KB | 真实生产租户 + 真实 provider/Agent + Railway 三服务部署验收 | `kimi-review-report-2026-07-24.md` HN-04E3 结尾 |

**Agent Memory 迁移的当前状态未确认。** 2026-07-03 之后 repo 内无执行记录。若确实未执行，生产状态为：代码已全切两平面（`LEGACY_T3_FILES` 注释：*"NOT accepted write targets since the C7 cutover"*），存量 `memory/t3/*.md` 留在原地但新代码不读、gate 拒写、dream 报 `migration_required`——**存量 agent 历史 T3 记忆处于失联状态**。该事实需 owner 在生产环境核实。

---

## 4. 撤回记录（v1 → v2）

v1 基于过期文档得出三条施工建议，逐条撤回：

| v1 建议 | 撤回依据 |
|---|---|
| ①「抄 Hindsight 失效状态机——先抄这个」 | **已存在且更严谨**。`retire_entry` / `_retire_md_entry` / `tombstones.jsonl` / 两阶段收敛环（§1.3）。v1 误信 CLAUDE.md「T3 覆盖式编辑」的描述 |
| ③「抄 Hindsight 动态主题页」 | **已存在且更严谨**。`memory/knowledge/**` + Relations 边强制 + Contradictions 保留（§1.3）。v1 误信 CLAUDE.md「T3 = 4 个固定文件」的描述 |
| ②「双时间轴」 | 缺口成立，但**参考实现改为 Hive 自己的 ontology 层**（`valid_from`/`valid_until`/`observed_at`），非 Hindsight |
| 「中文混合检索是 0→1 的洞」 | **表述错误**。HN-04C 原文为 "PostgreSQL FTS/ILIKE **baseline**"——有意的基线选择。是否升级 dense 应由生产真实召回数据决定，而非由 grep 不到 pgvector 判定。另注：`memory-system-spec.md` §0 硬约束 1 对 memory 层明确「**不上 vector**」，与 Company KB 层策略有意不同 |
| 「`validate_package()` 硬编码 `passed: True` 是缺口」 | **批错层级**。HN-04D 已验收 activation dry-run **"真实执行 typed golden query/action、input/output schema、ACL、deny/hold conflict 和 temporal case，不再信任清单里的 `passed=true`"** |
| v1 §1.3「ontology engine 是 passthrough 空壳 / 执行原子断点」 | 已在 v1 内首次修正为「正确分层」；v2 进一步确认 HN-04D 将该边界验收为设计意图 |

### 根本原因（工程教训，值得单独处理）

四次判断失误同源：**依据 CLAUDE.md 与日期化 memo，而非最新 review report。**

CLAUDE.md 当前写着 T3 Accepted Memory = `memory/t3/{episodes.md,user.md,worker.md,capabilities.md}`。该布局已在 C7 cutover 中退役，`kimi-review-report-2026-07-24.md` §6.1 点名 *"docs 的 episodes/user/worker/capabilities 已退役为 LEGACY_T3_FILES"*，代码内 `LEGACY_T3_FILES` 注释亦写明不再是可接受写入目标。

CLAUDE.md 每个 session 自动加载，其过期描述会持续误导所有新会话与新协作者。**建议单独排一项：以 `memory-system-spec.md` v1.2 + `kimi-review-report-2026-07-24.md` 为准，校正 CLAUDE.md 的 Memory System 章节。** 该项不在本轮范围内，未执行。

---

## 5. 实测证据清单（可复现）

```bash
# Hindsight 运行时
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}'   # → hindsight  ghcr.io/vectorize-io/hindsight:latest
curl -s http://127.0.0.1:8888/version                      # → audit_log:false, llm_trace:true
curl -s http://127.0.0.1:8888/v1/default/banks             # → 2 banks, 94 + 126 facts

cd "/Users/example-owner/Context Engineering/semantica"

# Semantica 本体生成（真实，domain/range 推断正确）
python3 -c "
import sys; sys.path.insert(0,'.')
from semantica.ontology import OntologyGenerator
g=OntologyGenerator(base_uri='https://hive.test/ont/')
o=g.generate_ontology({'entities':[{'name':'Alice','type':'Employee'},{'name':'Bob','type':'Employee'},
 {'name':'Acme','type':'Company'},{'name':'Globex','type':'Company'}],
 'relationships':[{'source':'Alice','target':'Acme','type':'worksFor'},
 {'source':'Bob','target':'Globex','type':'worksFor'}]})
print(o['properties'][0])"
# → worksFor: owl:ObjectProperty, domain=['Employee'], range=['Company']

# Semantica Datalog 递归推理（真实）— 注意必须用字符串 API，传 DatalogFact 对象会被静默丢弃
python3 -c "
import sys; sys.path.insert(0,'.')
from semantica.reasoning.datalog_reasoner import DatalogReasoner
r=DatalogReasoner()
for f in ['reportsTo(bob, alice)','reportsTo(carol, bob)','reportsTo(dave, carol)']: r.add_fact(f)
r.add_rule('chain(X, Y) :- reportsTo(X, Y).')
r.add_rule('chain(X, Z) :- reportsTo(X, Y), chain(Y, Z).')
print(sorted(x for x in r.derive_all() if x.startswith('chain')))"
# → 全部 6 条派生正确

# Semantica 校验缺陷（悬空引用与循环继承均检不出）
python3 -c "
import sys; sys.path.insert(0,'.')
from semantica.ontology import OntologyValidator
print(OntologyValidator().validate({'uri':'https://x/','name':'t','version':'1.0',
 'classes':[{'name':'A','@type':'owl:Class','subClassOf':'B'},
            {'name':'B','@type':'owl:Class','subClassOf':'A'}],'properties':[]}))"
# → ValidationResult(valid=True, consistent=True, satisfiable=True, errors=[], warnings=[])
```

Hive 侧关键事实复现：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend

# T3 真实布局：两平面 + 动态页，旧四文件已退役
sed -n '20,48p' app/memory/t3_platform_gate.py

# 失效状态机已存在（Marking only，gate 从不机械删除）
grep -n "def _retire_md_entry" -A12 app/memory/t3_platform_gate.py

# 动态知识页：Relations 边强制 + Contradictions 保留
sed -n '253,282p' app/memory/t3_platform_gate.py

# ontology engine 是纯计算层；存储/鉴权/审计/时序在 gateway
sed -n '200,290p' app/services/company_ontology_gateway.py

# memory 层无双时间轴（零命中）；ontology 层有
grep -rn "occurred_at\|valid_from\|mentioned_at" app/memory/
grep -n "valid_from\|valid_until\|observed_at" app/models/company_ontology.py | head -5
```

**方法学声明**：Semantica 能力结论来自实际执行；Hindsight 结论来自运行中实例的 HTTP 响应；Hive 侧结论来自当前 checkout 源码行号，并以 `kimi-review-report-2026-07-24.md`（最新全系统 review）为状态权威。无一条来自 README 宣称。v1 的失误正源于未以最新 review 为权威（§4）。

---

## 6. 明确暂不做

1. **不引入 Hindsight**（§1.2）。
2. **不 fork Semantica**（§2.2）。
3. **不装 Semantica 进 `OntologyEnginePlugin`**——技术可行、零摩擦，但不补断点，当前不排期（§2.3）。
4. **不用 Semantica 替换本体校验**——Hive 现有实现更严格（§2.1）。
5. **不改 memory 层为 vector 检索**——`memory-system-spec.md` §0 硬约束 1 明确「不上 vector」。
6. **不启用 Semantica 的 `context` 模块**（18.3K LOC 的 DecisionRecorder / PolicyEngine / AgentContext）——与 `services/decision_trace.py`、`services/action_preflight.py` 重叠，治理权须留 Hive。
7. **不在本轮修 CLAUDE.md**——已识别为真实问题（§4 根本原因），需单独排期。
8. **不在本轮补 memory 层双时间轴**——缺口成立但非阻塞，且应在生产迁移完成后再动 T3 结构。
9. **上游文档未修订**——`knowledge-substrate-plugin-architecture-2026-07-09.md` §3 的 `Company KB = Missing` 状态表已被 2026-07-24 的 HN-04 闭环判定取代，本轮未改动该文件。

---

## 7. 修订记录

| 日期 | 变更 |
|---|---|
| 2026-08-17 | v1 首版。Hindsight / Semantica 实测评估；提出三条施工建议 |
| 2026-08-17 | v1 内修正 §1.3 空洞 A：`ReferenceOntologyEngine.query()` 不访问存储属正确分层，非 passthrough 空壳 |
| 2026-08-17 | v1 内确认业务前提：首批覆盖文档问答与业务对象关系查询两个场景 |
| 2026-08-17 | **v2 全文重写。** 读入 `memory-system-spec.md` v1.2、`memory-implementation-log-2026-07.md`、`kimi-review-report-2026-07-24.md` 三份权威后，撤回 v1 全部施工建议（§4）：失效状态机与动态主题页 Hive 已实现且更严谨；中文检索的「0→1 洞」表述错误（FTS/ILIKE 是有意 baseline）；`validate_package` 批评错层级（dry-run 已不信任该值）。文档定位由「评估 + 施工建议」改为「评估 + 撤回记录」，不产生施工项。新增 §3 记录真正的 owner 待办（生产验收）与 §4 根本原因（CLAUDE.md 过期描述持续误导，建议单独排期校正） |
