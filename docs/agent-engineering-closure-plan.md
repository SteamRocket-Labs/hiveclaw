# Agent 工程闭环一次性计划（Engineering Closure Plan）

> 状态: **v0.1 草案（2026-06-07）——待拍板**。双 AI 交叉 review（Claude 三日审计线 × Codex 只读排查）合成；§0 每条裁决均经源码二次验证，非单方采信。
> 范围: **Agent 本身的运行时**——① Max Token / 限制机制 ② Runtime 四元能力（Subagent / Skill / MCP / Workflow）③ Memory。把全部已验证的缺陷与断点一次性补齐。
> 完成定义（工程闭环）: 配置面承诺的语义在执行路径全部兑现；管线状态面板说真话；与 CC 的机制级差距清零或有定稿路线并执行完毕；每项红测先行、全量绿。**工程闭环 ≠ 生产实证**——生产验收（挂账 #7 五点清单）在本计划完成后放真实流量另案执行。
> 证据基线: 行号以 2026-06-07 HEAD `02fb8322` 为准，实施前按符号重定位，勿盲信行号。
> North Star 对齐: 服务 Goal 1（自我进化 agent 内核）；裁决镜头 = AI-Native 设计法律（L1 视野/预算/提示词 → L2 约束不替代 → L3 模型平等）+ "Hive = CC superset，先对标 CC 基线再谈 delta"。

---

## §0 交叉 Review 裁决表（计划的事实基础）

双方独立得出结论后逐条对源码验证。✅=主张成立进计划，❌=误报不进，⚠️=半成立按实情进。

| # | 主张（提出方） | 裁决 | 源码证据 |
|---|---|---|---|
| 0.1 | save_memory 不传 tenant_id → Hindsight immediate sync 对 agent 工具写入永跳过（Codex） | ✅ 真缺陷 → **A3** | `tools/handlers/memory.py:102` 调用 `append_t3_memory_candidate` 无 tenant_id；`memory/hindsight_sync.py:227` `tenant_id is None → return 0` |
| 0.2 | Memory Navigation 构建不传 PrincipalStack → owner/admin 的 PL3 导航少召回（Codex） | ❌ 误报（生产主路径正确） | 主路径 `runtime/invoker.py:460-484` 解析 `activation_context.principal_stack` 并传入 builder，None 仅为异常 fail-safe；Codex 看到的是 `runtime/prompt_builder.py:710` 的 **build_runtime_prompt 孤儿路径**（P10 已实锤只测试在用）。该孤儿已两次误导 review → **A4 清理** |
| 0.3 | MCP approval 模式在执行侧无强拦截，≈ auto（Codex） | ✅ 真缺陷（本计划最重治理项）→ **A2** | `services/agent_tool_domains/web_mcp.py:971-973` 只拦 `mode == "deny"`；`approval` 零处理直落执行。`mcp_server_service.py:379` 自注释 "``approval`` and ``auto`` are reachable modes" |
| 0.4 | wait_signal + PG durable resume 已落地（Codex；纠正 Claude 旧记忆） | ✅ 非断点，从清单删除 | `services/workflow_signal_consumer.py` docstring 自证 "§9 P11 — the v2 wait_signal backend"；`DELETE ... RETURNING` 原子消费；daemon 在 `main.py:491` 注册 |
| 0.5 | Skill/tool_search 未达 CC "名字常驻、schema 按需加载" 形态（双方一致） | ✅ 形态差距 → **轨道 C** | `tools/handlers/skills.py:156`（tool_search 仍是目录+pack 激活入口）；路线已定稿 `docs/execution-mode-spectrum.md` §4.4/§4.5/§8.3 |
| 0.6 | Memory 架构不推倒重来，定点补齐（双方一致） | ✅ 本计划只做 hardening | T3 唯一写链/直写封禁/navigation 生产默认开均已验证（`memory/t3_store.py` 模块 docstring、`invoker.py:942`） |
| 0.7 | 撞 max_tokens 静默截断，CC 有 64k escalate 重试（Claude；Codex 未提） | ✅ 唯一机制级限制差距 → **A5** | `services/llm_client.py` 记录 `finish_reason`（:118/:362/:459/:584）但全代码库**零消费**——无 length 检测、无重试、无 metric。CC 锚 `query.ts max_output_tokens_escalate` |
| 0.8 | 知识面板 exists≠fresh（Claude；生产实证咬过） | ✅ 真缺陷（验证一切的前提）→ **A1** | `services/knowledge_read_model.py` `_distiller_status` 只判 state 文件存在；2026-06-06 summary-model TypeError 事故期间四管线断一天而面板全绿 |
| 0.9 | T2 learnings 无 retention（Claude） | ✅ 设计缺口 → **B1** | T0 有 30d 清理、T3 有 150 cap + dream 退役，T2 只增不减（`memory/t2_store.py`）；蒸馏管线 06-05 复活后开始真实增长 |
| 0.10 | coordinator + delegation suffix 共享 5000-char trim 互挤（Claude） | ✅ 小缺陷 → **A6** | `kernel/engine.py _effective_suffix` + `prompt_builder._SYSTEM_PROMPT_SUFFIX_CHAR_CAP=5000` |
| 0.11 | subagent 背景 spawn 完成后父自动重入（Claude 记忆标"后续"；Codex 未提） | ⚠️ 半闭环（结果不丢、消费延迟）→ **B2** | `agents/subagent.py:871-875`：背景完成 emit durable Signal + ledger 回写；但父不被唤醒，靠下次 run（heartbeat 2h 兜底）消费。CC 语义 = run_in_background 完成即通知父 |
| 0.12 | 限制/压缩参数已对齐 CC（Codex 盘点：200 rounds / heartbeat 40 / summary 20K / provider 8K-16K / DB override 65,536 / microcompact / 50KB spill 等） | ✅ 无新断点 | 与 2026-06-05~07 预算审计三连（`3d300b48`/`09fac531`/`dc8c1afa`）一致；数值层已收口，仅余 0.7 的机制层 |

---

## §1 轨道 A — 缺陷修复（必修，6 项）

### A1 知识面板 exists≠fresh（量级 S）

- **问题**: `_distiller_status` 只看 state 文件存在即报 `active`，旧文件误报。可观测性失真直接掩盖生产事故（0.8 实证）。修不掉它，后续一切生产验证都不可信。
- **修法**: 按 state 文件 mtime 新鲜度分级 `active / stale / never_ran`；stale 阈值挂各管线节奏（建议 >3×interval：heartbeat 2h→6h、dream 24h→72h、extract 按最近会话活动窗口）。阈值从 Settings 读，不硬编码。
- **改动面**: `services/knowledge_read_model.py`（判定函数）+ 前端 Knowledge 面板状态徽标（en+zh i18n）。
- **红测**: ① 新鲜 state → active；② mtime 超 3×interval → stale；③ 无文件 → never_ran；④ 阈值随 Settings 联动。
- **验收**: 把 state 文件 mtime 人为做旧，面板必须转 stale——复现 0.8 事故场景不再全绿。

### A2 MCP approval 执行侧兑现（量级 M，治理最重项）

- **问题**: `approval` 模式工具运行时与 `auto` 完全等价——UI 承诺审批、执行面静默放行。对企业治理产品是虚假承诺级缺陷。
- **修法**: 执行路径（`web_mcp.py _execute_mcp_tool` 及 read/list 同族入口）resolve 到 `approval` 时接入**既有** approval flow（security zone → capability gate → approval_service checkpoint 体系，不另起炉灶）：未批准 → 创建审批请求并返回 pending 信息（含审批入口指引），不触达 MCP server；批准后放行；`deny` 维持现状硬阻断。审计记录完整（who/what/verdict）。
- **改动面**: `services/agent_tool_domains/web_mcp.py`、`tools/governance_resolver.py` 或 approval 接线层、审批 UI 已有面则零前端改动。
- **红测**: ① approval 模式未批准 → 不执行 + pending 返回 + 审批记录创建；② 批准后同调用放行；③ deny 仍硬断；④ auto 不受影响；⑤ 多租户隔离（A 租户审批不影响 B）。
- **验收**: 配置 approval 的 MCP 工具在无审批时**物理上无法**触达远端 server（断言 MCPClient 未被调用）。

### A3 save_memory tenant_id 接线（量级 S）

- **问题**: agent 经 `save_memory` 工具写入时 Hindsight immediate sync 永跳过（0.1）——开了 Hindsight 加速的租户，工具写入的记忆在读侧加速层不可见，直到下一次全量 sync。
- **修法**: handler 从 ExecutionIdentity ContextVar / session context 取 tenant_id 传入 `append_t3_memory_candidate(tenant_id=...)`。同时 grep 其余 `append_t3_memory_candidate` 调用方做一次 tenant_id 传递一致性审计（防同病灶他处复发）。
- **红测**: 开 Hindsight 的 tenant 经 save_memory 写入 → `sync_t3_to_hindsight` 收到非 None tenant_id（mock 边界仅限外部 Hindsight HTTP，符合 Test Double rationale）。
- **验收**: 调用链 tenant_id 全程非 None；一致性审计零遗漏。

### A4 build_runtime_prompt 孤儿清理（量级 S）

- **问题**: 生产唯一路径是 kernel 回调（P10 已接 Memory Navigation 主路径），`build_runtime_prompt` 只剩测试在用，且因缺 principal_stack 等差异**已两次误导 review**（P10 边界注记 + 本轮 Codex 0.2 误报）。
- **修法**: grep 确认零生产调用方 → 删除函数；挂靠测试迁移到 kernel 路径等价断言或删除。
- **红测**: 删除后全量绿；kernel 主路径 navigation 测试覆盖保持。
- **验收**: `grep -rn build_runtime_prompt app/` 仅余（若有）显式 deprecated 注记，无生产引用。

### A5 escalate-retry-on-cap（量级 M，Max Token 线收口项）

- **问题**: 全部 LLM 调用撞 `max_tokens` 静默截断——内容丢失且零信号。CC 撞 cap 以 64k 干净重试一次（`query.ts max_output_tokens_escalate`）。预算数值已对齐后这是唯一机制级差距；低频但故障形态最阴。
- **修法**（两段式，先可观测后自救）:
  1. **可观测**: `llm_client.py` 统一消费 `finish_reason ∈ {length, max_tokens}` → WARNING log + `llm_output_cap_hit` metric（带调用方标签）。覆盖流式与非流式全部出口。
  2. **escalate 重试**: 非流式 `chat_complete` 路径撞 cap → 以 64k（clamp 到 provider/DB override 上限）**干净重试一次**（重发非续写），仍撞则带截断标记返回并计 metric。六大蒸馏/生成消费方经 `create_llm_client_from_config` 统一工厂自动受益。流式主循环撞 cap 的重试语义对齐 CC 现役行为，实施时先读 CC 源码锚定（`/Users/rocky243/vc-saas/Context Engineering/claude-code-org`），不自创范式。
- **红测**: ① 模拟 finish_reason=length → metric+log；② 非流式撞 cap → 一次 64k 重试成功路径；③ 重试仍撞 → 标记返回不死循环；④ 正常 stop 零开销。
- **验收**: 任何调用点撞 cap 不再静默；蒸馏管线（extract/dream/summarizer/skill_distiller/进化起草）全部在覆盖面内。

### A6 suffix 互挤（量级 S）

- **问题**: coordinator 与 delegation suffix 共享一个 5000-char trim，同时注入互相截尾（0.10）。
- **修法**: 各自独立预算（或合并前分段保护），上限值进常量并注释来源。
- **红测**: 双注入场景两段内容均完整；单注入行为不变。
- **验收**: 极限长度双注入无截尾。

---

## §2 轨道 B — 设计补完（必修，2 项）

### B1 T2 retention（量级 M）

- **问题**: 金字塔四层唯独 T2 无生命周期——T0 30d、T3 150 cap + dream 退役、soul 由 dream 守护，T2 learnings 只增不减（0.9）。蒸馏复活后熵增开始真实累积。
- **修法**（对齐既有语义，不发明新机制）:
  1. heartbeat 策展消化的 T2 条目打 `absorbed` 标记（或以 curation cursor 界定已消化集——实施时选侵入更小者）；
  2. dream 周期把 absorbed 且 age>N 的条目归档到 `memory/archive.md`（de-index 非物删，与 T3 P3 同哲学，可逆）；
  3. cap 兜底：T2 文件条目/字节上限触发最老 absorbed 强制归档。
- **改动面**: `memory/t2_store.py`、`services/heartbeat.py`（标记）、`services/auto_dream.py`（归档）、`templates/DREAM.md` SOP 文案（蒸馏器行为改 SOP 模板，不 runtime 旁路注入——heartbeat≠worker 纪律）。⚠️ 同步 `hr_agent_template/HEARTBEAT.md` 克隆模板。
- **红测**: ① 消化后标记/界定正确；② 归档可逆且 archive 不进检索；③ 活跃未消化条目永不归档；④ cursor/幂等不破坏。
- **验收**: 构造超 cap T2 → 归档触发 → 活跃条目无损 + archive.md 含退役记录 + INDEX/检索不见退役条目。

### B2 subagent 背景完成唤醒父（量级 M，CC 对齐）

- **问题**: `run_in_background=True` 完成后 emit durable Signal + ledger 回写（结果不丢），但父 agent 不被唤醒，消费延迟到父下次 run（heartbeat 2h 兜底）。CC 语义 = 后台 agent 完成即重新唤起父（0.11）。
- **修法**: 与 `workflow_signal_consumer`（P11）同构的消费层：daemon 扫描未消费 `subagent_completed` Signal 且父当前无活跃 run → 对父触发一次**受治理**的 invoke（source 走 SessionContext 既有枚举扩展，原子消费防双唤醒，depth/预算治理防连锁唤醒风暴）。**实施前先核 Sentinel**（`agents/coordination.py` Sentinel 对 trigger-like open loop 的 Signal/Checkpoint 处理）是否已部分覆盖——若覆盖则只补缺口，不重复建设。
- **红测**: ① 后台子完成 → 父被唤醒一次且 Signal 原子消费；② 父正在 run 中 → 不重复唤醒（run 内 consume 路径不变）；③ 连锁场景（父 resume run 内再 spawn）受 depth 治理；④ 跨租户隔离。
- **验收**: 后台 spawn → 父空闲 → 子完成 → 父在 daemon 周期内被唤醒并消费结果，全链审计可见。

---

## §3 轨道 C — CC deferred-loading 形态对齐（先拍板后实施）

> 0.5 的正解。**路线已在 `docs/execution-mode-spectrum.md` §4.4（11 接线点盘点）/§4.5（迁移序列）/§8.3（切口表）定稿——本计划不复制施工细节，单一权威源是该文档**；此处只列拍板项与执行序。

| # | 项 | 内容 | 依赖 | 量级 |
|---|---|---|---|---|
| C0 | **拍板三待决**（§4.5） | ① 名字宣告载体细节（消息流增量的事件形态）② 发现集持久化位置 ③ subagent 是否带独立发现集 | 用户 | — |
| C1 | T3a 基建（纯加法） | 发现集状态 + tool_search 语义反转（名字常驻、schema 按需加载）+ 名字宣告；pack 解锁机制原样保留，功能只增不减 | C0 | M |
| C2 | T3b 切换（Breaking，可单独 revert） | skill 去解锁化 + pack 降目录——唯一解锁路 = 发现 | C1 | M |
| C3 | T4 前端联动 + 双轨清理 | 工具面板/MCP 文案、always_load 配置面、单一路径兑现 | C2 | M |

完成判据: `tool_search` 从"技能/能力目录与 pack 激活入口"变为 CC 形态的 deferred loader——工具名字常驻宣告、schema 按需取用、skill 回归纯知识载体。

---

## §4 轨道 D — 明确留账不进本轮（每项有理由）

| 项 | 不进理由 | 归属 |
|---|---|---|
| T-G3 catalog 16 项 planned（coordination×3 / plan×2 / budget×4 / IDE×5 / 小件×2） | 对齐增强非缺陷；tool_visibility×3 已被轨道 C 覆盖；budget×4 前置 = provider-normalized counters；IDE×5 前置 = IDE bridge substrate | `kernel/runtime_guidance_catalog.py` 冻结差集钉守 |
| 进化闭环 P3 eval | P0-P2 已带单测，eval 是实证锦上添花 | 挂账 #5 维持 |
| 组织层晋升入库 | 大件，需先拍 `docs/org-agent-asset-rights-model.md` §6 宪法六问 | 挂账 #6 维持 |
| 生产实证验收（五点清单） | 本计划完成后放真实流量 1-2 天另案执行；A1 修完面板才可信 | 挂账 #7 维持 |
| claude-mem borrow 提案 | 已废除并删除（2026-06-07，`02fb8322`）——检索侧动机由 P9 wikilink-KG+PPR 落地 | 已清账 |

---

## §5 执行序与里程碑

```
M1 真相层      A1 面板 exists≠fresh ────────── 一切后续验证的可信前提
M2 治理兑现    A2 MCP approval → A3 tenant_id → A4 孤儿清理
M3 限制收口    A5 escalate-retry → A6 suffix ── Max Token 线就此清账
M4 记忆补完    B1 T2 retention → B2 父唤醒 ──── Memory 线就此清账
M5 形态对齐    C0 拍板 → C1 T3a → C2 T3b → C3 T4 ─ Runtime 形态对齐 CC 就此清账
```

- 量级合计: 轨道 A+B 共 8 项（S×4、M×4）；轨道 C 共 3 切口（M×3）。
- 纪律: 每项独立可验收可 revert；一项一 commit；红测先行（RED→GREEN→REFACTOR）；全量测试绿才算项完成；新 agent 工具若有必须注册 `capability_gate.py CAPABILITY_MAP`（STRICT_CAPABILITY_MAPPING 坑）。
- C0 不阻塞 M1-M4，可并行拍板。

## §6 完成态 DoD（工程闭环判据）

1. **承诺兑现**: 配置面每个语义（MCP approval / deny / auto，Hindsight opt-in，subagent background）在执行路径有对应 enforcement 或兑现，红测钉死。
2. **真相可观测**: 面板状态 = mtime 实证状态；撞 cap 有 metric；无静默失败路径新增。
3. **CC 机制差距清零**: escalate-retry 落地后，限制/压缩机制层与 CC 无已知差距（数值层已于 06-05~07 收口）；deferred-loading 完成后 Runtime 形态无已知差距。
4. **金字塔生命周期完备**: T0/T2/T3/soul 四层全部有 retention 语义。
5. **全量绿**: 后端 + 前端 + tsc + build，0 failed（Docker 可用环境含真 PG 套件）。
6. 本文档 §1-§3 每项落地后回写证据块（commit + 测试数），全部 ✅ 后挂账清单同步更新、计划归档。

---

*修订记录: v0.1 2026-06-07 初稿（Claude × Codex 交叉 review 合成，§0 双向源码验证）。*
