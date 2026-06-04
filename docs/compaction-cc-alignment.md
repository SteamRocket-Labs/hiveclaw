# Compaction 对齐 Claude Code 基线 — 诊断与改造

> 2026-06-04 系统性对标调查（CC 源码：`claude-code-org/src/services/compact/`）。
> 结论：Hive 压缩主干**是 LLM 的**，但三个机械环节架空了它——名义上 AI 压缩，实际 LLM 只看到机械剪枝后的残片。
> 本文档是唯一权威改动清单；每项完成后更新状态列。

## 1. 真实调用链（现状）

```
kernel/engine.py
  ├─ :1740  入口预压缩（默认阈值 82%，tenant 可配）
  ├─ :2704  L3 mid-loop compaction（每 3 轮检查，≥75% 触发）
  ├─ :1975  PTL reactive（丢轮组×2 → 第 3 次 full compress threshold=0.5）
  └─ :2596  L1 time-based microcompact（机械清旧 tool results，对齐 CC，不动）
        ↓
memory_service.maybe_compress_messages (:317)   ← 唯一真实压缩入口
  ├─ tenant 有 enabled model → conversation_summarizer._llm_summarize（LLM 路径）
  ├─ LLM 失败 → _extract_summary（正则切片拼模板）silent fallback
  └─ 都空 → last-resort trim
```

⚠️ `conversation_summarizer.summarize_conversation` 是孤儿入口（零生产调用方），真实流量全走 `memory_service`。

## 2. 与 CC 的逐环节判定

| 环节 | Hive 现状 | CC 基线 | 判定 |
|---|---|---|---|
| microcompact | 机械清旧 tool results（>60min/压力下 10min，留 5 个） | time-based MC 同样机械 | ✅ 已对齐 |
| 重压缩输入 | 机械剪枝后才给 LLM：`[-40:]` + 单条截 800/1500/300 chars | **完整历史**进 forked agent（复用 prompt cache） | 🔴 P0 |
| 摘要输出预算 | `max_tokens=2500` | 20,000（p99.99 实测 17.4K） | 🔴 P0 |
| 摘要模型 | tenant summary_model_id→default→最新 enabled（可能≠主模型） | 永远 mainLoopModel（cache key 一致性） | 🟡 P1 |
| 失败处理 | 每次重试 LLM→silent fallback 正则提取，无熔断无指标 | 连续 3 次熔断停止；**绝不降级**（劣质摘要→任务漂移） | 🟡 P1 |
| PTL 兜底 | 机械丢轮组×2→full compress | 机械 truncateHeadForPTLRetry×3→报错 | ✅ 哲学一致 |
| 压缩后恢复 | soul/focus/ledger/T3 + 最近文件 **3 个×4K chars** | 最近读过文件 **≤5 个/50K tokens** + skills 25K + plan | 🟡 P2 |
| 孤儿入口 | summarize_conversation 零调用方 | — | 🟡 P2 |

核心哲学差异：CC 的机械截断**只出现在 PTL 兜底**；Hive 的机械截断出现在**正常路径的输入预处理**——这是"看起来是 AI 能力、实际是系统拼接"的根因。

## 3. 改动清单

### P0 — 完整历史进摘要 LLM（`conversation_summarizer.py`）

**现状**：`_llm_summarize` 序列化时 user/assistant 截 800、tool_result 截 1500、tool args 截 300，最后 `conversation_text[-40:]` 只留 40 条——触发压缩时 old_messages 常几百条，大头静默丢弃。`max_tokens=2500`。

**目标**：
1. 去掉 `[-40:]`；单条截断放宽为防御性高上限（tool_result 12000、user/assistant 8000、tool args 2000 chars）——防单条异常，不再是常态剪枝。
2. 超摘要模型窗口时才机械兜底：按 `_get_input_context_limit(provider, model)` 的 70% 预算，从头丢最老消息直到 fit（对标 CC truncateHeadForPTLRetry：机械只做兜底）。
3. `max_tokens` 2500 → 8000（CC 用 20K；Hive 多 provider 输出上限差异大，8K 是普遍安全值，常量化便于调）。

**状态**：⬜ 待做

### P1-1 — 摘要模型默认主对话模型（`memory_service.py`）

**现状**：`_get_summary_model_config` 解析链与主对话模型无关，摘要可能落在更小/不同模型上（窗口不一致、行为不一致）。

**目标**：优先级改为 tenant 显式 `summary_model_id` > **当前主对话模型**（按 provider+model 查 enabled LLMModel 拿凭据）> default 链。`maybe_compress_messages` 已有 `model_provider/model_name` 入参可用。

**状态**：⬜ 待做

### P1-2 — LLM 摘要熔断 + P1-3 降级 metric（`memory_service.py`）

**现状**：LLM 持续故障时每次压缩都重打 LLM；降级到正则提取完全静默（CC 曾因无熔断单日浪费 25 万次 API 调用）。

**目标**：per-tenant 连续 3 次 LLM 摘要失败 → 熔断跳过 LLM（TTL 10min 半开重试），成功重置；降级时 `logger.warning(..., extra={"metric": "compaction_llm_fallback"})` 对齐 engine.py metric 风格。熔断状态为运维性 in-memory（CC 同样做法）。

**状态**：⬜ 待做

### P2-1 — post-compact 文件恢复对齐工作现场（`kernel/engine.py:1209`）

**现状**：`recent_files[-3:]`、每个 `min(max(cap//2,2000),cap)`=4K chars ≈ 总 12K chars。CC 恢复 ≤5 个文件/50K tokens——CC 恢复"工作现场"，Hive 偏重"身份"。

**目标**：`[-3:]` → `[-5:]`；per-file 预算直接用 `_per_file_cap`（8K chars）。总预算 60K chars 已够容纳，不动。

**状态**：⬜ 待做

### P2-2 — 删除孤儿入口（`conversation_summarizer.py`）

**现状**：`summarize_conversation`（:65）零生产调用方，改压缩逻辑容易改错地方。

**目标**：删除函数；同步清理引用它的测试（test_conversation_summarizer.py、test_memory_integration.py 等）。保留 `estimate_tokens`/`_llm_summarize`/`_extract_summary` 等在用函数。

**状态**：⬜ 待做

## 4. 明确不做（本轮）

- **fork/cache 共享摘要请求**：CC 用 forked agent 复用主线程 prompt cache；Hive 的 llm_client 无 fork 机制，P1-1 的模型一致性已拿到大部分收益，cache 共享留待 subagent 源能力成熟后评估。
- **cached microcompact（cache_edits API）**：依赖 Anthropic beta header，多 provider 环境不通用。
- **partial compact（from/up_to 双向）**：cache 前缀优化，收益依赖 cache 共享，同上顺延。
- **手动 /compact + custom instructions**：产品面入口，独立需求另立项。
