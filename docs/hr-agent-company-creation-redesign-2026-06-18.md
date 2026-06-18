# HR Agent 公司级创建流整改方案（2026-06-18）

> 状态：下一轮代码整改前的设计文档。本文只定义目标、边界、文件去留、实现路径和验收口径，不表示代码已经完成。
>
> 核心纠偏：HR Agent **不判断要不要创建员工**。用户进入 HR 创建入口时，创建意图已经成立。HR 的唯一工作是把这个意图塑造成更符合这家公司、更像真实职场人的数字员工。

## 0. 结论

本轮整改应把 HR 创建体系收敛成：

```text
公司级 HR Agent
  + 一个 canonical create_employee skill
  + 极薄 soul.md 身份锚点
  + 后端 preview / confirmation / hash / session gate
  + 公司级 Agent DNA 记忆沉淀
```

需要明确删除的错误方向：

1. HR 不做“是否应该创建员工”的判断。
2. HR 不做招聘审批、HC 管理、组织规划或员工编制建议。
3. HR 不把“是否扩展现有 agent”作为创建流前置决策。
4. HR 不把流程本身当成价值；流程只服务于更好地创建员工。

HR 的目标是：

1. 接收用户已经表达的创建意图。
2. 用尽可能少的追问补齐必要信息。
3. 把模糊需求转成一个像真实公司成员的数字员工：精英实习生、职场 partner、职场搭档。
4. 让这个员工第一天就有清晰任务、质量标准、边界、汇报方式和可进化起点。
5. 从本公司过往创建语料中学习公司偏好，让后续新员工越来越像这家公司的人。

## 1. 北极星对齐

Hive 的北极星不是“后台配置生成器”，而是：

1. 最强数字员工：单个 agent 有智能、记忆、自进化、技能、可靠性和安全边界。
2. 公司级控制中台：在企业规模下运营这些 agent，覆盖权限、预算、治理、协作和观测。

HR Agent 属于这两个目标的入口层。它创建出来的不是工具壳，而是公司级数字员工。

因此 HR 创建质量的判断标准不是“字段是否填齐”，而是：

- 新 agent 是否有清晰的职场身份。
- 新 agent 是否知道服务谁、产出什么、第一天做什么。
- 新 agent 是否像一个高潜实习生或靠谱职场搭档，而不是泛用聊天助手。
- 新 agent 是否有能进入记忆和自进化循环的初始 DNA。
- 新 agent 是否遵守公司、owner、权限、外部可见行为的边界。

## 2. 当前问题

当前 HR 模板中存在三类重复：

1. `soul.md` 写了较完整的创建流程。
2. `skills/hr-guide/SKILL.md` 写了一套招聘流程。
3. `skills/create-employee/SKILL.md` 又写了一套创建流程。

这些内容虽然已被临时改成一致，但结构上仍然有问题：

- 两个 skill 都在讲创建，模型可能不知道哪个是主流程。
- `soul.md` 太像 SOP，长期会和 skill 漂移。
- `hr-guide` 容易把 HR 拉向“判断是否创建 / 是否扩展现有员工”的方向。
- 真正重要的目标，即“创建符合公司语境的真实职场搭档”，没有成为唯一中心。

## 3. 目标职责边界

### 3.1 HR Agent 是什么

HR Agent 是每家公司一个的数字员工创建搭档。

它是公司级共享 agent，不是某个员工私有的创建助手。公司内所有用户创建新数字员工时，都应进入同一个 HR Agent，使 HR 能沉淀本公司的创建语料、默认风格和隐性偏好。

如果当前系统中 `tenant == company`，则实现上继续保持每个 tenant 一个 `__system_hr__`。这不是平台全局共享，也不能跨 tenant 学习。

### 3.2 HR Agent 不是什么

HR Agent 不是：

- 招聘审批人。
- 组织设计顾问。
- 编制/预算裁决器。
- “是否应该创建这个员工”的判断器。
- 防止用户创建员工的 gatekeeper。

用户进入创建入口后，HR 默认目标就是完成创建。HR 可以补齐信息、提出默认值、提示边界、展示 setup debt，但不应把“是否创建”变成问题。

### 3.3 HR Agent 的唯一任务

HR 的唯一任务是：

```text
把用户的创建意图，转成一个符合本公司语境、第一天可工作、后续可自进化的数字员工。
```

## 4. 文件去留判断

### 4.1 保留：`backend/hr_agent_template/soul.md`

`soul.md` 保留，但必须缩薄。

它只写身份和不可绕过的边界：

- 你是公司级 Digital Employee Creation Partner。
- 你不判断是否创建；用户来到这里就是要创建。
- 你创建的是精英实习生 / 职场 partner / 职场搭档，不是工具壳。
- 你必须利用公司级创建记忆，让新员工符合这家公司。
- 你必须走 `preview_agent_blueprint` 和 `create_digital_employee` 的后端确认链路。
- 你不能跨公司复用或泄露创建偏好。

它不再重复完整 SOP、字段表、示例流和 skill routing 细节。

### 4.2 保留并扩展：`backend/hr_agent_template/skills/create-employee/SKILL.md`

`create_employee` 成为唯一 canonical HR 创建 skill。

它负责完整 SOP：

- 创建目标：塑造真实职场搭档。
- 动态轮次：只追问缺失信息。
- 创建门禁：Identity / Work Contract / Governance / Capability & Setup / Preview & Confirmation。
- 公司适配：读取和应用公司 Agent DNA。
- blueprint preview。
- explicit confirmation。
- `confirmed_blueprint_hash` 创建。
- 创建后把本次创建案例写入可治理记忆候选。

### 4.3 退役：`backend/hr_agent_template/skills/hr-guide/SKILL.md`

`hr-guide` 应退役。

退役原因：

- 它和 `create_employee` 重复。
- 它容易把 HR 变成“是否创建”的判断器。
- 它让 skill catalog 出现两个创建入口，增加模型选择成本。

迁移方式：

1. 把仍然有价值的内容并入 `create_employee`。
2. 模板同步时删除或隔离旧 `skills/hr-guide/`。
3. 对已有 HR workspace 做可逆清理：移动到归档目录，而不是直接硬删。

建议归档路径：

```text
skills/.retired/hr-guide/<timestamp>/SKILL.md
```

## 5. 新创建流程

### 5.1 流程总览

```text
User wants to create a digital employee
  -> HR accepts creation as the goal
  -> Load company Agent DNA
  -> Clarify only missing creation material
  -> Build workplace-partner blueprint
  -> Preview with gates + setup debt
  -> User confirms
  -> create_digital_employee with confirmed_blueprint_hash
  -> Persist creation case evidence for HR learning
```

### 5.2 不是“五轮”，而是动态补齐

轮次不是目标。

正确规则：

- 如果用户已经给足信息，一轮即可 preview。
- 如果缺少关键信息，只问缺失部分。
- 如果用户让 HR 决定，HR 应结合公司 Agent DNA 和合理默认值直接提出方案。
- 不因为固定流程而拖慢创建。

### 5.3 门禁改名：从流程门禁到员工质量门禁

现有门禁可继续保留，但表达应更贴近“真实员工质量”：

| 门禁 | 目的 |
|---|---|
| Identity Gate | 这个员工是谁，服务谁，产出什么 |
| Work Contract Gate | 第一件工作、成功证据、汇报方式、工作节奏 |
| Governance Gate | 不能做什么，哪些行为需要确认，外部可见边界 |
| Capability & Setup Gate | day-one 能力、默认能力、必须安装的能力、setup debt |
| Preview & Confirmation Gate | 用户确认的是当前 blueprint，而不是模糊口头描述 |

注意：这些门禁不是用来判断“是否创建”，而是保证创建出来的员工质量足够高。

## 6. 公司 Agent DNA

### 6.1 目的

公司 Agent DNA 是 HR 从本公司所有创建 session 中沉淀出来的创建偏好。

它回答：

- 这家公司偏好什么样的数字员工命名。
- 常见岗位类型是什么。
- 默认工作质量标准是什么。
- 默认边界是什么。
- 常见第一任务是什么形态。
- 哪些集成通常 day-one 必须具备。
- 哪些能力通常先不装，等 agent 自己在工作中进化。
- 这家公司认为“靠谱员工”的隐含标准是什么。

它不回答：

- 要不要创建这个员工。
- 公司应该招什么岗位。
- 用户的需求是否合理。

### 6.2 写入来源

只从创建相关证据中学习：

- HR 与用户的创建对话。
- `preview_agent_blueprint` 的参数和结果。
- `create_digital_employee` 的成功结果。
- 创建后初始 `soul.md`、first tasks、triggers、setup debt。
- 用户对创建结果的明确反馈。

不能从无关聊天、其他 agent 私有工作内容或跨 tenant 数据中学习。

### 6.3 记忆边界

公司 Agent DNA 必须走现有 Memory Governance Layer：

```text
LLM 负责判断、提炼、归纳、候选生成；
平台负责 source_refs、权限、去重、审计、回滚、最终落盘。
```

实现上不应新增一套平行记忆系统。

目标形态可以是 HR agent 自己的公司级记忆区中的一个稳定语义面，例如：

```text
memory/t3/company_agent_dna.md
```

如果当前 memory clean-loop 已收敛为固定 T3 文件集合，则 `company_agent_dna` 应作为 HR agent 的 T3 accepted section，而不是硬塞一套新目录。最终代码实现要服从当时的 canonical memory layout。

### 6.4 激活规则

HR 创建新员工时，应把公司 Agent DNA 作为创建上下文激活。

激活内容应包括：

- 最近稳定的公司创建偏好。
- 与当前角色类型相关的过往创建模式。
- 常见边界和质量标准。
- 常见 setup debt。

不应激活：

- 其他 tenant 的创建偏好。
- 用户无权访问的私有信息。
- 其他 agent 工作过程中的敏感内容。

## 7. `create_employee` skill 目标结构

建议重写后的 skill 结构：

```text
---
name: create_employee
description: Create a company-fit digital employee as a day-one-useful workplace partner.
tools:
  - preview_agent_blueprint
  - create_digital_employee
  - web_search
  - web_fetch
  - firecrawl_fetch
  - discover_resources
  - search_clawhub
---

# Create Digital Employee

1. Role
2. Core Principle: user already wants creation
3. Company Agent DNA usage
4. Dynamic clarification
5. Workplace Partner Blueprint
6. Gates
7. Capability routing
8. Preview
9. Confirmed create
10. Creation case learning
11. Anti-patterns
12. Examples
```

必须明确写入的反模式：

- 不问“你确定要创建吗”作为流程判断。
- 不把“是否扩展现有 agent”作为前置分支。
- 不把创建降级为配置字段收集。
- 不创建泛用助手。
- 不用固定轮次拖慢用户。
- 不前置安装一堆不必要能力。
- 不隐藏 setup debt。

## 8. 后端硬约束保留

Prompt 不是最终边界，后端硬约束仍必须保留：

1. `preview_agent_blueprint` 必须生成 `blueprint_hash`。
2. `create_digital_employee` 必须要求 `confirmed_blueprint_hash`。
3. 创建必须能在同一 session 找到匹配 preview。
4. 高风险或外部可见角色必须有边界。
5. 重复创建失败要停止重试并报告日志/config 问题。

这些约束不是 HR 判断“是否创建”，而是防止创建动作失真、绕过确认或产生低质量员工。

## 9. 实现计划

### 9.1 文档与模板

1. 新建或更新 prompt contract 测试，锁定：
   - `hr-guide` 不再存在于模板。
   - `create_employee` 是唯一创建 skill。
   - `soul.md` 不再复述完整 SOP。
   - `soul.md` 明确 HR 不判断是否创建。
   - `create_employee` 明确创建目标是 workplace partner / elite intern。
2. 重写 `backend/hr_agent_template/soul.md`。
3. 合并 `hr-guide` 内容到 `create-employee/SKILL.md`，删除错误的判断职责。
4. 模板同步逻辑增加 retired skill cleanup。

### 9.2 HR Agent 公司级语义

1. 保持每 tenant 一个 `__system_hr__`。
2. 明确 HR 是 company-scoped system agent，不是 first requester 的个人 agent。
3. 如果 DB 仍需要 `creator_id/sponsor_user_id`，将其视为 bootstrap 记录，不进入 HR identity prompt。
4. 保持 `AgentPermission(scope_type="company", access_level="use")`。

### 9.3 创建案例学习

1. 在成功创建后生成 creation case evidence。
2. evidence 必须包含 source refs：session、preview tool call、create tool call、agent id。
3. 由 LLM 生成公司 Agent DNA 候选。
4. 由平台 gate 做权限、去重、审计、落盘。
5. 下次 HR 创建时激活相关公司 Agent DNA。

## 10. 验收测试

### 10.1 Prompt / template tests

必须覆盖：

- `backend/hr_agent_template/skills/hr-guide/` 被退役。
- `create_employee` 是唯一 HR 创建 skill。
- `soul.md` 包含 “does not decide whether to create” 等价语义。
- `soul.md` 不包含完整字段清单、长 SOP、固定轮次。
- `create_employee` 包含 “workplace partner / elite intern / day-one useful” 等价语义。
- `create_employee` 不包含“ask whether to create / extend existing agent instead” 这类前置判断。

### 10.2 Runtime tests

必须覆盖：

- GET `/agents/system/hr` 会同步新版模板。
- 旧 workspace 中的 `skills/hr-guide` 会被归档或移除。
- 旧 workspace 不会继续暴露两个创建 skill。
- `create_digital_employee` 仍要求同 session preview hash。
- HR 创建失败预算仍有效。

### 10.3 Memory tests

必须覆盖：

- 成功创建后产生 creation case evidence 或 memory candidate。
- candidate 带 source refs。
- candidate tenant-scoped。
- HR 下一次创建能激活同 tenant 的公司 Agent DNA。
- HR 不激活其他 tenant 的创建偏好。

## 11. 非目标

本轮不做：

- 平台全局 HR Agent。
- 跨 tenant 创建偏好共享。
- 招聘审批流。
- 预算/编制判断。
- 组织架构规划。
- 让 HR 决定是否应该创建员工。
- 把公司 Agent DNA 做成新的平行记忆系统。

## 12. 最终判断

这次整改的核心不是“再加流程”，而是把 HR 拉回产品目标：

```text
用户已经要创建员工；
HR 负责把这个员工塑造成符合公司语境、第一天能工作、以后能自进化的职场搭档。
```

因此：

- `HR Guide` 和 `Create Employee` 应合并。
- 只保留一个 `create_employee` skill。
- `soul.md` 缩成身份和边界。
- 公司级学习服务于“创建得更像这家公司”，不服务于“阻止创建”。
- 后端 gate 只保证创建一致性、安全性和审计性，不替代 HR 的塑形智能。
