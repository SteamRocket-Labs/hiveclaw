# WIP：Agent Memory 生产迁移 + Company KB 生产验收

> 建档 2026-08-19，生产核查 2026-08-20
> 执行：Claude　　授权：owner (rocky243)
> 状态：**代码与前端全部完成并已在生产运行；任务 A 已完成（历史）、B1 完成、B2 不需要。**
> **统一行动方案见 `docs/wip/production-remediation-plan-2026-08-23.md`**（本文档为诊断证据，方案在那里）
> 未决：① **51 个 agent trigger 空转 = P0 静默产品故障（§3.6.4 诊断，方案 P0）**；② B3 首次真实业务数据录入（待 owner 参与）；③ 图投影流转（设计已定稿，见 `docs/company-knowledge-graph-projection-design-2026-08-23.md`，未施工）；④ SA-07 T0→T2 生产回填（§3.6.2，与已完成的 two-plane migration 是两件事）
> 代码基线：本地 `0cac825b`；生产 `b3e0546f`（两者 backend/frontend 零差异）

---

## 0. 结论摘要

| 任务 | 结论 |
|---|---|
| **A. Agent Memory 生产迁移** | **已于 2026-07-03T01:14Z 完整执行完毕**，无残留、无未完成项。repo 内「生产未执行」的记录是过期的 |
| **B1. 部署状态核对** | 三服务均 `SUCCESS`，部署于 2026-07-24T19:48Z |
| **B2. 三服务部署** | **不需要**。生产 commit `b3e0546f` 与本地 HEAD `0cac825b` 在 `backend/`、`frontend/` 上**零差异**（仅差 docs 与 deck）；alembic head 生产=本地=`merge_incident_kimi_0725`，单头 |
| **B3. 真实租户端到端验收** | **未做，且从未做过** —— Company KB/Ontology 28 张表在生产**总行数 = 0**。这是唯一剩余项，需 owner 参与（会写生产数据） |

---

## 1. 任务 A：已完成（生产实测证据）

`railway ssh --service backend` 只读核查，2026-08-20：

```
AGENTS=129              生产 agent workspace 总数
LEGACY_FILES=0          memory/t3/*.md 残留 = 零
T3_DIRS=56              56 个 agent 有（已空的）memory/t3/ 目录
ARCHIVE_LEGACY=56       56 个 agent 有 memory/.archive/legacy_t3/
MARKERS=56              memory/control/two_plane_migration.json
APPLIED=56              全部 status: applied
ARCHIVED_MD=277         归档的 legacy .md 文件总数
PLANS=69                .staging/migration/*/migration_plan.json
ISSUES=13               .staging/migration/*/issues.json（9 个 agent）
```

**一致性验证**：`T3_DIRS = ARCHIVE_LEGACY = MARKERS = APPLIED = 56`，四项完全相等。
**held 重试验证**：9 个曾产生 validation issues 的 agent（共 13 次 held），逐一比对 marker 列表 → **全部存在 applied marker，无一遗漏**（`comm -23` 结果为空）。`PLANS 69 = APPLIED 56 + HELD 13`，账目平。

marker 样本：

```json
{
  "agent_id": "003f7336-...", "status": "applied",
  "applied_at": "2026-07-03T01:14:22.514673+00:00",
  "archived_to": ".../memory/.archive/legacy_t3/20260703T011422Z",
  "notes": "从原始soul中剥离实然的\"How I Learn\"整段，将其转化为self.md唯一的条目…
            profiles由身份段和利益相关者段演绎：owner为AlbertZ，collaborators含实习生和PM，
            domain聚焦玻璃基板/先进封装产业链。无T3遗留材料可生成knowledge或milestone pages。"
}
```

两平面产物分布：`self.md × 67`、`profiles/*.md × 78`、`knowledge/*.md × 21`、`milestones/*.md × 2`、`soul.md × 100`。129 个 agent 中 56 个曾有 legacy（C7 cutover 前建），其余 73 个 cutover 后创建，天生两平面。

### 1.1 需要修正的 repo 记录

| 位置 | 现状表述 | 事实 |
|---|---|---|
| `docs/hive-sota-master-goal.md:167` | 「dream 第二刀 `migrate_memory_two_planes --apply` 生产未执行」 | 该行写于 2026-07-03，而迁移在 **2026-07-03T01:14Z** 执行 —— 记录写在执行之前，此后未更新 |
| `docs/final-atomic-review-2026-07-09.md:223` | 把 two-plane 迁移列为 P1 收尾债 | 7-09 时迁移已完成 6 天 |

**本轮未改动这两份文档**（属历史归档性质），但引用它们判断状态会得出错误结论。

---

## 2. 任务 B：B1 完成，B2 不需要，B3 是唯一剩余

### 2.1 B1 部署状态（只读实测）

| 服务 | 状态 | 部署时间 | commit |
|---|---|---|---|
| backend | SUCCESS | 2026-07-24T19:48:20Z | `b3e0546f` |
| backend-api | SUCCESS | 2026-07-24T19:48:28Z | `b3e0546f` |
| frontend | SUCCESS | 2026-07-24T19:48:35Z | `b3e0546f` |

### 2.2 B2 不需要部署 —— 生产代码已是最新

```
git diff --stat b3e0546f..0cac825b -- backend frontend   →  空
git log --oneline b3e0546f..0cac825b                     →  86223d1f, 0cac825b（均为 docs commit）
非 docs 改动                                              →  .gitignore, deck/README.md, deck/index.html
```

alembic head：生产 `merge_incident_kimi_0725` = 本地 `merge_incident_kimi_0725`（单头）。

**Company KB 的全部代码、schema、RLS 已在生产运行。**

### 2.3 生产基础设施验证（只读实测）

```
Company KB / Ontology 表数 = 28        总行数 = 0
RLS: 28 张表全部 ENABLE + FORCE
运行时角色 = app_rls  superuser=False  bypassrls=False
```

（注：探针脚本的 RLS 查询把 `*_pkey` 索引一并计入了 `pg_class`，是脚本瑕疵；表本身 28/28 全部 ENABLE+FORCE。）

### 2.4 B3：唯一剩余项

**28 张表总行数 = 0 → Company KB 在生产从未被真实租户使用过。** 这正是 `kimi-review-report-2026-07-24.md` HN-04E3 结尾所述「真实生产租户、真实 provider/Agent 与 Railway deployment 仍由发布验收负责」中尚未完成的那一半（deployment 那半已完成，见 2.1）。

需覆盖的旅程（HN-04C/E3 已在本地真 PG 跑通，生产需复现）：

```
Company KB:  source contract → ingest → proposal → review → publish → index
             → 授权 search / read / cite / explain
             → 同租户 deny 零侧信道 → Library deny → permission revoke 即时拒绝
             → Agent submitted proposal（active publication 数不变）
             → retire → new-version restore → event chain 完整
Ontology:    install → activate（dry-run 真实执行 golden query）→ curate
             → review → publish → query / explain / simulate → retire / restore
```

**阻塞原因**：B3 会**写生产数据**，且需要真实认证租户用户与真实文档。这超出只读核查范围，需 owner 授权并参与（提供测试租户与素材）。我不会自行造认证 token 或写生产数据。

---

## 3. Review 发现（据生产实测重新定级）

### R-1（已不适用）多文件 apply 无跨文件事务

原发现：apply 中途失败会留下孤儿新文件（本地探针 `scratchpad/probe_partial_failure.py` 已复现：legacy 与 soul 完好、不丢数据，但部分新文件落盘且 marker 未写，重跑若 slug 变化会残留孤儿知识页）。

**降级理由**：迁移已于 2026-07-03 完整跑完且账目平（§1），56/56 applied、13 次 held 全部重试成功、零残留。新建 agent 天生两平面不走迁移路径。**该风险已无触发场景，无需修复。** 探针与分析保留作为记录。

### R-2（已成既成事实，可复核）apply 用 LLM 生成内容覆写了 soul.md

`migrate_memory_two_planes.py:161-164` 无条件覆写 `soul.md`，原文件归档。这是 spec §6.3「soul 拆纯」的设计意图，且有空值保护（LLM 返回空 soul_md 而原 soul 有内容 → held）。

**2026-07-03 那次 apply 已用 LLM 重写了 56 个 agent 的 soul.md。** 从 marker `notes` 看重组质量高（准确区分应然/实然）。若需复核任一 agent 的原始 soul，归档在：

```
/data/agents/<agent_id>/memory/.archive/legacy_t3/20260703T011422Z/soul.md
```

**这不是缺陷，是已发生且可回溯的事实。** 是否抽样复核由 owner 定。

### R-3（降级：历史偶发，不阻塞）runtime_control_bus T0 投影失败

`/api/health` 的 `runtime_control_bus.last_error` 显示 `LookupError: transcript_event ebc4771b-… not visible after 40 attempts`（`runtime_control_bus.py:390`）。

**生产日志实测**：最近 500 行中 `not visible after` 出现 **0 次**，bridge/projection 相关 0 次。`last_error` 仅保留最后一次，属历史偶发，非持续故障。

**顺带验证的生产健康**：无 ERROR、无 CRITICAL。251 条 WARNING 全部是设计内的 audited RLS bypass，最大项 `reason='tenant resolution for agent <uuid>'`（解析 agent 归属前必须先 bypass，daemon 轮询产生，每 agent 4 次）。四个 daemon（evolution/trigger/workflow/sandbox probe）均 healthy；`rls_runtime_role` 报 `enforcement: strict, violations: []`。

---

## 3.5 前端实装核查（2026-08-23，wiring proof）

owner 提问「前端做完了吗 / Company KB 前端能不能显示」，逐条实测：

### 入口链（路由存在 ≠ 用户能到达）

两条链均完整接线：

```
员工侧：AppSidebar.tsx:53  { to: '/knowledge', labelKey: 'nav.knowledge' }
        → PersonalKnowledge.tsx:1039  <Link to="/knowledge/company">Open Company Library</Link>
        → App.tsx:146  knowledge/company → CompanyKnowledgeLibrary        （ProtectedRoute）

管理侧：surfaces/workspace/sections.ts:34  path '/enterprise/knowledge'
        → App.tsx:168  knowledge → CompanyKnowledgeControlPlane           （ProtectedRoute + WorkspaceGuard）
```

注：Company Library **不在侧边栏一级导航**，入口是 Personal Knowledge 页内的按钮。这符合 HN-04E3「员工路由只消费授权 Library search/read，不展示管理动作」的设计，非缺口。

### 两平面适配（关键风险已排除）

后端 C7 cutover 后前端若仍按旧四文件渲染即为断点。实测：

- 前端引用旧布局（`episodes.md` / `worker.md` / `capabilities.md` / `memory/t3`）→ **grep 零命中**
- `WorkspaceFeatureHub.tsx:320` `collectMemoryRows` 调 `knowledgeApi.overview(agentId)`，读取 `overview.planes.self.entries` / `planes.profiles.entries` / `planes.knowledge.pages` / `planes.milestones.pages` / `planes.self.failureModes.active` / `identity.pendingSoulCandidates` / `linkedCapabilities.skillCandidates` → **正是两平面结构**

`/memory` → `WorkspaceFeatureHub kind="memory"` 是真实功能（967 行，独立 query），非占位页。

### API 接线

`src/api/domains/companyKnowledge.ts` 覆盖全链路端点：`documents` / `search` / `documents/{key}` / `promotion-intakes`（personal / legacy / retry / candidate）/ `proposals` / `materialize` / `review` / `publish`。

### 硬证据

| 项 | 结果 |
|---|---|
| i18n 对齐 | en **3648** = zh **3648**，仅 en 有 = 0、仅 zh 有 = 0；`companyKnowledge` 相关 121 key、`personalKnowledge` 93 key；`nav.knowledge` / `enterprise.tabs.knowledge` / `personalKnowledge.openCompanyLibrary` 双语齐备 |
| 前端测试 | **139 files / 819 tests passed** |
| 生产构建 | ✓ built in 3.08s；`CompanyKnowledgeControlPlane` 26.70 kB + CSS 4.4 kB、`CompanyKnowledgeLibrary` 6.17 kB + CSS 2.2 kB、`PersonalKnowledge` 31.64 kB、`companyKnowledge` API chunk 9.97 kB；AgentDetail 与 vendor bundle budget 均通过 |
| **生产前端已含该功能** | 生产 main chunk = `/assets/index-B6-NT0gr.js`，与本地刚构建产物**同名**（Vite hash 含内容 → 内容一致）；该 chunk 内 `CompanyKnowledge\|knowledge/company` 命中 **7 次** |

### 结论

**前端开发完成，且已在生产运行。** Company KB 页面**能显示**——但因后端 28 张表 0 行（§2.3），当前呈现为**空状态**。这是「没有数据」，不是「不能显示」或「未接线」。

---

## 3.6 外部 review 交叉核查 + 生产容量诊断（2026-08-23）

对 Codex 提交的当前 review 做独立核查。**主体准确，一条比本文档更细，两条需修正。** 核查过程中另发现一项此前未记录的生产容量问题（§3.6.4），优先级高于 B3。

### 3.6.1 核查一致的部分

当前一轮四条结论（Agent Memory 迁移完成 / Company KB 后端+schema+RLS+双端前端已部署 / 三服务为对应最新代码 / 唯一剩余 B3）与本文档 §1–§3.5 独立验证一致。

整体产品未完成项中，以下经交叉核查成立：hermes-agent 行为级对标未做（`kimi-review-report-2026-07-24.md` §11 第 1 项）；真 PG 故障注入/双进程恢复/真实浏览器/Hive Connect 真机/secret rotation 验收未全执行（§11 第 2–7 项）；`proactive_employee_loop`（HN-01）与 `memory/policy_replay`（HN-02）明确登记未建设；Memory 层双时间轴缺失；unified exec / execpolicy / session_search / verify-on-stop 属 §10.3 的「Codex 增量吸收建议」，非已承诺漏做项。

### 3.6.2 Codex 抓到了本文档漏掉的一点：SA-07 ≠ two-plane migration

**这条成立且重要。** 本文档 §1 反复表述的「Agent Memory 迁移完成」仅指 **T3 布局迁移**（旧四文件 → 两平面，56/56 applied）。

`kimi-review-report-2026-07-24.md` §11 第 13 项的 **SA-07 T2 authority 生产回填**是完全另一件事：将历史会话回填为 T2 Segment Package，工具 `python -m app.scripts.backfill_t0_to_t2`（脚本存在），验收标准 `post_apply_inventory.coverage_complete=true`。**未执行。**

回填对象规模真实且不小：生产 `chat_transcript_events` **n_live_tup = 279,245**、`chat_sessions` **42,802**。

同项第 14 项 **SA-08** 的验收标准明载「整个员工 DOM 不得出现 T0/T2、heartbeat、Dream、runtime task/job id」——与 §3.5 独立发现的术语泄漏（`featureHub.memory.subtitle` 含「T0 会话真相、T2/T3 蒸馏」）是同一件事，**当前不满足**。

### 3.6.3 两条需修正

**① 「没有证据表明是持续事故」→ 应改为「结构性持续，根因已定位」。** 见 §3.6.4。

**② 「1 条 dead-letter outbox 记录」→ 数字无法确认，且基数远大于 1。** `session_event_outbox` n_live_tup = **28,068**，且 grep 确认消费后无删除逻辑（纯累积）。其中 dead-letter 占比需在 BYPASS 或具体 tenant 上下文下确认；`app_rls` 角色的 RLS 会过滤掉全部行。

### 3.6.4 【新发现，优先级高于 B3】runtime_tasks 容量泄漏

**性质：不是事故（incident），是结构性容量泄漏（capacity leak）。** 服务正常、`/api/health` = ok、四个 daemon healthy、业务查询（带 tenant 过滤）不受影响。但泄漏速率恒定、删除率为零、无任何 retention，且运维侧症状已出现。

**实测数据（`pg_stat_user_tables` 累计统计，不受 RLS 影响）：**

| 表 | 累计插入 | 累计删除 | 删除率 | 体积 |
|---|---|---|---|---|
| `runtime_tasks` | 2,322,010 | **0** | **0.0%** | **4,946 MB** |
| `runtime_budget_runs` | 1,627,782 | **0** | **0.0%** | 1,769 MB |
| `invocation_spans` | 665,589 | **0** | **0.0%** | 1,771 MB |
| `agent_activity_logs` | 610,198 | 1,282 | 0.2% | 1,159 MB |
| `chat_transcript_events` | 285,480 | 32,203 | 11.3% | 1,215 MB |

数据库总大小 **12 GB**，`runtime_tasks` 单表占 **41%**。

**时间跨度与速率**（BYPASS 上下文下的单行索引读）：最早 `2026-04-08 02:36Z`，最新 `2026-08-23 05:50Z` → **137 天**，日均 **~1.7 万行 / ~36 MB**。

**根因（决定性）**：近 7 天 `runtime_tasks` 的 status 分布——

```
skipped: 250,044   ← 99.6%
running:     504
failed:      408
```

**99.6% 的写入是创建后立即 `skipped` 的记录**（约每 2.4 秒一条）。这类记录无审计价值：它记录的是「本次不需要做事」。真正有信息量的 running/failed 合计不足 0.4%。

**次生症状**：

1. `runtime_task_worker.last_error` = `session_terminal_recovery: QueryCanceledError: canceling statement due to statement timeout`（`statement_timeout = 30s`）。
2. 运维聚合查询在该表上不可用。EXPLAIN 显示根因是 RLS filter 不可下推：
   ```
   Index Scan using ix_runtime_tasks_created_at  (cost=0.43..891695.63 rows=11613)
     Filter: (current_setting('app.current_tenant_id') = 'BYPASS'
              OR tenant_id::text = current_setting('app.current_tenant_id'))
   ```
   `current_setting()` 是函数调用，无法转成索引条件 → 未设 tenant 上下文时逐行扫 232 万行 → 超时。**这是架构特征不是缺陷**，但它意味着表越大、运维越瞎。
3. autovacuum 在大表上滞后：`agent_activity_logs` last_autovacuum = **2026-07-03**（7 周前）、`chat_transcript_events` = 2026-07-17（5 周前）。`runtime_tasks` 本身正常（autovacuum 08-22、autoanalyze 08-23）。

**风险路径**：磁盘持续增长 → Railway PostgreSQL 容量上限 → 写入失败 → 全站不可用。当前 12 GB、日增 36 MB。**Railway PG 的容量上限未确认，这是评估紧迫性的缺失输入。**

**建议修复顺序（未施工，待 owner 定）：**

| 序 | 动作 | 收益 | 风险 |
|---|---|---|---|
| 1 | **止血**：不再持久化「创建即 skipped」的 task —— 在创建前判定，而非创建后落库再 skip | 写入量立降 ~99.6% | 低。需确认 skipped 记录无下游消费者（审计/报表/幂等） |
| 2 | **确认容量上限**：查 Railway PG plan 的磁盘上限 | 把「还有多久」从未知变成数字 | 无 |
| 3 | **retention 策略**：terminal 态记录按保留窗归档/删除。`invocation_spans` 是 CLAUDE.md 明载的 canonical trace surface，只可归档不可删 | 存量止增 | 中。需定保留窗口与归档目标 |
| 4 | **历史清理**：分批删除历史 skipped 行（小批次 + 间隔，避免长事务与锁表），完成后 `VACUUM (ANALYZE)` | 释放约 4 GB | 中。必须分批，且先在非高峰执行 |
| 5 | **autovacuum 调优**：对超大表下调 `autovacuum_vacuum_scale_factor` | 修 7 周未 vacuum | 低 |

**先做第 1 项和第 2 项**：第 1 项是唯一能阻止问题继续恶化的动作，且改动面最小；第 2 项决定后三项的紧迫性。

### 3.6.5 本文档自身的两处修正

1. §2.3 曾报告「Company KB 28 张表总行数 = 0」——该查询使用 `app_rls` 角色，**RLS 会过滤掉未设 tenant 上下文的全部行**。已用不受 RLS 影响的 `pg_stat_user_tables.n_live_tup` 复核：Company KB/Ontology 28 张表 **n_live_tup 确实全为 0**，`last_analyze` / `last_autoanalyze` 均为 None（从未有数据触发）。**结论成立**。
2. 但同批查询中报告的 `chat_sessions: 0 行`、`session_event_outbox: 0 行` **是 RLS 假象**，真实为 42,802 与 28,068。**教训：在 RLS-FORCE 库上做只读盘点，必须用 `pg_stat_user_tables` / `pg_class` 或显式 BYPASS 上下文，`SELECT count(*)` 在受限角色下会静默返回 0。**

由此得到一个更准确的产品图景：**生产在被重度使用**（42,802 会话 / 279,245 transcript events / 2,322,010 runtime tasks），**而 Company KB 零采用**。B3 因此不只是工程验收，也是产品采用问题。

---

## 4. 已完成的本地工作

| 项 | 取证 |
|---|---|
| CLAUDE.md Memory 章节校正 + 状态权威顺序声明 | 8 处改动，638 → 668 行；备份 `scratchpad/CLAUDE.md.bak` |
| 迁移脚本代码 review | 九项安全机制核对（幂等 marker / 无模型即 held / plan 落 staging / dry-run 默认 / tmp+os.replace 原子写 / legacy 永不删 / plan 校验 / soul 空值保护 / index 失败不阻断） |
| 迁移 + 读侧测试 | 24 passed |
| memory 全量基线 | 387 passed |
| R-1 部分失败复现 | 探针实跑，见 §3 R-1 |

---

## 5. 明确暂不做

1. **B3 之前不预先施工检索层升级** —— `PostgreSQL FTS/ILIKE baseline` 是有意基线；是否升级 dense/CJK 由 B3 的真实召回数据决定。
2. **memory 层双时间轴** —— `app/memory/` grep 零命中，缺口成立但非阻塞。参考实现取自 Hive 自己的 ontology 层（`models/company_ontology.py:322-323`），不抄外部项目。
3. **R-1 事务化修复** —— 已无触发场景（§3 R-1）。
4. **Semantica 接入 `OntologyEnginePlugin`** —— 可行、零摩擦、不补断点、不排期。见 `docs/memory-ontology-external-baseline-evaluation-2026-08-17.md` v2 §2.3。
5. **引入 Hindsight** —— 已否决，同上 §1。
6. **修订 `hive-sota-master-goal.md:167` 与 `final-atomic-review-2026-07-09.md:223`** —— 已确认过期（§1.1），本轮未改。
7. **CLAUDE.md 行数精简** —— 现 668 行，`harness-governance.md` 建议 always-loaded < 200 行。本轮只做必要校正。

---

## 6. 进度

| 项 | 状态 | 取证 |
|---|---|---|
| CLAUDE.md 校正 + 权威顺序 | ✅ 2026-08-19 | §4 |
| 迁移脚本 review + 本地测试 | ✅ 2026-08-20 | 24 + 387 passed |
| A1 生产 legacy 检测 | ✅ 2026-08-20 | §1，legacy 残留 = 0 |
| A2 dry-run 审 plan | ⏭️ 无需执行 | 迁移已于 7-03 完成 |
| A3 受权迁移 | ⏭️ 无需执行 | 56/56 applied，账目平 |
| B1 部署状态核对 | ✅ 2026-08-20 | §2.1 |
| B2 三服务部署 | ⏭️ 无需执行 | 生产代码 = 本地 HEAD（§2.2） |
| 前端实装核查（入口链 / 两平面适配 / i18n / 测试 / 构建 / 生产 bundle） | ✅ 2026-08-23 | §3.5 |
| 外部 review 交叉核查（Codex） | ✅ 2026-08-23 | §3.6.1–3.6.3 |
| runtime_tasks 容量泄漏诊断 | ✅ 2026-08-23 已定位根因，**未修复** | §3.6.4 |
| SA-07 T0→T2 生产回填 | ⬜ 未执行 | §3.6.2 |
| B3 真实租户端到端验收 | ⬜ 待 owner 参与 | §2.4 |
| 图投影流转设计（证据层抽取 + 审核前置门） | 📄 设计已定稿，未施工 | `docs/company-knowledge-graph-projection-design-2026-08-23.md` |

B3 完成后：把耐久结论折进 `docs/` 正式文档，删除本文件。
