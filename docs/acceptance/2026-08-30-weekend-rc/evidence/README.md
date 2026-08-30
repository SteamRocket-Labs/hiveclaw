---
document_id: weekend-rc-2026-08-30-evidence-contract
owner: Codex
status: active
authority: canonical-evidence-format
last_reviewed: 2026-08-30
source_commit: c18b181c
verification_status: frozen-machine-contract-no-production-evidence-created
---

# Evidence 记录合同

[返回索引](../README.md) · [Journey Ledger](../04-journey-ledger.md) · [Runbook](../06-runbook-and-release-gates.md)

本目录只保存实际运行证据。Spec、状态、finding 和 raw log 不在这里重复维护。

## 路径

```text
evidence/<exact-commit>/<journey-id>-pass-1.md
evidence/<exact-commit>/<journey-id>-pass-2.md
evidence/<exact-commit>/<journey-id>-fault-<name>.md
evidence/<exact-commit>/release-gates.md
evidence/<exact-commit>/final-verdict.md
```

Evidence 接受后不原地改写。发现错误时新增 `<original-name>-correction-N.md`，声明 `supersedes`、错误内容和新的事实源。

## 每份文件必须包含

```yaml
journey_id:
pass:
environment:
source_commit:
deployed_commit:
manifest_sha256:
deployment_ids:
persona_principal:
data_version:
started_at:
ended_at:
result: PASS | FAIL | BLOCKED_PRECONDITION
fault_recovery_result: PASS | FAIL | BLOCKED_PRECONDITION
negative_authority_result: PASS | FAIL | BLOCKED_PRECONDITION
cleanup_result: PASS | FAIL | BLOCKED_PRECONDITION
supersedes:
```

正文固定为：

1. Input：从哪个真实入口、什么输入、是否可恢复。
2. Authority：tenant/user/Agent/delegation/grant/approval 如何绑定；负向结果。
3. Execution：live entry 到唯一 executor 的 wiring/path proof。
4. Evidence：event/span/transcript/file/DB row/artifact/receipt 的机械事实引用。
5. Recovery：reload/disconnect/restart/retry/cancel/duplicate/rollback。
6. Consumption：普通用户实际看到并使用什么；admin/operator 看什么。
7. Acceptance：外部硬判据、截图状态、耗时/成本、最终 verdict。
8. Cleanup：合成资产、owner、保留/归档/删除方式。
9. Not proven：本证据不能外推的能力或范围。

## 记录规则

- 只保存精确命令与简短结果；完整 stdout、trace、video、screenshot 和 payload 放在 artifact storage，并保存不可变 reference/hash。
- 不保存 secret、token、个人隐私或真实客户内容。
- read-only DB 查询要声明 transaction/read-only/tenant scope；生产写入要有 owner action-time authorization。
- 受控 fake 必须逐项披露，且结果只能进入 CI evidence，不能进入 production NPTCR。
- `PASS` 必须说明无管理员、DB 手改、console 或人工补状态。
- ambiguous provider delivery 记 `FAIL` 或 `BLOCKED_PRECONDITION`，禁止自动 replay 或称 transient。
- deployment success 只证明 freshness；signed-in journey 才证明 product consumption。
- screenshot 只证明 UI observation；需与 canonical mechanical evidence 交叉。
- pass 1 证明 fresh signed-in clean path；pass 2 必须同时填写 `fault_recovery_result`、`negative_authority_result` 和 `cleanup_result`。额外 fault 文件只在需要独立保存大型故障证据时创建。

## Release Gate 机械字段

`release-gates.md` 的 frontmatter 必须把应用提交 `D` 分别绑定到 `backend_commit`、`backend_api_commit`、`frontend_commit`，三个 status 均为 `SUCCESS`；同时记录五个 `guardrail_*`、五个 `coverage_*`、`zero_known_defects` 和 `cleanup_result`。这些是 Codex 已作出语义判断后的机械投影，不允许脚本自行生成 PASS。

```bash
python3 backend/scripts/weekend_rc_gate.py score \
  --deployed-commit <40-char-application-sha>
```

该命令只检查 96 条旅程的文件、frontmatter、manifest hash、双遍、三服务 exact commit、护栏与覆盖字段。它不读取自然语言来判断答案或 UI 质量，输出固定包含 `semantic_verdict: not_computed_by_tool`。

## Closed 门槛

Journey Ledger 只能在下列文件都存在并一致时改为 `Closed loop`：pass 1、pass 2、适用 fault/recovery、negative authority、release-gate/exact deployment。任何 correction 使旧证据失效时，状态立即回到 `Partial loop` 或 `Breakpoint`，直到重新双遍。
