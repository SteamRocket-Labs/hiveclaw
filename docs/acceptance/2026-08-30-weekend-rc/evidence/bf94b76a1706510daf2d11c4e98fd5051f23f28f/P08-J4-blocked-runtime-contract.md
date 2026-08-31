---
document_id: weekend-rc-2026-08-30-p08-j4-runtime-contract-precondition
owner: Codex
status: immutable
authority: runtime-precondition-evidence-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: blocked-precondition-no-three-runtime-same-envelope
journey_id: P08-J4
pass: preflight
environment: local-real-runtime-preflight-plus-production-readback
source_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
deployed_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
checkout_head: b6b516f496ed91b2a573879378c4fc5a85947486
manifest_sha256: d320edceeb26cf68fa724e77502d811e5476fa04ee3c9128075cc8c79eb38117
result: BLOCKED_PRECONDITION
fault_recovery_result: BLOCKED_PRECONDITION
negative_authority_result: PASS
cleanup_result: PASS
---

# P08-J4 real-runtime contract precondition

本文件不是 canonical `P08-J4-pass-1.md` 或 `P08-J4-pass-2.md`，不会被 scorer 当成通过。调查没有运行模型 benchmark、安装依赖、登录、读取 credential、改变 provider/model 配置或创建 synthetic asset。

## Frozen contract

- manifest 要求 Hive、FreeCode、`hermes-agent` 使用同一 task、workspace、model/resource envelope 和外部硬判据真跑。
- runtime unavailable 必须输出空 blocker report；官方 Claude Code 不能替代冻结的 FreeCode target，单独 Hermes 结果也不能补齐三方比较。

## Read-only reproduction

- deployed application 仍为 exact `bf94b76a`；backend `07059ce5…`、backend-api `c70ff972…`、frontend `308e7789…` 均为 `SUCCESS`，backend health `status=ok`，frontend HTTP 200。
- current manual runner `backend/app/evals/bakeoff_runtime.py` 只接受 `claude_code` 和 `hermes_agent`；没有 Hive 或 FreeCode target。Claude command 固定 `--model sonnet`，Hermes command不绑定相同 model，故当前入口无法证明 same-model envelope。
- installed `claude` 是官方 Claude Code `2.1.251`。FreeCode live source 位于 `/Users/example-owner/Context Engineering/free-code-main`、revision `7dc15d6c8fb0c40c7fcc02ce9b58204324252632`、package `2.1.87`；当前没有 built `./cli`，也没有 `node_modules`。未把官方 CLI 冒充 FreeCode，未安装 supply-chain dependencies。
- installed Hermes 为 `0.20.6`；只读取版本，没有发起 benchmark task。Hive checkout 没有可供该 manual runner 使用的 local CLI target；已退役的常设 `hive_live_runner` 由 architecture regression 明确禁止恢复。

## Validation and verdict

- `uv run pytest -q tests/evals/test_bakeoff_runtime.py tests/evals/test_run.py tests/evals/test_eval_retirement.py` → **21 passed**。
- 当前最早断点是“三个真实 runtime + 同 model/resource envelope”不存在，而不是 scorer、报告或 production health。修复前不得生成分数、横向结论或 canonical pass evidence。
- 建立 FreeCode runnable environment 或共享模型认证会涉及依赖安装和/或 credential authority；本轮未越过这些边界。P08-J4 保持 `BLOCKED_PRECONDITION`，NPTCR 不变。
