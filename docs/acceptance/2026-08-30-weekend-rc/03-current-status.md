---
document_id: weekend-rc-2026-08-30-current-status
owner: Codex
status: in_progress
authority: canonical-working-state
last_reviewed: 2026-09-09
source_commit: f0b6939e
verification_status: remaining-functional-closure-in-progress
---
# 当前状态与唯一下一动作

[返回索引](README.md) · [旅程账本](04-journey-ledger.md) · [Findings](05-findings.md) · [Runbook](06-runbook-and-release-gates.md)

## 当前决定与本批次出口

16:33 收尾核对：once04正式GET为fire_count1/disabled，last_fired_at=08:31:48.425672Z；文件 `workspace/wrc-once-chat-04.md`读取200，真实内容为marker及19*4=76。聊天原生Plan建议→authenticated decline→set_trigger→到时文件消费通过；父聊天重开仍只有创建回执，未把它说成自动回帖已验。Office、固定Workflow、Goal、Local、once/schedule/event已通过的有界路径不重做。A2A/Dynamic最终父消费、Growth回答矛盾、活动Skill卸载、角色/自审政策、完整D/E及cleanup仍未闭环，**不是全部完成，NPTCR仍0/96**。文档结构10passed/0.35s，diff检查通过；最新应用CI34327770653后端仍在全量pytest阶段，前端与15journeys成功。完整分类及精确资产见[收尾记录](evidence/c655a4d351b5c9a17d4602f50a158bb93ef64c21/remaining-functional-2026-09-09-01.md)。

16:25 实测：`f0b6939e`已push，三服务SUCCESS：backend `2f7e0fdb-0229-4f47-87d8-236270ce05f8`、backend-api `1eee73e1-4f86-4840-9917-55f3781b68e4`、frontend `f91fcdd4-0d94-425d-a1f6-93b91ebe3fd0`。public backend与精确archive同1062文件/hash `103d56d156ecfd6c3ac9e7bf317320095e0158a1f8c1f481f515a3abb0fdbdac`；backend-api SSH三次连接关闭，未宣称其独立运行hash已核实。最终RLS17passed/202.73s，Skill真实PG及API10passed，release archive hygiene3487paths通过，CI34327770653仍在执行。Event03已捕获canonical input并仅fire1后停用，正式file GET200包含marker及17*5=85。once04真实request_plan_mode→用户拒绝→set_trigger成功，原生推荐账本绑定有效，trigger `f07ba328-52ed-4c6f-adc9-8ced3e7cd044`尚待16:30:43唤醒，不提前算PASS。Skill registry创建/编辑/安装/实际load_skill及29+7=36完成，待精确清理。A2A02最终报告正式UI下载hash5897f784…与journal一致，源文件hashd0150e0c…一致，三员工交接及最终文件已交付；父会话仍未见完整最终结果页，不声称自动父消费通过。离职仅打开合成member的影响预览并取消：1个Agent、1条直接权限、接收人为同公司owner；未转移、停用或升权。

16:08 收尾候选：V2 admitted human input 已接入事件触发；预算拒绝父模型续轮时仍投递已提交结果；技能库新建关系集合已修复。原生 Plan 卡片的真实工具回执现可绑定 authenticated user 的拒绝，只对当前 run 生效，同 run 恢复可复用、其他 run 不可重放；未新增权限或改变 Plan 高风险边界。Plan/工具/编排110项检查通过，真实PG覆盖错误工具/用户/状态拒绝；其余相关与最终RLS检查进行中。旧once03 permission已正式deny200，同run resumable，不执行过期时间。Useful反馈已active且fresh会话load_memory读到143→286和证据续轮做法，但模型同时声称找不到，两者矛盾，成长效果仍不计PASS。CI f0671806 的唯一失败已定位为漏同步RLS源码指纹；新候选按真实源码差异复核后更新，不放宽断言。尚未部署本候选。

15:55 继续：f0671806三服务SUCCESS并同1062文件/hash `dd1532cf2e02fd629dd056eb988580f57ac6a3c7c7a478d521b594d5dc7db64d`。A2A02最后节点Attempt2实际completed，最终文件/hash `5897f784ad3ee187aecb1686a33f9d3a250af5e18afcd1e0f177455762d35b5a`，包含161/9/152及两上游hash；root也terminal_committed completed。父消费被旧page epoch2的runtime_budget_exhausted挡住，不记全链PASS。新候选将明确预算拒绝局部化为已提交结果的只读投影、不启动模型，并避免阻塞后续页；真实PG反例及相关72+12检查通过。Event02输入已进入canonical Session V2且正常回复，但旧on_message只查ChatMessage而无匹配；候选补齐三个human入口的已admitted V2读取，保持tenant/agent/用户或sender范围，过滤内部通知/Goal/取消和未准入输入，真实PG三模式及拒绝反例通过。Skill正式新建失败，真实PG复现新建关联集合MissingGreenlet，一行初始化后完整新增/编辑/读取/删除13项通过。Useful新反馈 `f80e07f8-4e4e-4c65-ad8d-1491b1221556`已active，fresh回忆验证中；旧held不改写。once03已有真实request_plan_mode及用户回复，但仍plan_confirmation_required：新卡片协议未落旧recommendation且旧binder只认固定中文assistant marker；继续修正此确认接线，不扩大权限。新候选尚未提交/部署；RLS16pass/1指纹失败，复核588 scope及callsites不变，需完成当前源码精确指纹复验。

15:30 继续：Office完整原生链已通过；Goal02已按两个独立run完成143→286，首轮45941+自动续轮142026=187967，canonical goal同值、continuation_count1、无active run，最终文件正式读取200。schedule01 runtime `c5272968-66d8-4735-ac4a-c66dc6002860` completed，实际文件91，trigger仅fire1后停用。event01虽唤醒但匹配正文在跨worker交接丢失，产物诚实报缺payload，不计PASS；候选9939609e把精确event数据随原intent保存并仅恢复数据字段。A2A02前两员工与人工gate通过，第三child `721ed9b2-7b68-5070-b913-c1e5c8b2d681`实际failed：queued metadata遗漏max_depth、恢复默认2而depth3；同候选补齐既有请求参数，不扩大权限或全局默认。相关205项（含真实PG及max_depth0拒绝反例）、ruff/diff通过，待部署后仅retry第三节点。Dynamic07通知确实delivered并启动父run，但模型收到35K runtime notice内的结果refs后仍重复旧preview；并非通知没投递，不能伪造自动消费PASS。bfd三服务同源1062/hash `5896fddc72dbf8eba4d8642bdbfc3dcda3c99bb030ac4d40cee66e537c0a93fb`已核实。

15:12 Office原生预览已真实恢复，f8adc3ec三服务SUCCESS，backend/backend-api/archive同1062文件/hash `53e3146c3221c4838c0b6c4821a96f57ca3c48167add291682227e2f869624aa`。正式页面Word标题/正文/表格与Excel网格/标签/29正常。新原生修正版run `a115c34e-f133-577b-bbc6-25ec902a746e` completed，artifact `d9446c8b-2c61-4390-a9be-ee278d41f1e2`正式下载200/2938B/hash `07e7f8ea82f5f17deb76ffc06e5503bd8fc63d838f7e4f94f59ce39fd98fef82`，实际OOXML formula SUM(17,12)/cache29、生产native validate0/text29与正式保存快照预览通过；原DOCX/XLSX未删除。A2A默认轻量200K导致used104770+reserved50000+下一轮52247超限，非无故取消；bfd354b7最小改为既有configured workflow envelope，显式预算仍保留并展示真实budget终止原因，26项相关含真实PG通过，已三服务SUCCESS。新run `991cb743-2697-4e7b-9736-41fd2e5292a5`明确2M/1800s，独立02路径与hash `a76fa82beb92f7059dd9cba08275ca2d350fdf469a7fec7aeea6bdf5f49a85c2`，首child继续真实执行。新Goal `bdb0d07e-350d-48b6-a8d4-4d70886563b8` / Session `fc36005f-ca20-400f-8e85-2698b1cb1b96`明确首轮仅phase1，500K/3continuations/1200s，自动两轮仍实测中。Dynamic07实际两个execute_code51/57、join108和正式文件读取通过，但父会话仍只显示预览/两条失败通知，最终自动消费缺口仍查，不伪装通过。继续once聊天、schedule/event、Growth/能力/角色原清单。

15:00 Office 原生渲染修复：适配器仅对HTML模式接收CLI实际raw HTML，保留非零退出/其他模式严格JSON错误；preview contract v2使旧错误fallback缓存失效。相关42项通过、ruff/diff通过；生产1.0.88临时进程加载候选后，DOCX/XLSX/PPTX真实HTML、text、service preview HTML及CSP全部通过（36621/7541/19240 bytes）。未替换生产文件，待正式部署后UI复验。A2A原run已确认budget service以runtime_budget_exhausted终止，首child实际failed、无重发；默认200K graph上限与真实预算预留冲突正在核对，绝不伪装成功或扩大租户权限。

14:50 live：e3e2d26a三服务SUCCESS，同1062文件/source SHA256 `5d271b150dfc7ec88a446f48ece54ea4a8c03d584a20dfb5320633a0f425cf62`。Office snapshot预览CSP阻断已消失，但原生HTML被适配层按JSON解析失败而错误fallback；owner明确要求修好OfficeCLI自带渲染。生产CLI1.0.88临时副本已复现，fresh原生XLSX公式29/validate0通过，旧表styles/无cache另保留。A2A已从正式UI预览并启动run `f781153f-d23c-4724-8b1d-a0f2c16f978b`，rootSession `e1e9dc43-9773-4ebf-9253-8635e461fe0f`，三participant为原Analyst/Reviewer/B-Worker，未改模型/权限。首child MiniMax真实运行中，但root意外显示killed（未执行取消）；正在查这一生产故障，不重发原run或声称业务通过。

13:40 更新：A2A候选085e7cfe已完成原生worker/完整Agent执行、不可变文件交接、独立Session、人工gate、缺产物暂停、同child恢复、显式node retry和取消对账；未知/缺失child不伪装cancel成功。新增真实PG及相关25项、前端7项、完整build、RLS17项通过；之前124后端/28前端支持证据保留。MCP isError修复一并集成，Office本地blob预览修复待同源部署。尚无A2A生产业务PASS，继续三员工真实链、Office预览/表格styles验证、其余原剩余清单。保留main无关owner脏文件；workflow_runtime_service只集成候选blob至index，不覆盖owner工作区内容。

13:29 消费反例：Office页面重开已见12:58最终两卡，但XLSX预览被父页面CSP拦截，且snapshotStoragePath存在却因缺旧snapshot_hash显示“无快照”。候选仅允许本地blob frame（iframe沙箱与脚本策略不变）并消费canonical快照路径，8项前端检查通过，尚未部署复验；表格既有styles.xml schema warning仍需处理，不把下载/内容通过外推完整Office通过。A2A真实PG扩展检查已通过缺产物暂停、跨user读拒绝、显式retry保留原attempt并只新增一个child。

13:25 更新：`b09310d0` 三服务已于12:52同源SUCCESS（backend/source hash `0259d6f2a55e699aea0c706858db3ab417855e1654777ac418e5bc96be573860`，1058文件）。Office原Session最终新run正常completed，两最终artifact下载/哈希/新marker/29及B7真实公式SUM(17,12)均读回，旧marker已清除；Context7两次真实查询与最终terminal提交已核实。MCP协议isError被丢弃的独立修复74passed，干净候选a073e39f尚未部署。A2A独立parser、原生完整Agent委派、不可变artifact交接、journal/API/UI已在隔离worktree实现，首轮124后端/28前端与build通过；真实PG已验证单预算/原child恢复，继续验证缺产物、retry、权限反例，尚未发布或宣称业务通过。Dynamic07、其余清单及最终清理继续，未缩减完成标准。

12:48 更新：Office 原历史失败已定位为 duplicate prepare 污染 committed aggregate，最小候选恢复 exact seal 读取且防止再污染；Context7 正式接入并审核两工具后，实调用发现动态 capability 分类缺失，候选补齐但不改权限。相关 semantic25/MCP70/RLS17通过，待部署复验。Dynamic修订07已真实逐项计算51/57，停review后继续等待/汇总消费；06的绑定缺失不冒充通过。MCP旧run cancel后留下ambiguous_provider_send/unknown，未当取消成功或重发。A2A新增Graph仍未编码，其余剩余清单不变。

12:22 续接：production `9311b5ad` 三服务SUCCESS。Local独立新会话真实pwd/exit0、完整final/ack与页面重开通过，本地CLI候选可回退、未发npm；固定v1两组参数实跑108/162，review/wait/文件消费通过。通用once正式API触发一次后停用且读回64文件；聊天入口仍被独立Plan前置拦住，旧时间请求的cancel已受理但终态尚未核实。Office最终2卡片下载通过，表内旧marker纠正仍受原Session的committed_model_seal_unavailable阻挡。Goal已观察到自动第二run完成及123674tokens计费，分阶段跨run要求仍未通过。Dynamic工具说明修正后，新模型run正确停在ready预览且run_id为空；正式确认后并行两叶done、停review，随后批准进入等待与最终文件步骤，尚未全链验完。12:12 owner明确包含新增A2A编排，12:13指定Context7，继续既有实验范围实施与接入；其余未完成项保留，不宣称全部完成。

2026-09-09 owner 在核对功能覆盖后明确要求“把真正剩下的工作一次性全部完成掉……全部完成之后和我说，要求还是那一个”。继续由单一 Codex 实施、验证、提交推送、三服务部署和真实消费验收，不再沿旧两小时批次自动停止，也不重做已通过主路径。当前剩余执行清单：固定 Workflow 实跑与复用、A2A Workflow 编排、Dynamic 并行/等待恢复；通用 once/schedule/event 与 Local 最终 result；Office 最终卡片及 Goal 自动两轮/计费复验；Memory/Growth、Hook、Skill、MCP 的未闭环路径；角色/移交/离职和最终回归/清理。权限扩大、凭据、收费、真实外发与业务政策仍保留对应 owner 决策边界，不由“全部完成”推导授权。

当前先追踪固定 Workflow 与 A2A Workflow 的真实入口，同时用既有合成 member/Agent 检查 MiniMax 新请求可用性。新合成资产前缀 `WRC-REMAINING-20260909`，仅本实验公司的合成会话、定义、受限 workspace 文件与可恢复触发器；不改真实业务数据，不创建新的凭据或权限。A2A 直接委派成功不替代 workflow 编排，固定定义生命周期成功不替代实跑；逐项结果按新证据追加，旧通过证据保留。

**第四批进行中（2026-09-09 01:54:45 起，Asia/Shanghai）**。owner 要求除明确阻塞项外完成剩余功能测试和必要修复，结束时 commit、更新文档并告知结果；新增授权覆盖“rocky的实验室”公司内经过认证的合成 Agent、对话、接口、知识与工作流实验。危险操作、真实外发、凭据/计费变更、无关数据与全库破坏性操作不在此授权内。按功能实际结果推进，不拿最终发布计数概括本轮；每个根因仍按一个方案与一次集中返修收束，阻塞单列，不阻挡独立功能。未重新启动旧 Goal/heartbeat，不夹带未接受的旧 runtime 候选。B4 明细与资产登记见[第四批记录](evidence/87b845dba4ae397bd4205b21e657e6efeb9fac7f/functional-batch-2026-09-09-04.md)。

第三批按 **2026-09-09 00:31:46—02:31:46（Asia/Shanghai）** 的有界方案收尾，**部分交付，不是两项全部通过**。Local 默认会话恢复已通过 49 项主审检查，commit/push `87b845db`，三服务同源部署 SUCCESS；01:34—01:35 正式页面刷新、离开重开均读回原批准记录、实际 `pwd` 退出 0 和 marker，未重发。本机 Hive Connect v0.1.7 只发 text、无 result，云端仍 pending/requeued；升级未获新批准，正式安装源也 unavailable，未安装或重启。once 精确草案已确认，但原 `set_trigger` 停在过期的 permission waiting、尚无 trigger；一个实现及唯一返修均未接受，主审真实 live 等待回归仍失败，候选未提交/部署。按 PDEC-015 不自动第三轮、不延长或启动下一批。CI `34256570667` 前端及 15 条机械全栈检查通过，backend harness 仍在运行。[第三批完整证据与剩余问题](evidence/aeaaacb59704ac7631da553c97d699c2cc87bdb4/functional-batch-2026-09-09-03.md)

owner 于 2026-09-08 认可[有限验收方案](../../../thinking/weekend-rc-convergence/RESULT.md)，按 PDEC-015 执行：zCode（GLM-5.3）负责前后端实现，Codex 负责 Review、集成、部署、真实 E2E 与反馈。首批已交付；owner 随后要求“先 commit，然后 git push 吧，然后我们开始下一轮结束的时候，也要有文档”。上一批文档与对应检查已 commit/push `f4575b0f`，应用源码未变。第二批 **13:38:12—15:38:12（Asia/Shanghai）两小时**，聚焦普通第二轮输入的终态回执阻塞，包含定位、必要修正、复验和文档交付；不恢复日常 CC/Kimi 门，也不恢复无限 Goal/heartbeat。

第二批下午首轮已在上限前结束：一个方案与唯一返修均未接受，当时未发布应用、未恢复两条输入，最终 NPTCR 仍 0/96。已定位 summary reconciliation 阻塞，并用本地复现排除会重复已成功摘要子调用的 whole-summary retry；按照 PDEC-015 暂停并交付策略选择，不自动生成第三版。[第二批结果文档](evidence/33f6332f663f6e648f27eb704593876c4de17053/functional-batch-2026-09-08-02.md)

22:02:49（Asia/Shanghai）owner 明确要求先 commit/push 旧记录，再重试并完成第二批；已推送两份旧记录 `ade50f74`。本次继续第二批的**显式管理员恢复入口**策略，不恢复被拒绝的自动重试候选；最多继续到 2026-09-09 00:02:49（包括部署、实测与交付）。zCode 在隔离 worktree 实现最小现有 API 消费入口，主 Codex 审查与真实恢复；同一明确策略一个实现、一次集中返修，不自动第三轮。**本次有限恢复范围已闭环**：23:11:09 最后一条 HR boundary 自然 delivered，随后只读确认 summary sealed 至 seq 896；不消耗剩余时间扩大范围。

首个试批次为 **2026-09-08 10:16:24—12:16:24（Asia/Shanghai）两小时**，包括准备、执行、记录与交付。先从不同功能域走真实入口，遇到单项失败记录最小复现后继续独立功能；仅共同入口缺陷可插入必要小修。到点交付实际结果与剩余问题，未获下一轮授权不自动续作。资源到期不是 PASS，96 条分母、PDEC-013 产品语义及最终 D/E 完成标准保持不变。

## 当前可核实结果

- 当前生产应用为 **`f0b6939e`**；最新部署、检查与功能结论以上方16:25及后续收尾记录为准。以下带旧commit/时间的项目均为历史支持证据，不是当前阻塞清单。
- 10:11历史production为 **`6c3e1a11`**（当时三服务SUCCESS），backend/backend-api/archive同1058文件、source SHA256 `f510c09697d945c4a46325abfd9e7583745ebc1004875f3bae21ad3373e286bb`。headless固定Workflow当时已能真实启动；两叶provider限流暴露subagent错误done，gate未批准、原run正式取消，无产物。后续固定v1两组实际结果108/162已通过，见[剩余执行记录](evidence/c655a4d351b5c9a17d4602f50a158bb93ef64c21/remaining-functional-2026-09-09-01.md)，不继续沿用10:11未完成判断。
- B4 当前 production application **`3ac6e2a1`** 三服务同源SUCCESS（08:34核对），backend/backend-api/干净archive均1058files/source SHA256 `0d68c1c49c3f55b90dc84c800f1725f5a583cf3aabfb3d133fc13d1e50f412c3`。本批应用修复均已push和部署；185d779d干净archive100项Goal/terminal/artifact检查、110前端检查及build通过，3ac6e2a1干净archive36项Office检查通过/2项无本机二进制skip，生产同二进制另有红绿对照。RLS指纹逐项584→588对账后仅同步精确hash，17项安全检查通过224.07s，已push `4318c490`（仅审查常量与文档，不再部署相同应用行为）；其完整CI `34296805208`仍在执行，前两次CI前端与15journeys已成功。08:35 public health degraded原始错误是Vercel sandbox_stopped410，08:39同配置单次探针复验3/3通过，未覆写持久health记录；另有旧trigger stale fence，未称零错误。旧runtime候选原样保留，未夹带。
- B4 已取得五格式个人文档/Agent消费/报告下载、公司发布/引用/下线/恢复v2、知识重建、显式记忆更正/退役/fresh排除、HR首任务归属与精确待命、临时子Agent、固定Reviewer A2A、member Team完整计算/父报告/正式关闭、内置Skill消费、clear、rewind续接、branch正式创建/历史读取的局部实证。MiniMax Workflow三叶计算/文件/父消费及完成组刷新、Plan确认后真实同会话执行/文件消费且零trigger、compact失败工具历史消费、Local审批通知深链接均已修复并生产通过。原生DOCX/XLSX校验、公式缓存29及workspace下载通过；最终旧卡片定位CLI resident未flush，3ac6e2a1生产adapter及快照helper通过，但正式模型卡片复验在首请求即被MiniMax限流拒绝。Goal暂停→继续→产物→完成通过；canonical计费/自动两轮新检查及08:36唯一continue均被MiniMax限流拒绝，08:39正式停止/无活动run。Useful反馈唯一落盘但memory held，成长UI有T0/T2/Skill候选但无成长报告，不记成长生效；临时管理员、自批政策未获答复，不扩大权限。完整范围和未测项见B4功能表，未提升为全部通过。
- B3 历史 production application 为 exact **`87b845db`**：仅在 B2 已接受源码上加入 Local 默认会话恢复。三服务部署均 SUCCESS，backend/backend-api 运行 source hash `eb7edcf72abd898a3fed7b9396280c456bd40af5edef51e11a24425efffec577` 与干净 archive 一致，public health ok、frontend HTTP 200。正式默认页刷新/离开重开恢复原 7 条事件；日志仍仅一次本地执行。49 项主审 service/API/real-PG 检查通过。once 候选及旧 dirty runtime 均未进入发布；Local 完整终态和 once 交付仍阻塞，不增加 0/96。
- 上一批 `aeaaacb5` 于 22:30 三服务部署均 SUCCESS，新增前端管理员显式终态恢复入口，复用已有 API，不改 backend/自动重试/模型。70 项主 Codex 相关检查、i18n、TypeScript/build 通过；CI `34238046718` 三 job 全部 success。23:04 经 owner 授权仅重启 backend 原部署一次，23:05 health 恢复 ok，23:07 worker/终态消费者 running；未重新构建或重启另外两服务。该 B2 历史证明不替代本批源码与业务复验。
- B2 续接：fresh 文件创建/续写/刷新和 HR 三→五要点草案修订/刷新已通过，草案已拒绝、无员工创建。原两条死信各经正式 UI 恢复一次，旧 boundary 均 delivered、原 admissions 均自然 dispatched/completed；旧文件新 74 B artifact 的预览/下载 HTTP 200/刷新重开通过。旧 HR 草案已拒绝不可原位修改，Agent 保留原记录并生成正确的替代预览，未 provision。
- **B2 收尾通过：两条原输入、最终摘要/回执、页面刷新与草案拒绝均有实证。** 22:37 起 backend 容器内外请求均超时，根因未知；上述单次重启后 HR 两轮历史、正确替代预览可刷新读回。替代 draft `7ecfd611-7c7e-41a7-b3b9-29d2a5183706` 于 23:05:50 正式拒绝，DB 与再次刷新均确认 rejected、无员工/provision。最新 boundary 经原生回收 attempt 2 于 23:11:09 delivered，summary sealed 至 seq 896；两条 Session 的四条 boundary 全部 delivered，目标 tasks 全部 completed，无 failed transcript 或残留 lease，恢复审计仍恰好两次。重启恢复可用性不是根因修复；独立 trigger 终态事务/过期 fence 错误未诊断。B2 有限恢复通过不提升最终 0/96。[B2 完整证据](evidence/33f6332f663f6e648f27eb704593876c4de17053/functional-batch-2026-09-08-02.md)
- 现有 `17f073bb` P01 正常双遍、权限负向与 Session/文件 cleanup 保留为历史支持证据，不迁移到新版本；真实断线/worker 重启恢复尚未闭环。**最终 NPTCR=0/96**。
- 第一批只记实际测试版本、真实身份、可见操作和结果；入口点击不等于整个 Journey，通过数在完整要求完成前不提升。[第一批证据](evidence/17f073bb4f07098e55d9ef1684781dc67cfa454e/functional-batch-2026-09-08-01.md)。
- 第一批实际操作覆盖个人知识、文件交付、HR、Session 命令、角色 API、Automation、Local Agent、后台/设置八类入口。跑通个人知识粘贴/检索/归档排除/恢复重现、Markdown 预览/下载/刷新重开、MiniMax HR 草案生成/拒绝、已保存 Session 的 context/permissions/usage 面板及部分角色 API 正负向；完整格式/角色/故障要求未跑完，不把八类入口记成八条 Journey PASS。
- `33f6332f` fresh GLM Session 真实调用 `search_personal_kb`、`read_personal_kb`，读回唯一合成文档两段，采用修正后的 12 而非旧值 10，并写读真实报告。2m43s 完成，canonical terminal 已提交；报告预览/下载 HTTP 200、刷新重开与本地文件内容均核对。最终已归档该合成知识文档，唯一标记搜索零命中；报告及失败现场留作证据，未宣称完整 cleanup。
- CEDAR R2 `member` 与 GROVE R3 `org_admin` 已经正式 API 登录重新核对 exact tenant；fixture 员工 Agent GET 200、平台公司后台 403、跨租户测试 Agent 与跨个人合成文档均 404。Chrome 旧员工 UI 为缓存，刷新后为平台管理员；本批次未完成员工/公司管理员 UI 登录，API evidence 不冒充 UI evidence。
- 第一批 11:47 时，两条普通第二轮输入（文件续写、HR 修订）被旧 turn_stop boundary 的 dead_letter/attempt 8/`WebTerminalBoundaryPending` 阻挡，app_rls/read-only/tenant-scoped 对账确认 `waiting_for_terminal_boundary_ack`。这一历史阻塞现已通过上述两次显式恢复推进，不保留为当前 pending 结论。新 draft `/context` 的 422、Local Agent 仅到 `approval_required`、单次自动化未创建仍是未闭环项；不把 B-Worker 改回未 ready 的 DeepSeek。
- zCode 工具加载小修复及一次集中返修由 Codex 审查，343 targeted checks 与 Ruff/format/diff check 通过；commit/push `33f6332f` 仅两文件，CI `34182826176` 三 job success，三服务已同源部署且完成上述真实 KB 消费。旧 KB 失败与此源码缺陷的因果关联并非唯一解释；第二轮输入仅做有界只读定位，不加新代码返修。
- 第八轮 zCode 候选已取消；未审查的应用修改原样保留，未接受、未提交、未部署。本批次不要求先救完该候选。原 heartbeat 保持 PAUSED，无新 Goal。

## 仍未完成 / 不重做

- 全部 96 条最终 D 双遍、真实故障恢复、完整角色/权限负向、rollback、cleanup 与 evidence-only E 未完成；本批次不能替代它们。
- Memory/Growth、J4 bakeoff、多格式知识/文件交付、HR/首任务、协作/工作流/A2A、Automation/Hook/Skill/MCP/Local Agent 和 selected-model compatibility 仍按原合同逐项验证，不从分母删除。
- M0、managed-shell、ChannelConfig/Feishu、inactive-tenant、post-claim/defer-order、PDEC-013、后台返回 App、折叠设置、知识库错误披露及历史 projection recovery 等已实现/部署结果不重建；当前生产业务消费仍须据实记录。完整历史及未关闭 finding 见[原状态快照](archive/current-status-before-functional-batch-2026-09-08.md)与 [Findings](05-findings.md)。
- DeepSeek 缺 billing/credential readiness 不盲重试；MiniMax/GLM 旧 bounded probe 不冒充 P33 PASS。旧 reviewer/CI/部署绿不迁移成 Journey Closed。
- 保留 owner 无关 dirty/untracked 文件和所有未接受候选；按07:25新指示仅Codex在真实失败与既有合同范围内修复，不再委派zCode或CC。

## 权限与执行规则

沿用 owner 2026-09-06 授权：仅 supported-path、经过认证、先登记且可回收的合成生产操作；允许本任务必要修正、commit/push、CI、三服务同源部署和 cleanup。无新充值/凭据、伪造身份、直接 role/tenant DB 修改、RLS weakening、真实外发或无关数据效果。浏览器动作仍遵守具体效果的确认要求。

每包默认一个实现方案、一次集中反馈后的返修；同根因两次仍失败就交付策略选择/明确阻塞，不改名重置预算。新增反馈须有合同与可达路径依据；本包回归和可信严重危害仍阻止受影响发布。优先行为测试，普通迭代不重复全量门；等待用完成通知/有界等待与退避小状态，不做 15 秒模型轮询。

## 唯一下一动作

提交本轮收尾记录，向owner交付确切未闭环清单与待决边界；不自动新建第三轮整链、改变自审政策或升权。once04已实际文件消费，不再等待或重发。父会话消费需明确原页的受支持恢复策略，不能直接重置DB；Growth保留实际矛盾，活动Skill缺卸载入口，角色转移仅有只读预览。队列event/委派depth已部署，A2A02第三节点已retry完成，最终报告已真实下载，不再执行历史下一动作。Office、Goal和固定Workflow已通过路径不重跑。细节与精确资产见[剩余执行记录](evidence/c655a4d351b5c9a17d4602f50a158bb93ef64c21/remaining-functional-2026-09-09-01.md)。

此前交付背景（不替代上述下一动作）：

owner随后明确“接下来都只剩Codex一个了……没有什么zCode，也没有什么CC……继续吧”。由主Codex独立实现、验证、集成与交付，不再委派其他作者。本次集中修复和RLS指纹已提交推送，模型端到端复验停在明确限流；MiniMax恢复后再做Office最终卡片及有新时间窗口的Goal两轮复验，不重放已停止的旧Goal。Plan/compact/Workflow显示/Local通知修复均已生产复验，不重复修建。误建测试trigger已暂停，不重放确认、不扩展管理员权限。B3 once/Local仍需按既有失败证据决定修复，不以换作者清零旧返修记录。完整成长、角色/转移/离职、时序与外部渠道仍没有业务通过证据，不把未测项改名已完成。第四批仍未全功能PASS。

owner 自行修改的共享摘要模型 `zhipu / glm-5.3` 已实时核对。首版无限重试、返修 whole-summary 429 重放仍保持拒绝；本次实现的是明确操作员意图与重算风险确认的既有 API 消费入口。两次恢复均有正式 audit，未提取浏览器 token、伪造身份或手改 DB。完整历史见 B2 文档；目前仍没有完整单旅程吞吐样本，不能可靠外推 96 条总工期。

## 当前合成资产登记
| marker | 目标与允许效果 | 禁止效果 | cleanup 状态 |
|---|---|---|---|
| `D3-SETTLEMENT-C37-8K4P` | EventPilot synthetic Session；已新建/读回一个 marker 文件 | 不外发、不建 workflow/trigger/delegation、不读 credential；write failure 不重试 | `created-evidence-retained`；已登记 final cleanup，删除前 exact-target/readback |
| `P01-MAIN-CLEAN-P1-3482B-LARCH-927` | 历史无效入口 probe；run 成功但不是 frozen fresh Session | 不修改其他路径或外部系统 | `invalid-entry-evidence-retained`；永不计 PASS，待 final cleanup |
| `P01-MAIN-PASS1-3482B-MAPLE-581` | 历史功能 probe；实际 principal 为 platform_admin | 不外发、不读 credential、不外推 employee persona | `invalid-persona-evidence-retained`；永不计 PASS，待 final cleanup |
| `UI-CMD-003-PROBE` | read-only `/context`、`/usage`、`/permissions` probe | 不调用 provider/tool、不改权限 | `failure-evidence-retained`；待 final cleanup |
| `WEEKEND-RC-ROLE-FIXTURE-1B4BE5D2` | 通过公开 register/assignment/join 与正式 role/permission API 建立 synthetic company-admin、employee、scoped-operator | 不复用真实邮箱/密码、不跨 tenant、不外发、不读取/修改 provider credential；禁止 forged token、直接 DB role mutation 或 RLS weakening | `supported-path-created`；production 已记录双 org-admin、双 member 与 operator candidate，但 tracked artifacts 不保存登录材料或 exact principal/grant 清单；须复用安全登录态或 action-time supported login，final cleanup pending |
| `WRC-M1-EMPLOYEE-20260904-CEDAR` | 原计划经正常注册与 member invitation 加入 fixture 的合成 employee | 不猜测、重置或复用未知密码，不升级管理员、不外发 | `legacy-registered-credential-unrecoverable`；未用于当前验收，保留为 final cleanup exact target |
| `WRC-M1-ORGADMIN-20260904-GROVE` | 正常注册后由平台后台 assign-user org_admin 加入上述既有合成公司，仅用于成员邀请及公司管理员验收；不把 CEDAR 升级 | 不新建 tenant、不改其他用户权限、不读取或修改 provider/组织 secret、不外发；保留 In-app owner 登录，临时密码仅在操作内存 | `org-admin-assigned-credential-unrecoverable`；username `wrc_m1_admin_20260904_grove`，synthetic mailbox 同名 `@example.com`。owner当场授权后经production正式确认框提交，fixture member count `6→7`；Codex未持久化创建密码且当前产品无无需旧密码的受支持重置路径，因此该principal不能用于后续登录，保留为final cleanup exact target。条款确认遗漏已披露 |
| `WRC-M1-ORGADMIN-20260906-GROVE-R2` | 经production正式注册并由后台赋予同一fixture `org_admin`的替代候选 | 不猜测或重置密码、不新建tenant、不改其他用户权限、不外发 | `org-admin-assigned-login-unusable`；username `wrc_m1_admin_20260906_grove_r2`。Chrome密码管理器覆盖了注册时的预期密码，原Keychain值独立登录返回401，错误Keychain item已删除；该 principal 不再用于验收，保留为 final cleanup exact target |
| `WRC-M1-ORGADMIN-20260906-GROVE-R3` | 经production正式注册并由平台后台赋予同一fixture `org_admin`，用于成员邀请与公司管理员验收 | 不新建tenant、不改其他用户权限、不外发；凭据只存macOS Keychain，不写仓库、聊天、命令输出或普通文件 | `org-admin-usable`；username `wrc_m1_admin_20260906_grove_r3`，Keychain service=`hiveclaw-weekend-rc-production-grove-r3`。独立登录响应核对 `role=org_admin` 与 exact tenant，Chrome Settings 显示“公司管理员”；已生成恰一次、最大使用次数1的 CEDAR R2 invitation。final cleanup pending |
| `WRC-M1-EMPLOYEE-20260906-CEDAR-R2` | 经production正式注册并消费 R3 生成的一次性 member invitation 加入同一fixture，用于普通员工验收 | 不升级管理员、不外发、不读provider/组织secret；凭据只存macOS Keychain，不写仓库、聊天、命令输出或普通文件 | `employee-usable-api-verified`；username `wrc_m1_employee_20260906_cedar_r2`，Keychain service=`hiveclaw-weekend-rc-production-cedar-r2`。09-08 正式 API 登录重新核对 `role=member` 与 exact tenant；此前 Chrome“成员”为旧缓存，当前 UI 不作为员工证据。历史 HR provisioning 与 P01 evidence 保留。final cleanup pending |
| `WRC-P01-HR-MODEL-GATE-20260906` | CEDAR R2 从唯一 HR 入口提交一次合成员工简报，验证 P01 最小前置 | 禁止重试non-retryable run、禁止伪造模型/Agent、禁止读取或跨tenant复制API key | `failure-evidence-retained`；HR Session `cae41732-3a3b-41b9-9ff2-477d5befcc9c`，输入 marker=`WRC-P01-EMPLOYEE-AGENT-R1-20260906`，终态“失败 / 当前 Agent 尚未配置模型 / 不可重试”。无 blueprint、provider call、tool effect 或新员工；final cleanup pending |
| `WRC-P01-HR-PROVIDER-BILLING-20260906` | owner 配置并绑定 GLM-5.3 后，由 CEDAR R2 fresh HR Session 原样提交一次受限简报 | provider 不可用时不重试、不充值、不换或读取 credential；禁止伪造 blueprint/Agent | `historical-external-unavailable-evidence-retained`；HR Session `8544a582-e995-4adc-9ff0-e1f3d9e6f4d6` 显示实际模型 GLM-5.3，终态“失败 / 模型额度或余额不足 / 可重试”。后续 fresh Session 已证明 provider readiness 恢复；本失败 run 未复用，final cleanup pending |
| `WRC-P01-HR-PREVIEW-20260906` | CEDAR R2 fresh HR Session 经真实 GLM-5.3 生成 exact synthetic Agent blueprint，并在owner授权后只消费该draft | 禁止额外 Skill/MCP/connector、外部消息/外网/凭据/其他Agent/workflow/trigger/automation/公司级数据 | `confirmed-and-provisioned`；Session `3b0b01e8-3819-455d-bade-58a042323845`、blueprint `048a0ec3-e19a-449b-95cb-3e3f59317722` v1/hash `bp_133714e0424802b944e4cf77`；task `ce3dad21…` attempt 1七步完成，created Agent=`4e5261a6-c182-5248-9ca1-669f9419d44f`。final cleanup pending |
| `WRC-P01-CEDAR-WORKER-R1` | CEDAR R2经canonical HR provisioning创建的P01专用Agent，只执行受限workspace开放任务 | 不外发、不访问credential/公司级数据/其他Agent，不装plugin/MCP/connector，不建workflow/trigger/automation | `created-evidence-retained`；Agent `4e5261a6-c182-5248-9ca1-669f9419d44f`，GLM-5.3、standard/request-approval、自用可见、9 default skills、plugin/MCP/external snapshot为0。final cleanup pending |
| `P01-MAIN-PASS1-CEDAR-7K9M-20260906` | employee fresh Session完成公开plan、Ledger、唯一workspace artifact写读和hard reload | 同上且不重复业务effect | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS1-CEDAR-ASTER-20260907` | employee fresh Session完成A/B开放决策、公开plan、5/5 Ledger、唯一workspace artifact写读与terminal receipt后hard reload | 不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS2-CEDAR-BIRCH-20260907` | employee fresh Session完成C/D开放决策、公开plan、6/6 Ledger、唯一workspace artifact写读与terminal receipt后hard reload | 不执行真实切换，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-NEGATIVE-CEDAR-ELM-20260907` | employee fresh Session只尝试一次越界写，并继续唯一允许workspace写读 | 不重试越界效果，不读被拒路径，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS1-CEDAR-FIR-20260907` | exact `cc152f66` employee fresh Session完成A/B开放决策、公开plan、6/6 Ledger、唯一workspace artifact写读与terminal receipt后hard reload | 不执行真实切换，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS2-CEDAR-GALE-20260907` | exact `cc152f66` employee fresh Session完成C/D开放决策、公开plan、5/5 Ledger、唯一workspace artifact写读与terminal receipt后hard reload | 不执行真实演练或排班，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-NEGATIVE-CEDAR-HARBOR-20260907` | exact `cc152f66` employee fresh Session只尝试一次越界写并继续唯一允许workspace写读 | 不重试、读取、编辑或探测越界路径；不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS1-CEDAR-IRIS-20260907` | exact `17f073bb` employee fresh Session完成A/B开放决策、公开plan、6/6 Ledger、唯一workspace artifact写读、自然terminal receipt与hard reload | 不执行真实维护，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-PASS2-CEDAR-JUNIPER-20260907` | exact `17f073bb` employee fresh Session完成C/D开放决策、公开plan、5/5 Ledger、唯一workspace artifact写读、自然terminal receipt与hard reload | 不执行真实维护，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-NEGATIVE-CEDAR-KELP-20260907` | exact `17f073bb` employee fresh Session唯一越界写typed deny/零效果/未重试探测，同run唯一合法workspace写读、6/6 Ledger、自然terminal与hard reload | 不换写法或探测越界路径，不外发、不访问credential/公司数据/其他Agent，不建workflow/trigger/automation | `cleanup-verified`；该精确Session及workspace文件均已删除，RuntimeTask/terminal outbox保留；见current D P01 cleanup证据。历史pass不迁移 |
| `P01-MAIN-RECOVERY-CEDAR-LINDEN-20260907` | 当前D employee新合成Session `9dd7289e-14c6-4ec5-8e40-5838f9e71874`、run `a08158a3-9815-5f17-b3fa-e3941ff80870`用于断线/worker restart恢复 | 仅既有六工具；一次workspace文件写入，不外发/不访问凭据或正式数据；重启前核对无关活跃run | `failed / SESSION-WORKER-RESTART-ROUND-001`；真实重启后attempt2撞round1并失败，唯一write/input保持；terminal outbox attempt1自然delivered。保留现场，修复后验证与cleanup |
| `P01-STAGE1-FRESH-FALCON-682` | EventPilot fresh production Session 的当前提交功能 truth test；3-step plan、Work Ledger、一次 write/read、硬判据 deliverable | 不外发、不调其他 Agent/外网/workflow/trigger/delegation、不读 credential；仅允许目标 `workspace/` 文件 | `completed-supporting-evidence-retained`；Session `65b98e1a…` 与唯一 artifact 已登记 final cleanup；platform-admin evidence 永不冒充 employee PASS |
共享合成 fixture 保留到所有依赖旅程完成；lane-local transient effect 在 reconciliation 后清理；final `D` 双遍结束后清理全部 Goal-created synthetic assets。owner Example Owner 基础账号、immutable evidence 和无关数据永不作为 cleanup target。

## 本批次新增合成资产预登记

前缀 `WRC-FUNCTIONAL-B1-20260908`。允许在 Example Owner 实验 scope 内创建个人知识测试文档、隔离 Agent Session、生成文件和不向外发送的单次自动化任务；仅使用虚构内容，不读取真实业务内容。每个实际 ID/路径/状态和 cleanup 记入[本批次证据](evidence/17f073bb4f07098e55d9ef1684781dc67cfa454e/functional-batch-2026-09-08-01.md)。当前预登记不是已创建证明；先确认账号/公司再提交。保留共享 fixture，不把真实账号或历史证据当 cleanup target。
