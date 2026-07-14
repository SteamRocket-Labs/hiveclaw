你现在需要对当前代码仓库执行一次独立、完整、证据驱动的原子化架构审查。

这是一项全新审查。不要假设项目以前做过类似检查，不要依赖任何历史结论，也不要默认现有文档、测试、接口或页面是正确的。你必须从当前源码、数据库结构、迁移、运行时路径、测试和实际 UI 消费路径出发，独立得出结论。

本次任务以审查和报告为主。除非我另行明确授权，否则不要修改业务代码、数据库、部署配置或现有文档。

如果需要输出报告，请新建一份独立报告，不要覆盖或修改已有报告。文件名可以根据当前执行环境自动生成，但不得依赖提示词中的固定日期、固定轮次或固定编号。

# 一、最终目标

从以下四个方面，对整个项目进行端到端原子化扫描：

1. 单 Agent 核心架构
2. Hive Native 能力
3. 企业治理、安全与 AI 资产管理
4. 用户真实使用体验与 UI/UX

最终需要回答：

- 当前系统真正实现了什么？
- 哪些能力只是“有代码”“有 API”“有表”“有页面”，但没有形成生产闭环？
- 哪些地方存在双事实源、旁路、状态漂移、恢复缺失、消费断裂或 UI 断点？
- 哪些治理、安全、RLS、权限或审批限制可能导致 Agent 无法正常运行？
- 哪些复杂度是不必要的，可以依据第一性原理、奥卡姆剃刀和 KISS 原则删除或合并？
- 当前架构是否足够模块化、鲁棒、可维护，并能够支持云端 Agent 长期运行？
- 上线前还存在哪些必须解决的技术债务？

不要为了给出高分而弱化问题。只有证据覆盖充分时才允许给出高置信度结论。

# 二、原子化判定标准

不要把“存在 API”“存在数据库表”“存在组件”“存在测试文件”或“存在文档描述”视为完成。

每个能力必须按照以下七个原子进行检查：

1. 输入
   - 谁发起？
   - 输入结构和契约是什么？
   - 输入是否经过验证、规范化和持久化？
   - 断线或重启后能否恢复？
   - 模型是否被迫重复生成已经确认过的长参数？

2. 权威
   - 谁拥有读取权、决策权和写入权？
   - tenant、organization、user、owner、agent、sub-agent、service account 之间如何绑定？
   - RLS、RBAC、ABAC、代理身份和资源归属是否一致？
   - 是否存在绕过权限、跨租户访问或权限过度收紧的情况？

3. 执行
   - 唯一生产执行入口是什么？
   - API、Worker、工具、Workflow、定时任务和后台任务是否走同一套规则？
   - 是否存在旁路、重复执行器或部分路径绕过治理？
   - 执行状态机是否合法、明确、可终止？

4. 证据
   - event、span、transcript、run、turn、thread、tool call、数据库、对象存储和文件系统中，谁是机械事实源？
   - 是否存在多个事实源互相漂移？
   - 写入、产物、审批、失败、取消和重试是否都有可验证证据？
   - UI 展示的数据是否能追溯到真实证据？

5. 恢复
   - 断线、刷新、进程重启、Worker 重启、超时、重试、取消、回滚、fork、rewind 是否幂等？
   - 是否能够区分可重试错误和不可重试错误？
   - 是否存在重复执行、重复扣费、重复写入或孤儿任务？
   - 审批前后、工具执行前后和多 Agent 返回前后能否恢复？

6. 消费
   - Memory、Skill、Workflow、Knowledge、Artifact、Sub-agent、Agent Team 和 UI 是否真实消费了执行产物？
   - 文件生成后是否能被主 Agent、Workspace、附件系统和最终用户看到？
   - 后端已有的数据是否只是写入但从未消费？
   - UI 是否展示了用户不需要的内部实现数据？

7. 验收
   - 是否有覆盖真实生产路径的测试？
   - 是否包含迁移、回填、兼容、故障注入和恢复测试？
   - 是否有可观测性和告警？
   - 是否可以用明确证据复现、验证和关闭问题？

状态只能使用以下定义：

- 闭环：七个原子全部成立，而且存在当前生产消费路径。
- 局部闭环：主路径成立，但仍存在双事实源、旁路、恢复、权限或 UI 断点。
- 断点：能力存在，但生产路径在两个原子之间中断。
- 缺失：当前源码没有实现。
- 已知缺失：产品明确暂不建设，不得伪装为已完成或回归。
- 排除：上游产品或服务商的私有远程能力，不属于当前项目必须复制的能力。

每一个“闭环”结论也必须附带源码和测试证据，不能只为问题项提供证据。

# 三、审查原则

## 1. 源码优先

证据优先级如下：

1. 当前生产源码和真实调用路径
2. 数据库 schema、migration、RLS policy 和约束
3. API、Worker、队列、运行时及对象存储行为
4. 前端真实消费路径
5. 自动化测试和故障测试
6. 当前部署配置
7. 文档、注释、设计稿和命名

文档只能作为线索，不能作为实现证据。

如果文档与源码冲突，以源码和真实运行路径为准，并记录“文档漂移”。

## 2. 独立验证

不要沿用项目已有报告的结论。

可以搜索已有文档以发现设计意图，但必须重新验证每个结论。不能因为旧报告写着“已完成”，就把能力判定为闭环。

## 3. 端到端追踪

对重要能力必须沿真实路径追踪：

用户操作
→ 前端状态
→ API
→ 权限与治理
→ Agent Runtime
→ 模型调用
→ 工具或 Workflow
→ Worker/队列
→ 数据库/文件/对象存储
→ 证据登记
→ 主 Agent 消费
→ UI 最终呈现
→ 重试与恢复

不得只检查单一文件或单一服务。

## 4. 反向追踪

除正向路径外，还要从以下结果反查来源：

- UI 中的状态从哪里来？
- Workspace 中的文件从哪里来？
- 最终附件如何被登记和交付？
- Agent 的成功/失败由谁决定？
- Sub-agent 的结果如何返回父 Agent？
- 企业后台的数据如何从运行时产生？
- Memory、Knowledge 和 Skill 的内容在什么条件下进入模型？
- 审批结果如何恢复被阻塞的运行？

## 5. 不掩盖不确定性

找不到证据时，标记为“未证实”，不能推定已完成。

如果无法达到高置信度，必须说明缺少哪些证据、哪些环境不可用，以及如何补齐。

# 四、第一部分：单 Agent 核心架构

目标是判断单 Agent 是否达到成熟 Agent 产品应有的完整生命周期，并融合优秀的工程化和桌面端交互能力，同时适配云端环境。

## 1. 生命周期

逐项检查：

- thread/session 创建
- 用户消息进入
- turn 创建
- run 创建
- 上下文组装
- 模型请求
- streaming
- reasoning 和输出事件
- tool discovery
- tool call
- permission/approval
- tool execution
- tool result 回注
- 多轮循环
- final response
- artifact 交付
- usage/cost 记录
- compaction
- checkpoint
- branch
- rewind
- cancel
- retry
- resume
- reconnect
- timeout
- provider failure
- empty model response
- process restart
- Worker restart
- run terminalization
- cleanup

检查是否存在：

- 状态已经完成但 UI 仍显示运行中
- 工具成功但 Agent 判定失败
- 模型失败后没有用户可理解的最终响应
- 数据库状态、事件状态和 UI 状态不一致
- 重试导致工具重复执行
- 取消只取消前端，没有取消后端执行
- 重连后丢失中间状态
- final response 与 artifact 交付脱节
- 模型需要重复复述已经确认的结构化数据

## 2. 上下文组装

明确列出原始上下文中允许出现的内容，以及必须通过工具按需获取的内容。

重点检查：

- system prompt
- developer/project instructions
- user message
- conversation history
- tool definitions
- permission context
- active plan/goal/task
- runtime state
- Memory
- Personal Knowledge Base
- Enterprise Knowledge Base
- Skill
- Dynamic context
- Workflow state
- Sub-agent result

必须遵守：

- 个人知识库不能默认进入最原始上下文组装。
- 个人知识库应作为原生工具，由 Agent 按需搜索、读取和引用。
- 企业知识库建立在知识工具层之上，并增加 tenant、organization、ACL、RLS、审计和数据治理。
- Memory 与 Knowledge 必须分层，不能混成一个无边界的注入池。
- 动态上下文注入必须有预算、来源、优先级、去重、过期和可解释机制。
- 任何自动注入都必须评估 prompt injection、数据泄漏和跨租户污染风险。

## 3. 工具系统

检查：

- 工具注册与发现
- schema 校验
- 参数规范化
- 工具权限
- 风险分类
- 用户审批
- 执行幂等键
- timeout
- retry
- cancellation
- side effect 记录
- tool result 回注
- artifact 声明
- 文件写入证据
- 最终消费
- MCP、内置工具和自研工具是否统一
- 工具失败后是否能够恢复并输出用户可理解的信息

对 Preview → Confirmation → Execute/Create 类型工具重点检查：

- 用户回答是否沉淀为服务端结构化状态
- Preview 是否生成不可变 canonical object
- Confirmation 后是否只引用 canonical ID/version/hash
- 模型是否被迫再次生成整份长参数
- hash 是否对无关格式变化过度敏感
- 校验失败是否存在可靠恢复路径
- 重试是否会重复创建资源

## 4. 云端运行

检查：

- API 与 Worker 是否共享必要状态
- workspace 是否共享、同步或通过统一存储访问
- 本地磁盘是否被误当作持久事实源
- 多副本部署一致性
- sticky session 依赖
- 队列投递和重复消费
- lease/heartbeat
- crash recovery
- distributed cancellation
- distributed locking
- 对象存储
- 临时文件清理
- 大文件上传和下载
- secrets
- network egress
- sandbox 隔离
- 部署滚动更新期间的运行恢复

# 五、第二部分：Hive Native

不要只检查已经被点名的模块。请搜索并识别仓库中所有 Hive Native 能力。

至少覆盖：

- Memory
- Personal Knowledge Base
- Enterprise Knowledge Base
- Skill
- Skill evolve
- Dynamic context
- Local Agent
- A2A
- Sub-agent
- Agent Team
- Dynamic Workflow
- Goal
- Plan
- Task
- Background Task
- Scheduled Task
- Checkpoint
- Branch
- Rewind
- Compaction
- Artifact
- Workspace
- Agent identity
- Agent capability
- Agent inheritance/composition
- Evaluation
- Feedback
- Evolution
- Human-in-the-loop

## 1. Memory

区分并检查：

- working memory
- session memory
- episodic memory
- semantic memory
- preference memory
- project memory
- agent memory
- organization memory
- procedural memory

对每种 Memory 检查：

- 产生条件
- 写入入口
- 归属
- 权限
- 去重
- 冲突解决
- 可信度
- 时效性
- 版本
- 撤销
- 删除
- 引用
- 注入或工具读取方式
- 对执行结果的真实影响
- 是否存在“写了但永远不消费”

## 2. Knowledge

检查 Personal Knowledge Base 是否：

- 由 owner 管理
- 允许 owner 添加不同来源
- 作为工具调用
- 不默认进入原始上下文
- 支持检索、读取、引用、权限、删除和来源追踪
- 能区分用户内容、Agent 生成内容和外部同步内容

Enterprise Knowledge Base 应标记当前真实状态，不得把设计、空接口或未消费表结构判定为完成。

若已有相关实现，检查：

- organization/tenant 边界
- ACL/RBAC/ABAC
- RLS
- document ownership
- connector identity
- data classification
- retention
- legal hold
- audit
- citation
- indexing
- deletion propagation
- cross-tenant isolation

## 3. Skill 与进化

检查：

- Skill 的定义、安装、发现、版本、依赖和权限
- Skill 是否真实改变 Agent 行为
- Skill 与工具、Memory、Knowledge、Workflow 的边界
- Skill evolve 的输入证据
- 评估门槛
- 版本化
- shadow/canary
- 回滚
- owner approval
- enterprise policy
- 恶意或错误进化的隔离
- 进化是否可能污染所有 Agent

## 4. 多智能体

明确区分：

- 普通工具调用
- Sub-agent
- Agent Team
- A2A
- Workflow node
- Background Task

检查触发条件、状态机和返回契约：

- 谁创建
- 谁授权
- 谁付费
- 谁取消
- 谁拥有 workspace
- 是否继承父 Agent 的权限
- 是否允许权限放大
- 中间状态在哪里展示
- 用户如何查看、切换和干预
- 子任务成功后如何回到父 Agent
- 子任务失败、超时或部分成功如何处理
- 父 Agent 退出时子 Agent 怎么处理
- 多 Agent 并发写同一文件时如何解决冲突
- 是否存在无限派生、循环委派或成本失控

## 5. Workflow、计划、目标与任务

明确这些概念是否真的不同，还是仅命名不同：

- Goal
- Plan
- Task
- Workflow
- Dynamic Workflow
- Background Task
- Scheduled Task

检查它们之间：

- 创建关系
- 状态关系
- ownership
- persistence
- trigger
- dependency
- retry
- timeout
- cancellation
- approval
- artifact
- completion criteria
- UI 消费
- Agent 消费

发现重复抽象时，提出合并方案。

# 六、第三部分：企业治理、安全与 AI 资产管理

治理不能只是 UI、策略表或审批记录。它必须真实介入 Agent 生命周期，而且不能无条件阻塞 Agent。

## 1. 身份与权限

检查：

- tenant
- organization
- workspace
- project
- user
- owner
- member
- admin
- agent
- sub-agent
- service account
- connector identity
- delegated identity

验证：

- 身份如何传播到 API、Worker、数据库、对象存储、工具和子 Agent
- RLS session context 是否在异步 Worker 中仍然成立
- service role 是否绕过过多限制
- Agent 是否有稳定且可审计的执行身份
- 子 Agent 是否继承了不应继承的权限
- 权限不足时是否有合理的申请、审批和恢复机制

## 2. 治理与运行冲突

重点寻找：

- RLS 使 Worker 无法读取自己创建的任务
- RLS 允许创建但不允许读取
- API 有权限但队列消费者没有权限
- 审批后原 run 无法恢复
- 权限检查重复且结果不一致
- UI 允许操作但后端拒绝
- 后端允许操作但 UI 不提供入口
- 安全规则阻止必要证据或 artifact 登记
- 管理员策略覆盖个人策略时没有明确优先级
- deny/allow 冲突没有确定性决策
- 策略更新影响正在运行的任务但没有版本绑定
- 限额、预算、审批、RLS 同时作用时导致死锁
- Agent 为完成任务不断重复申请相同权限

必须画出治理决策顺序，并说明唯一权威决策点。

## 3. AI 资产管理

检查以下资产是否具有完整生命周期：

- Agent
- Sub-agent
- Agent Team
- Skill
- Workflow
- Prompt
- Tool
- Connector
- Model configuration
- Memory policy
- Knowledge source
- Evaluation
- Template
- Artifact

每类资产检查：

- owner
- organization
- visibility
- permission
- version
- draft/published/deprecated
- dependency
- provenance
- audit
- approval
- rollback
- import/export
- duplication
- deletion
- retention
- runtime binding
- usage evidence

不要把只有 CRUD 的资产管理判定为闭环。

## 4. 安全与合规

检查：

- secrets management
- encryption
- tenant isolation
- prompt injection
- tool injection
- data exfiltration
- SSRF
- path traversal
- arbitrary command execution
- sandbox escape
- connector scope
- audit immutability
- PII
- retention
- deletion
- export
- incident traceability
- rate limit
- quota
- budget
- model/provider policy
- content policy
- supply-chain risk
- Skill/Plugin/MCP trust

每个安全限制都要同时检查：

1. 是否真正生效。
2. 是否会错误阻断 Agent 的正常生产路径。
3. 被阻断后是否有恢复路径。
4. 是否能向用户提供适当且不泄密的解释。

# 七、第四部分：用户使用体验与 UI/UX

必须从真实用户完成任务的视角检查，不要只做组件目录审查。

## 1. 信息分层

将信息分为：

- 普通用户必须看到
- 用户按需展开
- Workspace/交付物区域展示
- 管理员或公司后台展示
- 开发与诊断模式展示
- 不应直接展示

逐项检查当前 UI 是否错误暴露：

- schema
- raw JSON
- internal ID
- correlation ID
- run ID
- thread ID
- API request
- typed data
- evidence metadata
- provider payload
- token internals
- raw stack trace
-内部权限实现
-数据库字段

普通用户界面应优先呈现：

- 当前 Agent 在做什么
- 是否需要用户操作
- 当前进度
- 关键计划与任务
- 交付物
- 可恢复错误
- 下一步
- 成本或额度的必要提示
- 可理解的权限申请

## 2. 页面布局

重新确认当前整体布局，不要默认必须保留现有栏数。

检查：

- 左侧区域的核心职责
- 主 Session 区域的核心职责
- 右侧 Workspace/Artifact 区域的核心职责
- 底部状态区的核心职责
- 管理员和调试信息是否进入了普通用户布局
- 桌面尺寸、窄屏、折叠、展开和全屏
- 面板之间是否重复展示同一信息
- Workspace 是否被运行时调试数据占据
- 交付物是否始终容易找到
- 面板开关是否保存
- 焦点、键盘、无障碍和滚动是否合理

Workspace 原则上应优先展示：

- 当前任务交付物
- 文件和目录
- 预览
- 版本或变更
- 生成状态
- 下载、打开、定位和引用
- 与当前 Session 的关联

不要把原始运行数据默认放入 Workspace 主视图。

## 3. Session 表达

检查每类事件的用户表达：

- reasoning
- progress
- plan
- tool call
- tool result
- approval
- question
- warning
- retry
- failure
- cancellation
- sub-agent
- workflow
- artifact
- final response

判断：

- 哪些应显示为消息
- 哪些应折叠
- 哪些只应进入 activity/status
- 哪些只应进入管理员审计
- 哪些应支持点击查看详情
- 哪些状态需要持续更新而不是追加重复消息

## 4. 交付物闭环

必须追踪：

文件生成
→ workspace 同步或持久化
→ 写入证据
→ artifact 登记
→ 权限校验
→ 主 Agent 消费
→ Session 附件
→ Workspace 展示
→ 下载或打开

检查：

- 文件存在但 artifact 被拒绝
- artifact 已登记但 UI 不显示
- 聊天可见但 Workspace 不可见
- Workspace 可见但无法下载
- Worker 与 API 不共享文件
- 本轮写入规则误伤脚本生成文件
- 路径、软链接、大小、MIME、安全扫描造成的交付失败
- 最终回复声称已交付但实际没有附件
- rejected artifact 是否有恢复机制

## 5. 分支与历史

检查：

- Branch
- Rewind
- Checkpoint
- GitLine/历史线
- 消息编辑
- Retry
- Fork

明确每个操作影响：

- 消息
- run
- workspace
- artifact
- memory
- task
- approval
- sub-agent
- cost
- audit

避免用户看到“回到了过去”，但文件、Memory 或子任务仍留在未来状态。

## 6. 多智能体 UI

检查：

- Sub-agent 何时出现
- Agent Team 何时出现
- 用户是否能识别谁在执行
- 是否能查看子任务
- 是否能取消单个成员
- 是否能看到部分成功
- 子 Agent 结果何时合并
- 父 Agent 是否明确接收结果
- 多 Agent 状态是否挤占主对话
- 是否存在主 Agent 已结束但子 Agent 仍在运行
- 用户是否能从失败状态恢复

# 八、代码极简性与架构质量

单独进行一轮代码层面的原子化审查。

遵循：

- 第一性原理
- 奥卡姆剃刀
- KISS
- 单一事实源
- 单一职责
- 显式状态机
- 最少必要抽象
- 组合优于继承
- 删除无消费路径的代码
- 避免提前泛化

重点寻找：

- 重复模型
- 重复状态枚举
- 重复权限判断
- 重复事件转换
- 重复 artifact 逻辑
- 前后端各自推导状态
- 多套 Agent 执行入口
- 多套 Workspace 抽象
- 多套 Memory/Knowledge 注入机制
- 相同概念不同命名
- 不同概念使用相同命名
- 过度封装
- 无消费者的数据表
- 无生产调用者的 service
- 永远不会触发的 fallback
- 兼容层长期变成主路径
- feature flag 已失效但仍保留
- 为未来设计却增加当前复杂度的代码
- 巨型组件、巨型 service 和循环依赖
- 隐式副作用
- 依赖 UI 猜测后端状态

对每个简化建议说明：

- 可以删除、合并或重写什么
- 为什么不会破坏现有能力
- 风险和迁移方式
- 简化后减少了哪些状态或旁路
- 应补充哪些契约测试

# 九、对标审查

如果环境中可以访问相关参考实现，请对成熟的单 Agent 生命周期和桌面端交互模式进行源码级或行为级对比。

对标时必须区分：

- 可验证的公开实现
- 可以通过当前环境观察到的行为
- 合理推断
- 服务商私有远程能力
- 当前项目不需要复制的能力

不得把猜测写成源码事实。

对标重点：

- Agent loop
- tool loop
- permission
- plan
- task
- background execution
- compaction
- checkpoint
- branch
- retry
- artifact
- workspace
- terminal
- diff
- status
- notification
- keyboard interaction
- panel behavior
- error recovery
- cloud adaptation

目标不是机械复制，而是判断当前系统是否达到同等级别的完整性、稳定性和交互清晰度。

# 十、执行步骤

按照以下顺序执行：

1. 建立仓库地图
   - 应用
   - 服务
   - package
   - runtime
   - Worker
   - 数据库
   - 前端
   - 测试
   - 部署
   - 文档

2. 建立核心实体图
   - tenant
   - organization
   - user
   - agent
   - thread
   - turn
   - run
   - event
   - tool call
   - approval
   - task
   - workflow
   - memory
   - knowledge
   - artifact
   - workspace

3. 建立状态机
   - Agent run
   - tool call
   - approval
   - artifact
   - task/workflow
   - sub-agent
   - scheduled/background task

4. 建立事实源矩阵
   - 每种状态由谁写入
   - 谁是权威
   - 谁消费
   - 谁可以修正
   - UI 从哪里读取

5. 沿生产路径逐条验证七个原子。

6. 做反向消费检查
   - 对所有表、事件、API、组件和状态检查是否有真实消费者。

7. 做失败路径检查
   - timeout
   - reconnect
   - retry
   - cancel
   - crash
   - permission denied
   - partial success
   - duplicate delivery
   - stale state

8. 做代码极简性检查。

9. 汇总断点，去重并确定根因。

10. 给出按依赖排序的落地方案。

# 十一、断点记录格式

每个断点必须使用统一格式：

## [稳定编号] 断点名称

- 所属模块：
- 严重级别：P0 / P1 / P2 / P3
- 当前状态：局部闭环 / 断点 / 缺失 / 已知缺失
- 影响对象：
- 用户可见现象：
- 触发条件：
- 输入原子：
- 权威原子：
- 执行原子：
- 证据原子：
- 恢复原子：
- 消费原子：
- 验收原子：
- 断裂位置：
- 根因：
- 是否存在双事实源：
- 是否存在治理冲突：
- 是否存在跨租户或安全风险：
- 是否可能导致 Agent 无法继续运行：
- 源码证据：
- 数据库或迁移证据：
- UI 消费证据：
- 测试证据：
- 反证或不确定性：
- 修复方案：
- 最小化方案：
- 需要删除或合并的旧实现：
- 依赖项：
- 验收标准：
- 建议测试：
- 建议故障注入：
- 预计风险：

源码证据必须包含准确文件路径、符号名和行号。不要只写目录名。

# 十二、报告结构

最终报告至少包含：

1. 执行摘要
2. 审查范围与未覆盖范围
3. 证据方法和置信度计算方式
4. 系统架构地图
5. 核心实体与事实源矩阵
6. 单 Agent 生命周期结论
7. Hive Native 结论
8. 企业治理与安全结论
9. 用户体验与 UI/UX 结论
10. 代码极简性结论
11. 全部断点清单
12. P0/P1 阻塞上线项
13. 双事实源清单
14. 治理、RLS 与运行冲突清单
15. 无消费路径的代码、表、API 和组件
16. 应删除、合并或收敛的抽象
17. 已知缺失与明确排除项
18. 分阶段落地方案
19. 依赖关系和建议实施顺序
20. 验收矩阵
21. 测试与故障注入方案
22. 残余风险
23. 最终置信度

# 十三、优先级定义

- P0：数据泄漏、跨租户、安全绕过、不可逆数据破坏、核心运行完全不可用。
- P1：高频任务失败、运行无法恢复、产物无法交付、治理导致 Agent 卡死、状态严重漂移。
- P2：局部路径失败、明显体验问题、维护风险、缺少关键测试。
- P3：低频边缘问题、代码清理、非阻塞一致性或视觉问题。

# 十四、落地方案要求

修复方案不能只写“增加校验”“补充测试”“优化 UI”。

每项方案必须说明：

- 唯一事实源是什么
- 唯一写入口是什么
- 状态机如何变化
- 权限在哪里决策
- 证据在哪里产生
- 失败后如何恢复
- 谁消费结果
- UI 如何呈现
- 旧路径如何迁移和删除
- 如何测试
- 如何证明闭环

实施顺序必须按依赖关系排列，优先解决：

1. 身份和事实源
2. 状态机和执行入口
3. 权限与治理冲突
4. 恢复和幂等
5. artifact/workspace 消费
6. 多智能体返回契约
7. UI 信息架构
8. 代码简化和清理

# 十五、置信度要求

最终给出整体置信度和四个分项置信度。

置信度不能凭主观感觉给出，应至少参考：

- 生产路径覆盖率
- 七原子覆盖率
- 源码证据覆盖率
- 数据库与 RLS 覆盖率
- UI 消费覆盖率
- 失败路径覆盖率
- 测试覆盖率
- 无法访问或无法验证的范围

如果置信度不足，明确说明：

- 哪些范围没有验证
- 为什么没有验证
- 补充什么证据才能提高置信度

不要为了满足目标而宣称达到某个置信度。结论必须诚实、可复核。

# 十六、禁止事项

- 不要只总结已有文档。
- 不要因为有代码就判断完成。
- 不要因为有测试就判断生产路径成立。
- 不要只检查 happy path。
- 不要只检查后端或只检查前端。
- 不要忽略数据库、RLS、Worker、对象存储和部署拓扑。
- 不要把计划中的能力写成已完成。
- 不要隐藏重复实现和架构复杂度。
- 不要用模糊语言掩盖无法验证的结论。
- 不要预设断点数量。
- 不要预设项目已经做过多少次审查。
- 不要引用不存在的历史轮次。
- 不要使用固定日期作为审查逻辑的一部分。
- 不要在未经授权的情况下直接修改生产代码或生产数据。

# 十七、完成条件

只有满足以下条件才算完成：

- 四个目标模块均已覆盖。
- 核心生产路径完成端到端和反向追踪。
- 每个结论都能追溯到当前源码或明确标记为未证实。
- 每个断点都按照七个原子记录。
- 给出事实源、状态机、治理冲突和 UI 信息分层结论。
- 给出可执行、可验收、按依赖排序的落地方案。
- 单独完成代码极简性审查。
- 明确已知缺失、排除项、未覆盖范围和残余风险。
- 报告不包含虚构的完成状态或无证据的高置信度结论。

现在开始。先建立仓库地图和审查计划，再执行完整扫描。不要在只完成目录浏览或文档阅读后提前给出最终结论。
