---
document_id: weekend-rc-2026-08-30-current-status
owner: Codex
status: in_progress
authority: canonical-working-state
last_reviewed: 2026-09-10
source_commit: 4fb087ddfb1fcc278f52738599dcb1be0a1fa50e
verification_status: runtime-recovery-three-service-deployment-verified
disclosure: public-redacted
---
# 当前状态与唯一下一动作

[返回索引](README.md) · [旅程账本](04-journey-ledger.md) · [Findings](05-findings.md) · [Runbook](06-runbook-and-release-gates.md)

## 最近一次已记录的发布

### 2026-09-10 03:31 Runtime 候选部署结果

遗留候选基于 `0236daae` 完成集成与测试，独立提交 `4fb087dd` 已推送并与当时远端 main 一致。该版本的三服务同源发布已获授权并完成。本次范围不包含 Ling 兼容性处理或旧 96 条验收。

部署预检：Railway 项目 Hive / production 与三项服务布局已实时核实，精确 Git archive 3505 路径发布卫生检查通过；本批没有 migration、依赖或部署配置变更。CI [34385279787](https://github.com/SteamRocket-Labs/hiveclaw/actions/runs/34385279787) 首次三 job 均因 Chrome APT 源 `Hash Sum mismatch` 在依赖安装阶段失败；原提交 attempt 2 三 job 已全部 success。远端后端 `9394 passed / 4 skipped / 2 warnings`，2757.10秒，prompt gate、对抗检查与内部评估100分通过；前端全量/构建、79项浏览器检查与15条真实 API/worker/browser journeys 均通过。

三服务已从同一 `4fb087dd` archive 部署并全部 `SUCCESS`：backend `00000195-0000-4000-8000-000000000000`；backend-api `000001b4-0000-4000-8000-000000000000`；frontend `00000388-0000-4000-8000-000000000000`。先确认 backend 就绪，再同步其余两项。发布前回退参照为 `bccda8c7`，三项旧 deployment 分别 `00000175-0000-4000-8000-000000000000` / `0000029f-0000-4000-8000-000000000000` / `00000394-0000-4000-8000-000000000000`；未执行回退。

独立运行核对：public backend `/api/health` 为 runtime 角色，frontend `/api/health` 代理为 API 角色，两者均 `ok`，与精确 archive 同1064文件/hash `3fedfcaef5d2e31793a85967b70374cb1d1be0ca5b1aabc934d3c4c5000fd9f6`；两者 RLS strict、非 superuser、无 bypass、零 violations。runtime worker/control bus/stream 均 running；19:30:36 UTC 的新 sandbox probe 通过、network deny 与 workspace round trip 成立。前端首页200，新入口 `index-CfXki53u.js` 与含清理入口的 `ControlPlane-zO99eyYf.js` 实际下载 SHA-256 均匹配已验证本地构建；未登录 GET `/api/agents/cleanup-pending` 返回401。上述证明本次部署、源码和有界健康/鉴权，不冒充生产重启/Stop/HR 清理业务验收；未做生产故障注入、新建任务或额外数据操作。

已补齐 HR abandon 清理来源条件、删除/放弃后刷新可发现的清理重试入口与列表总数边界，保留普通成员/跨租户/非活动公司拒绝。Runtime 候选包含 advisory-first 终态锁序、已封存模型/工具轮恢复、Stop 效果围栏、逐轮幂等计费与有界公平重试。首次全量发现终态任务可因相同请求重新 prepare；已恢复终态拒绝，原不可变 seal 反例不改断言，未结束任务的合法恢复仍通过。主循环仅提取最终响应处理，架构上限不变。

最终本地验证：首次后端全量 `9379 passed / 15 failed / 3 skipped`，失败逐项定位修复；数据库测试路由改为每测试恢复并覆盖 `_async_session`，真实 worker 按既有批量分轮消费且检查精确目标回执，真实非 owner RLS 隔离 claim，逆序并发检查保留模型结果顺序。第二次全量 `9395 passed / 3 skipped / 1 warning`，1113.97秒；两项跳过因本机缺 OfficeCLI，另一项钉钉 Skill 无声明工具，一项既有 Starlette 弃用警告。修后整目录真实 PG 与连接池另有 `380 passed / 2 skipped`；语义历史与恢复37、架构/内核116、来源ACL/Hook15等相关组合通过。前端175文件/1316项、TypeScript/Vite构建与预算、中英文4187项及i18n审计通过；50个本批Python文件 Ruff/格式、文档结构10项和diff检查通过。全量包含RLS正反例，审查保留110个直接调用，仅新增已审查的 deleted-agent cleanup 查询；指纹 `b2a25dfc940738bbd3b031bd7525ebec45a946af85fe7717b130dd20b12de51c`。上述均为本地/隔离PG验证；生产发布与远端CI结果见上方独立记录，均不替代真实业务验收。

## 八步功能结果

验收范围按 [PDEC-016](02-owner-decisions.md#2026-09-09-最新裁决pdec-016) 确定。下表汇总截至上述记录日期的证据；本次文档整理没有重新执行生产验收。

| 步骤 | 当前已验证结果 | 限制与后续条件 |
|---|---|---|
| 1 固定 / A2A 工作流 | 固定v1两组参数复用；A2A三Agent交接、gate/retry、文件下载及原父自动报告/预览 | 保留失败历史，发布回归按实际改动覆盖，不重跑已完成链 |
| 2 Dynamic | 修正版冻结定义、真实并行51/57、review/wait、文件108、原父自动消费/正确解释旧失败及重开通过 | 有界路径完成；保留首版绑定错误和人工修正记录 |
| 3 自动化 | once/schedule/event真实文件交付；same_session输入缺失已修、真实PG恢复及生产回帖/文件/重开通过 | 有界路径已完成，保留旧失败 |
| 4 Local | 实际pwd、final/ack、重开；0.1.10上游/npm发布、隔离安装、本机更新与同绑定在线 | 已完成，保留发布证据 |
| 5 Office / Goal | 原生预览/校验/公式/下载/重开；自动两run及计费一致 | 保留已验路径，按后续改动做必要回归 |
| 6 Memory / Growth | 更正/退役/fresh排除；Useful反馈落盘；召回修复/去重已部署；单次MiniMax原问题正确消费/来源引用 | Ling仍FAIL；需Owner决定模型兼容性处理范围，来源权限拒绝不绕过 |
| 7 Hook / Skill / MCP | Skill加载、卸载/重装/幂等；Context7实际调用；Hook pre/post/stop真实回执、121项本地生命周期/故障检查；新增合成Hook已卸载/删除可恢复 | 有界路径完成；本地故障测试不称生产故障注入 |
| 8 角色 / 移交 / 离职 / 发布清理 | 正负向API、临时org_admin授予/撤回；真实合成移交/停用、旧token与新登录撤销；资产诊断修复发布和刷新复验通过 | 有界路径完成；后续应用 `4fb087dd` 的 CI 结果见上节，不外推生产业务验收 |

第 6 步保留 Ling 的语义消费失败。一次经授权的 MiniMax 对照成功后已恢复 Ling，不能据此宣称所有模型通过。其他七步仅在已记录的有界路径完成，本地故障测试不等于生产故障注入。

## 唯一下一动作

已授权的 `4fb087dd` 发布已完成：CI 三 job success，backend、backend-api、frontend 三服务同源 SUCCESS，健康、运行源码身份与前端资源核对通过；本次证据仅更新本文，不重新部署文档提交。没有自动续作项。无关旧审计草稿、临时产物和构建缓存原样保留；Ling 暂不处理，不重启旧验收。此前 Dynamic 自动父消费、Hook 清理及合成账号移交/停用已交付，不再沿旧下一动作重做；历史证据见[剩余执行记录](evidence/c655a4d351b5c9a17d4602f50a158bb93ef64c21/remaining-functional-2026-09-09-01.md)。

## 权限与执行规则

沿用 owner 2026-09-06 授权：仅 supported-path、经过认证、先登记且可回收的合成生产操作；允许本任务必要修正、commit/push、CI、三服务同源部署和 cleanup。无新充值/凭据、伪造身份、直接 role/tenant DB 修改、RLS weakening、真实外发或无关数据效果。浏览器动作仍遵守具体效果的确认要求。

每包默认一个实现方案、一次集中反馈后的返修；同根因两次仍失败就交付策略选择/明确阻塞，不改名重置预算。新增反馈须有合同与可达路径依据；本包回归和可信严重危害仍阻止受影响发布。优先行为测试，普通迭代不重复全量门；等待用完成通知/有界等待与退避小状态，不做 15 秒模型轮询。

## 历史批次与证据

本页只保留最近发布、八步结果、剩余条件和资产登记。较早批次的执行过程、失败、修复和复验在以下记录中查询；其当时的状态与授权不覆盖本页或 PDEC-016。

| 批次 | 证据入口 |
|---|---|
| B1：功能摸底 | [入口、结果与未完成范围](evidence/17f073bb4f07098e55d9ef1684781dc67cfa454e/functional-batch-2026-09-08-01.md) |
| B2：终态恢复 | [第二轮输入、恢复与回执](evidence/33f6332f663f6e648f27eb704593876c4de17053/functional-batch-2026-09-08-02.md) |
| B3：Local 与 once | [局部交付与原始阻塞](evidence/aeaaacb59704ac7631da553c97d699c2cc87bdb4/functional-batch-2026-09-09-03.md) |
| B4：跨功能验证 | [功能结果、失败及合成资产](evidence/87b845dba4ae397bd4205b21e657e6efeb9fac7f/functional-batch-2026-09-09-04.md) |
| 八步收尾 | [各能力的实际消费、修复、发布和清理](evidence/c655a4d351b5c9a17d4602f50a158bb93ef64c21/remaining-functional-2026-09-09-01.md) |
| 旧 96 条验收 | [旅程账本](04-journey-ledger.md)与[阶段快照](archive/current-status-before-functional-batch-2026-09-08.md) |

资产诊断曾发生一次对非合成 Agent 的核对偏差，只更新诊断状态为 `drifted`，原生配置、文件和权限未变。该偏差及 `87565c5b`、`7a4777f8`、`bccda8c7` 的修复和复验保留在八步收尾记录中，不改写为未发生或已恢复 `applied`。

## 当前合成资产登记
| marker | 目标与允许效果 | 禁止效果 | cleanup 状态 |
|---|---|---|---|
| `D3-SETTLEMENT-C37-8K4P` | EventPilot synthetic Session；已新建/读回一个 marker 文件 | 不外发、不建 workflow/trigger/delegation、不读 credential；write failure 不重试 | `created-evidence-retained`；已登记 final cleanup，删除前 exact-target/readback |
| `P01-MAIN-CLEAN-P1-3482B-LARCH-927` | 历史无效入口 probe；run 成功但不是 frozen fresh Session | 不修改其他路径或外部系统 | `invalid-entry-evidence-retained`；永不计 PASS，待 final cleanup |
| `P01-MAIN-PASS1-3482B-MAPLE-581` | 历史功能 probe；实际 principal 为 platform_admin | 不外发、不读 credential、不外推 employee persona | `invalid-persona-evidence-retained`；永不计 PASS，待 final cleanup |
| `UI-CMD-003-PROBE` | read-only `/context`、`/usage`、`/permissions` probe | 不调用 provider/tool、不改权限 | `failure-evidence-retained`；待 final cleanup |
| `WEEKEND-RC-ROLE-FIXTURE-1B4BE5D2` | 通过公开 register/assignment/join 与正式 role/permission API 建立 synthetic company-admin、employee、scoped-operator | 不复用真实邮箱/密码、不跨 tenant、不外发、不读取/修改 provider credential；禁止 forged token、直接 DB role mutation 或 RLS weakening | `supported-path-created`；production 已记录双 org-admin、双 member 与 operator candidate，但 tracked artifacts 不保存登录材料或 exact principal/grant 清单；须复用安全登录态或 action-time supported login，final cleanup pending |
| `WRC-M1-EMPLOYEE-20260904-CEDAR` | 原计划经正常注册与 member invitation 加入 fixture 的合成 employee | 不猜测、重置或复用未知密码，不升级管理员、不外发 | `legacy-registered-credential-unrecoverable`；未用于当前验收，保留为 final cleanup exact target |
| `WRC-M1-ORGADMIN-20260904-GROVE` | 正常注册后由平台后台 assign-user org_admin 加入上述既有合成公司，仅用于成员邀请及公司管理员验收；不把 CEDAR 升级 | 不新建 tenant、不改其他用户权限、不读取或修改 provider/组织 secret、不外发；保留 In-app owner 登录，临时密码仅在操作内存 | `org-admin-assigned-credential-unrecoverable`；username `wrc_m1_admin_20260904_grove`，synthetic mailbox 同名 `@example.com`。owner当场授权后经production正式确认框提交，fixture member count `6→7`；Codex未持久化创建密码且当前产品无无需旧密码的受支持重置路径，因此该principal不能用于后续登录，保留为final cleanup exact target。条款确认遗漏已披露 |
| `WRC-M1-ORGADMIN-20260906-GROVE-R2` | 经production正式注册并由后台赋予同一fixture `org_admin`的替代候选 | 不猜测或重置密码、不新建tenant、不改其他用户权限、不外发 | `org-admin-assigned-login-unusable`；username `wrc_m1_admin_20260906_grove_r2`。Chrome密码管理器覆盖了注册时的预期密码，原Keychain值独立登录返回401，错误Keychain item已删除；该 principal 不再用于验收，保留为 final cleanup exact target |
| `WRC-M1-ORGADMIN-20260906-GROVE-R3` | 经production正式注册并由平台后台赋予同一fixture `org_admin`，用于成员邀请与公司管理员验收 | 不新建tenant、不改其他用户权限、不外发；凭据只存macOS Keychain，不写仓库、聊天、命令输出或普通文件 | `org-admin-usable`；username `wrc_m1_admin_20260906_grove_r3`，凭据定位由维护者受限保存。独立登录响应核对 `role=org_admin` 与 exact tenant，Chrome Settings 显示“公司管理员”；已生成恰一次、最大使用次数1的 CEDAR R2 invitation。final cleanup pending |
| `WRC-M1-EMPLOYEE-20260906-CEDAR-R2` | 经production正式注册并消费 R3 生成的一次性 member invitation 加入同一fixture，用于普通员工验收 | 不升级管理员、不外发、不读provider/组织secret；凭据只存macOS Keychain，不写仓库、聊天、命令输出或普通文件 | `employee-usable-api-verified`；username `wrc_m1_employee_20260906_cedar_r2`，凭据定位由维护者受限保存。09-08 正式 API 登录重新核对 `role=member` 与 exact tenant；此前 Chrome“成员”为旧缓存，当前 UI 不作为员工证据。历史 HR provisioning 与 P01 evidence 保留。final cleanup pending |
| `WRC-P01-HR-MODEL-GATE-20260906` | CEDAR R2 从唯一 HR 入口提交一次合成员工简报，验证 P01 最小前置 | 禁止重试non-retryable run、禁止伪造模型/Agent、禁止读取或跨tenant复制API key | `failure-evidence-retained`；HR Session `000003c5-0000-4000-8000-000000000000`，输入 marker=`WRC-P01-EMPLOYEE-AGENT-R1-20260906`，终态“失败 / 当前 Agent 尚未配置模型 / 不可重试”。无 blueprint、provider call、tool effect 或新员工；final cleanup pending |
| `WRC-P01-HR-PROVIDER-BILLING-20260906` | owner 配置并绑定 GLM-5.3 后，由 CEDAR R2 fresh HR Session 原样提交一次受限简报 | provider 不可用时不重试、不充值、不换或读取 credential；禁止伪造 blueprint/Agent | `historical-external-unavailable-evidence-retained`；HR Session `00000287-0000-4000-8000-000000000000` 显示实际模型 GLM-5.3，终态“失败 / 模型额度或余额不足 / 可重试”。后续 fresh Session 已证明 provider readiness 恢复；本失败 run 未复用，final cleanup pending |
| `WRC-P01-HR-PREVIEW-20260906` | CEDAR R2 fresh HR Session 经真实 GLM-5.3 生成 exact synthetic Agent blueprint，并在owner授权后只消费该draft | 禁止额外 Skill/MCP/connector、外部消息/外网/凭据/其他Agent/workflow/trigger/automation/公司级数据 | `confirmed-and-provisioned`；Session `0000012c-0000-4000-8000-000000000000`、blueprint `0000001b-0000-4000-8000-000000000000` v1/hash `bp_133714e0424802b944e4cf77`；task `ce3dad21…` attempt 1七步完成，created Agent=`00000187-0000-4000-8000-000000000000`。final cleanup pending |
| `WRC-P01-CEDAR-WORKER-R1` | CEDAR R2经canonical HR provisioning创建的P01专用Agent，只执行受限workspace开放任务 | 不外发、不访问credential/公司级数据/其他Agent，不装plugin/MCP/connector，不建workflow/trigger/automation | `created-evidence-retained`；Agent `00000187-0000-4000-8000-000000000000`，GLM-5.3、standard/request-approval、自用可见、9 default skills、plugin/MCP/external snapshot为0。final cleanup pending |
| `P01-MAIN-PASS1-CEDAR-7K9M-20260906` | employee fresh Session完成公开plan、Ledger、唯一workspace artifact写读和hard reload | 同上且不重复业务effect | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS1-CEDAR-ASTER-20260907` | employee fresh Session完成A/B开放决策、公开plan、5/5 Ledger、唯一workspace artifact写读与terminal receipt后hard reload | 不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS2-CEDAR-BIRCH-20260907` | employee fresh Session完成C/D开放决策、公开plan、6/6 Ledger、唯一workspace artifact写读与terminal receipt后hard reload | 不执行真实切换，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-NEGATIVE-CEDAR-ELM-20260907` | employee fresh Session只尝试一次越界写，并继续唯一允许workspace写读 | 不重试越界效果，不读被拒路径，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS1-CEDAR-FIR-20260907` | exact `cc152f66` employee fresh Session完成A/B开放决策、公开plan、6/6 Ledger、唯一workspace artifact写读与terminal receipt后hard reload | 不执行真实切换，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS2-CEDAR-GALE-20260907` | exact `cc152f66` employee fresh Session完成C/D开放决策、公开plan、5/5 Ledger、唯一workspace artifact写读与terminal receipt后hard reload | 不执行真实演练或排班，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-NEGATIVE-CEDAR-HARBOR-20260907` | exact `cc152f66` employee fresh Session只尝试一次越界写并继续唯一允许workspace写读 | 不重试、读取、编辑或探测越界路径；不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS1-CEDAR-IRIS-20260907` | exact `17f073bb` employee fresh Session完成A/B开放决策、公开plan、6/6 Ledger、唯一workspace artifact写读、自然terminal receipt与hard reload | 不执行真实维护，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS2-CEDAR-JUNIPER-20260907` | exact `17f073bb` employee fresh Session完成C/D开放决策、公开plan、5/5 Ledger、唯一workspace artifact写读、自然terminal receipt与hard reload | 不执行真实维护，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-NEGATIVE-CEDAR-KELP-20260907` | exact `17f073bb` employee fresh Session唯一越界写typed deny/零效果/未重试探测，同run唯一合法workspace写读、6/6 Ledger、自然terminal与hard reload | 不换写法或探测越界路径，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-RECOVERY-CEDAR-LINDEN-20260907` | 当前D employee新合成Session `000002f8-0000-4000-8000-000000000000`、run `00000301-0000-4000-8000-000000000000`用于断线/worker restart恢复 | 仅既有六工具；一次workspace文件写入，不外发/不访问凭据或正式数据；重启前核对无关活跃run | `failed / SESSION-WORKER-RESTART-ROUND-001`；真实重启后attempt2撞round1并失败，唯一write/input保持；terminal outbox attempt1自然delivered。保留现场，修复后验证与cleanup |
| `P01-STAGE1-FRESH-FALCON-682` | EventPilot fresh production Session 的当前提交功能 truth test；3-step plan、Work Ledger、一次 write/read、硬判据 deliverable | 不外发、不调其他 Agent/外网/workflow/trigger/delegation、不读 credential；仅允许目标 `workspace/` 文件 | `completed-supporting-evidence-retained`；Session `000001f5…` 与唯一 artifact 已登记 final cleanup；platform-admin evidence 永不冒充 employee PASS |
共享合成 fixture 保留到所有依赖旅程完成；lane-local transient effect 在 reconciliation 后清理；final `D` 双遍结束后清理全部 Goal-created synthetic assets。owner Example Owner 基础账号、immutable evidence 和无关数据永不作为 cleanup target。
