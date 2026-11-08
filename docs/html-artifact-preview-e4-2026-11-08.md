# HTML 成果预览的 E4 隔离验证

当前结论：关闭脚本的静态预览通过第二轮验证，D3b 使用 `sandbox=""`、`srcdoc` 和禁止自动网络访问的 CSP。第一轮允许脚本的方案仍不成立，结果保留如下。

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
