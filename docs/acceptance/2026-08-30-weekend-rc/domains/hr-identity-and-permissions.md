---
document_id: weekend-rc-domain-hr-identity-permissions
owner: Rocky / Codex
status: active
authority: canonical-domain-acceptance
last_reviewed: 2026-08-30
source_commit: 45340a3a
verification_status: acceptance-spec-not-execution-result
---

# HR、身份与权限验收标准

[返回索引](../README.md) · [Journey Ledger](../04-journey-ledger.md) · [Frontend](frontend-and-product-consumption.md)

## Persona 与默认可见面

| Persona | 默认能做/看 | 默认不能看 |
|---|---|---|
| employee | 自己可用的 Agent、Session、Personal KB、获准 Company KB、task result、所需 approval | raw schema/ID/payload、他人 Personal KB、tenant/operator evidence |
| company admin | member/Agent lifecycle、Company KB intake/review/publish、组织权限、budget/audit summary | 员工 Personal KB 正文、私有 Session/model reasoning、平台跨租户运维 |
| platform admin | tenant health、provider/runtime/config/compliance、跨租户 operator surface | 未经 reason/authority 的业务正文或 personal content |
| operator | 明确 reason 下的 scoped evidence、span/event/recovery/repair | 默认员工体验、无限制跨租户浏览、语义决策权 |

## System HR Agent 创建旅程

- [ ] employee 能发现 HR Agent，而不是理解 provision API。
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
- [ ] Agent、Sub-agent、Team member、Workflow worker、A2A peer 权限只会收窄，不因 delegation 扩张。
- [ ] Personal KnowledgeGrant、Company ResourcePermission、Session participant、Agent owner/sponsor/manager 分开建模。
- [ ] read/draft/commit 分离；external send、权限变更、财务、删除、deploy 等在最窄 effect boundary approval。
- [ ] 自然语言不能成为 approval/grant；模型不能批准自己的动作。
- [ ] plan/branch/resume/steer/compact 不改变企业权限。
- [ ] revoke/expire 在 active Session、retry、worker restart 和多 tab 中实时生效并写 audit。
- [ ] denied、approval-required、unavailable、retryable typed 分开，并有 repair/cancel/abandon。

## Agent Detail 信息边界

### Employee 默认

- Agent name/avatar/role/status、能完成什么、当前 Session、近期 deliverables、需要用户决定的事项。
- 可读的 skills/knowledge/permissions 摘要和 owner/contact；不显示 raw tool/schema/runtime policy。
- Memory、Personal KB、Company KB 分层呈现，不合并为“知识总数”。

### Company admin

- owner/sponsor/manager、member access、Company KB bindings、budget、lifecycle、transfer/offboarding preview、audit summary。
- 不读取 Personal KB 内容、私有 Session 或 hidden reasoning。

### Platform/operator

- model/provider/runtime version、span/event、typed failures、repair controls、RLS/worker/queue evidence。
- 必须有 operator reason、scope 和 audit；不能成为普通 employee 默认页面。

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
- admin 能治理 Company assets，但不能默认读取 Personal content。
- Sub-agent/Team/A2A cannot exceed parent delegated scope。
- operator_view 参数不能由普通 principal 自助升级。
- cross-tenant resource 一律 not-found/deny，无 count/title/timing 存在性泄漏。

## Acceptance

使用真实 employee、company admin、platform admin/operator 身份分别走正向与负向路径；UI 与 API verdict 必须一致。每个 grant/revoke/transfer/offboarding 都覆盖 reload、concurrent run、duplicate request 和 audit/readback；不能通过手工 DB 改角色完成。
