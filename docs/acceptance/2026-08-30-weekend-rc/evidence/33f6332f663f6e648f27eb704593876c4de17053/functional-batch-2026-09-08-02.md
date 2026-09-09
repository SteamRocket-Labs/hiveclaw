---
document_id: weekend-rc-2026-09-08-functional-batch-02
owner: Codex
status: completed
authority: bounded-production-functional-evidence
last_reviewed: 2026-09-08
source_commit: aeaaacb59704ac7631da553c97d699c2cc87bdb4
verification_status: bounded-input-recovery-and-consumption-verified
disclosure: public-redacted
---
# 第二批：普通第二轮输入恢复

[当前状态](../../03-current-status.md) · [第一批证据](../17f073bb4f07098e55d9ef1684781dc67cfa454e/functional-batch-2026-09-08-01.md)

当前摘要：**本次有限恢复范围通过并交付**。晚间续接已发布显式管理员恢复 UI，两条旧输入均自然执行完成；fresh 文件/HR 多轮与旧文件交付通过，旧 HR 替代草案刷新及拒绝通过。owner 授权的单次 backend 重启恢复页面可用性，最后 HR boundary 于 23:11:09 delivered，摘要 sealed 至 seq 896。下文保留下午拒绝方案及晚间恢复的时间线，不把下午“未恢复”当作当前状态；不宣称 runtime 根因已修复或完整 96 旅程通过。

## 范围与出口

- owner 明确要求先 commit/push，再开始下一轮且轮末有文档。已完成 `f4575b0f40a10fe950455c1b0038ec5ab71742a2` 推送，17 文件仅验收文档/证据/对应文档检查；10 项文档测试、Ruff check/format 与 frozen 96 manifest validation 通过。旧未接受应用候选原样保留，未提交。
- 本批次时间 **2026-09-08 13:38:12—15:38:12（Asia/Shanghai）**；聚焦文件续写、HR 修订被旧 terminal boundary 阻塞的共同断点。zCode GLM-5.3 实现，Codex 审查、集成、部署和真实复验；一个实现方案、一次集中返修，不恢复无限 Goal/heartbeat，不自动开启第三批。
- 生产应用起点为 `33f6332f`；`f4575b0f` 是文档/测试提交，不是新应用发布。96 条分母与最终 D/E 门不变，起始最终 NPTCR=0/96。
- 下午首轮已在上限前收束：一个方案及唯一返修均未通过，不自动消耗余下时间生成第三版。当时结果是定位共同阻塞、排除两种不安全修复，并交付恢复策略选择；**当时没有恢复文件续写或 HR 修订，没有新增 Journey PASS**。随后 owner 明确授权晚间续接，见后文。

## 合成效果登记

复用第一批已登记的两个隔离 Session，不读取真实业务内容，不新增员工/外发/权限变更，不重发原输入或直接修改 DB/outbox。必要恢复仅通过经过认证、受支持的产品/控制面入口；源码小修和同源部署沿用已授予的本任务权限。

| 目标 | 已有输入与预期 | 下午首轮快照 / cleanup |
|---|---|---|
| 文件 Session `00000079-0000-4000-8000-000000000000` | admission `00000188-0000-4000-8000-000000000000`；原 `workspace/WRC-FUNCTIONAL-B1-20260908/file-check.md` 续写 | 本批只读复核仍 pending / boundary dead_letter；未恢复，保留现场 |
| HR Session `000000a8-0000-4000-8000-000000000000` | admission `00000004-0000-4000-8000-000000000000`；仅蓝图修订，不 provision | 本批只读复核仍 pending / boundary dead_letter；原蓝图已拒绝，未恢复，保留现场 |

## 下午首轮证据与结果

13:42—13:46 通过现有 Railway SSH 对 exact 实验 tenant 做 `app_rls`、`READ ONLY` 事务并 rollback，只读两条已登记 Session 的机械状态。脚本 `tmp/wrc-functional-b1-20260908/inspect_followup_receipts.py`；无 DB/outbox 修改、无重放输入、无 provider 重试。

| 事实 | 文件 Session | HR Session |
|---|---|---|
| 原 admission | admitted / dispatch pending，attempts 1，无 lease/error | admitted / dispatch pending，attempts 2，无 lease/error |
| 原 boundary | dead_letter / attempt 8，delivered_at null | dead_letter / attempt 8，delivered_at null |
| transcript 投影 | 198/198 projected，seq 1—198，无 failed | 366/366 projected，seq 1—366，无 failed |
| 摘要投影 | needs_reconciliation / LLMError / attempt 1；terminal seq 191 | needs_reconciliation / LLMError / attempt 1；terminal seq 359 |
| summary_through_sequence | null | null |

实时 tenant `memory_config.summary_model_id` 指向 enabled `deepseek-v4-flash`（ID `00000062-0000-4000-8000-000000000000`）；这不证明当时请求的实际 HTTP 状态，也不证明当前 billing readiness。源码 `_get_summary_model_config` 优先使用这一显式 tenant 设置；首轮 GLM/MiniMax 正常不代表后台摘要采用同一模型。旧部署按时间窗查摘要错误日志返回空，不能据空日志补写原始错误详情。

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

正式入口为 `POST /api/runtime-terminal-boundaries/{boundary_id}/redrive`，`summary_disposition=retry`，须匹配 authenticated tenant；文件 boundary `000001a8-0000-4000-8000-000000000000`、HR boundary `0000013c-0000-4000-8000-000000000000`。当前尚未取得此 exact tenant 的受支持 operator API 认证，前端也未发现该恢复按钮；不能提取浏览器 token 或用另一 fixture 的身份代替。若不能从现有支持入口完成，明确报告入口缺口，另获范围决定，不手改 DB。

未获 owner 配置/风险决定前暂停该效果。完整 96 旅程、真实故障恢复和最终 cleanup 仍未完成；本批不替代最终验收。

## 批后更新：owner 已修改模型，旧任务尚未恢复

2026-09-08 16:17—16:18（Asia/Shanghai），owner 告知“改完了已经”后，Codex 用同一 exact tenant / app_rls / READ ONLY 查询核对：摘要模型已为 enabled `zhipu / glm-5.3`，ID `00000354-0000-4000-8000-000000000000`。两条原摘要仍 needs_reconciliation / LLMError / attempt 1，summary watermark null；两条 boundary 仍 dead_letter / attempt 8，后续 admissions 仍 pending。修改配置不自动恢复历史死信，本次无新 provider call、redrive 或输入重发。首次 SSH 连接关闭，第二次只读查询成功；浏览器控制初始化超时，没有取得新的 UI 证据。

剩余断点是已有恢复 API 缺少前端入口，以及当前缺 exact tenant 的正式 operator API 登录态；不绕过认证。此更新不是自动开启第三批，也不改变两版候选拒绝结论。生产 health 返回 ok，运行源码 hash 仍 `510f6eb45fdb671eb4cf4852bbc5e49d0f3a7322ade18098d240b2577ec2620c`。

收尾提交 `085dc47c` 的 CI `34193585606` 已结束为 failure：Backend harness 和 Frontend gates success；atomic full-stack journeys 为 5 passed / 1 failed / 1 flaky / 8 did not run。失败 J-07 等待个人知识 ingest job completed 超过 90 秒；J-01 的重试 bootstrap 曾在 auth/register 超过 120 秒。仅确定失败观察，尚未查明根因；不标作环境故障或自动 rerun，不由文档提交推断应用回归。

## Owner 明确续接：显式恢复入口与第二批复测

owner 随后提供一个新 HR 问候会话的成功截图，并要求“你再试一下，然后把第二批完成……之前的先 commit，然后 git push”。旧两份核对记录已单独 commit/push `ade50f740a4d142d96d16ad51362e59f1752b4a9`；exact archive 发布标识检查通过。截图只证明新会话问候成功，不证明旧摘要或第二轮输入恢复。

本次续接从 **2026-09-08 22:02:49 至 2026-09-09 00:02:49（Asia/Shanghai）**，包括必要修正、部署、业务复验和文档。22:00 左右新只读对账确认同一 GLM 设置，但两条旧摘要、admissions、boundaries 与下午状态相同；真实浏览器旧文件 Session 显示“处理中 666m”，原 artifact 仍 44 B，正式运行面板无恢复按钮。

选择此前已提出的不同策略：补齐既有 `runtime-terminal-boundaries` API 的最小管理员产品入口，由管理员明确确认摘要重算及可能重复调用成本；不让平台自动推断全摘要可安全重放。zCode GLM-5.3 author `72a0e2f692614d37bb930dd523e00edd`，工作面 `/private/tmp/hiveclaw-b2-operator.amxGjh`，base `ade50f74`，22:04:53 派发；主 Codex 仍负责 Review/集成/部署/E2E。旧两版自动重试实现保持拒绝，不改名重置其预算。

补充合成效果预登记：`WRC-FUNCTIONAL-B2R-20260908`，允许在既有实验公司与已授权 Agent 下新建隔离 Session，创建/续写唯一新 workspace Markdown 文件，并生成/修订 HR 蓝图草案；不 provision 新员工、不检索真实业务内容、不外发、不覆盖旧文件或重发旧已接受输入。保留精确 Session/artifact/blueprint 与 cleanup 状态，fresh 正常结果和旧任务恢复分开记录。本次继续不是授权无限后台执行或下一批。

### Fresh 正常链路复验（仍为 33f6332f 应用）

22:07—22:20，从正式浏览器同一已认证实验公司入口新建两个合成 Session；模型均显示 GLM-5.3。未重复发送输入、未执行 operator redrive。

| 消费路径 | 实际结果与精确定位 | 边界 / cleanup |
|---|---|---|
| 文件创建、同 Session 续写 | Session `000000b3-0000-4000-8000-000000000000`；创建 `workspace/WRC-FUNCTIONAL-B2R-20260908/file-check.md`，49 B，artifact `000002ed-0000-4000-8000-000000000000`；续写第三行 `Version marker: WRC-B2R-V2-683` 后 80 B，artifact `0000046b-0000-4000-8000-000000000000`。真实 write/read、edit/read；预览保留原两行且出现第三行，刷新重开仍正确 | 文件与两版 immutable artifact 留证；未覆盖旧 B1 文件。V2 正式下载动作的 artifact download API 为 HTTP 200 / text/markdown；未取得本机落盘证据，不冒称该层通过 |
| HR 蓝图、正式“要求修改” | Session `00000278-0000-4000-8000-000000000000`；合成名称 `WRC-B2R-Review-682`，首版三要点，要求修改后新预览的交付物和首任务均为五要点；名称、仅自己可见及全部访问限制保留。22:20 刷新后两版预览仍可见，最终 UI 为完成。只读确认同一 draft `000001a6-0000-4000-8000-000000000000` v2/hash `bp_f3517c6503db48a300995e4a`，provisioning_task_id / created_agent_id 均 null | 22:23 经正式“拒绝”按钮提交一次，随后 UI 两版均显示已拒绝，Session 留证；无确认创建、provision 或外发 |

文件首轮 UI 1m05s/6 步，第二轮 3m19s/7 步；HR 首轮 3m26s/6 步，修改轮 6m06s/3 步（UI 时长包含等待）。14:14 UTC 只读回执确认文件原首轮 boundary 已自然 delivered、后续 admission 从 pending 自然 dispatched；HR 修改当时仍等待首轮 summary，随后才实际生成五要点预览。这证明配置变更后 fresh 链路能自然跨轮推进，不把数分钟等待误报永久死锁，也不证明旧 dead_letter 已恢复。

### 显式恢复 UI 审查

作者 `72a0e2f692614d37bb930dd523e00edd` 610.039 秒正常结束，交回六文件 frontend 候选，无 backend 变更。入口是 platform-admin 的 `/admin/platform-settings` Dashboard 中既有运行对账下方；org_admin 的 backend operator 权限不变，本切片未提供 org_admin UI 入口。作者报告 7 项新 mounted、64 项相邻/i18n、TypeScript、build 和 7 项既有 backend API 合同检查通过；这些是本地检查，不代表生产消费。

Codex 完整阅读变更、真实 HTTP adapter、selected-company 服务端校验、redrive/summary 消费及 route/确认组件。发现成功空队列后刷新失败仍保留旧空结论、mutation 成功后 reload 失败仍保留旧可操作行；22:19:52 发唯一集中返修 `e259e06cb3fc4d2ca72d22751956de67`，要求失效旧 loaded 状态但保留真实 requeued receipt，并加两条精确事件检查。另对齐服务端审计原因 1000 字符上限；不扩大后端/自动重试范围。

返修 106.812 秒正常结束并被接受。Codex 只集成上述六文件到主工作面，未纳入旧 backend dirty 候选；应用 commit/push `aeaaacb59704ac7631da553c97d699c2cc87bdb4`。exact staged archive hygiene 3454 paths 通过；主工作面独立检查 `src/pages/admin-companies src/i18n src/App.routes.test.ts` 共 10 files / 70 tests 通过，i18n 9 tests 与全部 gates=0，TypeScript/build/bundle budgets 通过。第一次裸 Node 26.8.1 运行的 31 项 mounted 在 `localStorage.clear is not a function` 处失败，连未改 sibling 都受影响；只在命令环境使用 `NODE_OPTIONS=--no-experimental-webstorage` 后同一套 70 项通过，未改应用/断言以适配该本机环境差异。

两次原生 model I/O 均按本次 mission 与时间核对：初版 68、返修 11 次记录，configured/wire `GLM-5.3`、response `glm-5.3`。作者原始 usage 合计 input 10,056,352（其中 cache-read 9,899,072）、output 24,150、total 10,080,502，cache-write 0；输入含跨调用重复读取的上下文，不等于独立新增文本或可由此直接推出的账单。主 Codex token 不可获得，以上不冒充任务总 token。新 CI `34238046718` 已开始；上一文档 CI `34235352724` 于 22:34 核对三 job 全部 success。

### 同源部署与原输入恢复（aeaaacb5）

22:26 从 exact committed archive 上传三服务，没有部署工作面 dirty 文件，backend tree 与 `33f6332f` 完全一致。22:30 核对三服务均 SUCCESS：backend `00000425-0000-4000-8000-000000000000`、backend-api `0000036e-0000-4000-8000-000000000000`、frontend `000000a9-0000-4000-8000-000000000000`。backend health `ok`、build hash `510f6eb45fdb671eb4cf4852bbc5e49d0f3a7322ade18098d240b2577ec2620c`，frontend HTTP 200。启动期间曾有 backend 502 / 页面 GET 504；当时没有提交 mutation，服务就绪后正式刷新读到三条死信。此时 CI 前端及 15 全栈旅程均 success，backend full suite 尚在执行；不是最终 RC 发布声明。

22:30—22:32，从 `/admin/platform-settings` 的“终态边界恢复”，核对自动读取的 selected-company echo 和两条 exact Session/boundary/task；分别填写审计原因、勾选重算终态摘要、阅读包含未知原始投递状态及可能重复子调用/费用的确认框，然后每条只点一次确认。

- 文件 boundary `000001a8-0000-4000-8000-000000000000`：正式页面返回 requeued / pending；原死信行从列表移除。
- HR boundary `0000013c-0000-4000-8000-000000000000`：同样返回 requeued / pending；列表只剩未操作的第三条死信。
- 原输入没有重发、DB/outbox 没有手改、未假写 delivered、未创建员工；仅执行已授权的两次显式恢复。最终摘要、dispatch 和业务结果仍待消费者证据，不以 pending 回执宣告成功。

22:33 只读审计核对恰好两条恢复记录：文件 audit `0000006e-0000-4000-8000-000000000000`（22:30:43）、HR audit `00000076-0000-4000-8000-000000000000`（22:31:36），均 `summary_disposition=retry`、previous attempts 8。文件旧摘要已 sealed 至 seq 191，原 boundary attempt 9 于 22:31:44 delivered；原 admission `00000188-0000-4000-8000-000000000000` 自然 dispatched 到新 runtime task `000003eb-0000-4000-8000-000000000000`。随后浏览器显示该输入真正 read/edit；HR 原修改也开始运行。UI 的 699/704 分钟包含上午提交以来的排队等待，不是此次 provider 执行时长。

22:33 旧文件输入完成，真实 read → 一次 edit → read，产出 74 B 新 artifact `000003e3-0000-4000-8000-000000000000`（22:33:14），保留原 artifact `00000059-0000-4000-8000-000000000000`。正式预览含原标题/标记和新增 `Version marker: WRC-B1-V2-573`；新 artifact 正式下载返回 HTTP 200 / text/markdown。未把 Agent 自述当成唯一内容证据，也未声称本机下载落盘已验证。

HR 原草案 `00000410-0000-4000-8000-000000000000` 已在上午 11:15 被拒绝，本来就不能原位变 v2。恢复后的 Agent 遇到 typed `immutable` 拒绝，明确解释原因，保留原记录并通过正式 `preview_agent_blueprint` 返回替代草案；新预览保留名称、仅自己可见和所有边界，首任务明确 cobalt folders 12 + amber folders 7 = folders 19，另有 label sheets 2，all physical items 21，标签纸不是文件夹。该结果是可恢复的替代草案，不冒称已拒绝草案被原位修改，也不执行 provision。

22:37 只读确认：旧 HR boundary attempt 9 已在 22:33:16 delivered；原 admission `00000004-0000-4000-8000-000000000000` 自然 dispatched，runtime task `00000371-0000-4000-8000-000000000000` 已 completed。替代 draft `00000267-0000-4000-8000-000000000000` v1/hash `bp_67256bdd91b03db397752a0d` 为 awaiting_confirmation，provisioning_task_id / created_agent_id 均 null。文件恢复轮的新 boundary `0000024f-0000-4000-8000-000000000000` 已 delivered，summary watermark 384；HR 恢复轮的新 boundary `0000013a-0000-4000-8000-000000000000` 当时仍 processing，summary watermark 尚为上一轮 359。

### 收尾出现 runtime 服务不响应，不能冒称完整通过

旧文件在刷新后重开快照仍正确。HR 22:36 的新预览完成后，刷新页面长时间停在“正在加载持久会话历史”；一次带请求观察的正式 reload 显示 runtime 路径的 transcript、permissions、workbench、context-usage、runtime-summary 等返回 504，而 auth/me、agents list、sessions list 和 active-run 的 API-plane 路径返回 200。没有通过重发原输入修复页面。

22:40—22:45 有界只读诊断：公共 backend health 20 秒无字节超时；容器内本机 health 与 OpenAPI 请求分别 5 秒超时，说明不能仅归为浏览器或公网代理问题。`uvicorn` PID 22 存在、state S，两个独立样本 CPU ticks 均 23539 / 样本增量 0；这不定位具体 Python 栈或根因。数据库 own-role pg_stat_activity 可读取，样本全部 blocking_pids 为空、未见锁等待；不据此排除所有数据库/客户端问题。Railway 仍显示 deployment SUCCESS，不能据该标记否認 runtime 不响应。

backend-api 的运行 source hash 已独立 SSH 核对，与 backend 及 exact archive 的 1058 文件 hash 一致。本次 backend 源码没有变化；尚无证据把此次不响应归因于新前端恢复 UI，也不能默认叫环境波动。此时替代 HR 草案未拒绝，HR 刷新和最新 summary/boundary 尚未收尾。

已请求 owner 明确授权仅重启 backend 一次（不改源码/模型/数据，可能中断其他活跃任务，先核对），等待决定。未重启、未再次 redrive、未新增实现任务；第二批保持 in_progress，不把两条旧输入已经执行扩大成完整验收通过。

### Owner 授权的单次 backend 重启

23:01 owner 明确回复“授权”。23:03 经 backend-api 的 app_rls / READ ONLY / exact tenant 核对：验收租户没有 running/pending 任务，原两条目标任务均 completed；HR 最新 boundary 仍 processing / attempt 1、summary watermark 359。其他租户活动未跨权限核验。公共 health 再次 8 秒零字节超时；一次 backend SSH 连接关闭，未产生业务效果。

23:04:18 仅对 production backend service `0000018a-0000-4000-8000-000000000000` 执行一次受支持 Railway restart，命令 exit 0，返回原 deployment `00000425-0000-4000-8000-000000000000`。没有 rebuild、源码/模型变更、DB 手改、输入重发或再次 redrive；backend-api 与 frontend 未重启。刚提交后 health 502 属于重启期间观察，最终恢复待核对。

应用 commit `aeaaacb5` 的 CI `34238046718` 此时已结束，Backend harness、Frontend gates、15 全栈旅程三个 job 全部 success；不扩大成 96 条验收通过。

23:05 health 恢复 `ok`、source hash 不变；worker 在启动恢复 gate 释放后于 23:07 可见 running，terminal boundary worker 也 running。23:08 health 显示已自然 claimed/delivered 15 条回执，event-loop 当前 lag 1.03ms；另有 `trigger exception terminal transaction did not commit` 的 worker last_error，未在本批定位，不宣称平台所有后台工作健康。

HR 正式刷新后重新加载完整两轮历史，最新预览的 12/7/19/2/21 和全部边界正确。23:05:50 经最新替代草案“拒绝”按钮提交一次；随后 UI 和 app_rls 只读查询均确认 `00000267-0000-4000-8000-000000000000` rejected，provisioning_task_id / created_agent_id 仍 null，原草案拒绝时间未改。再刷新后两张卡均显示已拒绝；未创建员工。HR 最新 boundary 被原生租约回收后 attempt 1→2，仍等待自然终态消费完成，没有再次 operator redrive。

### 最终对账与本批出口

23:13 后返回的 app_rls / READ ONLY / exact tenant 查询确认：HR boundary `0000013a-0000-4000-8000-000000000000` attempt 2 已于 **23:11:09.345735** delivered；摘要 state sealed / attempt 3，watermark 与 terminal_sequence 均 896，result SHA-256 `3103f105ac544afb245a9e183a1c1f6beb130596dc497cc5a9af8ffa3748fc18`。文件 summary watermark 384 不变。两个旧 Session 的四条 boundary 全 delivered、lease 清空、四个对应 RuntimeTask 全 completed，全部 384/896 个 transcript events 已 projected、无 failed；两条原 admissions 仍各自 dispatched，operator audit 恰好两条，没有再次提交输入或恢复请求。重启后旧文件页面再次刷新，74 B 快照重开仍含原两行和 V2 marker。

| 本批范围 | 最终结论 |
|---|---|
| 显式管理员恢复入口 | 已审查、测试、commit/push、同源部署，并真实恢复两条旧死信 |
| fresh 文件 / HR 普通多轮 | 已通过；HR 同一草案 v2、拒绝与无 provision 有证据 |
| 原文件第二轮输入 | 已完成实际续写、预览、下载 HTTP 200、刷新与最终摘要/回执 |
| 原 HR 第二轮输入 | 已完成正确替代草案、刷新、拒绝及最终摘要/回执；原已拒绝草案不冒称原位 v2 |
| runtime 可用性 | 单次授权重启后恢复；**不响应根因未定位/修复**。另观察到 trigger 终态事务及 stale worker fence 错误，未扩入本批修正 |
| 全量验收 / cleanup | 最终 NPTCR 仍 0/96；全部 96 条 D/E、完整故障恢复、角色与格式覆盖、rollback 未完成。两组 Session/文件与不可变 artifact 留证，HR 草案已拒绝，未创建员工；完整 cleanup 未声明 |

本轮不再启动代码修订、重启、redrive 或下一批。Ponytail 的最小实现原则体现为复用现有受支持 API，仅补前端消费入口；Task State 沿用既有当前状态/证据文档，未新建平行验收账本。
