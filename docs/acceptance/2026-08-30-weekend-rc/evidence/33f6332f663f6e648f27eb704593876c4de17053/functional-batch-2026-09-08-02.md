---
document_id: weekend-rc-2026-09-08-functional-batch-02
owner: Codex
status: completed
authority: bounded-production-functional-evidence
last_reviewed: 2026-09-08
source_commit: 33f6332f663f6e648f27eb704593876c4de17053
verification_status: blocked-recovery-two-candidates-rejected-no-deployment
---
# 第二批：普通第二轮输入恢复

[当前状态](../../03-current-status.md) · [第一批证据](../17f073bb4f07098e55d9ef1684781dc67cfa454e/functional-batch-2026-09-08-01.md)

## 范围与出口

- owner 明确要求先 commit/push，再开始下一轮且轮末有文档。已完成 `f4575b0f40a10fe950455c1b0038ec5ab71742a2` 推送，17 文件仅验收文档/证据/对应文档检查；10 项文档测试、Ruff check/format 与 frozen 96 manifest validation 通过。旧未接受应用候选原样保留，未提交。
- 本批次时间 **2026-09-08 13:38:12—15:38:12（Asia/Shanghai）**；聚焦文件续写、HR 修订被旧 terminal boundary 阻塞的共同断点。zCode GLM-5.3 实现，Codex 审查、集成、部署和真实复验；一个实现方案、一次集中返修，不恢复无限 Goal/heartbeat，不自动开启第三批。
- 生产应用起点为 `33f6332f`；`f4575b0f` 是文档/测试提交，不是新应用发布。96 条分母与最终 D/E 门不变，起始最终 NPTCR=0/96。
- 本批已在上限前收束：一个方案及唯一返修均未通过，不自动消耗余下时间生成第三版。结果是定位共同阻塞、排除两种不安全修复，并交付恢复策略选择；**没有恢复文件续写或 HR 修订，没有新增 Journey PASS**。`completed` 仅指本批次交付结束。

## 合成效果登记

复用第一批已登记的两个隔离 Session，不读取真实业务内容，不新增员工/外发/权限变更，不重发原输入或直接修改 DB/outbox。必要恢复仅通过经过认证、受支持的产品/控制面入口；源码小修和同源部署沿用已授予的本任务权限。

| 目标 | 已有输入与预期 | 当前状态 / cleanup |
|---|---|---|
| 文件 Session `18911e1a-efff-4f49-b304-c7c3e93a30d7` | admission `4e79304d-f5cf-5f92-bf51-8f1327aefdfb`；原 `workspace/WRC-FUNCTIONAL-B1-20260908/file-check.md` 续写 | 本批只读复核仍 pending / boundary dead_letter；未恢复，保留现场 |
| HR Session `22522912-a9b4-4a8a-98b2-5082d31b997c` | admission `0049d4ed-5c32-5aa1-9b3a-3abf577c9f31`；仅蓝图修订，不 provision | 本批只读复核仍 pending / boundary dead_letter；原蓝图已拒绝，未恢复，保留现场 |

## 当前证据与结果

13:42—13:46 通过现有 Railway SSH 对 exact 实验 tenant 做 `app_rls`、`READ ONLY` 事务并 rollback，只读两条已登记 Session 的机械状态。脚本 `tmp/wrc-functional-b1-20260908/inspect_followup_receipts.py`；无 DB/outbox 修改、无重放输入、无 provider 重试。

| 事实 | 文件 Session | HR Session |
|---|---|---|
| 原 admission | admitted / dispatch pending，attempts 1，无 lease/error | admitted / dispatch pending，attempts 2，无 lease/error |
| 原 boundary | dead_letter / attempt 8，delivered_at null | dead_letter / attempt 8，delivered_at null |
| transcript 投影 | 198/198 projected，seq 1—198，无 failed | 366/366 projected，seq 1—366，无 failed |
| 摘要投影 | needs_reconciliation / LLMError / attempt 1；terminal seq 191 | needs_reconciliation / LLMError / attempt 1；terminal seq 359 |
| summary_through_sequence | null | null |

实时 tenant `memory_config.summary_model_id` 指向 enabled `deepseek-v4-flash`（ID `13157038-87da-48bb-8d48-f3e354ac473e`）；这不证明当时请求的实际 HTTP 状态，也不证明当前 billing readiness。源码 `_get_summary_model_config` 优先使用这一显式 tenant 设置；首轮 GLM/MiniMax 正常不代表后台摘要采用同一模型。旧部署按时间窗查摘要错误日志返回空，不能据空日志补写原始错误详情。

判断：阻塞已定位到 post-terminal summary 的 reconciliation 状态，不是 transcript 未投影、未 enqueue 或 provider 正在执行第二轮。尚未证明哪一种 LLMError，也未取得恢复后业务结果。现有受支持 operator redrive API 需要 exact boundary 和明确 summary retry disposition；未执行前先核对实际可恢复条件，不自动改全租户模型。

zCode 首次只读追踪 `4f537fa4f63e4f41b7064bb08c398d0a` 在约 61 秒时由 Codex 中断以补充上述新证据，worktree 仍 clean；同一 native Session 继续 `af7311e347a44099a12ac81c9071b6d0`，同一方案与原批次截止不变，不以 steering 重置返修预算。

## 第一版审查与集中返修

- `af7311e347a44099a12ac81c9071b6d0` 529.833 秒正常结束，候选四文件（两应用、两测试），未提交/部署。作者报告 47 项 PG-backed、138 项相邻 suite、17 项 ingress 检查通过；Codex 核实 fixture 确为 PostgreSQL Testcontainers 链，但这些绿不代表生产恢复。
- Codex 不接受两点：`LLMError` 类名不能证明历史请求的 delivery_state；将所有 `WebTerminalBoundaryPending`（含无关 T0/seal）及 400/401/402 等明确拒绝改成无限 30 秒 provider 重试，会增加资源消耗且没有恢复所需的新条件。已要求保留原有限预算，区分未知结果、需配置/运营动作的明确拒绝、可有界重试的 429，并保存安全 typed 状态而非 raw 错误体。
- 唯一集中返修 `5188bd49d93d46d3aec6d51389d7b179` 于 13:54:45 开始。主 Codex 未另写应用补丁；不再自动发第三次同类任务。
- 13:48 重新打开原文件 Session，当前正式 UI 登录为已授权平台管理员；仍显示“处理中 174m / 思考中”，只有原 44 B artifact 和一条待处理 V2 输入。不是员工证据；未重发输入。
- 实验公司摘要模型调整不只影响这两个合成 Session：`_get_summary_model_config` 还被 memory extraction、compaction、T2、AutoDream 等后台记忆路径复用。已补充这一扩大影响并请求 owner 明确授权；尚未收到决定，不改配置、不做摘要 redrive。主对话模型/凭据不在本次调整请求内。

## 唯一返修结果：仍不接受

`5188bd49d93d46d3aec6d51389d7b179` 于约 14:01 结束，耗时 385.221 秒。返修完整撤回 outbox 源码变更，只保留 processor 和两项测试文件（298 insertions / 1 deletion）。它保留原有限 outbox 预算，把 typed `rejected / HTTP 429` 记为 `retryable`，其他拒绝或未知结果保留 `needs_reconciliation`；作者报告 47 项 PG-backed、155 项相邻 suite、17 项 ingress 以及 Ruff 通过。主 Codex 阅读完整 diff，但未为已拒绝候选重复全套测试；作者自测不冒充独立验收。

新的阻断有可达调用路径与本地复现依据，不是增加抽象要求：真实 `conversation_summarizer._llm_summarize` 对长输入执行多次 map/reduce provider 调用。若第一段成功、第二段明确 429，返修把整个摘要标为可重试；下一次从第一段重新开始，重复已成功的 provider 工作。单次 HTTP 的 `rejected` 不能证明整个多调用摘要尚无成功效果。

Codex 本地诊断直接调用真实 `_llm_summarize`，仅替换分段输入与 provider client：两段输入，第一段成功，第二段仅首次抛出 typed 429，随后按候选决策重跑整个摘要。实际输出：

```text
candidate_failure_state: retryable
accepted_phase_counts:
  map_chunk_1_of_2: 2
  map_chunk_2_of_2: 1
  reduce_level_1_group_1_of_1: 1
external_calls: 0
```

复现证明潜在重复调用/费用，不宣称已经观察到真实重复计费。原两条摘要的实际 HTTP/delivery 状态仍未知，不能由这个合成 429 反推历史错误。受保护性质是现有资源与外部效果边界；不接受以 whole-summary retry 重放已成功子调用。

本地复核定位（未进入应用提交，临时目录不是永久证据存储）：

- 候选工作面 `/private/tmp/hiveclaw-functional-b2.P5urXk`，base `f4575b0f`；processor SHA-256 `82ec58cb793e698a8b1a22587ce83c075a4135864237fbc2ca729cf4464a1d2a`。
- 诊断脚本 `tmp/wrc-functional-b1-20260908/check_b2_partial_summary_replay.py`，SHA-256 `b765c4e04c82c5eb8f0f12c35036c8adb1e8b79bc4b4f1122a59479fb0597297`。在仓库根目录运行 `PYTHONPATH=/private/tmp/hiveclaw-functional-b2.P5urXk/backend /private/tmp/hiveclaw-sa01-staged.5EpyKi/backend/.venv/bin/python tmp/wrc-functional-b1-20260908/check_b2_partial_summary_replay.py`，exit 0；assert 确认已成功首段被再次调用。无需凭据、DB 或网络。
- 原生模型 I/O 已按 mission/request 匹配核对：初版与返修的 configured/wire 模型均为 `GLM-5.3`、响应模型 `glm-5.3`；不是只从 Session 标题推断。作者任务正常结束不代表业务验收。

没有集成、提交或部署这两版应用候选，没有第三次实现任务；主工作面原有未接受修改也未动。

## 文档推送补正

`f4575b0f` 自动 CI `34191368737` 失败于 committed-archive 发布标识检查：B1 证据含本机用户名/设备名。已修正文档说明为不含个人标识的描述，保留文件名、UUID 和 hash；单文件补正 commit/push `cb527dda`。其 exact archive hygiene 与 10 项文档结构检查本地均通过；14:06 核对 CI `34192210086`，前端与 15 条全栈旅程 job success，backend harness full suite 仍 in_progress，不能称全绿。没有修改检查规则、改写 Git 历史或重新部署应用；原提交仍在历史中。本问题不隐瞒为 CI 环境故障。

## 投入与效果

- 应用作者一次短追踪被新证据 steering 中断、一个实现、一次返修，共 3 次派发；已结束的两个实现任务合计 915.054 秒，短追踪约 61 秒。未重新启动无限任务或自动第三批。
- 主 Codex 完成 exact tenant 只读对账、历史日志有界查询、UI 复核、完整候选审查和一个零外部调用诊断。主会话与作者合并 token 总数不可获得，不以账户额度变化估算。
- 本批新生产部署 0、摘要模型变更 0、摘要 redrive 0、重复输入提交 0；只有本任务文档与对应检查的 Git 推送。业务新增完整通过 0，最终 NPTCR 仍为 0/96。
- 文档收尾：10 项现有文档结构/链接检查通过（0.44 秒）；本批只暂存 4 份文档，staged diff check 与 exact staged archive 的发布标识检查通过。未因文档变更重跑已拒绝应用候选的全套测试。

## 未完成与下一动作

建议先采用现有受支持的操作恢复路径，不再给失败重试方案加第三版：owner 决定是否允许把实验公司的共享摘要/后台记忆模型改为已实际工作过的 GLM-5.3，保留原设置作为 rollback；取得 exact tenant 的正式 operator 认证，并确认当前 readiness 后，再明确授权重算原两条摘要。恢复可能重复此前已成功的摘要子调用，必须明确这一费用风险；不能假称幂等、跳过消费者或伪造 delivered。

正式入口为 `POST /api/runtime-terminal-boundaries/{boundary_id}/redrive`，`summary_disposition=retry`，须匹配 authenticated tenant；文件 boundary `55c4549c-7384-5d63-a626-d5843bc8da53`、HR boundary `3de38b11-efb9-5478-bae2-8d95a4e7bf25`。当前尚未取得此 exact tenant 的受支持 operator API 认证，前端也未发现该恢复按钮；不能提取浏览器 token 或用另一 fixture 的身份代替。若不能从现有支持入口完成，明确报告入口缺口，另获范围决定，不手改 DB。

未获 owner 配置/风险决定前暂停该效果。完整 96 旅程、真实故障恢复和最终 cleanup 仍未完成；本批不替代最终验收。

## 批后更新：owner 已修改模型，旧任务尚未恢复

2026-09-08 16:17—16:18（Asia/Shanghai），owner 告知“改完了已经”后，Codex 用同一 exact tenant / app_rls / READ ONLY 查询核对：摘要模型已为 enabled `zhipu / glm-5.3`，ID `ae56afc0-f3d4-4021-96b3-158ebe766cab`。两条原摘要仍 needs_reconciliation / LLMError / attempt 1，summary watermark null；两条 boundary 仍 dead_letter / attempt 8，后续 admissions 仍 pending。修改配置不自动恢复历史死信，本次无新 provider call、redrive 或输入重发。首次 SSH 连接关闭，第二次只读查询成功；浏览器控制初始化超时，没有取得新的 UI 证据。

剩余断点是已有恢复 API 缺少前端入口，以及当前缺 exact tenant 的正式 operator API 登录态；不绕过认证。此更新不是自动开启第三批，也不改变两版候选拒绝结论。生产 health 返回 ok，运行源码 hash 仍 `510f6eb45fdb671eb4cf4852bbc5e49d0f3a7322ade18098d240b2577ec2620c`。

收尾提交 `085dc47c` 的 CI `34193585606` 已结束为 failure：Backend harness 和 Frontend gates success；atomic full-stack journeys 为 5 passed / 1 failed / 1 flaky / 8 did not run。失败 J-07 等待个人知识 ingest job completed 超过 90 秒；J-01 的重试 bootstrap 曾在 auth/register 超过 120 秒。仅确定失败观察，尚未查明根因；不标作环境故障或自动 rerun，不由文档提交推断应用回归。
