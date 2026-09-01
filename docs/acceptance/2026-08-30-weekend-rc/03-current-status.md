---
document_id: weekend-rc-2026-08-30-current-status
owner: Codex
status: active
authority: canonical-working-state
last_reviewed: 2026-09-01
source_commit: 6d46459e3a3dcf50dd32043583f4ab57667b0701
verification_status: candidate-d2-i18n-revalidation
---

# 当前状态与唯一下一动作

[返回索引](README.md) · [旅程账本](04-journey-ledger.md) · [Findings](05-findings.md) · [Runbook](06-runbook-and-release-gates.md)

> owner 已接受 PDEC-001～PDEC-011 与 D/E 合同：主 Codex 独占 Goal、语义 verdict、生产 E2E/A2A、集成、部署和交付，可使用 Codex 原生 Multi-Agent/subagent 做 bounded repo 调查、实现、测试、build 与 review；Kimi、zCode、Coze、ACP、`agent-delegation` 和第二语义控制器仍禁用。96 条 production journeys 保持冻结；当前 manifest 下 pass 1/pass 2 均未运行，没有同提交双遍 Closed journey，NPTCR 为 0%。

## 当前目标与可观察 Done

完成 Weekend RC 整体验收与修复闭环：真实 E2E → earliest defect → live-path root cause → 完整修复与回归 → 主 Codex 独立 review/integration → coherent application `D` 三服务部署 → signed-in 双遍、权限负向与故障恢复 → evidence-only `E` → 合成资产 cleanup。

可观察 Done：全部 in-scope frozen journeys `Closed loop`；NPTCR=100%；五条护栏全部通过；Evidence Coverage ≥95 且不抵消七原子缺口；Zero Known Defects；backend/backend-api/frontend 运行同一 exact `D`；双遍、故障、权限、cleanup 和 rollback evidence 完整。

当前 proof order 固定为：`Agent 智能 → 全部前后端功能可用 → 权限/RLS/安全 → Release`。现有 authority/secret/effect 边界全程保留，但权限加固与安全评分不得抢在功能补全前或阻断无关功能；最终必须同时达到“很好用”和“很安全”。

## 当前事实快照

| 事实 | 当前值 | 证据边界 |
|---|---|---|
| execution roles | 主 Codex 全责；原生 Codex subagent 只做 bounded repo 工作 | subagent 结果是 candidate，不拥有 verdict、生产 effect、部署或交付权威 |
| 当前 production | 三服务同为 `6d46459e3a3dcf50dd32043583f4ab57667b0701` | backend `637818b5…`、backend-api `0bce9b71…`、frontend `5dccd5b8…` 均 `SUCCESS`；public health/frontend 绿，只是当前 supporting baseline，不是最终 D |
| production manifest | 35 组 / 96 条，external fake 禁止 | `valid=true`，hash `7994b502361de0eafbea17b0fa5fd33eaa47d8a1381dc151126a463e2416d93a`；只改 execution metadata/proof order，旅程 ID、分母和语义未改 |
| NPTCR | 0/96 Closed | current manifest pass 1/pass 2 均未运行；机械 scorer 固定 `semantic_verdict=not_computed_by_tool` |
| P29-PADMIN | 旧 manifest hash 有 historical pass-1 supporting evidence；六个 audience/secret 根因 production `Verified` | current manifest pass 1/pass 2、expired-session/role-change 与四角色矩阵均未运行；历史 evidence 不迁移为当前 PASS |
| role fixture | production synthetic tenant `0430e023…` 已有 2 org_admin、2 member 与 1 scoped-operator candidate | 全部经 register/company invitation/join/正式 permission API 建立；没有 forged token、直接 role/tenant DB mutation 或 RLS weakening；身份只约束角色旅程，不阻断普通功能探针 |
| P08-J4 | `Breakpoint / LOCAL_SEAM_GREEN` | exact-session tool + resolved-path boundary、active-run profile lock、run-scoped provider/terminal evidence、完整 authority attestation 与 server-derived build identity 已形成 local candidate；169 项 J4/Session/tool/health seam 回归通过，仍须完整 profile、部署和 same-envelope production bakeoff |
| admin company / invite | `Partial loop / PROD_SUPPORTING_VERIFIED` | production 已完成 platform-admin 建公司、两名 org-admin、两名 member、member 禁止创建管理员码、管理员码 tenantless replay 拒绝、member join 与后台 Back to App→Home；current manifest 双遍仍未运行 |
| Legacy-brand release residue | `Deployment gate / PROD_SUPPORTING_VERIFIED` | `6d46459e` committed archive gate 3375 paths 通过，production hard reload/导航无已退役品牌残留；只保留兼容合同允许项；新候选提交仍须重跑 exact archive gate |
| operator inspection authority | `Breakpoint / LOCAL_REVALIDATION_GREEN` | production 已复现 generic `manage` 可任意理由跨用户读、权限主体泄露与静默 scope 改写；本地 candidate 已分离 `operator.inspect`、reason+同事务审计、只读、deny-wins/revoke/expiry、严格 permission schema、最小 list/detail shell 与 UI grant/revoke；当前完整本地门已绿，仍须新 D production 正负复验 |
| backend local validation | `LOCAL_REVALIDATION_GREEN` | 当前树第二次完整 suite **8864 passed / 2 skipped / 0 failed（14:49）**；首跑 4 个红点已收敛为 RLS allowlist 重构元数据与两个过时 workflow 测试断言，主流程隔离复验 **4/4 passed**；Ruff/format/diff check 全绿 |
| GitHub Harness CI | `D1_FRONTEND_I18N_FAILED / D2_PENDING` | `ee4be6a0` run `33526446421` 的 i18n inventory 捕获 7 个新增 operator UI key 未进入双语 catalog；该提交不得部署。当前 candidate 已补齐 exact 7 keys，须相称本地复验、形成 D2 并等待全 CI 绿 |
| frontend local validation | `LOCAL_REVALIDATION_GREEN` | 当前树 i18n audit **9/9**、双语 catalog 均 **4071 keys**、全部 gap 为 0；D2 相称重验 Vitest **163 files / 1216 tests**、production build/bundle gate、Playwright **34/34** 全部通过。operator 浏览器 fixture 真实执行 reason→Apply；本地结果不迁移为 production Journey PASS |
| atomic full-stack journeys | `LOCAL_REVALIDATION_GREEN` | 当前树在全新数据库、空 Redis、固定顺序下 **15/15 passed（2.1m）**；J-09/J-11/J-13/J-15 使用正式 scoped grant 与 operator reason→Apply，普通功能旅程仍不依赖 operator 身份；本地结果不改变 production 96 条分母或 NPTCR |
| response learning | `Breakpoint / LOCAL_RECOVERY_GREEN` | required post-commit terminal outbox 已把 canonical binding、claim/validate/process/ack、retry/dead-letter/reconciliation 与 input admission hold 接入共享 terminal settlement；backend full-suite 已绿，仍须 frontend/build、fresh-chain、部署与 production crash/replay 证明 |
| session summary | `Breakpoint / LOCAL_RECOVERY_GREEN` | Kernel 不再提前持久化 summary；Web terminal processor 只从 committed transcript/result 生成 summary，未知 provider outcome 进入 durable reconciliation，旧 sequence 不覆盖新 summary；仍须 coherent D production hold/abort/replay 证明 |
| Local Agent | daemon running；API `401 Invalid bridge token`；UI linked 0/offline | `Breakpoint / RECOVERY_QUEUED`；PDEC-008 授权 lab login/pair/revoke，不授权读取或轮换真实 secret |
| model readiness | MiniMax 与 GLM bounded live probe 成功；DeepSeek 单次 `HTTP 402 Insufficient Balance` | DeepSeek 为 `EXTERNAL_UNAVAILABLE`，不是 Hive defect、PASS 或 Closed；P33-DEEPSEEK 在恢复或 owner Excluded 前保持未闭环 |
| Stage 1 probe | `P01-STAGE1-FRESH-FALCON-682` | signed-in `platform_admin` + EventPilot Session `65b98e1a…` 已完成并 hard reload：GLM-5.3、3/3 todos、一次写、一次读、1 artifact、0 running/0 waiting；只作功能 supporting evidence，不替代 employee persona PASS |
| workspace boundary | 保留 owner 既有 `.ultra/.runtime/compact-snapshot.md`、`bp-kingdee/`、`output/`、root `package*.json`、`tmp/pdfs/` 等内容 | 不纳入本轮提交、不清理、不覆盖 |

## 当前产品总判断

| 验收域 | 当前判断 | 仍需证明 |
|---|---|---|
| Git / Production | 基线健康，不等于 RC 完成 | 后续修复形成 coherent `D` 后三服务同提交部署和完整双遍 |
| Single Agent / Session | `Partial loop` | employee persona 开放任务、stream/reload/recovery、20 commands 完整双遍 |
| Memory / Growth | `Partial loop` | T0→T2→T3→Soul/Skill 真实消费、J1/J2 纵向收益、J4 same-envelope bakeoff |
| Personal / Company KB | `Partial loop` | 多格式、多入口、解析/索引/引用、promotion/import、权限负向与恢复 |
| HR / Agent creation / identity usability | `Breakpoint` aggregate | 最小 supported-path employee fixture、Agent/HR 创建、首个任务与正常生命周期主路径 |
| Permission / RLS / Security | `Breakpoint` aggregate | 功能补全后再验四角色矩阵、offboarding/active revocation、RLS no-leak 与完整安全对抗 |
| Subagent / Team / Workflow / A2A | `Partial loop` | 五种独立语义的真实入口、失败恢复、父任务/最终 UI 消费 |
| Automation / Hook / Skill / MCP / Local Agent | `Breakpoint` aggregate | lifecycle、Trust Review、真实调用、offline/reconnect/revoke |
| Frontend / Agent Detail / Artifact | `Partial loop` | Codex Desktop streaming/recovery、Letta-only rail、角色信息边界、格式/preview/download、a11y 矩阵 |
| Model fidelity | `Breakpoint` aggregate | MiniMax/GLM/DeepSeek frozen compatibility task、selected-model fidelity、token/cache/cost/operator evidence |
| Release | `Partial loop` | final `D`、三服务 exact deployment、两遍 96 journeys、fault/negative/cleanup、evidence-only `E` |

## Model Agency、RLS 与 fixture 当前边界

- Hive 产品 turn 的 selected runtime LLM 拥有任务语义与回答表达；RC 循环由主 Codex解释 evidence、决定验收 priority/quality/Closed 与最终交付表达，owner 裁决产品语义和风险授权；subagent、NPTCR、CI、测试、receipt、timeout、attempt、health、Railway 和 `mechanical_ready` 只报告 exact facts 或聚合已接受 verdict。
- fixture 只经受支持、经过认证的 UI/API/control-plane path 建立。禁止 forged JWT/token、直接修改 tenant/role DB 字段、RLS weakening 或用 broad bypass 创造业务授权。
- RLS/ACL 只在 exact unauthorized ingress/read/write/effect 处 fail closed。已登记 read-only cross-tenant deny/not-found probe 继续执行，但不得获取 protected bytes 或产生效果；一个 denial 不得停止无关推理或获准工具。probe 若意外返回 protected bytes，立即停止该 lane，不传播或保存 raw bytes，只留最小脱敏 P0 事故证据。
- 不设人工 Goal-wide timeout/step/attempt cap；task-sized per-call/per-attempt timeout、cancel、quota 与 backoff 只控制当前 attempt。

## 最近有效进展与验证

- fresh-schema atomic suite 已完整 **15/15 passed（3.0m）**。迭代中依次修复：Goal 初始输入未走 canonical admission、terminal boundary 对 T0 relay metadata 误判、T0 UUID hex/hyphen identity 漂移、artifact trigger 的 terminal UUID 文本匹配、subagent terminal 与 durable notification 非原子、canonical `session_terminal_outcome` 绕过 Team close projection。最终整组固定顺序无失败；该证据不迁移为 production Journey PASS。
- 十个 Session/tool/authority/runtime/audit 根因已有 production `Verified` evidence；finding-level Verified 不自动升级 Journey。
- 旧 manifest hash 上的 `P29-PADMIN` clean pass 1 与 pass-2 blocker 文件只保留为历史 supporting evidence；current manifest 下 pass 1/pass 2 均未运行。缺少 employee/operator fixture 不再阻断 Agent 功能探针，只影响后续 canonical persona acceptance。
- signed-in Browser 只读复核确认 D3 Session `0731ec15…` 在生产显示 `完成 · GLM-5.3`、精确 final、一个 77 B artifact、0 running/0 waiting；它只证明真实 Session/model/tool/artifact 基础路径存在，不是当前 manifest P01 PASS。
- `RESPONSE-LEARNING-COMMIT-ORDER-001` 已形成本地 `Fix Candidate`：Kernel/invoker 不再发低层 terminal hook；Web 只在 canonical commit receipt 后调度 secret-redacted `RESPONSE_COMPLETE`；candidate/projection 按稳定 key 原子幂等。主 Codex跨域 **304 passed**，独立 review **52 passed**，Ruff/format/diff check 绿；但 postcommit crash recovery 尚未闭环，不能标 `Verified`。
- `SESSION-SUMMARY-COMMIT-ORDER-001` 已 production-shaped RED **1 failed**：实际顺序为 `summary_projection → canonical_hold`；`ChatSession.summary/last_message_at` 在 final authority 前已 commit，并可经 Session recall 回流后续 Agent。
- owner action-time 确认后，`P01-STAGE1-FRESH-FALCON-682` 只发送一次。production GLM-5.3 完成公开 3 步计划、3/3 Work Ledger todos、一次 `write_file`、一次 `read_file`、七项硬判据 final 与一个 artifact；主 Codex通过 artifact preview 独立核对标题、marker、3 行议程、`TOTAL_MINUTES=90`、2 行风险、`RISK_ROWS=2`、四项现场清单。hard reload 后 final/todos/artifact/0 running/0 waiting 全部恢复。该 principal 为 `platform_admin`，只证明功能 supporting evidence，NPTCR 不变。
- 本轮合同修订后 production manifest 为 `valid=true`、denominator 96、hash `7994b502361de0eafbea17b0fa5fd33eaa47d8a1381dc151126a463e2416d93a`，且 `semantic_verdict=not_computed_by_tool`；96 个 variant ID 与 HEAD 基线逐项相同，未新增、删除或重排旅程。
- P08-J4 candidate 已把四个 seam-level P1 收口到共享 authoritative boundary：exact-session 工具/路径在 execution pipeline 与最终文件 I/O 双重校验，active exact profile 在 terminal 前锁定，provider/final/terminal evidence 绑定 exact run，authority/build identity 不完整即 fail closed；J4/Session/tool/health 合并 **169 passed**，尚未升级 Journey verdict。
- terminal/learning/reconciliation candidate 的历史合并回归达 **497 passed**（含真实 PostgreSQL）；Web、Session V2、trigger、business task、delegation、budget、worker、startup/orphan/reconciliation 均复用 required terminal outbox。重跑还发现并修复 delegation parent projection 脱离注入 tenant DB 的旁路，direct real-PG **21 passed**；这些结果早于当前 Budget/operator 修复，最新 working tree 仍须 full backend、frontend/build、fresh-chain 与 production crash/replay 证明。
- 最新源码在 CI 同型 `DATABASE_URL=127.0.0.1:1` fail-fast 环境下完整 backend 套件 **8786 passed / 2 skipped / 0 failed**（14:19）。此前同一套件捕获的 5 个失败来自 terminal recovery 把 BYPASS DB capability 带入动态 HookRegistry；现按既有 locator→tenant-scoped pattern 只在 BYPASS 下定位 `(tenant_id,id)`，实际 sealed/candidate recovery 在 tenant session 执行，RLS scope 回归 **17 passed**、terminal/Team 邻域 **51 passed**。两项 skip 的 exact 原因已登记；该结果只建立 local candidate，不迁移为 production Journey PASS。
- GitHub Harness CI 的既有全仓 Ruff 步骤会把 5082 条历史债务误算成本次提交失败；现与发布合同对齐为仅检查本次新增/修改/重命名 Python 路径，同时执行 `ruff check` 与 `ruff format --check`，full pytest 保留。首个 D CI 又暴露 `ruff>=0.8.0` 在 GitHub 漂移到 0.16.5、与本地验证的 0.15.12 规则集不一致；dev/CI 已精确 pin `ruff==0.15.12`。工作流结构 **3 passed**、YAML 解析、b2→candidate 230 个 Python 路径 Ruff/format、prompt/adversarial/internal eval 均绿；须以修正提交重跑 CI，失败 D 未部署。
- frontend 完整回归已绿：Vitest **161 files / 1193 tests**，Playwright **34/34**，i18n **9/9** 且双语 catalog 均为 4039 keys、全部 gap 为 0，production build 通过。E2E 收口了 server-owned plan hash 不回显、live-tail cursor、offline→online 连接态与完成态 disclosure 合同；4 张旧视觉图逐张核对后仅更新 stale baseline，桌面/窄屏/深色及 Axe 均通过。
- 首个 D 的 Linux CI 功能 E2E **29 passed**，另 5 个 active-state visual 因只更新了 Darwin 而稳定命中旧 Linux baseline（约 3%）。主 Codex逐张核对 CI expected/actual/diff，actual 与已接受的 Darwin active-state 结构一致，首跑/重试除 25 个抗锯齿像素外稳定；5 张 Linux baseline 已用 CI actual 精确更新，须随 Ruff pin 一起重跑 CI。
- 修正提交 `e6957205` 的 CI run `33448515330` 已确认 frontend **34/34** 与 atomic full-stack **15/15（2.9m）** 全绿；backend 干净 runner 的最早根因是工作流忽略既有 `uv.lock`，拉取 Testcontainers 4.15 后将旧 import 的弃用警告放大为 812 个真实 PG setup error，同时 runner 缺少产品要求的 Linux `bubblewrap`。余下 4 个 unit failure 是新 DB/extension-policy seam 未显式注入，1 个 `/private/tmp` 断言错误地在 Linux 执行。candidate 现统一使用 frozen `uv.lock`、安装并验证 `bubblewrap`、补齐 unit seam 与 OS-specific gate；CI 同型定向 **15 passed**、完整 backend **8786 passed**，仍须新提交重跑三条 CI 后才部署。
- `d5282517` 的 CI run `33451025118` 已把 frontend **34/34** 与 atomic full-stack **15/15（2.9m）** 跑绿；backend 在 Ubuntu 24.04.4 干净 runner 跑到 89% 后被 30 分钟 job timeout 取消，且 collection 顺序把 51% 唯一 `F` 精确映射为真实 `_run_command` Linux sandbox 执行测试。根因是 `bwrap --version` 只证明二进制存在，而 Ubuntu 24.04 默认以 AppArmor 限制无特权 user namespace。D4 candidate 安装官方 `apparmor-profiles`、只加载 scoped `bwrap-userns-restrict`，并调用仓库既有 `probe_os_sandbox_capability()` 证明真实 launch；不关闭全局 AppArmor、不启用 unsandboxed bypass、不跳过测试。完整 backend 本机实际需 14:19，job budget 从 30 调为 60 分钟；仍须新 CI 全绿才部署。
- coherent baseline `6d46459e` 的 CI run `33453594851` 已全部成功，且 exact frozen archive 的 backend `637818b5…`、backend-api `0bce9b71…`、frontend `5dccd5b8…` 均为 production `SUCCESS`；public health/frontend 绿。它是当前 supporting baseline，不是含 operator 修复的最终 D。
- production `WEEKEND-RC-ROLE-FIXTURE-1B4BE5D2` 已通过支持路径形成 tenant、双 org-admin、双 member 与 operator candidate；公司创建、管理员/成员邀请、join/token refresh、Back to App→Home、管理员码不可见/不可复用与 member 负向均取得 supporting evidence。普通 Agent 功能探针不要求先切换这些角色。
- `OPERATOR-AUTHORITY-001` 已在 production 复现：generic `manage` 能以任意 reason 跨用户读 Session，delegated permission GET 泄露主体，非法 company/root mutation 被静默改写。本地 candidate 将 operator inspect 变成独立 governed grant，跨 owner 只读且 reason 必填/审计，mutation 永拒，active deny 优先于 allow，revoked/expired fail closed，并把 operator-only Agent list/detail 降为最小身份壳；当前 backend/frontend/Playwright/atomic 已统一重验全绿，但尚未形成或部署新 D。
- 首个 application candidate `ee4be6a0` 的 CI run `33526446421` 在 frontend i18n inventory 首门失败：7 个新增 operator UI key 依赖源码 fallback、未进入 en/zh catalog。当前树已补齐 exact 7 keys，local i18n audit **9/9**、en/zh **4071/4071**、所有 gap 为 0，且 Vitest **1216/1216**、build/bundle、Playwright **34/34** 相称重验全绿；`ee4be6a0` 永不部署，须以新提交重跑完整 CI。
- admin company/invite candidate 已通过 backend API + migration + real non-owner RLS lifecycle **33 passed**、最新 rolling/backfill 聚焦 **19 passed** 与 frontend mounted/UI **20 passed**。production 只读盘点为 113 个历史码、105 个 active unused，105 个目标 tenant 均已有 admin，因此部署回填全部保持 `member`；empty-tenant bootstrap 才授予 `org_admin`。完整 backend 已绿，仍不等于 production PASS。
- 旧品牌 release hygiene 的 working-tree gate 当前通过（3184 paths），对应结构回归 6 passed；`6d46459e` committed archive gate 同样通过（3375 paths）。已登记本机路径、个人账号和真实形态测试身份已从 tracked candidate 中性化，历史 KDF salt、旧 env input fallback、一次性 theme-key 迁移与 LICENSE attribution 保留。该结果只证明候选 archive 兼容门，不替代三服务部署与生产复验。
- task-state resolve 指向本文件；本文件只保存当前目标、事实、证据摘要、唯一下一动作和 Not Done，不再保存旧 Kimi/zCode/ACP/timeout 执行日记。
- `backend/scripts/weekend_rc_worker_gate.py` 与对应 zCode/`agent-delegate` 测试仍是未被 active RC gate 调用的 legacy compatibility artifact；本 Goal 不调用它们，也不把其结果当作当前工作流或验收证据。

## 当前合成资产登记

| marker | 目标与允许效果 | 禁止效果 | cleanup 状态 |
|---|---|---|---|
| `D3-SETTLEMENT-C37-8K4P` | EventPilot synthetic Session；已新建/读回一个 marker 文件 | 不外发、不建 workflow/trigger/delegation、不读 credential；write failure 不重试 | `created-evidence-retained`；已登记 final cleanup，删除前 exact-target/readback |
| `P01-MAIN-CLEAN-P1-3482B-LARCH-927` | 历史无效入口 probe；run 成功但不是 frozen fresh Session | 不修改其他路径或外部系统 | `invalid-entry-evidence-retained`；永不计 PASS，待 final cleanup |
| `P01-MAIN-PASS1-3482B-MAPLE-581` | 历史功能 probe；实际 principal 为 platform_admin | 不外发、不读 credential、不外推 employee persona | `invalid-persona-evidence-retained`；永不计 PASS，待 final cleanup |
| `UI-CMD-003-PROBE` | read-only `/context`、`/usage`、`/permissions` probe | 不调用 provider/tool、不改权限 | `failure-evidence-retained`；待 final cleanup |
| `WEEKEND-RC-ROLE-FIXTURE-1B4BE5D2` | 通过公开 register/assignment/join 与正式 role/permission API 建立 synthetic company-admin、employee、scoped-operator | 不复用真实邮箱/密码、不跨 tenant、不外发、不读取/修改 provider credential；禁止 forged token、直接 DB role mutation 或 RLS weakening | `tenantless-admin-registered`；user `c51b38e8…` 尚未变更 tenant/role，密码/token 未落盘 |
| `P01-STAGE1-FRESH-FALCON-682` | EventPilot fresh production Session 的当前提交功能 truth test；3-step plan、Work Ledger、一次 write/read、硬判据 deliverable | 不外发、不调其他 Agent/外网/workflow/trigger/delegation、不读 credential；仅允许目标 `workspace/` 文件 | `completed-supporting-evidence-retained`；Session `65b98e1a…` 与唯一 artifact 已登记 final cleanup；platform-admin evidence 永不冒充 employee PASS |

共享合成 fixture 保留到所有依赖旅程完成；lane-local transient effect 在 reconciliation 后清理；final `D` 双遍结束后清理全部 Goal-created synthetic assets。owner Example Owner 基础账号、immutable evidence 和无关数据永不作为 cleanup target。

## 唯一下一动作

将 operator authority 完整修复形成新 application `D`：只提交授权范围，重跑 exact archive gate，push 并等待全 CI 绿，再从同一 frozen SHA 部署 backend/backend-api/frontend。随后在 production 证明无 grant 403、allow+reason 200、mutation 403、deny/revoke/expiry 403、delegated permission redaction 与 company-manage 422；再继续 signed-in 96 journeys 双遍、fault/recovery、rollback、cleanup 与 evidence-only `E`。D3/P01 supporting probe 不重发；DeepSeek 未获 billing/credential 授权不重试。

## Not Done / Do Not Redo

- 96 条 production journeys 未完成双遍；NPTCR=0/96，Evidence Coverage 尚未成立。
- MiniMax/GLM 只完成 bounded probe，不是 P33 compatibility PASS；DeepSeek 为 `EXTERNAL_UNAVAILABLE`，不得在未获 billing/credential 授权时盲重试。
- P08-J4 adapter、四角色 fixture、Local Agent recovery、完整权限负向、全产品 E2E/A2A、final `D/E`、rollback 与 cleanup 均未完成。
- 平台管理员创建公司、管理员/成员邀请、后台返回 App 与旧品牌兼容门已完成 `6d46459e` production supporting 复验；新 coherent D 必须保持这些结果，且 current manifest 双遍仍未完成，不得提前升级 Closed。
- `OPERATOR-AUTHORITY-001` 尚未部署；production 仍处于已复现漏洞的旧行为，部署前不得把本地绿测试表述为生产修复。
- Goal/Issue/subagent/CI/Railway/health 的机械状态不得升级 Journey verdict，也不得因失败/timeout 停止所有无关工作。
- 不把 archive、旧 manifest hash、历史 PASS、无效 persona/entry probe 或 finding-level `Verified` 自动迁移成当前 aggregate `Closed loop`。
- 不触碰或提交 owner 既有无关 dirty/untracked 路径。
