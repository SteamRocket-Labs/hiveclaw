# HTML 成果预览的 E4 隔离验证

当前实现：HTML 在进入 `srcdoc` 前由浏览器的 `DOMParser` 净化并复核，链接不再导航；空 `sandbox` 和 CSP 保留。第二轮的隔离结论被 Chrome 156 的 prerender 反例推翻，第三轮验证及限制见文末。第一轮允许脚本的方案仍不成立。

## 第一轮浏览器结果

| 浏览器 | 良性文本、内联样式和脚本 | 指定的资源请求和越权探测 | 预览框自身跳转 | E4 |
|---|---|---|---|---|
| Chrome 156.0.8078.81 | 通过 | 通过 | 本机监听器收到 1 个请求 | 不通过 |
| Chromium 148.0.7778.96 | 通过 | 通过 | 本机监听器收到 1 个请求 | 不通过 |
| Firefox | 未安装，未测试 | 未测试 | 未测试 | 未验证 |
| WebKit | 未安装，未测试 | 未测试 | 未测试 | 未验证 |

验证使用同一 HTTP 源上的父页面和 `srcdoc` 预览框，只有本机监听器可以访问。预览框没有 `allow-same-origin`、顶层导航、弹窗、表单或下载权限。

CSP 包含 `default-src 'none'`、`connect-src 'none'`，图片只允许 `data:`，样式和脚本只允许内联与 `data:`；同时设置 `base-uri 'none'`、`form-action 'none'`、`object-src 'none'`、`frame-src 'none'`、`worker-src 'none'`。额外尝试的 `navigate-to 'none'` 被两个浏览器报告为无法识别。

## 阻断范围与反例

`fetch`、XHR、WebSocket、图片和 CSS `url()` 均未到达监听器。读取 cookie、localStorage、父页面 DOM 和父页面 cookie 抛出 `SecurityError`。修改顶层地址被拒绝，`window.open` 返回 `null`，表单未提交，父页面地址和 DOM 保持原样。

`postMessage` 可以发出；沙箱不负责阻止消息。本次父页面探测夹具没有消息处理器，因此没有业务反应。这不构成 HiveClaw 应用忽略消息的集成验收。

以下脚本仍可导航预览框自身，监听器实际收到对应请求：

```html
<script>location.replace('/attempt/self-navigation')</script>
```

顶层导航权限的缺失没有阻止预览框自身导航，资源加载的 CSP 也没有拦截这条路径。即使目标只是部署所在的同一个域名，请求仍违反“不能访问应用或网络”的要求；只验证资源加载会遗漏这个反例。

## 第一轮交付边界

此次只记录 E4 结论，没有修改生产代码或启用 HTML 执行，也没有调整部署 CSP。浏览器夹具、结构化结果、控制台输出和 1440/390 宽截图保存在本次 E4 证据包中。

D3b 的真实应用红绿验证、组件测试、构建、i18n 和 TypeScript 检查未执行：E4 前置条件失败，未进入实现阶段。此次没有验证下载回退的新增行为。

## 第二轮：静态预览

以下是第二轮的历史结果，不能作为当前隔离验收：当时没有逐向量的无隔离对照，也没有覆盖 prerender、声明式影子文档和 SVG 动画恢复链接。

预览框使用空 `sandbox`，不授予脚本、同源、表单、弹窗、下载或顶层导航权限。受信的 CSP 和 `x-dns-prefetch-control: off` 元标签位于成果内容之前；`script-src 'none'`，其余网络限制保持不变。不需要解析或移除 HTML 元素，也不需要独立预览域名。

| 浏览器 | 静态文本、标题、表格、内联 CSS | 自动请求 | TCP 连接 | 父页面消息 | E4 |
|---|---|---|---|---|---|
| Chrome 156.0.8078.81 | 通过 | 0 | 0 | 0 | 通过 |
| Chromium 148.0.7778.96 | 通过 | 0 | 0 | 0 | 通过 |

在真实隔离应用的普通成员会话中，以下 31 项逐项及合并验证均通过：

- 脚本中的 fetch、XHR、WebSocket、顶层导航、弹窗、cookie、localStorage、父 DOM、postMessage、预览框自身导航、表单提交；脚本均不执行。
- 图片、CSS `url()`、meta refresh。
- link 的 preconnect、prefetch、preload、dns-prefetch、stylesheet。
- img srcset、picture/source、video poster/src、audio src。
- object、embed、SVG image。
- CSS @import、@font-face、background 属性。
- base 配合相对资源、带 autofocus 的表单。

监听器同时记录 HTTP 请求和 TCP 连接，以上自动访问阶段两者均为 0。父页面 cookie、storage、页面内容和地址保持原样。成果预览没有注册消息处理器；页面脚本关闭，无法向应用发送消息。

成员主动点击普通链接后，浏览器在预览框内部导航到链接目标，监听器收到点击触发的请求；应用页面不导航。`target="_top"` 和 `target="_blank"` 链接均被沙箱阻止。这与自动访问阶段分开记录。

`.html` / `.htm` 使用现有成果内容和下载接口，兼容旧的 text/download 预览类型。预览可切换到原始源码，下载字节与原文件一致。页面脚本不会执行；依赖脚本的图表等内容按静态页面显示。内容超过 1,000,000 个 UTF-16 代码单元时显示源码和下载提示。

修前真实页面显示 HTML 源码且没有 iframe；修后两浏览器均显示静态页面。相关组件测试 14 项、i18n 检查、构建和 TypeScript 编译通过。两种宽度的良性与恶意页面截图、逐项连接计数保存在第二轮证据包中。

## 第三轮：解析移除与惰性链接

`htmlPreviewDocument` 在应用内解析成果，删除以下元素后序列化，再解析一次检查。若重新出现被禁止的元素或链接属性，再净化一次；两轮后仍不稳定则只显示原始源码和下载，不创建预览框。受信 CSP 和 DNS 关闭元标签最后加入，成果自己的元标签不能覆盖它们。

| 移除内容 | 原因 |
|---|---|
| 所有 `link` | 包含绕过 CSP 的 prerender，以及 DNS 预取、预连接和资源提示；不依赖 rel 的拼写或实体编码 |
| 成果中的 `meta`、`base` | 阻止恢复 DNS 预取、刷新和改写相对地址 |
| 所有 `script` | 包含 speculation rules；静态预览不执行页面脚本 |
| `iframe`、`frame`、`object`、`embed`、`portal` | 删除可承载另一份文档的入口，包括嵌套 srcdoc |
| `template` | 删除声明式影子文档及其中的资源提示 |
| SVG `set`、`animate` | 即使脚本关闭，也能在初次净化后恢复链接地址；监听器已复现点击请求和连接 |
| HTML/SVG 的 `a`、`area` 上的 `href`、`xlink:href`、`ping`、`target` | 使链接和图片映射惰性，保留文字并把地址放入 `title` |

空 `sandbox`、`srcdoc`、`referrerPolicy="no-referrer"` 和既有 CSP 保留，不增加任何 sandbox 权限。内联样式与 data 图片仍可显示；普通外部资源由 CSP 阻断。SVG 成果继续用图片显示和下载，按后缀或 MIME 识别 SVG 后不提供新窗口入口；其他图片保持原有行为。HTML/HTM 成果行在中英文中显示“预览”/“Preview”。

验证通过真实浏览器加载 BASE（`527c8156`）与 HEAD 的预览构造函数，网络计数从调用 DOMParser 前开始。每项先在无 sandbox、无 CSP 的顶层文档运行，再运行 BASE 和 HEAD；noscript 对照关闭页面脚本，使浏览器实际解析 noscript 内容。监听器同时记录请求和 TCP 连接；DNS/预连接使用逐项唯一域名、环回解析映射和浏览器 net-log。只有顶层对照产生请求、连接或解析事件的项才计入已验证范围。

点击对照另外覆盖普通、顶层、新窗口、ping、SVG href、SVG xlink，以及 SVG set/animate 恢复地址；每项均在没有父 CSP 和带 nginx CSP 两种父页面中运行。点击目标使用数值环回地址，避免把不可观测的域名解析误当作点击已阻断。

最终无头浏览器监听运行退出 0，共 154 项、124 项有效对照，HEAD 没有泄漏：

| 浏览器 | 对照可观测 / 不可观测 | HEAD 请求 / TCP 连接 / 解析事件 | 已测范围 |
|---|---|---|---|
| Chrome 156.0.8078.81 | 64 / 13 | 0 / 0 / 0（77 项） | 通过 |
| Chromium 148.0.7778.96 | 60 / 17 | 0 / 0 / 0（77 项） | 通过 |
| Firefox | 应用和自动化引擎未安装 | 未测试 | 未验证 |
| Safari 18.6 | 已安装；驱动因未开启远程自动化拒绝创建会话，未改设置 | 未测试 | 未验证 |

Chrome 的 BASE 对 plain、noscript、嵌套 srcdoc、实体编码的 prerender 均发出 2 请求 / 1 连接（包含子资源），声明式影子文档中的 prerender 也是 2/1；frame 为 0 请求 / 1 连接。HEAD 均为 0/0。Chromium 148 的四个 prerender 顶层对照和 BASE 都没有触发，不计入该浏览器的有效验证。两浏览器的普通、SVG、set/animate 链接在 BASE 无父 CSP 时实际导航；Chrome 带 nginx CSP 时仍建立连接。所有 32 个点击对照在 HEAD 均为 0/0。

两浏览器中不可观测的自动项为：仅执行读取或消息的 cookie/storage/parentDOM/postMessage、没有表单的原始 scriptForm、preconnect、dns-prefetch、只含 autofocus 的 form、内容 meta 开启 DNS 后的链接、SVG use、track、icon、portal。Chromium 另有四个 prerender 项。DNS/预连接的唯一域名在顶层对照中也未产生解析事件或连接，因此没有证明这两项被阻断；没有将监听器的零计数写成已验证。另有包含真实表单的脚本提交对照，确实发出请求且被 HEAD 阻断。

普通成员从真实会话成果卡打开 HTML 和 SVG，在两浏览器中验证静态内容、内联样式、原始源码、下载字节一致、大文件回退、title 地址和惰性点击、SVG 无新窗口入口；父 cookie/storage 不变、消息和 SVG 脚本副作用均为零。1440 和 390 宽截图已保存。相同新增测试在 BASE 为 25 失败 / 6 通过；HEAD 五个成果测试文件 54 项通过，构建、i18n（八项 gate 为零，28 项测试）及 `tsc --noEmit` 通过。生产代码净增 25 行。

已知限制：

- 大于 1,000,000 个 UTF-16 代码单元时仍显示完整源码和下载提示；大量内嵌 base64 会出现在源码中，没有调整上限或大文件体验。
- 非 UTF-8 HTML 仍可能由既有内容接口返回 `[binary artifact snapshot…]` 标记，没有新增解码或后端处理。
- 页面脚本和 SVG set/animate 不执行；依赖它们的图表或动画不能完整呈现，原始源码和下载保留。
- Playwright 页面截图不包含浏览器原生 title 提示浮层；悬停地址由实际 title 属性及点击前后监听计数另行断言，截图没有添加模拟提示。
