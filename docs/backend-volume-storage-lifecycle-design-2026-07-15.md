# Backend Volume 存储生命周期与冷热分层设计

> 状态：P0 启动期写放大止血与 transaction payload 生命周期底座已于 2026-07-15 部署；21,163 个历史 default-Skill transaction 已完成 authority/backfill。生产事故处置已完成 restore drill、两批 transaction 精确 physical sweep、无引用 web-fetch cache 清理、PostgreSQL ACK trace spool 收敛，以及 retry-exhausted T2 可重建 staging payload 逐出；容器文件系统占用已降至 `11,316,330,496` bytes（24%）。T0、当前 Memory、10 个非 exhausted/running T2 job、workspace、workspace snapshots、当前/held transaction 与其它核心数据均未删除；Object Storage、snapshot CAS、sealed T0 cold archive 和常态化 trace/cache/T2 lifecycle 仍待 Group 8 后续施工。
>
> 日期：2026-07-15。
>
> 范围：Hive Railway production 的 `backend-volume`、Agent workspace、Memory T0/T2、workspace snapshot、Agent asset transaction、invocation trace 和可重建缓存。
>
> 本文定义机制、迁移、保留和验收，并持续记录生产施工证据。§2.3.1 记录启动期止血，§2.3.2 记录已落地的 transaction lifecycle 与 physical receipts，§2.7 记录事故期安全清理和明确停止边界。不得把事故期一次性处置冒充完整 retention/cold-storage 闭环：Object Storage/resolver、跨资产 ref/pin/lease/legal hold、T2 authority/replay、snapshot CAS、T0 cold archive 与常态化 trace/cache policy 仍未完成。

## 0. 文档定位

本文解决的问题不是“怎样一次性删掉一些大目录”，而是建立一套有权威边界、可恢复、可审计、可持续运行的存储生命周期，使以下条件同时成立：

1. 停止无变化写入和完整历史复制导致的持续增长。
2. 保住 T0、Agent 当前状态、合法快照和未完成 T2 job 等必须保留的数据。
3. 将大型不可变数据从热 POSIX Volume 迁移到适合长期保存的 Object Storage。
4. 让任何自动清理都只依据精确机械事实，不依据自然语言、目录名猜测或“看起来不重要”。
5. 删除前经过 inventory、引用校验、quarantine、grace window 和再次校验，能够恢复并留下 receipt。
6. 不以削弱 Agent Memory、恢复、fork、rollback 或审计能力换取磁盘下降。

本文补充而不替代以下 canonical contract：

- `docs/memory-vault-path-contract-2026-06-23.md`
- `docs/agent-memory-md-first-spec.md`
- `docs/agent-memory-purity-spec.md`
- `docs/hive-sota-master-goal.md`

如果本文的物理冷热分层获得批准，实施时必须同步更新 Memory Vault Path Contract，明确“逻辑 canonical path”和“物理 storage location”的区别；不能让业务代码通过裸 `Path.read_text()` 绕过 storage resolver。

## 1. 决策摘要

### 1.1 推荐目标

不拆成两个 PostgreSQL，也不尝试给同一个 Railway `backend` Service 挂两块 Volume。

目标形态是：

```text
                       +------------------------------+
                       | PostgreSQL: control authority |
                       | tenant / manifest / ref / pin |
                       | lease / retention / GC receipt|
                       +---------------+--------------+
                                       |
                         authoritative metadata
                                       |
        +------------------------------+------------------------------+
        |                                                             |
+-------v----------------------+                       +--------------v----------------+
| Hot POSIX: /data/agents      |                       | S3-compatible Object Storage |
| current workspace            |                       |                               |
| soul / current memory / skill|                       | durable bucket                |
| open T0                      |                       | - sealed T0                   |
| in-flight transaction        |                       | - retained source evidence   |
| bounded spool/cache          |                       | - snapshot content blobs     |
+------------------------------+                       |                               |
                                                       | rebuildable bucket            |
                                                       | - reconstructable T2 staging |
                                                       | - web conversion/cache       |
                                                       | - acknowledged trace archive |
                                                       +-------------------------------+
```

因此，准确表述是：

- 一个 PostgreSQL 权威元数据层；
- 一个热 Volume；
- 两个不同删除权限和保留策略的对象空间：`durable` 与 `rebuildable`。

### 1.2 核心顺序

完整修复必须按以下依赖顺序执行：

1. 先阻止 Skill seeding、transaction payload、snapshot 和 trace 继续无界增长。
2. 建立 tenant-scoped blob/ref/retention 权威和统一 storage resolver。
3. 修复 T2 tenant authority，保证 held job 可以合法重放。
4. 让所有新写入经过新边界，并保持旧数据可读。
5. 回填旧数据、做 byte/hash/count parity，再切换读取位置。
6. 生成 production dry-run manifest。
7. 经明确确认后 quarantine；常态自动策略观察 grace window 后才 physical sweep。事故期只有在逐对象 restore drill、独立 immutable manifest、二次 hash/count/authority 复核和显式 operator disposition 同时成立时，才允许对该精确 manifest 做即时 sweep；该例外不得泛化到 T0、Memory、snapshot、workspace、held/current transaction 或未完成 job。

不可逆删除的确认门是安全要求，不是分阶段交付或 MVP。代码、迁移、回填、观测、恢复和测试仍应在同一完整施工轮中完成。

## 2. 当前生产事实

### 2.1 Railway 存储状态

2026-07-15T15:28Z 最新复核结果：

- `backend-volume` 挂载于 `/data/agents`。
- Railway UI 配额仍为 `50 GB`；容器内 `df -B1 /data/agents` 的文件系统总量为 `48,891,670,528` bytes。
- 止血部署前容器使用量为 `28,648,972,288` bytes；止血部署后为 `28,650,721,280` bytes，均为 `59%`。用户随后观察到 Railway UI `30.27 GB`，但 UI/GraphQL 是滞后采样面；生产清理的即时机械事实源为同一挂载点的容器内 `df`、逐目录 allocated bytes、immutable manifest 和 receipt。
- 完成 §2.7 的精确清理后，`df -B1 /data/agents` 为 total=`48,891,670,528`、used=`11,316,330,496`、available=`37,558,562,816`、usage=`24%`。同卷 quarantine 为 0 bytes；fresh transaction GC=`candidate_count=0/hold_count=2091`，fresh sweep=`candidate_count=0`。
- 本文早期的约 `24.80 GB`、`27,111.91552 MB` 和 UI `30.27 GB` 都是事故过程快照，不再代表当前内核占用；后续曲线必须同时标注 Railway 采样时间和容器 `df` 时间。
- PostgreSQL Volume 约 `9.60 GB`，与 `backend-volume` 已经物理分离。
- Redis Volume 约 `1.50 GB`。
- production 当前没有 Railway Bucket。

所以，`backend-volume` 的大数据主要是文件系统数据，不是“数据库迁移备份堆在同一个数据库里”。

### 2.2 主要数据组成

| 类别 | 当前只读取证 | 权威判断 | 当前动作边界 |
|---|---:|---|---|
| Agent asset transaction journals | lifecycle 部署后原始 inventory 为 23,224 个、payload `11,836,695,357` bytes；两批 sweep 后整个 transaction tree allocated=`777,310,208` bytes | 历史 committed payload 是容量异常主因；journal、当前 revision、hold 与恢复元数据仍保留 | fresh GC/sweep 均无 candidate；2,091 个 policy hold 不动 |
| `active_skill_package_install` | 先对 11,977 个原过期 payload 做 restore drill 后精确重隔离并释放 `6,211,996,397` bytes；再只按 `next_revision < current_revision` 释放 9,118 个 superseded payload、`5,176,697,828` bytes | exact-match 增长已停止；旧 revision 可由当前 revision/journal 和 receipts 证明已被替代 | current/latest revision、68 个 superseded dry-run hold、其它 policy hold 均保留 |
| `evolution/skill_review.md` transaction payload | 约 7.10 GB；当前实际 `skill_review.md` 总计仅约 25.4 MB | 全文件 append + stage + backup 产生约 280 倍放大 | 改为 append delta transaction |
| T0 `source.md` + `events.jsonl` + indexes | allocated=`6,365,040,640` bytes | canonical portable evidence 与 readable projection，不是异常垃圾 | 本次零删除；后续只能 sealed archive + byte/hash/order replay |
| Workspace snapshots | allocated=`2,546,212,864` bytes | rollback/fork 核心数据；相邻 checkpoint 高重复，但删除会降低恢复能力 | 本次零删除；只允许后续 tenant-scoped CAS，在 checkpoint 语义不变后回填 |
| T2 staging jobs | 清理前 allocated=`1,601,486,848` bytes；7,095 个 retry-exhausted job 的 15,803 个可重建临时文件释放后为 `67,907,584` bytes | `job_manifest.json`、status、issues、tenant/session/segment 和 replay identity 是恢复权威；`source_bundle/candidate` 可从 T0/DB 重建 | 7,095 个 manifest 全保留；10 个非 exhausted/running job 连 payload 一起保留 |
| Invocation JSONL spool | rotation 前 allocated=`2,093,240,320` bytes；PostgreSQL 已 ACK 637,844 lines / `2,081,917,321` logical bytes，未 ACK 3,305 lines / `10,634,149` bytes | `InvocationSpan` PostgreSQL 是 canonical query surface；未 ACK bytes 仍是 recoverable spool | 只逐出 PG ACK 段，未 ACK 全量保留；常态 bounded rotation 仍待实现 |
| `.hive/web_fetch` conversion cache | 清理前 10,967 files、allocated=`1,620,996,096` bytes | 唯一 consumer 使用 `force_refresh=True` 可重建，且无 durable ref/index consumer | hash manifest 后清空；未来如被 artifact/T0 引用必须先提升为 durable ref |
| Agent workspace | allocated=`623,652,864` bytes | 用户与 Agent 当前工作区，是 live product state | 本次零删除 |

### 2.3 修复前 Transaction 增长根因

commit `b2fbb530e` 部署前的真实代码路径：

1. `backend/app/main.py` 的 startup lifespan 每次 Volume-bound backend 启动都会执行 `push_default_skills_to_existing_agents()`。
2. `backend/app/services/skill_seeder.py::push_default_skills_to_existing_agents()` 的注释说只补缺失 Skill，但实现会对每个 Agent、每个 default Skill 无条件调用 `install_active_skill_package(..., overwrite=True)`。
3. `backend/app/services/skill_installation.py::install_active_skill_package()` 在没有外部 transaction 时创建 `active_skill_package_install` transaction，并 stage 每个文件。
4. 同一安装会调用 `record_skill_lifecycle_event()`。
5. `backend/app/services/skill_lifecycle.py::record_skill_lifecycle_event()` 读取完整 `evolution/skill_review.md`，追加一行带新时间戳的内容，再 stage 整份文件。
6. `backend/app/services/agent_asset_transaction.py::stage_bytes()` 保存完整目标内容；`_prepare()` 再复制完整旧文件到 `backups/`。
7. `commit()` 将 transaction 标为 committed，但没有 finalization 或 payload retention lifecycle，因此 stage/backups 永久保留。

按当前 Agent/default Skill 组合推算，一次完整 startup seeding 可以新增约 0.8-0.9 GB transaction payload。这个数字受 Agent 数、Skill 数和现有 ledger 大小影响，不应作为永久常量；它说明的是增长机制，而不是固定容量预算。

生产逐小时 journal 证据进一步坐实了重启批次特征：多个小时各新增约 `918` 个 `active_skill_package_install`，对应约 `0.51-0.53 GB`；`2026-07-15T12` 单小时新增 `1,622` 个、约 `0.95 GB`。因此截图中的阶梯不是普通 Memory 自然增长，而是 deployment/restart 触发的重复写放大。

### 2.3.1 启动期写放大止血闭环（已部署）

实现 commit：`b2fbb530e`（`fix(storage): stop default skill startup write amplification`）。

变更边界：

- `AgentAssetTransaction` 对只读/no-change context 延迟创建 journal；没有 staged change 且调用方不 commit 时，不产生 transaction 或 revision。
- `install_active_skill_package()` 在 transaction lock 内逐文件比较；exact match 返回 `status=unchanged`，不 stage、不写 lifecycle、不改 mtime、不升 revision。
- startup seeding 从“每个 Agent × 每个 Skill 各开一次 transaction/recovery scan”收敛为“每个 Agent 一次 lock/recovery scan + 至多一个 batch transaction”。
- 一个 Agent 的默认 Skill 全部 exact match 时，整个 batch 不 commit、不留下 journal。

TDD 与回归证据：

- Red：3 个事故回归分别因 no-op journal、重复安装返回 `installed`、缺少 batch helper 而失败。
- Green：事故回归 `3 passed`。
- 聚焦回归：`21 passed`。
- 完整 backend：`pytest tests -q` -> `7221 passed, 2 skipped in 261.22s`。
- Ruff：涉及实现与测试文件 `All checks passed!`。

Railway production 证据：

- backend deployment：`33b02f96-7b3f-4b7f-95a5-2ed1788ca215` -> `SUCCESS`。
- backend-api deployment：`26e0972a-bc04-41bf-bb77-6544654f4c7e` -> `SUCCESS`。
- frontend deployment：`f2c85d24-73ce-4733-ade1-621392a55335` -> `SUCCESS`。
- `active_skill_package_install`：重启前 `21,163`，重启后仍为 `21,163`；已消除约 918 个一批的 startup 增量。
- Volume：重启前 `28,648,972,288` bytes，重启后 `28,650,721,280` bytes，仅增加 `1,748,992` bytes（约 1.67 MiB），未再出现约 0.5 GB 阶梯。
- health：`status=ok`；新实例 `event_loop.max_lag_ms=33,468.71`，相较修复前实例的 `198,063.26` 明显下降，但仍作为后续 startup/lifecycle 性能债继续跟踪。

闭环边界：本节只证明“继续制造大批重复 transaction”已经停止。历史 transaction 的 lifecycle/backfill/quarantine 进展见 §2.3.2；storage resolver/Object Storage、T2 authority、snapshot CAS、T0 cold archive 和 physical sweep 仍不由本节关闭。

### 2.3.2 Transaction lifecycle、backfill、restore 与 physical sweep（已部署并执行精确事故处置）

实现 commit：`df4a815c5`（`feat(storage): add recoverable volume lifecycle`）。

已落地边界：

- `AgentAssetTransaction` 的 append 只 stage delta；recovery/compensation 用 size、suffix hash 和 append hash 保证 crash-idempotent，不再复制整份历史 ledger。
- transaction 保留兼容 `status=committed`，同时新增 `staging/prepared/applying/committed_recoverable/finalized/compensated` 生命周期、`rollback_deadline`、`payload_gc_at`、pin/projection/retention metadata。普通 file-only transaction 成功退出后自动 finalize；cross-store transaction 必须由真实 projection consumer 显式 finalize。
- Skill Distiller 采用“file commit -> DB asset projection -> finalize”；DB projection 失败时仍走 compensation，不把未完成 saga 当作可 GC。
- 新增 tenant-scoped `storage_blobs`、`storage_blob_refs`、`storage_gc_runs` 与严格 RLS/FORCE RLS migration `storage_blob_lifecycle_0715`。这三张表是后续对象存储权威底座；当前没有据此宣称 S3 provider/resolver 已完成。
- 新增 verified immutable `FilesystemBlobStore` 和 transaction lifecycle CLI；path traversal、hash/size mismatch、无 GC receipt 的 delete 均拒绝。
- backfill 只自动 finalize allowlist 中的 `active_skill_package_install` / `startup_default_registry_skill_batch`；unknown、corrupt、unowned、未 committed 或 evidence mismatch 全部 hold。apply 绑定 immutable manifest SHA，并在每个 Agent lock 内重新验证 journal hash/状态。
- `gc --apply` 只把 `stage/backups` 原子移动到 `.storage_lifecycle/quarantine/<run_id>/...`，保留 journal 和 restore 路径；`sweep --apply` 才是 physical delete，且必须使用 grace 后重新生成的 manifest。

TDD 与本地验收：

- 初始 transaction/storage Red 为 `7 failed`；model/migration/blob Red 为 `6 failed`，分别证明 append full-copy、finalize contract、lifecycle module、tenant/RLS schema 和 blob contract 缺失。
- 聚焦 transaction/storage Green=`8 passed`；model/migration/blob/RLS Green=`11 passed`；扩大聚焦回归=`72 passed in 1.73s`。
- 首次完整 backend 为 `5 failed, 7230 passed, 2 skipped`，暴露 migration head、RLS bypass registration、RLS migration coverage 和 Skill Distiller auto-finalize compensation 漂移；修复后 targeted=`14 passed in 10.72s`。
- 最终完整 backend：`pytest tests -q` -> `7235 passed, 2 skipped in 271.69s`；scoped Ruff=`All checks passed!`；`git diff --check` 通过。

Production migration/deployment：

- backend=`b47ea815-d41f-42d1-b011-6bdf1f006deb`、backend-api=`372ab45d-8c03-47f5-a252-7e08ea773015`、frontend=`cf930cde-b88c-4e6f-bc14-bb78f449d977`，latest 均为 `SUCCESS`。
- backend migration readiness：expected/actual head 均为 `storage_blob_lifecycle_0715`，`checked_table_count=130`、`issues=[]`、`ready=true`。首次 backend-api 在 migration 完成前按 fail-closed readiness 拒绝旧 schema；schema ready 后用同一 `df4a815c5` archive 重提并成功。该时序恢复缺口继续进入 Group 8 bounded schema-wait 验收，不被最终成功掩盖。
- backend health=`status=ok`；runtime role=`app_rls/strict/non-superuser/non-BYPASSRLS`；三 daemon、RuntimeTask worker 和 sandbox probe 健康；frontend=`HTTP/2 200`。

Production inventory/backfill/quarantine 证据：

1. inventory artifact=`/data/agents/.storage_lifecycle/manifests/inventory-2026-07-15T14-24-25.112625+00-00.json`：23,224 transactions，logical=`11,927,841,204` bytes，payload=`11,836,695,357` bytes。
2. backfill manifest=`backfill-a6e367767e8f4bb9b6ea6b887adf1f24`、SHA-256=`a0dba48ef5affb211e6f187fe9331348c167ba5eef31215d69ee8eaec38d439a`：21,163 candidates、`11,422,977,781` bytes、2,061 holds。Railway SSH 输出通道中断后远端幂等 apply 继续；第二份 manifest=`backfill-51b1879ee8d649b6905c3c14d0bfeac0`、SHA-256=`1d0a4c95c4b77a6a024efb343b900ed12e324deb3467ace20501bf833b43605d` 对剩余候选重验，两份 durable backfill receipt 均已落盘。
3. backfill 终态复核 manifest=`backfill-746e86895f074935aecffa1908406cc4`：`candidate_count=0`、`candidate_bytes=0`、`hold_count=2061`。
4. GC dry-run manifest=`gc-9acf3eafae5c413098e9f786140f3d2b`、SHA-256=`9c0adf4f497750effaa14e4b5ffd5f957260e681f2b75a517e8b15c10784ccd4`：11,977 candidates、`6,211,996,397` bytes、2,071 hard holds；另有 9,186 个 finalized payload 仍在 commit-based retention 窗口，不进入 candidate，也不被 CLI 伪装成 hard hold。
5. 首次 quarantine receipt=`gc-9acf3eafae5c413098e9f786140f3d2b.quarantine.json`：processed=`11,977` / `6,211,996,397` bytes、`skipped=[]`。因同卷 quarantine 不释放物理块，先执行 production restore drill：receipt=`gc-9acf3eafae5c413098e9f786140f3d2b.restore.json`，processed=`11,977`、`skipped=[]`。
6. 以原 11,977 个 candidate key 重建 exact-scope emergency manifest=`gc-emergency-gc-912ffd7a4a1148aa94c0225c0a92bb08.json`、SHA-256=`ed827ee0554f14215ad532e06a437bd0665f6e361ba98b345b27203684af805f`；新近跨过 retention 的另 1 个对象（876,181 bytes）明确排除。重隔离 receipt=`gc-912ffd7a4a1148aa94c0225c0a92bb08.quarantine.json`，独立 sweep run=`sweep-e5065cf4fcb94203a7963ab7bc0d40c3`、manifest SHA-256=`b7dd6cfa313ec35578e375bbf35896c8aa07aae888d37054fdbcba9f9f9a7433`，processed=`11,977` / `6,211,996,397` bytes、`skipped=[]`。
7. 第二批只选择 `next_revision < current_revision`、allowlisted operation、finalized/hot、tenant 已归属、无 legal hold/pin 的 superseded revision。dry-run run=`superseded-tx-463a31ab067e4ddba1c0cfd9cd3c1230`、SHA-256=`bad71215de264df92ba13d01856dc3c861827cb2a2f22a5d28a1fe2256836895`：9,118 candidates / `5,176,697,828` bytes、68 holds。逐对象复核隔离 0 skip 后，独立 sweep run=`sweep-734cc757559540d1b3b50e1a243d452a`、SHA-256=`bcfdc738df0ae687a8cb3720eee1dc6bc7abab45c59a13a70870248f3931c10c` 精确释放同一数量/字节，`skipped=[]`。
8. 终态复扫：transaction quarantine=0 bytes；fresh sweep=`candidate_count=0/hold_count=0`；fresh GC=`candidate_count=0/hold_count=2091`。这些 hold、latest/current revision 和未知 operation 均未删除。

闭环裁决：transaction payload 的 Input/Authority/Execution/Evidence/Recovery/Acceptance 已建立，且 production startup/Skill Distiller 是真实 consumer；两批逐对象 physical receipts 与 restore drill 已补齐该子域的事故处置证据，因此可称“transaction lifecycle 子闭环”。它不关闭 `MISS-RETENTION-001`：跨 Memory/Knowledge/Artifact/Audit 的统一 policy、legal hold/export/deletion ledger、Object Storage/resolver、T2 authority/replay、snapshot CAS、T0 cold archive，以及常态化 trace/cache consumer 仍未闭环。

### 2.4 T2 backlog 根因

生产只读扫描发现：

- 7,041 个 job 已达到 `retry_count=3`。
- 6,519 个主要 issue 是 `no summary model config for T0->T2 package build`。
- 这 6,519 个 job 的 `tenant_id` 全部为空。
- 另有 source ref、controlled enum、LLM 输出和少量 HTTP 402 等问题。

当前真实代码路径：

1. `backend/app/runtime/hooks_setup.py::_build_t2_for_sealed_segment()` 只从可选的 `ctx.metadata["tenant_id"]` 获取 tenant。
2. tenant 缺失时仍创建 T2 job。
3. `backend/app/memory/t2/segment_package.py::build_t2_segment_package_with_llm()` 在 tenant 为空时不会调用 `_get_summary_model_config()`。
4. job 被 hold，完整 `source_bundle.json` 留在 staging。
5. sweep 达到 retry 上限后保留 job，但根因仍未修复。

这属于 Authority -> Execution 断点。正确处理是从服务端 Agent 权威记录解析 tenant、回填 job 并重放，而不是删除 held 目录。

### 2.5 Snapshot 重复根因

`backend/app/services/session_workspace_snapshot.py::capture_workspace_snapshot()` 对每个 checkpoint 使用 `shutil.copy2()` 复制当前 workspace 的所有受支持文件。即使相邻 checkpoint 的内容完全相同，也会生成新的物理副本。

现有 `prune_session_workspace_snapshots()` 已限制每个 session 的 checkpoint 数量，但它只能按 snapshot 目录删除，不能跨 snapshot 复用相同内容。因此保留数量有界不等于物理内容去重。

### 2.6 T0 不是异常垃圾

`backend/app/memory/t0/ledger.py::append_t0_session_event()` 同时维护：

- `events.jsonl`：portable raw evidence truth；
- `source.md`：确定性、可读投影；
- `index.json`：segment ordering、hash chain 和 locator metadata。

`ChatTranscriptEvent` 仍是 cloud run ordering/replay/fork/checkpoint 的 transactional authority；T0 是 exactly-once portable Memory evidence projection。冷热分层不能把两者重新混成双运行权威，也不能因 `source.md` 与 `events.jsonl` 内容相关就任意删除其中一个。

### 2.7 事故期派生数据收敛与停止边界

在 transaction physical sweep 之外，本次只处理了三类具备机械重建/ACK 事实的数据：

1. **Web conversion cache**：manifest=`web-fetch-cache-76dd7f64e078404da5c7a90b951939e8.json`、SHA-256=`5c17bd38ab58a587b70ce6096be46b1a49500f6681424cea2633dd1fce72d77e`，10,967 files、logical=`1,593,226,204`、allocated=`1,620,996,096` bytes、open fd=0。隔离后重新创建空 active root，再按同一 exact file set sweep；quarantine/sweep receipts 均 `skipped=[]`。
2. **Invocation compatibility spool**：dry-run=`invocation-spool-ack-dry-run-20260715.json`、SHA-256=`e37164e5f5a984bc33db1daafaaaebced2dd23b96473288562bc5eed02cdb18d`。67 个 active JSONL 原子轮转；按 `(agent_id, trace_id, span_id)` 与 canonical PostgreSQL `InvocationSpan` 对账，637,844 lines / `2,081,917,321` bytes 已 ACK，3,305 lines / `10,634,149` bytes 未 ACK，invalid=0。只清 ACK 副本，未 ACK bytes 写入 per-agent `unacked_spool`；receipt=`invocation-spool-ack-20260715.sweep.json`、`skipped=[]`。
3. **Retry-exhausted T2 可重建 staging payload**：manifest=`t2-exhausted-staging-f96a016b0ee4479094d738f7877bdd27.json`、SHA-256=`dd265d42a4a6f0cc4f1edd4f3445d239d24d3788fe21c09c5460d530485dda6b`。只选择 `status=held`、`retry_count>=3`、已有 `retry_exhausted_alerted_at`，且目录内文件全部属于 `source_bundle.json` / `*.candidate.md` / `platform_gate_report.json` allowlist 的 7,095 个 job。15,803 个文件、logical=`1,487,234,748`、allocated=`1,533,579,264` bytes 经全量 hash 复核后隔离并 sweep；每个 `job_manifest.json`、issues、tenant/session/segment、retry state 与重放 identity 原位保留，10 个不满足条件的 job 连 payload 一起保留。quarantine/sweep receipts 均 `skipped=[]`。

事故处置在 `df used=11,316,330,496` bytes 时按 owner 决策停止。随后启动的 workspace snapshot 重复率只读扫描已被终止，没有执行去重、hardlink、移动或删除。剩余大项的明确裁决是：

- T0 allocated=`6,365,040,640` bytes：核心证据，禁止直接删除；
- workspace snapshots allocated=`2,546,212,864` bytes：rollback/fork 核心，禁止通过减少 checkpoint 清理；
- workspace allocated=`623,652,864` bytes：当前用户/Agent 数据，禁止清理；
- transaction tree allocated=`777,310,208` bytes：journal、latest/current revision、hold 与恢复元数据，fresh GC 无 candidate；
- T2 staging allocated=`67,907,584` bytes：保留的 manifests 与 10 个非 eligible job，不再清理。

这条停止边界高于“继续降低数字”的优化目标：后续只能通过完整 Group 8 的 sealed archive、tenant-scoped CAS、bounded spool/cache 和 authority/replay 合同优化，不能复用本次一次性脚本继续删除核心数据。

## 3. 不采用的方案

### 3.1 不拆成两个 PostgreSQL

原因：

- 大头是文件 blob，不是适合关系数据库存储的行数据。
- 两个数据库引入分布式事务、跨库 RLS、备份一致性和恢复顺序问题。
- 不能解决无变化 Skill 重写、full-file transaction 和 snapshot 重复复制。
- PostgreSQL 应负责 blob 元数据和引用权威，不应成为大型原始文件仓库。

### 3.2 不给同一个 backend 挂第二块 Railway Volume

Railway 当前限制每个 Service 只能挂一块 Volume。另建一个 Service 再挂 Volume，会把问题变成自建网络文件服务：

- 需要自行处理 RPC、锁、权限、吞吐、恢复和故障域；
- 仍然没有对象级引用、dedup、retention 和 lifecycle；
- 形成新的单点和运维负担。

因此它不是本问题的合理边界。

参考：<https://docs.railway.com/volumes/reference>

### 3.3 不把 `/data/agents` 整体搬到 Object Storage

Agent 当前 workspace、原子 rename、append、文件锁和 transaction recovery 需要 POSIX 语义。S3-compatible storage 不是共享文件系统，不能直接替换所有 `Path` 操作。

必须按数据生命周期拆分：

- 可变、活跃、需要 POSIX 语义的数据留在热层；
- 已封存、不可变、可按内容寻址的大对象进入冷层；
- 可重建缓存进入有 TTL 的 rebuildable 层。

### 3.4 不使用裸 TTL 或 `rm -rf`

目录年龄不能证明：

- transaction 已完成所有跨存储 projection；
- snapshot 没有 branch/pin 引用；
- T2 job 已成功 commit；
- T0 已归档且可读取；
- legal hold 已解除。

任何直接按路径、mtime 或容量阈值删除的方案都不符合 Authority、Evidence 和 Recovery 要求。

## 4. 数据分级与推荐保留策略

下表是推荐默认值，不是写死的唯一产品策略。tenant retention policy、legal hold 和合规要求可以延长保留时间，但不能绕过权威验证缩短 canonical evidence 的保留。

| 数据类别 | Retention class | 热层 | 冷层 | GC 条件 | 推荐默认值 |
|---|---|---|---|---|---|
| `soul.md`、当前 T3/profile/knowledge、当前 Skills、当前 workspace | `canonical_hot` | 当前版本常驻 | 由备份保护 | 不进入普通 GC | 无 TTL |
| T0 open segment | `canonical_hot` | 常驻 | 不归档未封存 segment | seal 前不可归档/删除 | 直到 seal |
| T0 sealed segment | `canonical_archive` | 短期热留 | durable bucket | 归档校验通过、retention 到期、无 legal hold | 热留 30 天；冷留 365 天或 tenant policy |
| Agent transaction stage/backups | `rollback_payload` | rollback window 内保留 | 通常不需要 | finalized、无 pin/lease、grace 到期 | finalized 后 24 小时 |
| Agent transaction journal/receipt | `audit_receipt` | 小型记录 | 可归档 | 审计策略到期 | 90 天或 enterprise policy |
| T2 held/failed job manifest、issues、candidate | `recoverable_work` | 保留 | 可冷存 | 不能因 retry exhausted 自动删除 | 直到 commit/reject/operator disposition |
| T2 reconstructable `source_bundle` | `rebuildable` | 仅工作期间 | rebuildable bucket 或按 T0 refs 重建 | T0 refs 完整、resolver 可重建、job 非 running | commit 后 24 小时；held 时可逐出但必须可重建 |
| Snapshot manifest/ref | `recovery_metadata` | 常驻有效 checkpoint | 可备份 | checkpoint 被合法 prune 且无 branch/pin | 保持当前 last 50 + branch/pin |
| Snapshot content blob | `canonical_archive` | 可选本地 cache | durable bucket | ref count=0、无 pin/lease、grace 到期 | 由引用决定，无独立 TTL |
| Invocation span DB row | `audit_trace` | PostgreSQL | 可选压缩归档 | tenant audit policy 到期 | DB 热留 90 天；冷留 365 天 |
| Invocation local spool | `delivery_spool` | DB ACK 前 | 可选 | DB ACK + receipt 后 | ACK 后 24 小时内清除 |
| 未引用 web conversion | `cache` | 有界 cache | rebuildable bucket | 无 durable ref | 24 小时 |
| 未引用 raw web/source cache | `cache` | 有界 cache | rebuildable bucket | 无 citation/artifact/T0 ref | 7 天 |
| 已被 citation/artifact/T0 引用的源文件 | `canonical_archive` | 按需 cache | durable bucket | 跟随 owner evidence retention | 跟随引用对象 |

`legal_hold=true`、active lease、branch pin、rollback pin 和 operator quarantine 均拥有高于普通 TTL 的优先级。

## 5. 权威数据模型

### 5.1 PostgreSQL 仍是控制权威

建议新增三类 tenant/RLS-scoped 表。

#### `storage_blobs`

建议字段：

```text
id
scope_type                 # tenant | system
tenant_id                  # tenant scope 必填；system scope 必须显式
kind                       # t0_events | t0_source | snapshot_file | source_document | ...
retention_class
state                      # uploading | available | quarantined | deleting | deleted | failed
provider
bucket
object_key
content_sha256
size_bytes
media_type
encryption_key_id
created_at
verified_at
delete_after
quarantined_at
deleted_at
last_error
```

关键约束：

- tenant blob 的 `tenant_id` 不得为空。
- tenant dedup 唯一键至少包含 `tenant_id + content_sha256`。
- system scope 必须使用显式 system authority，不得用 `tenant_id=NULL` 暗示全局公开。
- `available` 前必须完成应用侧 SHA-256 和 size 校验。
- object key 不得覆盖；blob 内容一旦 available 即 immutable。

#### `storage_blob_refs`

建议字段：

```text
id
tenant_id
blob_id
owner_type                 # t0_segment | snapshot | artifact | citation | t2_job | ...
owner_id
purpose
created_at
pinned_until
legal_hold
```

ref 是 blob 是否可删的权威，不允许通过扫描文件名猜 owner。

#### `storage_gc_runs`

建议字段：

```text
id
tenant_id                  # fleet run 可使用显式 operator/system scope
policy_version
mode                       # inventory | dry_run | quarantine | sweep | restore
manifest_sha256
candidate_count
candidate_bytes
processed_count
processed_bytes
failed_count
started_at
finished_at
receipt_json
```

### 5.2 BlobStore provider boundary

仓库当前没有通用 S3/BlobStore 抽象。建议新增窄接口，而不是让各业务模块直接使用 provider SDK：

```python
class BlobStore(Protocol):
    async def put_verified(self, request: BlobPutRequest) -> BlobReceipt: ...
    async def stat(self, location: BlobLocation) -> BlobStat: ...
    async def open_stream(self, location: BlobLocation) -> AsyncIterator[bytes]: ...
    async def delete(self, location: BlobLocation, *, gc_receipt_id: UUID) -> None: ...
```

实现：

- `FilesystemBlobStore`：本地开发、测试和 fault injection。
- `S3BlobStore`：生产 Object Storage。

使用成熟 S3 SDK，不自行实现 AWS signing、multipart、重试或 presigned URL 协议。Provider 细节只能存在于 adapter 内。

### 5.3 两个对象空间

#### `durable`

存放：

- sealed T0 `events.jsonl`、`source.md` 及 segment manifest；
- snapshot content blobs；
- 被 artifact、citation、T0 或合法业务对象引用的源文件；
- 必须长期保留的 audit export。

权限建议：

- 普通 backend principal：Put/Get/Head/List scoped prefix，无 Delete。
- 独立 retention/GC principal：仅在拥有 DB GC receipt 时执行 Delete。
- 生产运维 principal：break-glass，完整 audit。

#### `rebuildable`

存放：

- 可由 T0 refs 重建的 T2 source bundle；
- 未被 durable ref 提升的 web/raw conversion cache；
- 已有 DB ACK 的可选 trace archive；
- 其他明确可重建 payload。

它可以使用更短 TTL 和更宽松的删除权限，但仍必须记录 GC receipt。

如果 provider 或运维约束只能使用一个 bucket，至少要使用两个独立 prefix、两套 IAM policy 和严格的 retention class；从 blast-radius 隔离看，两个 bucket 更安全。

### 5.4 加密与 tenant 隔离

canonical Agent/Memory 内容可能包含企业敏感信息。

要求：

- 优先选择支持 SSE/KMS、versioning、Object Lock 和 lifecycle 的 S3-compatible provider。
- 如果使用缺少这些能力的 provider，必须实现 per-tenant envelope encryption 和独立备份复制。
- content hash 可以用于 tenant 内 dedup，但 object key 建议使用 tenant-scoped HMAC 或不可枚举随机 key，避免直接暴露 plaintext digest。
- 相同内容跨 tenant 也必须形成不同 object/crypto boundary，禁止跨租户物理 dedup。
- Provider credential 只在 storage adapter/GC worker 中可见，不进入 Agent runtime、prompt 或 tool result。

Railway Bucket 是私有 S3-compatible storage，但当前不提供 server-side encryption、object versioning、object lock 和 bucket lifecycle policy：<https://docs.railway.com/storage-buckets>。

因此推荐：

- rebuildable 数据可以优先使用 Railway Bucket；
- durable canonical 数据优先使用具备原生企业保留能力的 provider；
- 如果统一使用 Railway Bucket，应用层 encryption、retention、backup replication 必须在同一完整施工轮落地。

## 6. 统一读写与恢复协议

### 6.1 写入协议

每次 blob 写入遵循：

1. 调用方提交 typed `BlobPutRequest`：tenant、kind、retention class、owner、预期 size/hash。
2. 服务端从 authenticated authority 或 Agent/owner DB 关系解析 tenant；不信任客户端回显 tenant。
3. 在 PostgreSQL 对 `tenant + digest` 获取 advisory lock 或唯一键仲裁。
4. 创建 `storage_blobs(state=uploading)`。
5. 流式上传，不在内存拼接完整大文件。
6. 计算应用侧 SHA-256，执行 provider HEAD/size 校验。
7. 事务内写 blob ref 并转为 `available`。
8. 返回包含 blob id、hash、size、owner ref 的 receipt。

如果上传成功但 DB commit 失败，对象保持不可消费的 orphan/uploading 状态；orphan sweeper 在 grace 后校验并回收。不能让 reader 通过裸 object key 绕过 DB state。

### 6.2 读取协议

所有可冷热分层的数据必须经 `StorageResolver`：

1. 验证 principal、tenant、owner/ref authority。
2. 读取逻辑 manifest。
3. 如果 hot copy 存在且 hash 正确，直接读取。
4. 如果在 cold layer，stream 或 hydrate 到受控 cache。
5. 校验 content hash。
6. 返回 typed result：`available`、`denied`、`unavailable`、`corrupt`、`retryable_error`。

`unavailable` 不能伪装成空文件，`denied` 不能伪装成不存在。

### 6.3 逻辑路径与物理位置

对 Memory 等现有 path contract：

- 逻辑路径、segment id、source refs、hash 和 ordering 不变。
- 物理位置可以是 `hot_local`、`mirrored` 或 `cold_object`。
- `index.json`/DB manifest 保存 locator，不把 S3 key直接暴露给 Agent。
- portable export 必须能够重建原始目录结构和逐文件 byte/hash。
- 在所有消费者切换到 resolver 之前，不允许删除原始本地文件。

## 7. 停止继续增长的根修

### 7.1 Default Skill seeding 幂等

定义 canonical package digest：

```text
package_digest = sha256(
  canonical_json([
    {path, sha256(content)}
    ... sorted by path
  ])
)
```

在 `AgentAssetTransaction` lock 内执行比较，避免 compare-before-lock 的 TOCTOU：

- exact match：返回 `status=unchanged`；不创建 transaction、不写 lifecycle、不改 mtime、不增加 revision。
- registry update：只 stage 真实变化的 registry-managed 文件。
- registry 中删除的文件：只允许删除旧 manifest 明确标记为 registry-managed 的路径。
- agent-owned divergence：不得覆盖；产生 typed conflict/review receipt。
- lifecycle event：只在 old digest -> new digest 的真实 transition 时写入。
- idempotency key：绑定 `agent_id + folder_name + package_digest`。

### 7.2 AgentAssetTransaction 生命周期

当前 `committed` 同时承担“文件 commit 完成”和“所有补偿需求结束”两个不同语义。必须拆开：

```text
staging
  -> prepared
  -> applying
  -> committed_recoverable
  -> finalized

failure branches:
  staging -> aborted
  prepared/applying -> rolled_back
  committed_recoverable -> compensated
```

新增机械字段：

```text
retention_class
committed_at
finalized_at
rollback_deadline
pinned_until
projection_ref
payload_gc_at
```

规则：

- crash-only transaction 在 context 成功退出后可以自动 finalize。
- 涉及 DB projection/saga 的 caller 必须在外部 commit 后显式 `finalize(receipt, projection_ref)`。
- projection 失败时仍可在 rollback deadline 前 `compensate()`。
- recovery 只依赖 staging/prepared/applying payload；finalized 后大 payload 可进入 GC。
- 小 journal/receipt 按 audit policy 保留。
- legacy operation 未分类、journal 损坏或 projection 不明时一律 quarantine/manual review，不自动删。

### 7.3 Append-only transaction 使用 delta

`append_text()` 不再读取并 stage 完整文件。新增 append operation：

```text
action=append
before_size
before_suffix_sha256
append_size
append_sha256
stage_file=<only appended bytes>
```

应用：

1. 检查当前 size 和 suffix hash。
2. 使用 append/O_APPEND 写入 delta。
3. fsync 文件和父目录。
4. 记录 applied boundary。

恢复：

- size 等于 `before_size`：安全重放 append。
- size 等于 `before_size + append_size` 且尾部 hash 匹配：视为已应用。
- 其他情况：typed corruption，停止并人工处理。

补偿只在当前尾部仍匹配 append hash 时截断到 `before_size`，否则禁止破坏后来合法写入。

适用对象包括 `skill_review.md`、`evolution_ledger.jsonl` 和其他 append-only audit ledger。

### 7.4 T2 tenant authority 和 staging 收敛

`tenant_id` 必须来自服务端权威：

1. Hook/runtime task 传递 server-issued tenant identity。
2. T2 execution boundary 根据 `agent_id` 查询 `Agent.tenant_id` 并比对。
3. 缺失或冲突时 hold typed job，不调用模型、不写错误 tenant 数据。
4. job manifest 保存 authoritative tenant resolution receipt。

现有 job 回填：

- 根据 manifest 的 `agent_id` 从 DB 回填 tenant。
- 验证 T0 segment、source refs、hash 和 tenant ownership。
- retry exhausted 因 tenant 缺失的 job 恢复为 retryable。
- 按 tenant/model/budget 限速重放，避免一次性模型费用和 provider overload。
- HTTP 402、invalid output、source refs 缺失等其他问题继续维持独立 typed reason。

新 staging 机制不永久保存完整 source bundle：

- manifest 保存 T0 source refs、coverage ledger、source hashes。
- 每次模型执行前通过 resolver 重建完整授权 source bundle。
- LLM 仍看到完整授权证据；不得用机械截断替代。
- 保留 candidate summary/labels/review、issues 和 retry history。
- commit 且 package/hash/source refs 校验后，bulk staging payload 进入短期 GC。

### 7.5 Snapshot tenant-scoped CAS

Snapshot manifest 从：

```text
path + copied file
```

改成：

```text
path + blob_id + size + sha256 + mode/metadata
```

捕获过程：

1. 在 workspace lock 内枚举文件和计算 hash。
2. 对同 tenant 已存在 digest 只增加 ref。
3. 不存在时写 immutable blob。
4. manifest 原子 commit 后才发布 checkpoint。
5. branch clone 复制 manifest/ref，不复制 blob。

Restore/fork：

- 先获取 snapshot lease。
- 逐 blob 校验并写入 staging workspace。
- 全部完成后原子切换。
- incomplete snapshot 继续保持 `complete=false`，不能伪装完整。

保持现有 last 50 checkpoint 语义；优化物理内容，而不是为了省空间削弱恢复能力。

### 7.6 Invocation trace 本地文件变成有界 spool

`InvocationSpan` PostgreSQL 表是 canonical trace consumption surface。当前 `invocation_spans.jsonl` 应收敛为 delivery spool：

- DB 写成功并有 ACK/receipt 后，对应 spool segment 可 GC。
- spool 按 segment 轮转，不能单文件无界 append。
- DB timeout/error 时保留 segment、记录 oldest age 和 backlog bytes。
- 长时间不可投递时告警，不静默丢 evidence。
- 缺 tenant 时 fail closed，不能写成全局 DB span；同时要保留可诊断的 bounded failure receipt。

2026-07-15 事故处置已完成一次 canonical ACK reconciliation，详细 receipt 见 §2.7。这证明“已 ACK 副本可安全逐出、未 ACK bytes 必须保留”，但 active writer 仍会继续 append 当前 JSONL；没有轮转阈值、backpressure、oldest-age metric 和定时 sweeper 之前，本节仍是待 Group 8 常态化实现，不能用一次性清理冒充关闭。

### 7.7 Web/raw conversion cache

转换产物按引用提升：

- 初始属于 rebuildable cache。
- 被 artifact、citation、T0 或 durable source object 引用时，创建 durable ref 并切换 retention class。
- `force_refresh=True` 只能生成新 cache candidate，不能绕过 dedup/ref lifecycle。
- 无引用的 raw/conversion 按 TTL 回收。

2026-07-15 已对当时无 durable ref/index consumer、且 consumer 使用 `force_refresh=True` 重建的 `.hive/web_fetch` cache 做 exact manifest 清理，详细 receipt 见 §2.7。active root 已原位重建，生产 reader 路径未改变。未来增长仍需本节的 ref promotion、TTL、容量 metric 与定时 GC；若产物进入 artifact/citation/T0，必须先 durable promotion，不能沿用此次“全部可重建”的历史判断。

## 8. 可恢复 GC 机制

### 8.1 GC 只能使用 hard facts

允许作为删除依据：

- exact lifecycle state；
- committed/finalized timestamp；
- DB ref count；
- pin、lease、legal hold；
- hash/size verification；
- tenant retention policy；
- explicit operator disposition；
- verified projection/archive receipt。

禁止作为 hard outcome：

- 文件正文关键词、自然语言重要性判断；
- 目录名看起来像 backup/cache；
- 单纯 mtime 或磁盘压力；
- retry 次数本身；
- “当前 UI 没有展示”；
- 未验证的孤立路径扫描结果。

### 8.2 Inventory -> Mark -> Quarantine -> Sweep

#### Inventory

- 枚举每类数据的 count、logical bytes、disk bytes、oldest/newest。
- 关联 tenant、owner、refs、pins、leases、state。
- 输出不可变 inventory manifest 和 SHA-256。
- 未知、corrupt、unowned 数据进入 separate hold list。

#### Mark / dry-run

- 使用 versioned policy 计算 candidate。
- 每个 candidate 写明 class、reason、current refs、grace、预计回收 bytes。
- dry-run 不移动、不删除、不改变消费路径。
- 输出 manifest hash，后续 apply 必须绑定相同 hash。

#### Quarantine

本地 payload：

- 在 agent/storage lock 下原子移动到 `.gc_quarantine/<gc_run_id>/...`。
- 原位置保留 tombstone/locator。
- 保留 journal 和 receipt。

Object Storage：

- 不为 quarantine 复制一份大对象。
- DB state 改成 `quarantined`，普通 reader 不再建立新 ref。
- 已有合法 restore 可以在 grace 内撤销 quarantine。

#### Sweep

- 获取 tenant/agent/advisory lock。
- 重新检查 state、ref、pin、lease、legal hold、hash 和 manifest hash。
- 条件变化则 skip，并记录 reason。
- 删除幂等；not-found 也要和 receipt 状态对账。
- fsync 本地父目录，写 provider delete receipt。
- `storage_blobs.state=deleted` 在确认 physical result 后提交。

### 8.3 高水位保护

建议告警阈值：

- 60%：warning，计算 growth rate 和 days-to-full。
- 75%：operator action required，强制输出 dry-run inventory。
- 85%：critical，暂停新的可重建 cache 写入或缩短 cache TTL。

即使达到高水位，也禁止：

- 自动删除 canonical T0；
- 删除 held T2 work；
- 删除未 finalized transaction；
- 删除有 ref/pin/lease/legal hold 的 blob；
- 通过截断 LLM evidence 或关闭 Memory 能力来降低磁盘。

## 9. Railway 与备份边界

### 9.1 Volume backup

Railway Volume backup 支持 daily、weekly、monthly schedule；其作用是基础设施灾难恢复，不是应用 retention 或 GC。

参考：<https://docs.railway.com/volumes/backups>

建议在热层收敛后：

- 启用 daily + weekly；
- 是否启用 monthly 根据 enterprise retention 决定；
- 定期做 restore drill，而不是只看 backup 状态。

### 9.2 Object Storage backup

Railway Bucket 当前没有 versioning、Object Lock 和 lifecycle，不能仅凭“对象在 bucket 里”就视为已有可恢复备份。

durable 数据至少满足其一：

1. provider 原生 versioning + Object Lock + lifecycle + SSE/KMS；或
2. 应用层 envelope encryption + 跨故障域复制 + 定期 restore/hash verification。

### 9.3 成本不是首要目标

Railway 当前公开计费中，Volume 约为 `$0.15/GB-month`，Bucket 约为 `$0.015/GB-month`，但服务上传到 Bucket 可能产生 service egress。

参考：

- <https://docs.railway.com/pricing>
- <https://docs.railway.com/storage-buckets/billing>

冷热分层的主要收益不是每月节省几美元，而是：

- 消除 50 GB 单 Volume 的容量倒计时；
- 获得对象级 dedup、引用和 retention；
- 将 hot POSIX 故障域限制在真正需要 POSIX 的数据；
- 让清理变成可证明、可恢复的系统能力。

## 10. 单轮完整迁移施工

### 10.1 施工前 inventory

在任何写入或清理前生成：

- Railway Volume 使用量；
- 每类目录 count/logical bytes/disk bytes；
- transaction operation/status/source/evidence refs；
- T0 open/sealed segment 和 hashes；
- T2 job status/tenant/issues/retry/source refs；
- snapshot manifests、refs、unique hashes；
- trace spool segments 和 DB ACK 状态；
- raw/conversion durable refs。

Inventory 结果保存为审计 artifact，不能只打印终端摘要。

### 10.2 P0 止血与程序级完整交付的边界

§2.3.1 的 P0 修复是一个可独立验收的安全闭环：它只停止已坐实的重复写入，不删除旧数据、不引入临时 cleaner，也不声称 lifecycle 已完成。P0 不得被 Object Storage、snapshot CAS 等后续大施工绑架；同样，P0 已部署也不得被用来宣称整个存储程序完成。

剩余存储程序的一次完整交付包含：

- Skill digest/idempotency；
- transaction finalization 和 append delta；
- T2 authoritative tenant resolution；
- StorageBlob/Ref/GC schema 与 RLS；
- BlobStore/Resolver；
- 新 snapshot CAS；
- trace spool ACK；
- metrics/alerts；
- backfill/GC dry-run CLI。

不能先部署一个只会删旧文件、但继续制造新文件的 cleaner。

### 10.3 迁移状态

每个可迁对象经历：

```text
local_only
  -> uploading
  -> mirrored_verified
  -> object_preferred
  -> local_quarantined
  -> cold_only
```

任何失败都停留在可恢复状态；不得通过删除 local source“推动状态前进”。

### 10.4 Backfill 顺序

1. 回填 Agent/owner -> tenant authority。
2. 注册现有 canonical hot objects 和 active refs。
3. 分类 legacy transactions：crash-only、cross-store saga、user rollback、unknown。
4. 对 startup default Skill transaction 使用 source/evidence ref 和当前 revision/hash 做安全 finalization 候选，unknown 保留。
5. Snapshot 计算 tenant-scoped hashes，上传 unique blobs，生成新 manifests；旧 manifest 保持可读。
6. sealed T0 上传 `events.jsonl`、`source.md` 和 manifest，验证逐文件 hash、event count、ordering、hash chain。
7. 修复并回填 T2 tenant/source refs；重放 exhausted jobs。
8. 将 trace fallback 改为 spool 并对账 DB ACK。
9. 注册被引用 web/source content，剩余内容进入 cache policy。

### 10.5 读取切换

每类数据只有在以下条件全部满足后才能进入 `object_preferred`：

- 所有生产消费者已经使用 resolver；
- local 与 object byte/hash/count parity 通过；
- denial/unavailable/corrupt typed behavior 通过；
- restore/fork/replay/fault injection 通过；
- metrics 能区分 local hit、cold hit、hydrate failure；
- rollback 能切回 local source。

### 10.6 删除确认门

实施完成后先运行：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

python -m app.scripts.storage_lifecycle inventory --json
python -m app.scripts.storage_lifecycle backfill --dry-run --json
python -m app.scripts.storage_lifecycle gc --dry-run --json
```

transaction payload 子路径的上述 CLI 已由 `df4a815c5` 实现并在 production 执行；T2、snapshot、T0、trace/cache 与 Object Storage 的 inventory/backfill 尚未接入该 CLI，不能因 transaction 子路径可用就宣称全资产 inventory 完成。

apply contract 分成两个不同风险层：

- `backfill --apply`：只写 lifecycle/authority metadata，不移动或删除 payload；仍必须绑定已审阅 manifest SHA 并逐 journal recheck。
- `gc --apply`：只进入可恢复 quarantine，不做 physical delete；必须绑定 manifest SHA、重新检查 finalized/retention/pin/legal-hold/journal hash，并产生 receipt。
- `sweep --apply`：在 grace 后 physical delete quarantine payload，属于不可逆生产操作；必须使用 grace 后重新生成和审阅的 sweep manifest，不能复用 GC manifest。

所有 apply 必须：

- 标记为 `HIGH-RISK OPERATION`；
- 绑定已审阅的 dry-run `manifest_sha256`；
- 经用户明确确认；
- quarantine 与 physical sweep 分离；
- grace window 后再次确认或按已批准 policy sweep；
- unknown/corrupt/unowned/changed/held 对象 fail closed，不以容量压力跳过权威校验。

## 11. TDD、已验收范围与剩余故障注入

### 11.1 Red tests

以下清单同时作为完整方案的 test ledger。transaction/blob/schema/CLI 子集已按 §2.3.2 Red→Green；T2/snapshot/T0/trace/Object Storage 子集仍是待施工 Red，不得把未创建或未执行的测试算作通过。

#### Skill/transaction

- startup default Skill exact match 不创建 transaction。
- exact match 不写 lifecycle event、不改 revision、不改 mtime。
- registry package 真实变化只写 changed files。
- agent-owned divergence 不被覆盖。
- append delta recovery 在 before/after 两种 crash point 幂等。
- append suffix 已变化时 compensation fail closed。
- unfinalized/pinned transaction 永远不是 GC candidate。
- finalized transaction 在 grace 到期后仅删除 payload，journal/receipt 保留。

#### T2

- hook metadata 缺 tenant 时从 Agent DB authority 正确解析。
- metadata 与 Agent tenant 冲突时 hold 并产生 audit evidence。
- held job 可以从完整 T0 refs 重建 source bundle。
- source ref 缺失时 typed held，不机械生成摘要。
- retry exhausted 的 tenant-loss job 回填后重新变为 retryable。
- provider 402、invalid output 和 denied 保持不同状态。

#### Snapshot/blob

- 同 tenant 相同内容只创建一个 blob、多个 refs。
- 不同 tenant 相同内容形成不同 blob/crypto boundary。
- branch clone 只复制 refs。
- pinned checkpoint 的零普通 ref blob仍不可 GC。
- cold snapshot restore 与原文件 byte/hash/mode 一致。
- incomplete snapshot 不被报告为 complete。

#### T0/resolver

- sealed T0 object replay 的 event count、order、event hash、source projection hash 完全一致。
- Object Storage unavailable 返回 typed unavailable，不返回空 evidence。
- denied 与 unavailable 不混淆。
- portable export 重建 canonical directory contract。
- cold hydrate 中断可重试且不发布半文件。

#### GC

- dry-run 不改变任何文件、DB state 或 ref。
- candidate 在 quarantine 前新增 ref 时必须 skip。
- quarantine restore 幂等。
- manifest hash 不匹配时 apply 拒绝。
- unknown/corrupt/unowned object 进入 hold，不删除。
- concurrent GC 只有一个 worker 获得删除权。
- provider delete 成功、DB commit 失败时可对账恢复。

### 11.2 验证命令

实现后执行聚焦回归：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest \
  tests/services/test_skill_installation.py \
  tests/services/test_agent_asset_transaction.py \
  tests/services/test_agent_asset_transaction_retention.py \
  tests/services/test_storage_lifecycle.py \
  tests/services/test_blob_store.py \
  tests/services/test_session_workspace_snapshot.py \
  tests/memory/test_t2_storage_lifecycle.py \
  tests/memory/test_t0_cold_storage.py \
  tests/services/test_invocation_trace_spool.py \
  -q
```

然后执行完整 backend suite：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q
```

当前已执行证据只覆盖实际存在的 transaction/blob/schema/CLI 与邻接测试，最终完整 backend 为 `7235 passed, 2 skipped in 271.69s`。上述尚不存在的 `test_t2_storage_lifecycle.py`、`test_t0_cold_storage.py`、`test_invocation_trace_spool.py` 等命令仍是后续验收合同；不能在测试尚未创建或未执行时声称通过。

## 12. 可观测性与运营指标

建议新增：

```text
storage_bytes{tenant,class,state,location}
storage_objects{tenant,class,state,location}
storage_daily_growth_bytes{class}
storage_days_to_volume_full

asset_transaction_count{operation,status,retention_class}
asset_transaction_payload_bytes{operation,status}
asset_transaction_unfinalized_oldest_seconds
skill_seed_result_count{status=unchanged|installed|updated|conflict}

t2_job_count{status,reason,retry_state}
t2_job_source_bundle_bytes{status}
t2_job_missing_tenant_count

snapshot_referenced_bytes
snapshot_unique_blob_bytes
snapshot_dedup_ratio

trace_spool_bytes
trace_spool_oldest_seconds
trace_spool_replay_failures_total

storage_gc_candidate_bytes{class}
storage_gc_quarantined_bytes{class}
storage_gc_reclaimed_bytes{class}
storage_gc_failures_total{reason}
storage_quarantine_restores_total

cold_storage_reads_total{result}
cold_storage_hydration_seconds
cold_storage_checksum_failures_total
blob_orphan_uploads_total
```

每次 GC/backfill 需要 structured log 与 durable receipt；只打印“job started”不构成生产完成证据。

## 13. 七原子闭环

| 原子 | 本设计要求 | 不闭环的典型表现 |
|---|---|---|
| 输入 Input | 所有 blob/write/retention 操作使用 typed request，带 owner、tenant、kind、retention、预期 hash/size | 模块直接写任意路径，GC 不知道来源 |
| 权威 Authority | tenant 从 authenticated principal 或 Agent/owner DB 关系解析；PostgreSQL ref/pin/lease/legal hold 为删除权威 | 信任 Hook/client metadata；按目录名猜 tenant/重要性 |
| 执行 Execution | BlobStore、StorageResolver、StorageLifecycle/GC 是唯一物理 I/O 边界 | 业务模块继续直接 `Path`/SDK 读写冷数据 |
| 证据 Evidence | SHA-256、manifest、state transition、archive/GC receipt、metrics 和 audit event | 只有终端输出或容量下降，无对象级证明 |
| 恢复 Recovery | uploading/mirrored/quarantined 状态、idempotency、lease、grace、restore 和 fault injection | 上传或删除中断后无法判断真实状态 |
| 消费 Consumption | T0 replay、T2 build、snapshot restore/fork、artifact/citation、trace UI 都真实通过 resolver 消费 | 数据已迁移但生产消费者仍只读本地路径 |
| 验收 Acceptance | red/green tests、migration/backfill、byte/hash/count parity、restore drill、production metrics、dry-run/apply 对账 | 只有 schema、bucket 或脚本，没有端到端证明 |

只有七个原子都有当前真实消费路径，才能称为闭环。

## 14. 精确施工文件图

### 14.1 新增文件状态

```text
# 已新增并进入 df4a815c5
backend/app/models/storage_blob.py
backend/app/services/blob_store.py
backend/app/services/storage_lifecycle.py
backend/app/scripts/storage_lifecycle.py
backend/alembic/versions/storage_blob_lifecycle_0715.py

backend/tests/services/test_blob_store.py
backend/tests/services/test_storage_lifecycle.py
backend/tests/services/test_agent_asset_transaction_retention.py
backend/tests/models/test_storage_blob.py
backend/tests/migrations/test_storage_blob_lifecycle_migration.py

# 完整冷热分层仍待新增
backend/app/services/storage_resolver.py
backend/tests/services/test_invocation_trace_spool.py
backend/tests/memory/test_t2_storage_lifecycle.py
backend/tests/memory/test_t0_cold_storage.py
```

### 14.2 修改文件状态

本轮已修改 `agent_asset_transaction.py` 和 `skill_distiller.py`，并保留 §2.3.1 已完成的 Skill startup 止血。下表其余行仍是完整 Group 8 施工范围；“出现在表里”不代表已实现。

| 文件 | 函数/边界 | 改动 |
|---|---|---|
| `backend/app/services/skill_seeder.py` | `push_default_skills_to_existing_agents` | package digest、lock 内幂等、managed ownership、unchanged result |
| `backend/app/services/skill_installation.py` | `install_active_skill_package` | manifest/digest contract；真实 transition 才 transaction/lifecycle |
| `backend/app/services/skill_lifecycle.py` | `record_skill_lifecycle_event` | 使用 append delta；只记录真实 transition |
| `backend/app/services/agent_asset_transaction.py` | `stage_bytes`、`append_text`、`commit`、recovery、compensation | append operation、committed_recoverable/finalized、retention metadata、payload GC |
| `backend/app/services/skill_distiller.py` | native package commit/DB projection | file commit 后显式 projection/finalize；projection 失败可 compensation |
| `backend/app/runtime/hooks_setup.py` | `_build_t2_for_sealed_segment` | authoritative tenant resolution；metadata 比对；typed hold |
| `backend/app/memory/t2/segment_package.py` | `build_t2_segment_package*` | source refs/coverage ledger、resolver reconstruction、commit 后 staging lifecycle |
| `backend/app/memory/t2/job_sweep.py` | retry/sweep | authority 修复后的 retry state；reason 分型；不删 exhausted work |
| `backend/app/services/session_workspace_snapshot.py` | capture/clone/restore/prune | tenant CAS manifests、refs、leases、mark-and-sweep |
| `backend/app/memory/t0/ledger.py` | append/seal/replay | sealed locator、cold resolver、portable export/parity |
| `backend/app/services/invocation_trace.py` | append/persist/read | JSONL fallback -> segmented ACK spool；DB 仍是 canonical trace |
| `backend/app/services/document_conversion.py` | raw/conversion storage | rebuildable cache + durable ref promotion |
| `backend/app/services/agent_tool_domains/web_mcp.py` | fetched content conversion | force refresh 不绕过 dedup/retention |
| `backend/app/config.py` | storage settings | provider/bucket/limits/encryption/retention；生产 fail closed |
| `backend/app/main.py` | startup recovery/sweeps | bounded recovery、metrics、无无界全量 rewrite |

### 14.3 Canonical 文档同步

剩余 storage lifecycle 实现施工时，同一轮同步：

- `docs/memory-vault-path-contract-2026-06-23.md`：逻辑 path 与物理 cold locator。
- `docs/agent-memory-purity-spec.md`：staging/rollback/retention 生命周期。
- 运维 runbook：inventory、backfill、dry-run、quarantine、restore、sweep。

§2.3.1 的止血不改变 Memory Vault 物理路径 contract，因此尚未改写这些 canonical 文档。后续一旦引入 cold locator、blob/ref 或 retention lifecycle，必须在同一施工轮同步，不能让实现与 canonical contract 漂移。

## 15. 验收门

### 15.1 停止增长

- **已通过（§2.3.1）**：default Skill exact-match startup 为 0 transaction、0 lifecycle event、0 revision change、0 mtime change。
- 只有真实 Skill package transition 才产生 transaction。
- append-only ledger 单次 transaction payload 与新增行大小同阶，不与历史文件大小同阶。
- invocation local spool 有 segment limit、ACK 和 oldest-age alert。

### 15.2 Authority

- 新 T2 job `tenant_id=NULL` 为 0。
- Hook metadata 缺失不再导致错误 tenant；冲突必须 fail closed。
- Blob/ref/RLS 跨 tenant 读取、dedup、GC 全部拒绝。
- system-scoped blob 必须显式 system authority。

### 15.3 数据完整性

- sealed T0 cold replay 的 event count/order/hash chain 与本地原始数据完全一致。
- `source.md` 和 `events.jsonl` 都可恢复，职责保持不变。
- Snapshot restore/fork 的文件 byte/hash 与迁移前一致。
- T2 source bundle 能由完整 refs 重建，LLM input coverage 无丢失。

### 15.4 GC 与恢复

- dry-run 零 mutation。
- quarantine 可恢复。
- apply 绑定 manifest hash，状态变化时 skip。
- 未知、corrupt、unowned、unfinalized、有 ref/pin/lease/legal hold 的对象绝不删除。
- Volume backup 和 Object Storage backup 均完成 restore drill。

当前 production 证据：transaction dry-run/hash-bound apply/journal recheck/quarantine/restore/sweep 均有 durable receipt；首批 11,977 个对象先完成 production restore drill，再以 exact scope 重隔离和 sweep，第二批 9,118 个 superseded revision 也经独立 manifest/sweep 复核，全部 `skipped=[]`。web cache、trace ACK spool 与 T2 rebuildable staging 也有各自 exact manifest 和双段 receipt。Volume 级备份恢复、Object Storage restore drill 和跨资产统一 GC 尚未通过，因此本小节仍是“已完成事故 scope + 完整生命周期局部闭环”。

### 15.5 生产容量

事故处置前本文曾估算完整冷热分层后热 Volume 约 8-12 GB。2026-07-15T15:28Z 的实际 production `df` 已为 used=`11,316,330,496`、available=`37,558,562,816`、usage=`24%`；这是清理明确可重建/已 ACK/已 supersede 数据后的事实，不是继续删除核心数据的目标值。

后续容量优化仍依赖：

- 旧 transaction 大 payload 安全 finalization/GC；
- snapshot CAS 回填；
- T2 authority 修复和 staging 收敛；
- sealed T0 冷归档；
- trace/cache 有界化；

后续不再以“低于 11.3 GB”为完成条件。T0、snapshot、workspace、current Memory 和 hold 的自然体量可以使热层高于该值；只有在 capability-preserving 的 sealed archive/CAS/resolver 路径上线并完成 byte/hash/replay 后，才允许相应物理块离开热 Volume。最终同时以 production immutable manifest、容器 `df`/allocated bytes 和滞后的 Railway metrics 为准。

### 15.6 完成定义

只有以下证据同时存在才能称为完整 storage lifecycle 完成：

1. 当前代码路径和 migrations。
2. 聚焦测试与完整 backend suite 零失败。
3. 生产 backfill receipt。
4. byte/hash/count parity report。
5. dry-run manifest 与 quarantine/sweep receipt。
6. restore/fork/replay/fault-injection 结果。
7. Railway Volume、T2 backlog、transaction growth、snapshot dedup 和 cold read metrics。

“创建了 Bucket”“新增了表”“磁盘变小了”都不足以单独证明闭环。

截至 2026-07-15T15:28Z：第 1、2、3 项已对 transaction substrate 成立；第 5 项已有 transaction restore/sweep、web cache、trace ACK 与 T2 staging 事故处置 receipts；第 7 项已有本次 `df` 和主要目录 allocated-byte 终态。Object Storage parity、Volume/Object restore drill、snapshot CAS/fork、sealed T0 replay、T2 authority/replay、常态 scheduler/metrics 和跨资产 legal-hold/export/deletion ledger 仍未齐。因此当前裁决是“transaction lifecycle 子闭环 + 派生数据事故处置完成 + 完整 Group 8 方案未闭环”，不是 Group 8 或 `MISS-RETENTION-001` 完成。

## 16. 当前继续禁止的动作

本次事故清理已按 §2.3.2/§2.7 收口，owner 明确要求“可优化的优化，不勉强；不删除核心数据”。从该时点起：

- 不再对当前 Volume 做一次性物理清理；任何新动作必须回到完整 Group 8 代码、测试、migration/backfill、restore 和用户对具体不可逆 manifest 的确认门。
- 不删除或即时 dedup T0、T2 job manifest/issues/replay identity、current Memory/Soul/Skill、workspace、workspace snapshot、current/latest transaction、policy hold、unacked trace 或有 durable ref 的 cache/artifact。
- 不把“目录大”“重复率高”“retry exhausted”“Railway 曲线未回落”单独作为删除事实；T2 只允许逐出可由 T0/DB 重建的 allowlisted 临时 payload，且本次 eligible 集合已处理完。
- 不执行 snapshot hardlink/CAS one-off；必须先实现 tenant-scoped CAS、immutable ref、checkpoint pin/lease、fork/restore parity 与 rollback，再迁移。
- 不以旧 GC、sweep、cache、trace 或 T2 manifest 扩大候选范围；所有 receipts 只证明当时 exact file set。
- 不创建 Railway Bucket。
- 不修改 Volume mount、容量、backup schedule 或 service variables。
- 不部署 storage provider。
- 不对现有 held job 执行批量重放。
- 不把现有 `backend-volume` 目录直接同步后删除源文件。

这保证容量优化永远服从 Memory、恢复、fork、rollback、审计与用户文件完整性，也保留后续对 provider、retention 和 legal hold 的审阅空间。

## 17. 参考资料

- Railway Volumes reference：<https://docs.railway.com/volumes/reference>
- Railway Volume backups：<https://docs.railway.com/volumes/backups>
- Railway Storage Buckets：<https://docs.railway.com/storage-buckets>
- Railway Bucket billing：<https://docs.railway.com/storage-buckets/billing>
- Railway pricing：<https://docs.railway.com/pricing>
- Hive Memory Vault Path Contract：`docs/memory-vault-path-contract-2026-06-23.md`
- Hive SOTA master goal：`docs/hive-sota-master-goal.md`
