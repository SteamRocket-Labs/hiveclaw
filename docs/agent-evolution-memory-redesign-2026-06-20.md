# Agent Evolution 与新记忆系统重构方案

> 状态：核心路径已实装并回归验证。本文档同时保留设计边界与 2026-06-20 本轮实现证据。
> 日期：2026-06-20
> 范围：Agent Detail 的 `Evolution / 进化` 页面、memory/self-evolution 读模型、Skill 生态管理与微调进化、Dream/Heartbeat 写入边界、legacy evolution sidecar、测试与迁移脚本。

## 1. 问题定义

修复前，Agent Detail 的 `Evolution` 页面仍然是旧 self-evolution 投影。它主要读取 agent workspace 里的三个文件：

```text
evolution/skill_usage.json
evolution/skill_review.md
evolution/evolution_ledger.jsonl
```

这在旧语义下还能解释为“技能生命周期 + heartbeat lineage + promotion audit”。但现在 Hive 的记忆系统已经重构为 Markdown-first Learning Vault，主梯度是：

```text
T0 append-only session ledger
  -> T2 Segment Package
  -> T3 accepted semantic layer
  -> soul.md
```

所以本轮要修掉的问题是：

1. 页面名字叫“进化”，但它没有展示新记忆系统里 agent 真正的学习轨迹。
2. 页面漏掉了 T0 / T2 / T3 / lifecycle / soul governance 这些当前主路径。
3. 页面过度依赖旧 `evolution/*` 文件，让用户误以为这些文件仍然是语义进化 truth。
4. 修复前 Dream 和 Heartbeat 代码里仍残留对 `evolution/*` sidecar 的读写，导致边界不清；本轮已把 Dream soul lane 与 Heartbeat 外围维护迁出旧路径。
5. Skill 当前被混称为“进化”，但代码事实更接近 Skill 资产管理：catalog、usage、stale/archive、pin、candidate、audit。
6. Skill 的真正微调进化标准不清楚：重复任务模式被误放大为进化依据，但它最多只能作为“新 Skill 候选”的弱信号，不能作为“已有 Skill 微调”的主标准。
7. 修复前 Skill catalog 渐进披露还不是 usage-aware / scenario-aware 动态排序；`use_count` 和 `last_used_at` 主要用于 curator 和页面，不决定运行时可见优先级。

这次改造必须一次性闭环，不能做 MVP，不能只补 UI，不能留“以后再接新记忆系统”的尾巴。

## 2. 当前代码事实

### 2.1 当前页面读什么

当前前端调用链：

```text
frontend/src/pages/agent-detail/AgentEvolutionSection.tsx
  -> frontend/src/api/domains/evolution.ts
  -> GET /agents/{agent_id}/evolution
```

当前后端调用链：

```text
backend/app/api/agents.py::get_agent_evolution()
  -> backend/app/services/agent_evolution_view.py::build_agent_evolution_view()
```

当前 `build_agent_evolution_view()` 是 v2 lane-based projection，读取：

```text
memory/.staging/t3_jobs/*/manifest.json
memory/t3/*.md
memory/.staging/soul_candidates/*/manifest.json
evolution/skill_registry.json
evolution/skill_usage.json
evolution/skill_candidates/*/manifest.json
legacy evolution/scorecard.md、blocklist.md、lineage.md detection only
```

`evolution/evolution_ledger.jsonl` 仍可作为 Skill/eval/capability candidate audit 的兼容面，但不再作为 memory/soul lane 的 source。

### 2.2 当前 Dream 还在做什么

`backend/app/services/auto_dream.py` 的顶部已经声明 Dream 工作在 canonical markdown layers 上：

```text
T2 Segment Packages
accepted T3 memory
soul.md
```

当前实现边界：

```text
不读取 evolution/evolution_ledger.jsonl 作为 soul/memory candidate evidence
不调用 record_memory_promotion_candidate()
不调用 record_memory_promotion_decision()
不维护 evolution/blocklist.md
把 soul candidate package 放在 memory/.staging/soul_candidates/<candidate_id>/
把 soul rollback 放在 memory/.rollback/soul/
把 committed/held soul candidate audit 写入 memory/distillation_audit.jsonl
```

### 2.3 当前 Heartbeat 还在做什么

`backend/app/services/heartbeat.py` 的当前边界：

```text
自主反思执行、活动日志、HEARTBEAT_TICK_END hook
```

Heartbeat 不再拥有这些外围进化任务：

```text
run_skill_distillation_cycle
run_skill_curator_pass
run_scene_wiki_curation_tick
record_dream_activity / should_dream / run_dream
validate_and_normalize_t3
sync_t3_to_memory_enhancement
_update_evolution_files / _auto_seed_evolution / _validate_bootstrap_completion
```

这些外围维护由 `HEARTBEAT_TICK_END` 后的 `evolution_maintenance_on_heartbeat` hook 调用
`backend/app/services/evolution_daemon.py::run_heartbeat_evolution_maintenance()` 执行。

### 2.4 当前新记忆 truth surface

当前新记忆系统的主 truth surface 是：

```text
T0:
  memory/t0/sessions/<session_id>/segments/<segment_id>/source.md

T2:
  memory/t2/sessions/<session_id>/segments/<segment_id>/
    summary.md
    labels.md
    review.md
    manifest.json

T3 job:
  memory/.staging/t3_jobs/<job_id>/
    source_bundle.json
    t3_neighborhood.md
    consolidation_pitch.md
    review.md
    revised_patch.md
    manifest.json

T3 accepted truth:
  memory/t3/episodes.md
  memory/t3/user.md
  memory/t3/worker.md
  memory/t3/capabilities.md

Lifecycle / audit:
  memory/control/lifecycle.json
  memory/distillation_audit.jsonl
```

## 3. 目标架构

`Evolution` 页面保留，但语义改成：

```text
这位 agent 如何通过记忆、soul、技能逐步变强。
```

后端需要从旧的单一 `evolution/*` 投影，变成 lane-based projection：

```text
Memory lane:
  T0 evidence -> T2 package -> T3 job -> accepted T3 block -> lifecycle status

Soul lane:
  accepted T3 evidence -> soul candidate package -> Soul Memory Gate review
  -> Platform Soul Gate decision -> soul.md commit 或 hold

Skill ecosystem lane:
  catalog visibility -> dynamic ranking -> hot/default/cold/archived state
  -> pin / scenario boost / usage heat / explicit load recovery

Skill tuning lane:
  loaded Skill behavior -> deviation signal -> common-vs-episodic judgment
  -> LLM-authored patch candidate -> Referee review -> Platform Skill Gate
  -> exact commit / hold / rollback

Legacy audit lane:
  旧 scorecard/lineage/blocklist，仅作为归档兼容信息
```

最终写入边界必须收敛为：

```text
T0 truth:
  memory/t0/sessions/<session_id>/segments/<segment_id>/source.md

T2 package:
  memory/t2/sessions/<session_id>/segments/<segment_id>/

T3 job:
  memory/.staging/t3_jobs/<job_id>/

T3 accepted truth:
  memory/t3/episodes.md
  memory/t3/user.md
  memory/t3/worker.md
  memory/t3/capabilities.md

Memory lifecycle:
  memory/control/lifecycle.json
  memory/distillation_audit.jsonl

Soul candidate:
  memory/.staging/soul_candidates/<candidate_id>/
  memory/.rollback/soul/<candidate_id>.soul.md.before
  soul.md

Skill/capability lane:
  evolution/skill_registry.json
  evolution/skill_usage.json
  evolution/skill_usage.jsonl
  evolution/skill_review.md
  evolution/evolution_ledger.jsonl
  evolution/skill_candidates/*
  evolution/rollback/skills/<candidate_id>/*
```

Skill 在本设计里必须拆成两条线：

```text
Skill Ecosystem Manager:
  负责“哪些 Skill 在什么场景下优先被看见、被加载、被冷藏、被恢复”。
  这不是语义进化，而是运行时能力资产的排序和预算管理。

Skill Tuning Loop:
  只处理“已固化 Skill 被真实调用后发生稳定偏差，需要改 SKILL.md 方法正文”的情况。
  这才叫 Skill 微调进化。
```

## 4. 不可违反的边界

1. Dream 不再把 memory/soul 事件写入 `evolution/evolution_ledger.jsonl`。
2. Dream 不再写或维护 `evolution/blocklist.md`。
3. Dream 可以读取 accepted T3 和 `soul.md`，可以提出 soul candidate，但必须交给 Soul Memory Gate 和 Platform Soul Gate。
4. Heartbeat 不再把 `evolution/scorecard.md`、`evolution/lineage.md`、`evolution/blocklist.md` 当当前语义状态写回。
5. Heartbeat / T3 Curator 可以 stage T3 job，可以把 runtime event 写入 T0。
6. Evolution 页面不能把 legacy `scorecard.md`、`lineage.md`、`blocklist.md` 当作当前 learning truth。
7. Skill ecosystem 和 Skill tuning 可以继续使用 `evolution/*`，但页面必须明确区分“生态管理”和“微调进化”。
8. 系统内置标准 Skill 只参与排序和 telemetry，不参与 agent self-evolution patch/promote。
9. 自进化 Skill 只有两类默认入口：T3 自动创建、用户通过 Skill Creator 主动创建。
10. 平台代码只负责 refs、rollback、路径、权限、原子提交，不能机械生成语义记忆内容。
11. T0 不能直接进入 T3。T3 必须从 T2 package 或 explicit overlay 起步，再沿 source refs 回看 T0。
12. Workflow definition 不属于 memory evolution。Memory 可以引用 workflow evidence，但不能晋升 workflow definition。
13. 重复任务模式 / T3 capability evidence 可以作为新 Skill Pitch 的主要原材料，但不能单独证明已有 Skill 需要 patch；已有 Skill 微调必须额外有该 Skill 被真实 `load_skill` 后的稳定偏差信号。
14. `candidate_signal.md`、usage counter、失败次数、regex 分类都只能是证据，不能直接变成 active Skill 内容。
15. Active `skills/<slug>/SKILL.md` 的语义正文必须由 Agent / Skill Writer LLM 写成完整草稿；Platform Skill Gate 只能 exact commit 通过验证的草稿。
16. 任何涉及 Skill Creator 的路径，最终落笔者必须是 Agent / Skill Writer LLM。平台不得模板拼接、规则生成、机械合成 `SKILL.md` 语义正文。
17. 平台可以提供 Skill Creator scaffold、source refs、候选包目录、校验、权限、回滚和 exact commit，但不能替 Agent 写 Skill。
18. Skill 微调必须先判断偏差是共性问题还是偶发行为；偶发一次性行为只能留在 session/T0/T2 evidence，不进入 Skill patch 序列。
19. Skill 生态排序是 read-model / prompt visibility 问题，不得伪装成语义进化。

## 5. 新 API 契约

保留当前路径，避免前端路由和调用面大迁移：

```text
GET /agents/{agent_id}/evolution
```

返回 v2 schema：

```json
{
  "schema": "agent_evolution_view.v2",
  "summary": {
    "t0_segments": 0,
    "t2_packages": 0,
    "t3_jobs": 0,
    "t3_active_blocks": 0,
    "soul_candidates": 0,
    "skill_assets": 0,
    "hot_skills": 0,
    "cold_skills": 0,
    "skill_tuning_candidates": 0,
    "held": 0,
    "committed": 0,
    "legacy_audit_files": 0
  },
  "lanes": {
    "memory": [],
    "soul": [],
    "skill_ecosystem": [],
    "skill_tuning": [],
    "legacy_audit": []
  },
  "timeline": [],
  "skill_summary": {
    "active": 0,
    "stale": 0,
    "archived": 0,
    "total": 0,
    "system_builtin": 0,
    "t3_auto_created": 0,
    "user_skill_creator": 0,
    "evolvable": 0
  },
  "skills": []
}
```

每个 event 使用统一形状：

```json
{
  "id": "string",
  "at": "ISO-8601 or empty",
  "lane": "memory|soul|skill_ecosystem|skill_tuning|legacy_audit",
  "stage": "t0|t2|t3|soul|skill_catalog|skill_usage|skill_patch|legacy",
  "kind": "evidence|package|review|commit|hold|rebase|promotion|rollback|ranking|cold_storage|deviation|legacy_audit",
  "status": "active|hot|default|cold|staged|reviewed|committed|held|rebase_required|archived|unknown",
  "title": "string",
  "detail": "string",
  "refs": ["string"],
  "paths": ["string"],
  "metadata": {}
}
```

Skill 事件和兼容 `skills[]` 必须在 metadata 中暴露：

```json
{
  "skill_origin": "system_builtin|t3_auto_created|user_skill_creator|manual_import",
  "evolvable": true,
  "active_version_hash": "sha256:...",
  "last_candidate_id": "skill-cand-..."
}
```

`skill_summary` 和 `skills` 暂时保留一个部署周期，作为兼容字段。但前端主渲染必须使用 `lanes` 和 `timeline`。`skills` 里的排序必须来自 Skill Ecosystem Manager，而不是旧注册顺序。

## 6. 后端一次性实现计划

### 6.1 新增统一读模型

新增：

```text
backend/app/services/agent_evolution_view.py
```

职责：

```text
只读，不产生任何写入
容忍缺文件和坏文件
输出稳定 JSON
全局 timeline 按时间倒序
每个 lane 内排序稳定
绝不解析 T0 raw body 成语义事实
```

建议函数：

```python
build_agent_evolution_view(workspace: Path) -> dict[str, Any]
collect_t0_events(workspace: Path) -> list[EvolutionEvent]
collect_t2_package_events(workspace: Path) -> list[EvolutionEvent]
collect_t3_job_events(workspace: Path) -> list[EvolutionEvent]
collect_t3_accepted_block_events(workspace: Path) -> list[EvolutionEvent]
collect_memory_lifecycle_events(workspace: Path) -> list[EvolutionEvent]
collect_soul_candidate_events(workspace: Path) -> list[EvolutionEvent]
collect_skill_ecosystem_events(workspace: Path) -> tuple[list[EvolutionEvent], skill_summary, skills]
collect_skill_tuning_events(workspace: Path) -> list[EvolutionEvent]
collect_legacy_audit_events(workspace: Path) -> list[EvolutionEvent]
```

数据源映射：

| Source | Lane | Event 语义 |
|---|---|---|
| `memory/t0/sessions/*/segments/*/index.json` | memory | T0 evidence segment 存在 |
| `memory/t0/sessions/*/segments/*/source.md` | memory | 只暴露 evidence path，不解析语义 |
| `memory/t2/sessions/*/segments/*/manifest.json` | memory | T2 package status / source refs |
| `memory/t2/sessions/*/segments/*/review.md` | memory | Memory Gate review 高层状态 |
| `memory/.staging/t3_jobs/*/manifest.json` | memory | T3 job staged/held/rebase/committed |
| `memory/t3/*.md` | memory | accepted block count / block ids |
| `memory/control/lifecycle.json` | memory | sketch/active/superseded/archived/discarded |
| `memory/distillation_audit.jsonl` | memory | held/rejected audit decisions |
| `memory/.staging/soul_candidates/*/manifest.json` | soul | soul candidate package status |
| `soul.md` | soul | 当前 committed identity 文件存在 |
| `evolution/skill_usage.json` | skill_ecosystem | usage heat、pin、state、cold/archive 状态 |
| `evolution/skill_usage.jsonl` | skill_tuning | load_skill 后的成功/失败/noop runtime evidence |
| `evolution/skill_registry.json` | skill_ecosystem | skill origin / evolvable / active version registry |
| `evolution/skill_review.md` | skill_ecosystem / skill_tuning | curator state change 或 tuning audit |
| `evolution/evolution_ledger.jsonl` | skill_tuning | skill/eval/capability candidate ledger |
| `evolution/skill_candidates/*/manifest.json` | skill_tuning | inactive Skill Candidate Package / patch package |
| `evolution/rollback/skills/*` | skill_tuning | promoted/patched Skill rollback snapshot |
| `evolution/scorecard.md`、`lineage.md`、`blocklist.md` | legacy_audit | legacy audit only |

`evolution/evolution_ledger.jsonl` 过滤规则：

```text
包含：
  skill
  skill_candidate
  skill_patch
  eval_run
  harness/canary/eval promotion records

不再作为 memory/soul source：
  memory_promotion_candidate
  memory_promotion_decision
```

如果老 workspace 里存在旧 memory/soul ledger records，只显示到 `legacy_audit`，状态为 `archived`。

### 6.2 替换 API 投影

修改：

```text
backend/app/api/agents.py
```

把当前：

```python
from app.services.evolution_view import build_evolution_view
return build_evolution_view(workspace)
```

替换为：

```python
from app.services.agent_evolution_view import build_agent_evolution_view
return build_agent_evolution_view(workspace)
```

`check_agent_access()` 不变。

### 6.3 保留旧 `evolution_view.py` 作为 skill digest

暂时不要直接删除：

```text
backend/app/services/evolution_view.py
```

原因：`runtime/invoker.py` 当前还用 `render_skill_evolution_digest()` 给 agent 注入 skill assets / stale skills digest。

本次安全做法：

```text
用户页面/API 使用新的 agent_evolution_view.py
runtime skill digest 暂时继续使用旧文件里的 render_skill_evolution_digest()
把旧文件注释和 docstring 改成 skill evolution view，避免再误认作 memory evolution
```

### 6.4 Skill 自进化参与边界与来源

这一段必须先讲清楚，否则 Skill evolution 会被误解成“所有 Skill 都可以被 agent 自动 patch”。

参与边界：

```text
系统内置标准 Skill 不参与自进化链。

系统内置标准 Skill 可以：
  1. 进入 catalog 排序
  2. 记录 usage / success / failure / noop telemetry
  3. 被冷藏降权，减少 prompt visibility
  4. 通过平台 release / repo change 更新

系统内置标准 Skill 不可以：
  1. 被 agent-authored Skill Writer 自动 patch
  2. 被 Skill Curator 自动 archive
  3. 进入 evolution/skill_candidates/* 作为 patch/promote target
  4. 用用户会话 evidence 直接覆盖 bundled SKILL.md

系统内置 Skill 如果暴露缺陷：
  记录为 platform skill issue / maintenance evidence，
  由平台工程或明确授权的 release 流程修复，
  不走 agent self-evolution exact commit。
```

只有自进化 Skill 进入 evolution chain。当前自进化 Skill 来源有两类：

```text
1. T3 自动创建
   source_type = t3_auto_created
   原材料来自 memory/t3/capabilities.md、skill_seed、T0/T2 source_refs、重复任务模式。
   生成路径：
     T3 capability evidence
       -> Skill Pitch
       -> Skill Writer 生成 Skill Candidate Package
       -> Skill Referee / Platform Skill Gate
       -> active skills/<slug>/SKILL.md
       -> 后续 tuning / rollback 链路

2. 用户主动创建
   source_type = user_skill_creator
   原材料来自用户在对话框里明确调用 Skill Creator 的创建意图、样例、约束、期望输出、依赖工具。
   生成路径：
     user request / Skill Creator interview
       -> Skill Writer / Skill Creator 生成 Skill Candidate Package
       -> Skill Referee / Platform Skill Gate
       -> active skills/<slug>/SKILL.md
       -> 后续 tuning / rollback 链路
```

这两类来源只影响创建入口，不影响后续微调标准：

```text
t3_auto_created 和 user_skill_creator 一旦成为 active evolvable skill，
后续 patch 都必须统一遵守：

1. 真实 load_skill evidence
2. common-vs-episodic 判定
3. Skill Writer 完整 SKILL.md.draft
4. Skill Referee 独立复核
5. Platform Skill Gate
6. rollback snapshot
7. behavior regression / eval evidence
```

Skill registry 必须显式记录来源和自进化资格：

```json
{
  "skills/<slug>/SKILL.md": {
    "skill_origin": "system_builtin|t3_auto_created|user_skill_creator|manual_import",
    "evolvable": true,
    "created_from": {
      "source_refs": ["t3:memory/t3/capabilities.md#...", "session:...", "runtime_task:..."],
      "candidate_id": "skill-cand-...",
      "created_by": "agent|user|platform"
    },
    "active_version_hash": "sha256:...",
    "last_candidate_id": "skill-cand-...",
    "state": "hot|default|cold|archived"
  }
}
```

默认规则：

```text
system_builtin:
  evolvable=false

t3_auto_created:
  evolvable=true unless tenant policy disables autonomous skill evolution

user_skill_creator:
  evolvable=true unless user marks it locked / pinned-no-evolve

manual_import:
  evolvable=false by default; must be explicitly opted in
```

### 6.5 Skill Ecosystem Manager：动态排序与冷藏

新增或修改：

```text
backend/app/services/skill_catalog_ranker.py
backend/app/services/agent_context.py
backend/app/skills/registry.py
backend/app/services/skill_curator.py
backend/tests/services/test_skill_catalog_ranker.py
backend/tests/services/test_skill_catalog_dynamic_ordering.py
backend/tests/services/test_skill_evolution_source_boundary.py
```

目标：

```text
Skill catalog 不再只是 registry 注册顺序。
运行时看到的 Skill 顺序由 usage heat、scenario match、pin、state、recent success、declared tool relevance 共同决定。
冷 Skill 默认不占高优先级 prompt 预算，但仍可被显式 load_skill 或场景匹配恢复。
```

排名输入：

```text
1. skills/<slug>/SKILL.md frontmatter:
   name / description / tools / packs / allowed-tools / requires_skills

2. evolution/skill_usage.json:
   use_count / view_count / last_used_at / state / pinned / created_by / archived_at

3. evolution/skill_registry.json:
   skill_origin / evolvable / active_version_hash / last_candidate_id

4. 当前任务上下文:
   latest_user_query / source / channel / active tool groups / task profile

5. Skill runtime evidence:
   recent success/failure/noop summary from evolution/skill_usage.jsonl
```

排序公式必须是可解释 read model，不是隐藏 magic：

```text
score(skill, context) =
  semantic_match_to_current_task
  + recent_use_heat
  + pinned_boost
  + scenario_tool_relevance
  + recent_success_rate_boost
  - stale_penalty
  - recent_noop_or_mismatch_penalty
```

状态语义：

```text
hot:
  高频、近期、场景匹配，默认进入 catalog 前段。

default:
  普通可见 Skill，仍按 score 排序。

cold:
  低频或长期未用，默认进入 catalog 后段或折叠摘要。
  用户显式点名、query 强匹配、trigger/agent scenario 强匹配时可恢复为可见。

archived:
  不进入默认 catalog。
  只能通过显式路径、管理 UI 或恢复动作重新启用。
```

冷藏不是删除：

```text
冷藏只影响 prompt visibility 和排序。
只有 Skill Curator 的 archive 才移动目录到 skills/.archive/<slug>。
```

实现约束：

```text
1. `load_skill` 成功后继续 bump use_count / last_used_at。
2. `build_skill_catalog_section_for_agent()` 必须走 ranker，不能直接 render registry insertion order。
3. Catalog budget 不够时，保留 hot + strong scenario match，再降级 default，最后只给 cold 摘要。
4. `pin_skill` 只影响排序和自动归档豁免，不代表 Skill 一定被加载。
5. 系统/平台内置 Skill 可以参与排序和 telemetry，但不得被 agent-authored curator 自动 archive 或 patch。
6. Usage heat 是选择面管理，不得写成“Skill 语义进化”。
```

### 6.6 Skill Tuning Loop：偏差纠偏与可回滚 patch

新增或修改：

```text
backend/app/services/skill_tuning.py
backend/app/services/skill_distiller.py
backend/app/services/skill_lifecycle.py
backend/app/services/skill_candidate_package.py
backend/app/services/skill_installation.py
backend/tests/services/test_skill_tuning.py
backend/tests/services/test_skill_rollback.py
backend/tests/services/test_skill_evolution_source_boundary.py
```

目标：

```text
只有“已固化 Skill 被真实调用后产生稳定偏差”才进入微调进化。
重复任务模式不再是已有 Skill 微调主依据。
系统内置标准 Skill 不进入 self-evolution patch target。
```

偏差信号来源：

```text
1. load_skill 后该 invocation 失败、绕路、或 assistant 自报 [OUTCOME: failure|crash]
2. 用户明确纠正：“这个 Skill 应该这么做 / 刚才这个 Skill 用错了”
3. Session feedback 标记 misleading / not useful，且可追到 loaded_skill_names
4. evaluator / behavior report 指向某个 Skill 的 reusable procedure 缺陷
5. 成功/失败对比显示失败是 Skill instructions 缺失或误导，而不是工具权限、外部系统、一次性数据问题
```

必须先做 common-vs-episodic 判定：

```text
Common deviation:
  可以进入 Skill patch candidate。
  条件：多个独立 evidence refs，或一个强用户纠正 + 可复现/可验证；偏差指向 Skill 的通用方法正文。

Episodic deviation:
  不能进入 Skill patch。
  例子：单次 API 故障、一次性日期/ID/客户背景、当前项目临时约束、用户当天偏好、凭据缺失。
  处理：留在 T0/T2/session evidence，必要时进入 memory 或 Work Ledger，不改 Skill。
```

微调尺度不是“重复了几次”本身，而是：

```text
证据强度 + 归因清晰度 + 影响半径
```

具体判定看 6 类信号：

```text
1. Skill 是否真的被调用
   必须能追到 load_skill("...")、loaded_skill_names、目标 skills/<slug>/SKILL.md。
   最好有 skill version/hash。
   没有真实 load_skill evidence 时，不能 patch 已有 Skill，只能进入新 Skill Pitch 或 T3 capability evidence。
   如果目标 Skill 的 evolvable=false，只能记录 platform issue，不能进入 Skill patch candidate。

2. 结果是否偏离 Skill 预期
   不是“任务失败了”就 patch Skill。
   必须证明失败与 Skill 本应提供的 procedure 有关：
   用户明确纠正、assistant 自报 failure/crash、session feedback 标记 misleading/not useful、
   evaluator/behavior report 指向 Skill procedure 缺陷。

3. 偏差归因
   Skill instruction 缺失或误导 -> 可以考虑 patch。
   工具权限不足、凭据缺失、外部服务不可用、网络失败 -> 不 patch Skill。
   一次性项目背景、日期、客户上下文 -> 不 patch Skill。
   模型没有遵守已经写清楚的 Skill -> 优先排查 runtime/prompt，而不是改 Skill。

4. 是否是共性问题
   Common deviation:
     多个独立 evidence refs 指向同一个 Skill 缺陷；
     或一个强用户纠正 + 可验证/可复现；
     且能提炼成通用规则。
   Episodic deviation:
     只有一次；
     或只和当前项目、当前用户、当前日期、当前 API 状态有关；
     只留 evidence，不改 Skill。

5. 能否形成局部、可复用的 patch
   好 patch 应该小而明确：
     补 decision rule
     补 failure case
     补工具选择顺序
     补 anti-pattern
     补 verification step
   如果 patch 变成项目记忆、客户上下文、当前任务状态，则应进入 Memory / Work Ledger，不应进入 Skill。

6. 验证和回滚是否齐备
   patch 前旧 SKILL.md 必须有 rollback snapshot。
   新 SKILL.md.draft 必须是 LLM 写的完整草稿。
   Skill Referee 必须判断 common-vs-episodic。
   SkillGuard / parse smoke / load smoke / behavior regression 必须通过。
   rollback_ref 必须可恢复。
```

一句话原则：

```text
新 Skill 看 T3 capability evidence 是否足够稳定；
已有自进化 Skill 微调看 load_skill 后的稳定偏差是否能归因到 Skill 本身；
系统内置标准 Skill 的问题进入 platform maintenance，不进入 agent self-evolution。
```

角色边界：

```text
Agent / Skill Writer LLM:
  读取偏差证据、成功/失败对比、当前 SKILL.md、相关 T3 capability evidence，写完整 SKILL.md.draft patch 或 promote draft。
  它不是 Memory Writer，不能把 T3 总结机械改写成 Skill。
  它的职责是把稳定、可复用的 procedure 封装成 progressive-disclosure capability capsule。
  它只能对 evolvable=true 的 Skill 写 patch；对 system_builtin 只能写 issue evidence / maintenance recommendation。
  凡是用户通过 Skill Creator 主动创建 Skill，最终 `SKILL.md.draft` 也必须由 Agent / Skill Writer LLM 写成完整文件。

Skill Referee LLM:
  独立判断这是 common deviation 还是 episodic deviation。
  复核 patch 是否只修改 reusable procedure，不夹带一次性上下文。
  复核候选 Skill 是否满足 Skill Creator 标准，而不只是语义上“看起来合理”。

Platform Skill Gate:
  检查路径、权限、SkillGuard、parse/load smoke、declared tools/packs、resource boundary、artifact gate、behavior regression、rollback snapshot、atomic commit。
  不得生成、补全、重写、拼装 Skill 语义正文。

Owner / tenant policy:
  决定哪些风险级别需要人工确认。
```

Skill Writer 产物标准：

```text
Skill Writer 的输出必须是一个完整 Skill 候选包，不是单段总结。
Skill Creator 可以负责访谈、收集样例、初始化目录、生成 scaffold、组织资源和 eval，但不能作为平台规则直接产出最终语义正文。

合格候选必须满足：

1. `SKILL.md.draft` 是完整 Markdown 文件，包含 YAML frontmatter 和正文。
2. frontmatter 至少包含 `name` 和 `description`。
3. `description` 必须写清楚 Skill 做什么、何时触发、何时不该触发；触发条件不能只写在 body。
4. body 只放核心 procedure、decision rule、anti-pattern、failure case、verification step。
5. 详细背景、schema、长例子进入 `references/`，不能重复塞进 body。
6. 重复、易错、需要稳定执行的步骤进入 `scripts/`，并必须有代表性运行验证。
7. 模板、样例工程、输出素材进入 `templates/` 或 `assets/`。
8. eval 用例进入 `evals/` 或 `eval_plan.md`，assertion 必须可验证，不能只检查表面字段。
9. patch 不得包含 session id、当前日期、客户私密上下文、一次性项目状态。
10. patch 必须能局部解释：为什么改、改了什么、对应哪些 evidence refs、如何回滚。
11. `candidate_signal.md`、scaffold template、usage counter、T3 摘要只能作为 evidence，不得被平台提升为 active `SKILL.md`。
```

Skill 标准格式：

```text
skills/<slug>/
  SKILL.md
  references/      optional, 按需读取的长文档、schema、规则、案例
  scripts/         optional, 可执行且可测试的确定性步骤
  templates/       optional, 可复用输出模板或工程模板
  assets/          optional, 输出素材、图标、字体、样例文件
  evals/           optional, evals.json、输入样例、断言文件
  agents/          optional, UI / harness metadata，如 openai.yaml
```

Hive runtime 当前必须消费的核心字段：

```yaml
---
name: example-skill
description: "Use this skill when ..."
tools:
  - optional_tool_name
packs:
  - optional_pack_name
allowed-tools:
  - optional_scope_hint
is_system: false
metadata:
  hive:
    requires_skills:
      - optional-dependent-skill
---
```

兼容原则：

```text
portable Skill 标准只要求 name / description。
Hive 可以扩展 tools / packs / allowed-tools / is_system / metadata.hive.requires_skills。
Platform Gate 必须容忍 legacy frontmatter，但只能依据 runtime 实际消费字段做治理判断。
```

Skill Referee / eval 判断标准：

```text
1. Static format:
   SKILL.md.draft 有合法 frontmatter、name/description、可解析 body。

2. Trigger quality:
   description 是 selector。
   它必须能触发相关任务，并避免误触发相邻但不相关的任务。
   不允许靠关键词堆砌扩大触发面。

3. Progressive disclosure:
   SKILL.md body 保持精简。
   长文档、schema、变体细节、模板、脚本必须拆进资源目录，并在 SKILL.md 中说明何时读取。

4. Procedure quality:
   patch 应该改善可复用 procedure，而不是记录某次任务背景。
   Referee 要检查 decision rule、failure case、tool order、verification step 是否真的覆盖偏差。

5. Eval quality:
   eval assertion 必须能区分真实成功和表面合规。
   例如不能只检查“文件存在”，还要检查内容正确性、结构、工具使用或输出质量。

6. Behavior delta:
   对 promote：最好能证明 with_skill 相比 without_skill 有正向 pass_rate / quality delta。
   对 patch：必须证明 patched_skill 相比 previous_skill 修复 common deviation，且没有明显 regression。

7. Validation integrity:
   forward-test / subagent / evaluator 只能拿到原始任务、输出、trace、artifact。
   不能泄露预期答案、诊断结论或 intended fix。

8. Governance:
   declared tools/packs、resource path、敏感内容、artifact 输出、rollback snapshot 全部通过 Platform Skill Gate。
```

Skill Candidate Package 形态：

```text
evolution/skill_candidates/<candidate_id>/
  SKILL.md.draft
  skill_pitch.md
  eval_plan.md
  evals/
  references/
  scripts/
  templates/
  assets/
  failure_cases.md
  deviation_report.md
  referee_review.md
  grading.json
  comparison.json
  analysis.json
  manifest.json
```

manifest 必须包含：

```json
{
  "schema": "skill_candidate_package.v1",
  "package_type": "patch|promote",
  "target_path": "skills/<slug>/SKILL.md",
  "skill_origin": "t3_auto_created|user_skill_creator",
  "evolvable": true,
  "source_refs": ["session:...", "runtime_task:...", "trace:..."],
  "deviation_class": "common|episodic|unknown",
  "writer": "Skill Writer Agent",
  "reviewer": "Skill Referee Agent",
  "validation": {
    "static_format": "pass|fail|not_run",
    "trigger_quality": "pass|fail|not_run",
    "behavior_delta": "pass|fail|not_run",
    "regression": "pass|fail|not_run"
  },
  "resource_paths": ["references/...", "scripts/...", "templates/...", "assets/...", "evals/..."],
  "rollback_ref": "evolution/rollback/skills/<candidate_id>/<slug>/SKILL.md.before",
  "status": "candidate|held|patched|promoted|rolled_back"
}
```

回滚必须从审计引用升级为可执行能力：

```text
promotion / patch 前:
  保存当前 active skill 到 evolution/rollback/skills/<candidate_id>/<slug>/SKILL.md.before

promotion / patch 后:
  evolution_ledger 记录 rollback_ref

rollback:
  按 candidate_id 找 promotion_decision.rollback_ref
  exact restore 到 skills/<slug>/SKILL.md
  写 evolution_ledger rollback event
  更新 skill candidate manifest status=rolled_back
```

微调拒绝条件：

```text
1. 没有真实 load_skill evidence
2. 目标 Skill 是 system_builtin 或 evolvable=false
3. 只有一次失败且无强用户纠正
4. 偏差来自工具权限、凭据、网络、外部服务不可用
5. patch 内容包含 session id、日期、客户私密上下文、一次性项目状态
6. 已有 Skill 不匹配，应该新建 Skill 或进入 memory/workflow，而不是 patch
7. Skill Referee 无法确认 common deviation
8. Platform Gate / regression / artifact gate 不通过
```

### 6.7 Dream soul candidate 迁到 memory staging

已修改：

```text
backend/app/services/auto_dream.py
```

路径迁移：

```text
evolution/soul_candidates/<candidate_id>/
  -> memory/.staging/soul_candidates/<candidate_id>/

evolution/rollback/soul/<candidate_id>.soul.md.before
  -> memory/.rollback/soul/<candidate_id>.soul.md.before
```

新 manifest 应包含：

```json
{
  "schema": "soul_candidate_package.v1",
  "candidate_id": "...",
  "target_path": "soul.md",
  "status": "committed|held",
  "source_refs": ["t3:memory/t3/..."],
  "pitch_path": "memory/.staging/soul_candidates/<id>/soul_pitch.md",
  "patch_path": "memory/.staging/soul_candidates/<id>/soul_patch.md",
  "next_path": "memory/.staging/soul_candidates/<id>/soul.md.next",
  "review_path": "memory/.staging/soul_candidates/<id>/review.md",
  "rollback_ref": "memory/.rollback/soul/<id>.soul.md.before",
  "write_audit": {
    "semantic_writer": "Dream / Soul Writer Agent",
    "reviewer": "Soul Memory Gate Agent",
    "physical_committer": "Platform Soul Gate"
  }
}
```

移除 Dream 对 `evolution/evolution_ledger.jsonl` 的 memory/soul 写入：

```text
不要再为 Dream soul candidate 调 record_memory_promotion_candidate()
不要再为 Dream soul decision 调 record_memory_promotion_decision()
```

改为写：

```text
memory/distillation_audit.jsonl
```

写入 event：

```json
{
  "stage": "soul_candidate",
  "outcome": "committed|held",
  "reason": "<gate reason>",
  "detail": {
    "candidate_id": "...",
    "candidate_package_path": "memory/.staging/soul_candidates/<id>",
    "rollback_ref": "...",
    "source_refs": []
  }
}
```

`soul.md` 仍然只在 gate pass 后 exact apply。

本轮行为红线：

```text
backend/tests/services/test_auto_dream.py::TestApplyDreamDecisions::test_commits_reviewed_soul_candidate_package_as_exact_next_file
backend/tests/services/test_auto_dream.py::TestApplyDreamDecisions::test_self_reviewed_soul_candidate_is_held
backend/tests/services/test_auto_dream.py::test_dream_consolidator_template_is_loaded_into_prompt
backend/tests/services/test_auto_dream.py::test_auto_dream_does_not_use_legacy_evolution_ledger_for_soul_writeback
```

### 6.8 移除 Dream blocklist 维护

已修改：

```text
backend/app/services/auto_dream.py
```

已移除调用和符号：

```python
_review_blocklist(agent_id)
```

Dream 不再维护：

```text
evolution/blocklist.md
```

如果旧 blocklist 仍需要可见，由新的 `agent_evolution_view.py` 放到 `legacy_audit` lane。

本轮源码红线：

```text
backend/tests/services/test_auto_dream.py::test_auto_dream_does_not_use_legacy_evolution_ledger_for_soul_writeback
```

### 6.9 停止 Heartbeat legacy evolution 写回

已修改：

```text
backend/app/services/heartbeat.py
backend/app/runtime/hooks_setup.py
backend/app/services/evolution_daemon.py
```

Heartbeat 主路径不再包含：

```python
_update_evolution_files(...)
_auto_seed_evolution(...)
_validate_bootstrap_completion(...)
run_skill_distillation_cycle(...)
run_scene_wiki_curation_tick(...)
run_dream(...)
validate_and_normalize_t3(...)
```

正确路径：

```text
Heartbeat emits HEARTBEAT_TICK_END hook
  -> T0 ledger 记录 runtime event
  -> evolution_maintenance_on_heartbeat hook schedules post-heartbeat maintenance
  -> evolution_daemon.run_heartbeat_evolution_maintenance()
       -> Skill distillation / curator
       -> scene/wiki curation
       -> Dream gate
       -> T3 normalization repair audit
       -> optional enhancement adapter sync
```

Heartbeat 可以读取：

```text
accepted T3
pending T2 packages
pending/committed T3 jobs
```

Heartbeat 不应该再依赖：

```text
evolution/scorecard.md
evolution/lineage.md
evolution/blocklist.md
```

本轮源码红线：

```text
backend/tests/services/test_evolution_daemon.py::test_heartbeat_source_no_longer_owns_peripheral_evolution_jobs
backend/tests/services/test_heartbeat.py::test_heartbeat_service_does_not_expose_legacy_evolution_writeback
backend/tests/services/test_evolution_state.py::test_heartbeat_no_longer_contains_legacy_evolution_writeback_symbols
backend/tests/architecture/test_memory_intelligence_boundaries.py::test_heartbeat_reflection_routes_to_memory_hooks_not_legacy_scorecard
```

### 6.10 Workspace bootstrap 清理

修改：

```text
backend/app/tools/workspace.py
```

停止 seed：

```text
evolution/scorecard.md
evolution/blocklist.md
evolution/lineage.md
```

保留或 lazy-create：

```text
evolution/skill_review.md
evolution/skill_usage.json
```

原因：

```text
skill ecosystem / skill tuning sidecars 仍属于 evolution/*
memory/soul evolution 不再属于 evolution/*
```

### 6.11 Legacy repair script

新增：

```text
backend/app/scripts/repair_memory_evolution_legacy.py
backend/tests/scripts/test_repair_memory_evolution_legacy.py
```

默认 dry-run。

apply 命令：

```bash
python -m app.scripts.repair_memory_evolution_legacy --apply --confirm
```

行为：

```text
dry-run:
  只报告会迁移什么，不写文件

apply:
  创建 memory/legacy/evolution/
  复制旧 scorecard/lineage/blocklist 到 memory/legacy/evolution/
  复制旧 memory/soul evolution ledger records 到 memory/legacy/evolution/evolution_ledger.memory_legacy.jsonl
  迁移 evolution/soul_candidates/* 到 memory/.staging/soul_candidates/*
  迁移 evolution/rollback/soul/* 到 memory/.rollback/soul/*
  写 memory/legacy/evolution/migration_report.json
```

实装命令：

```bash
cd backend
source .venv/bin/activate
python -m app.scripts.migrate_agent_evolution_paths --workspace /path/to/agent-workspace
python -m app.scripts.migrate_agent_evolution_paths --workspace /path/to/agent-workspace --apply
python -m app.scripts.migrate_agent_evolution_paths --data-root "$AGENT_DATA_DIR"
python -m app.scripts.migrate_agent_evolution_paths --data-root "$AGENT_DATA_DIR" --apply
```

安全原则：

```text
第一版不删除旧文件
目标文件已存在时保留两份，追加 timestamp/hash suffix
不重写语义内容
```

### 6.12 Runtime 边界、完整工作包与工期估算

这次完整修复需要碰 runtime，但只能碰已经存在的边缘接缝，不能重写核心执行循环。

允许修改的 runtime 接缝：

```text
backend/app/runtime/invoker.py
  - 继续使用现有 skill_catalog 动态注入位置。
  - 只把 latest_user_query / source / channel / task profile 等上下文传给 Skill Catalog Ranker。
  - 不改 AgentKernel，不改 tool loop，不改 provider 调用链。

backend/app/services/skill_runtime_telemetry.py
  - 增强 load_skill 后的 usage evidence。
  - 记录 loaded skill、目标路径/hash、terminal status、失败摘要、source refs。
  - telemetry 仍然 best-effort，不能影响用户请求成功/失败。
```

明确不动的核心：

```text
backend/app/kernel/engine.py 的主循环
ToolRuntimeService 执行链
LLM provider 调用链
现有 tool governance / Platform Gate 基础路径
```

完整工作包：

```text
1. Evolution v2 read model
   新增 agent_evolution_view.py。
   把页面/API 改成 read model，汇总 memory / soul / skill_ecosystem / skill_tuning / legacy_audit。

2. Skill Ecosystem Manager
   新增 skill_catalog_ranker.py。
   Skill catalog 不再按 registry insertion order，而是按 semantic match、usage heat、pin、scenario relevance、recent success、stale/cold penalty 排序。
   同时维护 skill_registry.json，明确 system_builtin / t3_auto_created / user_skill_creator / manual_import 的来源和 evolvable 状态。

3. Runtime catalog 接缝
   轻改 invoker.py 和 agent_context.py。
   把当前任务上下文交给 ranker，但不改变核心执行循环。

4. Skill Tuning Loop
   新增 skill_tuning.py，重构 skill_distiller / skill_lifecycle / skill_candidate_package 的 patch 判定路径。
   只有 evolvable=true 且真实 load_skill 后的稳定 common deviation 才能进入已有 Skill patch。
   系统内置标准 Skill 的缺陷只记录 platform issue / maintenance evidence。

5. 可执行 rollback
   promotion / patch 前保存 rollback snapshot。
   支持按 candidate_id exact restore active SKILL.md。
   rollback 写 evolution_ledger rollback event，并更新 candidate manifest。

6. Memory / Soul 退旧 evolution 写入
   Dream soul candidate 迁到 memory/.staging/soul_candidates。
   Dream memory/soul 不再写 evolution/evolution_ledger.jsonl。
   Heartbeat 不再写 evolution/scorecard.md、lineage.md、blocklist.md。
   legacy repair script 支持 dry-run/apply。

7. Frontend Evolution 页面更新
   更新 evolution API types、AgentEvolutionSection、i18n、测试。
   页面展示 Memory / Soul / Skill ecosystem / Skill tuning / Legacy audit，而不是模糊的 Skills。
```

原始工期估算：

```text
本地代码完整闭环：4-6 个工程日。
加前端验证、迁移 dry-run、回归修复：5-7 个工程日。
如果只做后端最小闭环、不含前端完整重做：3-4 个工程日。
```

本轮实际完成：

```text
1. Agent Evolution API 已切到 agent_evolution_view.v2 read model。
2. Frontend Evolution section 已使用 v2 类型展示 Memory / Soul / Skill ecosystem / Skill tuning / Legacy audit 信息。
3. Skill registry / origin / evolvable 边界、usage-aware ranker、Skill Creator 标准格式已接入并有测试。
4. Dream soul candidate 已写入 memory/.staging/soul_candidates，并把 committed/held audit 写入 memory/distillation_audit.jsonl。
5. Dream 不再读取或写入 memory/soul 的 evolution/evolution_ledger.jsonl，不再维护 evolution/blocklist.md。
6. Heartbeat 不再拥有 legacy evolution writeback，也不再直接执行外围 evolution jobs。
7. Heartbeat 结束后由 hook 调 evolution_daemon.run_heartbeat_evolution_maintenance() 处理外围维护。
8. T2 path contract 已统一为 memory/t2/sessions/<session_id>/segments/<segment_id>/{summary,labels,review,manifest}。
9. Knowledge read model 的 pending soul candidates 已从 memory/.staging/soul_candidates 与 memory/distillation_audit.jsonl 读取。
```

本轮验证证据：

```bash
cd backend && source .venv/bin/activate && pytest tests -q
# 4826 passed, 2 skipped, 4 warnings

cd backend && source .venv/bin/activate && ruff check \
  app/services/auto_dream.py app/services/heartbeat.py app/services/evolution_daemon.py \
  app/runtime/hooks_setup.py app/services/agent_evolution_view.py app/services/knowledge_read_model.py \
  app/api/agents.py tests/services/test_auto_dream.py tests/services/test_heartbeat.py \
  tests/services/test_evolution_state.py tests/services/test_evolution_daemon.py \
  tests/services/test_heartbeat_skill_distillation.py tests/services/test_agent_evolution_view_v2.py \
  tests/services/test_knowledge_read_model.py tests/architecture/test_memory_intelligence_boundaries.py \
  tests/runtime/test_fast_reflection_hook.py tests/test_memory_integration.py
# All checks passed

cd frontend && npm run build
# tsc && vite build passed

cd frontend && npm run test
# 47 test files passed, 232 tests passed
```

估算依据：

```text
主要复杂度不在新增代码量，而在边界测试：
  - Skill catalog 动态排序不能破坏 prompt budget / progressive disclosure。
  - Skill tuning 不能把偶发失败误判成 patch。
  - rollback 必须从审计引用升级为真实恢复能力。
  - Dream / Heartbeat 退旧路径不能破坏 memory/soul 主链。
  - Evolution 页面必须从旧单 lane 改成多来源 read model。
```

## 7. 前端一次性实现计划

修改：

```text
frontend/src/api/domains/evolution.ts
frontend/src/pages/agent-detail/AgentEvolutionSection.tsx
frontend/src/i18n/en.json
frontend/src/i18n/zh.json
frontend/src/pages/agent-detail/AgentDetailSections.test.tsx
```

新类型：

```ts
export type EvolutionLane = 'memory' | 'soul' | 'skill_ecosystem' | 'skill_tuning' | 'legacy_audit';

export type EvolutionEvent = {
  id: string;
  at: string;
  lane: EvolutionLane;
  stage: string;
  kind: string;
  status: string;
  title: string;
  detail: string;
  refs: string[];
  paths: string[];
  metadata: Record<string, unknown>;
};

export type AgentEvolutionView = {
  schema: 'agent_evolution_view.v2';
  summary: Record<string, number>;
  lanes: Record<EvolutionLane, EvolutionEvent[]>;
  timeline: EvolutionEvent[];
  skill_summary: EvolutionSkillSummary;
  skills: EvolutionSkill[];
};
```

UI 结构：

```text
Summary strip:
  T0 evidence
  T2 packages
  T3 commits
  Soul candidates
  Hot skills
  Cold skills
  Skill tuning candidates
  Held/rebase items

Lane segmented control:
  Memory
  Soul
  Skill ecosystem
  Skill tuning
  Legacy audit

Timeline row:
  lane badge
  kind/status
  timestamp
  title/detail
  refs/paths compact expandable row

Empty state:
  暂无学习轨迹。运行时会先产生 T0 evidence，随后 memory pipeline 会生成 T2/T3 轨迹。

Skill ecosystem:
  展示 hot/default/cold/archived 技能分布
  展示排序依据：use heat、scenario match、pin、stale penalty
  明确说明冷藏只影响默认可见性，不是删除

Skill tuning:
  展示 common deviation / episodic deviation 判定
  展示 patch candidate、referee review、gate result、rollback ref
  偶发问题只显示为 evidence，不显示为 patch trajectory

Legacy audit:
  默认折叠
  明确标注 legacy / compatibility，不是 current memory truth
```

## 8. TDD 测试计划

本文档是文档改动，不需要 TDD。实现阶段必须按 TDD 执行。

### 8.1 后端红灯测试

新增：

```text
backend/tests/services/test_agent_evolution_view.py
backend/tests/services/test_skill_catalog_ranker.py
backend/tests/services/test_skill_catalog_dynamic_ordering.py
backend/tests/services/test_skill_tuning.py
backend/tests/services/test_skill_rollback.py
backend/tests/services/test_auto_dream_memory_evolution_boundary.py
backend/tests/services/test_heartbeat_memory_evolution_boundary.py
backend/tests/scripts/test_repair_memory_evolution_legacy.py
```

更新：

```text
backend/tests/api/test_agent_evolution_api.py
backend/tests/services/test_auto_dream.py
backend/tests/services/test_heartbeat.py
backend/tests/services/test_skill_distiller.py
backend/tests/services/test_skill_lifecycle.py
backend/tests/services/test_skill_loading.py
backend/tests/tools/test_workspace.py
```

必须覆盖：

```text
1. build_agent_evolution_view() 从 T2 package manifest 生成 memory lane event
2. 从 memory/.staging/t3_jobs/*/manifest.json 生成 committed/held/rebase event
3. 从 memory/control/lifecycle.json 生成 lifecycle event
4. 从 memory/.staging/soul_candidates/*/manifest.json 生成 soul event
5. 从 skill sidecars 生成 skill_ecosystem lane event
6. 从 skill candidate / patch / rollback 生成 skill_tuning lane event
7. 从 skill_registry.json 生成 skill_origin / evolvable metadata
8. system_builtin Skill 只生成 ecosystem/issue event，不生成 self-evolution patch target
9. t3_auto_created 和 user_skill_creator 都能进入 Skill Candidate Package -> active Skill -> tuning/rollback 链路
10. Skill catalog 按 usage heat、pin、scenario match 排序，不再按 registry insertion order
11. cold skill 默认降权，但显式 query / load_skill 能恢复可见
12. repeated task pattern 只能创建 new skill candidate，不能直接触发 existing skill patch
13. common deviation 可以生成 patch candidate；episodic deviation 只能留作 evidence
14. Skill Creator 主动创建路径必须产生 Agent-authored `SKILL.md.draft`，不能由平台 scaffold 直接变 active Skill
15. `candidate_signal.md` 只能作为 evidence，不能被安装为 active `skills/<slug>/SKILL.md`
16. Skill patch 前必须保存 rollback snapshot
17. rollback by candidate_id 可以 exact restore active SKILL.md 并写 rollback event
18. 旧 memory/soul evolution_ledger records 只能进 legacy_audit，不能进 memory/soul lane
19. corrupt/missing manifest 不导致 500
20. Dream soul candidate package 路径在 memory/.staging/soul_candidates
21. Dream 不再为 memory/soul 创建或追加 evolution/evolution_ledger.jsonl
22. Dream 不再编辑 evolution/blocklist.md
23. Heartbeat 不再写 evolution/scorecard.md、lineage.md、blocklist.md
24. workspace bootstrap 不再创建 legacy scorecard/lineage/blocklist
25. repair script dry-run 不写文件
26. repair script apply 写 migration report 且保留旧数据
```

### 8.2 前端红灯测试

更新：

```text
frontend/src/pages/agent-detail/AgentDetailSections.test.tsx
frontend/src/api/domains/evolution.test.ts
```

必须覆盖：

```text
1. API adapter 接受 v2 schema
2. AgentEvolutionSection 渲染 Memory / Soul / Skill ecosystem / Skill tuning / Legacy audit lanes
3. Legacy audit 不作为 primary memory truth 展示
4. 空状态说明 T0/T2/T3 learning trajectory
5. Skill ecosystem 显示 hot/default/cold/archived 和排序原因
6. Skill ecosystem 显示 system_builtin / t3_auto_created / user_skill_creator 来源和 evolvable 状态
7. Skill tuning 显示 common/episodic deviation、patch candidate、rollback ref
8. system_builtin Skill 的问题显示为 platform maintenance issue，而不是 patch candidate
9. skill compatibility fields 仍可显示 skill asset summary
```

## 9. 验证命令

后端 targeted：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_agent_evolution_view.py \
  tests/services/test_skill_catalog_ranker.py \
  tests/services/test_skill_catalog_dynamic_ordering.py \
  tests/services/test_skill_evolution_source_boundary.py \
  tests/services/test_skill_tuning.py \
  tests/services/test_skill_rollback.py \
  tests/services/test_auto_dream_memory_evolution_boundary.py \
  tests/services/test_heartbeat_memory_evolution_boundary.py \
  tests/scripts/test_repair_memory_evolution_legacy.py \
  tests/api/test_agent_evolution_api.py \
  -q
```

后端 regression slice：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest \
  tests/services/test_auto_dream.py \
  tests/services/test_heartbeat.py \
  tests/memory/test_t2_segment_package_builder.py \
  tests/memory/test_t3_consolidation_platform_gate.py \
  tests/services/test_skill_distiller.py \
  tests/services/test_skill_lifecycle.py \
  tests/services/test_skill_loading.py \
  tests/tools/test_workspace.py \
  -q
```

后端 lint：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/ tests/
```

前端 targeted：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- AgentDetailSections.test.tsx evolution.test.ts
```

前端 build：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run build
```

迁移 dry-run：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m app.scripts.repair_memory_evolution_legacy --dry-run
```

迁移 apply：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m app.scripts.repair_memory_evolution_legacy --apply --confirm
```

`apply` 会改变 workspace 文件，因此必须先 dry-run，再由操作者明确确认。

## 10. 实施顺序

这是一个完整 change set，但实现顺序必须服务 TDD 和安全边界：

```text
1. 写 agent_evolution_view 后端红灯测试
2. 实现 agent_evolution_view.py
3. 切换 GET /agents/{agent_id}/evolution 到 v2 read model
4. 写 Skill Ecosystem Manager 红灯测试
5. 实现 skill_registry.json、usage-aware / scenario-aware catalog ranker、自进化 Skill 来源边界
6. 写 Skill Tuning Loop 红灯测试
7. 实现 common-vs-episodic deviation 判定、evolvable gate、Skill Writer 候选包、Skill Referee review、rollback snapshot
8. 写 Dream boundary 红灯测试
9. 把 soul candidate staging 移到 memory/.staging/soul_candidates
10. 停止 Dream memory/soul 写 evolution_ledger
11. 停止 Dream blocklist maintenance
12. 写 Heartbeat boundary 红灯测试
13. 停止 Heartbeat legacy evolution writeback
14. 更新 workspace bootstrap
15. 写 repair script 测试并实现脚本
16. 更新 frontend types 和页面
17. 更新 i18n
18. 跑 targeted backend/frontend tests
19. 跑 regression slices 和 build
20. 跑 migration dry-run 并检查输出
21. 把最终实现证据补回本文档
```

生产迁移不能随 deploy 自动 apply。迁移脚本必须先 dry-run，并要求显式 apply confirmation。

## 11. Definition of Done

全部完成必须同时满足：

```text
1. GET /agents/{agent_id}/evolution 返回 agent_evolution_view.v2
2. 前端 Evolution 页面展示 Memory / Soul / Skill ecosystem / Skill tuning / Legacy audit lanes
3. T0/T2/T3/lifecycle/soul events 出现在主 timeline
4. legacy scorecard.md、lineage.md、blocklist.md 不再作为当前 memory truth
5. Dream soul candidates 写到 memory/.staging/soul_candidates
6. Dream memory/soul events 不再 append 到 evolution/evolution_ledger.jsonl
7. Dream 不再编辑 evolution/blocklist.md
8. Heartbeat 当前路径不再写 evolution/scorecard.md、lineage.md、blocklist.md
9. Skill catalog 使用 usage-aware / scenario-aware 动态排序，hot/default/cold/archived 状态可解释
10. cold skill 默认降权但不删除，显式调用或强场景匹配可恢复可见
11. `evolution/skill_registry.json` 记录每个 active Skill 的 skill_origin、evolvable、active_version_hash、last_candidate_id
12. system_builtin Skill 可以排序和记录 telemetry，但不进入 self-evolution patch/promote target
13. t3_auto_created 和 user_skill_creator 两类 Skill 都通过 Skill Candidate Package 入链，并统一进入后续 tuning / rollback
14. Skill tuning 只处理 evolvable=true 的 common deviation；episodic deviation 不进入 patch 序列
15. Active `SKILL.md` patch 必须由 LLM 写完整草稿，平台只 exact commit 通过 gate 的草稿
16. 用户通过 Skill Creator 主动创建的 Skill，也必须由 Agent / Skill Writer LLM 写完整 `SKILL.md.draft`
17. 平台不得从 scaffold、candidate_signal、usage counter、T3 摘要机械生成 active `SKILL.md`
18. Skill patch/promote 前存在 rollback snapshot，rollback by candidate_id 可恢复并写 ledger
19. Skill Candidate Package 包含完整 `SKILL.md.draft`、`skill_pitch.md`、eval/referee 证据、resource manifest、rollback_ref
20. Skill 候选满足 Skill Creator 标准：frontmatter、触发描述、progressive disclosure、资源边界、eval assertion、forward-test integrity
21. Skill evolution 不再依赖“重复任务模式”作为已有 Skill 微调主依据
22. 旧 workspace 文件可以通过 dry-run/apply 脚本迁移
23. 没有 memory/soul semantic write 绕过 Memory Gate 或 Platform Gate
24. 测试覆盖 read model、Skill ecosystem、Skill source boundary、Skill tuning、Skill candidate validation、Dream boundary、Heartbeat boundary、migration script、API、frontend rendering
25. ruff check app/ tests/ 通过
26. frontend tests 和 build 通过
27. 本文档补充最终实现证据
```

## 12. 编码前仍需确认的实现选择

这些不是产品方向 blocker，只是实现策略：

### 12.1 是否同步重命名 `evolution_view.py`

建议：

```text
本次先不重命名，避免扩大 diff。
新增 agent_evolution_view.py 作为用户页面/API 的主读模型。
旧 evolution_view.py 只服务 skill digest，并补注释说明。
```

### 12.2 repair script 默认 copy 还是 move

建议：

```text
默认 copy。
不做删除。
如未来要 move/delete，再单独加 --move-legacy 或 --delete-legacy，并要求额外确认。
```

### 12.3 是否把旧 memory/soul evolution ledger 转成 distillation audit

建议：

```text
是，但只通过 repair script 做。
原始 JSON 必须完整保存在 memory/legacy/evolution/。
转换结果进入 memory/distillation_audit.jsonl。
```

### 12.4 Legacy audit 是否默认可见

建议：

```text
默认折叠。
它是 evidence，不是 current truth。
```

### 12.5 Skill catalog 排序是否影响实际工具权限

建议：

```text
不影响。
排序只决定 prompt visibility 和 progressive disclosure 优先级。
真实工具调用仍由 ToolRuntimeService / Capability Gate / ActionPreflight 决定。
```

### 12.6 Skill 微调是否允许 agent 自己批准自己

建议：

```text
不允许。
执行中的 Agent 可以发现偏差和写 patch 草稿。
Skill Referee 必须独立复核 common-vs-episodic。
Platform Skill Gate 负责硬门和 exact commit。
高风险或 tenant policy 要求的 patch 需要 owner/admin confirmation。
```

### 12.7 重复任务模式保留在哪里

建议：

```text
保留，但按用途分开。
对新 Skill：重复任务模式、T3 capability evidence、skill_seed 可以作为 Skill Pitch 的主要原材料。
对 Workflow：如果证据显示“不许漂移的固定步骤/审批链/周期编排”，它应进入 workflow_reference_hint，而不是 Skill。
对已有 Skill patch：这些材料只能作为背景证据，不能单独决定 patch；必须额外出现真实 `load_skill` 后的稳定偏差信号。
```

### 12.8 系统内置 Skill 暴露问题时走哪里

建议：

```text
不进入 agent self-evolution。
记录为 platform skill issue / maintenance evidence。
可以在 Evolution 页面 skill ecosystem/tuning lane 展示为 system_skill_issue。
修复必须通过平台 release / repo change / 明确授权的维护流程，不允许 agent 用会话 evidence 直接 patch bundled Skill。
```

## 13. 最终对外解释口径

改完后，当用户问“进化到底写在哪里”，答案应该是：

```text
Evolution 页面不是一个新的写入位置。它是 read model，汇总三个来源：

1. Memory / Soul 学习轨迹：写在 memory system 和 soul.md 里。
2. Skill 生态与微调轨迹：写在 evolution/* skill sidecars 里。
3. Legacy audit：旧 evolution scorecard/lineage/blocklist，只作为兼容归档。

Memory / Soul 侧的学习轨迹具体写在：

- raw evidence: memory/t0/sessions/<session_id>/segments/<segment_id>/source.md
- reviewed summaries: memory/t2/sessions/<session_id>/segments/<segment_id>/
- semantic memory: memory/t3/{episodes,user,worker,capabilities}.md
- lifecycle/audit: memory/control/lifecycle.json and memory/distillation_audit.jsonl
- soul promotion candidates: memory/.staging/soul_candidates/<candidate_id>/
- committed identity: soul.md

Skill 侧不写进 memory/t3 作为最终 source of truth。它只消费 memory/t3/capabilities.md 里的 capability evidence / skill_seed，然后把候选、评估、晋升、回滚写在：

- skill ecosystem: evolution/skill_registry.json, evolution/skill_usage.json, dynamic catalog ranker, hot/default/cold/archived state
- skill tuning: evolution/skill_usage.jsonl, evolution/skill_candidates/<candidate_id>/, evolution/evolution_ledger.jsonl, evolution/rollback/skills/<candidate_id>/

旧 evolution scorecard/lineage/blocklist 只是 legacy audit，不是当前记忆 truth。

不是所有 Skill 都参与“自进化”。

系统内置标准 Skill：
  只参与 catalog 排序、usage telemetry、冷藏降权。
  不参与 agent self-evolution patch/promote。
  如果暴露问题，记录为 platform skill issue，由平台 release 或明确授权的维护流程修。

自进化 Skill 只有两个默认来源：

1. T3 自动创建：
   memory/t3/capabilities.md / skill_seed / T0/T2 source_refs
   -> Skill Pitch
   -> Skill Candidate Package
   -> Referee / Platform Gate
   -> active skills/<slug>/SKILL.md

2. 用户主动创建：
   用户在对话中调用 Skill Creator，提供意图、样例、约束、期望输出
   -> Agent / Skill Writer LLM 按 Skill Creator 规范写完整 `SKILL.md.draft`
   -> Skill Candidate Package
   -> Referee / Platform Gate
   -> active skills/<slug>/SKILL.md

这两种来源一旦成为 active evolvable Skill，后续微调标准完全统一。

Skill 的“进化”不等于“重复任务次数到了就写新 Skill”。
Skill 分两层：

1. 生态管理：根据场景、热度、pin、近期成功率动态调整 Skill 的默认可见顺序；低频 Skill 冷藏但不删除。
2. 微调进化：只有某个 evolvable=true 的已加载 Skill 发生稳定、可复核的共性偏差时，才由 Skill Writer 写完整 progressive-disclosure Skill patch，经 Referee 和 Platform Skill Gate 后 exact commit；偶发一次性问题只留 evidence，不改 Skill。

Skill Writer 写的不是“记忆总结”，而是一个完整 Skill Candidate Package：

- `SKILL.md.draft`: 包含 `name` / `description` frontmatter 和核心 procedure。
- `skill_pitch.md`: 说明为什么需要新 Skill 或 patch、证据来自哪里。
- `eval_plan.md` / `evals/`: 说明如何验证触发、行为、回归。
- `references/` / `scripts/` / `templates/` / `assets/`: 只在真正需要时进入候选包。
- `referee_review.md` / `grading.json` / `comparison.json` / `analysis.json`: 记录独立评判、真实任务证据和改动原因。
- `manifest.json`: 记录 skill_origin、evolvable、source_refs、target_path、validation、resource_paths、rollback_ref、status。

实现状态（2026-06-23）：

- `evolution/skill_candidates/<candidate_id>/SKILL.md.draft`、`candidate_signal.md`、T3 `[container=skill_candidate]` 现在进入同一个 distiller intake；没有新的 session workflow evidence 时也会被送入 Skill Writer，而不是只被计数后 `idle`。
- `referee_review.md` 已成为 active commit 前的必需候选包产物。Skill Referee LLM 必须 `approve`，且 `common_vs_episodic`、`scope`、`overlap`、`safety`、`eval_readiness` 全部 >= 3；否则 promotion decision 记录为 `held`，不写 `skills/<slug>/SKILL.md`。
- Platform 仍只做 manifest、路径、SkillGuard、behavior eval、regression、artifact gate、Referee 结果、rollback 与 exact commit，不生成或改写 `SKILL.md.draft` 的语义正文。

平台不能替 Agent 写 Skill。
Skill Creator 的平台部分只能做 scaffold、证据组织、资源目录、校验、权限、回滚和 exact commit。
`SKILL.md.draft` 的语义正文必须由 Agent / Skill Writer LLM 写成，并且必须符合 Skill Creator 规范。
```
