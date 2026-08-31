---
document_id: weekend-rc-domain-frontend-product-consumption
owner: Example Owner / Codex
status: active
authority: canonical-domain-acceptance
last_reviewed: 2026-08-30
source_commit: 45340a3a
verification_status: acceptance-spec-not-execution-result
---

# 前端与产品消费验收标准

[返回索引](../README.md) · [Journey Ledger](../04-journey-ledger.md) · [Single Agent](single-agent-and-session.md) · [HR/Permissions](hr-identity-and-permissions.md)

## 设计基准

### Codex Desktop 为主

- 内容优先：用户任务、Agent 判断、所需决定和 deliverable 是视觉主线。
- 过程克制：同一 turn 只有一个 process owner，工具细节默认折叠。
- 状态诚实：accepted/working/waiting/failed/completed/recovery 不互相矛盾。
- 恢复自然：reload/resume/rewind/branch/Retry 不要求 raw ID 或控制台。
- progressive disclosure：普通用户、admin、operator 逐层增加机械证据。

### Letta 只补多 Agent 外壳

```text
Agent rail（头像/状态，可折叠）
  -> 当前 Agent sidebar（功能入口 + Sessions）
    -> 主 Session / Workbench
      -> 右侧 contextual inspector
```

不复制 Letta 的 Memory、Secrets、Working directory、Connect Models IA。Agent rail 只承载持久数字员工，不混入 Sub-agent、Team member、Workflow run。

## Agent rail 与规模

- [ ] 1、5、20、50+ Agent 下搜索、分组、favorite/recent、keyboard navigation、状态更新稳定。
- [ ] active Agent 明确；切换期间旧 Session response/approval/artifact 不能落入新 Agent。
- [ ] avatar/status 有 accessible name；rail collapse 后仍可发现和定位。
- [ ] status 只用用户可理解的 active/needs attention/offline 等，不展示 daemon/runtime 内部态。
- [ ] Agent sidebar 只显示该 Agent 的 Sessions、Memory/Knowledge 摘要、Schedules、Connections、Skills 等获准入口。

## Agent Detail

- [ ] 默认展示角色、能力、当前任务、最近 deliverables、所需决定和可用入口。
- [ ] employee 不看 model row id、token raw accounting、tool schema、runtime policy、internal error code。
- [ ] Memory、Personal KB、Company KB 分层，标 owner/authority；不合并一个模糊“知识”。
- [ ] Sub-agent/Team/Workflow/A2A 以当前 Session context 呈现，持久 Agent 才进入 rail。
- [ ] admin management 和 operator evidence 不占 employee 默认阅读流。
- [ ] empty/loading/success/failure/needs decision/recovery 全部有明确下一动作。

## Session 与流式呈现

- [ ] accepted feedback、single process card、streaming Markdown、terminal final、failure/recovery 均遵守 [Single Agent](single-agent-and-session.md) 合同。
- [ ] live frame、REST replay、WebSocket backfill 和 hard reload 结构同构。
- [ ] canonical user/assistant/tool identity 去重；同一 run 的 compact/hyphen UUID 表示共享 owner。
- [ ] terminal final 后 stale active observation 不生成第二 process row。
- [ ] historical blocker/runtime activity 不覆盖 current turn header。
- [ ] long Session 使用真实 virtualization/stable-tail，不吞历史或跳 scroll position。
- [ ] console 无持续 error、unhandled rejection、raw provider response 或 retry storm。

## Artifact 与文件

- [ ] 输入 attachment、Session temp file、Personal/Company KB asset、Agent deliverable 是四类对象。
- [ ] upload 不自动永久化或跨 authority；每类有 owner/ACL/retention/provenance。
- [ ] deliverable 支持 preview、download、version、source/citation、permission denial、reload/reopen、archive/retire。
- [ ] PDF/DOCX/Markdown/TXT 等 UI 宣传格式逐一通过；未验收即移除或准确 `Excluded`。
- [ ] conversion/preview failure 不显示假成功；原 bytes/hash/receipt 可追溯。

## Inbox 与异步返回

- [ ] Notification/Approval 是完成、失败、clarification、permission change 的唯一聚合入口。
- [ ] deep-link 定位 exact Agent/Session/item；read/unread/dedupe/expiry/resolved 正确。
- [ ] 用户离开再回来能直接消费 result/artifact，不必查 Activity 或 admin queue。
- [ ] 多 tab 和 Agent 快速切换不把旧 response/approval 提交到新 run。

## 角色 surface

- employee：任务、Agent、Session、Personal KB、获准 Company Library、deliverable、必要 approval。
- company admin：members、Agent lifecycle、Company Knowledge、permissions、budget/audit summary、offboarding。
- platform admin：tenant/runtime/provider/config/compliance health，不默认读取业务正文。
- operator inspector：带 reason/scope 的 raw span/event/repair；默认折叠且全程 audit。

前端隐藏不是权限；相同 URL/API 在不同角色下必须由后端返回相同 authority verdict。

## 状态截图矩阵

每个关键 surface 保留统一视窗的：

- empty、loading/skeleton、success、long content；
- waiting user/approval、denied、unavailable、retryable、non-retryable、ambiguous；
- live streaming、terminal、hard reload、offline/reconnect；
- employee/admin/operator；
- light/dark、窄屏、200% zoom、keyboard-only、reduced motion。

截图只证明视觉状态，不替代 DB/event/tool/authority 证据。

## Accessibility 与表达

- [ ] tab/focus order、visible focus、escape/close、keyboard submit、screen-reader names 正确。
- [ ] status 不只靠颜色；live region 不重复播报 streaming token。
- [ ] dialogs/panels 焦点可恢复；disabled action 解释原因。
- [ ] 中文/英文 key 完整；未知 code 使用安全通用文案，raw exception 不进 DOM。
- [ ] 所有员工文案以任务语言表达，不要求理解 RuntimeTask、span、index、tenant、provider request id。

## Model、资源与体验观测

- [ ] 每次 model call 的 selected model/provider、prompt/tool bundle、context/token、cache、TTFT、total latency、retry、cost、terminal 有权威记录。
- [ ] employee 只看预算/用量摘要和必要限制；operator 看机械细节。
- [ ] provider fallback typed 可见且能力面不静默缩水。
- [ ] p50/p95、first visible feedback、reload convergence、duplicate/flicker、manual intervention 进入最终报告。

## Acceptance

Codex 基准通过真实 signed-in screenshot/state matrix 对比；Letta 只评 Agent rail shell。每个 surface 连续两遍并覆盖 hard reload、role negative、narrow/a11y 和真实 result consumption。视觉“像”但任务、权限、恢复或 deliverable 不闭环，判失败。
