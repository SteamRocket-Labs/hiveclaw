# CCPlus V1 延迟隐藏排除裁决 — D-05 / D-23（2026-06-24）

状态：**显式排除裁决（Exclusion Ruling），不是沉默缺口（Silent Gap）。**
归属单元：U6-a1-exclusion。
对照基线：CC / FreeCode 运行时语义为基准；本裁决处理 CC-LOCAL 的延迟隐藏（latency-hiding）工程优化，对应 Codex 工程增量类别。

本文档配套一个**契约/排除测试**
`backend/tests/kernel/test_latency_hiding_exclusion.py`，把 A1 沉默缺口转成可被
`pytest -k "streaming_tool_executor or latency_hiding or skill_prefetch or memory_prefetch"`
收集（COLLECT）的显式排除契约。reconciliation §5（line 168）要求的正是一个**重命名后的契约/排除测试**来证明它不是沉默缺口——一句散文式承认不足以满足该要求。

---

## 1. 被排除的 delta

| Delta | CC-LOCAL 本体 | 类别 |
|-------|---------------|------|
| **D-05 StreamingToolExecutor** | CC 在模型流式输出尚未结束时，就开始执行已经成型的 tool_use 块（mid-stream / 流中工具执行），用工具执行的时间窗口覆盖剩余的模型解码延迟。 | 延迟隐藏优化（latency hiding） |
| **D-23 memory/skill prefetch + tool-use 摘要** | CC 在回合早期就预取（prefetch）memory / skill 上下文，与模型解码、工具执行重叠（overlap）；并以异步方式产出 tool-use 摘要（async tool-use summary），同样是把准备工作藏进既有等待窗口里。 | 延迟隐藏优化（latency hiding） |

这两项的**共同本质是延迟隐藏**：它们不改变"哪些工具被执行、以什么输入执行、产生什么结果"，只改变"这些工作在时间轴上如何与解码/等待重叠"。换言之，它们是**性能（latency）优化，不是正确性（correctness）需求**。

---

## 2. 裁决：V1 显式排除，作为 Codex 类工程优化推迟

Hive 在 V1 **刻意推迟（deliberately defer）** D-05 / D-23，理由如下：

**(a) Hive 的循环实现形态是 buffered-async + 回调（callbacks）。**
内核 `AgentKernel.handle()`（`app/kernel/engine.py`）是一个**带缓冲的异步方法**，签名为
`async def handle(self, request: InvocationRequest) -> InvocationResult`——它**返回 `InvocationResult`，不是 async generator**（方法体内没有顶层 `yield`）。
循环采用**按回合的"先生成—后执行"（round-based generate-then-execute）**：模型在一个回合内完整产出 assistant 输出（含 tool_use 块）后，平台再执行工具、把结果喂回下一回合。这条路径在**工具执行的正确性与完整性**上是**正确且完整的**——它唯一缺少的是"并行延迟隐藏"（parallel latency hiding），而非任何工具被漏执行或错误执行。
流式 UX 不依赖 mid-stream 工具执行：它通过 `InvocationRequest.on_chunk`（`ChunkCallback`，`app/kernel/contracts.py`）这一**回调**增量推送模型文本。因此"用户看到流式输出"这一 UX 目标，在不引入 StreamingToolExecutor 的情况下已经达成。

**(b) 实现真正的 mid-stream 执行是一次高风险的热循环重写（high-risk hot-loop rewrite），收益只有延迟。**
要在流尚未结束时启动工具执行，必须把当前"完整 assistant 回合 → 工具回合"的清晰边界，改写成"边解码边调度工具"的交错状态机，触碰内核最热的循环、压缩边界、工具治理（`tools/service.py` → `tools/governance.py`）的执行时序、以及 T0/span 写入顺序。这次重写的**唯一收益是延迟降低**，却把正确性、治理时序、可观测顺序全部置于回归风险之下。在 V1 收益/风险不成立。

**(c) 目标已经达成。**
本能力域的**目标（GOAL）= 正确、完整的工具执行 + 通过回调实现的流式 UX**。这一目标已由现有 buffered-async + 回调 + 按回合生成-执行的循环满足。D-05 / D-23 只会改善延迟，不会补齐任何缺失的正确性，因此不是 V1 的正确性需求。

---

## 3. 与北极星（North Star）的绑定

按 CCPlus 边界契约（`docs/ccplus-north-star-contract-2026-06-24.md`）的决策序：

1. **CC / FreeCode 语义边界优先。** D-05 / D-23 不属于 CC 能力边界（即"哪些工具能被执行、产生什么结果"），它们属于 CC 的**本地工程实现优化**——延迟隐藏。排除它们**不**削弱任何 CC 能力边界。
2. **Codex 工程/控制改进只有在保留该边界时才可采纳。** D-05 / D-23 正是 **MAY-adopt（可采纳）的 Codex 工程类增量**：它们改善的是工程化的延迟控制，不重新定义 CC 能力边界。Hive 对此类 delta 的态度是"可采纳，但非 CC parity 必需"。
3. 因此，本裁决把 D-05 / D-23 标记为 **MAY-adopt Codex engineering deltas，V1 显式排除（explicitly excluded for V1）**，留待将来作为 **Hive-native 优化（Hive-native optimization）** 重新评估——届时它是 Hive 主动选择的性能增强，**不是隐藏的 CC parity 债**。

这与 `CLAUDE.md` 的 CCPlus 边界一致：CC 本地 CLI 能力若属"工程控制/可观测/延迟"层面的优化，Hive 可以映射或推迟为 Codex 类增量；其推迟必须被**显式记录**，而非沉默缺席。

---

## 4. 这是排除裁决，不是沉默缺口

- 排除的边界是**清晰的**：仅 D-05（mid-stream 工具执行）与 D-23（prefetch overlap + async tool-use 摘要）这两项延迟隐藏优化被推迟；工具执行的正确性/完整性、流式 UX（回调）均**不在**排除范围内，且已实现。
- 排除是**有意的且被追踪的**：配套测试
  `tests/kernel/test_latency_hiding_exclusion.py` 断言——内核 `handle()` 是返回
  `InvocationResult` 的 buffered async 方法（不是 async generator）、仓库中**不存在**
  `StreamingToolExecutor` 符号（记录这一刻意缺席）、本排除文档存在、且流式 UX 经
  `on_chunk` 回调达成。测试函数名包含
  `streaming_tool_executor` / `latency_hiding` / `skill_prefetch` / `memory_prefetch`，
  从而被 reconciliation 验收选择器
  `pytest -k "streaming_tool_executor or latency_hiding or skill_prefetch or memory_prefetch"`
  **收集**。
- 因此 A1（D-05、D-23）从"沉默缺席（silent absence）"转为**显式、可追踪的排除契约（explicit, tracked exclusion contract）**。

---

## 5. 重新评估触发条件（将来 Hive-native 优化时）

当且仅当下列条件成立，才把 D-05 / D-23 作为 Hive-native 延迟优化重新引入：

1. 按回合的正确性、工具治理时序、T0/span 写入顺序、压缩边界已有充分的回归护栏（不会被交错状态机破坏）；
2. 延迟收益经实测证明值得热循环重写的风险；
3. 重新引入走 Hive-native 设计与审计路径，并明确标注为**主动性能增强**，而非补缴的 CC parity 债。

在满足之前，V1 维持本排除裁决。
