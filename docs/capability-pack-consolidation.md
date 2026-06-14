# Capability Pack 体系整改设计稿（一次改完，零技术债）

> 状态：设计稿 v0，待 owner 在本文档上拍板核心决策点（见 §8）。
> 主线归属：`docs/execution-mode-spectrum.md`（CORE 收敛 / 选择性去 skill 化）的延伸。
> 纪律：本整改按"交付纪律 — 一次改完，零债"执行（CLAUDE.md / [[feedback_no_mvp_finish_completely]]）——
> 完整 scope 一次定义、一次交付，禁 MVP / 禁分期首版 / 禁默认关 flag 藏半成品。

---

## 0. 缘起与北极星

对标 Claude Code(`/Users/rocky243/Context Engineering/claude-code-org`)的工具体系时发现：CC 没有
"capability pack"这层概念——它用 `getAllBaseTools()`(`src/tools.ts:193`)**扁平全暴露**所有内置工具，
靠 **ToolSearch defer** 控制 context 膨胀。Hive 用 **CORE 常驻 + pack 按需激活 + MCP** 三层。

pack 这层对 Hive **有合理性**(CC 所没有的 Hive delta)：多租户凭据隔离、领域工具分组、按需激活控制
frozen-prefix 体积。**因此目标不是把 pack 砍光向 CC 看齐，而是把 pack 提纯**——让三层各司其职：

| 层 | 定义（北极星判据） | 对标 |
|----|-------------------|------|
| **CORE** | **无凭据即可用**的底座能力——任何数字员工开箱即用，不依赖任何租户配置 | CC `getAllBaseTools()` base |
| **pack** | **需凭据 / 需渠道配置 / 需治理**的领域增强，多租户按需激活 + 凭据隔离 | Hive delta（CC 无） |
| **MCP** | 外部扩展，运行时导入 | CC MCP |

**判据落到一句话**：一个工具如果"裸租户也能直接跑"，它属于 CORE；如果"必须先配 key / 连渠道 / 过治理"，
它属于 pack。当前体系大量违反这条判据——这就是要整改的根。

---

## 1. 现状全景（已验证证据）

### 1.1 CORE 常驻 41 个（`app/services/agent_tools.py:136 CORE_TOOL_NAMES`）

代码执行 `execute_code` `run_command` · 文件 `read_file` `write_file` `edit_file` `list_files`
`glob_search` `grep_search` `fs_read` `fs_write` `fs_list` · 技能 `load_skill` `save_skill` ·
记忆 `search_memory` `load_memory` `save_memory` `update_memory` `retire_memory` ·
触发 `set_trigger` `update_trigger` `cancel_trigger` `list_triggers` ·
协作/源能力 `send_message_to_agent` `delegate_to_agent` `spawn_subagent` `preview_workflow` `start_workflow` ·
异步 `check_async_task` `cancel_async_task` `list_async_tasks` ·
工作便签 `track_todo` `record_finding` `read_ledger` ·
渠道 `send_channel_message` `send_channel_file` ·
Plan/交互 `request_plan_mode` `exit_plan_mode` `ask_user_question` ·
其他 `tool_search` `web_fetch` `get_current_time`

### 1.2 9 个 pack（`app/tools/runtime_tool_groups.py:20 RUNTIME_TOOL_GROUPS`）

| pack | source | infer | 工具数 | 与 CORE 的重叠 |
|------|--------|-------|--------|----------------|
| `web_pack` | system | ✓ | 4 | `web_fetch` 在 CORE（脚踏两船） |
| `feishu_pack` | channel | ✓ | 33 | 无（纯领域，健康） |
| `plaza_pack` | system | ✓ | 3 | 无（健康） |
| `email_pack` | system | ✓ | 3 | 无（纯领域，健康） |
| `coordination_pack` | system | ✓ | 8 | **8/8 全在 CORE（死 pack）** |
| `mcp_admin_pack` | mcp | ✓ | 5 | 无（健康，source=mcp 特殊路由） |
| `office_pack` | system | ✗ | 9 | `read_file` `list_files` `send_channel_file` 在 CORE（底座混入） |
| `deep_research_pack` | system | ✗ | 5 | 无（健康） |
| `plan_mode_pack` | system | ✗ | 3 | **3/3 全在 CORE（死 pack）** |

---

## 2. 问题清单（6 类结构债）

**P1 — 死 pack（零增益激活单元）。** `coordination_pack`(8 工具)与 `plan_mode_pack`(3 工具)的成员
**全部已在 CORE 常驻**。激活它们不会带来任何新工具——它们作为"capability pack"(激活=带出工具)是死的。
更糟的是它们仍出现在 `tool_search` 发现结果里(`iter_runtime_tool_groups`)，**误导模型以为要先激活才能用**，
而其实早已常驻。负价值。

**P2 — 脚踏两船工具。** `web_fetch` / `read_file` / `list_files` / `send_channel_file` 同时声明 `pack=` 又被
列入 CORE。语义自相矛盾(到底常驻还是需激活)；现状 CORE 提升优先(实际常驻)，pack 成员身份纯属误导 +
让 pack 体积虚胖。

**P3 — 底座工具混入领域 pack。** `office_pack` 塞进了 `read_file` / `list_files`(纯文件底座)。违反"pack=领域
增强"语义——读文件不是办公领域能力，是所有 agent 的底座，已在 CORE。

**P4 — 凭据耦合（需区分真假病）。**
- **真病**：`web_search` 把 Exa/Tavily key 挂在 **per-agent 工具 config**(`search.py:45` 的 `config_schema`
  password 表单)。它本该是 CORE 联网底座(无 key 也能跑，见 §3)，却被 per-agent 凭据拖住进不了 CORE。
- **非病**：`feishu_pack`(app_id/secret)、`email_pack`(SMTP/IMAP)、`firecrawl/xcrawl`(API key)的凭据是
  **渠道/provider 配置**，本就该 per-agent 或 per-tenant 配、本就该 pack 化。保留。

**P5 — pack 职责二义 + 机制不统一。** `activation_mode` 字段每个 pack 描述都不同(有的"tool_search 发现"、
有的"按需激活"、有的"显式启用"、plan_mode "用户批准")；`infer_from_tools` 有 True 有 False 无统一规则。
根因：pack 同时承担了**两个打架的职责**——"激活单元"(激活带出工具)与"tool_search 展示分组"(给模型看的能力
目录)。死 pack 正是这个二义的产物(作为展示分组列出来，作为激活单元是空的)。

**P6 — 文档漂移。** `CLAUDE.md` 称 pack 定义在 `tools/packs.py` 的 `ToolPackSpec`——**该文件已不存在**，
真实定义在 `runtime_tool_groups.py` 的 `RuntimeToolGroupSpec`。`CLAUDE.md` 的 "100+ tool / 18 handler" 等
数字也需复核。整改完一并修文档。

---

## 3. web_search 专项：兜底升级 + 凭据归位

### 3.1 复杂度定位（已验证）

`web_search` 的 handler 仅 3 行(`search.py:104` 转发到 `web_mcp.py:_web_search`)。它和已在 CORE 的
`web_fetch` 是孪生(同 `pack="web_pack"`、`is_default=True`、`governance="safe"`、`read_only`、`parallel_safe`)。
**唯一差别**：`web_search` 绑了 provider 凭据 config，`web_fetch` 没有 → 所以 fetch 干净进了 CORE，search 被
凭据拖住。

且 `web_search` **无 key 已能运行**(`web_mcp.py:211` `_web_search`)：`auto → 有 Exa 用 Exa / 有 Tavily 用
Tavily / 都没有走 DuckDuckGo`；指定 provider 缺 key 也**自动 fallback DuckDuckGo + note**(`web_mcp.py:261`)，
**无任何硬拒 raise**(旧 "Tier2 硬拒" 已移除)。→ 功能上提进 CORE 零阻塞，只差把凭据归位。

### 3.2 兜底生态现状（2026.6 已联网核实）

| 方案 | 免费额度 | 速率/可靠性 | 适配 Hive |
|------|---------|------------|-----------|
| DuckDuckGo html 抓取（现状） | 免费 | **30 req/min 搜索，并发即限流报错** | 差，撑不起 agent |
| Bing Web Search API | — | **2025-08-11 整族退役** | 死（含 `bing_search` alias） |
| Brave Search API | ~$5/月 credit | **2026-02-12 砍免费层** | 退化为付费 |
| Serper（Google SERP） | 2500/月 | 稳，便宜 | 可选商业增强 |
| Tavily（已集成） | 1000/月 | agent 专用，稳 | 租户增强 |
| Exa（已集成） | 1000/月 | 语义最佳 | 租户增强 |
| **SearXNG（自托管）** | **无限（自控基础设施）** | **零 API key、零速率限制**，聚合 70+ 源 | **最优——见下** |

### 3.3 方案：SearXNG 作平台级默认兜底

裸租户的搜索底线从 DuckDuckGo 换成**自托管 SearXNG**，理由(契合 Hive 定位)：

- **零边际成本 + 零限流**——根治 DDG 限流痛点，裸租户也有体面搜索质量。
- **中立第三方**——不绑任何商业搜索 vendor，符合 AI-Native L3 "模型平等 / 组织级中立控制中台"。
- **自托管可控 + 隐私**——查询不出平台，多租户共享一个平台实例。
- **Railway 原生**——有一键部署模板，Hive 生产即在 Railway。

**最终兜底链**：`SearXNG(平台默认，自托管)` → `Exa/Tavily(租户配 key 的语义增强)` →
`DuckDuckGo(SearXNG 实例不可用时的最后兜底)`。

实现：在 `web_mcp.py` 新增 `_search_searxng(query, max_results)` provider；`_web_search` 的 `auto` 分支
优先 SearXNG(读平台级 `SEARXNG_URL` 配置)，缺失才退 DDG。`SEARXNG_URL` 走环境变量/平台配置，非 per-agent。

### 3.4 凭据归位

把 Exa/Tavily key 从 **per-agent 工具 config**(`search.py` `config_schema` password 字段)迁到
**tenant 级搜索 provider 配置**(加密存储，走 `SECRETS_MASTER_KEY` + RLS，复用 `_get_exa_api_key` /
`_get_tavily_api_key` 已有的系统级读取路径，`web_mcp.py:373/382`)。⚠️ 别重蹈 [[project_system_audit_20260609]]
租户 key 泄漏——凭据加密 + 租户隔离 + 不回显明文。

---

## 4. fs_* 专项：双轨收敛

CORE 同时有细分版(`read_file`/`write_file`/`list_files`/`glob_search`/`grep_search`)与合并版
(`fs_read`/`fs_write`/`fs_list`，mode-dispatch，`filesystem.py:438`)，语义重叠，8 个 schema 全进 frozen prefix。

**方案：删 `fs_read`/`fs_write`/`fs_list`，留细分版**(对齐 CC——CC 只有 `FileRead/Write/Edit/Glob/Grep`，
不做 mode-dispatch；细分工具语义清晰、可独立设 governance/parallel_safe、错误信息精确)。

**必带的 backfill(否则就是留债)**：`fs_read` 的 `mode=document` 映射 `read_document`，而 `read_document`
**不在 CORE** → 删 fs_* 会丢"常驻读 PDF/Word/Excel"。删除前逐一核对 `fs_read/fs_write/fs_list` 各 mode 映射的
细分工具是否都在 CORE，缺口(已知 `read_document`)一并补进 CORE。

---

## 5. 逐 pack 处置方案

| pack | 处置 | 动作 |
|------|------|------|
| `coordination_pack` | **删除（死 pack）** | 8 工具已在 CORE；删 pack 定义，工具不动 |
| `plan_mode_pack` | **删除（死 pack）** | 3 工具已在 CORE；删 pack 定义，工具不动 |
| `web_pack` | **重构** | `web_search`+`web_fetch` 出 pack 进 CORE；pack 改名 `web_provider_pack`，仅留 `firecrawl_fetch`/`xcrawl_scrape`；Exa/Tavily key 移 tenant 级（§3.4） |
| `office_pack` | **提纯** | 移出 `read_file`/`list_files`/`send_channel_file`（已在 CORE）；保留 `read_document`+`office_document_*` 专属工具 |
| `feishu_pack` | 保留（健康） | 无 |
| `email_pack` | 保留（健康） | 无 |
| `plaza_pack` | 保留（健康） | 无 |
| `mcp_admin_pack` | 保留（健康） | 无 |
| `deep_research_pack` | 保留（健康） | 无 |

### CORE 净增删

| 动作 | 工具 | 理由 |
|------|------|------|
| **删** | `fs_read` `fs_write` `fs_list` | 双轨冗余（§4） |
| **加** | `web_search` | 联网搜索底座（SearXNG 兜底，无 key 可用，§3） |
| **加** | `read_document` | 补 fs_* 删除后的 document 读取能力（§4） |

CORE：41 − 3 + 2 = **40**。

### pack 体系净变化

9 pack → **删 2(死) + web_pack 重构为 web_provider_pack + office_pack 提纯** → 实际激活型 pack 收敛为 7 个，
每个都满足"裸租户跑不了、必须配凭据/连渠道/过治理"的判据。

---

## 6. 职责二义根治（P5）

pack 的两个打架职责拆开：
- **激活单元**：`RuntimeToolGroupSpec` 只保留"激活后带出工具"的语义；死 pack 删除后，所有 pack 激活都有真实增益。
- **tool_search 展示**：模型发现能力走 `tool_search`，CORE 工具本就常驻可见、无需"发现"；pack 工具通过
  `tool_search` 发现 schema 再按需激活。CORE 工具不应出现在"需激活"的发现结果里。

`activation_mode` 字段统一为三类枚举(取代当前自由文本)：`schema_discoverable`(tool_search 发现 schema)/
`explicit_only`(显式启用，如 mcp_admin)/`approval_gated`(用户批准，已无——plan_mode 删除后)。
`infer_from_tools` 统一规则：仅 `source=system/channel` 且工具名唯一归属时 `True`。

---

## 7. 实施计划（一次改完，单 PR 完整交付）

按依赖顺序，但**同一 PR 一次合入**(非分期)：

1. **SearXNG provider** — `web_mcp.py` 加 `_search_searxng`，`_web_search` auto 分支优先 SearXNG；
   `SEARXNG_URL` 平台配置 + Railway 部署 SearXNG 实例。红测：无 key 时走 SearXNG、实例挂走 DDG。
2. **凭据归位** — Exa/Tavily key per-agent config → tenant 级加密配置；`search.py` 移除 password 字段；
   迁移现存 per-agent key(backfill)。红测：tenant key 读取 + 隔离 + 不回显。
3. **CORE 增删** — `CORE_TOOL_NAMES` 删 `fs_read/fs_write/fs_list`、加 `web_search`/`read_document`；
   删 `fs_*` handler；核对 mode 映射无能力缺口。红测：文件能力无回退、web_search 常驻可见。
4. **pack 处置** — 删 `coordination_pack`/`plan_mode_pack`；`web_pack`→`web_provider_pack`；`office_pack` 提纯。
   扫 `pack_policy_service`/`capability_gate`/`skill_seeder`/`deep_research` 引用，清孤儿映射。
5. **职责统一** — `activation_mode` 枚举化、`infer_from_tools` 规则化、CORE 工具排除出 tool_search 发现。
6. **文档归位** — 修 `CLAUDE.md`(packs.py→runtime_tool_groups.py、工具/pack 计数)；本文档转交付记录。

---

## 8. 待 owner 拍板的决策点

1. **pack 目标形态**：确认"提纯保留"(本稿推荐)而非"激进消减向 CC 扁平看齐"？
   理由：Hive 多租户凭据隔离/治理需要 pack 这层，CC 无多租户故无需。
2. **SearXNG 部署**：平台是否部署一个共享 SearXNG 实例作 web_search 默认兜底？(决定裸租户搜索/DR 体验底线)
3. **凭据粒度**：Exa/Tavily key 归 **tenant 级**(本稿推荐，一租户配一次全员受益)还是保留 per-agent 可选覆盖？
4. **fs_* 收敛方向**：确认删 fs_*/留细分版(对齐 CC)，而非反向(留合并版省 schema)？

---

## 9. 风险

- **DR 耦合**：`web_search` 被 `deep_research/leaf_presets.py`、`routing_reminder.py` 引用；改 pack 归属/凭据
  来源须验 DR 工具激活链不破。
- **SearXNG 可靠性**：自托管实例需监控；实例挂时 DDG 兜底链必须验证生效(fail-soft 不可静默全断)。
- **凭据迁移**：per-agent→tenant 迁移是数据迁移，按"交付纪律"唯一例外用 dry-run + 确认门(非 MVP 分期)。
- **回归面**：pack 重命名/删除波及 `pack_policy_service`/`capability_gate`/`skill_seeder`/前端 pack 展示，
  全量回归 + 前端 tool 管理面核对。
