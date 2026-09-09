---
document_id: weekend-rc-2026-09-08-functional-batch-01
owner: Codex
status: completed
authority: bounded-production-functional-evidence
last_reviewed: 2026-09-08
source_commit: 33f6332f663f6e648f27eb704593876c4de17053
verification_status: partial-results-not-full-journey-acceptance
disclosure: public-redacted
---

# 功能试批次 B1：2026-09-08

- 执行窗口：10:16:24—12:16:24 Asia/Shanghai（02:16:24—04:16:24 UTC），包括准备和交付。
- 当前状态：bounded batch closed with partial results；最后业务复查 12:07，12:16:24 前交付，不自动续作。操作者：Codex；zCode 完成一个工具加载小修复及一次集中返修，Codex 复核后于 11:14 提交/推送 `33f6332f`，CI 通过并三服务同源部署；未混入旧恢复候选。
- 起始生产版本：`17f073bb4f07098e55d9ef1684781dc67cfa454e`。10:20 实时三服务部署 SUCCESS；backend health HTTP 200，build source SHA256 `8858ddcabb44fd055a0bb37f391b0a8e3127fc48228ae7a4780ccbfc430d51c9`（1058 files）。11:54 后生产为 exact `33f6332f663f6e648f27eb704593876c4de17053`，发布与 fresh 复验见末节；本文件目录名保留起始版本，不把早期结果迁移到新版本。worker 最近错误字段非空，不据此声称所有工作队列健康。
- 分母/最终判断：96；NPTCR 0/96。下面的操作性证据不升级整条 Journey。
- 证据方式：真实浏览器交互与 DOM/AX 可见结果；所需实际业务回执逐条补充，无假 provider 或模拟浏览器数据。

## 先行观察

| 路径 | 实际结果 | 边界 |
|---|---|---|
| 平台管理员：设置→公司后台→返回 App | 真实打开 dashboard，并由返回 App 回到 home | 仅此入口；不是 P29/P32 全变体 |
| 折叠侧栏→设置 | 菜单真实展开，账户/公司/平台/主题/通知/语言可达 | 未声称全部设置消费通过 |
| Chrome 旧员工标签 | 旧 UI 显示 CEDAR 成员；Agent 详情加载失败；进入 KB 后数据与旧身份不一致，停止该 persona 读取并刷新 | 刷新后为平台管理员及已登记的 owner 实验公司；旧身份缓存结果作废，不声称员工跨权限成功或泄漏；需新登录后复验 |

## 合成资产与 cleanup

本批次前缀 `WRC-FUNCTIONAL-B1-20260908`，仅虚构内容、实验 scope、隔离 Session/文档/文件/不外发自动化。此表从创建前登记更新为已观察的实际状态；未创建项明确记零：

| 对象 | 主体/范围 | 预期效果 | 实际 ID / cleanup |
|---|---|---|---|
| 个人知识粘贴 MD | 已确认 Example Owner，个人 KB | 导入→搜索→读取→隔离 Agent 引用 | `0000036a-0000-4000-8000-000000000000`；归档排除/恢复命中、fresh Agent 消费均已验证；12:02 最终归档且唯一标记搜索零命中，可恢复。MD/TXT 上传为零 |
| B-Worker KB Session | 已登记合成 B-Worker | 合成 KB 消费任务 | `000001fb-0000-4000-8000-000000000000`；停止后 provider delivery unknown，零交付物，保留 reconciliation 现场 |
| B-Worker 文件 Session / 文件 | 同上 | 文件生成、预览/下载/续写 | Session `00000079-0000-4000-8000-000000000000`；`workspace/WRC-FUNCTIONAL-B1-20260908/file-check.md` 已写/下载，原 V2 输入被 terminal ack 死信 hold，保留现场 |
| B-Worker 新版本 KB R2 Session / 报告 | 同上，fresh 隔离 Session | 唯一知识文档消费→正确修正→Markdown 写读/预览/下载/reload | Session `0000007f-0000-4000-8000-000000000000`；RuntimeTask `0000033d-0000-4000-8000-000000000000`；artifact `00000414-0000-4000-8000-000000000000`；`workspace/WRC-FUNCTIONAL-B1-20260908/harbor-summary-r2.md`，completed，保留交付证据，不宣称完整 cleanup |
| HR 草案 / Session | 系统 HR / 已确认 owner | 生成、修订、拒绝，不 provision | Session `000000a8-0000-4000-8000-000000000000`；原蓝图已拒绝、零新员工；修订 input 被 terminal ack 死信 hold，保留现场 |
| Local Agent 合成消息 | 既有在线 Codex user channel | 仅 marker/cwd 回显 | 一次发送后 approval_required，无实际回显；没有重配/重连/安装或权限修改，不假报完成 |
| 单次 Automation | 已登记 WRC 实验 Agent | 一次有界内部报告，无外部发送 | **零创建**；新建表单取消，无需停止不存在的 trigger |
| B-Worker 临时模型设置 | 既有共享合成 Agent | DeepSeek → GLM-5.3 支持本次功能检查 | 已保存 GLM-5.3；pending 原输入恢复/取消前暂不改回未 ready 的 DeepSeek，避免待处理输入换模型执行；必须在交付中披露 |

10:35 前后：个人粘贴知识文档已创建，ID `0000036a-0000-4000-8000-000000000000`，owner `00000156-0000-4000-8000-000000000000`；两段内容真实可预览，唯一标记检索命中 segment `0000024d-0000-4000-8000-000000000000`，但 UI 状态为“部分索引”，尚未 Agent 消费或 cleanup。Chrome file chooser 设置文件被扩展拒绝（Not allowed），文件上传未到达产品；未算 HiveClaw failure。临时 MD/TXT 文件仅本地创建，未上传。

拟通过产品设置临时将现有合成 B-Worker `000001b0-0000-4000-8000-000000000000` 的主模型从 `deepseek/deepseek-v4-flash` 改为 `zhipu/glm-5.3`，只用于本批次；不改权限、工具、可见性或备用模型，结束后恢复原主模型。当前为空闲、巡检未启用。此项为创建前效果登记，不表示已保存。

10:34 已保存 B-Worker 主模型 GLM-5.3，并在新对话真实显示；恢复原模型仍待批次结束执行。10:35 个人 KB 消费 Session `000001fb-0000-4000-8000-000000000000` 发起，UI 显示两次加载工具、四次 `search_memory` 和一次 `read_context_resource`，Agent 连续声称准备调用个人 KB 而未实际取到内容。2m53s / 22 UI 步骤处 Codex 请求停止。后续运行面板为“失败 / 受阻 / 模型服务请求投递状态未知 / 需运营核对”，0 运行中、0 等待中、0 交付物。未重复该请求、未强制清理，保留待 reconciliation；不能把停止后一瞬间“完成”标签当业务成功。

10:39:52 将这个工具选择问题派给 zCode 一次 12 分钟只读诊断：`9bfc6134daf847f69c83768160b5e2f5`，只看 exact production 延迟工具加载→下一次 provider tools，禁止改代码/部署/扩大恢复审计。另开独立 Session 验证基本文件交付，不重放失败 KB 任务。

自动化创建表单实测只提供每小时/每天/每周/自定义 cron；自定义仍为 cron，无单次入口或时区/下次运行预览。表单已取消，未创建自动化。是否存在对应 Session command 替代尚待验证，不据此宣布整个 P22 缺失。

新增效果预登记：HR 草案 `WRC-FUNCTIONAL-B1-20260908-DraftWriter`，仅描述合成资料整理岗位、私有范围、无外部发送/真实个人知识/后台巡检；先验证草案生成、修订与拒绝，不在未核对最终权限前 provision。实际 draft/session ID 与拒绝/cleanup 结果待写入。

新增效果预登记：向已登记的现有在线本机 Local Agent 的用户级通道发送一条标记 `WRC-B1-LOCAL-684` 的只读回显消息，只允许回报 cwd 与 marker，不改配置/权限/文件，不查看其他内容，不安装/重连或重建绑定。本机设备名不进入公开证据。此消息不授权处理历史任务。实际消息/回执与 cleanup 结果待记录。

## 结果与消耗

业务结果：八类跨域入口/局部操作已实际检查；个人知识→正确引用→真实文件交付在发布后新会话跑通，旧文件首轮、HR 首轮/拒绝、三个已保存 Session 面板、部分角色 API 正负向和后台/设置入口取得局部正向证据。文件续写/HR 修订共同阻塞、新 draft context 422 为明确失败；once 零创建、Local Agent 未获得实际回显为未完成。完整冻结协议新增执行 0/96、完整功能要求通过 0/96、最终 NPTCR 0/96；不得把八类入口换算成八条完整旅程。

实际投入：总预算 120 分钟，涵盖准备、执行、记录与交付，未自动延长。zCode 4 次正常完成的调用共 1,044.357 秒（诊断、实现、一次集中返修、共同输入断点只读定位），另有 1 次 adapter 启动失败 1.912 秒，合计 1,046.269 秒；不是全部 Codex/产品模型 token 或串行墙钟。源码修复包 1 个、集中返修 1 次、CI 1 次、三服务同源发布批次 1 次。CI 从 11:14:31 到 11:48:37，共 34m06s，与其他功能检查并行，未循环重跑完整门。产品可见首轮耗时：文件 1m16s、HR 2m57s、发布后 KB R2 2m43s；首个失败 KB 在 2m53s 请求停止，delivery unknown 原样保留。Codex 本任务总 token **不可用**；不把账户额度变化或产品 Usage 面板归因为本任务。

下一批估算：建议仍只授权两小时，集中处理第二轮输入共同断点并复验原文件 V2/HR 修订；一个方案和一次返修不能收束时交付策略选择。该建议未获执行授权。完整 96 条摸底、修复与最终双遍目前缺少完整单旅程吞吐/缺陷分布样本，不能据八类局部操作作线性工期承诺。

## 11:16 阶段结果与新发现

| 实际操作 | 观察结果 | 保留的边界 |
|---|---|---|
| 个人知识粘贴 Markdown→切段预览→唯一标记搜索 | 文档真实创建，两段可读，`violet heron 473` 命中其段落 | 部分索引；上传被浏览器扩展拒绝，PDF/DOCX/TXT 未到达产品；Agent 消费未通过 |
| B-Worker 首轮文件生成→预览→下载→刷新重开 | 完成 1m16s / 6 UI 步骤；`file-check.md` 44 B，下载内容逐字一致，刷新后预览仍可读 | 仅此 Markdown 主链；不是全格式、全部角色或整个 P30 |
| 同 Session 文件续写 | 10:54:07 输入已接收，11:11 UI 仍显示运行中；`runs/active=null`，workbench 只有旧 completed RuntimeTask，`active_turn=null`，最新事件为 `input_admission.admitted` | 新任务并无可确认的活跃执行；不得归因为 provider 慢，不重复提交；V2 未通过 |
| HR 草案生成→要求修改→拒绝 | MiniMax M3 首轮生成 2m57s / 6 UI 步骤；修订 UI 持续运行超过 26 分钟，无新版；11:15 原草案实际显示“已拒绝” | 未点击确认/创建、未 provision 员工；修订未通过；无首任务执行证明 |
| Automation 新建/自定义表单 | 仅见小时/日/周/cron；已取消，零创建 | 一次性调度的 Session command 路径待查，不据单个表单判整体缺失 |
| Local Agent 只读回显 | 现有 Codex 绑定在线，新消息返回 `approval_required: Waiting for owner approval`；未收到 marker 回显 | 未安装、改权限、重连或重建绑定；新页面通道记录为空，不冒充恢复/交付成功 |

文件 Session `00000079-0000-4000-8000-000000000000`；首轮 RuntimeTask `0000009c-0000-4000-8000-000000000000`；artifact `00000059-0000-4000-8000-000000000000`；目标 `workspace/WRC-FUNCTIONAL-B1-20260908/file-check.md`。实际下载文件 `file-check.md`（操作者本机 Downloads 目录），SHA256 `97e5277e613013daacd2a01cba72882e9dc1bcb6be72c38382dce3d32bc3ec9d`。续写 input `00000134-0000-4000-8000-000000000000`，admission `00000188-0000-4000-8000-000000000000`，checkpoint sequence 192，admitted sequence 198；没有 V2 artifact 回执。HR Session `000000a8-0000-4000-8000-000000000000`；草案名 `WRC-FUNCTIONAL-B1-20260908-DraftWriter`。

### 工具加载小修复（源代码结论，不替代生产复验）

- 只读诊断 `9bfc6134daf847f69c83768160b5e2f5`：202.369s；实现 `c6adf898f2f343b2920ea23b2321f2d2`：236.451s；一次集中返修 `f4b81fd44a01425599c3025099dba2b3`：308.784s。实际 zCode 主请求 model/wire model 为 GLM-5.3，模型响应 ID 为 glm-5.3；不把 wrapper success 当产品通过。
- exact `17f073bb` 的 `_resolve_tool_expansion` 每次只解析新查询，而 kernel 用返回数组替换整个 toolset；已加载工具因此能在后续 disjoint search/MCP activation 后丢失。该源码缺陷可达，但本批次 KB 失败 UI 本身不能唯一证明其根因。
- 修复从现有 `SessionContext.discovered_tools` 累积名称，再经现有 registry 重新解析，保留仍授权工具、避免盲合并过期 schema；同时覆盖 MCP 激活分支。未修改权限、模型预算、coordinator policy 或旧恢复代码。
- 独立 worktree `/private/tmp/hiveclaw-functional-b1.GMMOls` 仅两文件变动；Codex 完整差异审查，实际执行 `tests/runtime/test_invoker.py tests/kernel -q`：**343 passed in 6.65s**；Ruff check、format check、diff check 通过。解释器 `/private/tmp/hiveclaw-sa01-staged.5EpyKi/backend/.venv/bin/python`，实际 cwd 为本 worktree 的 `backend/`。测试中的 registry 是替身，不能冒充生产权限或真实 provider E2E。
- 11:14 源码 commit/push `33f6332f`，main 仅快进这两个文件，其他 dirty/untracked 保留；当时 CI 已触发、生产尚未更新。后续 CI/部署成功见末节。本批次只有这一轮修复，不启动无限审查。

### 11:20—11:39 补充实测

- 同一合成个人文档归档后状态为“已归档”，唯一标记检索零命中；点击正常“恢复”后状态回到“部分索引”，再次搜索命中原 segment。未删历史文档；文档目前恢复，供发布后 Agent 复验使用，最终归档尚待执行。
- 新 draft Session `draft:000002b5-0000-4000-8000-000000000000` 执行 `/context`：command execute HTTP 200，但导航到该 draft 字符串作为 Session ID 的页面，index/workbench/context-usage 各返回 422，UI 持续“正在解析会话”。该草稿未得到实际持久化 UUID，不作已创建 Session 清理对象。
- 既有 UUID KB Session 上 `/context`、`/permissions`、`/usage` 最终均打开对应面板。发送后短暂显示“排队/思考”而后转为面板，不据此声称发生了 provider call。Context 显示 available tools 0、loaded skills 0，Permissions 为 Use/request-approval，未核实这些面板覆盖的完整性；Usage 显示 Session 263,817（input 262,654/output 1,163）、Agent today/month 各 157,497 tokens。只是当时 UI 数据，未做计费对账，也不是 Codex 本任务总消耗。
- HR 后端同样只有旧 completed RuntimeTask `0000010c-0000-4000-8000-000000000000`，active_run/active_turn null、can_start_turn true、can_stop false、无 tool-effect reconciliation。修订 input `00000290-0000-4000-8000-000000000000`、admission `00000004-0000-4000-8000-000000000000`；seq360—366 最终 admitted，02:49:05.573935Z 后无后续 run。两个不同模型的第二轮均出现此现象。
- CEDAR R2 member `0000030a-0000-4000-8000-000000000000` 与 GROVE R3 org_admin `000002ca-0000-4000-8000-000000000000` 各通过正式 auth/login 与 auth/me 实时核对，tenant 均为 `00000018-0000-4000-8000-000000000000`。两者读取该公司的员工测试 Agent `00000187-0000-4000-8000-000000000000` 均 200；平台 `/admin/companies` 均 403；另一实验公司 B-Worker 与上述另一人的个人合成文档均 404。只输出角色/ID/status；Keychain 凭据与 token 不落盘、不打印，未改浏览器登录态。
- 可重跑 read-only 检查：`SSL_CERT_FILE=/etc/ssl/cert.pem python3 tmp/wrc-functional-b1-20260908/check_employee_api.py`（repo root）。初次系统 Python 缺证书链而在 TLS 握手失败；改用现有系统 CA bundle 后成功，未禁用 TLS 或安装依赖。API 正负向不冒充 employee/org-admin 浏览器 UI、完整撤销或故障矩阵。
- 只读定位首次 `13ecd04633e74cb5b0c68843a48f205a` 在启动阶段 1.912s 失败：adapter 不支持显式 `--model`，无 prompt 分析或代码修改。正确复用既有配置的 `ffc5208120e64f2dacb2705e451cffea` 最多 600s，仅定位共同输入断点、不授权新返修，仍在 B1 截止内。
- `/once` 的源码入口存在：`commands.py::_execute_schedule_command` 对自然语言先回交 Agent 草拟、确认后才能启用。未据创建表单没有 once 就宣称功能缺失；本批次未走完这条路径，也没有创建任何自动化。

### 11:47 共同输入断点的真实只读对账

zCode 只读定位 `ffc5208120e64f2dacb2705e451cffea` 正常结束，296.753s，未修改文件。匹配本任务的 native main request `000001e2-0000-4000-8000-000000000000` 的 model/body model 均 GLM-5.3，response model glm-5.3。源码指出 `_terminal_boundary_dispatch_hold` 会在旧 terminal boundary 未 delivered 时 defer 新输入；这在读取生产前只是待证假设。

Codex 经现有 Railway SSH 身份、exact Hive production/backend 执行一次合成目标限定 SELECT。实际连接 `current_user=app_rls`、`transaction_read_only=on`，仅 pin 已认证 owner 的实验 tenant，事务最后 rollback，无 bypass、无数据修改、无 redrive。脚本为 `tmp/wrc-functional-b1-20260908/inspect_followup_receipts.py`。

| Session | 新输入 dispatch | 阻塞 boundary | 实际旧任务 / 回执状态 |
|---|---|---|---|
| 文件 `00000079…` | pending；attempts 1；`terminal_boundary_ack_pending` / `waiting_for_terminal_boundary_ack`，无 dispatch_last_error | `000001a8-0000-4000-8000-000000000000` | 旧 task completed；boundary 已在 02:42:07 enqueue，但 turn_stop 回执 dead_letter、attempt 8、`WebTerminalBoundaryPending`，未 delivered |
| HR `000000a8…` | pending；attempts 2；同一 ack-pending receipt，无 dispatch_last_error | `0000013c-0000-4000-8000-000000000000` | 旧 task completed；boundary 已在 02:45:25 enqueue，但 turn_stop 回执 dead_letter、attempt 8、`WebTerminalBoundaryPending`，未 delivered |

结论由假设推进为：**第二轮被旧终态回执死信阻挡，不是 provider 正在思考，也不是没有创建过 terminal boundary**。UI 没把这个等待原因暴露出来。为什么 boundary 消费仍 Pending，还需窄查该回执对应的 T0/transcript/projection；同名错误不自动证明旧 root cause 原样复发。当前不重放输入、不手改 outbox 状态、不把 retry 当完成。两条原输入与现场保留；恢复前不应把 B-Worker 的模型改回未 ready 的 DeepSeek，以免待处理输入换模型执行。

## 11:54 发布与有界复验

Harness CI `34182826176` 对 exact `33f6332f663f6e648f27eb704593876c4de17053` 三 job 全部 success。三服务均从该提交的 `git archive` 产生的干净包上传，不含主 checkout 的其他 dirty/untracked：backend `00000164-0000-4000-8000-000000000000`、backend-api `000000cf-0000-4000-8000-000000000000`、frontend `000004b7-0000-4000-8000-000000000000`，三者终态均 SUCCESS。backend public health HTTP 200，新运行源码 SHA256 `510f6eb45fdb671eb4cf4852bbc5e49d0f3a7322ade18098d240b2577ec2620c`、1058 files，与本地 exact archive 一致；frontend HTTP 200。

复验效果预登记：仅 B-Worker 的一个 fresh Session，重新从已恢复的本批次个人 KB 文档读取事实，生成新文件 `workspace/WRC-FUNCTIONAL-B1-20260908/harbor-summary-r2.md`。使用 GLM-5.3，禁止其他文档、公网、委派、循环或后台任务；不重放旧 Session/input。以下为实际结果；12:16:24 总截止不变。

## 11:58—12:07 新版本消费与收尾

- backend-api 的实际运行源码也通过只读 SSH 回读，SHA256 同为 `510f6eb45fdb671eb4cf4852bbc5e49d0f3a7322ade18098d240b2577ec2620c` / 1058 files，与 backend 和 exact archive 一致。CI 三个 job（Backend harness gates、Frontend unit/visual/accessibility、Fifteen API/worker/browser journeys）均 success；CI 里的受控外部 fake 不冒充本节真实生产消费。
- Fresh Session `0000007f-0000-4000-8000-000000000000` 于 11:55:52 创建；RuntimeTask `0000033d-0000-4000-8000-000000000000` 于 11:58:34.705810 完成，UI 为 GLM-5.3、2m43s、21 步。输入只给文档标题和需回答的字段，未给事实答案。
- 刷新时真实 transcript API HTTP 200，554 个 V2 events；全部 `tool_call.started` 为 1 次 report_progress、3 次 tool_search、1 次 search_personal_kb、1 次 read_personal_kb、1 次 write_file、1 次 read_file。没有其他知识/公网/委派工具调用，不依赖模型“已完成”的自述。
- search result 在 sequence 116 返回该唯一 document 的两个 segment，`status=ok/outcome=success`，authority `allowed=true/interactive_owner_agent`；文本检索可用，optional vector 的 `provider_unconfigured` 对应“部分索引”，不冒充向量检索通过。
- read result 在 sequence 192 返回唯一 document 的两段完整内容，均 `truncated=false`，authority `allowed=true`；原文为验证短语 `violet heron 473`、当前 cobalt 12、amber 7、总 folders 19、虚构协调员 Mira、Tuesday 14:30 UTC，Correction 明确旧 10 已被当前 12 取代。read result content hash `c84672ef1e4145345ab402cceb391cb447b40d4b20f8d540e22cb32500c0d48c`。
- write_file 在 sequence 369 开始，唯一 artifact `00000414-0000-4000-8000-000000000000` 创建于 11:57:47.960128；read_file 在 sequence 397 成功回读报告及本 Session/run provenance。canonical `run_outcome.terminal_committed` 为 sequence 554，time `11:58:34.583439`；workbench RuntimeTask completed、active_run/active_turn null、无需 tool-effect reconciliation。此处未测第二轮或故障恢复，terminal commit 不冒充旧 outbox 已恢复。
- 实际文件预览和下载各 HTTP 200；内容正确引用两个 segment，采用 12 与 19，包含 Mira 和 Tuesday 14:30 UTC。下载文件 `harbor-summary-r2.md`（操作者本机 Downloads 目录）为 1485 bytes（1483 characters），SHA256 `9371dee024a6777e0f6fe071fe04676dbe63310b386bcdac6ade6cc8b4231807`。Codex 已读取实际下载内容；刷新 Session 后再次打开保存快照，内容仍相同。
- 12:02 仅将合成知识文档 `0000036a…` 正常归档；详情为“已归档”、操作为“恢复”，唯一标记 `violet heron 473` 搜索零命中。未删除历史文档或共享 fixture，可恢复。两份下载文件及报告 Session/artifact 保留为交付证据；旧 KB unknown、文件 V2/HR pending 与 Local approval-required 现场不强行清理。
- 12:07 发布后正常刷新两个旧 UUID Session：HR 修订仍“处理中 78m13s”，原蓝图保持“已拒绝”，没有修订版本；文件续写仍“处理中 73m11s”，只有原 44-byte 文件，没有 V2。确认本次工具加载发布没有恢复这两个旧阻塞。没有重发输入、手改回执、新建员工或额外 provider 重试。
- B-Worker 保持本批次临时 GLM-5.3；在 pending 输入恢复/取消前不改回未 ready 的 DeepSeek，避免待处理输入换模型执行。没有新增 automation/Goal/heartbeat、重连/安装或真实外发；原 heartbeat 仍 PAUSED。此保留项和未完全 cleanup 在交付中披露。
- 收束校验：`python3 backend/scripts/weekend_rc_gate.py validate` 为 valid、denominator 96、errors=[]、`semantic_verdict=not_computed_by_tool`；manifest hash `73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37`。该结构校验不产生业务 PASS。应用修正已 commit/push/deploy；本批次可变验收记录保留在工作区，不为记录再部署新应用版本。
