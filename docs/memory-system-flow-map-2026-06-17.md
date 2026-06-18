# Hive 记忆系统流程图（2026-06-17）

> 用途：把 Hive 记忆系统从运行输入到 prompt recall 的完整链路放在一张图里，用来定位断点、重复职责和越权写入。
>
> Core boundary: LLM 负责判断、提炼、反思、归纳、候选生成；平台负责证据引用、权限、去重、回滚、审计、最终落盘。
>
> 当前目标：Agent Markdown Wiki / Learning Vault 是包含 T0、T2、T3、`soul.md`、skills/evolution/audit sidecars 的完整 Markdown 文件系统；T3 只是其中的语义收敛层。主梯度仍是 `T0 -> T2 -> T3 -> soul.md`。残差连接表示 T3 curation 先看 T2，再沿 T2 `source_refs` 回到目标 T0 evidence 做复核；它不是新记忆层，也不是 T0 -> T3 捷径。

```mermaid
flowchart TB
  %% ---------------------------------------------------------------------------
  %% Runtime producers and T0
  %% ---------------------------------------------------------------------------
  subgraph A["A. 运行输入与 T0 证据层"]
    U["User / channel / web chat"] --> INV["invoke_agent / AgentKernel"]
    TR["Trigger / scheduler / workflow / delegation"] --> INV
    SM["Explicit save_memory signal"] --> SRC_PACKET
    FB["Session feedback / owner correction"] --> SRC_PACKET["Source packet builder"]
    WL["Work Ledger findings / artifacts"] --> SRC_PACKET

    INV --> DB_CHAT["DB: ChatSession + ChatMessage"]
    INV --> SPANS["DB: invocation_spans / tool spans"]
    INV --> ACTIVITY["DB: AgentActivityLog"]
    INV --> SRC_PACKET

    SRC_PACKET --> T0_CORE["T0 session ledger: memory/t0/sessions/<session_id>/segments/<segment_id>/source.md"]
    INV --> T0_RUNTIME["T0 runtime events: chat/task/trigger/delegation/heartbeat/dream ledger events"]
  end

  %% ---------------------------------------------------------------------------
  %% T0 -> T2
  %% ---------------------------------------------------------------------------
  subgraph B["B. Extractor: T0 -> T2"]
    T0_CORE --> EXTRACTOR["Extractor LLM: summarize raw evidence into tagged summary blocks"]
    DB_CHAT --> EXTRACTOR
    SPANS --> EXTRACTOR
    WL --> EXTRACTOR

    EXTRACTOR --> T2_CANDIDATE["T2 summary block candidate: final summary, controlled tags, source_refs, residual check"]
    T2_CANDIDATE --> MGL_T2["Memory Governance Layer: privacy, permissions, dedupe, audit"]
    MGL_T2 --> T2_FILES["T2 tagged summary blocks: memory/t2/summary.md + compatibility views"]
    MGL_T2 --> T2_LIFECYCLE["memory/lifecycle.json sidecar"]
  end

  %% ---------------------------------------------------------------------------
  %% T2 -> T3, with residual evidence backreference
  %% ---------------------------------------------------------------------------
  subgraph C["C. Heartbeat Curator: T2 -> T3"]
    HB["Heartbeat tick"] --> SELECT_T2["Select mature T2 summary blocks"]
    T2_FILES --> SELECT_T2

    SELECT_T2 --> READ_REFS["Read T2 source_refs"]
    READ_REFS -. "residual evidence backreference" .-> TARGETED_T0["Targeted T0 evidence / runtime artifacts"]
    T3_WIKI --> READ_T3["Read current T3 canon / relations / contradictions / index"]
    SESSION_CTX["Short-lived session projection, optional context only"] --> HB_CURATOR

    SELECT_T2 --> HB_CURATOR["Heartbeat Curator LLM"]
    TARGETED_T0 --> HB_CURATOR
    READ_T3 --> HB_CURATOR

    HB_CURATOR --> T3_CANDIDATE["T3 semantic page candidate / patch"]
    T3_CANDIDATE --> LB_WIKI["Learning Brain / Wiki Intelligence: frontmatter, tags, page placement, dedupe, relations, index hints"]
    LB_WIKI --> MGL_T3["Memory Governance Layer: source refs, permissions, dedupe, rollback, audit, final write"]
    MGL_T3 --> T3_WIKI["T3 converged semantic layer: memory/t3/canon.md / relations.md / contradictions.md"]
    MGL_T3 --> COMPAT_T3["Compatibility T3 views: memory/wiki/** + feedback.md / knowledge.md / strategies.md / blocked.md / user.md"]
    MGL_T3 --> AUDIT["distillation_audit.jsonl / evolution ledger / activity log"]
  end

  %% ---------------------------------------------------------------------------
  %% T3 -> Soul and skill lane
  %% ---------------------------------------------------------------------------
  subgraph D["D. Dream And Skill Lane"]
    DREAM_TICK["Dream scheduled job"] --> DREAM_INPUT["Read stable T3 + soul + candidate/evolution ledger"]
    T3_WIKI --> DREAM_INPUT
    COMPAT_T3 --> DREAM_INPUT
    SOUL["soul.md"] --> DREAM_INPUT
    EV_LEDGER["evolution/evolution_ledger.jsonl"] --> DREAM_INPUT

    DREAM_INPUT --> DREAM_LLM["Dream Reconsolidator LLM"]
    DREAM_LLM --> DREAM_DECISION["DreamDecision: soul patch candidate, T3 lifecycle patch, contradictions, preservation flags"]
    DREAM_DECISION --> MGL_DREAM["Memory Governance Layer apply_dream_decision"]
    MGL_DREAM --> SOUL
    MGL_DREAM --> T3_WIKI
    MGL_DREAM --> ARCHIVE["memory/archive.md / lifecycle patches"]
    MGL_DREAM --> EV_LEDGER

    LB_WIKI --> SKILL_CAND["Skill candidate lane"]
    LB_WIKI -. "workflow evidence handoff only" .-> WORKFLOW_REF["Workflow system reference hint"]
    SKILL_CAND --> SKILL_EVAL["SkillDistiller + eval + promotion ledger"]
    SKILL_EVAL --> SKILL_SRC["skills/<name>/SKILL.md"]
    WORKFLOW_REF -. "outside memory target" .-> WORKFLOW_SYS["Workflow runtime / JSON-YAML-DSL definitions"]
  end

  %% ---------------------------------------------------------------------------
  %% Read side and prompt assembly
  %% ---------------------------------------------------------------------------
  subgraph E["E. Read / Activation / Prompt Assembly"]
    PRINCIPAL["PrincipalStack: owner / company / current user / creator"] --> ACTIVATION["ActivationContext"]
    T3_WIKI --> RETRIEVER["MemoryRetriever"]
    COMPAT_T3 --> RETRIEVER
    T2_FILES --> RETRIEVER
    SESSION_CTX --> RETRIEVER
    ACTIVATION --> RETRIEVER
    RETRIEVER --> SCORE["ActivationScorer: relevance, lifecycle, sensitivity, owner/company scope"]
    SCORE --> SNAPSHOT["Dynamic memory snapshot"]

    SOUL --> IDENTITY["Frozen identity section"]
    SNAPSHOT --> LONG_TERM["Active long-term memory: selected T3 snippets"]
    SNAPSHOT --> ACTIVE_T2["Active summary memory: selected T2 blocks"]
    SNAPSHOT --> WORKING["Session working memory: TTL projection"]
    SNAPSHOT --> NAV_MAP["Optional compact index.md navigation map"]
    SKILL_SRC --> SKILL_CONTEXT["Loaded skills only when relevant"]
    SKILL_CONTEXT --> LONG_TERM
    IDENTITY --> FINAL_PROMPT["Final prompt: frozen prefix + cache boundary + dynamic suffix"]
    LONG_TERM --> FINAL_PROMPT
    ACTIVE_T2 --> FINAL_PROMPT
    WORKING --> FINAL_PROMPT
    NAV_MAP --> FINAL_PROMPT
    FINAL_PROMPT --> INV
  end

  %% ---------------------------------------------------------------------------
  %% Guards
  %% ---------------------------------------------------------------------------
  T0_CORE -. "forbidden: direct T0 -> T3 write" .-> MGL_T3
  T0_SYSTEM -. "audit only by default; not semantic substrate" .-> AUDIT
  SESSION_CTX -. "context only; not durable truth" .-> FINAL_PROMPT
  LB_WIKI -. "suggestions only; no filesystem write" .-> MGL_T3
  AUDIT -. "audit/candidate only; not semantic memory body" .-> DREAM_INPUT
```

## 按流程读图

1. Runtime turn 先产生 source packet 和 T0 evidence。T0 是可回放证据，不是语义结论。
2. Extractor 是唯一正常的 T0 -> T2 路径，输出带 `source_refs` 的 T2 tagged summary blocks。
3. Heartbeat 是正常的 T2 -> T3 路径。它从 T2 summary blocks 起步，再沿 `source_refs` 回到目标 T0 evidence 做残差式复核。
4. Learning Brain 不替代三层蒸馏器。它只组织 Wiki candidate：页面归位、frontmatter、tags、aliases、relations、去重、矛盾和检索提示。
5. Memory Governance Layer 是 T2、T3、lifecycle、archive、soul patch 的唯一最终写入权威。
6. Dream 读取稳定 T3 和 `soul.md`，提出 identity 或 lifecycle 变更；它不直接写 durable files。
7. Skill 可以从 memory evidence 中长出候选并晋升为 `skills/<name>/SKILL.md`。Workflow 是独立执行控制体系；memory 只提供 evidence handoff / reference hint，不生成 workflow definition。
8. Prompt assembly 只经过 activation 和 visibility filter 读取，不创建新记忆状态；`soul.md` 常驻，`index.md` 只作为可选 compact navigation map，T0 raw 只在 residual/debug/replay 时按 refs 加载。

## 断点检查清单

- `RESPONSE_COMPLETE` / source packet 缺失：Extractor 不能可靠生成 T2；后续 replay 必须标记为 replay/backfill。
- Extractor cursor drift：T2 可能跳过 source slices；检查 T2 summary block 的 `source_refs`。
- T2 summary block 缺少 `source_refs`：Heartbeat 不能做残差式证据复核，必须 hold T3 candidate。
- Heartbeat 从 raw T0 起步而不是从 T2 起步：这是边界违规，因为 T0 不能绕过 T2 进入 T3。
- Learning Brain 写文件：这是边界违规。它只能提出 Wiki organization suggestion。
- T3 semantic layer 漂移：broken `[[wikilinks]]`、缺 source refs、low-confidence claims、contested pages 必须在 wiki lint 中暴露。
- 机械 fallback 失真：regex、counter、截断摘要不能直接写成语义记忆；预算不足时必须保留 source refs、range、hash、artifact path 和 held candidate。
- 验证强度错位：metadata/index patch 可以轻验证；T3 稳定结论需要 T2/T0 证据；soul/skill 晋升需要强验证和 gate；workflow promotion 不属于 memory lane。
- `evolution/lineage.md` 误用：它只能是 audit/counter/candidate evidence，不能成为 semantic memory body。
- T2 过早 absorbed：heartbeat 只能在 curation result 和 write decision 记录后标记 absorbed。
- `save_memory` 误用：它是显式高优先级用户记忆信号，默认进入 T2 summary block / Review Queue；不能绕过治理层直写 T3。
- Skill lane bleed：capability candidates 可以引用 T3 evidence，但最终 source of truth 不能存进 T3。
- Workflow lane bleed：workflow definition 不能进入 memory target；只能通过 Workflow 系统自己的 JSON/YAML/DSL 和治理链路管理。
- Dream candidate blindness：如果 `evolution_ledger.jsonl` 有 useful candidates 但 dream prompt 看不到，soul/T3 promotion 会停滞。
- Activation fail-closed：principal context 不能解析时，prompt memory 可以被抑制，不能泄漏。
- Prompt budget trims：dynamic memory 应优先保留证据指针；如果内容无法完整放入 prompt，应保留可回溯引用而不是写失真摘要。
