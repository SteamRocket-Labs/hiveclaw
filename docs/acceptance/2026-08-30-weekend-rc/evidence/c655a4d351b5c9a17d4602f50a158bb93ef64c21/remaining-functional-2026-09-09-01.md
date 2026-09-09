---
document_id: weekend-rc-remaining-functional-2026-09-09-01
owner: Codex
status: in_progress
authority: supporting-evidence
last_reviewed: 2026-09-09
source_commit: 6c3e1a11
verification_status: partial-not-complete
---
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

## 09:44—10:03 部署与真实复验

- `b153d8cd79e99b7a21d4368754391a19c3d67f72` 已提交推送，仅包含上述修复与证据。09:48 三服务部署 SUCCESS：backend `a70e7707-ae86-4856-8725-f594f8a0c3bf`、backend-api `d6fc6c51-b720-4335-87ec-4d9642b6ef17`、frontend `642b8bdb-73cc-4d14-8402-0c8986aebcaa`。两后端运行源码与精确 archive 均为1058文件/source SHA256 `3f23136c948b484670d825c01d61aaaa8f4891b5d353fda986bc82f49e5299ee`；health ok/strict app_rls，启动时实际代码执行探针3项通过。旧候选未进入发布。
- 正式推荐拒绝/启用入口只恢复原无效果失败 trigger `797e51a5…`，保留定义hash、用户、max_fires1；新推荐 `1ca0566b-65ab-43ae-a433-7d1db6aee546`。09:51真实 wrapper `2070589a-bebc-447b-b71f-052d081a69a2` 成功启动 registered run `726a11ff-cfb6-5f0c-99ce-ca7ffc6b192a`，Session `fc397e2d-0989-5570-a130-0c3c2c476b09`；证明headless租户启动缺陷消失，**不证明计算完成**。
- 09:56读取原始结果，两个compute叶输出均为provider限流错误，却被共享subagent入口记为 completed，进而journal记done；tokens均0。审批checkpoint `7013c3e8-3455-4897-bc5f-e6c2a1e254ba` 未批准，无实际51/57结果、无最终文件。10:03正式GET确认trigger fire_count1/max_fires1且disabled。保持失败证据，不批准坏结果、不重复整次计算。
- fresh owner Session `e45c4db2-c737-410e-a876-0ac471e4397e`，Agent `9fd0e9f9-2293-5514-b7f9-0c1df5ce2fe3`，marker `WRC-REMAINING-20260909-ONCE-CEDAR-926`；09:50正式页面仅提交一次10:10唤醒/64计算草案要求。原页面最终显示MiniMax繁忙/失败，无草案确认、无trigger、无待批工具调用；未点击重试，故新permission修复尚缺该业务路径生产通过证据。计划时间到期后不得复用旧时间窗口。
- 新共享根因修复在干净 `b153d8cd` worktree：subagent消费已有 `terminal_reason`，仅turn_stop映射completed，失败保留内容/token/typed错误，T0 seal和Stop hook状态一致，既有memory成功判定不再接受失败。未用自然语言扫描、未换模型或工具。13种终态反例修复前12failed/1passed；修复后相关117passed。新增真实spawn→workflow leaf连接检查，不把mock provider当生产成功。
- CI `34300370707` 的15条机械journeys通过；前端1297passed/1failed，唯一失败是AgentDetail新增wiring导致2901行超过既有2900行上限。仅收敛相邻函数签名排版，未放宽上限；修正后全部168文件/1298前端测试通过（8.87s）。backend完整CI当时仍运行。
- 已询问是否允许剩余合成验收临时使用现有GLM-5.3并结束后还原；未获答复前保持MiniMax，不将替代模型结果记成MiniMax兼容性通过。

10:04收束：真实spawn→workflow leaf连接检查及RLS登记/指纹4 passed（112.65s），本次没有指纹漂移、不需更新白名单常量。Ruff/format/diff检查通过。原坏结果run经正式cancel返回200/killed，审批未批准；保留失败叶和原回执，不篡改历史done、不自动repair或重放。

## 10:11 最新交付与未完成项

- `6c3e1a1151cdab7608ec12b20211c5083f7f8519`已commit/push，包含共享subagent终态修复、真实连接回归、前端行数收敛及记录。最初Railway连接TLS失败时尚未上传；逐服务确认仍旧deployment后才重试同一archive，没有不明状态重复发布。最新三服务全部SUCCESS：backend `e7772aee-20f4-436b-b56e-d7377b5967cb`、backend-api `3be5f674-95d6-46ac-8401-1e7f0e9de13b`、frontend `3bbc2075-f743-4d7d-a677-2cf75feaab8c`。两后端运行与archive同1058文件/source SHA256 `f510c09697d945c4a46325abfd9e7583745ebc1004875f3bae21ad3373e286bb`，backend health ok/strict app_rls、frontend200；最新sandbox实际探针通过。旧terminal candidates held仍300，不称运行时零积压。
- CI `34301808749`前端unit/visual/accessibility及15条机械journeys全部success，backend完整harness仍in_progress；本地117项加真实spawn连接/RLS4项与1298前端检查保持上述证据，不冒充完整CI或生产业务闭环。
- 已部署且本地回归完成：headless固定Workflow租户恢复；原Run权限等待/正式live与reload审批卡；shared subagent失败不再伪装completed。仅第一项已验证生产实际启动。后两项真实成功路径因当前MiniMax限流尚未复验，保留界限。
- **未完成：**固定Workflow实际计算、gate/wait恢复/文件/复用；A2A固定流程graph（源码缺失，尚未实现）；Dynamic并行/等待的完整恢复；once/schedule/event成功交付；Local最终result；Office最终模型卡片；Goal自动两轮/计费；完整Growth/J1–J4、Hooks/Skill/MCP生命周期；角色/转移/离职与最终D双遍/故障/rollback/cleanup。直接A2A成功不替代固定graph；当前未开启新graph开发分支或把设计文档记成实现。
- 模型选择、Hive Connect批准源与既有临时管理员/政策问题仍待owner答复，不擅自修改。GLM此前06:18记录为周/月额度耗尽、provider提示09-13重置；这不是10:11实时可用性证明，不能承诺换用GLM即恢复。模型切换即使获准也须先核实目标可用性，失败不得盲重试。

## 下一步

10:37 owner 已批准 MiniMax / 新 Ling 切换及 Local 安装；10:46 明确批准准确来源：Skill 为 `https://github.com/SteamRocket-Labs/hiveclaw`，CLI 为 `@hiveclaw243/hive-connect`。原 CLI 仓库没有 Skill，本地主仓库 `.agents/skills/hive-connect/SKILL.md` 也未被 Git 收录，故将现有指南原样放入可分发的 `skills/hive-connect/SKILL.md`，结构验证通过。npm 正式注册信息版本0.1.9、仓库 `rocky2431/hive-connect`；两后端批准来源配置已提交，等待生效验证和保留旧绑定的升级。

模型正式 API 现有且启用 Ling 与 MiniMax；MemberAnalyst 的模型绑定为空，初始 Session `8e8bc8e0-48b1-4da5-a66b-58ccd42d7230` 在模型调用前失败。按授权 PATCH 绑定 Ling 后，新 Session `7c7ab098-e40a-4ff6-96fc-8c0ad88b1d94` / run `fc03c252-a858-5290-9093-ae609858fc1b` 使用真实 `openrouter / inclusionai/ling-3.0-flash-sante:free`，已实际调用 execute_code，canonical 终态提交；工具回执为无输出，尚不作为计算108成功证明。旧取消 run 不复活。

CI `34301808749` 最终后端9253passed/1failed/4skipped，唯一失败是本记录漏 frontmatter；已补齐原有七项必需元数据，相关文档结构检查10passed（0.36s），未改测试断言。前端与15journeys原 success 保持。

继续验证 Ling 的实际结果与固定 Workflow / once 新时间窗口，完成 Local 升级后的真实最终 result。A2A固定graph等实现和其余功能继续按03-current-status原范围保留，不缩减、不宣称全部完成；无新heartbeat或后台Codex任务。
