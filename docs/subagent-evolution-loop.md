# Subagent 进化闭环：记忆 → 定义晋升（个体层）

> **状态：v0 讨论稿（2026-06-05），待拍板。** 单一核心：一个 subagent 定义如何用自己的运行经验改进自己的 定义.md。组织层（晋升公司库）显式排除在外，归 `docs/org-agent-asset-rights-model.md` §6。
>
> 起点 = 用户架构质疑（2026-06-05）："Sub-agent 应该是一个单独的进化路径——memory 积累，最终晋升空间是它自己的 定义.md 改进，它不会干其他事。"核对结论：前半句已成立（§13.2 修后记忆只进自有 memory.md），后半句**通道不存在**——本文档补这条通道。

---

## 1. 病灶：只有积累、没有消化的断头进化

现状链（§13.2 修复后）：

```
spawn 运行 → LLM 蒸馏 How-craft → <name>.memory.md 追加（write gate 把关）
                                        ↓ 每次 spawn
                              全量注入 standalone prompt（## Subagent Memory）
```

三个后果：

| # | 后果 | 机理 |
|---|------|------|
| 1 | **注入膨胀** | memory.md 只进不出，条目无上限；每次 spawn 全量注入，prompt 越来越肥 |
| 2 | **craft 不固化** | 被反复验证的经验永远以"工作笔记"形态旁挂，不会成为定义本体（system prompt body）的一部分——定义是 subagent 的"本体"，记忆只该是尚未成熟的増量 |
| 3 | **与主金字塔不对称** | 主 agent：T2 有 heartbeat 策展、T3 有 dream 晋升进 soul + archive.md 可逆退役。subagent：蒸馏一层后断头 |

切口⑥当时刻意只做一层（"One layer only…No T3/soul/dream"——为了 governed-write 不变量先落地），这是排期债不是设计错误；权责利讨论稿已把"subagent 无晋升 lane"列为 L1 病灶。本文档解**个体层**（记忆→自己的定义）；组织层（定义→公司库快照）依赖 §6 拍板，只在 §7 划接口。

## 2. 对标：CC 基线 + Hive delta

**CC 基线**：agent memory = `agent-memory/<name>/MEMORY.md` 注入（Hive 现状同构）。CC **没有**"memory→定义自动改写"——工程师用户手动让模型改 `agents/<name>.md` 即可。

**Hive delta 的正当性**（为什么 Hive 必须有而 CC 可以没有）：① Hive 的数字员工长期无人值守运行，"手动让模型改定义"不可依赖；② 北极星 Goal 1 要求 self-evolution 是基础设施而非用户操作；③ Hive 已有 CC 没有的承载基建——approval 流、evolution_ledger（candidate→eval→promotion 审计链）、write gate。

**Hive 内部范式对齐**（第四条 lane，零新范式）：

| 已有 lane | 形态 | 本闭环复用什么 |
|-----------|------|----------------|
| skill candidate lane | 蒸馏→候选→独立审核 | "候选不直接生效"心智 |
| workflow promote | 运行证据→提名→审批→registered | 证据驱动提名 + 人审 gate |
| dream（T3→soul） | 晋升 + lifecycle patch + archive 可逆退役 | **吸收必须伴随退役** |
| evolution_ledger | candidate→eval→promotion JSONL 链 | provenance 落账 |

## 3. 设计原则

1. **Plan 来自 agent，治理归系统**：改进稿由 LLM 起草；写回必经审批与校验链；系统记 provenance。
2. **吸收必须伴随退役**（防熵增铁律）：craft 进 body 的同时，对应记忆条目标记 absorbed 并从注入中过滤——闭环必须收敛，绝不双份生效。
3. **快照 + provenance**（对齐解耦律二）：每次写回是新版本事件，进 evolution_ledger；旧文本可从 ledger 回溯。
4. **How-not-What 不变**：吸收进 body 的只能是工作方法；领域知识仍然禁止沉淀在 subagent 任何一层。
5. **v1 只动 body，不动契约**：frontmatter（tools/model/max_tool_rounds/isolation）是行为契约，自动变更=权限面变更，v1 代码级禁止（见 §6）。
6. **模型平等（L3）**：起草提示词 vendor-neutral，钉子测试同款。

## 4. 核心机制

```
<name>.memory.md 积累
   │  触发：active 条目 ≥ N 且无 pending proposal（spawn 蒸馏写入后内联检查，事件驱动）
   ▼
LLM 起草（输入=当前定义全文+全部 active 记忆条目；输出=修订版定义.md + absorbed 条目 id + rationale）
   │  约束：frontmatter 与 base 完全一致（只动 body）；产物过同一 parse/render 校验链
   ▼
proposal 落盘 <workspace>/subagents/.proposals/<name>.proposal.md + 通知 owner
   │  人审（manage 权限）：前端 diff 预览 → 批准 / 拒绝
   ▼ 批准
写回 定义.md（复用 PUT 校验链）+ evolution_ledger 落账 + absorbed 条目标记
   ▼
下次 spawn：_load_subagent_memory 过滤 absorbed 条目 → 注入瘦身兑现
```

### 4.1 触发（提名）

- **位置**：`_record_memory_from_result` 蒸馏写入成功后内联检查（事件驱动，零新 daemon、零调度依赖）。
- **条件**：active（未 absorbed）条目数 ≥ N（默认 **8**，可 env 调）且该定义无 pending proposal。
- **范围**：仅 **agent 级定义**。tenant 级（公司库资产，多 agent 共享，变更影响面大）与 builtin（只读模板）v1 不提名。inline spec（无定义文件）天然无此路径。
- 备选过 heartbeat SOP 周期扫描：刻意不选——蒸馏器行为归 SOP 模板（heartbeat≠worker 教训），把 subagent 提名塞进主 agent SOP 是职责窜位；事件驱动更准时且无新面。

### 4.2 起草（LLM，平台调用）

- 走 `chat_complete`（同 HR refine / 生成器 / 蒸馏器先例），用父 agent primary model。
- 输入可见性完整（L1）：定义全文 + 全部 active 条目，不截断。
- 输出 JSON：`{"body": 修订后完整 system prompt, "absorbed_entry_ids": [...], "rationale": 一段话}`。
- 提示词要点：吸收的是**反复出现/普适**的 craft；孤例留在记忆；body 改写保持原有角色骨架（增量编辑非重写）；语言跟随定义语言；禁 vendor 名。
- 起草产物在落盘 proposal 前先 `parse_subagent_definition` 验证（frontmatter 不动 + body 替换后必须合法），失败 → log + 放弃本次提名（fail-soft，下次蒸馏再触发）。

### 4.3 审核（v1：人审 only）

- proposal 文件：`<workspace>/subagents/.proposals/<name>.proposal.md` —— frontmatter 记 `base_definition_sha`（防 TOCTOU：批准时定义已被人改 → 拒绝过期 proposal）、`absorbed_entry_ids`、`rationale`、`created_at`、`status: pending`；body = 修订后完整定义文本。
- 通知：复用 notification 服务通知 agent owner；配置面 Sub-agents tab 列表行加"待审改进"徽标。
- 批准 = 前端 diff 预览 → 调用新端点 `POST /agents/{id}/subagents/{name}/proposal/approve`（内部走与 PUT 同一校验+写回）→ ledger 落账 → 标记 absorbed → proposal 置 approved。拒绝 = status: rejected 留档（同样进 ledger，负样本可供后续校准）。
- **无自动批准**。低风险自动审（Asset Curator）是组织层概念，归 §6。

### 4.4 吸收 + 退役

- memory.md 条目行追加 `[absorbed=<proposal_id>]` 标记（对齐 dream 的 archive 可逆语义：标记不删除，人工可回滚）。
- `SubagentMemoryStore.load` 增 `active_only=True` 路径；spawn 注入走 active_only。
- ledger 行：`{"kind": "subagent_definition_promotion", "name", "agent_id", "proposal_id", "base_sha", "new_sha", "absorbed_ids", "approved_by", "ts"}`。

## 5. 数据与文件布局（MD-first，零新表）

| 物 | 位置 | 说明 |
|---|------|------|
| proposal | `<workspace>/subagents/.proposals/<name>.proposal.md` | dot-dir 避开定义 glob（同 `.memory/` 手法）；状态机在 frontmatter |
| absorbed 标记 | memory.md 条目行内 | 追加标签，不迁移文件 |
| 审计 | `evolution_ledger.jsonl` | 复用现有链 |

文件状态机弱于 DB（并发/查询），但 subagent 定义本来就是文件域、owner 单人审批、提名互斥（无 pending 才触发），v1 可接受；若组织层入库需要强查询再升 DB。

## 6. 治理与硬边界

1. 写回必经 `parse_subagent_definition`（与 PUT 同链，无第二 schema）。
2. **契约字段冻结**：approve 端点 enforce 修订版 frontmatter 与 base 逐字段一致，否则 422——"prompt 吸收"与"权限变更"在代码层物理隔离。
3. `base_definition_sha` 不匹配当前文件 → 409 过期拒绝。
4. 提名/起草/审批每步可观测（log + ledger）；起草失败绝不阻塞 spawn 主流程。
5. 本闭环**不新增任何运行时权力**（§12.9 不变量延续）：spawn 时的工具收窄、防递归、write gate 原样。

## 7. 与组织层的接口（划界，不实装)

个体层的 ledger 链（版本 N、absorbed 证据、approved_by）正是组织层"快照入库晋升"审核所需的 provenance 输入。两层共享"证据驱动 + 独立审核 + 快照语义"，但触发器、审批人、目标存储完全不同——组织层等 `org-agent-asset-rights-model.md` §6 拍板后另切。

## 8. 切口路线

| 切口 | 内容 | Red tests | 验收 |
|------|------|-----------|------|
| **P0 退役原语** | memory store：absorbed 标记 + `load(active_only)` + spawn 注入走 active_only | 标记后 load 默认不含/全量含；注入过滤 | 闭环的"出口"先于"入口"存在，任何吸收立即兑现瘦身 |
| **P1 提名+起草** | 触发条件检查 + LLM 起草 + proposal 落盘 + 通知 | 阈值触发/互斥/起草产物过校验/失败 fail-soft/提示词 vendor-neutral 钉子 | spawn N 次后 .proposals/ 出现合法 proposal |
| **P2 审核面** | approve/reject 端点 + 契约冻结 enforce + base_sha 校验 + ledger + 前端 diff 预览/徽标 | 422 契约变更拒/409 过期拒/批准写回+absorb+ledger 原子序 | owner 在配置面完成一次完整批准，定义更新且记忆瘦身 |
| **P3 实证 eval**（可选） | absorbed 后注入长度下降 + 定义行为不回退冒烟 | — | 数字证据进 ledger traces |

## 9. 非目标

- 组织层入库晋升（§6 权责利）
- 契约字段（tools/model/rounds/isolation）的任何自动变更
- tenant 级 / builtin 定义的自动进化
- 跨 subagent 经验合并、记忆共享
- 自动批准（含 Asset Curator）

---

## 待拍板

1. 触发阈值 N=8 默认值（env 可调）是否接受？
2. 审批人 = agent owner（manage 权限），v1 无自动批准——确认？
3. proposal 文件态（零新表）vs 入 DB——v1 文件态，确认？
4. P0→P2 顺序（出口先于入口）——确认后按切口 TDD 实装。
