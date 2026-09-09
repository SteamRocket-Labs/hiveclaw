# 剩余功能继续执行记录

## 授权与起点

2026-09-09 owner 要求一次性完成上一轮核对出的真正剩余工作，完成后交付，沿用单一 Codex 与真实功能验收要求。生产仍为 `3ac6e2a1`；HEAD `c655a4d3` 仅追加此前记录。保留主工作区所有旧候选，新的修复在独立干净 worktree 实现。未改变模型、权限、凭据或启动新 heartbeat。

## 09:13—09:21 已核实结果

- 既有合成 member 正式登录成功，identity/tenant/role 与 B4 一致，仍为 member。其 MemberAnalyst owner/creator 同源，模型 ID `dcafa6dc-b410-4e9f-954b-659300ab6c77` 未改变。
- Office 原 Session `e228f140-317f-4c4b-b4c0-02f4c9c43067` 单次正式续发受理 201，run `43d6f3dd-df38-5a7b-9353-cfc2ab87d22b`，仅请求原 DOCX/XLSX 更新合成 marker 并返回最新最终卡片。首次请求 prepared1176→failed1177→runtime_failure1178，明确 rejected/rate_limited；无文件修改效果，不重试。
- 固定 Workflow 新定义 `cad74069-f244-430f-ae13-72c04776ad47`，名称 `WRC-REMAINING-20260909-FIXED-CEDAR-914`，v1/hash `4ec6896d470d6ae020fd77c092b8b9b9ce5876ecd7cfab1ff6a337f8947cb13e`：两叶并行分别计算17×3与19×3→显式review gate→10秒wait→求和写读唯一文件。正式 draft/activate均200。
- 正式 Session `142b7434-fc23-4673-966f-78acfcb8eaab` 与 Plan recommendation `1b7a0195-69cc-4e65-a8bd-822007cf89ad` 创建；通过产品已有“无需另建Plan”推荐拒绝路径，创建一次触发器 `797e51a5-1fb0-422a-96c7-c246f09716e9`，正式201，锁定定义版本/hash、max_fires1、30分钟有效期、明确root user/session；未更改任何全局审批设置。
- 09:17:21实际daemon触发，wrapper task `8e380800-01b7-4a3d-871f-5644eaa1ce4e` failed，workflow trigger session `8eb40fb5-7fee-56a7-8174-6dd3e2e7b4be`；app_rls/read-only/exact tenant与trigger对账得到 `invalid_ref / agent ... not found`，无workflow run。09:17后正式PATCH停用200，阻止继续周期性失败；不是模型限流造成。
- 共享启动入口 `resolve_agent_runtime` 在headless调用缺tenant时依赖请求ContextVar，后台无该上下文，RLS正确过滤了Agent。真实non-owner PostgreSQL新反例修复前1failed/10deselected（13.67s）；修复复用既有单Agent审计tenant resolver，优先保持显式/当前tenant，绝不以lookup覆盖错误tenant。修复后 requester/trigger联合27passed（21.48s），包含错误显式tenant及错误继承tenant仍拒绝。候选尚未提交/部署，不称固定工作流业务通过。
- Local实时为Hive Connect v0.1.7/ac1bee7，现有binding connected、daemon running/online。正式install-guide返回批准skill repo/npm package均空、安装命令空/unavailable。已向owner询问批准升级源，未猜包名、未安装或重启。
- `A2AWorkflowDefinition`、`A2AWorkflowProcessGraphV1`、`A2AArtifactRef` 与独立process graph入口在当前应用源码无匹配；`a2a-workflow-orchestration-design-2026-06-24.md`明确docs-only，integrated plan将其列为第三层。此项不能把直接A2A委派当已实现graph验收，需保持实现缺口与直接协作通过分开。
- CI4318的J13 Slack ingress404/J10一次Team404是真实失败记录；后续相同应用行为c655的15journeys已success，前端success，backend仍执行。尚未将前一不稳定失败归为已修，也未声明全绿。

## 09:23—09:39 once 等待链修复候选

- 按实际 `tool_permission.waiting` → `run.waiting` 与旧 wrapper completed 的差异追踪共享入口：Kernel 已根据 typed ToolDecision 在本轮完整 tool batch 后暂停，但 web orchestrator 的 pause helper 只认 JSON 文本中的 `session_permission_required`，普通字符串解释丢失了同一 typed 决策。新反例修复前失败（1 failed / 130 deselected）；修复直接读取已有 `tool_execution_evidence.tool_decision.outcome`，调用已有 suspended/same-Run 恢复，不改模型、不解析自然语言、不重放过期调用。
- canonical permission event 原来仅有 invocation/approval ID；候选将已核实实际参数、工具、原 Run/Turn、相同到期时间持久化到该事件，由正式聊天页共享 live/hydration projector 消费到现有审批按钮。旧 Run 的 waiting 不能改变当前新 Run，resolved 后按钮消失；不新建另一个审批状态或放开工具权限。删除类操作沿用现有 exact 工具/参数识别，只能 once，页面提示和 resolve API 一致。
- 后端 runtime/permission/terminal/orchestrator 联合 192 passed（19.43s）；新增删除模式保护后 permission/terminal 31 passed（23.86s）。前端新 live/reload 反例先失败，再通过；共享 applier/consumer/hydration 75 passed，补 resolved/stale-Run 后 applier 19 passed；相关页面/socket/consumer 99 passed / 164 按名称过滤未运行。TypeScript、Vite、bundle budget 全部通过。
- RLS 检查实际调用点登记一致，16 passed；唯一失败为 reviewed source fingerprint 漂移，正与干净 HEAD archive 逐 scope 对账，未加 allowlist、未放宽断言。候选仍未提交/部署，生产保持旧版本，不称 once 或固定 Workflow 通过。

09:43 收束：RLS baseline/candidate 均588个精确记录，变化仅为 permission runtime/module、tool runtime/module及 `complete_tool_invocation`、web runtime/module四项；源码差异逐项核对为本包同源审批和卡片修改，查询白名单未变。更新精确指纹为 `5514ac5330f3adcd04d428fdda38a1f3b6a4f2d282724bb36778aba169ab73e9`，相关3项重验通过（90.12s）。共享真实 callback→finalizer 新检查1 passed（1.95s），证明本轮 tool batch 结束前不提前完成、结束后原 RuntimeTask suspended，未创建 assistant final。旧 HEAD `c655a4d3` 的完整 CI `34296978159` 已成功；它不替代本包待运行 CI。

## 下一步

验证并集成固定Workflow根因修复；继续收束once权限等待真实live/reload链，随后集中部署并复验固定定义、触发、gate/wait/产物。Office与Goal等待明确provider可用性；Local升级等待批准源。其余功能仍按03-current-status列出的剩余范围推进，不缩减为只做这一个修复。
