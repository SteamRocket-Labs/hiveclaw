# CCPlus Review → Eval → Release 协作手册

## 1. 定位

这是一套可跨时间、跨版本、跨审查轮次复用的协作入口。它把五种不同职责分开：

1. **Review**：从当前事实发现断点，回答“哪里没有闭环”。
2. **Frontend Product Review**：从真实用户旅程审查信息层级、受众、状态、恢复、交付与视觉产品化。
3. **Eval Design**：把尚未证明的关键主张变成可执行、不可临场改写的 Eval Manifest。
4. **Eval Execution**：按 Manifest 运行并保存原始证据，不修代码、不移动标准。
5. **Release Arbitration**：只基于版本匹配的证据作出发布裁决，不重新出题。

完整“终极大考”不进入 Review Prompt。它只在需要证明里程碑级产品主张时，由 Eval Designer 依据真实断点、生产分布和能力边界生成，并作为手动 bakeoff 执行。

本手册服从仓库现行 `eval-system-spec.md`：生产证据优先，不建设第二套生产、常设合成场景平台、Eval 专用前后端或克隆环境。

## 2. 文件组成

| 文件 | 使用者 | 产物 | 不负责 |
|---|---|---|---|
| `ccplus-agent-native-independent-review-prompt.md` | 独立 Reviewer | 原子化报告 + Eval Handoff | 完整出题、执行、发布裁决 |
| `ccplus-frontend-product-review-prompt.md` | 独立产品 Reviewer | 前端产品化报告 + Frontend Eval Handoff | 改代码、固定布局、发布裁决 |
| `ccplus-eval-design-prompt.md` | 独立 Eval Designer | Eval Manifest | 修代码、运行考试、判定发布 |
| `ccplus-eval-execution-prompt.md` | 独立 Executor | Eval Evidence Report + receipts | 修改 Manifest、修复失败 |
| `ccplus-release-arbiter-prompt.md` | 独立 Arbiter | GO / NO-GO / UNVERIFIED | 补测、改标准、实施修复 |

这些文件是职责边界，不要求四个不同模型。低风险任务可以使用同一模型的不同干净 Session；高风险发布建议使用不同 Reviewer/Executor/Arbiter，或者至少使用隔离上下文和不可变交接文件。

## 3. 与现有 Eval 四问的关系

所有 Eval 必须先归入现有四问，归不进去就不新建机制：

| Eval 问题 | 何时使用 | 主要证据 |
|---|---|---|
| J1：候选是否值得采纳 | Skill、Prompt、Memory/Soul/Workflow 候选晋升 | 硬验证、provisional 真实结果、回滚 ledger |
| J2：Agent 是否越来越强 | 持续自进化效果 | 生产 T2/feedback/spans/evolution ledger 与成长报告 |
| J3：平台改动是否让 Agent 变笨 | 每次实现变更与回归 | 确定性测试、契约、真实失败 trace 的定向 replay |
| J4：与 benchmark 相比怎样 | 里程碑声明 | 手动真实 bakeoff、相同条件对照与外部硬结果 |

“终极大考”属于 J4，或由多个 J1–J3 证据加一个 J4 总体验收组成。它不是第五套常设系统。

## 4. 标准协作流程

### Step 1：用户冻结问题与快照

用户只需要明确：

- 要审查的仓库或工作区；
- 希望回答的是架构审查、修复验收还是发布判断；
- 是否允许读取生产证据、运行测试、使用外部 benchmark；
- 对付费调用、真实外部效果和生产写入的授权边界。

Reviewer 记录 repo root、HEAD、dirty worktree、运行环境和证据时间点。后续每份产物都引用同一 `snapshot_id`；源码变化后旧证据自动降为 stale，不能继续证明新版本。

### Step 2：在干净 Session 运行 Review Prompt

把 `ccplus-agent-native-independent-review-prompt.md` 的“可复用正文”交给 CC、Codex 或另一独立 Reviewer。不要附带历史断点数量、旧修复轮次、希望它得出的结论或完整大考答案。

Reviewer 输出：

- 当前 CCPlus 基线账本；
- 四模块原子化报告；
- 断点与未证实项；
- 代码简洁性和能力保持判断；
- 只包含“证明责任”的 Eval Handoff。

### Step 3：把实现修复与 Review 分开

如果 Review 已发现明确源码断点，先由实现 Agent 修复并按七原子交付。Reviewer 不在原审查上下文中边审边改，否则报告基线会漂移。

修复完成后产生新 `snapshot_id`。确定性回归随代码实施完成；仍需要真实行为、长尾、对照或用户体验证明的主张进入 Eval Design。

### Step 3A：对用户可见改动运行 Frontend Product Review

只要本次范围涉及会话、状态、审批、恢复、交付物、Workspace、多智能体、Knowledge 或公司后台，就在同一新快照上运行 `ccplus-frontend-product-review-prompt.md`。提供可访问产品环境和受权角色，但不要提供预设布局答案。

Frontend Reviewer 使用真实浏览器旅程和截图，把功能事实与首页、Agent 概览、Session、右侧面板、Workspace、通知和管理面逐项对账。其产物与功能 Review 一起交给 Eval Designer。若只完成 backend 修复而产品消费仍矛盾，不得跳过此步。

### Step 4：在新 Session 运行 Eval Design Prompt

输入只包含：

- 新快照；
- 原子化报告及其 Eval Handoff；
- 现有 `eval-system-spec.md`；
- 被授权读取的真实 traces、历史失败和 benchmark；
- 成本、时间、安全和环境边界。

Eval Designer 可以修正 Reviewer 提出的评测方向，但必须说明理由。它根据任务选择 programmatic check、trace grading、pairwise/human rubric、故障模拟或手动 bakeoff；不要求每项能力都使用所有方法。

输出的 Eval Manifest 在执行前冻结。执行期间不得新增场景、修改权重或移动通过标准。

### Step 5：用户只批准“边界”，不替模型出答案

用户检查 Manifest 中的：

- 是否会访问真实生产或第三方系统；
- 是否产生写入、费用、通知或不可逆效果；
- 数据与租户边界是否正确；
- 时间、成本和清理方案是否可接受；
- 评测是否真的回答产品主张。

用户不需要规定 Agent 应该如何推理、用什么措辞或走哪条工具序列。授权针对数据、效果、资源和隔离，不针对语义答案。

### Step 6：在受控环境运行 Eval Execution Prompt

Executor 先校验 snapshot、Manifest 和环境，再执行允许的部分。每项只输出：

- `PASS`：预先声明的证明责任有可复现证据；
- `FAIL`：出现可复现的能力、治理、恢复或消费失败；
- `UNVERIFIED`：环境、权限或证据不足，不能诚实裁决。

精确权限、状态、schema、receipt、文件、重复效果和资源守恒使用程序判据；开放任务质量、推理、表达和 UX 使用预先校准的 model/human rubric。Executor 不得用合成分、固定 prose 或 Agent 自述填补缺失证据。

### Step 7：在新 Session 运行 Release Arbiter Prompt

Arbiter 只读取版本匹配的 Review、Manifest、Evidence Report 和 receipts。它不能补题、补跑、修代码或平均掉硬失败。

输出：

- `GO`：范围内所有发布阻断主张均被证明，且没有未处理硬失败；
- `NO-GO`：存在真实发布阻断失败；
- `UNVERIFIED`：关键证据缺失或版本不匹配。

最终业务风险接受仍由 owner 决定，但不能把 `UNVERIFIED` 改写成技术 PASS。

### Step 8：失败回到实现，不回到评分标准

失败项生成新的实现任务。修复后：

- 旧失败 case 成为定向回归证据；
- 重新冻结新 snapshot；
- 只重跑受影响范围及必要的相邻回归；
- 不为了通过而删除 grader、降低阈值或缩小真实输入覆盖。

## 5. 什么时候需要终极大考

只有在准备声明下列结论时才需要里程碑 Capstone：

- 单 Agent 达到 CCPlus 或不弱于指定 benchmark；
- Hive Native 让 Agent 在跨 Session 工作中产生可验证净增益；
- 企业治理在复杂权限和故障下仍保持 Agent 能力；
- 整体产品已达到上线或重大版本发布标准。

Capstone 应由 Eval Designer 根据当前最重要的产品主张和风险生成。固定的是证明责任，不是业务题目。它应验证真实用户结果、能力保持、KISS/正式入口、鲁棒恢复、可扩展边界和可维护证据，但不得为了凑齐组件制造不自然任务。

一次 Capstone PASS 不能覆盖模块级硬失败，也不能替代 J1/J2 的纵向真实证据。一次跨租户泄漏、未授权不可逆效果、不可恢复状态丢失或重复不可逆效果足以使对应发布主张失败。

## 6. 产物命名与交接

建议使用占位符而不是日期写死模板：

```text
reports/<snapshot_id>/atomic-review.md
reports/<snapshot_id>/eval-handoff.md        # 可内嵌在 review
reports/<snapshot_id>/frontend-product-review.md
reports/<snapshot_id>/frontend-eval-handoff.md
reports/<snapshot_id>/eval-manifest.md
reports/<snapshot_id>/eval-evidence.md
reports/<snapshot_id>/release-decision.md
reports/<snapshot_id>/receipts/**
```

`snapshot_id` 可以由 commit SHA、worktree tree hash、deployment id 和必要的 migration/config 指纹组成。只要任何会影响行为的输入变化，就必须明确旧产物是否仍可适用。

## 7. 最简使用方式

如果只想知道“还有什么断点”，只运行 Review Prompt。

如果要验证修复是否真的成立，运行：Review → Fix → Frontend Product Review（用户可见范围）→ Eval Design → Eval Execution。

如果要做上线或重大能力声明，运行完整链路：Review → Fix → Frontend Product Review → Eval Design → Eval Execution → Release Arbitration；只有这时才按需要加入 Capstone。

## 8. 方法参考

- OpenAI 建议先从完整 trace 识别行为与失败模式，再把已知的优质行为转化为可重复数据集和 eval run：[Evaluate agent workflows](https://developers.openai.com/api/docs/guides/agent-evals)
- Eval 应任务特定、接近真实分布、持续运行并通过人类判断校准，避免泛化指标和 vibe-based 判断：[Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- Trace grading 用于定位 Agent 工作流中的具体失败和规模化回归：[Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
- Model grader 本身也需要候选答案、ground truth 与专家判断校准：[Graders](https://developers.openai.com/api/docs/guides/graders)
