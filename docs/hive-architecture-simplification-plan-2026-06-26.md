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
| **P1** | **Legacy 双轨从不退役** | 记忆死 curator 子系统(`scene_curator`+`wiki_curator`+`memory_curation` 死 stub 永返 `disabled`+`distillation_audit`,~1500 行只能经死 stub 到达);extract-first 老管线(`extract_agent` 62KB+`t0_logger` 51KB+`t2_store`);no-op 空壳(`enhancement.py`/`backend.py`) | 主体,~111 legacy 文件来源 |
| **P2** | **迁移没收口、两世代并存** | `pack→plugin→extension`(一面三词)、`schedules→triggers`(`AgentSchedule` 影子表+`schedules.py` facade)、channel `per-agent vs per-tenant` 两代模型、`mcp_server vs installed_plugin` 两套安装原语、`agent_manager.py`(管 OpenClaw Docker 的名实不符孤儿) | 收口未完 |
| **P3** | **dispatch 碎片化** | plan_mode 6 handoff 文件=6 register 函数;8 个 per-IM-channel router 同形复制(`feishu.py` 单文件 2169 行);本地/桌面 agent 桥 3 套竞争机制 7 文件 50+ 端点 | 一概念拆 N 文件 |
| **P4** | **一职责多权威源(最危险)** | schema 三套:`create_all`×2 + entrypoint 41 条 `ALTER IF NOT EXISTS` + alembic,同列三处定义、alembic 失败 `\|\| echo WARNING` 静默吞 | 单项最大架构债 |
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

---

## 5. 逐项工作清单

### 5.1 RETIRE — 纯减法(T0,可立即做,零行为变更)

| 项 | 文件 | 证据 |
|---|---|---|
| 死 curator 子系统 | `memory/scene_curator.py` `wiki_curator.py` `services/memory_curation.py`(死 stub)`memory/distillation_audit.py`(主调用方已死)+ 清 `evolution_daemon.py:175-183` 死调用 | 唯一 live 入口 `run_scene_wiki_curation_tick` 永返 `{status:disabled}`;~1500 行 |
| 零调用孤儿模块 | `memory/understanding_store.py`(record/contradict 已 raise)`memory/promotion_router.py`(spec 设计从未接线)`services/extract_queue_replay.py` `services/heartbeat_reflection_backfill.py` | grep 跨 app/ 排除 test = 零命中 |
| 死守卫/死字段 | `retriever.py` 的 `_retrieve_high_priority_t2`/`_retrieve_understandings`(体已 `return []`)+ `include_legacy/derived_sources` 参数;`tools/handlers/memory.py::save_memory` 的 `candidate_type`(零读取);`llm_utils.py::ANTHROPIC_API_PROVIDERS`(死常量) | 函数体空/字段零读 |
| 死别名 | `resource_discovery.py::search_smithery`;`runtime_guidance_catalog.py` 两个 self-alias re-export;摘 `decision_trace.py` 三个 JSONL backfill 方法(保留 store 主体) | 零 import |
| no-op 空壳 | `memory/enhancement.py`(函数体 no-op)`memory/backend.py`(单实现协议) | docstring 自白 |
| 前端孤儿 | `frontend/.../pages/Dashboard.tsx` + `Dashboard.test.tsx`(855+ 行) | 0 路由、仅被自身测试钉住 |
| API 死孤儿 | `app/api/packs.py` | `main.py` 从不 import,`capabilities.py` 是 live 副本(逐字相同) |

### 5.2 收口 FINISH — 两世代统一(T3,需 backfill+影子验证)

| 收口 | 老世代 → 新世代(唯一) | 已有迁移资产 | 关键风险点 |
|---|---|---|---|
| 扩展面 | `pack`/`extension` → **`plugin`** 一个词 | `installed_plugin` 已"supersedes MCPServer install primitive";plugin 迁移在途 | 存量 pack 配置/租户安装行 |
| 安装原语 | `mcp_server` → `installed_plugin` 统一 | 同上 | 存量 MCP server 行 |
| 调度 | `AgentSchedule`+`schedules.py` facade → **`AgentTrigger`** | `migrate_schedules_to_triggers.py` 已就绪 | 存量 AgentSchedule 行(影子表) |
| Channel 配置 | `channel_config`(per-agent) → `tenant_channel_config`(per-tenant)收一代 | Phase 6 已建新模型 | 存量 per-agent channel 配置 |
| OpenClaw 孤儿 | 核实 `agent_manager.py`(Docker/Gateway)是否仍 wire,若否则退役 | — | 名实不符,需先确认无 live 依赖 |

### 5.3 CONSOLIDATE — 收敛碎片化(T1,行为不变,测试守门)

| 收敛 | 现状 → 目标 | 行为风险 |
|---|---|---|
| plan_mode handoff | 6 文件 6 register → 1 个 `plan_mode_handoffs.py`(registry 已是唯一聚合点) | 零(同样注册) |
| IM channel router | 8 个同形 router → 1 通用 `/channels/{provider}` + provider adapter(`channel_rls`/`channel_secrets` 已是共享地基) | 端点 parity 核对 |
| 本地/桌面桥 | `gateway`+`local_bridge`(自身双份)+`local_agent_channel`+`desktop×4` 3 套 → 1 套 local-agent 连接子系统 | 端点 parity 核对(最复杂,放后) |
| 小卫星合并 | `workspace_sync_dirty`→`workspace_sync`;`long_task_validation`+`runtime_reconciliation`→`runtime_task_service`;`conversation_summarizer`→`memory_service`(唯一 caller);记忆 `lifecycle_store`+`lifecycle_maintenance`+`access_log`→`lifecycle.py` | 零(纯合并) |

### 5.4 SUNSET — 给过渡期双跑挂日落(T2)

`t0_logger` startup backfill、`extract_agent` admin backfill、`legacy_migration`、`auto_dream` 迁移、`mcp_backfill_service`、filesystem 老工具 facade、Gemini openai-fallback、plan_mode `long_task` 兜底——**逐个挂"日落条件 + 删除 ticket"**:确认生产已迁移/已无走老路 → gate off 观察 → 删。不是现在删,是给每条退役路径一个明确终点,而不是永生。

### 5.5 RE-FILE — 归位(T1)

`prompt_eval.py`+`task_eval.py` → `app/evals/`;CLAUDE.md 文件计数改为生成;services 重打包(§7)。

---

## 6. Schema 单一权威方案(你已确认要做 — T4,最高风险,freeze→verify→shrink)

现状(每次启动三套都跑):`main.py:207` `create_all` + `apply_rls` + 内联 `ALTER TYPE`;`entrypoint.sh` **再次** `create_all` + **41 条** `ALTER TABLE IF NOT EXISTS` + `alembic upgrade head`(`|| echo WARNING` 非阻断)。

**绝不骤删 ALTER 块**(它现在真在兜底漂移,`entrypoint.sh:144` 自白"早期 create_all 漏 updated_at → sso 500")。安全 5 步:

1. **Freeze(纪律,零风险)**:立铁律"**新列只走 alembic migration**";entrypoint 41 条 ALTER 加注释"存量冻结、禁止新增"。从今天起债不再长大。
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

---

## 9. CC Hook 现状(post-Codex,2026-06-26 工作树)

Codex 这轮把 **`cc_hook_contract.py` 删除、CC wire standard 折叠进 `hooks.py`**(`hooks.py:531/645-671` 现直接 parse `exit_code`/`hookSpecificOutput`,"Hook wire standard")——**这正是规则 #1「一个标准、不拆独立兼容层」的范例**,与本方案完全一致。

残留:`hook_runner.py`(外部命令 `GovernedHookRunner`)仍**零生产调用**。所以"退役 vs 接通"的决策**收窄到只剩这一个 runner 文件**:CC 的"执行外部命令 hook"对标不在 Hive 范围 → 删 `hook_runner.py`;若将来要支持外部命令 hook → 保留为显式 fenced-deferred。建议:**默认删**(YAGNI,Hive 当前只跑进程内 Python handler),将来真要再加。

---

## 10. 建议执行顺序(每段独立、可停可回滚)

| 阶段 | 内容 | Tier | 何时 |
|---|---|---|---|
| **A. 纯减法** | §5.1 RETIRE 全部 + §9 删 hook_runner + §5.5 RE-FILE 的删 Dashboard/归位 eval | T0/T1 | 可立即,零风险一把清 |
| **B. 收敛碎片** | §5.3 CONSOLIDATE(plan_mode handoff、小卫星合并先做;channel/本地桥放后) | T1 | A 后 |
| **C. Schema freeze** | §6 第 1-3 步(立纪律 + 核对 + 去重 create_all) | T4 前段 | 与 A/B 并行,纪律即生效 |
| **D. 收口迁移** | §5.2 FINISH(schedules→triggers 先,迁移脚本已就绪;pack→plugin/channel/mcp 逐个 backfill+影子验证) | T3 | 逐个,每个独立主线 |
| **E. 重打包**(可选) | §7 services → domain 包 | T1 | 看你 §决策 |
| **F. Schema un-swallow** | §6 第 4-5 步(需生产 alembic 实证) | T4 后段 | 最后,最谨慎 |

**先动 A(纯减法)立竿见影减负且零线上风险;C 第 1 步(freeze 纪律)今天就能立,从此债不再长大。D/F 是碰生产的部分,逐个走 expand→verify→contract,不 big-bang。**

---

## 附:本方案不做什么

- 不动 8.5 分的执行核心(不 Rust 重写、不重写洋葱)。
- 不删 T0/T2/T3/soul 分层、ccplus_contracts live 族、audit 三源对账等有意结构。
- 不 big-bang 任何碰生产数据/startup 的改动——一律 expand→migrate→verify→contract。
