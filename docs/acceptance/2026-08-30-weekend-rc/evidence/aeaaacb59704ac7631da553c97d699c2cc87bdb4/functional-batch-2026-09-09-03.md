---
document_id: weekend-rc-functional-batch-2026-09-09-03
owner: Codex
status: blocked
authority: supporting-evidence
last_reviewed: 2026-09-09
source_commit: 87b845dba4ae397bd4205b21e657e6efeb9fac7f
verification_status: local-default-recovery-verified-once-and-connector-blocked
disclosure: public-redacted
---
# 第三批：一次性自动化与 Local Agent

## 范围与授权

- 时间：2026-09-09 00:31:46—02:31:46（Asia/Shanghai），包含实现、审查、部署、实测、文档与 commit/push；前置核对最多 20 分钟。
- owner 已批准本批方案，并提醒结束时 commit、更新文档。沿用 PDEC-015：zCode GLM-5.3 实现，Codex 审查/集成/部署/E2E，一个方案、一次集中返修。
- 自动化：正式入口创建一次性合成任务，实际触发、写读文件、通知消费、刷新/无重复及 cleanup。
- Local Agent：现有绑定上的合成请求、明确批准、实际本地执行、真实回传。时间允许才补经授权的离线/重连。
- 不新增权限、安装/凭据，不直接 DB 改写，不真实外发，不新增生产重启，不处理无关旧 runtime 候选。

## 合成效果预登记

前缀 `WRC-FUNCTIONAL-B3-20260909`。仅在已认证 Example Owner 实验 scope 操作既有验收员工；允许创建隔离 Session、一个 once 任务、一个唯一 `workspace/WRC-FUNCTIONAL-B3-20260909-once.md` 合成文件与站内通知。Local Agent 仅允许当前目录的只读命令及输出唯一合成 marker，不读取实际业务文件或凭据；若正式入口要求新权限批准，按具体效果确认，不自动扩大权限。所有实际 ID、结果与清理状态在下表追加，预登记本身不代表已创建。

| 时间 CST | 对象/操作 | 实际结果 | 后续/清理 |
|---|---|---|---|
| 00:34 | production health 只读核对 | health ok、worker running，source hash 与 B2 同源；仍记录 trigger 终态事务错误 | 未推断为全部 trigger 不可用；不据此扩大审计 |
| 00:41 | 自动化正式列表 | 可读取，已有 once 展示；新建表单无 once（B1 已观察），后端 `/once` 命令实际存在 | 继续验证正式命令路径；尚无本批任务提交 |
| 00:42 | B-Worker 新 Session 正式 `/once` 输入 | GLM-5.3 实际处理，请求 01:05 Asia/Shanghai 执行一次合成写读并站内交付，先呈现精确计划再启用 | 输入一次；未授权立即写文件；等待草案 |
| 00:43:31 | Local Agent 唯一只读输入 | 正式通道受理为 `approval_required`，approval `000001d2-0000-4000-8000-000000000000` | 未重复发送；无新权限/安装 |
| 00:46 | 公司后台 Approval Center | 与 00:43:31 本批请求对应的最新单经正式 UI 确认一次；旧 B1 单未动 | 批准仅本次已授权只读派发，不改变长期权限；实际回传待核对 |
| 00:46 | once Session `000003d5-0000-4000-8000-000000000000` | 精确排程/文件/站内通知边界呈现后，选择“确认启用”并提交一次 | 待实际创建和 01:05 触发 |
| 00:47—00:50 | Local 页面返回及 app_rls READ ONLY 对账 | 返回后显示无事件；原 message `00000310-0000-4000-8000-000000000000` 已于 00:46:04 delivered，原 channel Session `000004c4-0000-4000-8000-000000000000` 有 6 条事件，最后一条本地回传含 marker，尚无 result/completed_at | 原会话未丢失但默认入口找错；真实目录不写入仓库 |

## 本批修复包

`LOCAL-DEFAULT-RECOVERY-B3`：默认通道查询要求 `source_agent_id IS NULL`，但 send 路径在首次派发前绑定可信快照的 subject agent；返回默认页后找不到原记录而创建空会话。zCode 在隔离 worktree 从 `60262379` 实现最小恢复修正与回归，保留身份、租户、approval、idempotency 边界，不改 Automation 或旧 runtime 候选。

- 初次 delegation `ecec98e5c4034d7283ef74393c558df4` 在执行前因适配器不支持 `--model` 失败，未产生源码修改。
- 修正启动方式后的 delegation `e61ba3979c4d47b0bb5c879c9d85bb22`，session `wrc-functional-b3-local-20260909`；只读核对原生 `.model.main` 为 `builtin:bigmodel-coding-plan/GLM-5.3`。实现结果、Codex review 与生产消费尚未完成。
- 首版 00:56 返回两文件候选、报告 56 项检查通过。Codex 未接受：NULL 查询命中已有空替代会话时 fallback 根本不运行，不能恢复真实现场；新增 mock 用例没有同时存在原 bound 会话与新空会话。00:57 唯一集中返修 delegation `87b1f4196a2740ea93eedb7393fed3b6`，要求实际关系查询覆盖该场景及隔离边界。
- 唯一返修于 01:16 正常返回，关系回归中还发现并修正 SQLAlchemy `order_by` 追加导致优先级键排在唯一 ID 之后的问题。最终用单条 tenant/owner/web/active 查询，优先已绑定 agent 且没有 chat mirror 的用户默认会话，再按时间/id 选最新；agent-scoped mirrored 会话排除，不改 source-agent 身份、approval 或重放语义。多条历史 bound 默认会话仅恢复最新一条，不声称补全历史会话列表。
- 主 Codex 在隔离干净源运行 service + API + real-PostgreSQL recovery 回归：**49 passed**；Ruff check 通过，两个文件机械格式化后 diff check 通过。三个文件逐一 SHA-256 一致后集成，01:20 commit **`87b845db`**，尚未生产部署。未包含未接受的旧 runtime 候选。

## 本地连接器兼容性阻塞

本机日志明确记录本批 exact message 于 00:46:48 本地 turn complete，仅一个 Bash `pwd`、退出 0；云端 6 条原事件中的本地回传含真实执行输出与 marker。随后 00:51:57 出现 `delivery_requeued`，message 回到 pending，未有 result/completed_at。

只读构建元数据核对运行安装目标为 Hive Connect **v0.1.7 / `ac1bee755797735b6bf306d8ee5410446f94f2a6`**。该 exact Git 源码 `platform/hive/hive.go::Reply` 仅调用 `sendEvent(..., "text", ...)`，不发送 result；当前云端 `record_channel_result` 才提交终态。故文本回传不能作为云端终态完成证明。没有自动升级、重装或重启本机服务，也未伪造 result 或直接改写 DB。此阻塞独立于默认会话恢复候选，后续需要明确连接器升级范围/授权。

01:21 向 owner 单独询问本机连接器升级/重启授权（未视为默认批准）。按 Hive Connect Skill 读取正式 Local Agent 安装页，页面明确返回安装源 `unavailable`，要求配置已批准的 `HIVE_CONNECT_SKILL_REPO_URL` 与 `HIVE_CONNECT_NPM_PACKAGE`。故安装还缺明确来源；没有猜测包名、安装、重新登录、改生产环境变量或扩大权限。该 Skill 限制仅暂停连接器安装，不阻塞独立的应用修复。

## 判定

### Once 授权卡消费缺陷（01:05 只读对账）

精确名称的 trigger 仍为 0 条。第二轮 runtime task `000001a5-0000-4000-8000-000000000000` 与 terminal outbox 已 completed/delivered，但 Session UI 刷新后仍显示运行中、发送禁用、无授权卡。canonical transcript 1264—1268 分别为 tool_call.started、tool_permission.waiting、tool_call.waiting、run.waiting、turn.waiting。`set_trigger` invocation `000002ea-0000-4000-8000-000000000000` 为 `prepared_not_started / waiting`，permission item `000001c8-0000-4000-8000-000000000000`，01:18:27 过期，尚无 result。故直接阻塞是正式工具授权未被消费，不是 scheduler 未触发；不绕过授权、不重复创建。

01:06:56 分派独立 zCode GLM-5.3 候选 `3f8ae98be72943158660015429524416`，session `wrc-functional-b3-once-20260909`，仅修复 sparse V2 permission event 到正式授权卡的 live/reload 消费与等待态；与 Local 默认会话文件所有权分离。保持单次批准、可信版本/hash、现有 endpoint 和无重复执行语义，不导入旧 runtime dirty 候选。限定首版约 20 分钟，最多一次集中返修，仍须本批 deadline 内审查部署实测。

首版返回五文件候选，尚未接受：授权显示用 provider 参数而非实际 effective 参数；历史 sparse 请求没有完整效果/expiry；把所有 waiting 都当 approval，未证明 live phase；且现场 completed RuntimeTask 与现有 `_queue_same_run_continuation` 只接受 running/suspended/resumable 的条件矛盾，不能称纯显示修复已闭环。01:20 唯一集中返修 `deb14c6f40674230a2a3f2c5af6d5ab8`，要求权威读回、真实 live/reload、同 invocation 续跑与相同 permission item 终态一致；若需广泛 runtime 重写则交付明确阻塞，不自动扩大或第三轮。

01:28 唯一返修正常返回七文件候选；主 Codex **未接受**。修订只把权限等待 phase 接入 `hydrateSessionTranscriptEvents`，并没有接入真实 live `applyTranscriptToSessionRuntime`。01:31 在原 live harness 上新增最小只读 reviewer regression：初始 `tool_running`，应用 canonical `run.waiting / reason_code=tool_permission_required`，断言 `awaiting_approval`；实际仍为 `tool_running`，**1 failed / 17 skipped**。该检查不修改产品逻辑；不能用 hydration 测试替代 live 消费证明。候选还明确保留上游 permission pause 未正确挂起的原因，仅在 accept 处恢复部分 completed task；不把此候选报告成 once 功能完成。

按 PDEC-015 停止该策略，不启动第三次实现，不将七文件候选或 reviewer 检查混入主仓提交/部署。原 once 仍无 trigger、无文件/通知交付，不重发；候选保留在隔离 worktree 待 owner 决策。独立已接受的 Local 修复继续发布与复验。

### 本批发布

01:32 起仅发布已提交并推送的 `87b845dba4ae397bd4205b21e657e6efeb9fac7f`。三服务均从该 commit 的干净 `git archive` 提交；backend archive 中目标服务文件 SHA-256 与主审 worktree 一致。release hygiene `git-archive HEAD` 检查 3455 paths 通过。

| 服务 | 部署 ID | 终态 |
|---|---|---|
| backend | `0000048d-0000-4000-8000-000000000000` | SUCCESS |
| backend-api | `0000042e-0000-4000-8000-000000000000` | SUCCESS |
| frontend | `000003d9-0000-4000-8000-000000000000` | SUCCESS |

01:34 public health `ok`、runtime/terminal worker running；frontend HTTP 200。public backend 与 private backend-api 的运行指纹都为 `source-sha256:eb7edcf72abd898a3fed7b9396280c456bd40af5edef51e11a24425efffec577`（1058 files），与 exact archive 一致。CI `34256570667` 截至 01:35 前端与 15 条机械全栈检查通过，backend harness 尚在运行；不把两项成功或部署成功当完整 CI/功能验收。

### 生产消费与本批出口

01:34 刷新正式 `/local-agents`，原 approval_required、approval_resolved、四条本地文本和 delivery_requeued 共 7 条事件恢复可见。随后离开到首页、再打开正式默认入口，原批准记录、退出 0、`WRC-B3-LOCAL-806` 与 requeued 均仍可见。未重新发送命令。部署后 app_rls READ ONLY 对账保持原 message/session ID、7 条原事件、无 result/completed_at；本机 exact message 日志仍仅一次 receive 和一次 turn complete，无第二次执行记录。

| 观察范围 | 本批结果 |
|---|---|
| Local 合成请求、正式单次批准、本地 `pwd` 与文本回传 | 已有真实执行证据；不等于云端终态 |
| 默认页刷新 / 离开重开后恢复原会话 | **PASS，`87b845db` 生产消费已验证** |
| Local 云端终态回执 | **BLOCKED**：已安装 v0.1.7 不发送 result；message 仍 pending/requeued |
| once 草案、排程确认 | 仅草案与确认通过；不能称 trigger 创建 |
| once 创建 / 实际触发 / 文件 / 通知 / 无重复消费 | **BLOCKED / 未执行**：工具授权卡及 live 等待链未闭环，原 trigger 仍 0 条 |
| once 修复候选 | 一个实现 + 唯一返修均未接受；主审 live 回归仍失败，未提交/未部署 |
| 连接器安装 / 离线重连 | 未执行；升级未获新批准，正式安装源 unavailable，按 Skill 暂停安装 |

本批在上限内停止，而非耗尽剩余时间启动第三次方案。代码 `87b845db` 已 commit/push；本文件和唯一当前状态随本批收尾另行 commit/push。保留本批两个合成 Session、Local 原请求与失败现场作为后续恢复证据；未创建 once trigger 或目标文件，故没有删除它们的操作，不宣称完整 cleanup。旧 B1 请求、owner 配置、凭据、无关数据和未接受的旧候选均保留。无新增 Goal、heartbeat 或下一批授权。

**本批收尾，部分交付、两项阻塞保留；不是 Local 与 once 全部通过。** 下一步须由 owner 选择 once 新策略，以及连接器的明确升级授权/批准来源；不自动续作。最终 NPTCR 仍为 0/96，未新增完整 Journey PASS。
