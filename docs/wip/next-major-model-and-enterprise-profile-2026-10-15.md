# 模型支持矩阵与企业安装画像（M0-G）

> 日期：2026-10-15。源码身份：`main` `41bf3627d151d5e83c99ff67953f1343ef91b2ee`。委派：`6dbb93102fa14a37b2887a3265d18174`。
>
> 状态：计划中的 M0 调查。没有改应用、schema、UI、安装器或计划 §12。没有调用付费模型，没有加载 owner `.env`，没有启动 Compose、数据库或 Linux 安装。仓库声明和历史验收都不是当前支持承诺。M0 没有完成。M0-F 的 OpenConnector 采用门仍是 FAIL，本批不重开。M0-D 的环境采用门仍未通过。Linux 按 owner「linux先不测试」延期，静态阅读不是安装结果。

本地证据：原快照 [registry-extract.json](../../.git/next-major-2026-10-14/m0-g/registry-extract.json)、[source-manifest.json](../../.git/next-major-2026-10-14/m0-g/source-manifest.json)、[commands.txt](../../.git/next-major-2026-10-14/m0-g/commands.txt)。G1–G3 修正见 [revision1/reconstruction.md](../../.git/next-major-2026-10-14/m0-g/revision1/reconstruction.md)。组织与黄金任务沿用 [M0-E 合同](next-major-organization-provenance-contract-2026-10-15.md)。安装目标沿用 [计划 §1、§5、§7–§10、W9](next-major-version-development-plan-2026-10-14.md)。

> 🧠 **From Hindsight memory (下一版本：团队数字员工产品化)** — 历史页把 M0 记成修复、合同和支持矩阵，并保留 Ling 失败。该页不是当前实现证明。下面的矩阵和画像以本修订的源码和已引用的验收原文为准。

## 1. 分开的结论

**源码事实。** 运行时只有四套客户端：Anthropic、OpenAI Responses、Gemini、以及其余全部走 OpenAI-compatible。`PROVIDER_REGISTRY` 声明 15 个 provider id 和一组推荐模型字符串。租户真正使用的是 `llm_models` 里的任意 `provider` + `model` 字符串，不是推荐名单。未出现在注册表里的 provider 仍会落到 OpenAI-compatible 客户端。配置名称、管理页 Test、以及 mock 测试都不是已证明可用的模型。

**历史证据。** 2026-09-09/10 的八步记录保留 Ling 语义消费 FAIL，并记录一次已恢复绑定的 MiniMax-M3 对照。那不是当前生产状态，也不是全模型通过。本批没有重放。

**本次检查。** 只读源码，并用不导入应用的 AST 抽出注册表。七个基线文件里六个哈希与开工快照一致。计划文件哈希已变，本 worker 没有写它。

**建议。** 下一应用批先修源码已经确认的选模缺口，排在 M1 身份工作之前。本批不实现。Web 和心跳在已配置的主模型行缺失时提升显式备用；Web、渠道、心跳读取普通主模型和备用时都不过滤 `enabled`。渠道已经先检查缺失的主模型。保留只配置备用的 Agent 语义，以及显式错误路径上的备用。M0-E §4.1 第 1–2 步的身份干跑留在这次选模修正之后。Ling 保持 `known_semantic_consumption_failure`，直到同题、同绑定、同证据的授权重放改变它。

**仍开着的门。** 模型花费授权、租户测试配置和凭据未建立，本批未检查。提供方余额 unknown。设计伙伴与企业 IdP unknown。Linux 按 owner 延期。Codex 审查时只读 `command -v` 看到本机 Codex 与 Claude CLI；Hive 集成和测试设备就绪未检查。飞书、钉钉、企微、远程 A2A 未提供或未检查。这些都不是本批观察到的外部故障。M0 未完成。

## 2. 模型矩阵

注册表在 `backend/app/services/llm_client.py` 的 `ProviderSpec` / `PROVIDER_REGISTRY`（约 2289–2646 行）。AST 抽出 15 项，源文件 SHA-256 `7246f82d453812c12a4a0ff2a09168745b5994399f18069b0de63d3af4eadf68`。别名：`moonshot`→`kimi`，`glm` / `zai` / `z.ai`→`zhipu`，`openai_response` / `openairesponses`→`openai-response`。

`create_llm_client` 按协议选择客户端。`openai` 且模型 id 以 `gpt-5.5` 开头时，即使用户选的 provider 是 `openai`，也会改走 Responses 客户端。注册表之外的 provider 不拒绝，而是 OpenAI-compatible，缺省 base URL 落到 `https://api.openai.com/v1`。

| 家族 | provider id | 协议 | 仓库声明的推荐模型 id | 推理策略 | 本次证据 |
|---|---|---|---|---|---|
| Anthropic | `anthropic` | `anthropic` | `claude-opus-4-8`，`claude-sonnet-4-5`，`claude-opus-4-7` | `anthropic_thinking`；`tool_choice` 关闭 | 仅声明 |
| OpenAI Chat | `openai` | `openai_compatible` | `gpt-5.5`，`gpt-5.4`，`gpt-5.4-mini` | `openai_chat_reasoning`；`gpt-5.5` 实际走 Responses | 仅声明 |
| OpenAI Responses | `openai-response` | `openai_responses` | 同上三个 id | `openai_responses` | 仅声明 |
| Azure OpenAI | `azure` | `openai_compatible` | 无 | `none`；默认 URL 为空 | 仅声明 |
| DeepSeek | `deepseek` | `openai_compatible` | `deepseek-v4-flash`，`deepseek-v4-pro` | `deepseek_thinking` | 仅声明。历史余额失败见 §4，不是当前余额 |
| Qwen | `qwen` | `openai_compatible` | `qwen3-max`，`qwen3-coder-plus` | `qwen_thinking` | 仅声明。`qwen-plus` / `qwen-long` / `qwen-turbo` / `qwen-max` 只是输出上限前缀，不是推荐 id |
| MiniMax | `minimax` | `openai_compatible` | `MiniMax-M3`，`MiniMax-M2.7` | `minimax_reasoning_split` | 仅声明。历史一次语义对照见 §4 |
| OpenRouter | `openrouter` | `openai_compatible` | 无 | `none` | 仅声明。Ling 历史绑定走这里，见 §4 |
| Zhipu | `zhipu` | `openai_compatible` | `glm-4.7`，`glm-5` | `glm_thinking` | 仅声明。历史配置里的 `glm-5.3` 不在当前推荐名单 |
| Gemini | `gemini` | `gemini` | 无 | `none`；`llm_reasoning.py` 没有 Gemini 分支 | 仅声明 |
| Kimi | `kimi` | `openai_compatible` | `kimi-k2-thinking`，`kimi-k2.5` | `kimi_thinking` | 仅声明 |
| 本地兼容 | `vllm`，`ollama`，`sglang` | `openai_compatible` | 无 | `none`；默认 URL 指向本机端口 | 仅声明。不是离线企业决定 |
| 自定义 | `custom` | `openai_compatible` | 无 | `none`；默认 URL 为空 | 仅声明 |

租户可配置的是 `llm_models` 一行，不是上表。字段在 `backend/app/models/llm.py`：`provider`、`model`（最长 100）、加密后的 API key、`base_url`、`label`、`enabled`、`supports_vision`、输入/输出 token 覆盖、温度、reasoning mode/effort/budget/display、`preserve_reasoning`、`text_verbosity`、`provider_options`。公司默认模型是 `TenantSetting.key = default_model_id`。Agent 保存 `primary_model_id` 与 `fallback_model_id`。写入前 `validate_tenant_model_references` 要求目标行属于本租户且 `enabled`。

管理入口：`GET /api/enterprise/llm-providers` 返回注册表；`GET/POST/PUT /api/enterprise/llm-models` 读写租户行；`PUT /api/enterprise/llm-models/default` 设置默认。`POST /api/enterprise/llm-test` 会用真实 key 发一条 “Say 'ok'” 请求。本批没有调用它。Test 成功也只说明这一条探测返回了正文，不证明工具、流式、推理或黄金任务。

前端 `EnterpriseSettings.tsx` 在 provider 列表为空时使用内嵌 `FALLBACK_LLM_PROVIDERS`（约 70–86、438 行）。这份降级名单多一个注册表没有的 `baidu`，少 `openai-response`，并且 Anthropic/DeepSeek/Qwen/Zhipu/Kimi 的 `default_max_tokens` 与注册表不一致。API 正常返回时不以它为准。若空列表期间把 `baidu` 存成租户行，运行时会按未知 provider 走 OpenAI-compatible。

推理请求由 `llm_reasoning.build_reasoning_kwargs` 翻译。OpenRouter、Azure、Gemini、vLLM、Ollama、SGLang、custom 没有专用分支；只有持久化的 `provider_options` 会原样并进请求。MiniMax 只在 `preserve_reasoning` 为真时加 `reasoning_split`。工具面：注册表里只有 Anthropic 关闭 `supports_tool_choice`。流式、工具和推理都在对应客户端的 `stream` 路径上拼装；本批没有实呼，所以这些路径停留在“已接线”，不是“已证明”。

## 3. 选中模型保持，以及失败时怎么走

Web 任务在 `web_chat_runtime._resolve_runtime_models_for_task` 解析模型。`RuntimeTask.metadata.runtime_model_id` 可以覆盖本轮，但必须是本租户且 enabled，否则任务直接 `RuntimeError`。没有覆盖时用 Agent 主模型；主模型行按 id 和租户读取，**不**过滤 `enabled`。因此后来被禁用的主模型仍会被用上。主模型行不存在、而备用行存在时，Web 把备用升成主模型，不报“主模型不可用”。两边都没有时，`web_chat_run_orchestrator._handle_pre_invocation_terminal` 以 `failure_code=llm_model_missing`、`delivery_state=unavailable`、`requires_user_decision=true`、`retryable=false` 结束，并且不改用公司默认模型。

`choose_runtime_model_pair` 明确丢掉传入的公司默认模型（`del default_model`）。它只在主模型缺失时提升**已经配置的**备用，并在两者是同一行时去掉备用。渠道代码里“已加载公司默认备用”的日志因此不会因默认模型而触发。公司默认模型被 HR 创建等直接调用 `resolve_default_model_for_tenant` 的路径使用，不是 Web/渠道回合的暗中替换。

飞书等渠道的 `channel_agent_runtime.call_agent_llm` 在提升备用之前调用 `primary_model_unavailable`。主模型 id 已配置但本租户查不到行时，返回中文修复提示，不发模型请求。心跳没有这道检查：主模型行缺失且备用存在时，直接改用备用；两者都没有才以 `skip_reason=model_not_found` 跳过。这是下一修复批的边界，本批不改：

- Web：`_resolve_runtime_models_for_task` 的消费者是 Web 聊天 RuntimeTask。
- 心跳：`heartbeat.py` 约 1306–1343 行的消费者是心跳 RuntimeTask。
- 渠道：`call_agent_llm` 已经 fail-loud。
- 写入侧已经拒绝 disabled/跨租户引用；读取侧除了 `runtime_model_id` 覆盖，都不看 `enabled`。

`LLMError` 要先过传输对账，才可能换备用。`backend/app/kernel/turn_orchestrator.py`（SHA-256 `641f70f5ad4307d793c8c0791f92bae7ccbc6ce4db0fc970d62f733e19c95e7d`）2050–2076 行里，`classify_llm_error` 只产生展示类别和 `requires_user_decision`，不是传输裁决。`retry_safe` 仅当异常自己的 `delivery_state == "rejected"`。已经有 `provider_request_id` 且 `retry_safe` 为假时，含 `delivery_state=unknown`，2072–2076 行先抛 `ProviderRequestNeedsReconciliation`，执行到不了 2379–2421 行的备用。备用只发生在这道栅栏没有抛出之后：没有持久请求 id，或投递已是 `rejected`；并且显式备用存在、当前模型仍是请求模型、`should_surface_without_model_fallback(exc)` 为假。该函数等于分类上的 `requires_user_decision`。额度、余额、普通 429、认证、模型不存在和 400 类拒绝因此停在原模型。`529` / `overloaded` 同样标成 `rate_limited`，但 `requires_user_decision` 为假（`llm_error_policy.py` 73–78 行），普通 429 则为真（80–91 行）；分类放行仍然过不了上面的传输栅栏。超时、连接、上下文过长和其他未分类 `LLMError`，只有在没有持久请求 id 或投递已明确拒绝时，才会发一次 `runtime_fallback` 并改用显式备用。切换不写回 `primary_model_id`。非 `LLMError` 的意外异常在 2437 行另有备用分支，前面没有同一道对账抛出。以上是源码顺序，不是一次实呼。

每次真正发请求前，`turn_orchestrator` 用当时的 `active_model` 调用 `prepare_model_request`。快照含 `provider`、`model`、messages、tools、temperature、max_tokens 和 reasoning。`delivery_state=rejected`（HTTP 400/401/402/403/404/413/422/429）记 `retry_safe=true`。其他传输状态是 `unknown`，并且在已有 provider request id 时进入 `ProviderRequestNeedsReconciliation`，不盲目重放。`classify_llm_error` 对 HTTP 402 只看状态码；其余类别仍含正文关键字，那是展示分类，不是语义对错门。

## 4. Ling：原文证明了什么

当前源码没有 `ling` 或 `inclusionai` 字符串。Ling 不是注册表里的 provider 或推荐 id。历史绑定是租户行上的 OpenRouter 模型。

保留 FAIL 的原文是 [03-current-status.md](../acceptance/2026-08-30-weekend-rc/03-current-status.md) 第 6 步和第 46 行，细节在 [remaining-functional-2026-09-09-01.md](../acceptance/2026-08-30-weekend-rc/evidence/c655a4d351b5c9a17d4602f50a158bb93ef64c21/remaining-functional-2026-09-09-01.md)。当时源码身份是 `bccda8c7`，状态页后来的发布是 `4fb087dd`。本批不把它们当成今天的生产。

原文**建立了**这些事：

- 原中文问题、原 Ling 绑定的 fresh 复验失败。Session `00000072-0000-4000-8000-000000000000` / run `000000f8-0000-4000-8000-000000000000`：模型读到了 Useful 反馈和 143→286，仍以缺少中文“分两轮”标签否认关联。
- Owner 只批准一次 MiniMax-M3 对照。Session `0000003c-0000-4000-8000-000000000000` / run `000000d9-0000-4000-8000-000000000000` 在同一中文问题下对应了反馈、143→286、记忆路径和源 Session。对照后 PATCH/GET 确认 MemberAnalyst 回到 Ling，公司默认模型没变。这次授权已消费。
- 两个模型的历史检索都出现来源权限拒绝。拥有 Session 不等于拥有其中所有引用。没有绕过 ACL。
- 去重提交 `3213c1f5` 明确不宣称治愈 Ling。
- 绑定是 `openrouter / inclusionai/ling-3.0-flash-sante:free`。同一时期 Ling 曾实际调用 `execute_code` 并有 canonical 终态；工具回执无输出，当时不作为计算成功。
- 第二次语义失败：Growth 纠错 run `0000031d-0000-4000-8000-000000000000`。Ling 仍把“分两轮”和已读到的 143→286 自动续轮当成两件事。原文停止继续改写问题、改记忆或换模型。

原文**没有**建立这些事：

- 没有建立适配器崩溃、工具缺失或流式中断。失败发生在模型已经读到证据之后的语义判断。
- 没有保存那一次请求的完整 `model_request_snapshot`，所以不能从原文断定证据是否被截断、推理参数是否为空。按**今天**的代码，OpenRouter 没有推理分支，默认输出 4096、登记输入上限 128000，除非该租户行另有覆盖。这只是下一诊断要核对的当前形状，不是对 9 月请求的还原。
- 没有证明所有 Ling 变体、所有问题，或 MiniMax 长期可用。后续 MiniMax 限流是另一些回合，不能改写这一次对照。
- DeepSeek 的历史 `HTTP 402`、GLM/MiniMax 的有界 health Test，是当时部署上的探测，不是本矩阵的通过记录。

**最小下一步，本批不做。** 保持原问题、原证据覆盖和原绑定 `openrouter` + `inclusionai/ling-3.0-flash-sante:free`。不设关键词正确门，不换模型，不改记忆。获得额度授权后，只重放这一问，并留下发送前快照：证据句子是否在 messages 里、实际 `max_tokens`、reasoning 是否为空。证据不在或被截断，下一修复只碰这条绑定的请求组装和预算。证据在而回答仍把两件事拆开，失败就仍是该模型的语义消费；支持矩阵继续标开放失败，由 owner 决定兼容修复范围。在那次回放之前，历史 FAIL 保持不变。

## 5. 企业安装候选画像

候选名：`EP-1-compose-collapsed`。它是本仓库唯一把前端、后端、PostgreSQL、Redis 写在一起的拓扑，即当前 `docker-compose.yml`。它不是硬化安装器，也不是已执行的安装。本批没有渲染 Compose，因为在仓库目录渲染会读取 owner `.env`。

| 部件 | 候选里实际写了什么 | 代码还支持、但候选没有单独做成的 |
|---|---|---|
| 前端 | `frontend/Dockerfile`：Node 22 构建，`nginx:alpine` 监听 80。Compose 把 `${FRONTEND_PORT:-3008}` 映到 80 | 源码模式 README 是本机 3008/8008，不是这条镜像 |
| API | 不单独存在。`BACKEND_HOST=backend:8000` 把 nginx 里的 `backend-api:8000` 和 `backend:8000` 都改到同一个容器 | `HIVE_PROCESS_ROLE=api` 跳过 schema 写入，只做只读 readiness，然后 uvicorn :8000 |
| Worker | 同一个 backend 容器。角色默认 `runtime`，进程内 `RUNTIME_TASK_WORKER_ENABLED` 默认为真 | 无卷的控制面应设 `api`。历史 Railway 把 runtime 与 api 分成两个部署；那是旧记录，不是本候选的安装定义 |
| PostgreSQL | `postgres:15-alpine`，库/用户 `hive`，口令 `hive`，卷 `pgdata`，不对宿主机发布端口 | 运行时应有 owner 用的 `SCHEMA_DATABASE_URL` 和非超级用户 `app_rls`。`RLS_APP_PASSWORD` 才会创建该角色 |
| Redis | `redis:7-alpine`，无口令，卷 `redisdata` | 只做协调和 `hive:runtime_tasks:wakeup`。任务真相在 PostgreSQL |
| 文件 | `AGENT_DATA_DIR=/data/agents`，绑定 `./backend/agent_data` | 没有对象存储。`storage_lifecycle` 的 `stage`/`backups` 是工作区事务目录，不是数据库备份 |
| 环境 | 不包含 | M0-D 未采用。本地与 Vercel sandbox 代码在，不选进本画像 |
| 模型 | 镜像不内置模型 | 安装后由租户写入 `llm_models`。没有获准额度就停在 readiness |
| 企业身份 | 不包含选定的 IdP | 代码有本地注册、飞书、按租户的 OIDC。客户用哪一种是 unknown |

版本与源码身份：应用 `backend/VERSION` 与 `frontend/VERSION` 都是 `1.7.0`。Compose 后端用 `backend/Dockerfile`（Python 3.12-slim，uv 0.9.21，`uv.lock`）。根 `Dockerfile` 另含 Node 20 和 `@larksuite/cli@1.0.98`，`railway.json` 指向它；Compose 不用这张镜像。前端构建是 Node 22。架构、离线、国产硬件均未验证，保持 unknown。网络：只有前端端口发布；后端、数据库、Redis 在 `hive_network` 内。Compose 还绑定 Docker socket。没有仓库内的 TLS 终结。CORS 默认是本机来源。

秘密前提：`SECRET_KEY` 与 `JWT_SECRET_KEY` 在 Compose 里有 `change-me-*` 默认值；`SECRETS_MASTER_KEY` 默认为空。README 要求换掉前两项并备份主密钥。这些默认值使当前 Compose **不能**称为禁止默认高权凭据的企业安装。第一个注册用户会成为 `platform_admin` 并确保 slug `default` 的租户存在（`backend/app/api/auth.py` 约 121–135 行）。这也不是企业 IdP 配置。

干净安装、升级、备份和回退，代码里已经有的消费者与缺口：

- **干净安装。** `runtime` 入口依次 `create_all`、`alembic upgrade head`、schedule 数据迁移、`grant_rls_app_role`、`verify_schema_readiness`，失败就拒绝启动。`api` 角色只跑只读 readiness。没有安装向导，没有“备份已存在才允许升级”的门。容器起来不等于 GT-07 通过。
- **升级预检。** 现成消费者是 `python -m app.scripts.verify_schema_readiness`。它证明迁移 head 和 RLS 目录，不证明业务数据可恢复。
- **备份与恢复。** 本批没有找到一等的 `pg_dump`/恢复命令。要恢复的真相是 PostgreSQL 数据卷、`AGENT_DATA_DIR`，以及租户模型密钥所依赖的 `SECRETS_MASTER_KEY`。Redis 可以重建协调，不能代替任务和权限。
- **应用回退与数据回退。** 新 runtime 镜像启动时会向前迁移。退回旧镜像不会倒转 Alembic。计划 W9 和 M0-E §4.1 已要求把这两件事分开。后续消费者仍是：schema owner URL、`app_rls` 运行 URL、Postgres 卷、Agent 文件目录。

M1 只要求安装骨架能跑到 GT-01；完整升级、备份和恢复在 M4。本候选连骨架的实跑都还没有。Linux 实跑继续延期。

## 6. 资源与下一刀

| 资源 | 状态 | 挡住什么 |
|---|---|---|
| 模型花费授权、租户测试配置、凭据 | 未建立，本批未检查。没有读密钥，没有查余额，没有发请求。9 月那一次 MiniMax 对照授权已经消费 | 黄金任务的模型验收，以及 §4 的 Ling 重放 |
| 提供方余额 | unknown | 不能把余额写成已经观察到的耗尽 |
| 设计伙伴与一周真实使用 | unknown。合同使用 `fixture` | GT-01 的真实使用、GT-06 |
| 企业 IdP 与客户网络/硬件/离线要求 | unknown。代码有本地、飞书、OIDC | GT-07 的身份配置 |
| 飞书测试企业应用 | 未提供，本批未检查 | GT-01、GT-02 不能记通过 |
| 钉钉、企微 | 未提供，本批未检查 | GT-06 |
| Linux / ENV-A02 | owner 延期，未验证 | GT-04；GT-07 的干净安装实跑 |
| 本机 Codex、Claude CLI | 可执行文件已由 Codex 审查时的只读 `command -v` 解析到 `$HOME/.local/bin/codex` 与 `$HOME/.local/bin/claude`。本修正没有重跑，也没有做登录或余额探测 | 只说明文件在 |
| Claude Code / Codex 的 Hive 集成与测试设备 | 未检查 | GT-05。不要求再提供已经装上的客户端 |
| 远程 A2A、机器身份 | 未检查 | GT-05 |
| OpenConnector 原生缓存授权 | M0-F 保持 FAIL | 不在本批重开 |
| 环境 provider | M0-D 未通过 | 不选进 EP-1 |

这些门开着时，下一刀应用修正是 §3 的选模缺口：已配置主模型行缺失时，Web 和心跳不再静默提升备用；Web、渠道、心跳的普通主/备读取都要尊重 `enabled`。渠道现有的缺失检查保留。只配置备用的 Agent，以及显式 `LLMError` 路径上的备用，保持现在的语义。本批不写这刀。它通过之后，才是 M0-E §4.1 第 1–2 步：当前还不存在的只读 `POST /api/enterprise/org/identity-links/dry-run`，输出 `hive.identity_dry_run.v1`。消费方是后续切换和 `WorkspaceOrgSection` 要读的处置行；现有 `/org/departments` 继续读 `Department`。证明方式是两个租户、非超级用户、`app_rls` 和 `app.current_tenant_id`，夹具用户即可。不调用模型，不启动飞书，不做 Linux 安装。GT-01 仍是 `specified_not_run`。这是顺序建议，不是本批开工许可，也不是 M0 完成。

## 7. 本次检查与清理

检查过：`git rev-parse HEAD` 为 `41bf3627d151d5e83c99ff67953f1343ef91b2ee`，分支 `main`。AST 提取不导入应用。没有跑 `setup.sh`、`restart.sh`、Compose、pytest、模型 Test 或 Linux。

基线快照 `.git/next-major-2026-10-14/m0-g/baseline-files.json`：收尾复核时六个文件哈希仍一致。`docs/wip/next-major-version-development-plan-2026-10-14.md` 快照是 `ee1e8a7878b8a0e6265cc795640e4583dba578372c28fc6a8df0c15f70ea8853`，提取时实际是 `067c5cb71a45da486c147c5e7ae9db50c13ed880a9c93ec96fdc02074a8c787a`，收尾复核时实际是 `8bca2bacac21dbd4bd9b5ae137f343fea5685266910d7c645f08b92f2e662ae7`。本修正开始时又读到 `749f1b864cd72c4bf406c471ec57fdea9a89622ef723176a3e709214c1dcfde0`。本 worker 没有写该文件，也没有把它改回快照。§12 只由 Codex 维护。

第一次收尾删除了 `/tmp/m0g_extract.py`（5019 字节）。那次删除记在原来的 `commands.txt`，本修正不改那份记录。脚本有重放消费者，不该删。本次从委派回执 `events.ndjson` 第 2577 行初稿和第 2614 行 `AnnAssign` 补丁按字节找回，SHA-256 `59ca11024be85bec4cc5271e1ac5f7fb077b6a021c6d653e144252f882ec0146`，保存在 `revision1/recovered-m0g_extract.py`。可执行入口是 `revision1/run_extract.py`。它拒绝已存在的输出目录：对 `replay-1` 和原 `m0-g` 目录都退出 1，原快照哈希不变。一次重放写入 `revision1/replay-1/`，其中 `registry-extract.json` 与原快照同为 `b34f0947305ed242d3bdd4c3d3425f9009f794373ba8af8e50f8fb727e831bd3`。内核哈希补在 `revision1/source-manifest.json`，不改原来的清单。

没有本批容器、监听或应用进程。工作区里新增的可见文件是本报告。`/tmp/m0g_recover_probe.py`（1114 字节）和 `/tmp/m0g_recovered_candidate.py`（5019 字节）在副本核对后删除；回执和 `revision1` 里的脚本仍在。其余删除字节为 0。

未提交，未 push。
