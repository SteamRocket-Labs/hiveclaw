---
document_id: weekend-rc-2026-08-30-current-status
owner: Codex
status: active
authority: canonical-working-state
last_reviewed: 2026-08-31
source_commit: b23e94210e7e9523bafc3b591b35db8fc2762224
verification_status: p29-admin-audit-default-disclosure-production-verified
---

# 当前状态与唯一下一动作

[返回索引](README.md) · [旅程账本](04-journey-ledger.md) · [Findings](05-findings.md) · [Runbook](06-runbook-and-release-gates.md)

> owner 已接受 PDEC-001～PDEC-006、最终部署和 D/E 双提交合同；96 条 production journeys 已冻结并通过机械结构校验。七个既有 Session/tool/authority/runtime/audit 根因均已完成 production 复验。P29-PADMIN 的模型 Test 现在有 canonical audit；admin 审计默认流也只显示 tenant/provider/runtime/compliance summary，不再展开 Session/job/reason/raw provider payload。P29 完整 pass-1、第二遍、四角色/故障/权限矩阵仍未完成；NPTCR 保持 0%。

## 当前目标与可观察 Done

完成 Weekend RC 整体验收与修复闭环：由单一 Codex 直接持续执行“真实 E2E → finding → 前后端修复 → 本地验证 → 集成 → 三服务部署 → 生产重验 → 更新文档”，不再派发 Kimi Code、zCode、Coze/ACP 或任何 sub-agent，直到全部 in-scope 旅程 `Closed loop`。

可观察 Done：NPTCR=100%；五条不可平均护栏全部通过；Evidence Coverage ≥95 且不抵消七原子缺口；Zero Known Defects；backend/backend-api/frontend 同一 exact commit `SUCCESS`；frozen manifest signed-in 双遍、故障恢复和权限负向全部通过；合成资产 cleanup 完成。

## 当前事实快照

| 事实 | 当前值 | 证据等级 |
|---|---|---|
| 已推送控制基线 | `8771fb840e51f5743114f39a2b643f355da5c7a0` | 96 条 manifest、机械 gate、宽松 worker contract 的历史控制提交；当前 application HEAD 见 `b23e9421` production 行 |
| Goal | 当前 Goal `01a052d8-903d-7982-b149-6d3b0040424e`；三次等待发送确认后机械状态变为 `blocked`，owner 于 2026-08-31 明确恢复并确认 D3 发送 | Goal API 没有单独 resume mutation；owner 新输入恢复实际执行，机械状态不取代 canonical docs/task-state，也不得提前标 `complete` |
| execution roles | Codex 独自负责总控、前后端实现、独立验收、生产 E2E、文档与交付 | owner 2026-08-30 紧急改策；禁止 Kimi Code、zCode、Coze/ACP 和任何 sub-agent/delegation |
| delegation status | 全部外部派发已停止；原 Attempt 4 的 wrapper/zCode 进程已退出 | 精确 PID `99847/99850/99851` 已不在进程表；机器上其他 zCode 进程属于其他任务，未误杀 |
| delegation incident | Issue #4 首包在 `approve-all + Terminal` 下运行 901.112s 后被旧 900s target timeout 取消，1943 events、零 protocol error、零 diff；wrapper 同时报 `status=success` / `stop_reason=cancelled` | receipt `f052ff24…`；确认不是权限失败。旧 correction 在改合同前主动取消，worktree 保持 clean |
| 已拒绝 worker candidate | Attempt 3 `cd7a85f…` 正常返回并形成 3-path candidate；Codex 因 silent fallback、机械截断、pure-V2 rewind/branch/current-run legacy 缺口拒绝接受。Attempt 4 `b3708fd9…` 已按 owner 指令停止，未产生新 diff | 原 3-path 未提交候选仍保存在 `/tmp/hiveclaw-issue4-zcode.INEFH2`，仅作历史受审查输入，不得直接认定完成或盲目合入 |
| shared-config drift | Attempt 4 首次启动时发现另一写入把 wrapper/acpx/zCode 统一改成 7200s；约 10 秒内主动终止，未新增 diff。两个 registry 已恢复 43200s，doctor/dry-run/preflight 全部重新通过后再派 | 被终止 receipt `e2cea004…` 不计 worker attempt 或业务进展；当前实际 zCode argv 为 `--prompt-timeout-secs 43200` |
| GitHub 控制层 | milestone [#1](https://github.com/SteamRocket-Labs/hiveclaw/milestone/1)、umbrella Issue [#3](https://github.com/SteamRocket-Labs/hiveclaw/issues/3)、7 个 RC role/state labels | 本轮远程 readback 已核验；不构成 acceptance truth |
| 旧 WIP archive | 原 5,685 行、约 1.29 MB，完整迁移并增加 archive warning | 本轮本地已核验 |
| 当前 production 业务提交 | `b23e94210e7e9523bafc3b591b35db8fc2762224` | Railway 三服务 exact-commit fresh readback：backend `03d0919e…`、backend-api `b0bb7ca3…`、frontend `0dd299d8…` 均 `SUCCESS`；backend health `status=ok` / RLS strict / `runtime_control_bus.last_error=null`，frontend `/` 为 `HTTP 200` |
| production 身份/模型 | 实验 tenant，当前账号为 `超级管理员`；EventPilot primary `zhipu/glm-5.3`、fallback `minimax/MiniMax-M3`，可选 `deepseek/deepseek-v4-flash` | signed-in UI + bounded live readback：MiniMax success `7623ms`、GLM pre-fix success `7575ms`、post-fix audited GLM success `3411ms`；DeepSeek 单次 `HTTP 402 Insufficient Balance`，未重试或改 credential/billing |
| Local Agent | launchd daemon running；CLI/API `401 Invalid bridge token`；UI linked `0`、offline | `BLOCKER-BRIDGE-001` 已确认为 `BLOCKED_PRECONDITION`；未授权 re-login/token replacement |
| fresh Session P1 | `SESSION-CONTEXT-001` 的同一 Session 语义缺失已在 production exact commit `d0c9fffd` 上复验通过 | Session `3ce68041…`：P01 probe 只答 `ACK-FIRST` 且未回显 marker；P02 未携带 marker，却正确答 `HIVE-CANONICAL-Q7M4-83NP NO_TOOL`；operator workbench receipt 证明 current-run input 已排除、历史恰含前一轮 user+assistant、无机械上限或 held item |
| P01-MAIN pass-1 | MAPLE fresh Session 的开放任务、GLM-5.3、73-tool surface、3/3 todos、write/read、七项 artifact 标准、reload/no-replay 与普通预览均通过；但实际主体为 `platform_admin`，冻结 persona 要求 `employee`，故仍无可计分 pass-1 | immutable precondition evidence：`evidence/3482b57a383d3c5bd33a5bcf813b87c6fab23339/P01-MAIN-blocked-persona-platform-admin.md`；不得把功能 PASS 覆盖 principal mismatch |
| P29-PADMIN negative | 跨用户 Session URL 的 backend authority 与 frontend presentation 已在 `bbf6d234` 对齐：只显示 not-found、无 Session shell/正文，返回 `/agents` 无 stale error；合法 MAPLE Session 保持可消费 | immutable pre-fix FAIL：`evidence/3482b57a383d3c5bd33a5bcf813b87c6fab23339/P29-PADMIN-fault-denied-session-shell.md`；production verification：`evidence/bbf6d2340afe593b44f740fabfa178d126b5beca/SESSION-AUTHORITY-PRESENTATION-001-production-verification.md`。finding 为 `Verified`，不等于 P29 Journey PASS |
| P29-PADMIN positive pass-1 | 平台 dashboard、公司列表、tenant 模型配置、runtime protection 与 tenant audit 已只读打开。runtime presentation、provider Test audit 与 admin default disclosure 三个根因均 production `Verified`；hard reload 后 400 条摘要保留且六类业务/取证 payload 为 0 | evidence：`evidence/6a6695e88d915a0e37b44e64dcdfe5bdd90a9454/RUNTIME-GUARD-PRESENTATION-001-production-verification.md`、`evidence/cc6e726218bd491120f942edfa91e51d2d167ff4/LLM-PROBE-AUDIT-001-production-verification.md`、`evidence/b23e94210e7e9523bafc3b591b35db8fc2762224/AUDIT-DEFAULT-DISCLOSURE-001-production-verification.md`。完整 pass-1/pass-2 与四角色 matrix 仍 open |
| Session terminal/failure | 旧账记录 §7.77/§7.78 已部署并完成 signed-in 双遍 | 历史证据；不外推整体 RC |
| executable CI manifest | `acceptance/atomic_user_journeys.v1.json`，J-01～J-15，声明受控 external fakes | 本轮源码已核验 |
| production NPTCR manifest | `acceptance/weekend_production_journeys.v1.json`，35 组展开 96 条，external fake 禁止 | 已冻结；validator `valid=true`；当前无 production pass |
| mechanical gate | `backend/scripts/weekend_rc_gate.py` | 只校验 exact facts/算术，固定 `semantic_verdict=not_computed_by_tool` |

## 当前产品总判断

| 验收域 | 当前判断 | 仍需证明 |
|---|---|---|
| Git / Production | 基线健康，不等于 RC 完成 | 后续代码变更后 exact same commit 三服务部署和全旅程重验 |
| Session 核心终局 | `Partial loop` | 四个已复现 P1 根因保持 production `Verified`；MAPLE 功能路径通过但 principal mismatch。需要复用现有 signed-in `member` identity 从 fresh P01 重跑；缺失登录状态不能由 platform_admin、DB、mock 或账号变更代替 |
| 整体前端 | `Partial loop` | `SESSION-AUTHORITY-PRESENTATION-001`、`RUNTIME-GUARD-PRESENTATION-001`、`LLM-PROBE-AUDIT-001` 与 `AUDIT-DEFAULT-DISCLOSURE-001` 已 production `Verified`；仍需 P29 四角色 state screenshot/API/compliance matrix，以及密度、双主题、窄屏、键盘、Agent rail 规模 |
| Rewind / Resume / Fork / Rollback | 旧账标 `Closed loop` | 只在 fresh journey 复现新错误时重开 |
| Personal / Company KB | `Partial loop` | 多格式、多入口、权限负向、失败恢复、Agent/Personal→Company/后台路径统一生产矩阵 |
| Agent Memory | `Partial loop` | coverage、T3 consumption、fresh Session recall |
| Growth / Eval | `Partial loop` | J1/J2 longitudinal reuse、owner feedback/rework、真实 J4 FreeCode/Hermes bakeoff |
| HR 创建 Agent | `Partial loop` | Agent→HR 与普通员工 fresh 双遍、被创建 Agent 首任务 |
| Plan Mode | `Partial loop` | model-authored plan、revise/reject/cancel、exact-version approve、execute/recover |
| Sub-agent / Team | `Partial loop` | 两类真实语义任务双遍、成员失败与父结果 UI 消费 |
| Dynamic / Fixed Workflow | `Partial loop` | 各自从真实产品入口到结果双遍 |
| A2A | `Partial loop` | async continuation、A→B→C nested、长结果/artifact、固定路径 UI |
| Commands / AgentDetail | `Breakpoint` 候选 | 20 commands 逐条 UI↔API↔runtime；四个候选断点先复现 |
| 权限与角色 UI | `Breakpoint` | employee/admin/platform/operator 正负向矩阵 |
| Offboarding | `Partial loop` | signed-in 停用/重放/并发、通知、数据保留/导出/删除政策 |
| Trigger / Channel / Local Agent | `Breakpoint` aggregate | create→fire→execute→deliver→notify→retry/cancel/audit；offline/reconnect/revoke |
| Hooks / Skill / MCP | `Breakpoint` aggregate | lifecycle、Trust Review、真实调用、update/revoke/auth expiry |
| Artifact / async return / model-cost | `Breakpoint` aggregate | preview/download/version/ACL、deep-link、selected-model fidelity、latency/token/cost/cache |

## 已知外部前置条件

- 当前唯一可用的 signed-in Browser identity 经 server-side 核验为 `platform_admin`；现有三个 Browser tabs 共用该身份，没有已登录的 `member/employee` principal。P01/P02/P03/P04 等 frozen employee journeys 必须从真实 member 登录状态重跑；登录、创建账号、角色/grant 变更未授权，当前停在 login/principal gate。
- 三个 configured provider 的一次性 bounded live probe 已完成：MiniMax 与 GLM 成功；DeepSeek 单次返回 `HTTP 402 Insufficient Balance`。该 blocker 需要 owner 另行授权 billing/credential action 才可能改变；当前不充值、不换 credential、不盲重试。bounded health verdict 也不等于 P33 frozen compatibility task。
- Hive Connect 已 fresh reproduce `HTTP 401 Invalid bridge token`；daemon running 不等于 product path 可用。修复需要 re-login/token replacement，当前停在 owner action gate。

## 本次已完成

- owner 已授权文档组重构。
- 已加载并应用 `task-state-with-files` 与 `agents-best-practices`。
- 已确认现有 CI manifest 与未来 production manifest 必须分账。
- 已建立索引、North Star、owner decisions、current status、journey ledger、findings、runbook、六个 domain 和 evidence contract。
- 已把旧总账完整移入 `archive/legacy-ledger-2026-08-25.md` 并加入显著历史警告；旧路径缩为 21 行兼容跳转。
- 已把 `work/task-state.ref` 切换到本文件，恢复解析成功。
- 已更新 `docs/README.md`，并增加纯结构架构测试。
- owner 已批准一个 Goal + GitHub Issue work packet + stateless `agent-delegate` + isolated worktree 的执行模型。
- 已启动唯一 Goal；已明确 Codex/Kimi/zCode 权限边界、独立验收和上下文隔离合同。
- 验收文档与 Issue form 已原子提交并 push 为 `228682e5`；用户已有 dirty/untracked 内容未进入提交。
- 已建立远程 RC milestone、labels 和 umbrella Issue；它们只承担队列/审计，不拥有 verdict。
- 首轮 smoke 因 worker 请求 `git rev-parse` execute 权限而在 `approve-reads` 下正确 fail-closed；新无状态 correction packet 仅使用 read-file 后，Kimi/zCode 均成功，两个临时 worktree 保持零改动并已安全移除。
- owner 已进一步批准：Goal 只管最终 Done/停止条件，Skill 管派发协议，Issue 管工作包，Codex 独占验收/集成/生产 E2E。
- owner 已正式接受 PDEC-001～PDEC-006、最终三服务同应用提交部署和实验 tenant 内可回收合成 E2E；凭据、计费、DDL、不可逆效果仍未授权。
- 已冻结 96 条 production journeys；20 commands、Personal/Artifact 四格式、A2A 六路径、Automation 四模式、MiniMax/GLM/DeepSeek 和六类安全探针均独立计分。
- 已新增 manifest validator/evidence scorer；它只计算结构、双遍、部署 identity、护栏和证据覆盖，不判断语义质量。
- 控制冻结已 commit/push 为 `56ec5dd0`；18 个结构/架构测试、Ruff 与 diff check 通过。
- Gate 0 已 fresh 核验 production 三服务 exact commit、账号角色、EventPilot 的 GLM/MiniMax/DeepSeek binding、公共 health 与 Hive Connect。
- 已用 GLM 在 production 创建唯一标记 Session：第一轮答案满足外部语义判据；同一 Session 第二轮却否认存在上一轮回答。
- live readback 证明 canonical Session V2 transcript 有 635 个有序事件及两轮完整输入/输出，但兼容 `/messages` 投影只有 10 条 system/debug；runtime 当前仍从 `ChatMessage` 组装历史，`SESSION-CONTEXT-001` 已进入 P1 修复链。
- 已创建 GitHub Issue #4 并向 zCode 派发首个 backend 实现包；首包暴露 target-internal timeout 与 outer timeout 冲突，而非 worker 权限不足。
- owner 要求放宽派发规则后，工作流改为默认完整 capability、task-sized timeout、effective-timeout warning、receipt 分类和同 Issue verified checkpoint 续传；不再把固定 stop reason、固定缓冲或是否有 diff 当作语义裁决。
- owner 进一步撤销任意单任务 timeout；wrapper default/max 与 zCode target internal timeout 已统一为整轮 12 小时窗口 43200s，两个 registry 一致、dry-run 为 `approve-all + Terminal`，且 `agent-delegate doctor --json` 返回 `status=ok`。旧 900s correction 已安全取消，隔离 worktree 无改动。
- Issue #4 第三次无状态实现包已从 clean exact base `8771fb84` 派给 zCode；本次不传单任务 timeout，进程持续运行。Codex 已建立相关 Session history/reload/compact/rewind 的 7-test 绿基线，但尚未收到或接受 fix candidate。
- Attempt 3 已正常返回：新增 canonical semantic-history loader、live runtime 接线和 5 个真 PostgreSQL 回归；Codex 独立复跑为 `5 passed`，但 review 发现其仍会在 loader 异常时退回旧 `ChatMessage`、按固定窗口静默裁剪，并明确留下 pure-V2 rewind/branch 缺口，因此未接受、未集成、未部署。
- GitHub Issue #4 已追加 Attempt 3 receipt 与 rejection finding；Attempt 4 correction 在同一未提交 worktree 无状态重发，要求一次关闭所有 Issue 内边界。共享 timeout 漂移已在启动检查中捕获并恢复为 43200s 后才正式运行。
- owner 因剩余窗口不足 10 小时终止外部派发策略；Attempt 4 已停止且候选 diff 保留。后续改为新对话内单 Codex 直接实现、验证、部署和留证，不再运行任何 delegation 工作流。
- 单 Codex 已沿 live HTTP/runtime wiring 完成源码追踪，并用当前 production ingress、Session V2 round commit、terminal outcome 和 `_load_runtime_context` 建立 12 条 production-shaped 回归；实现前结果为 **12 failed**，明确覆盖同 Session 语义、tool pair、current-run ownership、legacy 去重、跨旧 model-derived cap 的完整历史、provider seal 字节保真、system/debug 排除、pure-V2 rewind、真实 branch prefix、typed empty 与 fail-closed unavailable。
- 主工作树现已形成 Codex 自审可接受的本地 Fix Candidate：canonical history 只读 tenant/agent/session 绑定的 transcript、committed model seal、settled tool result 和 anchored legacy row；无固定消息窗口、无不可用时 silent fallback，pure-V2 rewind 和真实 branch copied prefix 均由 canonical sequence/lineage 消费。
- Fix Candidate 已作为 application commit `d0c9fffd1ca4995ddea6d367e04e206e973560d5` 原子提交并 push，提交只含 3 个 runtime 路径与 2 个测试路径；RC evidence 本文件和所有 owner dirty/untracked 路径未进入 application commit。
- 三个 Railway production service 已部署同一 exact application commit `d0c9fffd1ca4995ddea6d367e04e206e973560d5` 并全部 `SUCCESS`；随后在 fresh production Session 完成两轮根因语义探针，证明 canonical history 已进入真实 provider path。
- `SESSION-RETRY-INPUT-001` 的 D2 application commit `2cee9f3e` 已完成完整 backend gate、push 和三服务部署；production retry round 精确绑定 durable input 并进入 GLM/Work Ledger，因此该 finding 推进为 `Verified`，但同一 run 在 write effect 后暴露新的 `TOOL-ARTIFACT-SETTLEMENT-001` P1 与独立 provider 429。
- `TOOL-ARTIFACT-SETTLEMENT-001` 已从真实 PostgreSQL FK failure 建立 RED，完成无 DDL 的原子 owner/artifact/V2/outbox settlement、typed reconciliation、kernel 串并行 hard-stop 与 frontend identity dedupe/artifact consumption；application commit `c37fefc56b92e658bfb64a3e79d685249a2a3add` 已 push。
- backend `62e4ef56-7e6b-456e-a505-fea90fd286a0`、backend-api `307f0df7-6ae0-4c57-817e-f9ca07fd59fc`、frontend `db6b605d-7b8b-40ea-8da8-247259db29f8` 已部署同一 exact D3 commit 并均为 `SUCCESS`。旧故障 run reload 不会自动 replay；fresh normal settlement/reload/preview 已通过。该 checkpoint 后续已由 `3482b57a` supported recovery 复验补齐。
- 单 Codex 已为旧 unknown-effect failure 补齐 fail-closed admission 与唯一 operator recovery：所有 fresh turn/branch/run 在 exact unresolved invocation 前返回 non-retryable typed blocker；抢先 admitted input 会落为 no-replay `needs_reconciliation` 而不是被 worker 无限 sweep；管理员只能提交必填 evidence reason 后 acknowledge/stop，系统不创建 `tool_result`、不恢复旧 provider round。应用 commit `3482b57a383d3c5bd33a5bcf813b87c6fab23339` 已 push。
- recovery candidate 本地最终门：后端定向 **310 passed**，完整 backend **8438 passed, 2 skipped, 1 warning**；frontend **1145 passed**、i18n、production build 与 AgentDetail/vendor bundle budgets 通过；Ruff/format、`git diff --check`、18 条 Weekend/atomic architecture tests 和 manifest `valid=true` / denominator `96` / hash `d320edce…` 均通过。React 结构门曾以 2439>2400 失败，未放宽预算，提取 recovery/feedback surfaces 后 AgentChatSection 为 2392 行并重验全绿。
- backend `7c196980-34c6-4846-bf25-0397b7b55c0e`、backend-api `8e7545b8-9b6c-4b32-a77d-48883191728a`、frontend `6f6bd18c-1681-4049-ac20-6660a3f84fc3` 已部署同一 exact `3482b57a` 并均为 `SUCCESS`。旧 D2 Session 的 fail-closed admission 与管理员队列 precheck 先只读通过；随后只对 `76a32f8e…` 提交一次 evidence-backed acknowledgement，并完成 canonical no-result reconciliation、旧 run no-replay、fresh-turn release 与 reload 复验。证据分别见 `evidence/3482b57a383d3c5bd33a5bcf813b87c6fab23339/TOOL-ARTIFACT-SETTLEMENT-001-recovery-admission-precheck.md` 和同目录 `TOOL-ARTIFACT-SETTLEMENT-001-recovery-verification.md`。
- recovery production result：目标 invocation 仍为 `needs_reconciliation` / `result_event_id=null`，`recovery_owner` 清空；sequence `312/313` 恰为 `tool_call.reconciled` / `recovery_action.reconciled`，outbox 均 `published` / attempts 1。旧 run 保持 `failed`；新的 no-tool run `f8cdd9ac…` 独立绑定 input `ad602cdc…`、返回精确 `D4_RECOVERY_OK` 并 `completed`，tool invocation 为 0。Session/Workspace/admin 三面 reload 均无旧轮重放、重复 write/artifact 或 operational hold，因此 finding 推进为 `Verified`。
- `SESSION-AUTHORITY-PRESENTATION-001` 已完成三个递进 application commits：`d4ae15fd` 建立 resolving/403/404 truthful surface，`57823bcf` 阻止离开 denied route 时重复选择同一 Session，`bbf6d234` 将安全恢复入口收敛到 `/agents`，避免共享 HR Agent 的默认 chat 再选中不可访问 Session。三次均以 mounted regression 驱动，未放宽结构或 bundle 预算。
- 最终 frontend gate 为 **154 files / 1148 tests passed**；i18n 双语各 3993 keys、全部 anomaly gate 为 0；production build 与 AgentDetail/vendor bundle budgets 通过，AgentDetail 恰为 2900 行。Weekend/atomic architecture **24 passed**，manifest `valid=true` / denominator `96` / hash `d320edce…`。
- exact `bbf6d2340afe593b44f740fabfa178d126b5beca` 已部署到 backend `4ad99e93-d3be-48c9-be8d-0107dff44f82`、backend-api `8aa5ccbc-fe9d-4da2-bb39-f16497de044f`、frontend `638da152-1ef6-444c-bcd8-4dd00fa0296d`，三者均 `SUCCESS`。signed-in 生产负向 route 只显示“找不到此会话”与“返回数字员工”，不显示 `Read-only · User`、完成、运行错误或会话交付物；返回后 URL 为 `/agents` 且无 stale alert。合法 MAPLE Session hard navigation 仍显示 marker、终局、3/3 todos、一个 artifact 和 0 running/0 waiting，finding 推进为 `Verified`，P29 不写 PASS。
- P29-PADMIN 正向只读检查在 deployed `bbf6d234` 的 `/enterprise/runtime-budgets` fresh 复现 `RUNTIME-GUARD-PRESENTATION-001`：heading/badge 为“被保护的任务 0”，但同一列表展示 5 条 `active` run、5 个暂停按钮，并把每条原因写成“系统保护机制已介入”。source path 证明 frontend 在无 protected run 时把 `runs.slice(0, 5)` 塞入 protected section，backend `_user_reason('active')` 又落入 intervention 默认值。
- application `6a6695e88d915a0e37b44e64dcdfe5bdd90a9454` 保留 recent active run 与暂停控制，只把 fallback section 明确命名为“最近运行”并将 active API 原因改为“运行正在正常进行”；真正 protected 状态仍优先使用原 section。RED 两条 → focused backend 8 / frontend 6、相邻 backend 87 / frontend 142、完整 backend **8439 passed, 2 skipped, 1 warning**、frontend **154 files / 1149 tests**；i18n 3995/3995、Ruff/format、build/budgets、24 architecture tests 与 96 manifest validate 全绿。
- `6a6695e8` 已部署到 backend `cdef3ce1-85e6-4662-a5aa-a6fb9793a21b`、backend-api `2261b169-3c8a-4c3e-a42b-7a1239b2b8e2`、frontend `feb46b17-e017-457a-8c09-b94065730ce1`，均 `SUCCESS`。signed-in hard navigation 后 DOM 为“最近运行 5”，5 条 active run 都显示“运行正在正常进行 / 等待当前运行完成”并保留暂停按钮；旧 intervention 文案与 protected heading 均不存在，finding 推进为 `Verified`，P29 不写 PASS。
- `LLM-PROBE-AUDIT-001` 从 deployed `6a6695e8` 真实复现：MiniMax/DeepSeek/GLM health Test 会产生外部 provider/token/cost effect，但 backend 无 canonical audit writer，audit UI 只消费 agent-bound legacy log。`cc6e7262` 在 effect 前 durable commit started audit、终态 durable commit completed audit；selected-tenant canonical audit 与 legacy log 由 platform admin UI 合并消费，且只保存安全字段。
- application `cc6e726218bd491120f942edfa91e51d2d167ff4` 的 RED/GREEN/full gates 全绿：backend focused 6 / selected-tenant file 22 / full **8443 passed, 2 skipped, 1 warning**；frontend adjacent 34 / full **154 files / 1149 tests**；i18n 3995/3995、9 node tests、Ruff/format、production build/budgets、24 architecture tests、manifest validate 与 diff check 通过。
- 首次 `cc6e7262` Railway 打包因手工扩展错误 full SHA 且脚本未 fail-fast，backend `446bb56e…`、backend-api `771d44b3…`、frontend `7f139625…` 三个空上传均立即 `FAILED`，未替换运行实例。恢复时使用 `git rev-parse HEAD`、`set -euo pipefail` 和 archive 内容校验；正确 deployment backend `f619e4a9…`、backend-api `7edd592d…`、frontend `beb9cd36…` 均 `SUCCESS` 且绑定 exact full SHA。
- post-fix production 只对 GLM Test 点击一次；probe `a0f1be98-27bd-4d69-9bde-247b57c6b16c` 形成 `05:21:32` started / `05:21:36` completed，`zhipu/glm-5.3`、`max_tokens=16`、`success=true`、`latency_ms=3411`。hard reload 后 started/completed 各一、probe ID 恰两次、无 raw API key、无第二次 provider call；finding 为 `Verified`，P29/P33 均不写 PASS。
- `AUDIT-DEFAULT-DISCLOSURE-001` 从 exact deployed `cc6e7262` 真实复现：`/enterprise/audit` 默认 DOM 含 `session_id=110`、`job_id/issues=94`、`reason=41`、`agent_name=77`、raw provider error 90；legacy/canonical API、raw-detail search、CSV 与 selected-tenant export/chain 共用同一根 authority/disclosure 断点。
- application `b23e94210e7e9523bafc3b591b35db8fc2762224` 以共享 server summary projection、CSV/search boundary、selected-tenant RLS pinning 和 frontend exact allowlist 修复，保留 canonical raw evidence/hash；RED backend 4 + frontend 1，GREEN backend 30 + frontend 3，full backend **8448 passed, 2 skipped, 1 warning**、frontend **154 files / 1149 tests**，i18n/build/budgets、35 architecture tests、manifest 与 diff check 全绿。
- `b23e9421` 已部署到 backend `03d0919e…`、backend-api `b0bb7ca3…`、frontend `0dd299d8…`，三者均 `SUCCESS` 且绑定 exact full SHA。production audit hard reload 后仍有 400 条 summary，GLM probe correlation/provider/model/success 保留，六类业务/取证 payload counts 全部为 0；跨用户 Session hard navigation 仍 truthful not-found、无 workbench/artifact/body，finding 推进为 `Verified`，P29 不写 PASS。

## 最近验证

- 上一控制层：Weekend 文档/CI manifest/Issue contract 15 tests passed；Kimi 与 zCode stateless read-only correction smoke 均 `exit=0`、零 worktree diff。
- 本轮 manifest：`python3 backend/scripts/weekend_rc_gate.py validate` → `valid=true`、denominator `96`、semantic verdict 未计算。
- 本轮控制冻结 tests：production manifest、文档组、既有 atomic CI manifest 合计 **18 passed**；Ruff check/format 与 scope-limited `git diff --check` 通过。
- task-state resolve 仍指向本文件；Goal 在三次 action-gate 等待后机械标为 `blocked`，owner 新输入已恢复实际执行；当前 deployed application 与 push checkpoint 为 `b23e94210e7e9523bafc3b591b35db8fc2762224`。工作树只在本验收文档/证据之外保留 owner 既有 dirty/untracked 路径，未触碰或纳入提交。
- D0 历史部署 checkpoint：backend/backend-api/frontend 曾在 `eb61d468221aa22a4f22c1d96353baadef3b51e6` 为 `SUCCESS`，随后产生 `SESSION-CONTEXT-001`；该 checkpoint 已被 D1/D2/D3 应用提交取代，不代表当前 production freshness。
- production `P01`/continuation probe：Session `59257e7a-960b-459a-9652-2ff39be117ee`，两次 run 均 `completed`；第二轮产生 `SESSION-CONTEXT-001`，因此不计入 NPTCR PASS。
- 宽松 worker contract：18 个 architecture tests 通过；Ruff check/format 与 `git diff --check` 通过；旧 cancelled receipt 被正确分类为 `interrupted`、仍可 review、且不计算 semantic verdict。43200s window-level preflight 为 `ready=true`、effective/internal/outer 全部一致、无 warning。
- `uv run pytest -q tests/services/test_session_semantic_history.py` 实现前 RED → **12 failed**，实现后真实 PostgreSQL → **12 passed**；日志中的 `runtime_budget_runs.root_external_principal_id` / `invocation_spans.decision_id` 缺列来自 helper 未注入 fixture 时触达的本机旧开发库，不是 Testcontainers schema，也未被当作通过证据。
- 完整 backend gate → **8413 passed, 2 skipped, 1 warning**；两个 skip 已精确核对为本机 OfficeCLI binary 缺失和 DingTalk guide 无静态 declared tools，Docker/Testcontainers 未整体 skip。另有 runtime 124、commands 46、terminal outcome 16、branch 14 和 Weekend/atomic architecture 18 条定向回归通过；Ruff、format、diff check 与 manifest validator 均通过。
- D1 push checkpoint：GitHub 首次 push 因 TLS `SSL_ERROR_SYSCALL` 失败且远端未变；同一 `d0c9fffd` commit 在只读连通性核验后以 HTTP/1.1 重试成功。该历史 HEAD 已被 D2/D3 application commits 取代。
- Railway 最终 readback：backend `ce0bdbf4-c8b6-4cd3-bbe2-77e74a75ca2e`、backend-api `ef4f7c81-b8cb-44d8-bbd7-37499e1765fb`、frontend `f6932ba1-9f7e-4b61-8b38-54ae709ba278` 均为 `SUCCESS`，三者 deployment message 均绑定 exact application commit `d0c9fffd1ca4995ddea6d367e04e206e973560d5`；backend `/api/health` 返回 `status=ok` 且 `runtime_control_bus.last_error=null`，frontend `/` 返回 `HTTP 200`。这只关闭部署原子，不升级任何 Journey verdict。
- production 根因复验：fresh Session `3ce68041-ccc4-4d4e-b729-ec9ace46d222`；run `71cffdb6-ef6b-53fa-9a63-ea57ac98349f` 的 assistant 只输出 `ACK-FIRST`，run `40c3e678-0ca9-59f8-8abd-e65ef64a4cf9` 在自身输入没有 marker 的情况下输出 `HIVE-CANONICAL-Q7M4-83NP NO_TOOL`。operator workbench 的 `hive.session_semantic_history_receipt.v1` 为 `complete`，truth source 为 `chat_transcript_events+session_model_results`，`message_count=2`、`user_checkpoints=1`、`committed_provider_messages=1`、`mechanical_message_limit_applied=false`、`held_items=[]`，且排除了 P02 current-run input。该证据只把 `SESSION-CONTEXT-001` 推进到 `Verified`，不构成完整 P01-MAIN/P02-STREAM PASS。
- P01-MAIN pass-1 Attempt 1：fresh Session `d1a2c63f-7082-424d-a9f3-a3330398e371` / RuntimeTask `ff9536bd-39fa-5bf3-bd02-f07aa6fb0e81`；GLM-5.3 真实完成公开 3 步计划、3 个 Work Ledger todo、只读失败恢复、受治理 `write_file` 和 `read_file`，并生成可在产品 Workspace 预览的 `workspace/WEEKEND-RC-P01-MAIN-PASS-1.md`。终稿生成前 provider 返回 busy；RuntimeTask `failed` / `result_summary=provider_error`，UI 为 typed `失败`、`可重试`、0 running/0 waiting，work ledger 2 completed + 1 in_progress。因此 Attempt 1 如实记 `FAIL`，不写 evidence PASS；一次“重试本轮”已从 canonical user event 建立 edit branch Session `ef9d6498-f4dc-49c1-a566-6446e220f0ef`，不得自动外推为 recovery PASS。
- P01-MAIN retry recovery：edit branch Session `ef9d6498-f4dc-49c1-a566-6446e220f0ef` / RuntimeTask `03419d5f-6166-479d-ad02-d929759c57df` 在无任何 tool call、Work Ledger 或 artifact write 的情况下被标记 `completed`，final 错称用户只发送了「1」。operator transcript 证明 seq `1` 的 `human_input.accepted` 是完整 P01-MAIN 原 prompt；workbench receipt 证明语义历史为合法 typed `empty`；但 seq `4` 的 `result_commit.prepared.bound_input_ids=[]`。live wiring 进一步确认 branch HTTP entry 直接调用 legacy `start_web_chat_run()`，未经过正常 `/runs` 使用的 `submit_live_human_input()` Session V2 admission/dispatch，因此 current input 没有进入 round binding。这一独立根因记为 `SESSION-RETRY-INPUT-001`，不回退 `SESSION-CONTEXT-001` 的 verified 状态。
- `SESSION-RETRY-INPUT-001` 本地 fix candidate：content-bearing branch modes 已统一进入 `submit_live_human_input()`，以 branch Session + mode 派生确定性 input/idempotency key，经 Hook admission 和 Session V2 dispatch 启动 run；`regenerate` 继续复用 canonical copied user prefix，禁止制造重复 HumanInput。RED 阶段 API 与真 PostgreSQL 测试分别证明 legacy bypass 和 branch 无 `SessionTurnInput`；GREEN 阶段长 Unicode retry prompt 成为 round-one 唯一 user message，`bound_input_ids` 精确等于 durable input。定向/cross-domain **139 passed**，完整 backend **8419 passed, 2 skipped, 1 warning**；Ruff check、目标文件 format check、manifest validate 与 `git diff --check` 通过。全仓 format check 仍报告 43 个与本 diff 无关的既有文件，未擅自格式化。
- D2 production retry：Session `b3962147-07cd-4223-8f23-f00193d7735c` / run `76a32f8e-f5d8-5a63-b02a-e591598321e9` 的 round one `bound_input_ids=[1fd5cc5b-8378-5629-8cdc-98fd8250f27f]`；GLM 消费完整 prompt 并创建 3 todos，故 `SESSION-RETRY-INPUT-001` 为 `Verified`。`write_file` 后的 sequence `304/305` 无 terminal pair、ChatArtifact FK rollback、sequence `308` 继续 provider 和随后 429 单独记为 `TOOL-ARTIFACT-SETTLEMENT-001`，不计 Journey PASS。
- D3 local final：核心交叉 **330 passed**；完整 backend 首轮唯一架构 owner-line budget 红点未放宽预算而通过 helper extraction 修复，第二轮最终 **8428 passed, 2 skipped, 1 warning**；frontend **1143 passed**、production build、AgentDetail/vendor bundle budgets 通过；Ruff、format、diff check 和 manifest validate 通过。
- D3 deployment：`HEAD = origin/main = c37fefc56b92e658bfb64a3e79d685249a2a3add`；三服务 deployment IDs `62e4ef56…` / `307f0df7…` / `db6b605d…` 均 `SUCCESS` 且 message 绑定 exact commit；backend health `status=ok` / `runtime_control_bus.last_error=null`，frontend HTTP 200。部署绿不升级 finding/Journey。
- D3 fresh normal probe：owner 确认后只发送一次；Session `0731ec15-c662-4552-9500-3f68f1094f11` / RuntimeTask `c124e51f-c09e-5b0d-9265-38b48ae0db27` 在 GLM-5.3 下 `completed`，attempt 1 / claim version 2。canonical spans 只有 `write_file` 与 `read_file` 两个 tool span且均 `status=ok`；invocation 计数为 write 1 / read 1，两者 `effect_committed`、无 recovery owner。
- D3 canonical settlement：write sequence `121 started → 122 effect_started → 123 tool_call.completed → 124 tool_result.completed`，下一 round 到 `127 result_commit.prepared` 才开始；read sequence `167 → 168 → 169 → 170`，下一 round 到 `173 prepared` 才开始。write terminal pair 共用非空 message ID `07afe8cd-ff96-5c03-b0f1-e54ca9c12462`；目标 ChatArtifact `be17c252-8a97-4782-ae3e-17e05d2f3519` 恰一行，owner ChatMessage 恰一行，对应 terminal outbox 均 `published` / attempts 1 / no error。invocation、event、RuntimeTask 的 reconciliation 计数均为 0。
- D3 artifact / Consumption：目标 snapshot 为 77 B，正文三行无尾随换行，content SHA-256 `2c3f309736338d6185614a50e56875de7fc1092cd239c765b7df1661f7ec07e6` 与期望逐字相等；canonical read tool-result event `24dabf4f…` 的 529 B provider-visible wrapper 完整包含同一 77 B 字节。normal AgentDetail 显示精确 final、一个文件、右栏一个 snapshot artifact、0 running/0 waiting。hard reload 后同一 Session/run/终答/tool pair/artifact 仍为唯一值且无自动 replay；普通「打开」预览显示 heading 与两个 marker 字段。
- D3 read-only production proof：Railway backend 内 `asyncpg` readonly transaction + 精确 tenant `set_config`，仅执行显式 tenant/session SELECT，事务 rollback；未输出 credential、无 DDL/写入/RLS 绕过。`2026-08-30T17:10:00Z–17:12:30Z` 的 backend/backend-api 精确部署日志对 FK / lifecycle persistence / reconciliation 五个错误码过滤均为 0；日志只作反证辅助，happy path 由 DB 与 signed-in UI 共同证明。
- `3482b57a` supported recovery：只对旧 D2 run `76a32f8e…` 点击一次 evidence-backed acknowledgement；canonical sequence `312/313`、published outbox、invocation `result_event_id=null`、旧 run 仍 `failed`、0 新 artifact 共同证明 no-result/no-replay。fresh input sequence `314` 建立独立 run `f8cdd9ac…`，唯一 round 绑定唯一 input、sequence `375` 为精确 `D4_RECOVERY_OK`、sequence `385/386` 正常终局且 0 tool invocation。signed-in Session hard reload 为 prompt/final 各一、0 blocker/running/waiting/Stop；Workspace 目标文件与三个验收字段各一；管理员目标 row 0、error 0。
- MAPLE P01 functional path：从真实 sidebar new-conversation draft 只发送一次，fresh Session `52ddde7f-63bf-44a6-973f-ffb1da06d14a` / run `38381d84-779d-59fe-954d-dd75b2c07079` 在 GLM-5.3 下 `completed`。round 1 保存完整 73-tool bundle 与唯一 input；公开 plan 先于工具。canonical invocation 为 `track_todo=7`、`write_file=1`、`read_file=1`，全部 `effect_committed` 且 result 非空；3/3 todos、一个 owned artifact、七项正文硬标准与用户 snapshot 预览均通过。660 个 outbox 全 `published` / attempts 1 / errors 0；两次 reload 后 input/run/artifact 仍 1、max sequence 660，计时 reload 2.920 s 收敛。server principal 同时核验为 `platform_admin` + user-scoped manage grant，违反 frozen `persona=employee`，所以只写 blocked-precondition evidence、不写 P01 pass-1。
- model health/audit：pre-fix MiniMax success `7623ms`、DeepSeek 单次 `HTTP 402 Insufficient Balance`、GLM success `7575ms`；post-fix audited GLM probe `a0f1be98…` success `3411ms`。audit hard reload 后同 probe 的 started/completed 各一且无 raw API key。immutable evidence：`evidence/cc6e726218bd491120f942edfa91e51d2d167ff4/LLM-PROBE-AUDIT-001-production-verification.md`。
- admin audit default disclosure：post-fix hard reload 仍显示 400 条；`session_id/job_id/issues/reason/agent_name/raw provider error` 均为 0，GLM probe ID 恰两次且安全 model/provider/success 仍可读。denied cross-user route 同时保持 truthful not-found。immutable evidence：`evidence/b23e94210e7e9523bafc3b591b35db8fc2762224/AUDIT-DEFAULT-DISCLOSURE-001-production-verification.md`。

## 当前合成资产登记（待后续清理授权）

| marker | 目标 | 唯一允许效果 | 禁止效果 | cleanup 状态 |
|---|---|---|---|---|
| `D3-SETTLEMENT-C37-8K4P` | 指定私有实验 tenant 的 EventPilot fresh Session | 新建并只读回 `workspace/WEEKEND-RC-TOOL-SETTLEMENT-C37-8K4P.md`；正文只含可识别测试 marker/预期值 | 不修改其他路径，不外发消息，不创建 workflow/trigger/delegation，不读取 credential；write 失败不重试 | `created-evidence-retained`；Session `0731ec15…` / run `c124e51f…` / artifact `be17c252…`。正常路径证据已读取，文件和 Session 保留；cleanup 仍需独立 owner action-time confirmation，不写 cleanup PASS |
| `P01-MAIN-CLEAN-P1-3482B-LARCH-927` | 无效验收操作：普通 `Open EventPilot` 链接仍选择既有 Session，不是侧栏加号 `New conversation with EventPilot` | 已在旧 Session 新建并读回 `workspace/WEEKEND-RC-P01-MAIN-CLEAN-PASS-1-3482B.md`；run 自身成功但违反 frozen fresh-Session input，永不计 Journey PASS | 不修改其他路径，不外发消息，不创建 workflow/trigger/delegation，不搜索外网，不读取 credential；write unknown/failure 不重试 | `invalid-entry-evidence-retained`；Session `b3962147…` / run `ee3703f0…`，terminal completed、write 一次/read 一次、final/artifact 可见；待最终 cleanup gate 清理 |
| `P01-MAIN-PASS1-3482B-MAPLE-581` | P01 功能路径 probe；真实 sidebar fresh Session，但实际 server principal 为 `platform_admin`，不满足 frozen employee persona | 已新建并读回 `workspace/WEEKEND-RC-P01-MAIN-PASS-1-3482B-MAPLE.md`，公开 3-step plan、3 个完成 todo、七项 hard criteria、reload/no-replay 与用户 snapshot 预览均通过 | 不修改其他路径，不外发消息，不创建 workflow/trigger/delegation，不搜索外网，不读取 credential；write unknown/failure 不重试 | `invalid-persona-evidence-retained`；Session `52ddde7f…` / run `38381d84…` / artifact `a8a036af…`，功能路径通过但永不计当前 P01 pass-1；待最终 cleanup gate 清理 |

### D3 探针七原子判定边界

| 原子 | 必须观察到的 PASS 事实 | FAIL / 不得外推 |
|---|---|---|
| Input | fresh Session 接受完整 marker、唯一路径和 no-retry 边界；round 绑定该 durable input | UI 里出现文字但 provider 未绑定输入 |
| Authority | principal/tenant/Agent/session 与实验范围一致；只访问 Agent-owned `workspace/` | 跨 tenant、credential、外部消息或未登记路径 |
| Execution | `write_file` 成功后只执行一次 `read_file`；每个 invocation 的 `effect_started → tool_call.completed → tool_result.completed` 均在后续 model round 之前有序落盘 | effect 已发生但 terminal pair 缺失、write 自动重试、terminal 前进入下一 round |
| Evidence | write 的 canonical `tool_result.message_id` 非空；同 message ID 只对应一个 ChatMessage owner、一个目标 ChatArtifact 与一个 outbox/event identity；无 `needs_reconciliation` | 仅凭文件存在、日志或兼容消息宣称 settlement 完成 |
| Recovery | hard reload 后相同 Session/run/event 顺序稳定，无重复 invocation、effect、artifact 或 final；旧 D2 failure 保持不自动 replay | reload 生成第二次 write、第二个 artifact 或掩盖 failure |
| Consumption | 普通 AgentDetail chat 只显示一张合并后的 write tool/artifact 卡，可预览精确 marker；read 与 final 均可读 | canonical/compatibility 双卡、0 artifact、附件丢失或 operator-only 可见 |
| Acceptance | exact c37 normal-path 与 exact 3482 supported-recovery deployment identity、真实 selected provider、normal/reload/no-replay/fresh-turn 证据全部成立，finding 才可到 `Verified` | health、Railway `SUCCESS`、本地绿测或 bounded finding probe 直接升级 Journey PASS/Closed |

2026-08-31 owner action-time confirmation 后，Codex 从 fresh EventPilot Session 只发送一次已登记探针；发送后输入框清空并绑定 Session `0731ec15…`，未发生重复 click/send。先观察到 write/read/final，再等待 RuntimeTask 与 artifact settlement 进入终态；证据见 `evidence/c37fefc56b92e658bfb64a3e79d685249a2a3add/TOOL-ARTIFACT-SETTLEMENT-001-normal-revalidation.md`。

Cleanup wiring 只读核验：普通 `workspace/` 文件有受治理的 `delete_file` 与文件 API/UI 删除入口，但两者都会物理 unlink；tool contract 明确标为不可恢复，并通过 `workspace.command.destructive_delete` / `destructive_once` 单次确认治理。当前没有受支持的 move-to-trash/rename 恢复入口，因此探针留证完成后必须另取一次 owner action-time confirmation 才能删除；未确认前保持已登记 synthetic evidence，不伪写 cleanup PASS。

## 唯一下一动作

继续当前 signed-in `platform_admin` 的 P29-PADMIN pass-1：provider health、audit summary、hard reload 与 denied-route recovery 已核验；下一步只读补齐其余 tenant/provider/runtime/compliance API verdict，再对照 frozen evidence contract 判断 pass-1。不得修改模型配置、credential、billing、角色/grant 或业务数据，也不得重试 DeepSeek 402。P29 完整 pass-1/pass-2 前不写 Journey PASS，NPTCR 保持 `0/96`。

## Not Done / Do Not Redo

- production manifest 已冻结；Gate 0 事实已落盘，但没有任何可计分的 pass-1/pass-2，NPTCR=0/96，Evidence Coverage 尚未成立。
- Issue #4 前两次派发未形成业务 diff；Attempt 3 被拒绝、Attempt 4 已停止。Session history、retry input、tool-artifact settlement、unknown-effect recovery admission、Session authority presentation、runtime guard presentation、model probe audit 与 admin audit default disclosure 的单 Codex application commits 均已 push/deploy；七个已复现根因均为 production `Verified`，但完整相关 Journey 双遍仍未执行。
- MiniMax/GLM bounded provider call 已成功；DeepSeek live call 以 typed `402 Insufficient Balance` 阻塞。三者都没有完成 P33 frozen compatibility task，DeepSeek 不得在未获 billing/credential 授权时重试。
- Goal 机械状态已因三次 action-gate 等待变为 `blocked`，但 owner 新输入已恢复执行；不得因 Goal 状态、历史 PASS、Railway/health 绿或候选单测绿而升级任何 Journey verdict。
- 不触碰 pre-existing `.ultra/.runtime/compact-snapshot.md`、`bp-kingdee/`、`output/`、root `package*.json`、`tmp/pdfs/` 等用户工作树内容。
- 不把 archive 中某个历史 `PASS` 自动迁移成当前 aggregate `Closed loop`。
