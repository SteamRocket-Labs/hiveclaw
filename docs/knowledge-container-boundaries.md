# 知识容器边界 — soul / memory / skill / workflow 的职责与流动（设计讨论稿）

> 2026-06-04。回答三个问题：①什么进 soul.md、什么进 skill？②memory/*.md 以什么方式组成上下文？③四者（含 workflow）之间的关系是什么？
> **讨论稿——只定边界与原则，不含实现切口；拍板后再谈改动。**
> 上位法：AI-Native Design Law（CLAUDE.md）；相关既有设计：workflow-source-capability.md、agent-lifecycle-cc-alignment.md §3.6。

## 1. 第一性原理：按「知识的性质」分容器，按「稳定性」定披露方式

四个容器对应认知科学里四类知识，判据各自一句话：

| 容器 | 知识性质 | 一句话判据 | 变化速度 | 执行者 |
|---|---|---|---|---|
| **soul.md** | 身份（identity） | 这条信息是否改变 agent **在任何任务中**的行为方式？ | 最慢（dream 4h+3sessions 才动；DREAM.md prompt 上限指引 ≤20 + 预算硬约束，runtime 无硬 cap——codex 核实） | —（是约束不是程序） |
| **memory/*.md (T3)** | 陈述性记忆（facts） | 这是关于世界/用户/项目的**事实**（可能过期、有置信度），而非"怎么做事"？ | 中（heartbeat 45min 蒸馏） | — |
| **skill (SKILL.md)** | 程序性方法论（procedural） | 这是教模型**怎么做某类事**的指令集，且执行中需要模型灵活判断/适配？ | 慢（distiller 凝练 + curator 防熵增） | **LLM 在循环里解释执行** |
| **workflow** | 确定性流程（deterministic SOP) | 这个流程的**步骤顺序/分支/重试必须由系统保证**而不是靠模型自觉？失败需可重放、每步需可审计？ | 最慢（版本化+hash 绑定） | **引擎执行**（leaf 可以是 agent step） |

### 例与反例（边界处最容易混的六组）

| 信息 | 归宿 | 为什么 |
|---|---|---|
| 「用户拒绝 emoji，永远纯文本」 | soul（Learned Behaviors） | 改变所有任务中的行为；从 feedback.md 重复 N 次后 promote |
| 「客户 A 的预算周期是季度末」 | memory/knowledge.md | 事实，会过期，只在涉及客户 A 时相关 |
| 「research→design→verify 三段法产出的 PR 评审更好」 | 先 memory/strategies.md，重复确认后 → **skill** | 初期是观察（事实），稳定后是方法论（程序） |
| 「周报撰写指南：结构/口吻/数据来源」 | skill | 教模型做事，执行需适配当周内容 |
| 「月度报销审批：收集→校验→审批→打款→归档」 | **workflow** | 步骤刚性、合规审计、失败要重放——模型走偏不可接受 |
| 「HEARTBEAT.md / DREAM.md」 | 第五类：**系统 SOP 模板**（平台级，per-agent 可覆盖） | 平台蒸馏器的操作手册，不属于 agent 的自学知识；见 §5 |

### skill vs workflow 的核心判据（你问的重点）

**"如果模型在某一步走偏了，后果可接受吗？"**

- 可接受（重试/自纠即可，且任务需要适应性）→ **skill**。skill 是模型的方法论，灵活性是特性。
- 不可接受（资金/合规/外部承诺/必须可重放审计）→ **workflow**。workflow 是引擎的流程，刚性是特性。

**长期 SOP 的归宿（你的判断我认同并补一刀）**：长期、步骤稳定的 SOP 最终沉淀在 workflow——但真实 SOP 多数是**混合体**（刚性骨架 + 需要判断的环节）。答案不是二选一，而是 **workflow 做骨架、skill/agent_step 做关节**：workflow 定义步骤序列与 gate（引擎保证），其中需要智能的 leaf 是 agent step（可挂 skill）。这正好对上 workflow 引擎已有的 leaf 设计——SOP 的"刚性部分"与"判断部分"各归其位，而不是整个 SOP 塞进任一边。

## 2. 硬化光谱：知识的生命周期是一条单向硬化路径

```
经历                 事实                成熟度分叉                         最终形态
T0 logs ──extract──▶ T2 learnings ──heartbeat──▶ T3 memory/*.md
                                                    │
                                  ┌─────────────────┼──────────────────┐
                                  ▼                 ▼                  ▼
                          重复+高信号的         重复出现的            （保持为事实，
                          行为级原则           "做法"模式             过期则衰减）
                                  │                 │
                            dream promote     skill_distiller
                                  ▼                 ▼
                              soul.md            SKILL.md
                          （人格化，cap 20）  （方法论，curator 防熵增）
                                                    │
                                          步骤稳定 + 需要刚性保证
                                                    │
                                        workflow promote suggestion (P13)
                                                    ▼
                                                workflow
                                          （确定性，版本化+审计）
```

三条 promote 通道**全部已存在**（dream promotion、skill_candidate_loop→skill_distiller、workflow_promote_suggestions）——本设计不新建机制，只补判定纪律。

**光谱的含义**：越往右越稳定、越刚性、**prompt 成本越低**（见 §3）。知识从"昂贵的上下文"硬化为"廉价的结构"，这就是自我进化在经济学上的意义。

## 3. 上下文组装：披露梯度 = 稳定性梯度（回答第二问）

| 容器 | 在上下文中的形式 | 位置 | 成本特征 |
|---|---|---|---|
| soul.md | **全文常驻**（身份必须每轮在场） | frozen prefix | 固定但 cache 命中；靠 `soul_budget_chars` 严格预算 + dream 少量高置信晋升控制膨胀 |
| memory T3 | **两级（目标协议，方向已对齐但未完全协议化）**：feedback.md/blocked.md 高优先直注；knowledge/strategies/user 按 query **检索激活**（keyword+权重初筛 → LLM rerank，A3 已接线）。⚠️ codex 核实：`MemoryAssembler` 仍把命中项渲染为 bullet 全文，尚未稳定为"常驻索引 + 少量全文 + `load_memory(ids)` 按需展开"协议（md_store manifest/load_memory 地基已在） | dynamic suffix | 按预算，60% memory budget |
| skill | **渐进披露**：catalog（名+描述）常驻，全文 `load_skill` 按需（C2 已加固防失明） | catalog 在 frozen prefix，body 进对话 | 索引便宜，全文按需 |
| workflow | **仅引用**：作为可调用对象（工具/trigger.workflow_ref 绑定），prompt 里只有"它存在"的认知 | 近零 | 执行时引擎接管，不占上下文 |

这个矩阵不是巧合：**知识越不稳定越需要按需检索（省上下文），越稳定越值得常驻（身份）或下沉为结构（workflow）**。现有架构的梯度方向已对齐；memory 的组装仍需协议化（见 §8 P2），不是"无需改动"。

## 4. 边界细则（模糊带裁决）

1. **soul vs memory/feedback.md**：feedback 是逐条事实（带日期、可过期）；soul 是蒸馏后的原则（近不变）。**纪律：正式路径不允许 agent 自行直写 soul（Memory Guide + Plan Mode policy 约束）；charter/dream/approval 路径例外**。⚠️ codex 核实：filesystem tool 描述仍说 soul.md 可谨慎写入——与纪律冲突，列入 §8 P0 文案对齐。soul 条目若被后续 feedback 持续矛盾 → dream 应降级回 T3（现状 dream 有 rewrite/dedup 能力，降级是其用法之一）。
2. **memory/strategies.md vs skill**：strategies 是"观察到某做法有效"（事实陈述）；skill 是"这样做"（可执行方法论）。**纪律：strategies.md 是 skill 的孵化器**——repeat 高的 strategy 是 skill_distiller 的第一候选源。strategy 一旦凝练成 skill，T3 里的原条目应标注 `[promoted_to_skill=X]` 避免双源漂移（这是现状没有的小纪律，待拍板）。
3. **skill vs workflow**：见 §1 判据。补充：**skill 永远不该包含"必须严格按序、失败需重放"的承诺**——写出这种句子的 skill 就是 workflow 候选。workflow promote suggestion（P13）应该把这个作为检测信号之一。
4. **混合 SOP**：workflow 骨架 + agent_step 关节（可挂 skill）。**反模式**：把整个混合 SOP 写成超长 skill 然后祈祷模型每次都遵守刚性步骤——这违反"软约束适用性"边界（CC 哲学的软约束是给适应性任务的，不是给合规流程的）。
5. **第五类：系统 SOP 模板**（HEARTBEAT.md/DREAM.md/EXTRACT_PROMPT）：平台蒸馏器的操作手册，**不在 agent 的四容器内**——它们是 runtime 的一部分（per-agent workspace/HEARTBEAT.md 覆盖=配置不是记忆）。纪律（来自 B4 教训，[[heartbeat-not-worker]]）：改蒸馏器行为改模板本身，不要 runtime 旁路注入。

## 5. 现状审计：已对齐 vs 待拍板的优化项

**已对齐（无需动）**：四容器披露梯度（§3）、三条 promote 通道、soul cap、write gate、skill curator 防熵增、C1/C2/A3/dream full-fidelity（本轮已修）。

**待拍板的优化项**（按价值排序，均为纪律/提示词级，非新机制）：

| # | 项 | 内容 | 改动面 |
|---|---|---|---|
| O1 | **判定边界进蒸馏器提示词** | HEARTBEAT.md/DREAM.md/skill_distiller 的 prompt 中加入 §1 判据表——蒸馏器在分流时显式回答"这是事实/原则/方法论/流程候选？"。目前 HEARTBEAT.md 只有 T3 文件选择矩阵，没有"这该不该是 skill/workflow 候选"的抬头 | 三个模板文件 |
| O2 | **strategy→skill 的去双源纪律** | strategy 凝练成 skill 后原 T3 条目标注 promoted_to，dream dedup 时识别 | skill_distiller + DREAM.md |
| O3 | **workflow 候选检测信号** | skill 文本含刚性承诺语（"必须严格按序"/"不得跳过"/"审计要求"）时，workflow_promote_suggestions 抬升优先级 | promote suggestions 启发式 |
| O4 | **soul 降级通道显式化** | DREAM.md 明示"soul 条目被持续矛盾时降级回 T3"的操作（现状只有 promote 方向的指引） | DREAM.md |

## 6. 待你拍板

1. §1 的四容器判据表 + skill/workflow 核心判据（"走偏后果可接受吗"）是否认可为正式边界？
2. 混合 SOP 的裁决（workflow 骨架 + skill 关节，反对超长刚性 skill）是否认可？
3. O1-O4 哪些做、顺序如何？（我的建议：O1 价值最高——把边界交给蒸馏器的 LLM 去执行，正是 AI-native 的做法：判定标准写进 prompt，而不是写死成代码规则）

## 7. Codex 复核反馈（2026-06-04）

整体结论：四容器方向是对的，尤其是 **workflow 骨架 + skill 关节** 这个判断。真正需要收紧的是“容器边界如何落到当前代码事实”，避免文档把还没有被运行时强约束的内容写成已完成机制。

### 7.1 我认可的核心边界

1. **soul.md = 身份与长期约束**
   它应该回答“这个 agent 是谁、为谁负责、边界是什么、什么叫好结果”。当前 runtime 会把 soul 读入身份段，属于 frozen prefix，因此只应放 6 个月后仍成立的身份级内容。当前任务、日期、工具配置、触发器、workflow definition、文件路径都不应进入 soul。

2. **memory/*.md = 可检索的长期事实与经验**
   T3 应该保存“我知道什么”：用户偏好、项目事实、失败模式、有效策略、外部参考。它不应该被当成稳定身份，也不应该被全文无脑常驻。正确方向是 query-aware activation：高优先安全/纠正类少量直注，其余用 index/search/load 展开。

3. **SKILL.md = 可复用的认知方法/操作指南**
   Skill 应该保存“我怎么做某类事”：判断标准、rubric、工具注意事项、示例、模板、脚本。它是 LLM 在循环内解释执行的方法论，适合开放任务、研究任务、写作任务、需要场景适配的操作。它不应承诺严格 exactly-once、审计、暂停恢复或版本 hash 绑定。

4. **workflow = 确定性 SOP / 自动化控制流**
   一旦流程有固定步骤、强 gate、可恢复 journal、预算 envelope、trigger 绑定、审批和审计要求，就应该沉淀为 workflow。SOP 不是 skill 的终态；稳定 SOP 的终态是 workflow，skill 只服务 workflow 中需要模型判断的 leaf。

### 7.2 需要按当前实现修正的表述

1. **不要写“soul cap 20”作为已实现事实**
   当前代码事实更像是：soul 受 `ContextBudget.soul_budget_chars`、frozen prefix warn/hard limit、dream 高置信晋升纪律约束；我没有看到一个明确的“20 条硬 cap”在 runtime 热路径里强制执行。建议把 §3 和 §5 里的 “cap 20” 改成“严格预算 + dream 少量高置信晋升”，除非另有实现点可引用。

2. **memory 的“两级披露”应明确为目标协议，而不是说“无需改动”**
   当前 `md_store.py` 已经有 entry manifest 和 `load_memory(ids)`，`search_memory` 也会返回 id/preview/load hint，这是正确方向。但 `MemoryAssembler` 仍主要把命中的 memory item 渲染成 bullet 全文；还没有完全变成“常驻索引 + 少量全文 + 按 ID batch load”的稳定协议。建议 §3 的结论从“四容器披露方式无需改动”改成“方向已对齐，但 memory assembler 还需要协议化”。

3. **skill_distiller 与 workflow_promote_suggestions 存在交叉分流风险**
   文档说三条 promote 通道都已存在，这是对的；但现在 repeated workflow signal 同时可能被 skill flywheel 推成 skill candidate，也可能被 workflow promote suggestion 推成 workflow candidate。需要一个显式分流器：
   - 认知方法、rubric、写作/研究/判断流程 → skill candidate
   - 固定步骤、外部副作用、审计/恢复/预算/审批需求 → workflow candidate
   - 混合 SOP → workflow definition + agent_step/skill leaf

4. **“soul 绝不直写”是产品纪律，不是所有工具层的硬事实**
   Memory Guide 和 Plan Mode policy 都在约束 soul 写入，但 filesystem tool 的描述仍说 soul.md 可以谨慎写入。文档应把它写成“正式路径不允许 agent 自行直写；charter/dream/approval 路径例外”，避免和实际工具描述产生冲突。

5. **Skill 文档里的 `<workflows>` 标签应改名**
   系统 skill 模板里大量使用 “Workflows” 作为普通操作流程标题，这会和产品级 Workflow 混淆。建议后续统一改成 `<usage_flows>` / `<procedures>` / `<recipes>`。产品语义里，Workflow 应保留给可执行、版本化、可审计的 runtime definition。

### 7.3 我建议的落地顺序

**P0：先改文案和 prompt 边界，不动执行机制。**

- `backend/app/tools/handlers/skills.py`：把 `save_skill` 描述里的 “Save a reusable workflow as a skill” 改成 “Save a reusable capability guide as a skill”。
- `backend/app/runtime/prompt_sections/memory.py`：把 “T3 (memory/*.md + soul.md)” 拆成 “T3 memory/*.md” 与 “soul.md top identity”，避免把 soul 说成普通 T3 文件。
- `backend/app/templates/system_skills/*/SKILL.md`：把面向 agent 的 “Workflows” 标题逐步改成 “Usage Flows / Procedures”，保留产品 Workflow 的专名。

**P1：把分流判据写进蒸馏器。**

- Heartbeat/Dream/Skill Distiller 在候选生成时都必须先回答：这是 `fact`、`identity_principle`、`cognitive_method`，还是 `deterministic_sop`？
- `deterministic_sop` 不进入 skill，进入 workflow promote suggestion。
- `cognitive_method` 才进入 skill candidate。

**P2：把 memory 组装协议化。**

- P0/P0-like 高优先记忆：少量全文。
- 旧 P0、P1、P2：默认 index line，含 `id/category/source/preview/load hint`。
- 模型需要依赖 preview 时，必须 `load_memory(ids=[...])` 展开全文。

### 7.4 最终裁决句

可以把这句作为本文的正式 north star：

> `soul.md` 定义“我是谁”，`memory/*.md` 保存“我知道什么”，`SKILL.md` 教“我如何判断和操作”，`workflow` 执行“我按什么确定流程运行”。越接近身份越常驻，越接近事实越检索，越接近 SOP 越下沉到引擎。

## 8. 三方对照与修订决议（2026-06-04：codex §7 + hermes + GenericAgent 调查后）

### 8.1 三方验证矩阵

| 判据/设计点 | hermes | GenericAgent | codex | 结论 |
|---|---|---|---|---|
| 四容器职责分离 | ✅ SOUL/MEMORY+USER/SKILL 三层完整验证（workflow 缺失） | ⚠️ 无 soul（身份模式绑定）、SOP-as-memory | ✅ 认可全部四条边界 | **采纳**；workflow 是 Hive 对两个参照系的真 delta |
| 披露梯度=稳定性梯度 | ✅ 完全一致（冻结快照+按需加载） | ✅ L1 常驻→L2 全局→L3 按模式/按需 | ✅ 方向认可、memory 组装需协议化 | **采纳**+P2 协议化 |
| skill vs workflow（"走偏后果"判据） | （无 workflow，刚性 SOP 只能写成 skill = 我们点名的反模式实证） | Checklist+Verify = **轻量刚性中间态**（prompt 纪律+状态文件+监察，无引擎保证） | ✅ "SOP 不是 skill 的终态；稳定 SOP 终态是 workflow" | **采纳**，并吸收 GA 的**按需硬化**哲学：SOP 初期可以是 skill+checklist（低成本），刚性需求出现才升 workflow——硬化是按需的，不是强制的 |
| 混合 SOP = workflow 骨架 + skill 关节 | — | plan_sop 的 [D]/[P]/[?] 标注本质就是"骨架+关节"的 prompt 版 | ✅ 明确认可 | **采纳为正式裁决** |
| soul 必须小 | SOUL ~20k chars 上限、职责边界清单（✓ tone/✗ 路径命令） | 干脆无 soul（token 经济学极端解） | "只放 6 个月后仍成立的身份级内容" | **采纳 codex 表述**（GA 的无 soul 不采纳——企业数字员工需要跨任务身份一致性，但它警示 soul 膨胀的代价） |

### 8.2 三方贡献的新输入（设计稿此前没有的）

**来自 codex（已并入正文修正）**：①soul cap 20 是 prompt 指引非 runtime 硬 cap；②memory 两级披露是目标协议（assembler 未协议化）；③**skill_distiller 与 workflow_promote_suggestions 交叉分流风险**——同一个 repeated workflow signal 可能被两边各推一份，需要显式分流器；④soul 直写纪律与 filesystem tool 描述冲突；⑤system skills 的 `<workflows>` 标签与产品 Workflow 撞名。

**来自 hermes**：①curator 是 **agent fork** 而非规则引擎（AI-native 范本——长期方向：dream Step 2 的 pattern promotion 也应由 LLM 主导）；②skill 带 `.usage.json` 定量遥测（use_count/last_used_at）——晋升判定有数据不靠感觉；③pin + 变更前自动备份（用户不怕 agent 动自己的 skill）；④MEMORY 与 USER 拆分（Hive T3 已有 user.md ✓）。

**来自 GenericAgent**：①**按需硬化**哲学（见 8.1 表）；②verify_sop 的对抗性验证框架——"必须有工具证据，无证据的 PASS=作废"、产物类型×必做检查表、VERDICT 三态——可直接进 workflow gate 与 verify 类 skill；③L1 索引的 **ROI token 经济学**（≤30 行、反直觉触发词、"命名自解释>加描述"）——skill catalog 描述纪律的范本；④时间预算驱动的质量递进（"预算耗尽即停"vs"完成即停"）——goal/long-task 模式可借鉴。

### 8.3 统一落地路线（取代 §5 的 O1-O4，吸收 codex P0-P2）

**P0 — 文案与 prompt 边界对齐（不动执行机制）**：
1. `tools/handlers/skills.py`：save_skill 描述 "Save a reusable workflow as a skill" → "Save a reusable capability guide as a skill"
2. `runtime/prompt_sections/memory.py`：拆分 "T3 (memory/*.md + soul.md)" 表述——soul 是 top identity 不是普通 T3 文件
3. `templates/system_skills/*/SKILL.md`：`<workflows>` 标签 → `<procedures>`（为产品 Workflow 保留专名）
4. filesystem tool 的 soul.md 写入描述对齐"正式路径不直写；charter/dream/approval 例外"

**P1 — 分流判据进蒸馏器（原 O1+O2+O3+O4 合并，codex P1 形态）**：
1. HEARTBEAT.md / DREAM.md / skill_distiller prompt 加四分类抬头：候选先回答这是 `fact` / `identity_principle` / `cognitive_method` / `deterministic_sop`？
2. `deterministic_sop` → workflow promote suggestion，**不进 skill**（解决交叉分流）；`cognitive_method` → skill candidate；刚性承诺语（"必须严格按序"/"审计要求"）是 deterministic_sop 的检测信号
3. DREAM.md 补 soul 降级通道（被持续矛盾 → 降回 T3）
4. strategy→skill 凝练后 T3 原条目标注 `[promoted_to_skill=X]`

**P2 — memory 组装协议化**：高优先（P0 级）记忆少量全文；其余渲染为 index line（id/category/source/preview/load hint）；模型需要详情时必须 `load_memory(ids=[...])` 展开。

**P3 — 借鉴项（拍板后排期）**：skill `.usage.json` 式 provenance 遥测进晋升判定（hermes）；pin+backup（hermes）；verify 对抗框架进 workflow gate（GA）；skill catalog 描述的"反直觉触发词"纪律（GA）；时间预算驱动的 goal 模式（GA）。

### 8.4 正式 North Star（codex 裁决句，三方验证后采纳）

> `soul.md` 定义"我是谁"，`memory/*.md` 保存"我知道什么"，`SKILL.md` 教"我如何判断和操作"，`workflow` 执行"我按什么确定流程运行"。越接近身份越常驻，越接近事实越检索，越接近 SOP 越下沉到引擎。**硬化是按需的：刚性需求出现时才升级，不为硬化而硬化。**
