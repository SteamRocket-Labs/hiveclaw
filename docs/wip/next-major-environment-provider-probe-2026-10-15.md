# 环境 provider 选型探针（M0-D）

> 日期：2026-10-15。基线：`9937fe9458b0a1e20c6b5c4cac91e0830778fec3`。委派：`eb1d68661ce34948b9ad136c780aab1e`。
>
> 状态：这是计划中的 M0 技术调查，本地原始证据保存在未纳入 Git 的 `.git/next-major-2026-10-14/m0-d/`。调查结论与修订探针已独立复查；没有重跑整套实验，第一次收据保持原样。本批未修改 Hive 运行时、schema、UI 或应用测试。报告提交不构成 `ENV-A02` 通过，M0 也没有完成，不推进 M2。

本地证据入口：[reconstruction.md](../../.git/next-major-2026-10-14/m0-d/reconstruction.md)、[host-preflight-extended.json](../../.git/next-major-2026-10-14/m0-d/host-preflight-extended.json)、[cleanup-receipt.json](../../.git/next-major-2026-10-14/m0-d/cleanup-receipt.json)。采用门仍以 [计划 W5](next-major-version-development-plan-2026-10-14.md) 和 [ENV-A02](agent-environment-control-plane.md) 为准。8 月控制面文档里的暂停和未确认浏览器范围，不覆盖 10 月已经授权的安全电脑与接管范围；本批只测量官方 profile 实际有没有这条路径。

## 1. 结论

本机可以在设定 `NO_PROXY` 之后，把 OpenSandbox 1.1.1 的 Docker/runc arm64 当作回环实验室控制器跑通命令和文件。这个 profile 的隔离等级是 Docker Desktop 上的 OCI runc。服务器自己的日志写着 `Secure runtime is not configured.` 它不能当作私有逻辑 Environment 的生产隔离 profile。

CubeSandbox v0.7.3 在本机的结果是 `unavailable`。产品路径是 KVM microVM。本机没有 `/dev/kvm`，也没有跑它的安装脚本。

本批不新增 Hive provider adapter，不把失败改写成 subprocess 或 `docker exec` 成功。现有选择器保持原语义。

## 2. 现有执行入口

`backend/app/services/code_execution/service.py` 的 `execute_agent_command` 只接受 `local_os_sandbox`（含 `local`、`os_sandbox`、`bwrap`、`sandbox-exec`）和 `vercel_sandbox`。其他 `HIVE_CODE_EXEC_PROVIDER` 返回明确的 `Code execution provider unavailable`，没有静默替换。直接 import 这个函数的生产代码是 `code_exec.py`、`skill_runtime.py`、`hr.py`、`governance_resolver.py`、`hook_runner.py`、`artifact_gate.py`、`probe.py`。`api/commands.py` 里的同名函数是另一条入口。

Vercel 路径仍是每次命令打包上传再停止，不是一份长于 provider 虚拟机的逻辑 Environment。W5 要求文件和回执是权威状态，缺能力时返回 typed `unsupported`。

## 3. 冻结身份

`release-1.1.1` 不能收成一个 SHA。

| 对象 | 标识 |
|---|---|
| 附注 tag 对象 | `7b4ff96b552b442dda206e6e4b6da2b2f7dcc439`，GitHub `verification.verified=false`，原因 `unsigned` |
| tag 指向的 commit | `531700ca18a7fa5fab6a2f25d6dc68a76eddab12` |
| tag message 中的构建 commit | `b7d7697750b45be2ccc876da645c8880a0cf69fc` |
| BOM `metadata.gitCommit` | `6150f3642bbf9db47f06d6c42ca9c7bfae06b3e9` |
| 生命周期规范 sha256 | `d05b35647bbf4231cc5eaf8bf8ad4c7781479c84c526ff8b0015e35d11b23f7c` |
| 实际运行的 execd / egress | 横幅版本 `b7d7697`，构建时间 `2026-10-09T13:36:16Z` / `13:35:26Z`，早于 BOM commit |

本机实际使用的字节：

- SDK wheel `opensandbox-1.1.1-py3-none-any.whl`，668271 字节，sha256 `98eb0d7b57fdeabbbb5202d5e3a5261741f68fe47a631f0ec483b1566eb5fef8`
- server wheel `opensandbox_server-1.1.1-py3-none-any.whl`，382100 字节，sha256 `7da042ecd5cfdd38bd8a96ca92d28d02bd91af2c26e5250382dd7a21c4bf04b8`
- execd 索引 `sha256:cda2bc35409bbf57561599b316cafb899598f7dce671332e7ceda14eb1cfeded`，arm64 manifest `sha256:1a6fc28b5efe098f6c168ea606dbad6bde2d1fc479329b4c8a6279c82ee02e94`
- egress 索引 `sha256:bce2cc2e4dc3a3594678b91ac29efb0669fccebfdfc137727018d3b087fbb13a`，arm64 manifest `sha256:ed10a676c306dc3fe78d47016922f6a0271493317d68b3ea133a0aed3e85a602`

服务器进程来自 server wheel。BOM 里的 `opensandbox/server:release-1.1.1`（`sha256:bcd5f264…`）没有作为进程运行。同 tag 的 example config / compose 仍钉着 execd `v1.1.0`、egress `v1.1.7`、server `release-1.1.0`，并把 `docker.sock` 挂进服务器容器；实验室没有使用这份 compose。GitHub 上 `docker/execd` 的 tag 停在 `v1.1.0`，`docker/egress` 一直到 `v1.1.7`。

来源：<https://github.com/opensandbox-group/OpenSandbox/releases/tag/release-1.1.1>，冻结树里的 `docs/releases/1.1.1.yaml`、`SECURITY.md`、`docs/community/release-verification.md`。本地副本在 `m0-d/preflight/`。

## 4. 本机和实际跑到的 profile

macOS 15.6.1（24G90），Darwin arm64，Apple M4 Max，`kern.hv_support=1`，没有 `/dev/kvm`。Docker Desktop 29.1.3，服务器 OS linux/aarch64，默认 runtime runc 1.3.4，cgroup v2。没有 gVisor、Kata、Firecracker、cloud-hypervisor 或 QEMU。Fast-sandbox 镜像是 linux/amd64，还要 KVM 和 XFS reflink，本机不能跑。改 Docker Desktop runtime 或宿主机 sysctl 不在本批授权内。

选定并实际启动的 profile：OpenSandbox server 1.1.1，`runtime.type=docker`，`network_mode=bridge`，`publish_host=127.0.0.1`，`resolve_internal=false`，egress `mode=dns`，能力丢弃，`pids_limit=256`，没有打开 `bootstrap.execd.isolation`。访客镜像是本机已有的 `python:3.12-alpine`（`sha256:4c47124a8391…`），这只替换访客用户态，不改变 runtime profile。

安装的 1.1.1 代码在设置 network policy 时拒绝用户自定义网络，egress sidecar 使用默认 bridge。因此带策略的沙箱共享默认 bridge。这是记录下来的限制，不是另外搭了一套网络。

这次实际跑到的是 `egress.mode=dns` 加 Docker/runc。冻结的 server 配置只接受 `dns` 或 `dns+nft`，实验室 toml 选了 `dns`。`dns+nft` 是任务配置：`SANDBOX_CONFIG_PATH` 把模式传给 sidecar 的 `OPENSANDBOX_EGRESS_MODE`，sidecar 创建时已经加 `NET_ADMIN`。它不要求修改宿主机 Docker daemon 或 sysctl。本批没有改这些，也没有再跑共享网络实验，所以不能把 dns-only 旁路写成每一个 OpenSandbox profile 都失败。`dns+nft` 在这台 Docker Desktop 上是否丢弃 IP，没有测量。

本机只有 runc 1.3.4，没有 `/dev/kvm`。gVisor、Kata、Firecracker、fast-sandbox 和 Cube 的 KVM microVM 需要另一台有对应 runtime 或 KVM 的 Linux。这是主机缺口，不是 dns profile 的同一条结论。

`bootstrap.execd.isolation` 这次没有设置。`isolation available=false` 只描述这个未设置的 profile。源码在扩展值为 `enable` 时会给该沙箱加 `CAP_SYS_ADMIN`、`apparmor=unconfined`、`seccomp=unconfined` 和 tmpfs。本批不因此禁止以后的任务级设置，也没有授权修改宿主机 sysctl 或 Docker runtime。

访客没有拿到宿主机 `docker.sock`、owner home，也没有 privileged。主沙箱 `c8ecd926-98e9-43e2-b3bf-a8b8f8e17136` 只挂了 `opensandbox-runtime-c8ecd926-…` 到 `/opt/opensandbox`。`HostConfig.Memory=134217728`，`PidsLimit=256`。execd 日志：bwrap 0.11.2，`isolation available=false`，原因是 setpriv 和 userns 都 `Operation not permitted`。egress 日志：`nftables disabled (dns-only mode)`，策略 `defaultAction=deny` 且规则为空。

## 5. 同一条行为序列

权威收据是 `m0-d/lab/receipts.json`，共 36 条。`receipts-summary.txt` 只覆盖前 30 条，不含更正后的网络重跑。

第一次创建没有 `NO_PROXY`。沙箱 `b1ea6acf-7534-4265-8d14-4639079aae79` 已启动，官方 httpx 健康检查在 180 秒内持续收到空 body 的 HTTP 502。urllib 直连发布端口通常是 200。原因是 httpx 0.28.1 对 `127.0.0.1` 走系统 HTTP 代理，而 `urllib` 会 bypass loopback。这是实验室客户端路径失败，不是隔离通过。保留在 `receipts-system-proxy.json`。

第二次由 `run_lab.sh` 只给本任务进程导出 `NO_PROXY=127.0.0.1,localhost,::1`。健康检查通过。

| 检查 | 结果 | 观察 |
|---|---|---|
| 创建与检查 | 通过 | 主沙箱 `c8ecd926-98e9-43e2-b3bf-a8b8f8e17136` |
| 命令与文件往返 | 通过 | `print 1+1` 输出 `2`；规范字节 sha256 `8d2ebb951737f144f201bbde573fadaed17291c9ee3a9093ba9242919a7386a6` |
| 第二次 attach | 通过 | 读到同一文件 |
| pause | 只证明 cgroup freezer | 状态 `Paused` / `CONTAINER_PAUSED`；暂停期间命令 `BACKEND_TIMEOUT`；文件仍在；后台执行 `b09e6bc0d8c14d77adf76834146c04f0` 解冻后同一个 id 结束。不是持久 VM 快照，也不是进程内存或 socket 恢复 |
| 控制器重启 | 通过 | 重启后文件哈希与预期一致 |
| 计算丢失 | 通过观察，不是恢复 | `docker kill` 后状态 `Failed` / `CONTAINER_EXITED_ERROR`，退出码 137 |
| 规范文件再水合 | 通过拷贝 | 新沙箱里原先不存在该文件，探针写入规范字节。不是容器内存或 `docker commit` 恢复 |
| 失败命令 / 超时 | 通过 | 退出码 17；超时退出码 -1，`CommandExecError` |
| PID 上限 | 通过 | 配置 256，输出 `LIMIT 245` |
| 内存上限 | 原始收据 `failed`；有效结论 unknown | `memory_ceiling` 只有分配 stdout `209715200` 和随后 `ALIVE`，加上创建时的 `HostConfig.Memory=134217728`。没有这条 profile 的 `MemorySwap`、cgroup swap 或 `OOMKilled`。分配成功不能推出 RAM 上限失效，也不能推出资源门通过。Codex 用同一访客镜像、`network=none`、无挂载做了对照，见 `review-candidate1-checks.json`：默认 `Memory=134217728`、`MemorySwap=268435456` 时，200MiB 分配成功，`memory.current=134070272`，swap 增加 84987904 字节；`--memory-swap=128m` 时 `memory.swap.max=0`，退出 137，`OOMKilled=true`。Docker 写明未设置 `--memory-swap` 时，容器还可使用与 `--memory` 等量的 swap。[resource constraints](https://docs.docker.com/engine/containers/resource_constraints/#memory) |
| 浏览器 / 查看 / 接管 | 未测 | 这条请求用的是 `python:3.12-alpine`，没有启动查看器。server 源码对 dockur/windows 会发布 RDP 3389 和 noVNC 8006。授权版本仍包含浏览器与接管，不因这条 lifecycle 未测而排除 |

索引 13–15、17–18、20 的 TCP 命令把 `json.dumps` 的 `\n` 交给了 `python -c`，客人进程语法错误、退出码 1、stdout 为空。这六条是探针错误，结果保持 `unknown` 或无效 `failed`，不能读成策略拒绝。`run_net.sh` 用 `shlex.quote` 重跑：

| 重跑 | 结果 | 观察 |
|---|---|---|
| `169.254.169.254:80` | 失败 | stdout `CONNECT`。默认拒绝没有挡住 IP。这是连通，不是 `ConnectionRefusedError`。拒绝连接或健康检查异常不能当成策略丢弃。本机不是 AWS，没有读取响应体，不能声称读到了实例元数据 |
| 未列出的 `host.docker.internal:18080` 与 `:18081` | 名称被挡住 | stdout `BLOCKED gaierror`。这是 DNS 失败，不是 nft drop |
| 允许 `host.docker.internal` 后访问 `:18081` | 通过 | `CONNECT`。夹具只绑在 `127.0.0.1:18081`，Docker Desktop 的 `host.docker.internal` 能到达它 |
| 同一允许名访问 `:18080` | 失败 | `CONNECT`。FQDN 允许没有端口选择器，实验室控制面端口一起打开 |
| 跨沙箱 TCP `172.17.0.3:44772` | 失败 | `CONNECT`。共享默认 bridge 上的 IP 流量没有被 dns-only 策略拦住 |
| 跨沙箱文件与 canary | 通过 | 另一沙箱读不到规范文件；canary 环境值输出 `ABSENT` |

凭据：探针自己把合成 canary 放进客人环境，所以 `canary_in_env=true`；标签和服务器日志里都没有。API key 没有出现在服务器日志。egress auth token 存在于容器标签，长度为 32，值没有写入收据。客人内的 docker socket 路径输出 `ABSENT`。

## 6. CubeSandbox

对照版本是 tag `v0.7.3`，commit `4411c96ba8658a959de34693f54f4918db2a432f`。master `f2777699da7e2f60b019519796f655c598a84342` 的 README 与该 tag README sha256 同为 `6d0f5e98e35b933bd39da054a64a00bbfe13472ded06d2bb00560b009d77b50f`。README 里的 ARM64 支持和性能数字不作为本机能力。

保存的 `LICENSE` 写明 Apache-2.0，并列出第三方例外：kata-containers 与 cloud-hypervisor 为经腾讯修改的 Apache-2.0；cilium/bpf 双许可 BSD-2-Clause OR GPL-2.0，文本选择 BSD-2-Clause。GitHub license API 对该仓库返回 `NOASSERTION` / `Other`。API 标签不能代替这份 LICENSE，也不能当成额外授权。

v0.7.3 quick start：已有 KVM 的路径覆盖 x86_64 或 aarch64 裸金属；PVM 包是 `*.x86_64.rpm` / `*_amd64.deb`，不支持 ARM64；ARM64 需要已有 `/dev/kvm` 的物理 Linux、glibc >= 2.31、`/data/cubelet` 上至少 50 GB 的 XFS reflink。QEMU dev-VM 不是验收路径。安装脚本是 root 下的 `curl | bash`，本批没有执行。

`cubesandbox-probe.py` 的第一次收据仍是 `cubesandbox-receipt.json`：`outcome=unavailable`，`kvm=false`，退出码 2。本机没有 Cube 实跑验收。不带 `--out` 或 `--out` 指向已有文件时，脚本拒绝覆盖并退出 2。新收据用 `python3 cubesandbox-probe.py --out <新文件>`。本机即使给出新文件，也只写 `unavailable`，因为没有 `/dev/kvm`。

命令仍是 `print(1+1)`。`e2b-code-interpreter` 2.11.0 的 `Execution.text` 只表示富文本主结果；这次 print 的 `text` 是 `None`，标准输出在 `logs.stdout`（`['2\\n']`）。探针按这份 stdout 和 `error` 判断，不再读 `.text`。没跑的矩阵项保持 `unknown`。两次冒烟不能写成总结果 `passed`。

它会清掉继承的 `E2B_*` 和代理变量。`Sandbox.create` 要显式的 `CUBE_API_URL`、`CUBE_SANDBOX_URL` 和一把新的合成凭据。固定的 e2b 2.56.0 `get_sandbox_url` 会原样返回 `sandbox_url`，所以探针不把 API 源站猜成沙箱地址，也不回退到 E2B 云。API 或沙箱任一端点是 https 时都要 `CUBE_CA_FILE`，不复用现有 `SSL_CERT_FILE`。2.11.0 / 2.56.0 来自已提取的 wheel，不是本机安装证据。

来源：<https://github.com/TencentCloud/CubeSandbox/tree/v0.7.3>，本地 `preflight/sources/cubesandbox/`。

## 7. 许可与签名

OpenSandbox 冻结树的 `LICENSE` 是 Apache-2.0。GitHub license API 返回 `Apache-2.0`。`SECURITY.md` 声称 GitHub/Sigstore attestation、cosign keyless 签名和 Maven 签名。本机没有 `cosign`，文档中的 `cosign verify` 没有执行。

`gh attestation verify` 已按冻结的 `docs/community/release-verification.md` 执行，仓库 `opensandbox-group/OpenSandbox`：

- execd、egress 的索引 digest 和 arm64 manifest，`--bundle-from-oci`：登记表中没有 attestation bundle
- 同一组 digest 向 GitHub 查询 SLSA provenance v1：HTTP 404
- 两个本地 wheel 的 sha256，分别用 `publish-server.yml` 与 `publish-python-sdks.yml`：HTTP 404

OCI index 里仍列有 `attestation-manifest` 描述符。描述符存在没有被验证，不能算签名通过。git tag 与 commit 本身 unsigned。缺证不是通过。

## 8. 能力矩阵

| 能力 | 本机 OpenSandbox 1.1.1 docker/runc | 本机 CubeSandbox v0.7.3 |
|---|---|---|
| 创建、命令、文件、再次 attach | 有，需 `NO_PROXY` | 未运行 |
| 停机后文件还在 | pause 期间在；kill 之后要靠探针拷贝规范字节 | 未运行 |
| 进程 / 内存 / socket 恢复 | 没有。pause 是 cgroup freezer | 未运行 |
| 跨沙箱文件 | 读不到对方文件 | 未运行 |
| 跨沙箱网络、元数据 IP | 这次 dns profile 没有挡住 IP | 未运行 |
| 按名字允许一个端口 | 这次 FQDN 规则没有端口选择器，控制面端口同时可达 | 未运行 |
| 资源上限 | PID 256 生效；128MiB 的 cgroup/swap 未测 | 未运行 |
| 浏览器查看与人工接管 | 未测，仍在授权版本内 | 未运行 |
| 生产级隔离 | 本机 runc profile 不是生产隔离。不是每一个 profile 都已失败 | 需要另一台带 secure runtime 或 `/dev/kvm` 的 Linux |
| 签名 / attestation | 按官方命令核查，没有通过项 | 未对安装产物做签名核查 |

## 9. 建议、剩余门和资源

建议：不要把本机这个 OpenSandbox docker/runc profile 冻结成 `ENV-A05` 的生产 provider，也不要在这个结果上写 Hive adapter。`ENV-A02` 的强制门没有通过，按控制面文档回到 owner 决策，不自动自研第二套 runtime。组织与来源合同、连接器选择、黄金任务仍是别的 M0 工作。选定模型的能力边界和完整 M2 结果都没有因为这次探针而改变。

下一台机器如果要继续 CubeSandbox，使用修正后的 `cubesandbox-probe.py`。前提仍是一次性 Linux 主机，x86_64 或 aarch64，已经有 `/dev/kvm`、glibc >= 2.31、`/data/cubelet` 上至少 50 GB 且支持 XFS reflink。PVM 包只覆盖 x86_64。不要把 QEMU dev-VM 当成通过，不要在探针里执行 `curl | bash`，不要采购云资源。命令是 `python3 cubesandbox-probe.py --out <新文件>`，并显式给出 `CUBE_API_URL`、`CUBE_SANDBOX_URL` 与 `CUBE_TEMPLATE_ID`。`--out` 不能指向已有证据。不要把 API 地址当作沙箱地址。API 或沙箱是 https 时还要 `CUBE_CA_FILE`。它不继承 `E2B_*`，也不回退到 E2B 云。上面写的 2.11.0 / 2.56.0 是 wheel 接口核对，不是本机已经装好的证据。本机没有执行这条命令去联系 Cube。

OpenSandbox 的下一次 nft 出网试验，先只改任务配置里的 `egress.mode=dns+nft`，写到新的 run 目录，不改本机 Docker daemon，也不新开一轮共享网络实验。本批没有做这次试验。cgroup/swap 也要在那个 profile 上测量之后才能判断内存门。gVisor、Kata、fast-sandbox / Firecracker 仍要另一台有相应 runtime 或 linux/amd64 KVM 与 XFS reflink 的主机。不要在这台 Mac 上安装 gVisor 或 Kata，也不要改宿主机 sysctl。

本批观察到、但不在本批改代码的具体问题：

1. 带系统 HTTP 代理的主机上，OpenSandbox server 的 httpx 客户端默认 `trust_env`，不会像 urllib 那样 bypass `127.0.0.1`。实验室用任务进程的 `NO_PROXY` 绕开。上游修复应是 loopback 转发不走外部代理，而不是放宽沙箱。
2. 这次 `egress.mode=dns` 只打断未列出的名字解析。记录到的 IP 和跨沙箱 TCP 是 `CONNECT`。`ConnectionRefusedError` 不是策略拒绝。`dns+nft` 仍是未跑的任务配置，不是已经证明的产品级失败。
3. 内存门要看该 profile 的 cgroup `memory.max`、`memory.current`、`memory.swap.max` 和 `OOMKilled`。现有分配 stdout 不够。原始 `memory_ceiling=failed` 保留，有效结论是 unknown。
4. egress auth token 放在容器标签里，Docker API 可以读到。正式采用前不能把凭据留在元数据里。
5. 不要修改宿主机 sysctl 或 Docker runtime。`bootstrap.execd.isolation` 是任务级扩展，这次没有设置；本结果不增加对它的一概禁止。

## 10. 证据等级和清理

直接观察：`lab/receipts.json`、两份服务器日志、`proxy-diagnosis.json` 中的 bwrap 行、`preflight/host-raw.txt`、本机 wheel sha256、`gh attestation` 的退出文件、体积和卷集合对比。横幅和 nftables 行来自第一次沙箱的 `docker logs` 摘录，容器已经删除，原文在 `preflight/execd-egress-banner.txt`。

上游原文：按 commit 或 tag 保存的 LICENSE、SECURITY、release-verification、example compose、Cube README / LICENSE / quick start。

没有验证：cosign 签名、index 内 attestation blob 的内容、这条 OpenSandbox profile 的 MemorySwap 与 cgroup、Cube 的真实 API、浏览器接管、`dns+nft`、gVisor、Kata、fast-sandbox。这些都不是通过。原始 `lab/receipts.json` 没有改写。回放脚本现在写入新的 run 目录。`run_lab.sh` 第二次启动服务器时追加 `server.log`，并写入 restart 标记，不再截断该次运行的第一段日志。原始 `lab/server.log` 不在这条路径上。本修正没有重跑整套探针，也没有实跑验证 OpenSandbox SDK 在创建失败时已有的 `kill_sandbox`。

清理时没有匹配的任务容器、sidecar 或 `opensandbox-runtime-*` 卷。`docker volume ls` 最后一列与探针前同为 29 个，增减都是空。没有 prune 共享 Docker 状态。`/dev/disk3s1s1` 的 Used 一直是 14213904 个 1KiB 块；Available 的变化不能当成这次任务的文件增量。保留 venv（264188157 字节）、两个 wheel、两张任务镜像（execd 185MB、egress 564MB）和全部收据。`lab/api-key` 仍是 mode 600，没有写入文档。
