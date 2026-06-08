# Plan Mode 对标 CC（Plan Mode CC Alignment）

> 状态: **v0.1 诊断 + 修复方向草案（2026-06-07）——待拍板**。由生产实证触发（Web3 研究员"RWA 周报"plan 用户实锤"用不了，不是 CC 的 plan mode"）。
> 范围: Hive runtime 的 **Plan Mode**——`exit_plan_mode` 工具 schema、plan-mode launcher prompt、plan 卡片渲染、澄清机制。不动 plan-mode 的 read-only gate / 持久化 / 审计（那些是 Hive 合理治理 delta，已对齐 `feedback_plan_from_agent_system_governs`）。
> 证据基线: CC 源码 `/Users/rocky243/Context Engineering/claude-code-org/src/tools/{EnterPlanModeTool,ExitPlanModeTool,AskUserQuestionTool}`；Hive `backend/app/tools/handlers/plan_mode.py` + `backend/app/services/plan_mode_system_run.py`。行号以 2026-06-07 为准。
> North Star: Plan Mode 是 Hive runtime 一等能力（`[[project_hive_plan_mode]]`）。**Hive = CC superset**：先对齐 CC 基线（plan 文件主体 + AskUserQuestion 澄清 + 场景门控），再叠 Hive 的治理/自治 delta。裁决镜头 = AI-Native 法律（L1 plan 来自 agent 智能、L3 治理是 Hive delta 但 surface 不暴露 plumbing）+ `[[feedback_surface_not_plumbing]]`。

---

## §0 触发：生产实证

用户对 Web3 研究员说"我需要做一个 RWA 的周报，进入计划模式计划一下"。agent 进入 plan mode，产出一张 plan 卡片：8 个平铺 section（目标 / 执行步骤 / 成功标准 / 唤醒策略 / 预估成本 / 停止条件 / 假设 / 待澄清问题），其中：
- 执行步骤每条标注"（执行阶段，本步不实施）"；
- agent 在卡片前明说"Plan Mode 锁住了 read-only，问不了你，我直接按最常见、可解释的默认值出 plan"；
- 列了 4 个"待澄清问题" + 一批"假设"，但都没问用户；
- "唤醒策略 none"、"预估成本 unknown · unknown" 等内部字段直接摊给用户。

用户判断："这个 plan 用不了 …… 你是按照 CC 的 planmode 来的吗 完全用不了啊。" — 经源码对照，**判断成立**。

---

## §1 CC Plan Mode 真实形态（源码实证）

CC 的 plan mode 是一条「探索 → 澄清 → 写 plan 文件 → 请求审批 → 执行」的流水，三个工具协作：

| 阶段 | 工具 / 机制 | 关键语义（源码原话） |
|---|---|---|
| 进入 | `EnterPlanModeTool` | "DO NOT write or edit any files except **the plan file**" / "This is a read-only exploration and planning phase"（`EnterPlanModeTool.ts:107,118`）——read-only 约束的是**外部副作用**，**唯独允许写 plan 文件** |
| 澄清 | `AskUserQuestionTool` | plan mode 期间 agent **可以问用户**。`ExitPlanMode/prompt.ts`："If you have unresolved questions about requirements or approach, use **AskUserQuestion first (in earlier phases)**" |
| 提交 | `ExitPlanModeV2Tool` | **plan 内容写在文件里，工具不接受 plan 作参数**——"This tool does NOT take the plan content as a parameter - it will read the plan from the file you wrote"（`prompt.ts`）。schema 唯一语义参数是 `allowedPrompts`（实施所需的权限类别）（`ExitPlanModeV2Tool.ts:77-90`） |
| 边界 | `ExitPlanMode/prompt.ts` | "Only use this tool when the task requires planning the **implementation steps of a task that requires writing code**. For research tasks where you're gathering information, searching files, reading files … **do NOT use this tool**" |
| 反模式 | 同上 | "Do NOT use AskUserQuestion to ask 'Is this plan okay?' … ExitPlanMode inherently requests user approval" |

**提炼 CC 三条铁律**：
1. **plan 是 agent 自由写的 markdown 文件**（一份散文计划），ExitPlanMode 只发"写完了、请审批"的信号 + 申请权限——plan 主体是给人读的文章，不是 schema 字段。
2. **有未决问题 → 先 AskUserQuestion 问用户 → 对齐后再 exit**——plan mode 的核心价值是"出 plan 前先和人对齐"。
3. **只用于"要写代码的实施任务"**——research / 查资料 / 读文件类任务**不该**走 exit_plan_mode。

---

## §2 Hive 现状（逐项 file:line）

| 维度 | Hive 现状 | 证据 |
|---|---|---|
| plan 提交 | `exit_plan_mode` schema 有 **13 个字段**：`title/objective/plan_markdown/steps/success_criteria/stop_conditions/assumptions/open_questions/risk_assessment/estimated_cost/wake_policy/handoff_target/handoff_payload`；required 6 个 | `tools/handlers/plan_mode.py:126-147` |
| plan 主体 | **有 `plan_markdown`**（描述 "Concise markdown plan preview"）——对应 CC 的 plan 文件，但它只是 13 字段之一 | `plan_mode.py:130` |
| 卡片渲染 | 前端把结构化字段**全部平铺**成 section（目标/步骤/成功标准/唤醒策略/成本/停止条件/假设/待澄清），`plan_markdown` 未作主体 | §0 生产卡片实证 |
| 澄清机制 | **Hive 无 `AskUserQuestion` / 任何 ask-user 工具**（`grep` 全空）→ agent 在 plan mode 里**物理上无法问用户** | `grep ask_user_question\|AskUserQuestion app/tools/` = 空 |
| launcher 引导 | 只教 "submit by exit_plan_mode … do not ask in prose whether the plan is OK"——对齐了 CC 的反模式，**但漏了 CC 的"有问题先 AskUserQuestion"**（因为没这工具） | `services/plan_mode_system_run.py:66-69` |
| 场景门控 | 任意 intent 都可进 plan mode 出 exit_plan_mode 重卡片，无 "research 任务不该用" 的门控 | `plan_mode_system_run.py` + `plan_mode_gate.py` |

---

## §3 三处偏离 + delta 性质分类

不是所有差异都是 bug。Hive 是自治数字员工 + 控制中台，比 CC（交互式编码助手）多出治理/自治维度——关键是区分**合理 delta**、**plumbing 暴露违例**、**真缺失**。

### 偏离① 形态错 —— plan 主体被降级，治理字段平铺暴露（plumbing 违例）
- **CC 基线**：plan = 一篇 markdown 文章（plan 文件），用户读文章。
- **Hive 现状**：`plan_markdown` 沦为 13 字段之一，前端把 `steps/success_criteria/stop_conditions/assumptions/open_questions/risk_assessment/estimated_cost/wake_policy/handoff_*` 全部平铺。
- **delta 分类**：
  - `wake_policy / handoff_target / handoff_payload / risk_assessment / estimated_cost / stop_conditions` = **Hive 合理治理/自治 delta**（CC 无，因 CC 不做无人值守调度）——作为**系统数据**正当，但**原样平铺给用户 = 违反 `surface≠plumbing`**（UI 不得把 API 1:1 翻成控件裸露原始形态）。
  - `wake_strategy: none`、`estimated_cost: unknown·unknown`、步骤"（本步不实施）" = 内部机制/空值直接漏给用户 = 最刺眼的 plumbing 暴露。
- **判定**：违例（形态），非字段本身的错。

### 偏离②（根因/最致命）缺 AskUserQuestion —— agent 无法澄清，只能自说自话（真缺失）
- **CC 基线**：有未决问题 → AskUserQuestion 问 → 对齐 → exit。
- **Hive 现状**：无 ask-user 工具 → agent "问不了你，按默认值出 plan" → 把未决项塞进 `open_questions`/`assumptions` 当**免责清单**。
- **后果**：plan mode 的核心价值（出 plan 前对齐）落空——用户拿到一份塞满未对齐假设的 plan，那些"待澄清问题"既确认不了、agent 又已替用户拍了默认值。**这就是"用不了"的根本原因**。
- **判定**：真缺失。`open_questions`/`assumptions` 字段在补上 AskUserQuestion 后，才能从"免责清单"回归"问之前的澄清来源 / 问之后仍存的已知假设"。

### 偏离③ 场景错 —— research 任务被强制重型化（门控缺失）
- **CC 基线**：exit_plan_mode 只用于"要写代码的实施任务"；research 不该用。
- **Hive 现状**："做 RWA 周报"（research/内容产出）也走了重型 exit_plan_mode 卡片。
- **delta 考量**：Hive 的 plan mode 定位比 CC 宽（定时任务/objective/long task/delegation 皆可叠加其上，见 `[[project_hive_plan_mode]]`）——所以**不能照搬 CC "只限写代码"**。但"任意任务都强制重卡片"是另一个极端。合理中线 = **按任务性质决定 plan 的轻重**（research 类轻量化或直接执行，实施/高风险类才上完整确认卡片）。
- **判定**：门控缺失（需 Hive 自己的判据，非照搬 CC）。

---

## §4 修复方向（待拍板）

> 顺序按根因：P1 补根因工具 → P2 形态归正 → P3 场景门控。每项遵循"CC 基线 + Hive delta"，红测先行。

### P1 — 补 `ask_user_question` 工具，plan mode 澄清回归对齐（根因）
- 新增 `ask_user_question` 工具（对齐 CC AskUserQuestionTool：question + header + options + multiSelect），在 plan mode（及普通对话）可用；治理上它是**问用户、不产生外部副作用**的读侧交互，与 read-only gate 不冲突。
- launcher prompt 补 CC 那条："有未决的关键问题先用 ask_user_question 澄清，对齐后再 exit_plan_mode"。
- `open_questions`/`assumptions` 语义归正：澄清后仍存的才进 plan 作"已知假设"，不再当"问不了你所以我猜"的免责清单。
- ⚠️ 这是通用能力（不止 plan mode 用）——`[[project_runtime_guidance_cc_alignment]]` 的 catalog 里 `AskUserQuestion` 是 CC native attachment，本项与之合流。

### P2 — plan 卡片以 `plan_markdown` 为主体，治理字段收进折叠（surface≠plumbing）
- 第一屏 = `plan_markdown`（agent 写给人的散文计划）+ objective + steps（用户语言）。
- `wake_policy / estimated_cost / risk_assessment / handoff_* / stop_conditions` 收进"技术详情/高级"折叠区，空值/none 不渲染。
- 验收（`[[feedback_surface_not_plumbing]]` 两问）：第一屏是用户语言吗？主操作是"看懂计划→确认/调整"这个用户真实动作吗？

### P3 — 按任务性质门控 plan 的轻重（Hive 判据，非照搬 CC）
- research/内容产出类（如周报）：轻量 plan（散文要点 + 一句确认）或直接执行；不强制 13 字段重卡片。
- 实施/外部动作/高风险/不可逆类：完整确认卡片 + 治理字段。
- 判据落在 intent_type / 任务分类，而非工具层硬编码"只限写代码"。

---

## §5 待拍板项
1. P1 ask_user_question 是否本轮做（根因，建议做）；与 runtime-guidance catalog 的 AskUserQuestion 合流口径。
2. P2 折叠哪些字段、第一屏放什么（需结合前端 plan 卡片现状）。
3. P3 门控判据：用 intent_type 还是新增任务性质分类；research 类默认轻量 plan 还是直接执行。

---

*North Star 裁决句*：plan 的**内容**来自 agent 的智能（L1，不机械约束其思考）；plan 的**治理字段**（wake/cost/risk/handoff）是 Hive 相对 CC 的合理 delta（L3 控制中台），但**作为系统数据存在、不作为 plumbing 平铺给用户**；缺失的 AskUserQuestion 是 CC 基线能力，必须补齐——**先对齐 CC 基线（plan 主体 + 澄清 + 场景门控），再谈 Hive delta**。

*修订记录: v0.1 2026-06-07 初稿（生产 RWA plan 实证触发，CC 源码三工具对照 + Hive file:line + delta 性质分类）。*
