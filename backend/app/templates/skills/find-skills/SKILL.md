---
name: Find Skills
description: 帮助发现和安装新技能。当用户问"怎么做X"、"有没有关于X的技能"时自动触发。搜索 → 排序 → 安全审查 → 安装。
tools:
  - execute_code
  - web_search
  - web_fetch
  - firecrawl_fetch
is_system: false
is_default: true
---

# Find Skills

帮助用户发现和安装来自开放技能生态的 skill。

**铁律：搜索 → 按排名选择 → 安全审查 → 用户确认 → 安装。绝不跳过安全审查。**

## 何时使用

当用户：
- 问"怎么做 X"，且 X 可能有现成的 skill
- 说"找一个关于 X 的技能"
- 问"你能做 X 吗"，且 X 是你当前不具备的专业能力
- 想扩展你的能力范围

---

## 第一步：搜索

### 方式一：Skills CLI（推荐）

通过 `execute_code` 工具运行：

```bash
npx skills find [关键词]
```

搜索结果按安装量降序排列，直接能看到排名。

### 方式二：网页搜索

用 `web_search` 搜索 `site:skills.sh <关键词>`，找到候选页面后用 `web_fetch` 读取 skills.sh 排行榜或 skill 详情页。

---

## 第二步：排名优先选择

**严格按安装量排序，优先推荐排名靠前的。**

| 安装量 | 可信度 | 操作 |
|--------|--------|------|
| 50K+ | 高 | 推荐，仍需审查 |
| 10K-50K | 中 | 推荐，仔细审查 |
| 1K-10K | 低 | 提醒用户安装量较低 |
| <1K | 极低 | **不推荐**，除非用户明确要求 |

**可信来源白名单**（这些来源的 skill 优先推荐）：
- `vercel-labs` / `anthropics` / `microsoft` / `google-labs-code`
- `ComposioHQ` / `stripe` / `supabase`

---

## 第三步：安全审查（必须）

**安装任何 skill 之前，必须执行安全审查。** 使用 `web_fetch` 读取 skill 的源码（GitHub SKILL.md）；如果页面抓取不完整，再升级到 `firecrawl_fetch`。按以下清单检查：

### 3.1 来源验证
- [ ] 作者是否为已知可信组织或有大量 follower
- [ ] 仓库 star 数 > 100
- [ ] 最近更新时间 < 6 个月

### 3.2 代码检查 — 红旗

以下任何一项出现则立即拒绝安装并警告用户：

| 红旗 | 示例 |
|------|------|
| 窃取凭证/API Key | 读取 ~/.ssh、读取 .env、获取 API key |
| 向外部发送数据 | curl/fetch 到未知 URL、数据外传 |
| 修改系统文件 | 写入 workspace 之外的路径 |
| 混淆代码 | base64 解码、动态执行代码 |
| 要求 sudo/root | 任何提权操作 |
| 修改 agent 核心文件 | 写入 soul.md、修改 memory |

### 3.3 权限分析
- 需要哪些文件访问？是否限于 workspace 内？
- 需要哪些网络请求？目标 URL 是否合理？
- 需要哪些工具？是否超出必要范围？

### 3.4 风险评级

| 等级 | 条件 | 操作 |
|------|------|------|
| LOW | 只读操作、无网络、可信来源 | 告知用户，可安装 |
| MEDIUM | 有文件写入但限于 workspace、可信来源 | 告知用户风险，建议安装 |
| HIGH | 有网络调用或不太知名的来源 | 明确警告，让用户决定 |
| EXTREME | 命中任何红旗 | **拒绝安装**，解释原因 |

---

## 第四步：向用户展示

审查通过后，向用户展示：

```
我找到了一个可能有用的技能！

**react-best-practices** — React 和 Next.js 性能优化指南
📦 185K 安装量 · 来源：vercel-labs（可信）
🛡️ 安全评级：LOW（只读指南，无代码执行）

安装命令：
npx skills add vercel-labs/agent-skills@react-best-practices -y

要我帮你安装吗？
```

**必须包含：** 安装量、来源、安全评级。用户确认后才安装。

---

## 第五步：安装

用户确认后，通过 `execute_code` 安装：

```bash
npx skills add <owner/repo@skill> -y
```

安装后技能文件写入 skills/ 目录，`load_skill` 即可使用。

---

## 常见技能分类

| 分类 | 搜索关键词 |
|------|-----------|
| Web 开发 | react, nextjs, typescript, css, tailwind |
| 测试 | testing, jest, playwright, e2e |
| DevOps | deploy, docker, kubernetes, ci-cd |
| 文档 | docs, readme, changelog, api-docs |
| 代码质量 | review, lint, refactor, best-practices |
| 设计 | ui, ux, design-system, accessibility |
| 效率 | workflow, automation, git |

## 找不到时

1. 告知用户没有找到匹配的 skill
2. 主动提出用你现有能力直接帮助
3. 建议用户可以自行创建 skill：`npx skills init my-skill`
