# Memory 主线实施台账（2026-07）

权威规格：`docs/memory-system-spec.md` v1.2（设计已闭环）。本文件是实施台账：每个任务完成时追加一节，含改动、接线/退役证据、测试数字、commit hash。规格本身不在此修订。

**每任务收尾五步（owner 纪律，2026-07-02）**：① 红测转绿＋全量回归（贴数字）② 接线证据（grep 生产调用点）③ 退役证据（被替代路径无残留 writer）④ 更新本台账 ⑤ 立即 commit（精确 add，只含本任务文件）。

**实装四问**：生产入口真接线？无 fake/手写掩盖 wiring？断言钉死 bug？旧路径退役了？

---

## C9 雨天地基（三断层，一次完整 pass）

### 侦察结论（2026-07-02，代码证据）

**断层 1 — T2 held/failed 无人捡回：**
- 生产入口：SESSION_IDLE / SESSION_CLOSE / TRIGGER_END / DELEGATION_END → `_build_t2_for_sealed_segment` → `run_t2_segment_package_job()`（`runtime/hooks_setup.py:604`）。
- Job 状态机：`memory/.staging/t2_jobs/<job_id>/job_manifest.json`，queued → running → committed/held/failed（`memory/t2/segment_package.py:353-442`）。job_id/package_id 是 session+segment 稳定哈希（`:1723-1730`）→ 重跑幂等。
- held 来源：无 summary 模型配置、候选 validation 不过、LLM 异常；failed：job 级异常；进程崩溃：manifest 永久卡 queued/running。
- 现状零捡回。既有可复用模式：startup 恢复 `main.py:360 resume_persisted_heartbeat_runs(limit=50)`；heartbeat 内非 LLM 维护挂点 `services/heartbeat.py:1326 _run_memory_lifecycle_maintenance`。
- 注意：`session_lineage` 未持久化进 job_manifest；重试时 lineage 由 `_build_lineage_payload` 从 T0 事件 metadata 兜底恢复——sweep 实现需补存或验证兜底充分。

**断层 2 — consolidation-debt 无台账无告警：**
- 消化入口：heartbeat → `stage_pending_t3_consolidation_job`（`memory/t3_consolidation.py:150`，每次上限 8 包）。
- absorbed 闭环已通：T3 Platform Gate 提交后改 T2 manifest `package_status=absorbed`（`memory/t3_platform_gate.py:535-544`）→ discover 的 reviewed/closed 过滤自然排除。
- 缺口：无"pending 了多久"台账、heartbeat 停摆/消化持续失败无告警。观测面现状：无统一 alert 服务；既有模式 = `write_audit_log`（admin 审计面）+ `memory/control/` 报告文件（`lifecycle_maintenance.json` 先例）。

**断层 3 — retention 未落地：**
- SQLite 索引不存在（`memory/indexes/` 仅 wiki_map.md）；引用计数无从谈起。
- 归档先例：`memory/hygiene.py:47,75` `_archive_target` + quarantine archive（可逆、留痕）。
- lifecycle sidecar 先例：`memory/control/lifecycle.json`（`lifecycle_store.py:333`）。

### 接线单（开工承诺，收工逐项 grep 验证）

| 项 | 新建 | 接入生产入口 | 退役 | 配套同步 |
|---|---|---|---|---|
| C9-1 sweep | `memory/t2/job_sweep.py`（扫 `.staging/t2_jobs`，stale queued/running 归位、held/failed 有界重试、超限告警） | ① `main.py` lifespan startup（仿 heartbeat resume）② `services/heartbeat.py` `_execute_heartbeat` 维护批 | 无（纯新增，不替代路径） | job_manifest 增 `retry_count/last_retry_at/sweep` 字段（同 schema_version 内向后兼容） |
| C9-2 debt 台账 | `memory/consolidation_debt.py` + `memory/control/consolidation_debt.json` | heartbeat 维护批刷新；超阈值 `write_audit_log` + logger.warning | 无 | 阈值进 config（环境变量，非硬编码） |
| C9-3 retention | `memory/indexes/index.sqlite`（反向 ref/引用计数表，C8 口径、可从 MD 重建）+ `memory/retention.py` 归档执行 | heartbeat 维护批 | 无 | 归档落 `memory/.archive/t2/**`，id 解析经索引仍可读（永不硬删） |

### 范围边界决定（2026-07-02）

1. **episode stitch job 的 held 不进 C9-1 重试范围**：segment 证据本体已进 T2，episode 合成滞留表现为"needs_previous/needs_next 段无法 t3_intake"→ 由 C9-2 debt 台账观测。避免 C9-1 范围扩散。
2. **debt 台账先落 `control/` sidecar JSON**（spec §6.2.2 原文允许），SQLite 台账表由 C8 补全；**引用计数直接落 SQLite**（spec §6.2.3 明确要求反向索引），schema 按 C8 口径（派生、可重建），C8 只补不推倒。
3. **startup sweep 只做状态归位**（stale queued/running → held，附崩溃 issue，零 LLM 成本）；**LLM 有界重试挂 heartbeat 节律**（120min，有 lease 防并发）。避免重启风暴打满 LLM。

### 实施记录

#### C9-1 T2 held/failed sweep（2026-07-02）

**改动：**
- 新增 `backend/app/memory/t2/job_sweep.py`：`sweep_stale_t2_jobs`（同步零 LLM 崩溃恢复：stale queued/running → held）、`sweep_t2_jobs`（heartbeat 节律：崩溃恢复 + held/failed 有界重试，`max_retries=3`，超限一次性 `t2_job_retry_exhausted` audit 告警）、`sweep_all_agents_stale_t2_jobs`（startup 全 agent 归位）。每次 sweep 写 `memory/control/t2_job_sweep.json` 报告。
- `segment_package.py`：`run_t2_segment_package_job` 增 `_carry_over_job_fields`——重跑保留 `retry_count/last_retry_at/retry_exhausted_alerted_at/recovered_from`（否则重试计数被 runner 重写抹掉）。
- `services/heartbeat.py`：`_run_t2_job_sweep` wrapper + `_execute_heartbeat` 维护批调用（lifecycle maintenance 旁，best-effort 不阻塞心跳）。
- `main.py` lifespan：startup 调 `sweep_all_agents_stale_t2_jobs`（仅状态归位，LLM 重试留给 heartbeat——避免重启风暴打满 LLM）。

**红测（先红后绿）：** `backend/tests/memory/test_t2_job_sweep.py`，15 个用例——RED 确认（ModuleNotFoundError 全红）→ GREEN 15 passed。覆盖：stale queued/running 归位（附 crash issue）、fresh in-flight 不动、startup 版永不重试、多 agent 遍历、held 重试成功→committed（lineage 从 T0 事件兜底恢复验证）、重试失败计数递增、failed 状态重试、超限一次性告警（重复 sweep 不重发）、committed 跳过、无目录 no-op、control 报告落盘、heartbeat wrapper 真跑真数据、`_execute_heartbeat`/`main.py` 接线断言。

**接线证据（grep）：** `app/services/heartbeat.py:1418 sweep_report = await _run_t2_job_sweep(agent_id)`；`app/main.py:383-385 sweep_all_agents_stale_t2_jobs(...)`。退役：无（纯新增，接线单声明一致）。

**回归：** 受影响面 `tests/memory + tests/services/test_heartbeat.py + tests/runtime/test_t0_to_t2_session_close.py` = 411 passed。全量 `pytest tests -q` = **5308 passed, 1 failed, 1 skipped**；唯一失败 `test_harness_canary_writes_runtime_task_artifacts_and_evolution_ledger`（`long_task_validation_passed=False`）经干净 HEAD（fa383cde）worktree 复跑**同样失败**——main 既有失败，与 C9-1 无关（已单独上报 owner）。

**Commit：** `ada7a97a`（6 files, +934/-1；main.py 采用 hunk 级 staging，规避与另一 session 的 eval 拆弹 WIP 混提交）

#### C9-2 consolidation-debt 台账 + 停滞告警（2026-07-02）

**改动：**
- 新增 `backend/app/memory/consolidation_debt.py`：`assess_consolidation_debt`（纯测量：pending t3_intake 包（segments+episodes 双面）、待 stitch 段（trigger 无产物）、held/exhausted job、active explicit 条目、最老龄）+ `refresh_consolidation_debt`（落盘 `memory/control/consolidation_debt.json` + 停滞时一次性 `memory_consolidation_stalled` audit 告警，恢复清除标记、再停滞重新告警）。
- `config.py` 增 `MEMORY_DEBT_PENDING_AGE_ALERT_HOURS=48` / `MEMORY_DEBT_EXPLICIT_AGE_ALERT_HOURS=72`（停滞阈值归 config，插入位避开 eval WIP hunk）。
- `services/heartbeat.py`：`_run_consolidation_debt_refresh` wrapper（读 settings 阈值传参，模块保持参数化）+ `_execute_heartbeat` 维护批调用（sweep 之后，best-effort）。
- 停滞判定：pending 包超龄 / explicit 条目超龄 / 存在 retry-exhausted job（`t2_jobs_retry_exhausted`——C9-1 的 job 级告警是即时事件，debt 台账提供持续可见面）。episode stitch job 的 held 滞留按范围边界决定 1 经"待 stitch 段"计数覆盖。

**红测（先红后绿）：** `backend/tests/memory/test_consolidation_debt.py`，14 用例——RED（ModuleNotFoundError + wrapper/config 断言全红）→ GREEN 14 passed。覆盖：空库全零、pending 计数与龄（segments+episodes）、absorbed/reinforced/contested/retired 排除、allowed_next=none/archive 不计、待 stitch 判定（有产物不计）、held/exhausted 计数、explicit active 计数与龄、停滞一次性告警＋恢复清标＋再停滞重告警（核心生命周期测试）、exhausted 触发停滞、explicit 超龄触发停滞、control 报告落盘、heartbeat wrapper 真跑真数据、`_execute_heartbeat` 接线断言、settings 阈值存在。

**接线证据（grep）：** `app/services/heartbeat.py` `_execute_heartbeat` 内 `debt_report = await _run_consolidation_debt_refresh(agent_id)`。退役：无（纯新增）。

**回归：** ruff 全过；受影响面 `tests/memory + tests/services/test_heartbeat.py` = 411 passed；全量 = **5323 passed, 0 failed, 1 skipped**（C9-1 时失败的 `test_harness_canary` 本轮同树通过——flaky 特征，已单独上报 owner）。

**Commit：** `e56d83cf`（5 files, +783/-1；config.py 采用 hunk 级 staging 避开 eval WIP）

#### C9-3 retention 引用计数 + 归档（2026-07-02）

**改动：**
- 新增 `backend/app/memory/reference_index.py`：SQLite 反向引用索引 `memory/indexes/index.sqlite`（C8 口径最小集：`refs` 反向索引表 + `id_resolution` id 解析表 + meta）。**纯派生**：`rebuild_reference_index` 是唯一写入口，全量从 MD/JSONL 真相重建（T3 accepted blocks 的 source_ref、active explicit overlay 的 source_refs、episode manifest 的 source_packages 归一化为段包 ref、活跃 + 归档双区包目录、archive_log）。删库零损失。
- 新增 `backend/app/memory/t2_retention.py`：归档执行器。保护顺序：① pipeline（reviewed/closed 且 allowed_next ∈ {t3_intake, episode_stitching} = 待消化债，受 C9-2 监控，不是垃圾）② decision/permission 域证据永不过期（§6.5 对标 Letta）③ 有引用即钉住 ④ 未超期留热区。归档 = `os.replace` 移到 `memory/.archive/t2/**`（保结构）+ append `archive_log.jsonl` + 索引 `mark_ref_archived`——**永不 delete，ref 归档后仍可解析**（§3.6）。
- `config.py` 增 `MEMORY_RETENTION_ARCHIVE_AFTER_DAYS=30`。
- `services/heartbeat.py`：`_run_t2_retention` wrapper + `_execute_heartbeat` 维护批第三项（sweep → debt → retention 顺序）。

**红测（先红后绿）：** `backend/tests/memory/test_t2_retention.py`，15 用例——RED（ModuleNotFoundError 全红）→ GREEN 15 passed。覆盖：三源引用计数（T3/explicit/episode 归一化）、删库重建幂等（派生不变量）、absorbed explicit 不计引用、活跃包 resolve、有引用 365 天不归档、无引用未超期不动、无引用超期 absorbed → 归档且 ref 解析到归档路径内容可读（**永不硬删核心**）、pipeline 双态保护、reviewed+allowed_next=none 死端归档、decision/permission 域永不归档、归档包重建后仍可解析（.archive 回扫）、control 报告、heartbeat wrapper 真跑、接线断言、config 字段。

**接线证据（grep）：** `app/services/heartbeat.py` `_execute_heartbeat` 内 `retention_report = await _run_t2_retention(agent_id)`。退役：无（纯新增；`memory/indexes/` 此前仅 wiki_map.md，SQLite 为全新派生面）。

**回归：** ruff 全过（3 文件经 ruff format 重排）；受影响面 426 passed；全量 = **5339 passed, 0 failed, 1 skipped**。

**Commit：** `b8131742`（6 files, +957/-2；config.py hunk 级 staging）

#### C9 验收：设计不变量自检（spec §7.1 + owner 无双轨条款，2026-07-02）

1. **所有文件产出有明确 writer（无孤儿）**：`control/t2_job_sweep.json` ← sweep（startup+heartbeat）；`control/consolidation_debt.json` ← debt refresh（heartbeat）；`control/t2_retention.json` ← retention（heartbeat）；`indexes/index.sqlite` ← `rebuild_reference_index`（retention 每轮重建，唯一写入口）；`.archive/t2/**` + `archive_log.jsonl` ← retention 归档。全部有真实生产调用点（接线断言测试钉死）。✅
2. **侧写只收敛、知识只成网**：C9 不触及四区语义文件，N/A。✅
3. **智能步骤 LLM 全视野、读取不跑 LLM**：sweep 重试复用 canonical runner（完整三角色 LLM 管线，零机械 fallback）；debt/retention 为纯机械 lifecycle 治理（L2 harness 职权，非语义步骤）。✅
4. **证据只指不可变源 + T2 永不硬删（永不悬空无条件）**：retention 只 `os.replace` 移动、零 delete 调用；归档后 ref 经 `id_resolution` 仍解析、内容可读（测试钉死）；pipeline/decision/permission 保护防误归档。✅
5. **语义写入全过 gate**：C9 无新语义写入面；重试产出的 T2 包走原 Memory Gate + Platform Gate 链。✅
6. **无双轨（owner 2026-07-02 条款）**：三模块均纯新增，无被替代旧路径；`memory/indexes/` 此前仅 wiki_map.md；`control/` 沿用 lifecycle_maintenance.json sidecar 模式而非另起体系。✅

**三 commit 均通过"只含本任务文件"检查**（main.py/config.py hunk 级 staging，eval WIP 零卷入）。C9 三断层一次完整 pass 交付完毕；下一阶段按序为**读侧**（§4.2）。

---

## Memory 全量完工主线（owner 2026-07-02 拍板：全部完工后一次汇报）

执行策略声明：各 Part 依 spec §6.1 顺序推进，中间 commit 保持系统可运行（新机制以并存方式落地），**Part H（C7）完成路径切换与全部废弃路径清退**——双轨在 Part H 收口清零，符合"一个大 pass 内退役"纪律。

### Part A 读侧：两平面分层（2026-07-02）

**改动：**
- 新增 `memory/profile_plane.py`：常驻侧写平面读取器——self/self.md + profiles/{owner,collaborators,domain}.md + explicit overlay active 整份加载（零 LLM、**永不裁剪**），self 的 active 失败模式置顶；同时承载 self.md 结构契约（`## 失败模式` + `- 状态:` 生命周期行，Part D 写侧共用）。超预算 = 写侧收敛失败信号：一次性 `memory_resident_over_budget` audit 告警 + `control/resident_budget.json` 标记（恢复清除），不硬截。
- `relation_graph.py`：`build_relation_graph` 参数化 `page_dirs`（默认仍 wiki/scenes，C7 切换）；新增 `KNOWLEDGE_PAGE_DIRS=("knowledge","milestones")`；`[[k:Title]]` 前缀剥离解析进 knowledge/；前向引用落主目录（knowledge 优先）。
- `wiki_retrieval.py`：`search_wiki_pages` 透传 `page_dirs`。
- `retriever.py`：新增 `_retrieve_knowledge_pages`（PPR top-k over knowledge/milestones，source_type=`knowledge_ppr`，always-on）；**knowledge 命中豁免 LLM rerank 池**（§4.2 读取零 LLM，写入建网 PPR 即终序）。
- `memory_service.build_memory_context`：常驻块顶层拼装（不进 assembler → 天然免个体裁剪）；检索预算 = memory_budget - resident 实际占用（下限 2000）；resident 含 overlay 时剔除 retriever 的 overlay 重复条目。
- `config.py` 增 `MEMORY_RESIDENT_BUDGET_CHARS=12000`。

**红测：** `tests/memory/test_read_side_two_planes.py` 10 用例，RED（9 失败+1 空池 vacuous）→ GREEN 10 passed。覆盖：整份加载与置顶排序、新 agent 空、inactive overlay 排除、超预算不截、一次性告警+恢复清标+再告警、knowledge/milestones 图构建与 k: 前缀/前向引用、PPR 检索命中、knowledge 豁免 rerank 池（spy 断言）、端到端 resident+检索拼装、config 字段。

**接线证据：** `memory_service.py build_memory_context` 内 `load_resident_memory`/`check_resident_budget`/`_retrieve_knowledge_pages`（经 retriever.retrieve 主流程 always-on）。退役：无（旧 t3 direct 读法保持至 Part H 收口）。

**回归：** ruff 全过；受影响面 tests/memory+tests/runtime = 1043 passed；全量 = 5352 passed / 1 failed / 1 skipped——唯一失败 `test_alembic_single_head_is_current_closure_head` 系另一 session 的 commit `1378bf74` 新增 migration 未同步测试内 pin 常量（`retire_atlassian_rovo_0629` vs 新 head `web_chat_final_message_idempotency_0702`），非本 part 面，留给该 session 收尾（终汇报向 owner 点名）。

**Commit：** `7f53ad6d`（8 files, +708/-18；config.py hunk 级 staging）。

### Part B T3 Platform Gate 四区化（2026-07-02）

**改动：**
- `t3_platform_gate.py`：目标集从旧四文件扩展为三类——固定收敛文件 `PROFILE_PLANE_TARGETS`（self/self.md + profiles×3）、动态页 `memory/(knowledge|milestones)/<slug>.md`（slug 正则排除 `/`、`.`，路径穿越无法通过校验）、旧四文件兼容（Part H 收口）。新操作：`upsert_page`（整页写入；**新 knowledge 页强制 ≥1 Relations 边，前向引用算数**，孤儿页 hold）、`upsert_entry`（### markdown 条目，`<!-- id: -->` 锚定，同 id 原位替换、按 `## section` 定位插入，缺 section 自动创建）、`retire_entry`（**机械只标记** `<!-- retired: -->`，收敛环工序 4 负责真正删除——gate 永不机械删除 LLM 内容）。base_revision/evidence/rubric 纪律原样继承；`_read_target` 容错不存在文件（新文件 empty-sha 语义）。
- `md_store.ensure_t3_layout`：增建 `TWO_PLANE_DIRS`（self/profiles/knowledge/milestones，只建目录——文件由 governed writer 创建）。

**红测：** `tests/memory/test_t3_gate_four_planes.py` 13 用例，RED（10 失败）→ GREEN 13 passed。覆盖：新 knowledge 页 commit、无 Relations hold、前向引用满足成网、stale sha rebase、milestones 无需 Relations、slug 三种非法形态、self 条目建/改（同 id 原位替换唯一性）、retire 标记不删内容、缺 id 注释 hold、无 t2/explicit 证据 hold、legacy XML block 兼容、layout 建新目录。

**回归：** ruff 全过；tests/memory 411 passed；全量 = 5365 passed / 2 failed / 1 skipped（两个失败均归属另一 session：alembic head pin 债 + `session_control_plane` WIP 中间态——该文件在其未提交修改集内）。

**Commit：** `03c85489`（4 files, +527/-11）。

### Part C knowledge 写侧：curator 视野 + 网络保护（2026-07-02）

**改动：**
- `t3_consolidation.py`：`build_t3_neighborhood` 增两平面区块——Profile Plane（固定文件 base_revision + 现有 `###` 条目 id/标题清单）+ Knowledge Plane（knowledge/milestones 页清单：title/status/base_revision/Current Claim 首行/Relations 边摘要）。**L1 完整视野**：update-vs-create 是 LLM 判断，必须看得见现有网络。`allowed_target_files` 扩为旧四 + PROFILE_PLANE_TARGETS + 动态页模式（含成网契约内联说明）。
- `t3_platform_gate.py`：knowledge 页更新的 **Contradictions 保留校验**——旧页 `## Contradictions` 每一行必须在新页中幸存，否则 hold（§3.4"冲突进 Contradictions 不删旧"的机械保护）。
- `templates/HEARTBEAT.md`：`<allowed_targets>` 四区化 + `<two_plane_curation>` 教学节（update-vs-create 判据、低置信不覆盖 Current Claim、强制成网+前向引用）+ `<phase_4_revised_patch>` 新操作教学。

**红测：** `test_knowledge_write_side.py` 4 用例 RED→GREEN。

### Part D 侧写写侧：operation patch 管线（2026-07-02）

**改动：**
- `t2/prompts.py`：`LEARNING_BRAIN_LABELS_PROMPT` 增 `<four_plane_signals>` 轴（self_signal 自我认知信号引述 / nutrients 四区养分归类 / milestone_signal 判据命中）——**工序 1/2 物理同 call**（spec §2 两道逻辑工序一次 LLM 调用）；版本 bump `t2.learning_brain_labels.v2`（旧断言同步）。新节点可选，T2 validator 向后兼容（红测钉住）。
- `HEARTBEAT.md` `<two_plane_curation>`：母题+场景条件三档（80% confirm / 15% 场景行 / 5% 新母题）、add-vs-update 交 LLM、失败模式生命周期、**反例下调**（explicit overlay 中 origin=session_feedback 负极性条目 = 最强打脸信号 → 降熟练度/重开失败模式/退役条目并留 fb 证据）。
- 反例下调输入的生产链**无需新接线**：`write_session_feedback_overlay` → explicit overlay → `discover_pending_t3_sources` → batch（红测钉住既有链路真通）。

**红测：** `test_profile_plane_write_side.py` 5 用例（prompt 教学 ×2、新 labels 节点过校验、HEARTBEAT 教学、feedback→batch 生产链）RED→GREEN。

### Part E milestones：判据 + 追认（2026-07-02）

**改动：**
- 判据①②③（owner_feedback/major_failure/first_success）搭 labels call（Part D 的 milestone_signal 轴）；判据④追认落 `HEARTBEAT.md`：要挂 `[[ms-]]` 锚点而 T2 段未升级 → **同一 patch** `upsert_page` 追认升级，锚点=可选导航、证据必须不可变 t2-（§4.1 分离）。归档 retention 复用 C9-3（milestones 页被引用即钉住）。
- gate 组合原子性由 Part B 天然支持（红测钉住：一个 patch 建 milestone 页 + self 条目引用它，两文件原子落盘）。

**红测：** `test_milestones_criteria.py` 2 用例 RED→GREEN（另一判据教学并入 Part D 测试）。

**C/D/E 合计回归：** ruff 全过；tests/memory 422 passed；全量 = 5376 passed / 4 failed（3 个归属外部：alembic pin 债、session_control_plane WIP×2——后者已被该 session 自行收掉；1 个 `test_has_t2_to_t3_guidance` 是本 pass 改 HEARTBEAT.md 缩写了 legacy 路径所致，**已修**：legacy 行写全路径，kairos 22 passed）。

**Commit：** `2cc5c720`（8 files）+ HEARTBEAT.md 路径修复随 Part F commit。

### Part F 成长机制：收敛环 + 提名交接（2026-07-02）

**改动：**
- 新增 `memory/convergence.py`（工序 4 机械半）：侧写平面脏度测量（字数超阈值/retired 条目积压/读侧 resident 超预算信号联动）→ `control/convergence_dirtiness.json` 台账 + `build_t3_neighborhood` 注入 `⚠ CONVERGENCE NEEDED` 警示（curator 视野触发）。LLM 半 = 收敛重写本身，经 gate。
- `t3_platform_gate.py` 新操作 `rewrite_file`：侧写固定文件**全文重写**（工序 4 执行面）——`convergence_note` 必填（审计）、拒绝清空非空文件（防误删）、base_revision 冲突检测、旧版自动存档（复用 `_atomic_write_targets` rollback staging，红测钉住备份存在且含旧内容）。
- `config.py` 增 `MEMORY_CONVERGENCE_MAX_CHARS_PER_FILE=6000` / `MEMORY_CONVERGENCE_MAX_RETIRED_ENTRIES=2`。
- `services/heartbeat.py`：`_run_convergence_dirtiness_refresh` wrapper + 维护批第四项（sweep→debt→retention→convergence dirtiness）。
- `HEARTBEAT.md` `<convergence_loop>` 节：完整文件输入（L1 不截断）、消重取 refs 并集、清 retired、"收敛≠一味变短"、**侧写收敛 vs 知识织网治理不可混用**。
- `DREAM.md` 新增 `<self_to_soul_nomination>`（工序 5：长期稳定+高置信+零反例三条件、提名走既有 Soul Memory Gate + Platform Soul Gate + owner 确认、promotion 后 self 条目保留）+ `<self_to_skill_handoff>`（工序 6：`- skill候选:` 标记 → 既有 skill 蒸馏 lane 拾取、固化后 `已固化 → [[skill-x]]` 双向链、记忆永不直造 Skill）。工序 5/6 复用既有 gate 链（auto_dream 的 soul gate、skill_distiller 的 candidate 读取），Part F 落教学与标记约定；distiller 输入面从 legacy capabilities 迁到 self.md 归 Part H（与旧路径退役同 pass）。

**红测：** `tests/memory/test_growth_mechanisms.py` 13 用例 RED→GREEN。覆盖：脏度双因子/干净文件/台账落盘/neighborhood 警示、rewrite_file 收敛成功+旧版存档、缺 note hold、stale sha rebase、清空拒绝、heartbeat wrapper 真跑+接线断言、HEARTBEAT/DREAM 教学断言、config 字段。

**回归：** ruff 全过；memory+heartbeat 面 495 passed；全量 = 5391 passed / 4 failed（全部外部归属：alembic pin 债 + `test_chat_artifact_delivery`×3——均为另一 session web chat 持久化线的活跃面）。

**Commit：** `c799575e`（8 files；config.py hunk 级 staging）。

### Part G 来源 ref 体系：短 id 家族 + 活引用 tombstone（2026-07-02）

**改动：**
- `reference_index.py` 扩展：`id_resolution` 表增 `kind` 列并登记全 id 家族——`t2-<hash>` 短证据 id（与 package_id 同源哈希）、`ms-`/knowledge 页 slug（导航解析）、explicit entry id；`resolve_memory_ref` 统一解析入口（认 `t2://` 完整 URI / `t2-` 短 id / `ms-` / `ex-` / `explicit://`）。**证据永不悬空无条件**：`mark_ref_archived` 归档时同路径的短 id 行随迁（红测钉住归档后 `t2-` 仍解析到 `.archive` 且内容可读）。
- **活引用 tombstone**（§4.1 次断点闭合）：`record_entry_tombstones`（真相源 = append-only `control/tombstones.jsonl`，SQLite `tombstones` 表为可重建投影）+ `resolve_entry_id` 链式解析（visited 环保护）。收敛合并的声明面 = `rewrite_file` 的 `<tombstone old new/>` 子节点，gate commit 成功后落账。
- 证据链 vs 活引用分离（§4.1 主断点）：证据只指不可变 t2-/ex-/fb-（HEARTBEAT.md Part E 已教），锚点 [[ms-]]/[[k:]] 仅导航。

**红测：** `tests/memory/test_source_ref_system.py` 8 用例 RED→GREEN。覆盖：短 id 解析、**归档后仍解析**、ms-/完整 URI/explicit 解析、tombstone 记录+链式解析+jsonl 真相、环安全终止、rewrite_file tombstone 声明流入索引、删库重建后 tombstone 幸存（jsonl 重建）。

**回归：** ruff 全过；tests/memory 443 passed；全量 = 5402 passed / 2 failed（alembic pin + web_chat_runtime，均外部归属）。

**Commit：** `fed455fa`（4 files）。

### Part H C7 迁移 + 路径统一清退：接线单（开工承诺，2026-07-02）

**退役清单（grep 盘点，收工逐项零残留验证）：**
| 废弃路径 | 现存调用面 | 处置 |
|---|---|---|
| `memory/t3/{episodes,user,worker,capabilities}.md` 运行时读写 | retriever（direct/index_first/shadow）、md_store T3_FILE_SPECS 骨架、gate legacy 目标+四操作、t3_consolidation legacy blocks 区、prompt memory.py、workspace.py、auto_dream/heartbeat 读、agent_evolution_view、kernel 引用 | 运行时全切两平面；旧文件由迁移脚本重组后归档隔离 |
| `memory/learnings/**` | retriever no-op、hooks_setup 注释、prompt 提及、extract_agent/reflection | 提及清除；已是 no-op 的删函数 |
| `memory/sessions/**`（legacy T2 root） | segment_package/_discover/t3_consolidation/debt/retention 双 root 扫描 | 保留只读归档扫描（证据永不悬空），新写零 |
| `wiki/`、`scenes/` 页目录 | relation_graph 默认 _PAGE_DIRS、wiki_retrieval 默认、retriever._retrieve_wiki_pages（derived 开关） | 默认切 knowledge/milestones；derived 开关与旧读法删除；旧目录 hygiene 隔离 |
| `understanding_store.py` | 零调用 | 删除（含测试） |
| `scene_curator.py`/`wiki_curator.py` | 零外部调用（孤儿） | 删除（含测试）；活的 relation_graph/wiki_retrieval 保留 |
| `t2_store.py`/`extract_agent.py`/extract_queue*/admin backfill | admin API + queue replay | 迁移工具化：仅迁移/导入脚本引用，运行时零接线（已达）；admin backfill 端点保留为数据导入面并明确标注 |
| retriever LLM rerank（读侧零 LLM） | build_memory_context | 删除（读取不跑 LLM 硬约束收口） |

**新建：** `app/scripts/migrate_memory_two_planes.py`（dry-run 默认 + `--apply --confirm` 安全门；LLM 全文判定重组：soul 拆纯/worker constraint 两分/capabilities 三分/episodes→milestones 筛选；无模型配置 → held 不动数据；apply = 原子落盘新区 + 旧四文件移 `memory/.archive/legacy_t3/` + 报告）。
**配套同步：** HEARTBEAT.md/DREAM.md legacy 段删除 + 各 agent 克隆同步机制核查；`docs/memory-vault-path-contract` 更新；hygiene.py 认新路径与旧 t3 隔离规则；HR 模板产应然 soul + self 骨架；`wiki_map.md` 改指两平面。
**Breaking 边界（安全门非 MVP）：** 运行时代码只认新路径；存量 agent 数据迁移由脚本执行——真实生产数据 `--apply` 属不可逆操作，交付物为"代码全切 + 迁移工具 dry-run 验证就绪"，生产 apply 由 owner 确认执行（唯一例外条款）。

### Part H 实施记录（2026-07-02，三个 commit 分批落袋）

**进度 1（`6ecd1738`）**：迁移工具 `app/scripts/migrate_memory_two_planes.py`（dry-run 默认/`--apply --confirm`、LLM 全 corpus 重组 plan、原子落盘、旧文件归档 `.archive/legacy_t3/`、幂等 marker、6 红测全绿）；读侧清退（retriever 删 direct/index-first/shadow/derived/legacy/LLM rerank——**读取零 LLM 收口**，rerank 配置成惰性参数并被测试钉死）；kernel compaction 恢复清单、workspace 种子、filesystem 工具文档、heartbeat T3 摘要全切两平面。

**进度 2（本 commit）——路径统一清退主体：**
- **gate**：旧四文件目标与 append/replace/retire/reinforce_block 四操作删除（`LEGACY_T3_FILES` 仅为迁移工具命名保留）；`TARGET_VIEW_VALUES` 收缩两平面。
- **读取面全切 `plane_read.py`**（新统一模块）：`search_memory(facts)`/`load_memory`/update/retire 判定、knowledge_read_model（overview/entries/pages 走 knowledge|milestones）、memory/backend、self_evolution_audit、auto_dream 计数、`rebuild_index`→两平面 wiki_map。
- **工序 6 输入面接线**（Part F 遗留）：skill_distiller 的 skill/workflow 候选从旧 `[container=]` T3 标记切到 self.md `- skill候选:`/`- workflow候选:` 行；`已固化 → [[skill-x]]` 双向链的写半落 `plane_read.mark_profile_entry_promoted`（平台记账职权，幂等，红测钉死）。
- **memory_navigation prompt 节退役**（数据流+模块+invoker 供给线删除；engine 6 处惰性形参因另一 session 活跃改 engine 暂留一个周期，终验收补摘——显式取舍非遗忘）。
- **死文件删除**：`t3_store.py`（`looks_episodic_observation` 护栏迁 explicit_overlay）、`legacy_migration.py`（workspace 挂点换 `_archive_legacy_memory_files`：legacy 单文件记忆归档 `.archive/legacy_import/` 永不删）、`retrieval_eval.py`、`scene_curator.py`、`wiki_curator.py`、`understanding_store.py`；md_store 旧条目生态（T3_FILE_SPECS/entry manifest/heat/dedup/prose/promoted stamp/load/search + 全部孤儿 helper/dataclass/正则簇）全删（1150→301 行，存活面=xml block 解析/两平面布局/jaccard 去重/bm25/wiki_map 重建/退役语义 retirement 空清单）。
- **hygiene/admin**：flat-T3 prose backfill 步骤退役（hygiene 报告字段清零语义、admin 端点返回 `status=retired`）；evolution_daemon 的旧 T3 形态修复 lane 退役（两平面形态纪律由 gate 操作本身承担）。
- **relation_graph 默认切 KNOWLEDGE_PAGE_DIRS**；HEARTBEAT.md legacy 教学三段删除+决策矩阵两平面化；HR 模板（hr_agent_template/HEARTBEAT.md）两平面化；克隆 marker 升级 `<two_plane_curation>`（存量 agent startup 重新克隆）；新 agent 创建流产 self.md 骨架（结构由平台、语义由经历经 gate）。
- **发现并修复的真实缺口**：overlay lifecycle metadata 未透传检索层（activation 抑制对 explicit 条目失效）；resident 常驻块未过滤 PL3/PL4 overlay 条目（敏感条目改走检索+activation 门）。
- **path contract 文档**已更新两平面布局与 control sidecar 清单。
- **测试面改造**：19+ 文件——gate 主套 4 用例改新载体（staging 视野/wiki_map 重建/absorbed/reinforced 纪律保留）、candidate lane 8 用例 self.md 载体、read model 16 用例两平面、boundary/architecture 守卫改"退役模块不存在"断言、6 个纯旧生态测试文件删除。

**误操作坦白（owner 必读）**：navigation 手术中 `git checkout backend/app/runtime/invoker.py` 恢复文件时，误抹了另一 session 在该文件的未提交 eval 拆弹修改（behavior-report 门拆除）。已按其测试期待等价重做（`_load_latest_skill_distiller_behavior_report` 删除 + RuntimeConfig 装配行删除），invoker 测试恢复绿；该 session 若有超出测试可见面的意图需其复核。同批 `test_standalone_prompt.py` 曾被整删后 checkout 恢复并只摘 navigation 单用例。三 part 共享教学面（HEARTBEAT.md/prompts.py），合并单 commit。

**进度 2 收尾（同 commit）**：`GET /agents/{id}/memory` facts 端点从已删 `parse_t3_facts` 切 `plane_read`（profile 条目 + knowledge/milestones 页元数据，零前端/测试消费者验证后保留为两平面查看面）；`.github/workflows/harness-ci.yml` 摘除指向已删 `retrieval_eval` 模块的死步骤（CI 否则必炸），`test_harness_ci_workflow` 守卫翻转为 `not in` 断言；`wiki_retrieval.py` 模块 docstring 死指针清除。事故记录：删除 `parse_t3_facts` 时误切 `_CJK_RANGE_RE` 常量（存活 `_bm25_tokenize` 依赖）致 tests/memory 3 失败，恢复常量后归绿——bm25 簇消费者为 `wiki_retrieval.py`（PPR 检索层）已 grep 钉死。
**退役零残留核查（grep 全仓）**：`parse_t3_facts`/`_filter_facts_by_date`/`T3_FILE_SPECS`/`build_t3_entry_manifest`/`ACCEPTED_T3_TARGETS`/`legacy_migration`/`scene_curator`/`wiki_curator`/`ParsedMemoryEntry`/`T3MemoryEntry` = 0；`understanding_store`/`t3_store` 仅存"退役契约"架构守卫断言与来源归属注释；`memory_navigation` 仅存 engine/prompt_builder/turn_envelope 惰性形参链（invoker 供给恒 None → 恒空字符串，Part J 与 contracts 字段一并摘除）。
**测试证据**：tests/memory + api/test_memory_api + tools/test_memory_handler + services/test_auto_dream = 373 passed；全量 `pytest tests -q` = **1 failed, 5251 passed, 1 skipped**（唯一失败 `test_self_evolution_bakeoff` 属并行 session eval 拆弹 WIP 面，干净归属已验证）；ruff check/format 全过。

**Part H commit：** `90018a21`（84 files，+999/−6920；hunk 级 staging 零 eval WIP 泄漏——skill_distiller 只取 `_CANDIDATE_MARKERS`/`mark_profile_entry_promoted` 两 hunk，harness-ci.yml 只取 retrieval_eval 步骤删除 hunk；commit 后干净 HEAD worktree 复验 `test_skill_distiller + test_promotion_hard_gate + test_harness_ci_workflow + tests/memory + test_candidate_lane = 348 passed` 证明混改拆分后 HEAD 自洽）。

## Part I C8 SQLite 派生表补全（spec §6.4）

**范围核对**：五件套中反向 ref 索引 / id 解析 / 引用计数三件 Part G/C9 已建；本 part 补齐 `t2_label_axes`（复合标签分轴）与 `consolidation_debt_history`（debt 台账表），并顺手修复 Part H 漏网的真实缺口。

- **`t2_label_axes`**（`reference_index.py`）：rebuild 时解析每个 live segment 包的 labels.md `<t2_labels>` 块 → 按轴入行（continuity_state / confidence / source_integrity / risk_flag / system / memory_domain / nutrient_plane / self_signal / milestone_criteria）；缺轴=无行（evidence-gap 纪律，不猜）；package_ref 用 spec §4.1 短 id。
- **`consolidation_debt_history`**：append-only 观测真相 = `memory/control/consolidation_debt_history.jsonl`（`refresh_consolidation_debt` 每次评估追加 + 同步 upsert 表行，best-effort 不破坏 debt 刷新本身）；rebuild 从 jsonl 重放全表——删 index.sqlite 后两表均从 MD/jsonl 还原（纯派生红测钉死）。
- **Part H 漏网修复（真实缺口）**：`_t3_reference_rows` 只扫已退役的 `t3/` 四文件，两平面证据引用（self/profiles 条目 `- 证据: t2-x` 行、knowledge/milestones 页 Evidence 段）无人扫 → retention 会把被平面引用的 T2 当零引用归档。新增 `_plane_reference_rows`：短 id 命中同时落短 id 行 + 规范化 `t2://` URI 行（retention 按 URI 计数）；旧 t3/ 扫描保留（迁移前存量兼容）。红测：`test_plane_evidence_refs_count_for_retention`（knowledge 页 + self.md 各引一次 → URI 计数 = 2）。
- **读者接线**：`knowledge_read_model.build_memory_observability`（debt 最新态 + 轨迹 ≤100 行 + label 轴聚合 COUNT；空 agent 返回空结构不抛错）→ 新 API `GET /agents/{agent_id}/knowledge/observability`（`api/agent_knowledge.py`，与既有六端点同构薄封装，check_agent_access 门）。路由守卫测试 six→seven 更新。
- **触发面（零新链）**：rebuild 扩展自动被既有触发点覆盖——`t2_retention`（heartbeat 维护批）、`_ensure_index` 惰性、迁移工具；debt 表行由 heartbeat 维护批的 `refresh_consolidation_debt` 实时 upsert。

**接线证据（grep）**：`rebuild_reference_index` 生产调用点 = t2_retention.py:70（heartbeat 链）+ reference_index.py:261（惰性）+ migrate_memory_two_planes.py:189；`refresh_consolidation_debt` 生产调用点 = heartbeat 维护批 `_run_consolidation_debt_refresh`；`build_memory_observability` 消费者 = api/agent_knowledge.py observability 端点。
**测试证据**：`tests/memory/test_c8_derived_tables.py` 8 红→8 绿；tests/memory + read model + heartbeat_kairos = 336 passed（唯一改造=路由守卫 six→seven）；ruff check/format 全过。全量 `pytest tests -q` = **1 failed, 5259 passed, 1 skipped**（唯一失败仍为并行 session 的 `test_self_evolution_bakeoff`，归属外部）。

**Part I commit：** `80d207e5`（7 files，+645/−8）。

## Part J 终验收（2026-07-02 收口）

### memory_navigation 惰性形参链补摘（Part H 挂账清偿）

另一 session 的 engine/contracts 改动已收敛入库，engine.py 可安全动刀：`ResolveMemoryNavigationContext` 类型别名、`KernelDependencies.resolve_memory_navigation_context` 字段、解析块（3082-3089）、7 处 `memory_navigation=` 传参全删；`prompt_builder.build_dynamic_prompt_suffix` 形参与 § Memory Navigation 渲染块删；`turn_envelope` manifest 字段（形参×2/add 行/传参）删；`test_prompt_cache_integration` 的 navigation callback 素材与 2 条断言摘除（frozen/dynamic 缓存边界语义由 retrieval 断言继续钉住）。摘后 kernel+runtime 套件 869 passed。附带清除 `hygiene.py` 注释里的 `backfill_t3_prose` 死指针。

### 退役零残留终查（grep app/ 全量）

`parse_t3_facts` / `T3_FILE_SPECS` / `build_t3_entry_manifest` / `mark_t3_entry_promoted` / `ACCEPTED_T3_TARGETS` / `memory_navigation` / `ResolveMemoryNavigationContext` / `legacy_migration` / `scene_curator` / `wiki_curator` / `retrieval_eval` / `backfill_t3_prose` / `validate_and_normalize_t3` / `t2_rerank` 全部 = **0**。`looks_episodic_observation` 仅存 explicit_overlay 定义 + memory handler 消费（护栏迁移后的正确存活）。

### spec §7.1 五不变量自检（逐条证据）

1. **所有文件产出有明确 writer**：四区文件 = Platform Gate 四操作唯一写入（HR/agent_manager 只产骨架）；T2 包 = segment_package builder；explicit = save_memory 写门；soul.md = Dream soul 双 gate；index.sqlite = rebuild_reference_index 单 writer + debt 实时 upsert（同模块族）；control sidecars 与 wiki_map 各有单一模块 writer。
2. **侧写只收敛、知识只成网**：profile plane 仅 upsert_entry/retire_entry + rewrite_file 收敛环（convergence_note 必填、拒清空、tombstone 声明）；knowledge plane 仅 upsert_page（强制 ≥1 Relations 边、Contradictions 保留）；`TARGET_VIEW_VALUES` 收缩两平面四值。
3. **智能步骤 LLM 全视野、读取零 LLM**：T2 summary/labels 全 corpus 单次 LLM 调用；T3 consolidation neighborhood 两平面区块全量注入；C7 迁移 plan 全 legacy corpus；读侧 resident 整份直读 + BM25/PPR 检索，rerank 已删。
4. **证据不可变 + T2 永不硬删 + 活引用 tombstone**：t2-/ex-/fb- 短 id 不可变；retention 只归档 `.archive/t2/**`，归档后 id 经 id_resolution 仍解析（短 id 随迁）；rewrite/retire 落 `control/tombstones.jsonl`；C8 把两平面证据引用接进反向索引（plane 引用计数保护热区）。
5. **语义写入全过 gate、soul 过 owner、durable 不绕 gate**：write_gate 封禁 memory/ 直写；soul 变更走提名+Soul Memory Gate+Platform Soul Gate；explicit 唯一入口 save_memory（PL4 拒绝）；skill 候选晋升的双向链由平台记账（mark_profile_entry_promoted）。

### 无双轨自检

旧 T3 四文件运行时 writer = 0（仅迁移工具重组 + 归档只读扫描）；读侧唯一路径 = plane_read + retriever(resident)/wiki_retrieval(检索)；t3_store/understanding_store/scene·wiki curator/navigation/rerank/entry-manifest 生态全部单轨退役，无新旧并行循环。

### 终验收数字

kernel+runtime 套件 = **869 passed**；全量 `pytest tests -q` = **1 failed, 5259 passed, 1 skipped**——唯一失败 `test_self_evolution_bakeoff` 为并行 session eval 拆弹 WIP 面（Part H/I/J 三轮全量一致，干净 HEAD 归属已验证）；ruff check/format 全过。

### 交付边界与 owner 待办

- 生产存量数据迁移属唯一不可逆步骤：工具 `python -m app.scripts.migrate_memory_two_planes`（dry-run 默认；`--apply --confirm` 才落盘；无模型配置自动 held）。代码已全切两平面，**存量 agent 的 t3 四文件重组需 owner 在生产环境执行**（先 dry-run 审 plan，再 apply）。
- engine 内其余另一 session 活跃面未触碰；`test_self_evolution_bakeoff` 修复归其 eval 拆弹主线。
