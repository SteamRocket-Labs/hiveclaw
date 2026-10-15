# OpenConnector 候选探针（M0-F）

> 日期：2026-10-15。基线：`7c1989d0a7aad18cd55e51caea9c5c6dd9498b15`。首轮委派：`8338b6983b8548b3a43c45d20254662e`。修复委派：`9dde5e8e587e46ea86a015ae9c75f88b`。
>
> 状态：计划中的 M0 技术调查。权威运行是 `runs/probe4`，退出码 1。测量完成；两条缓存披露检查保持失败，这是采用门，不是把原生缺陷改成通过。`runs/probe1`、`runs/probe2`、`runs/probe3` 和 `review-candidate1` 不改写。本批没有改 Hive 应用、schema、UI、测试或上游源码，没有实现网关，也没有写成真实 SaaS 验收。M0 没有完成。专用 Linux ENV-A02 由负责人推迟（“linux先不测试”），尚未验证，本批不申请主机。计划 §12 只由 Codex 修改。

本地证据入口：[reconstruction.md](../../.git/next-major-2026-10-14/m0-f/reconstruction.md)、[probe4/checks.json](../../.git/next-major-2026-10-14/m0-f/runs/probe4/checks.json)、[probe4/cleanup-receipt.json](../../.git/next-major-2026-10-14/m0-f/runs/probe4/cleanup-receipt.json)。离线反例在 [revision-repair1](../../.git/next-major-2026-10-14/m0-f/revision-repair1/)。采用门仍是 [计划 §7](next-major-version-development-plan-2026-10-14.md) 的长尾连接器一行。连接器身份和披露沿用 [M0-E 合同](next-major-organization-provenance-contract-2026-10-15.md)。

## 1. 结论

v1.9.0 可以按每个租户一个隔离实例来放，但这不是一条已通过的采用门。两个实例仍实跑：B 的库里没有 A 的 memos 连接 id。一个进程仍然只有一个管理员身份，管理员 bearer 既能管控制面，也能 `POST` 出效果。

同一实例里，当前连接授权只挡住新请求。把已存 runtime token 的 `allowedConnections` 从 alpha 收到 beta、并保留 `memos.list_memos` 之后，不带原键的 alpha 请求是 HTTP 403 `connection_not_allowed`，夹具增量为 0。同一 `Idempotency-Key` 和同一正文仍返回 HTTP 200、原先的 alpha 成功正文和同一个 `executionId`，夹具增量为 0。删除 beta 连接之后，新请求同样是 403，缓存请求仍返回删除前的 beta 成功正文。第二条 runtime token 复用同一键得到 409 `idempotency_key_conflict`，拿不到第一条正文，也没有新的夹具调用。

因此不能把这条运行时的幂等缓存当成授权边界。共享进程上的租户映射和调用前闸门仍没有实现，本批不把它们记为已接受。现有 custom API / MCP 继续是已声明 HTTP 动作的基线。长尾连接仍保留。缺设计伙伴的真实 SaaS 凭据，就继续缺验收输入。

## 2. 源码事实

冻结树是 tag `v1.9.0`，commit `9a2ac5b640ac7fb00f82a3bc159325ef6ee34caa`。本批没有改这棵树。许可证是 [LICENSE.txt](../../.git/next-major-2026-10-14/m0-f/source/open-connector/LICENSE.txt)（Apache-2.0）。[NOTICE.md](../../.git/next-major-2026-10-14/m0-f/source/open-connector/NOTICE.md) 不是完整第三方清单。开源运行时只有一个管理员身份，见 [programmatic-connections.md](../../.git/next-major-2026-10-14/m0-f/source/open-connector/docs/programmatic-connections.md)。Hosted 部署本批没有跑。

`POST /v1/actions/:actionId` 在 [connect-server.ts](../../.git/next-major-2026-10-14/m0-f/source/open-connector/src/server/connect-server.ts) 的 `createRuntimeActionRun` 里先做 `evaluate(action)`。动作不被允许时走执行路径，不进幂等完成态。动作被允许且带 `Idempotency-Key` 时，[hashActionRequest](../../.git/next-major-2026-10-14/m0-f/source/open-connector/src/server/actions/action-idempotency.ts) 的指纹是 `actionId`、连接名、连接 id、输入和 `runtimeTokenId`，没有 `allowedConnections`。`claim.kind === "completed"` 直接写回已保存的响应。连接授权在 [action-policy.ts](../../.git/next-major-2026-10-14/m0-f/source/open-connector/src/core/action-policy.ts) 的 `evaluateConnection`，由 `action-runner` 在真正执行时调用。空的 `allowedConnections` 或 `allowedActions` 在这条运行时里是不限制。`action_not_allowed` 的 HTTP 状态是 400。`connection_not_allowed` 是 403。

幂等相同键和相同指纹在保留窗口内重放原来的状态和正文；同键不同指纹是 409 `idempotency_key_conflict`。这是重放或冲突，不是恰好一次。MCP `execute_action` 没有幂等键。加密密钥覆盖凭据和已完成的幂等响应。Memos provider 只有 `api_key` 和 `baseUrl`，没有刷新令牌。

## 3. 冻结身份

| 对象 | 标识 |
|---|---|
| git commit | `9a2ac5b640ac7fb00f82a3bc159325ef6ee34caa` |
| 保留的 npm 记录 | `runs/probe2/npm-view.json`：`1.9.0`，integrity `sha512-j7KPsFifGfFk5BZYK6lBurfvUXh3/+4SJ8F3NfXXTRkhZweub80YwcUDB5GQzW1SHowPelnBP/fbEhDcBu0SoQ==`，shasum `b9a3530bb668cf4a62e71e9c48e08725da408508` |
| 镜像 | `ghcr.io/oomol-lab/open-connector@sha256:3a3974804fd0459806b1c9cee599f976b79ba9c74c236fe3f95620f54bfb8f93`。Id 与 digest 都是这个值，Size `111777584` |

重放使用这个 digest，并对照上面的保留 npm 记录和源码 HEAD。身份不一致就在创建容器、卷、网络之前退出。本批不再调用浮动的 `npm view`，也不再用可变 tag 启动容器。源码哈希见首轮 `closeout/facts.json`，本批没有改这些文件。

## 4. 现有 Hive 入口

`custom_api_connectors.py` 把 custom API 定义成租户拥有的 `Tool`。模型只看动作 schema。凭据经 `TenantToolConfig` 注入。没有 `agent_id` 就不执行。这条服务里没有 `Idempotency-Key`。`mcp_authz.py` 不把用户或 OAuth token 转给任意 MCP 资源服务器。`connector_acl.py` 只在结果自带 ACL 元数据时做失败关闭，不是效果授权边界。这次夹具响应没有 ACL 元数据。

## 5. 实际跑到的 profile

证据等级：本地 Memos 协议夹具。它实现 `GET /api/v1/auth/me`、`GET /api/v1/memos`、`POST /api/v1/memos`。不是 usememos.com，也不是其他 SaaS。

权威运行 `probe4` 的前缀是 `m0f00abd97cdf6b`，run id `probe4-486efb35`。网络、卷和容器都带 `m0f.task=9dde5e8e587e46ea86a015ae9c75f88b` 与该 run id。名字在创建前必须不存在；创建成功但标签或 id 对不上就拒绝收养，也不删除。A 发布 `127.0.0.1:18415`，B 发布 `127.0.0.1:18416`，错误密钥副本短时使用 `127.0.0.1:18417`。JWKS 只在 A 的网络命名空间里。两个实例都打开了进程级 `OOMOL_CONNECT_ALLOW_PRIVATE_NETWORK`。动作允许列表仍是 `memos.create_memo,memos.list_memos,memos.get_memo`。

`probe4` 写了 67 条布尔检查，2 条 `ok: false`，进程退出码 1，没有打印 `PROBE_COMPLETE`。`probe3` 保留：同一缓存失败已经出现；`admin-versus-runtime` 当时把响应头当成了正文，所以多记了一条假失败。产品响应仍是 GET 401、POST 200、夹具账户 alpha。`probe4` 改读正文后这条通过。`probe2` 的原始失败收据没有改写。

## 6. 运行观察

夹具共 20 次调用，其中 4 次写入：alpha 账户 3 次 `POST /api/v1/memos`，beta 账户 1 次。无幂等键的重复创建会再写。同一 token、同一键、同一输入重放一次，没有第二次写入。同键不同输入是 409，夹具增量为 0。

连接 id：alpha `d550dbcd-29ba-474a-838e-ab580cddf3e6`，beta `95f9e637-c17c-499a-92ea-c94b65d2d392`。`connect-beta` 的 `ok` 现在是布尔值。

| 检查 | HTTP | 观察 |
|---|---|---|
| 缺少或错误的管理员 token | 401 | A 的 `/api/connections` |
| A 未认证读 `/v1/actions` | 401 | JWT 验证器已配置 |
| B 未认证动作 / 发现 | 403 后发现 200 | 创建 runtime token 后发现变 401；删掉最后一张再变 200 |
| 允许列表外的 `hackernews.get_top_stories` | 400 `action_not_allowed`，delta 0 | 断言改为这个合同，检查通过 |
| 管理员 `GET /v1/actions` | 401 `unauthorized` | 不是 runtime 发现凭据 |
| 管理员 `POST` `memos.list_memos` | 200，delta 1 | 夹具账户 alpha，正文 `success: true`。这是断言，不再是无条件通过 |
| token 收窄到 `memos.list_memos` | 创建 400 `action_not_allowed`，列出 200 | 创建 delta 0。断言改为 400，检查通过 |
| MCP `tools/call` | 200 `text/event-stream` | 正文含 `"ok":true` 和 `memos/beta-1`，夹具 delta 1，账户 beta |
| 缓存 alpha 列表 | 200，delta 1 | 账户 alpha，`executionId` `6fa5039d-9d1b-466e-81a6-0a2f6ddbe6ea` |
| 同一 token 改只允许 beta 后的新 alpha 请求 | 403 `connection_not_allowed`，delta 0 | 动作仍保留 |
| 同一键再请求 alpha | 200，delta 0 | 正文和 `executionId` 与缓存相同。要求是拒绝。检查失败 |
| 第二张允许 alpha 的 token 复用该键 | 409 `idempotency_key_conflict`，delta 0 | 正文不是第一条缓存，没有新账户调用 |
| 删除 beta 连接 | 200 | 原生 `DELETE /api/connections/memos` 支持 |
| 删除后的新 beta 请求 | 403 `connection_not_allowed`，delta 0 | |
| 删除后的同一键 | 200，delta 0 | 正文和 `executionId` `94821625-4cd5-4638-bccc-f903f29a8425` 仍是删除前的 beta。要求是拒绝。检查失败 |

其余已通过项与首轮观察一致：租户头不改选连接，错误别名和别的连接 id 在夹具前 403，beta token 不加别名不会落到 default，JWT 的 `sub` / `tenant` 仍执行 default alpha 且不能读管理员列表，过期 JWT 401，失败的密钥替换不覆盖已验证密钥，第二把密钥会切换夹具标签，重启后 beta 仍可执行，撤销 alpha bearer 后 401 且 beta token 仍可用，B 不包含 A 的两个 UUID，错误加密密钥时健康 200、动作不执行、夹具不增加。8 个合成 canary 没有出现在 A/B 日志和复制出的 `data-a` 里。明文 canary 仍只在该次运行的 `secrets.json` 和请求收据里。

OAuth 刷新没有跑。Memos 这条 provider 没有刷新令牌，本批没有改上游去造正面结果。

## 7. 候选建议

以后若接入，最小工作是这些，本批都没有做：

1. 每个租户一个 OpenConnector 进程，独立卷、加密密钥和管理员 token。实例之间的隔离已经实跑，不能代替下面的缓存边界。
2. Hive 保管管理员 token 和加密密钥。管理员 token 能产生效果，不放进模型上下文或工具参数。
3. 每个调用方一张 runtime token。`allowedConnections` 必须是非空精确 id，`allowedActions` 必须写明动作。空列表是不限制。
4. Hive 在产生效果前把租户映到那一个实例。共享后端的权威映射和调用前闸门还没有实现，不能当成已接受。
5. 在向调用方披露缓存结果之前，也要重新检查当前连接授权和连接是否仍存在。只在外部效果前设闸门不够：这次缓存命中没有再打夹具，却返回了原先账户的正文和 `executionId`。原生幂等重放不能当作这道闸门。本批没有实现 Hive 网关，也没有把要求的拒绝改成 200。
6. 进程保持 runtime 认证。只删除最后一张已存 token 会把发现重新打开。
7. 需要避免重复写入时，由 Hive 对 HTTP 动作发送 `Idempotency-Key`。供应商行为是重放或 409。另一张 token 使用同一键会得到 409，不会重放第一张 token 的正文。MCP `execute_action` 没有这个键。
8. 保留现有 custom API 的 agent、租户、`approved_networks` 和 `external.api.call` 闸门。进程级私网开关不能代替按连接器的网络批准。
9. 结果进入提示词时继续使用 M0-E 的来源和披露。这次探针没有发出 `hive.knowledge_provenance.v1`。
10. 再分发镜像或 npm 包之前，补上 Apache-2.0 的 NOTICE 义务。本批没有做完整依赖清点。

## 8. 未验证

以下都不是通过：

- 真实 SaaS OAuth、刷新、重连和设计伙伴账号。
- Hosted Team 权限、PostgreSQL 显式迁移、`providerHttpDispatch`。
- npm lockfile 和镜像层的完整第三方许可清单。
- 客户端在进程外长期缓存工具句柄。服务器对已撤销 bearer 的后续调用返回 401。
- 支持矩阵、模型评估、第三方 agent 和企业安装器。它们仍在总计划里。专用 Linux ENV-A02 由负责人推迟（“linux先不测试”），本批未验证，也未申请主机。

连接 `DELETE` 和两张 runtime token 的同键行为已经按上面的范围测量，没有扩成通用 provider 审计。

## 9. 证据和清理

权威运行是 `runs/probe4/`。`probe3/` 保留那次头部绑定错误和同样的两条缓存失败。`probe2/checks.json` 的 sha256 仍是 `8e316e3eccdffb524a0b167ca5a00c561d48aa5b57daa6a5b89ecac270c8d344`。源码树工作区干净。

离线：`revision-repair1/offline-ownership/result.json` 通过，包括零创建不删除、哨兵身份不匹配则留下、已存在卷不收养、创建成功但标签不是本次 run 则不收养、镜像身份不符时在创建资源前退出。Codex 未改的 `check_probe_safety.py` 对最终脚本写到 `revision-repair1/codex-check-2/`：零创建时破坏性命令 0、无关日志读取 0，注入的假检查退出码 1。更早的 `codex-check/` 结果相同。

`probe4` 删除的资源，均在删除前核对了创建收据的 id 和标签：fixture `612e0ffe654a…`，A `ecfc6a065a98…`，B `5bc2e81729ac…`，jwks `393894c0538e…`，copy `91ec4db46047…`，wrong `7a8d76da5388…`，卷 `m0f00abd97cdf6b-a-data`、`b-data`、`wrong-data`，网络 `9fca2a7a8c31…`。删除结果都是 `removed`。保留副本的逻辑字节：`data-a` 398064，`data-b` 663136，`data-wrong` 307424。这些是副本的 `st_size`，不是 Docker 卷的物理占用。卷和容器的物理字节以及回收字节都记为 `unknown`。没有用全局 Docker 差额当作删除依据，也没有 prune 共享状态。结束后，该 task 标签下的容器、卷和网络为空。这条标签查询不包括镜像声明的匿名卷。镜像保留。

镜像 `Config.Volumes` 只有 `/app/data`。fixture、JWKS 和 copy 不在这条路径存状态，旧调用没有覆盖它，`docker rm -f` 就留下匿名卷。当前这三个调用点加上 `--mount type=tmpfs,destination=/app/data`。A、B 和 wrong-key 仍使用具名数据卷和保留副本。`revision-anon1/lifecycle-result.json` 通过真实 helper 起了三个 `/bin/true` 容器：挂载只有 tmpfs `/app/data`，清理后卷 id 集合不变，当时是 39 个。没有重跑完整 provider 矩阵。`probe4` 仍是授权证据。

八个已归因匿名卷在删除前重新核对了卷 id、`CreatedAt`、匿名标签、对应 `cleanup-receipt.json` 里的容器 id，以及当时没有消费者。只读 `--mount` 复制的结果都是逻辑字节 0、文件 0、可归因字节 0。物理字节和回收字节是 `unknown`。八个 id 都已删除，其余卷 id 没动。卷数从 39 到 31 只是事后观察，不是删除依据。标签过滤为空不能证明所有资源都已清掉。`probe1` 到 `probe4` 的收据没有改写。补充记录在 `revision-anon1/cleanup-supplement.json`。

`du -sk`：probe 68，probe3 1924，probe4 1908，revision-repair1 136，revision-anon1 76，source 94884，upstream-docs 136，单位 KiB。probe4 收据里的证据逻辑字节是 1648718。本批没有写 source、upstream-docs、probe3、probe4 或 revision-repair1。source 从上一轮记录的 94904 变为 94884，revision-repair1 从 132 变为 136；冻结源码工作区仍是干净的。删除了没有后续消费者的 `__pycache__`：探针目录 `du -sk` 80，离线脚本目录 16。

七个基线路径里，计划文件当前 sha256 是 `06e07bea9cc969f5a69276b71efdc5ed7af1ad892d740d85488847a571335441`，快照是 `d8c68b656e2285482c18944c4eebf5e640c23d2969a38806e491f5988015e5cc`。本工人没有写这个文件。其余六个哈希与 `baseline-files.json` 一致。HEAD 仍是 `7c1989d0a7aad18cd55e51caea9c5c6dd9498b15`。

计数：应用功能和测试 0 行。跟踪文档是本报告，118 行。未跟踪探针是 `fixture.mjs` 104 行、`jwks.mjs` 21 行、`run_probe.py` 1385 行，离线检查 `offline_ownership.py` 244 行，回放说明 `reconstruction.md` 81 行，本批补充 `helper_lifecycle.py` 124 行、`remove_attributed.py` 243 行。本批不借用 M0-B 的比例例外。
