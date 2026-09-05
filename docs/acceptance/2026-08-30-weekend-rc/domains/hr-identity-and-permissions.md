---
document_id: weekend-rc-domain-hr-identity-permissions
owner: Example Owner / Codex
status: active
authority: canonical-domain-acceptance
last_reviewed: 2026-09-05
source_commit: 0ce51f049e03c689a440075a5de8a7a9d99c609c
verification_status: acceptance-spec-not-execution-result
---

# HR、身份与权限验收标准

[返回索引](../README.md) · [Journey Ledger](../04-journey-ledger.md) · [Frontend](frontend-and-product-consumption.md)

## Persona 与默认可见面

| Persona | 默认能做/看 | 默认不能看 |
|---|---|---|
| employee | 自己及本公司公开的 Agent；自己的 Agent/Session/Personal KB、获准 Company KB、task result、所需 approval | 别人的私有 Agent/Session/Personal KB；删除 Agent、自授管理角色；工程字段不进入默认任务流 |
| company admin | 本公司全部 Agent、私有会话/文件/知识及企业管理；可任命本公司其他公司管理员 | 其他公司业务、平台级管理、明文密码/密钥/token |
| platform admin | 平台管理；明确目标公司内的全部 Agent/业务内容与公司管理员任命 | 明文密码/密钥/token；不能冒用员工身份或把权限范围扩大给普通用户 |

本表依据 owner PDEC-013，替代旧的管理员不读私有业务内容规则。角色由服务端 canonical User 决定；管理员基本业务读写不需要另取 `operator.inspect` 或手填理由。审计记录真实 actor/tenant/resource/action。`operator` 只是既有技术 inspector 能力/验收标签，不是第四个产品身份，不能绕过员工自己/公开 Agent 边界。公开 Agent 只授权使用，不自动公开其他使用者的私有内容。

### PDEC-013 实现对账结论

2026-09-05 CC 与 Codex 的方案双向挑战已收敛；以下是实现/验收要求，不是代码或生产通过记录。

- 管理员可作为本人向管理范围内的普通可写 Session 发言、启动或引导工作；`role=user` 是模型消息类型，不代表冒用会话主人。保留 Session/文件的原 owner，命令与事件记录真实 actor，页面与模型输入实际消费发送者归属；仅写入无人读取的 metadata 不算完成。既有 read-only session kind 和 exact approval/control lane 不旁路。
- 新 run 的执行、预算和审计主体是实际发起人；给正在执行的 run 补充输入不改写其既有 root principal，每个输入保留自己的 actor。审批/知识模型回执仍绑定它们真实的执行主体。Session 的已授权结果消费按 Session/resource scope 判断，不能为了显示结果把管理员伪装成员工，也不全局放宽 RuntimeTask/AgentRuntime 权限。
- 外发收件人仍来自原本已授权的明确 delivery target，不因管理员介入而自动改成管理员或扩大接收范围。预算的 requester、审计 actor、Session owner 与 transport target 不能混为同一字段。
- 授权必须贯穿 API 的已加载 Session 检查、durable command、admission/dispatch/recovery 与文件 root-session 检查。平台跨公司恢复时重新读取真实 DB 身份/角色，不能依赖请求内临时 tenant 覆写；降级、停用或撤权返回现有 typed 恢复状态，不卡成无限重试。
- 人类管理员对已纳入公司管理的私有知识内容有角色来源的业务权限，不另索普通 ResourcePermission。现有 source ACL 包含 Hive 自建/提交的记录，不能全部假定为第三方禁令；来源/证据完整性与凭据引用投影保留。人类 role-admin 分支必须与 AgentRuntime 实际主体分开，不能只检查 accountable_role 就扩大 worker 权限。
- `export_policy`/`legal_hold_policy` 的 Company KB 描述字段当前没有执行消费者，不能宣称其已提供保护，也不为它们新增无消费者的通用规则引擎。实际下载/导出/删除入口仍按既有可执行合同、明确来源义务和凭据边界逐一验收；遇到真实宣称而未执行的限制，修到对应效果边界，不用假定的限制阻塞所有管理员业务读取。

## System HR Agent 创建旅程

- [ ] employee 能发现 HR Agent，而不是理解 provision API。
- [ ] 公司管理员及平台管理员能在合法公司上下文进入同一 HR 创建流程；不能因旧 platform-admin 拒绝规则反复提示重试，未选择公司时有可发现的选择入口。
- [ ] HR 通过普通对话收集角色、职责、目标、权限、知识、工作方式和约束。
- [ ] model-authored blueprint 以用户决策卡呈现；schema/config/source evidence 默认折叠。
- [ ] `Request changes` 在卡片内收集修改；`Reject` 有清晰退出；`Confirm` 绑定 exact blueprint/hash。
- [ ] confirm 前零 provision/permission side effect；confirm 后进入唯一 HR RuntimeTask。
- [ ] provisioning 每个 step durable、幂等、可重试；最终状态 ready/incomplete/failed 诚实可消费。
- [ ] 新 Agent 出现在 employee rail/detail，identity/owner/status 正确。
- [ ] 新 Agent 在同一权限框架完成首个 Company KB 或真实业务任务。
- [ ] provider failure、ambiguous delivery、reload/retry 不重复创建 Agent。

## Agent→HR handoff

- [ ] 普通 Agent 只能提出受治理 handoff，不能直接 provision、授权或批准自己的请求。
- [ ] handoff 绑定 requester principal、source Agent/Session、requested role、reason、allowed context 和预算。
- [ ] HR 与 employee 的澄清/预览/确认仍是 canonical creation flow，不开第二条快捷路径。
- [ ] deny/unavailable/expired 可恢复，原 Agent 继续无关任务。
- [ ] completion 回到 source Session，并带新 Agent identity 和下一动作；不得泄露内部 blueprint/provision IDs。

## 权限模型

每个动作按以下交集裁决：

```text
principal × tenant × Agent × resource × action × session mode × approval
```

- [ ] 前端隐藏按钮不是后端授权；所有 API/tool/effect 重验 server-derived authority。
- [ ] 两类管理员可读取管理范围内员工 Agent 私有会话、文件与知识，并管理公司权限；普通成员的 API/直接 URL 均不能删除 Agent、自授管理员或跨公司。凭据在响应、下载/导出和错误路径都不明文展示。
- [ ] Agent、Sub-agent、Team member、Workflow worker、A2A peer 权限只会收窄，不因 delegation 扩张。
- [ ] Personal KnowledgeGrant、Company ResourcePermission、Session participant、Agent owner/sponsor/manager 分开建模。
- [ ] read/draft/commit 分离；external send、权限变更、财务、删除、deploy 等在最窄 effect boundary approval。
- [ ] 自然语言不能成为 approval/grant；模型不能批准自己的动作。
- [ ] plan/branch/resume/steer/compact 不改变企业权限。
- [ ] revoke/expire 在 active Session、retry、worker restart 和多 tab 中实时生效并写 audit。
- [ ] Local Agent 的私有会话也覆盖管理员 list/read/message、reload与浏览器事件订阅；平台管理员在合法公司上下文且无显式 `AgentPermission` 时仍走 canonical 角色权限，不能把 legacy `manage` access level 当管理员身份。仅带 session id 的入口从既有会话恢复 Agent/tenant 范围；保留真实管理员actor及原host owner，公开Agent不公开其他员工会话。连接管理与daemon凭据仍绑定具体Agent/公司/host，不因管理员业务权限把机器凭据升级为平台权限；角色撤销在真实消费端生效。
- [ ] denied、approval-required、unavailable、retryable typed 分开，并有 repair/cancel/abandon。

## Agent Detail 信息边界

### Employee 默认

- Agent name/avatar/role/status、能完成什么、当前 Session、近期 deliverables、需要用户决定的事项。
- 可读的 skills/knowledge/permissions 摘要和 owner/contact；不显示 raw tool/schema/runtime policy。
- Memory、Personal KB、Company KB 分层呈现，不合并为“知识总数”。

### Company admin

- owner/sponsor/manager、member access、Company KB bindings、budget、lifecycle、transfer/offboarding preview、audit summary。
- 可按任务需要查看本公司员工的 Personal KB、私有 Session/文件及 Agent 知识；保留资源 owner/provenance，不把管理读取变成所有权转移。
- 公司后台能直接返回 App，不退出登录、不丢合法公司上下文。

### Platform admin 与技术 inspector

- model/provider/runtime version、span/event、typed failures、repair controls、RLS/worker/queue evidence。
- 平台管理员同时具有目标公司的业务管理入口。普通业务访问以角色/资源范围授权；技术 inspector 仍精确校验资源、动作与审计，不因 client 参数或旧 grant 给员工扩权。工程细节不能占据默认员工任务流。

## Ownership transfer 与 offboarding

- [ ] admin 从真实 member 页面 preview 影响；preview 绑定 exact owned Agent 集合，漂移返回 typed conflict。
- [ ] successor 只接收明确转移的 Agent owner/企业责任；creator provenance 不改。
- [ ] Personal KB、private Session、跨 Agent Memory、个人 connector identity 不自动转移。
- [ ] refresh token、direct Agent/resource permission、knowledge grant、external principal、Local Agent/bridge、pending approval 和该用户拥有的非终态 RuntimeTask 原子撤销。
- [ ] active execution 及时 cancellation/revocation，不留下幽灵 effect。
- [ ] request id/replay 幂等；重复/response-lost retry 不二次转移或取消。
- [ ] 停用用户不能从旧 tab/refresh token 继续动作。
- [ ] account recovery、Personal KB export/archive/delete、private Session retention、Company publication provenance/legal hold 有明确合同；缺失则标 `Missing`。

## Negative authority matrix

- employee A 不能发现/读取 employee B Personal KB 或 private Session。
- employee 不能直接 publish Company KB、provision Agent 或 grant 自己权限。
- admin 能读其管理范围内的私有业务内容；company admin 不能跨公司，所有角色都不能获取明文凭据。
- Sub-agent/Team/A2A cannot exceed parent delegated scope。
- operator_view 参数不能由普通 principal 自助升级。
- 无跨公司权限的 principal 访问 foreign resource 得到 not-found/deny，无 count/title/timing 存在性泄漏；平台管理员的已授权目标公司业务访问是正向用例，不再冒充越权负向。

## Acceptance

使用真实 employee、company admin、platform admin 三种身份，并保留既有技术 inspector journey ID 分别走正向与负向路径；UI 与 API verdict 必须一致。每个 grant/revoke/transfer/offboarding 都覆盖 reload、concurrent run、duplicate request 和 audit/readback；不能通过手工 DB 改角色完成。旧 observer/private-deny evidence 只作历史，按 PDEC-013 重验；96 条分母、评分与非角色旅程不变。
