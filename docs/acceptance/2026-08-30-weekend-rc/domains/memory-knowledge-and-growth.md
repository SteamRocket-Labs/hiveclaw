---
document_id: weekend-rc-domain-memory-knowledge-growth
owner: Rocky / Codex
status: active
authority: canonical-domain-acceptance
last_reviewed: 2026-08-30
source_commit: 45340a3a
verification_status: acceptance-spec-not-execution-result
---

# Memory、Knowledge 与 Growth 验收标准

[返回索引](../README.md) · [Journey Ledger](../04-journey-ledger.md) · [HR/Permissions](hr-identity-and-permissions.md)

## 四类权威

| 层 | Owner | 用途 | 进入路径 | Agent 消费 | 不能混成 |
|---|---|---|---|---|---|
| Agent Memory | 单个 Agent，受 tenant/owner 治理 | 从经历中学习 | Session T0 → LLM reflection/curation → T2/T3 → governed commit | dynamic activation / governed memory tools | 用户文档库、Skill 本体、平台语义文本 |
| Personal KB | 个人 principal | 个人提供并授权 Agent 使用的资料 | 用户上传；Agent proposal 经个人确认；约定多格式 | `search_personal_kb` / `read_personal_kb` tool-only | 原始 context prefetch、当前附件自动入库、Agent Memory |
| Company KB | 企业 tenant / publication authority | 审核后按 ACL 使用的正式知识 | admin direct import；Personal/Agent proposal→review→publish；后台/connector | `search_company_kb` / `read_company_kb` tool-only + ACL/RLS/provenance | Agent 自发布、Personal 原地改 owner |
| Governance / Charter | 公司或平台政策权威 | 强制组织规则和 effect boundary | 受控配置/政策发布 | context/permission/effect gate | 相关性检索型 KB |

## 跨层关系

- [ ] Session attachment 只服务当前任务，除非用户明确“加入个人知识库”。
- [ ] Agent 产物进入 Personal KB 需要个人权威或可审阅 proposal。
- [ ] Personal→Company 创建独立 proposal/publication，保留来源、个人 owner consent 和 provenance；不修改原记录。
- [ ] Company KB 使用不会自动进入 Agent Memory；必须经过 Memory Gate。
- [ ] Memory 不等于 Skill；稳定、复用、eval-backed 的能力证据只能提名 Skill candidate，再独立治理发布。
- [ ] employee offboarding 不静默删除企业 publication，也不把 Personal KB 自动转给 successor。
- [ ] export/archive/forget/delete/legal hold/retention 没有真实合同就标 `Missing`，不能用 UI 文案冒充。

## Agent Memory 与自进化

- [ ] T0 raw evidence 与 cloud transcript ordering 对齐，source refs 完整。
- [ ] T2 tagged segment packages 覆盖所有适用证据；不可读/不适用状态 typed 可见。
- [ ] T3 `episodes.md`、`user.md`、`worker.md`、`capabilities.md` 从 T2 source refs 回到 T0 验证。
- [ ] `soul.md` 的 durable promotion 需要 owner/Platform Gate、版本、hard verification、rollback metadata。
- [ ] LLM 判断、提炼、反思、候选；平台只管来源、权限、去重、审计、试用和 commit。
- [ ] reviewer unavailable 只能 hold/retry；机械 fallback 不能 accept/reject/promote/delete/rewrite。
- [ ] fresh Session 能准确 recall 最新有效版本，不消费 revoked/retired/stale version。
- [ ] owner correction、forget、Skill/Soul rollback 和冲突/过时知识退役可达且可审计。

## Personal KB 输入矩阵

- [ ] employee `/knowledge` 直接上传和状态追踪。
- [ ] Agent 从任务中提议加入 Personal KB，个人可 preview/revise/reject/confirm。
- [ ] PDF、DOCX、Markdown、TXT 以及 UI 当前宣传的其他格式逐一通过；未通过即准确移除/Excluded。
- [ ] parser 输出 canonical Markdown、heading/table/list、page/source location、content hash、conversion receipt。
- [ ] chunk/index 可重建；duplicate import 幂等；semantic input change 产生 typed conflict。
- [ ] job queued/running/ready/failed/cancelled/archive/retry 有唯一 lifecycle owner；worker crash/stale claim 可恢复。
- [ ] search/read 保留 `kb://person/...` source refs；Agent tool sequence 真正发生且答案引用可消费。
- [ ] archive/delete 后无 searchable ghost；retention 与 recovery 明确。

## Company KB 输入与治理矩阵

- [ ] employee 在 `/knowledge` 发起 Personal→Company promotion proposal。
- [ ] employee 在 `/knowledge/company` 读取获准 Company Library。
- [ ] admin 在 `/enterprise/knowledge` 处理 source contract、direct import、import job、preview、proposal、review、publish/retire。
- [ ] 后台 batch/connector 路径有 publisher、credential owner、source ACL、provenance 和 failure recovery。
- [ ] direct import 先产 evidence/document/segments，再显式 create proposal；证据不能自动变成已发布知识。
- [ ] preview 展示 canonical content、segments、citations、sensitivity 和 parse warning；raw parser/index details operator-only。
- [ ] publish 前 Agent 不可检索；publish 后仅 ACL/RLS 范围可见。
- [ ] proposal 可 revise/reject/cancel；reviewer 不可自批；重复提交和 publish retry 幂等。
- [ ] version replacement 保留旧 citation/audit；retire/restore 不留下幽灵结果。
- [ ] cross-tenant 一律 not-found/deny，不泄露存在性。

## 五种必须区分的状态

`denied`、`unavailable`、`empty`、`not-indexed`、`parse-failed` 必须有不同 typed fact、UI 解释、下一动作和 recovery。不得统一显示“没有结果”。

## Growth / Eval J1–J4

| Gate | 必须证明 |
|---|---|
| J1 candidate trial | 同一任务/模型/资源 envelope 下，provisional candidate 启用后优于不启用；保留原始 evidence 和 reviewer decision |
| J2 longitudinal growth | failure recurrence/avoidance、knowledge/Skill reuse、owner negative feedback、rework 随真实后续任务改善；指标只陈述事实 |
| J3 non-regression | 平台/权限/UI 更新后真实 agent primary path 不降级；控制启用仍完成 |
| J4 benchmark | 真实 CLI、workspace、model/task/envelope 与 FreeCode、`hermes-agent` 跑外部硬判据；runtime unavailable 输出空报告而非 fake score |

## 故障与安全探针

- parser crash、malformed file、wrong MIME、duplicate/reordered job、worker restart、permission revoke、index unavailable、citation missing。
- 文档末页/最后 segment 存在决定性证据，不能因 cap/head-tail slice 丢失。
- 文档内 prompt injection 不能改变 system authority、approval、tool policy 或 external effect。
- Personal/Company source bytes 不进入未授权 prompt、DOM、log 或另一 tenant。

## Acceptance

每条路径必须从对应 persona 的真实 UI 进入，在 production 产生 canonical document/segment/index/proposal/publication/tool/citation 证据，并连续两遍由 Agent 在 fresh Session 中消费。代码/API/表存在、daemon healthy 或一次 search 命中均不等于闭环。
