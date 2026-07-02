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

**Commit：** 见 git log（C9-3 commit）
