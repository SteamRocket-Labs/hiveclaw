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
    SM["Explicit save_memory signal"] --> EXPLICIT_OVERLAY["Explicit Memory Overlay: immediate activation candidate"]
    SM -. "source evidence" .-> SRC_PACKET
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
  subgraph B["B. T0 -> T2 Segment Package"]
    T0_CORE --> BUNDLE
    DB_CHAT --> BUNDLE["Source Bundle Builder: memory/.staging/t2_jobs/<job_id>/source_bundle.json"]
    SPANS --> BUNDLE
    WL --> BUNDLE

    BUNDLE --> SUMMARY["Summary Agent: summary.md candidate"]
    BUNDLE --> LABELS["Learning Brain: labels.md candidate"]
    SUMMARY --> REVIEW_T2["Memory Gate Agent: review.md"]
    LABELS --> REVIEW_T2
    REVIEW_T2 --> PLATFORM_T2["Platform Gate: hard check, source_refs, permissions, audit, atomic commit"]
    PLATFORM_T2 --> T2_FILES["T2 Segment Package: memory/sessions/<session_id>/segments/<segment_id>/{summary,labels,review,manifest}"]
    PLATFORM_T2 --> T2_LIFECYCLE["memory/lifecycle.json sidecar"]
  end

  %% ---------------------------------------------------------------------------
  %% T2 -> T3, with residual evidence backreference
  %% ---------------------------------------------------------------------------
  subgraph C["C. Heartbeat Curator: T2 -> T3"]
    HB["Heartbeat tick"] --> SELECT_T2["Select reviewed/closed Segment Packages"]
    T2_FILES --> SELECT_T2

    SELECT_T2 --> READ_REFS["Read T2 source_refs"]
    READ_REFS -. "residual evidence backreference" .-> TARGETED_T0["Targeted T0 evidence / runtime artifacts"]
    T3_WIKI --> READ_T3["Read current accepted T3 files: episodes/user/worker/capabilities"]
    SESSION_CTX["Short-lived session projection, optional context only"] --> HB_CURATOR

    SELECT_T2 --> HB_CURATOR["Heartbeat Curator LLM"]
    TARGETED_T0 --> HB_CURATOR
    READ_T3 --> HB_CURATOR

    HB_CURATOR --> T3_PITCH["consolidation_pitch.md"]
    T3_PITCH --> T3_PATCH["revised_patch.md: LLM-authored T3 Patch Envelope"]
    T3_PATCH --> MGL_T3["Memory Gate Agent: final review latest revised_patch.md"]
    MGL_T3 --> PLATFORM_T3["Platform Gate: hard check, source refs, permissions, dedupe, rollback, audit, atomic commit"]
    PLATFORM_T3 --> T3_WIKI["T3 accepted semantic layer: memory/t3/{episodes,user,worker,capabilities}.md"]
    PLATFORM_T3 --> COMPAT_T3["Derived/read-only compatibility views and runtime/db read models"]
    PLATFORM_T3 --> AUDIT["distillation_audit.jsonl / evolution ledger / activity log"]
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

    DREAM_INPUT --> DREAM_LLM["Dream / Soul Writer Agent"]
    DREAM_LLM --> SOUL_PITCH["soul_pitch.md"]
    SOUL_PITCH --> SOUL_PATCH["soul_patch.md candidate"]
    SOUL_PATCH --> SOUL_GATE["Soul Memory Gate Agent: fresh review latest soul patch"]
    SOUL_GATE --> SOUL_PLATFORM["Platform Soul Gate: frozen-section check, source refs, rollback, audit, atomic commit"]
    SOUL_PLATFORM --> SOUL
    SOUL_GATE -. "held T3 concern only" .-> T3_REVIEW_QUEUE["Next T3 Consolidation Batch"]
    SOUL_PLATFORM --> EV_LEDGER

    T3_WIKI --> SKILL_SEED["Capability / skill_seed blocks"]
    SKILL_SEED --> SKILL_CAND["Skill candidate package"]
    T3_WIKI -. "workflow evidence handoff only" .-> WORKFLOW_REF["Workflow system reference hint"]
    SKILL_CAND --> SKILL_EVAL["Skill Distiller / Skill Review / eval + promotion ledger"]
    SKILL_EVAL --> SKILL_GATE["Platform Skill Gate"]
    SKILL_GATE --> SKILL_SRC["skills/<name>/SKILL.md"]
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
    EXPLICIT_OVERLAY --> RETRIEVER
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
  LABELS -. "candidate only; no direct filesystem write" .-> PLATFORM_T2
  EXPLICIT_OVERLAY -. "T3 absorption requires consolidation, review, and platform gate" .-> MGL_T3
  AUDIT -. "audit/candidate only; not semantic memory body" .-> DREAM_INPUT
```

## 按流程读图

1. Runtime turn 先产生 source packet 和 T0 evidence。T0 是可回放证据，不是语义结论。
2. Source Bundle Builder 只组装 T2 job 输入包；Summary Agent、Learning Brain 和 Memory Gate Agent 分别生成 `summary.md`、`labels.md`、`review.md`，Platform Gate 原子提交 Segment Package。
3. Heartbeat / T3 Curator 是正常的 T2 -> T3 路径。它从 reviewed/closed Segment Packages 起步，再沿 `source_refs` 回到目标 T0 evidence 做残差式复核。
4. Learning Brain 不替代三层蒸馏器。它在 T2 阶段负责 `labels.md`，T3 阶段只可由 Curator 在 Patch Envelope 内给出 target-view labels；不能拥有独立写入权。
5. Memory Gate Agent 是 LLM 裁判；Platform Gate 是 T2、T3、lifecycle、archive、soul patch 的最终原子提交权威，但不是内容作者。
6. Dream / Soul Writer 读取稳定 T3 和 `soul.md`，提出 `soul_pitch.md` / `soul_patch.md`；它不直接写 durable files，也不能借 soul 路径直接改 T3。
7. Skill 可以从 `capabilities.md` 的 evidence / skill_seed 中长出候选，经 eval、review、Platform Skill Gate 晋升为 `skills/<name>/SKILL.md`。Workflow 是独立执行控制体系；memory 只提供 evidence handoff / reference hint，不生成 workflow definition。
8. Prompt assembly 只经过 activation 和 visibility filter 读取，不创建新记忆状态；`soul.md` 常驻，`index.md` 只作为可选 compact navigation map，T0 raw 只在 residual/debug/replay 时按 refs 加载。

## 断点检查清单

- `RESPONSE_COMPLETE` / source packet 缺失：Source Bundle Builder 不能可靠生成 T2 job；后续 replay 必须标记为 replay/backfill。
- Source bundle / segment cursor drift：T2 可能跳过 source slices；检查 Segment Package 的 `manifest.json` 和正文 `source_refs`。
- Segment Package 缺少 `source_refs`：Heartbeat 不能做残差式证据复核，必须 hold T3 candidate。
- Heartbeat 从 raw T0 起步而不是从 T2 起步：这是边界违规，因为 T0 不能绕过 T2 进入 T3。
- Learning Brain 写文件：这是边界违规。它只能产出 `labels.md` candidate 或 T3 Patch Envelope 内的 target-view labels，不能直接落盘。
- T3 semantic layer 漂移：broken `[[wikilinks]]`、缺 source refs、low-confidence claims、contested pages 必须在 wiki lint 中暴露。
- 机械 fallback 失真：regex、counter、截断摘要不能直接写成语义记忆；预算不足时必须保留 source refs、range、hash、artifact path 和 held candidate。
- 验证强度错位：metadata/index patch 可以轻验证；T3 稳定结论需要 T2/T0 证据；soul/skill 晋升需要强验证和 gate；workflow promotion 不属于 memory lane。
- `evolution/lineage.md` 误用：它只能是 audit/counter/candidate evidence，不能成为 semantic memory body。
- T2 过早 absorbed：heartbeat 只能在 curation result 和 write decision 记录后标记 absorbed。
- `save_memory` 误用：它是显式高优先级用户记忆信号，默认进入 explicit memory overlay 并即时可激活；后续是否吸收到 T3 必须走 T3 Consolidator + Memory Gate Agent + Platform Gate，不能绕过治理层直写 T3。
- Skill lane bleed：capability candidates 可以引用 T3 evidence，但最终 source of truth 不能存进 T3。
- Workflow lane bleed：workflow definition 不能进入 memory target；只能通过 Workflow 系统自己的 JSON/YAML/DSL 和治理链路管理。
- Dream candidate blindness：如果 `evolution_ledger.jsonl` 有 useful candidates 但 dream prompt 看不到，soul/T3 promotion 会停滞。
- Activation fail-closed：principal context 不能解析时，prompt memory 可以被抑制，不能泄漏。
- Prompt budget trims：dynamic memory 应优先保留证据指针；如果内容无法完整放入 prompt，应保留可回溯引用而不是写失真摘要。
