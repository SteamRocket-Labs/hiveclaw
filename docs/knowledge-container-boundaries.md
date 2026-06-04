# 知识容器边界 — soul / memory / skill / workflow 的职责与流动（设计讨论稿）

> 2026-06-04。回答三个问题：①什么进 soul.md、什么进 skill？②memory/*.md 以什么方式组成上下文？③四者（含 workflow）之间的关系是什么？
> **讨论稿——只定边界与原则，不含实现切口；拍板后再谈改动。**
> 上位法：AI-Native Design Law（CLAUDE.md）；相关既有设计：workflow-source-capability.md、agent-lifecycle-cc-alignment.md §3.6。

## 1. 第一性原理：按「知识的性质」分容器，按「稳定性」定披露方式

四个容器对应认知科学里四类知识，判据各自一句话：

| 容器 | 知识性质 | 一句话判据 | 变化速度 | 执行者 |
|---|---|---|---|---|
| **soul.md** | 身份（identity） | 这条信息是否改变 agent **在任何任务中**的行为方式？ | 最慢（dream 4h+3sessions 才动，cap 20 条） | —（是约束不是程序） |
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
| soul.md | **全文常驻**（身份必须每轮在场） | frozen prefix | 固定但 cache 命中；靠 cap 20 控制膨胀 |
| memory T3 | **两级**：feedback.md/blocked.md 高优先直注；knowledge/strategies/user 按 query **检索激活**（keyword+权重初筛 → LLM rerank，A3 已接线） | dynamic suffix | 按预算，60% memory budget |
| skill | **渐进披露**：catalog（名+描述）常驻，全文 `load_skill` 按需（C2 已加固防失明） | catalog 在 frozen prefix，body 进对话 | 索引便宜，全文按需 |
| workflow | **仅引用**：作为可调用对象（工具/trigger.workflow_ref 绑定），prompt 里只有"它存在"的认知 | 近零 | 执行时引擎接管，不占上下文 |

这个矩阵不是巧合：**知识越不稳定越需要按需检索（省上下文），越稳定越值得常驻（身份）或下沉为结构（workflow）**。现有架构已经按这个梯度建好了——四容器的披露方式无需改动。

## 4. 边界细则（模糊带裁决）

1. **soul vs memory/feedback.md**：feedback 是逐条事实（带日期、可过期）；soul 是蒸馏后的原则（近不变）。**纪律：soul 只收 dream 从重复模式 promote 的条目，绝不直写**（write gate 已挡）。soul 条目若被后续 feedback 持续矛盾 → dream 应降级回 T3（现状 dream 有 rewrite/dedup 能力，降级是其用法之一）。
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
