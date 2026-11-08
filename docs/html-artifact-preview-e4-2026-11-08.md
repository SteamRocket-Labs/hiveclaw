# HTML 成果预览的 E4 隔离验证

结论：`sandbox="allow-scripts"`、`srcdoc` 和禁止网络资源加载的 CSP，不能满足任意 HTML 预览“连不上网络”的要求。D3b 的可执行预览暂不启用，回退方向为下载。

## 浏览器结果

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

## 交付边界

此次只记录 E4 结论，没有修改生产代码或启用 HTML 执行，也没有调整部署 CSP。浏览器夹具、结构化结果、控制台输出和 1440/390 宽截图保存在本次 E4 证据包中。

D3b 的真实应用红绿验证、组件测试、构建、i18n 和 TypeScript 检查未执行：E4 前置条件失败，未进入实现阶段。此次没有验证下载回退的新增行为。
