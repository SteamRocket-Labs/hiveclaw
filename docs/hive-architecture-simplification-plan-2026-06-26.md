# Hive 系统架构精简方案(Occam 收敛 + 生产安全收口)

Date: 2026-06-26
Status: architecture diagnosis + consolidation plan(讨论稿,实施需主理人逐项拍板)
Method: 6 个并行只读勘察 agent 全量测绘(services 207 / memory 30+自进化 / kernel-runtime-tools 核心 84 / api 75+models 53+core 12 / 前端 / 兼容-死面全局普查)+ 主理人复核

---

## 0. 一句话结论

系统**不是结构性混乱**——执行核心(kernel/runtime/tools)是全仓最干净的一层(DI 洋葱 8.5/10),前端、记忆 canonical 路径也清晰。你体感的"臃肿"根不在"代码多",而在**每个职责都背着不止一条并行路径**:canonical+legacy 双轨、新世代+老世代、3 套 schema 权威、N 个 dispatch 文件。

> **真正能"删"的死代码 < 1% LOC;重量是认知负担——111 个 legacy 文件不是 111 份垃圾,是 111 处"还有另一条路"。**

根因:**"一次改完零债"这条法律,只兑现在每次迁移的"加"那一半,"退役前任"那一半从没做。** 修法 = 完整的一次改完必须包含删掉前任。

**对你最关心的"收口会不会影响线上":会,如果 big-bang 做;不会,如果按生产风险分层、expand→migrate→verify→contract 逐步做。** 详见 §4——这是本方案的核心。

---

## 1. 诊断:清晰吗?守奥卡姆吗?

### 1.1 分类清晰度——比体感好

| 层 | 清晰度 | 证据 |
|---|---|---|
| 执行核心 kernel/runtime/tools | **8.5/10(最干净)** | `invoke_agent → AgentKernel.handle → ToolRuntimeService.execute → governance → handler` 是真正受治理的 DI 管线;历史担心的"tool 三源漂移""hook 双层"已显式文档化 + startup 断言 + 测试钉死 |
| 前端 src/ | **良好偏上** | api 层 33 domain 统一走 `request<T>`、教科书级干净;surface→page→section 分层清晰;chat 视图已收敛 |
| 记忆 memory/ | **canonical 一句话清** | `ledger → segment_package → consolidation+platform_gate → md_store/retriever → auto_dream/soul` |
| services/ | **13 个清晰簇** | LLM/记忆/自进化/Skill/Workflow/Plan/Trigger/Channel/MCP/治理/协作/DR/身份,docstring 质量普遍高 |

### 1.2 奥卡姆违规——是"机制多",不是"代码多"

几乎每个职责都有 >1 条并行做法。奥卡姆该砍的是**机制(并行路径数)**,不是**能力(产品功能)**——CC 对标的能力面、Hive 治理/记忆面都该留。最小化的是"干同一件事的代码条数 / 权威源数量",不是产品能力。

---

## 2. 臃肿的 6 个模式

| # | 模式 | 代表证据 | 性质 |
|---|---|---|---|
| **P1** | **Legacy 双轨从不退役** | 记忆死 curator(`scene_curator`+`wiki_curator` 真孤儿;`memory_curation` 是 evolution_daemon 接线但永返 `disabled` 的死 stub。注:`distillation_audit` **不在此列,它 LIVE**);extract-first 老管线(`extract_agent` 62KB+`t0_logger` 51KB+`t2_store`);no-op 空壳(`enhancement.py`/`backend.py`) | 主体,~111 legacy 文件来源 |
| **P2** | **迁移没收口、两世代并存** | `pack→plugin→extension`(一面三词)、`schedules→triggers`(`AgentSchedule` 影子表+`schedules.py` facade)、channel `per-agent vs per-tenant` 两代模型、`mcp_server vs installed_plugin` 两套安装原语、`agent_manager.py`(docstring 仍写 OpenClaw Docker 但**名实不符+职责混杂,非孤儿**——hr/advanced/agents/seeder 在依赖,见 §5.2,改名拆责任不退役) | 收口未完 |
| **P3** | **dispatch 碎片化** | plan_mode 6 handoff 文件=6 register 函数;8 个 per-IM-channel router 同形复制(`feishu.py` 单文件 2169 行);本地/桌面 agent 桥 3 套竞争机制 7 文件 50+ 端点 | 一概念拆 N 文件 |
| **P4** | **一职责多权威源(最危险)** | schema 三套:`main.py:207` create_all + `entrypoint.sh:68` 第二次 create_all + 42 条 `ALTER TABLE` / 39 条 `ADD COLUMN IF NOT EXISTS` + `entrypoint.sh:164` alembic(失败 `\|\| echo WARNING` 静默吞),同列三处定义 | 单项最大架构债(主理人确认) |
| **P5** | **过渡期双跑(个体合理、集体沉重)** | startup 一次性迁移(`t0_logger` backfill/`legacy_migration`/`auto_dream` 迁移/`mcp_backfill`)+ 运行时 fallback(filesystem facade/Gemini openai-fallback/plan_mode long_task 兜底) | 留着对,但需日落纪律 |
| **P6** | **错放 + 地图过期** | `prompt_eval`+`task_eval`(1400 行 eval 工具)错放 `runtime/` 核心目录;前端 `Dashboard.tsx`(855 行)纯孤儿;CLAUDE.md 计数全过期(services 写 163 实际 207) | 杂项 |

---

## 3. 目标架构

### 3.1 洋葱已经在了,核心别动

```
Contracts(纯 dataclass)
  → Kernel(engine,零 DB,纯 DI loop)
    → Invoker / Runtime(唯一收口 + 上下文装配)
      → Tools(registry @tool · governance · handlers)
        → Services(按 domain 分包)
          → API(一资源一 router)
            → Models(单一 schema 权威 = alembic)
+ Hive-native delta(记忆 T0/T2/T3/soul · 治理 gate · RLS · 自进化)
```

再次印证:**别用 Rust 重写一个 8.5 分的核心**(见 `docs/ccplus-breakpoint-audit-and-architecture-decision-2026-06-26.md` Part A)。

### 3.2 该保的(别误删)

T0/T2/T3/soul 四层分层、`ccplus_contracts` session/permission/context 族(重度 live)、`tools/audit.py` 三源对账断言、`mcp_backfill` core/shell 拆分、`deep_research/` 16-stage 管线、feishu 子域拆分、core/ 12 文件——**都是有意且正确的,不在精简目标内**。本仓库有"活兜底误判成死代码"前科,以上均已复核为 live 或显式 fenced-deferred。

**当前工作树新增的 Session UX / permission / artifact canonical 面也要保留(2026-06-26 复核)**:`docs/ccplus-session-ux-contract-2026-06-26.md` 是 session 用户体验契约;`services/chat_artifact_delivery.py` 是当前单一路径的 session artifact 交付 helper;`ChatArtifact` + `artifact_delivery` transcript event + 前端 artifact card / inspector 是活的交付物路径;`ChatSession.transcript_metadata_json` / `RuntimeTask.metadata_json` / `runtime_session_context.metadata` 的 permission profile 三层同步是当前运行面,不能在清理时当成重复字段随手删。它们需要另走 §5.4 SUNSET/收口纪律,不是 §5.1 死代码删除。

### 3.3 services/ 扁平 207 → 13 个 domain 包(= "重打包",见 §7)

---

## 4. ⭐ 会不会影响线上?——生产安全收口策略

**核心原则:绝不 big-bang。按生产风险分 5 个 Tier,每条收口走 `expand → migrate → verify → contract`,每步可回滚。** 你问"该怎么办"——答案就是这张分层表:

| Tier | 类型 | 生产风险 | 安全做法 | 可逆性 |
|---|---|---|---|---|
| **T0** | 纯死代码删除(零调用已证) | **零** | 删 + 跑全量回归。没人调用 = 删了线上无感 | git revert |
| **T1** | 代码组织重构(行为不变) | **低** | 全量回归 + 端点/注册 parity 核对。只改 import 路径/文件位置,行为零变 | git revert |
| **T2** | Legacy 双轨退役(老路仍有 live caller) | **中** | ① 确认生产数据已迁移(查幂等 marker/数据态)② 老路 gate 在 flag 后 default-off,观察窗口确认无人走 ③ 窗口后删 | flag 翻回 |
| **T3** | 两世代收口(存量生产数据在老模型) | **中高** | RLS-迁移式守卫:① backfill 老→新 ② 过渡期双读(新优先、老 fallback)③ cutover 写只走新 ④ **影子验证**生产新世代覆盖全部老数据 ⑤ 删老模型+facade | 每步独立,cutover 前全可逆 |
| **T4** | Schema 单一权威(碰生产 startup) | **最高** | freeze→verify→shrink,**绝不骤删**(见 §6) | 每步小且可逆 |

**为什么这样就不会炸线上**:T0/T1 本来就不碰运行时数据/契约;T2/T3 的危险点是"老路径还在被调用 / 老数据还在老模型里",所以**先确认/迁移/验证,最后才删**——任何一步发现覆盖不全,停在那步回滚,绝不会出现"删了才发现线上还在用"。T4 的危险点是 ALTER 补丁现在是**真在兜底**漂移,所以**先证明 alembic 在生产能干净跑,再拆兜底网**(否则会把"静默带病启动"变成"硬崩")。

> 一句话给你:**线上不会受影响的前提是"删除永远是最后一步,且前面每一步都在生产实证过覆盖"。** 这正是你们做 RLS 迁移时定的纪律,复用它即可。

### 4.1 当前工作树先决条件(Phase 0)

2026-06-26 当前工作树又新增了一条 Session UX / permission / artifact 主线(约 1500+ 行改动,集中在 `web_chat_runtime.py`、`chat_sessions.py`、`tools/governance.py`、`chat_artifact_delivery.py`、`chatRuntime.ts`、`AgentChatSection.tsx`)。这条主线不推翻本精简计划,但它必须先稳定,否则 A 阶段删除会和 session runtime 改动混在一起,难以判断失败来源。

**新的执行前提**:先完成 Phase 0(当前 session 改动的目标测试 + contract 核对),再动 §5.1 的 13 项删除。Phase 0 的验收重点:

- Session permission profile 在 `ChatSession.transcript_metadata_json`、`RuntimeTask.metadata_json`、`runtime_session_context.metadata` 三层一致,取消/disconnect/replay 后不漂移。
- 用户可见 permission mode 只暴露 `default` / `auto` / `bypassPermissions`;`acceptEdits`、`dontAsk`、`plan` 及旧别名只作 persisted compatibility,不进入新 UI。
- session artifact 统一走 `chat_artifact_delivery.py` 产生 `ChatArtifact` 或 row-free `artifact_delivery` parts;前端 replay 后仍能展示 artifact card / inspector。
- destructive delete 即使在 `bypassPermissions` 下也只能 allow once,不能 allow session。
- 当前主线绿后,再进入 §5.1 删除,避免把"新 session 行为失败"误判成"删除死代码失败"。

---

## 5. 逐项工作清单

### 5.1 RETIRE — 候选,逐项核实后退役(**不是"零风险一把清"**)

> **⚠️ 修订(2026-06-26,主理人+Codex 抽查后):此前把本表当"T0 纯减法零风险一把清"是错的。** Codex 复核坐实 census grep 漏判了 5 项(动态 import / 独立 writer / public tool schema):`distillation_audit`(auto_dream+evolution_daemon 在写)、`search_smithery`(web_mcp 在调)、`agent_manager`(hr/agents/seeder 在依赖)、`retriever` 派生参数(还 gate wiki_pages)、`container_candidate`(public schema)。**结论:下表是候选,每项删除前必须过"穷举 caller 闸"(见下),census 单次 grep 不够。**

**穷举 caller 闸(删任一项前逐项过)**:① 静态 import + **函数内动态 import**(`from x import y` 藏在函数体里);② tool 参数 schema 暴露面(影响旧 tool call);③ transcript / T0 replay 引用;④ admin/startup/daemon 接线;⑤ 测试外的任何读/写点。**五项全空才可删。**

**穷举核实结果(2026-06-26,只读,逐项过闸)** — grep 覆盖 `app/**/*.py` 静态+函数内动态 import+字符串引用(本批候选均非 registry/computed-string 动态加载,grep 充分);带测试的删除连其测试一起删:

| # | 候选 | 闸结论 | caller 证据 |
|---|---|---|---|
| 1 | `services/extract_queue_replay.py` | ✅ 过闸 | app 零引用;3 测试文件 |
| 2 | `services/heartbeat_reflection_backfill.py` | ✅ 过闸 | app 零引用;1 测试 |
| 3 | `memory/understanding_store.py` | ✅ 过闸 | app 零引用(含 `query()`);3 测试 |
| 4 | `memory/promotion_router.py` | ✅ 过闸 | app 仅 2 处**注释**提及(`types.py:57`/`extract_agent.py:133`),无 import/call;2 测试 |
| 5 | `memory/scene_curator.py` + `wiki_curator.py` | ✅ 过闸 | app 零引用;**census"经 memory_curation 可达"已证伪**——`memory_curation.py` docstring 自承 curators 仅 importable-for-tests,实际不 import 它们 |
| 6 | `retriever.py::_retrieve_high_priority_t2` + `_retrieve_understandings` | ✅ 过闸(局部) | 零外部 caller,仅 `:251/:253` 内部;**只删 2 方法体+2 内部调用,留** `include_derived_sources`+`_retrieve_wiki_pages`(`:258` live) |
| 7 | `llm_utils.py::ANTHROPIC_API_PROVIDERS` | ✅ 过闸 | app 零、测试零 |
| 8 | `runtime_guidance_catalog.py` 两 alias(`RuntimeGuidanceCatalogEntry`/`ATTACHMENT_ALIGNMENT_CATALOG`) | ✅ 过闸 | app 零;**留底层真名** `CC_NATIVE_ATTACHMENT_CATALOG`(live) |
| 9 | `decision_trace.py` 的 JSONL→SQL backfill/import 方法(`backfill_decision_trace_jsonl_to_sql`/`import_decision`/`import_feedback`) | ✅ 过闸(仅这几个方法) | 三者 app 零调用 + 专用测试。**严禁误删 live 的 JSONL store**:`DecisionTraceStore.persistent_default()`/`_load()`/`_append()`(`:60/:58/:73`)被 `session_feedback.py:143` 默认在用,必须保留 |
| 10 | `api/packs.py` | ✅ 过闸 | `main.py` 零引用;live 副本在 `capabilities.py:156/167` |
| 11 | 前端 `pages/Dashboard.tsx` + 测试 | ✅ 过闸 | `App.tsx:128/148` `/dashboard`→`Navigate`/`ControlPlane`,无组件 import;`guards.tsx` 的 `Navigate to="/dashboard"` 仅命中重定向 |
| 12 | `runtime/hook_runner.py`(`GovernedHookRunner`,=§9) | ✅ 过闸 | app 零(Codex 折叠 `cc_hook_contract` 后剩的孤儿);2 测试 |
| 13 | `services/memory_curation.py`(disabled stub) | ⚠️ 过闸但**连带改** | `evolution_daemon.py:174-182` 是调用它的 `try/except` 整块(动态 import + `run_scene_wiki_curation_tick` 返回 disabled + `report["scene_wiki_curation"]` + 异常日志)→ 删 stub **必须连删整个 try block**,否则留半截死 try |

**确认未过闸、保留(LIVE,本次复核坐实)**:`distillation_audit.py`(`evolution_daemon:196`+`auto_dream:417/1170` 在写)、`search_smithery`(`web_mcp:2096` 在调)、`agent_manager.py`(hr/advanced/agents/seeder 在依赖)→ 全留;`container_candidate`(`memory.py:77` public schema)→ §5.4 SUNSET;`enhancement.py`/`backend.py`(`main.py:540`/`memory.py:371`/`evolution_daemon:221` 在调,no-op 但被接线)→ §5.3 改 3 处调用点的小重构,非删。

**净结果:13 项过闸可删**(#13 需连删 `evolution_daemon` 整个 try/except 块、#6 是局部删)、**6 项确认保留**;删除合计净减约 2000+ 行,**每项带 caller 证据,可作为删除执行单**。

**删除验收补充(2026-06-26 当前工作树)**:A 阶段不只是"删代码 + 跑测试"。凡删除本表项目,必须同步移除/改写仍把旧路径当当前实现的 truth docs 和保护旧实现的 tests。否则会重新制造"地图对不上地形"。尤其要核 `extract_queue_replay`、`heartbeat_reflection_backfill`、`understanding_store`、`PromotionRouter`、`scene_curator`/`wiki_curator`、`Dashboard.tsx`、`hook_runner` 在 docs 下的当前 truth 引用;历史/archive 允许保留,当前 truth doc 必须改成"已退役"或移除执行入口。

**执行前核验命令(删任何东西前必跑)**:

```bash
# 1) 复核 decision_trace 边界:只删 backfill/import 方法,persistent_default/_load/_append 必须仍 live
rg -n "backfill_decision_trace_jsonl_to_sql|import_decision|import_feedback|DecisionTraceStore\.persistent_default|persistent_default\(" backend/app backend/tests
# 2) 复核当前 truth docs 不再把已删路径写成 live 入口
rg -n "extract_queue_replay|heartbeat_reflection_backfill|understanding_store|PromotionRouter|scene_curator|wiki_curator|Dashboard\.tsx|hook_runner" docs --glob '!docs/archive/**'
# 3) 后端全量回归(删后必须仍绿)
cd backend && source .venv/bin/activate && pytest tests -q
# 4) 前端构建(删 Dashboard.tsx 后必须仍过)
cd ../frontend && npm run build
```

### 5.2 收口 FINISH — 两世代统一(T3,需 backfill+影子验证)

| 收口 | 老世代 → 新世代(唯一) | 已有迁移资产 | 关键风险点 |
|---|---|---|---|
| 扩展面 | `pack`/`extension` → **`plugin`** 一个词 | `installed_plugin` 已"supersedes MCPServer install primitive";plugin 迁移在途 | 存量 pack 配置/租户安装行 |
| 安装原语 | `mcp_server` → `installed_plugin` 统一 | 同上 | 存量 MCP server 行 |
| 调度 | `AgentSchedule`+`schedules.py` facade → **`AgentTrigger`** | `migrate_schedules_to_triggers.py` 已就绪 | 存量 AgentSchedule 行(影子表) |
| Channel 配置 | `channel_config`(per-agent) → `tenant_channel_config`(per-tenant)收一代 | Phase 6 已建新模型 | 存量 per-agent channel 配置 |
| ~~OpenClaw 孤儿~~ | ❌**误判,非孤儿**:`agent_manager.py` 被 hr.py/advanced.py/agents.py/skill_seeder.py 大量依赖(`agents.py:329/336` 调 `initialize_agent_files`/`start_container`)。问题是**职责混杂 + docstring 命名过期(仍写 OpenClaw/Docker)**,不是可退役孤儿 → **改名 + 拆责任 + 更新 docstring**(归 §5.3 CONSOLIDATE/重命名,**不删**) | — | 零(改名+更注释) |

### 5.3 CONSOLIDATE — 收敛碎片化(T1,行为不变,测试守门)

| 收敛 | 现状 → 目标 | 行为风险 |
|---|---|---|
| plan_mode handoff | 6 文件 6 register → 1 个 `plan_mode_handoffs.py`(registry 已是唯一聚合点) | 零(同样注册) |
| IM channel router | 8 个同形 router → 1 通用 `/channels/{provider}` + provider adapter(`channel_rls`/`channel_secrets` 已是共享地基) | 端点 parity 核对 |
| 本地/桌面桥 | `gateway`+`local_bridge`(自身双份)+`local_agent_channel`+`desktop×4` 3 套 → 1 套 local-agent 连接子系统 | 端点 parity 核对(最复杂,放后) |
| 小卫星合并 | `workspace_sync_dirty`→`workspace_sync`;`long_task_validation`+`runtime_reconciliation`→`runtime_task_service`;`conversation_summarizer`→`memory_service`(唯一 caller);记忆 `lifecycle_store`+`lifecycle_maintenance`+`access_log`→`lifecycle.py` | 零(纯合并) |

### 5.4 SUNSET — 给过渡期双跑挂日落(T2)

`t0_logger` startup backfill、`extract_agent` admin backfill、`legacy_migration`、`auto_dream` 迁移、`mcp_backfill_service`、filesystem 老工具 facade、Gemini openai-fallback、plan_mode `long_task` 兜底——**逐个挂"日落条件 + 删除 ticket"**:确认生产已迁移/已无走老路 → gate off 观察 → 删。不是现在删,是给每条退役路径一个明确终点,而不是永生。

新增两条当前 session 主线的日落纪律:

- **Permission metadata mirror**:`ChatSession.transcript_metadata_json` / `RuntimeTask.metadata_json` / `runtime_session_context.metadata` 目前是运行时即时切换 permission profile 的三层同步面,短期保留;但它必须有一致性测试,并在后续 Session Workbench read model 稳定后收敛到一个 canonical read surface。删除或合并前置条件:active run、replay、disconnect/cancel、permission update、resume 五类路径都证明读同一份规范化 `permission_profile`。
- **Permission mode legacy alias**:`acceptEdits`、`dontAsk`、`plan`、`accept_edits`、`dont_ask_low_risk`、`auto_review`、`break_glass`、`full_access`、`bypass_permissions` 只用于 persisted metadata 兼容和 CC baseline internal semantics;用户菜单 canonical 只有 `default` / `auto` / `bypassPermissions`。日落条件:生产 metadata census 证明旧值为 0,或提供一次性 migration 把旧值规范化到三值集合;在此之前禁止把旧 alias 当新产品入口继续扩散。

### 5.5 RE-FILE — 归位(T1)

`prompt_eval.py`+`task_eval.py` → `app/evals/`;CLAUDE.md 文件计数改为生成;services 重打包(§7)。

---

## 6. Schema 单一权威方案(你已确认要做 — T4,最高风险,freeze→verify→shrink)

现状(每次启动三套都跑):`main.py:207` `create_all` + `apply_rls` + 内联 `ALTER TYPE`;`entrypoint.sh:68` **再次** `create_all` + **42 条 `ALTER TABLE` / 39 条 `ADD COLUMN IF NOT EXISTS`** 手补丁 + `entrypoint.sh:164` `alembic upgrade head`(`|| echo WARNING` 非阻断)。

**绝不骤删 ALTER 块**(它现在真在兜底漂移,`entrypoint.sh:144` 自白"早期 create_all 漏 updated_at → sso 500")。安全 5 步:

1. **Freeze(纪律,零风险)**:立铁律"**新列只走 alembic migration**";entrypoint 那 42+39 条 ALTER/ADD 加注释"存量冻结、禁止新增"。从今天起债不再长大。
2. **Verify(只读核对)**:逐条确认每个 ALTER 列都有对应 alembic migration(api 勘察已发现绝大部分有)+ 生产 schema = alembic head。
3. **Shrink create_all**:移除重复的第二次 `create_all`(lifespan 与 entrypoint 二选一保留)。
4. **Un-swallow(需先影子验证)**:把 `alembic upgrade head || echo WARNING` 的静默吞改为 fail-loud/告警——**但必须先在生产确认 alembic 能干净跑通**,否则从"静默带病启动"变"硬崩"。这一步本身要影子验证。
5. **Remove-ALTER**:第 2 步确认每列 alembic 覆盖 + 生产对齐后,逐步移除冻结的 ALTER 块。

> 净效果:从"3 套真相源互相兜底、失败被吞"收敛到"alembic 单一权威、失败可见"。**第 1 步立刻可做且零风险(纯纪律),后面每步都可独立验证 + 回滚。**

---

## 7. "重打包"是什么(你说没理解 — 这里讲清)

**现状**:`app/services/` 是 **207 个文件平铺在一个文件夹里**,没有子目录。要找"workflow 相关"得在 207 个文件名里靠前缀认(`workflow_runtime_service.py`/`workflow_daemon.py`/`workflow_launch.py`…)。

**重打包 = 把这 207 个平铺文件,按职责装进子文件夹**(就像 `memory/` `deep_research/` 已经做的那样):

```
现在(平铺):                          重打包后(按 domain 分包):
app/services/                         app/services/
  workflow_runtime_service.py           workflow/
  workflow_daemon.py                      runtime_service.py
  workflow_launch.py                      daemon.py
  llm_client.py                           launch.py
  llm_reasoning.py                      llm/
  heartbeat.py                            client.py
  auto_dream.py                           reasoning.py
  skill_distiller.py                    evolution/
  ... (再 200 个平铺) ...                  heartbeat.py
                                          auto_dream.py
                                        skill/
                                          distiller.py
                                        channels/ governance/ collaboration/ ...
```

**它改的只是文件位置(import 路径),行为零变化**(T1 低风险,纯 git mv + 改 import,测试守门)。**收益**:打开 `services/` 一眼看到 13 个 domain 文件夹而不是 207 个文件名;新代码"该放哪"有明确归属,不会再随手平铺加文件。

**代价**:churn 大(动很多 import 语句)。所以它是"想要清晰分类就值,只想减负可以先不做"——这是 §10 里你要选的。

---

## 8. 防"再臃肿"规则(建议写进 CLAUDE.md)

1. **一职责一条 canonical 路径**;legacy 路径出生即带"删除期限/ticket",不许"永久 compat"。
2. **"一次改完"包含退役前任**——迁移验收 = 新路接通 ∧ 旧路删除。(修订现有法律。)
3. **一职责一个权威源**(schema=alembic;工具 schema=`@tool`),startup 断言一致(推广 `tools/audit.py` 模式)。
4. **没有 live 消费者不准种契约**;确需 deferred 必须显式 fence + 标期限(本轮 cc_hook/side-effect channel 已转此形)。
5. **services 按 domain 包组织**,新文件进对应包,禁平铺。
6. **地图跟代码同步**(CLAUDE.md 计数生成化)。
7. **删除前过"穷举 caller 闸"**(静态 import + **函数内动态 import** + tool 参数 schema + transcript/T0 replay + admin/startup/daemon),**单次 grep 不算证据**——"活兜底误判成死"是本仓库反复踩的坑(2026-06-26 §5.1 又被抽查逮到 5 处误判:动态 import 的 `search_smithery`、独立 writer 的 `distillation_audit`、public schema 的 `container_candidate`、heavily-wired 的 `agent_manager`、还 gate live 路径的 retriever 参数)。
8. **新 canonical 面必须先登记再扩张**:新增 session artifact、permission profile、metadata mirror、UI read model 这类跨后端/前端/T0 的面时,必须在本计划或对应 truth doc 里明确"canonical surface / compatibility surface / sunset condition";否则它们会成为下一轮永久兼容双轨。

---

## 9. CC Hook 现状(post-Codex,2026-06-26 工作树)

Codex 这轮把 **`cc_hook_contract.py` 删除、CC wire standard 折叠进 `hooks.py`**(`hooks.py:531/645-671` 现直接 parse `exit_code`/`hookSpecificOutput`,"Hook wire standard")——**这正是规则 #1「一个标准、不拆独立兼容层」的范例**,与本方案完全一致。

残留:`hook_runner.py`(外部命令 `GovernedHookRunner`)仍**零生产调用**。所以"退役 vs 接通"的决策**收窄到只剩这一个 runner 文件**:CC 的"执行外部命令 hook"对标不在 Hive 范围 → 删 `hook_runner.py`;若将来要支持外部命令 hook → 保留为显式 fenced-deferred。建议:**默认删**(YAGNI,Hive 当前只跑进程内 Python handler),将来真要再加。

---

## 10. 建议执行顺序(每段独立、可停可回滚)

| 阶段 | 内容 | Tier | 何时 |
|---|---|---|---|
| **0. Session 主线稳定** | 当前工作树的 Session UX / permission profile / artifact delivery 改动先跑目标测试并核对契约:permission 三层 metadata 不漂移、artifact replay 可见、destructive delete one-shot、UI 只暴露三种 permission mode | T1/T2 | **A 之前** |
| **A. 逐项核实退役**(非"一把清") | §5.1 候选**逐项过"穷举 caller 闸"**后才删 + §9 删 hook_runner(也需先核实零 caller)+ §5.5 删 Dashboard/归位 eval | T0/T1 | 先做穷举审计,只删全过闸的项;Codex 已证 census grep 不够 |
| **B. 收敛碎片** | §5.3 CONSOLIDATE(plan_mode handoff、小卫星合并先做;channel/本地桥放后) | T1 | A 后 |
| **C. Schema freeze** | §6 第 1-3 步(立纪律 + 核对 + 去重 create_all) | T4 前段 | 与 A/B 并行,纪律即生效 |
| **D. 收口迁移** | §5.2 FINISH(schedules→triggers 先,迁移脚本已就绪;pack→plugin/channel/mcp 逐个 backfill+影子验证) | T3 | 逐个,每个独立主线 |
| **E. 重打包**(可选) | §7 services → domain 包 | T1 | 看你 §决策 |
| **F. Schema un-swallow** | §6 第 4-5 步(需生产 alembic 实证) | T4 后段 | 最后,最谨慎 |

**先收口 Phase 0 当前 session 主线,再动 A 中已过闸的 13 项(逐项已带 caller 证据,见 §5.1),低风险但删后仍需全量回归兜底,非"零风险一把清";C 第 1 步(freeze 纪律)今天就能立,从此债不再长大。D/F 是碰生产的部分,逐个走 expand→verify→contract,不 big-bang。**

---

## 附:本方案不做什么

- 不动 8.5 分的执行核心(不 Rust 重写、不重写洋葱)。
- 不删 T0/T2/T3/soul 分层、ccplus_contracts live 族、audit 三源对账等有意结构。
- 不 big-bang 任何碰生产数据/startup 的改动——一律 expand→migrate→verify→contract。
