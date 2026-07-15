# Hive Eval 系统 — 实施规格

版本:v1(2026-07-02,修订记录见文末)
状态:**设计已闭环,据此实施。** 本规格取代 `external-behavior-eval-ci.md`(E1-E10 体系,已降为历史档案);§2.1 同时是 `memory-system-spec.md` §7.2 承诺的"效果 eval 最小骨架"定稿。现状证据(七套子系统盘点、Railway eval 环境、nightly 全红)见 2026-07-02 eval 讨论轮记录。

---

## 0. 一页纲领

- **定位**:eval 不是一套系统,是**四个问题的答案**;答案尽量从生产已有证据里"读"出来,只有晋升裁决需要"做"一点事。
- **标尺**(判断任何 eval 设计):每增加一件基础设施,必须能指出它回答四问中的哪一问、以及为什么读现有证据答不了。答不上来 = 不建。
- **四问**:

| # | 问题 | 性质 | 组件 |
|---|---|---|---|
| J1 | 这个 skill/prompt 候选,采纳比不采纳好吗? | 事件触发,per-候选 | 晋升试用制(§2.2) |
| J2 | **这个 agent 有没有越来越强?** | 纵向持续 | 成长报告(§2.1) |
| J3 | 平台改动没把 agent 改笨吧? | per-PR | 确定性回归层(§2.3) |
| J4 | 和 hermes 比到底怎样? | 里程碑,一次性 | 手动 bakeoff(§2.4) |

- **为什么推倒重来**(病根三行,证据见讨论记录):旧体系把四问全塞进"合成场景 + 克隆环境 + 夜跑"一个机制——① 实验室形态错置(企业产品没有 per-skill benchmark 任务集,任务是异构真实工作);② 合成玩具场景信息量趋零(判据是字符串暗号,答不了"越来越强");③ gate-first 守空管道(晋升门 fail-closed 等一个结构性跑不通的报告 → 生产零真晋升,克隆环境烧两周、nightly 全红、live 分数零产出)。

**硬约束(实施不变量)**:
1. **读数,不建第二生产**:禁克隆环境、禁常设合成场景套件、禁 eval 专用前后端。eval 是生产证据(T2 labels / session_feedback / invocation_spans / evolution_ledger)的读者。
2. **裁决只用硬信号 + 真实世界结果**(owner 反馈、任务结果、exit code);LLM judge 只做选拔与观察,**永不裁决晋升与合并**。
3. **验证器在 agent 可写面之外**(evaluator_integrity 信任根,范围收缩到保留件)。
4. **lineage 全程 ledger 可审计、可回滚**(evolution_ledger 一行不动)。
5. **"不退化"= 纵向真实指标不恶化**;合成分数基线概念整体退役,成长报告就是新基线。

---

## 1. 目标形态:四个组件

```
① 成长报告(J2)——主角
   生产证据 → SQL/文件读数(零 LLM) → per-agent growth_report.md
   → heartbeat/dream 顺手生成 → 现有管理页展示。零新前后端、零新环境。

② 晋升试用制(J1)——唯一"做事"的环节
   硬地板(已有):artifact_gate + evolution_verification 硬 graders
   裁决:promote 拆两段 provisional → promoted
        试用窗口内真实结果信号超阈 → 自动回滚;安然度过 → 转正

③ 确定性回归层(J3)——保留收缩
   per-PR:adversarial_suite + evaluator_integrity + internal suite
   --fail-under + prompt 契约。nightly 克隆链路整体删除。

④ 手动里程碑 bakeoff(J4)
   一个手动命令,真 shell-out,要宣称时才跑、跑完记档。不进 CI。
```

**证明责任的转移**:"越来越强"从"合成场景分数 vs 基线"换成"纵向真实指标"——失败模式复发率降、规避成功率升、owner 负反馈占比降、返工率降。今天的底料(labels.md / session_feedback / invocation_spans)就能出 v1 报告;记忆 spec 工序 1 labels 与 C8 SQLite 落地后自动变精细。**J2 与记忆系统 §7.2 从此是同一件东西。**

---

## 2. 组件规格

### 2.1 成长报告(J2,主角)

- **数据源**(全部已有或记忆 C 系列在建):T2 `labels.md`(失败/返工/owner 反馈标签;工序 1 落地后含 self-signal 与失败模式 ref)、`session_feedback`(useful/misleading + 极性)、`invocation_spans`(任务量/时长/token)、`evolution_ledger`(晋升/回滚史)、读侧激活日志、C8 引用计数。
- **计算**:纯 SQL/文件读数,**零 LLM**——数数是机械活,不违 L1;**解读归 heartbeat/dream 的 LLM 反思**(报告数字作为反思输入,L1 用在解读)。
- **核心指标(v1 四个)**:
  1. 失败模式复发率 / 规避成功率(按 `self.md` 失败模式 id 聚合;复发检出 = labels 带失败模式 ref);
  2. knowledge/skill 复用命中率(读侧激活记录 × 后续任务结果 + 引用计数增长);
  3. owner 反馈极性趋势(正/负占比随时间);
  4. 返工率趋势(labels 返工标签随时间)。
- **产物**:per-agent `memory/control/growth_report.md`,heartbeat/dream 节律生成(蒸馏器顺手,不新增守护进程);管理端在**现有**agent 详情/可观测页展示。**零新前后端。**
- **这份报告就是 owner 问"有没有效果"时打开看的那份东西**,也是不变量 5 的"新基线"。

### 2.2 晋升试用制(J1,唯一"做事"的环节)

**门槛顺序:硬地板挡坏的 → 试用期裁决好不好。**

- **硬地板(全部已有,不动)**:候选先过 `evolution_verification` 硬 graders(deterministic_command / state_check / tool_call_check / skill_guard)+ `artifact_gate`(适用时:可执行产物在 microVM 真跑,只信 exit code 不信自述)。**去掉** `behavior_eval_passed` 前置与合成基线 regression gate——`decide_behavior_gated_promotion` 判据换为:硬 graders ∧ artifact_gate(适用时)→ 进入 provisional。
- **provisional(试用期)**:
  - **即生效**(否则无从积累真实使用证据),但被监控、可自动撤;转正只是移除监控。
  - **归因**:监测对象 = 加载/执行了该候选的 invocation(`record_skill_execution` / spans 已记录使用)。
  - **结算规则**(数值全部归 config,spec 只定机制与信号语义;首轮取保守默认):
    - 窗口:`TRIAL_WINDOW_DAYS`(默认 7)内累计真实使用 ≥ `TRIAL_MIN_USES`(默认 10)且负信号未超阈 → **转正 promoted**;
    - 负信号超阈 → **即时自动回滚**(`record_rollback_event`,已有原语):关联 invocation 的 owner 负反馈 ≥ 阈值(默认 2 次)、或失败/返工率显著高于该 agent 近期基线、或单次严重失败;
    - 窗口结束使用不足 → 延长一个窗口;仍不足 → 回滚(未证明价值的能力不转正)。
  - **串行试用**:同一 skill 同时只试用一个候选,其余候选排队。
- **选拔(可选后置,非本轮)**:T0 抽该 agent 自己近期真实任务重放对照,LLM pairwise judge 输出"值不值得试用"的建议分,只进 ledger 观察字段,**不裁决**(不变量 2)。
- **SkillOpt 传承**(取其思想弃其实验室):validation gating = 没有"不变差"的证据不转正;被拒/被回滚候选记入 ledger,作为后续候选生成的负反馈。
- **治理不放松**:provisional 候选仍走全部既有 gate(write gate / skill_guard / 审计);试用制只改"裁决信号",不开新的写入后门。

### 2.3 确定性回归层(J3)

- **per-PR 保留(已绿、便宜)**:`adversarial_suite`(四类 reward-hack 攻击必须被拦)+ `evaluator_integrity`(**从 nightly 移到 per-PR**,信任根清单收缩到保留件)+ `run.py` internal suite `--fail-under` + prompt 契约检查(`prompt_eval`;`task_eval` 审减并入 internal suite)+ `retrieval_eval`(读侧施工时同步更新)。
- `self_evolution_bakeoff` **降级为普通集成测试**:保留断言价值(服务级行为结构),退出"eval/打分"名分与外衣,施工时定位置。
- **删除**:nightly 全链(workflow job、跨环境 HTTP/ssh 桥、`ci_gate`)。
- **可选后置(P2,明确非本轮)**:活体冒烟 = 生产内 eval 租户 + 自家 trigger/RuntimeTask 定期自跑 1 个小任务、结果写 ledger。隔离靠自家多租户 RLS——不依赖克隆环境。

### 2.4 手动里程碑 bakeoff(J4)

- `bakeoff_runtime` 核心收缩成**一个手动命令**(真 shell-out `claude`/`hermes` CLI + 真实工作区 + 外部硬判据),**删除 `repo_evidence` 假分 fallback**;`hermes_baseline` 对比逻辑并入。
- 触发 = 人工、里程碑时;产物 = 报告记档(docs/ + ledger)。不进 CI、不常设。
- **宣称纪律(不变)**:任何"不弱于/超越 hermes"的说法,必须引用一次真跑记档;无记档 = Speculation。

---

## 3. 资产处置

### 3.1 删(约 5,000+ 行 + 全部常设运营负担;相关测试同批删)

| 对象 | 说明 |
|---|---|
| Railway eval 环境(五服务 + volume) | **owner 面板操作**;GitHub secrets(`RAILWAY_EVAL_*`/`HIVE_EVAL_API_URL`/`HIVE_EVAL_CI_TOKEN`)同批清理 |
| `harness-ci.yml` nightly behavior-eval job | 消灭每晚假红 |
| `app/evals/`:`hive_live_runner.py`、`baseline.py` + `baselines/`、`update_behavior_baseline.py`、`behavior_eval_evidence.py`、`ci_gate.py`、`cost_budget.py`、`hermes_baseline.py`(逻辑并入手动命令) | 合成场景晋升前置 + 合成基线概念整体退役 |
| `services/eval_ci_service.py`、`services/tenant_behavior_eval_publisher.py`、`api/eval_ci.py`、`api/enterprise.py` 的 eval-ci proxy 端点 | 跨环境通路 + 生产 24h 白烧通路 |
| 前端 `WorkspaceEvalCiSection.tsx(+test)`、`enterprise.ts` 4 端点、EnterpriseSettings `eval_ci` tab 及入口 | eval 专用前后端退役 |
| config:`BEHAVIOR_EVAL_AUTO_PUBLISH_ENABLED`、`BEHAVIOR_EVAL_REPORT_MAX_AGE_HOURS`、`HIVE_EVAL_*` | 通路同批删,配置不留孤儿 |
| templates 装饰性 `eval.yaml` ×8;`skill_creator_files` 中 shell `claude` CLI 的 eval-viewer/run_eval(从 seeder payload 去除) | 从未被执行的假格式不供养;Skill capsule **携带 evals 的能力保留**(可执行 eval 脚本自然并入 artifact_gate 验证) |
| CLAUDE.md 的 `policy_replay` 等过期引用 + eval 相关段落 | 文档与现实对齐 |

### 3.2 留(改造)

| 对象 | 处置 |
|---|---|
| `evolution_ledger` | 一行不动(审计链,不变量 4) |
| `evolution_verification` 硬 graders | 去掉 behavior_report 前置,接试用制(§2.2) |
| `artifact_gate` | 不动(硬地板) |
| `adversarial_suite` / `evaluator_integrity` | 保留;integrity 移 per-PR,信任根收缩到保留件 |
| `run.py` internal suite + `--fail-under` | 保留瘦身;bakeoff mode 收缩为 J4 手动入口 |
| `retrieval_eval` | 保留确定层;读侧施工时更新 |
| `bakeoff_runtime` 核心 | 收缩为 J4 手动命令,删假分 fallback |
| `self_evolution_bakeoff` | 降级普通集成测试 |

### 3.3 新(两个小件 + 一个可选)

| 件 | 说明 |
|---|---|
| 成长报告生成器 | SQL/文件读数 → `growth_report.md`,挂 heartbeat/dream;现有页展示 |
| provisional 晋升 + 试用监测 + 自动回滚接线 | ledger 已有 rollback 原语,是接线不是造轮子 |
| (可选后置)T0 重放对照器 | J1 选拔用,LLM judge 只建议不裁决 |

---

## 4. 施工计划

### 4.1 批 1「拆弹换心」(eval 独立批,先行)

§3.1 删除清单全清 + **晋升门同批换试用制**——一次完整 pass,**禁止中间死态**("删了供血但门还在等报告");现状本就是死态(永久 hold),本批把死门换成活门。同批:bakeoff 手动化、evaluator_integrity 移 per-PR、`self_evolution_bakeoff` 降级、`external-behavior-eval-ci.md` 降历史档案、CLAUDE.md 清理。

**红测方向(先写失败)**:① 候选无 behavior report 时不再永久 hold,而是过硬地板后进 provisional;② 试用期负信号真触发自动回滚(ledger 出 rollback 记录);③ 假分路径全灭(grep `repo_evidence` 无门控引用);④ 删除件无残余 import。

### 4.2 批 2「读数」(并入记忆 C 系列施工)

growth_report v1 用现有底料先出(labels.md / session_feedback / spans)→ C8 SQLite 批加失败模式聚合与引用计数指标 → 工序 1 labels 落地后指标自动补全。与记忆施工共用同一批地基,不单独排期。

### 4.3 Owner 操作项

Railway 面板关停 eval 环境(含 Postgres volume,内含仅 eval 租户配置与测试数据,无生产价值);GitHub 三组 eval secrets 删除。

---

## 5. 验收

### 5.1 设计不变量自检(每次改动)
- 每件 eval 基础设施能指出回答哪一问(§0 标尺)。
- 零克隆环境、零 eval 专用前后端、零常设合成场景。
- LLM judge 零裁决权;裁决全部来自硬信号 + 真实结果。
- lineage 可审计可回滚;验证器在 agent 可写面之外。
- "不退化"有定义:成长报告纵向指标。

### 5.2 效果验收
- owner 打开成长报告能回答"这个 agent 有没有越来越强"。
- **晋升在生产真实发生**:provisional → promoted 或 → 回滚都算"活"——终结"零真晋升"。
- J4 纪律:对标结论必须引用真跑记档。
- 诚实边界:本 spec 落地 = 机制活了,**不自动等于"效果好"**;效果由成长报告读数与 J4 记档说话。

---

## 修订记录

- **v1(2026-07-02)**:初版。方向 owner 拍板(2026-07-02 eval 专门讨论轮):读数化 eval、Railway eval 环境关停、nightly 删除、七套子系统归一为四组件、晋升换 provisional 试用制。取代 `external-behavior-eval-ci.md`。
- **v1.1(2026-07-02 施工对齐)**:全组件落地。①J1 provisional 试用制 + §3.1 删除清单 + 前端退役由 eval 拆弹批完成(commit `0034ed7e`);②J2 成长报告实装(labels v3 失败信号轴、零 LLM 读数生成器 `services/growth_report.py`、heartbeat 挂载、既有 observability 端点直出);③J3 `self_evolution_bakeoff` 降级为普通集成测试 `tests/evals/test_self_evolution_behaviors.py`(服务级行为断言保留,打分/Hermes 对比/CLI 外衣与 CI 步骤退役;失效的 rerank 延迟特征串 check 随读侧零 LLM 收口一并退役);④J4 `bakeoff_runtime` 删除 `repo_evidence` 假分 fallback(CLI 不可用 = 诚实 `runtime_unavailable` 空报告,场景失败不回填合成分);⑤§2.3/§3.2 的 `retrieval_eval` "保留"条目被记忆主线 C7 读侧重构取代——该模块评测的旧 wiki 检索路径已退役,读侧覆盖改由 `tests/memory` 套件承担(本条为文档与现实对齐,非新决策);⑥seeder 中 shell-claude 的 eval-viewer/run_eval/run_loop 与装饰性 `eval.yaml` ×6 已从 payload 去除,agents/grader·comparator·analyzer 指引作为 Skill capsule evals 能力保留。
