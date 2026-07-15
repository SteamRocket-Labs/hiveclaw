# 前端整体改造：诊断与方案（2026-07-03）

对照目标：**Codex Desktop**。核心要求：**克制但是精致**。
本文档是这轮改造的唯一设计权威，取代 CLAUDE.md 中引用但已不存在的 `.impeccable.md`。

---

## 一、诊断：现在到底是什么问题

"粗糙感"不是主观印象，它可以量化。以下全部来自 2026-07-03 对 `frontend/src` 的实测：

| 指标 | 实测值 | 精致系统的参考值 | 说明 |
|---|---|---|---|
| `style={{}}` inline style 总数 | **2,448 处** | <150（仅真动态值） | AgentChatSection 一个文件 172 处 |
| TSX 内 inline `fontSize` | **1,100+ 处、18 种取值** | 0（全走 class/token） | `12px`×393、`11px`×298、`13px`×237… |
| TSX 内 inline `padding` 取值种类 | **113 种** | 0 | 每一处都是手调 |
| CSS font-size 取值种类 | **30 种** | ≤7 | 含 `12.5px`/`10.5px`/`14.5px`/`13.5px` 半像素手调 |
| CSS border-radius 取值种类 | **20 种** | ≤5 | 含 `7px`/`9px`/`5px`/`3px` 等奇数手调 |
| CSS 硬编码 hex 色 | **60 种**（token 之外） | ~0 | 游离在色板外 |
| box-shadow 取值种类 | **23 种** | ≤3 | Codex 面内元素零阴影 |
| 原子组件（Button/Card/Input…） | **0 个** | ~12 个 | `components/` 16 个文件全是业务组件 |
| light theme 定义 | **~47 行覆盖** | 与 dark 等量 | dark 有 ~200 行 token，light 只薄薄一层 |
| 最大组件文件 | **4,530 行**（AgentChatSection） | <800 | 样式结构混写，不可维护 |

关键事实：**token 系统本身是存在的、而且不差**（`:root` 有完整的 spacing/type/radius/shadow 定义，07-02 还建了 `--session-tui-*` 密度 token）——但它被 2,448 处 inline style 和大量裸值**架空**了。问题不在设计系统缺失，在**设计系统没有执行结构**。

---

## 二、根因：粗糙感从什么地方来

### 直接来源（四层，从最伤到次伤）

**1. inline style 的结构性后果 —— 交互反馈系统性缺失。**
inline style 写不了 `:hover` / `:focus-visible` / `:active` / transition / 媒体查询。2,448 处 inline 意味着大面积可点元素**没有任何指针反馈**。"精致"的一半是微交互——鼠标划过有回应、聚焦有环、按下有确认。这层不是"值没调好"，是**根本写不出来**。

**2. 值域失控 —— 熵就是粗糙感的数学形式。**
30 种字号、113 种 padding、20 种圆角、60 种散装色。同类元素在不同页面长得都不一样（同是"卡片"，A 页 `radius 8 / padding 12`，B 页 `radius 10 / padding 14 16`）。人眼未必说得出哪里不对，但会持续感到"不齐"。半像素字号（12.5px）和奇数圆角（7px、9px）是逐处手调的铁证。

**3. 卡片化思维 —— 视觉重量堆积。**
每个信息单元都套一个 border + radius + background 的盒子，盒子里再套盒子。Codex Desktop 的克制 = **用间距和排版分隔，边框是最后手段**；转录读起来像一篇文档，不像卡片瀑布。Hive 目前每条消息、每个交付物、每个设置项都是一张卡。

**4. 排版微观层无人管。**
中英混排：正文字体 Plus Jakarta Sans 不含 CJK，中文回落系统字体 → 拉丁与中文的字重、基线、视觉字号不匹配（截图里大量中文，这是隐蔽但持续的粗糙源）。此外：uppercase 小标签 `letter-spacing: 0`（应 +0.03em 左右）、数字未用 tabular-nums、行高不成体系。

### 为什么会变成这样（病根）

**病根 1：增量生成 + 无执行结构。** 每轮功能开发"就地写 inline style"是最快路径，没有任何机制让"走 token/组件"成为更便宜的路。2,448 处不是某人的失误，是几十轮增量开发的自然沉积——**和后端记忆脏是同一种病：捷径累积，"later" = never**。

**病根 2：设计权威三重漂移。** CLAUDE.md 引用的 `.impeccable.md` **不存在**；CLAUDE.md 说 Inter，实际是 Plus Jakarta Sans；`index.css` 头注释说 "Linear-inspired"，6-28 计划说 "Codex-like"。没有一份"什么是对的"的可执行基线，每轮生成靠模型默认审美 = AI slop 平均值。

**病根 3：对齐做的是"功能对齐"，不是"形态对齐"。** 765b498f 的 12 项是行为/信息架构层（折叠、流式、chips），验收是测试绿。6-28 计划 §2.4 其实**已经把视觉诊断写对了**（"根因不是某一个按钮颜色，而是没有统一 density"、"不用随机 inline style"），但它只在 Session 区局部执行，从未有一轮**全站的形态 pass**。测试测不出丑，所以丑从来不算验收失败。

---

## 三、设计宪法：Codex Desktop 逆向规范

以下从 Codex Desktop 截图逆向提取 + codex-rs TUI 克制原则归纳，是全站唯一基线。§2.4 的 Session TUI 基线并入本节，升格为全站规范。

### 3.1 六条原则

1. **文档感，不是卡片感。** 分隔手段优先级：间距 → 微弱背景差 → hairline 分隔线 → 边框（最后手段）。assistant 消息不套容器，直接排版。
2. **窄字号谱系。** 11/12/13px 三档承担 90% 的界面；15px 只给页面标题。禁止半像素字号。
3. **灰阶为主，色彩 = 状态。** 单一强调色；状态色只上小点、小字、hairline，永不上大面积底色。
4. **每个可点元素三态齐全。** hover（背景提一档）、focus-visible（2px ring）、active（再提一档），transition 120–160ms。这是"精致"的及格线。
5. **密度高、行高松。** 信息密、留白准：行内紧（gap 4–6px）、块间明（12–16px）、正文行高 1.55–1.65。
6. **阴影只属于浮层。** menu/popover/modal 有阴影；一切面内元素零阴影。

### 3.2 Token 收缩表（改造后 tokens.css 的全部值域）

**字号（7 档封顶）**

| token | 值 | 行高 | 用途 |
|---|---|---|---|
| `--text-tiny` | 10px | 1.2 | 极短 counter/badge，禁长句 |
| `--text-meta` | 11px | 1.35 | 时间戳、来源、辅助说明 |
| `--text-row` | 12px | 1.45 | 表行、文件行、次级标签 |
| `--text-body` | 13px | 1.6 | 正文、消息、输入框（全站主字号） |
| `--text-emphasis` | 14px | 1.5 | 少量强调（表单 label、组标题） |
| `--text-title` | 15px | 1.35 | 页面/面板标题，禁 hero 化 |
| `--text-display` | 20px | 1.3 | 仅空态/引导页大字，全站 ≤3 处 |

**间距**：保留 4px 基（4/8/12/16/20/24/32），删除 40/48 的日常使用。
**圆角（4 档）**：`--radius-sm: 4px`（chip/badge）、`--radius-md: 6px`（按钮/输入/行）、`--radius-lg: 8px`（面板/卡/代码块）、`--radius-full`。**删除 10/12/14px 档**——大圆角是"AI 味"的主要来源之一。
**阴影（3 档）**：`--shadow-popover`、`--shadow-modal`、`--shadow-focus-ring`。其余全删。
**色板**：现有灰阶体系保留骨架；60 个散装 hex 全部归位到语义 token 或删除；强调色收敛为一个（当前 accent 即灰阶高亮 + `--info` 蓝作链接/选中，二选一定稿）；状态色四支（success/warning/error/muted）只允许出现在 ≤12px 的元素上。

**字体（需拍板，见 §6）**：推荐全平台中性系统栈，中英文浑然一体：
`-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif`；等宽保留 JetBrains Mono。

### 3.3 组件形态规范（原子层的验收标准）

- **按钮**：默认幽灵/文字级（Codex 的"撤销/审核"是文字按钮）；主按钮 = 高对比实底 + `--radius-md`，高度 28px（紧凑）/32px（常规）两档。
- **消息**：user = 右侧浅灰圆角泡（`--radius-lg`，不用彩色底）；assistant = 无容器直排 markdown；工具行 = 单行暗淡（`--text-row` + `--text-tertiary`），完成后视觉重量趋近于零。
- **代码/exec 块**：低对比底 + 语法着色 + `--radius-lg`，无边框；行内 `# 结果` 注释风格。
- **diff/文件编辑卡**：一张聚合卡（文件列表 + `+add/−del` 徽章 + 文字级操作），不是每文件一张大卡。
- **表格/列表行**：无边框，hover 背景一档，selected = 灰底 + 左侧 2px hairline。
- **chips**：`--text-meta`、`--radius-sm`、1px border 或纯背景差，禁彩色底。
- **空态**：图标 + 一句话 + 一个动作，居中，不做插画堆砌。

---

## 四、改造方案：怎么改

### 4.0 策略决策

- **CSS 架构：维持 vanilla CSS custom properties，不引入 Tailwind。** 理由：token 体系已存在，问题在执行；引入 utility 框架 = 全量类名重写 + 构建链变动，风险大而收益可由"原子组件 + token 封闭值域"覆盖。对 AI 增量生成场景，**原子组件的 props 是封闭集合，比 utility class 更强的约束**——这是治病根 1 的结构性手段（让正确的路成为便宜的路）。
- **改造单位：按 surface 纵切，每个 surface 一次改完（布局+组件迁移+双主题+截图验收）。** 不按问题类型横切（"先全站换字号"会让所有页面同时半残）。符合"一次改完、零技术债"纪律：分 phase 是范围管理，每 phase 内部无 MVP。
- **inline style 处置标准**：静态样式 100% 迁 class；真动态值（进度百分比、拖拽定位、动态色）保留 inline 或走 CSS 变量注入。全站目标 2,448 → **<150**。
- **CSS 文件结构**：`index.css`（5,783 行巨石）拆为 `styles/tokens.css` + `styles/base.css` + `styles/components.css` + 每 surface 一个文件；迁移完成后删除死规则。

### 4.1 Phase 划分

**P0 — 地基：设计宪法生效 + token 收缩 + 原子组件库**（先行，一个 session 内与 P1 一起完成）
- 本文档 §3 定稿 → 重构 `styles/tokens.css`（收缩值域、light theme 与 dark 等量补全、中英混排字体栈）。
- 建 ~12 个原子组件：`Button` `IconButton` `Card` `Chip` `Badge` `Input` `Select` `Modal`（收编现有 ConfirmModal/PromptModal）`EmptyState` `Spinner` `Tooltip` `SegmentedControl`——全部 class-based、三态齐全、双主题。
- 更新根 CLAUDE.md 设计段指向本文档（消灭 `.impeccable.md` 幽灵引用）。
- **验收**：原子组件 gallery 页截图（light+dark 双主题、全部三态）。

**P1 — App Shell**：侧边栏 + 顶栏 + 导航 + 全局滚动条/焦点样式。全站每一屏都在看的部分，性价比最高。
- **验收**：shell 双主题截图对照 Codex Desktop 左栏。

**P2 — Session/Chat surface（脸面，对照 Codex 最直接）**
- 前置：AgentChatSection 4,530 行**先拆后改**——拆成 ~8 个子组件文件（纯移动零行为变化，436 测试保护下做），否则每轮改造都在巨石里泅渡。
- 转录去卡片化（assistant 直排、user 灰泡）、工具行弱化、exec 块语法着色形态、diff/交付物聚合卡（path 聚合已有，形态收敛为 Codex 式紧凑卡）、流内标记（压缩分隔线、停止标记）、composer 收敛。
- **独立子项（涉及后端）**：转录编排粒度——narration 与工具行交替，而非全部步骤打包进一个"已处理"大块。这需要后端事件流配合，P2 先做纯前端可达的部分（渲染层按事件顺序交替展示已有数据），事件流增强单独立项。
- **验收**：同一 session 的 Hive 截图 vs Codex Desktop 截图并排对照。

**P3 — 高频页面**：Dashboard、Agent 列表、AgentDetail 各 section（Settings/Skills/Evolution/Workflows/Status）。
**P4 — 长尾页面**：Plaza、Workspace 系列、Admin、EnterpriseSettings、LocalAgents、登录/引导。
**P5 — 全局收口**：量化指标核查 + 双主题全站截图集 + `index.css` 死规则清理 + 根 CLAUDE.md/文档同步。

### 4.2 验收制度（本轮的关键变化）

吸取"文档全量承诺 → 实施子集 → 表层测试绿 → 宣称完成"的教训，**本轮验收以截图为第一验收物，量化指标为硬门槛**：

| 验收物 | 标准 |
|---|---|
| 截图对照 | 每 surface：本地实跑 → light+dark 双主题截图 → 与 Codex Desktop 并排，owner 过目拍板 |
| inline style 计数 | 每 surface 完成后 grep 计数；全站终值 <150 |
| 值域熵 | font-size 种类 30→≤7；radius 20→≤4；面内 box-shadow → 0；散装 hex → 0 |
| 三态覆盖 | 抽查可点元素：hover/focus-visible/active 齐全 |
| 测试 | 436+ 全绿维持（结构变化随改随修，不留最后一起爆） |

### 4.3 规模与风险（诚实评估）

- **总量约 4–6 个 session**：P0+P1 一个；P2 一个；P3 一到两个；P4 一到两个；P5 半个。
- **风险 1**：AgentDetailSections.test.tsx（5,505 行）等测试对 DOM 结构敏感，P2/P3 会大面积碰红——对策：拆分与样式迁移分两个 commit，测试随改随修。
- **风险 2**：light theme 补全是隐藏工作量（当前仅 47 行覆盖）——已计入 P0。
- **风险 3**：改造期间与其他 session 并行改前端文件冲突——沿用 hunk 级 staging 纪律，surface 边界即文件边界，降低碰撞面。

---

## 五、与既有工作的关系

- 6-28 计划 §2.4 的 Session TUI 基线**并入本文档 §3 升格为全站规范**；`--session-tui-*` token 在 P0 与全局 token 合并去重。
- 765b498f 的 12 项微交互（折叠、流式、chips、shimmer）全部保留，P2 在其上做形态层收敛。
- 转录"叙事编排粒度"（后端事件流）不属于本轮 CSS/组件改造，独立立项，避免范围失控。

## 六、需要 owner 拍板的三件事

1. **字体**：换全平台中性系统栈（推荐——中英混排浑然一体、Codex 同款观感、零加载成本）还是保留 Plus Jakarta Sans（品牌个性，但 CJK 混排基线不齐）？
2. **强调色**：灰阶高亮为主（现状、更像 Linear）还是启用蓝色作链接/选中强调（更像 Codex Desktop）？
3. **顺序确认**：P0→P1→P2→P3→P4→P5，P2（Session 脸面）之前必须先过 P0/P1 地基——接受这个顺序，还是要求 Session 区最先见效（可行，但 Session 区会先按旧 token 改一遍再随 P0 收敛，产生一次返工）？
