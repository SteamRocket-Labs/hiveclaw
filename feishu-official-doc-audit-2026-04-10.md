# Hiveclaw 飞书模块官方文档对照审计

日期：2026-04-10  
仓库：`/Users/rocky243/vc-saas/hiveclaw`

## 审计范围

本次审计只做两件事：

1. 读取当前仓库里飞书相关真实实现。
2. 对照飞书官方文档，判断当前模块哪些能力已经对齐，哪些只是条件性可用，哪些与官方要求不一致。

本次未做生产代码修改，只做静态审计与现有测试回归。

## 本地核查的关键文件

- `backend/app/api/feishu.py`
- `backend/app/api/tenant_channels.py`
- `backend/app/api/tools.py`
- `backend/app/services/feishu_service.py`
- `backend/app/services/feishu_ws.py`
- `backend/app/services/auth_provider.py`
- `backend/app/services/channel_user_service.py`
- `backend/app/services/agent_tool_domains/feishu_docs.py`
- `backend/app/services/agent_tool_domains/feishu_base.py`
- `backend/app/services/agent_tool_domains/feishu_tasks.py`
- `backend/app/services/agent_tool_domains/feishu_calendar.py`
- `backend/app/services/agent_tool_domains/feishu_approval.py`

## 已执行的验证命令

```bash
cd /Users/rocky243/vc-saas/hiveclaw
pytest backend/tests/services/test_feishu_service_api.py \
  backend/tests/api/test_feishu_streaming_cards.py \
  backend/tests/api/test_feishu_identity_auth.py \
  backend/tests/services/test_feishu_base_tasks_runtime.py \
  backend/tests/services/test_feishu_sheets_runtime.py \
  backend/tests/services/test_feishu_handler_runtime.py \
  backend/tests/services/test_feishu_ws.py \
  backend/tests/api/test_feishu_webhook_security.py

cd /Users/rocky243/vc-saas/hiveclaw/backend
alembic heads
```

结果：

- `36 passed, 0 failed`
- Alembic 仍为单 head

注意：现有测试是绿的，但它们没有覆盖本报告指出的所有官方文档差异点，尤其没有覆盖新版 OAuth 接入、tenant webhook 解密链路、`card.action.trigger` 注册面、`feishu_task_comment` 的 OpenAPI 路径。

## 官方文档基线

本次判断主要基于以下官方文档：

- 获取授权码  
  https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/authorize/get
- 获取 `user_access_token`（最新 OAuth v2）  
  https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token
- 接收并处理回调  
  https://open.feishu.cn/document/event-subscription-guide/callback-subscription/receive-and-handle-callbacks
- 处理卡片回调  
  https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks
- 流式更新卡片  
  https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview
- 创建任务评论  
  https://open.feishu.cn/document/task-v2/comment/create
- 创建审批实例  
  https://open.feishu.cn/document/server-docs/approval-v4/instance/create
- 创建多维表格  
  https://open.feishu.cn/document/server-docs/docs/bitable-v1/app/create

## 总结结论

结论不是“整个模块不可用”，也不是“当前模块已经完全没问题”。

更准确的判断是：

- 当前模块的主干能力是成立的，尤其是 `provider-driven identity`、消息发送、CardKit 流式主链、Docs/Base/审批主干，大体可工作。
- 但如果按“已经系统性对齐飞书官方当前能力要求”来衡量，当前模块还不能给出“没问题”的结论。
- 至少存在 4 个高优先级、直接可证实的官方文档不一致点，足以影响 SSO、tenant webhook 安全模式、交互卡回调覆盖面，以及 Tasks 评论能力。

我对这个总判断的置信度是 **93%**。

原因：

- 高优先级问题都来自“本地代码实态”与“官方文档正文”直接对照，不是推测。
- 正向结论也有本地实现和测试支撑。
- 置信度没有给到 98% 以上，是因为仍有少数边缘点只做了 spot-check，没有逐个能力做真实沙箱联调。

## 分项判断

### A. 身份认证与 OAuth

#### 现状

本地实现：

- `backend/app/api/feishu.py` 使用
  - `https://open.feishu.cn/open-apis/authen/v1/authorize`
  - 查询参数为 `app_id`, `redirect_uri`, `state`
- `backend/app/services/feishu_service.py` 与 `backend/app/services/auth_provider.py` 都使用
  - `POST /open-apis/authen/v1/oidc/access_token`
  - 再调用 `authen/v1/user_info`

#### 官方要求

官方“获取授权码”文档明确写的是：

- 授权页 URL 为 `https://accounts.feishu.cn/open-apis/authen/v1/authorize`
- 参数为 `client_id`, `response_type=code`, `redirect_uri`
- `scope`, `prompt`, `state` 为规范参数

官方“获取 user_access_token”当前推荐路径明确写的是：

- `POST https://open.feishu.cn/open-apis/authen/v2/oauth/token`
- 请求体包含 `grant_type=authorization_code`, `client_id`, `client_secret`, `code`, `redirect_uri`

#### 判断

**高优先级不一致**

- 当前 SSO authorize URL 与官方现行入口不一致。
- 当前参数名仍是 `app_id`，不是官方当前 OAuth 页面使用的 `client_id`。
- 当前 token exchange 仍停留在历史 `v1 oidc` 路径，不是官方当前推荐的 `v2 oauth/token`。

#### 影响

- 现有链路在部分租户或场景下可能仍然能工作，但它不是当前官方推荐接入方式。
- 后续 scope、refresh token、PKCE、prompt、标准 OAuth 库兼容性都会受限。

#### 结论

`OAuth/SSO`：**不建议视为已对齐**

---

### B. Webhook 安全与事件订阅

#### 现状

##### Per-agent webhook

`backend/app/api/feishu.py` 的 per-agent webhook：

- 在有 `encrypt_key` 时先验签
- 若请求体存在 `encrypt` 则解密
- 无加密时走 verification token fallback
- 再处理 challenge 和 event

这条链路整体是合理的。

##### Tenant webhook

`backend/app/api/tenant_channels.py` 的 tenant webhook：

- 在拿到 body 后，先直接返回 `challenge`
- 如果配置了 `encrypt_key`，只做签名校验
- 从头到尾没有对 `body["encrypt"]` 做解密
- 只处理 `im.message.receive_v1`

#### 官方要求

官方“接收并处理回调”明确要求：

- 开发者服务器模式下，若配置了 Encrypt Key，需要先安全校验
- 加密回调需先解密，再解析真实事件
- 收到回调后需在 3 秒内响应
- 长连接模式里会收到 `card.action.trigger` 等卡片回调

#### 判断

##### Per-agent webhook

**基本对齐**

##### Tenant webhook

**高优先级不一致**

问题有两个：

1. `challenge` 在安全校验前就直接返回。
2. 配了 `encrypt_key` 时，只验签，不解密，等于 tenant 路由不真正支持加密回调。

#### 影响

- Tenant webhook 在开启 Encrypt Key 的正式安全模式下，不是完整可用状态。
- 这会直接影响企业级统一 webhook 的真实性。

#### 结论

- `per-agent webhook`：**已基本对齐**
- `tenant webhook`：**高风险缺口，不能视为已对齐**

---

### C. 卡片回调与 CardKit 流式消息

#### 现状

正向部分：

- `backend/app/services/feishu_service.py` 已实现
  - `create_card_entity`
  - `send_card_by_card_id`
  - `stream_card_content`
  - `set_card_streaming_mode`
  - `update_cardkit_card`
- `backend/app/api/feishu.py` 的主回复链路是
  - CardKit 优先
  - patch fallback
  - 结束时关闭 streaming mode 并做最终卡片更新

这和官方 CardKit 流式更新指南是对齐的。

负向部分：

- `backend/app/api/feishu.py` 的事件处理主入口只处理 `im.message.receive_v1`
- `backend/app/api/tenant_channels.py` 也只处理 `im.message.receive_v1`
- `backend/app/services/feishu_ws.py` 的长连接 dispatcher 没有注册 `card.action.trigger`
- 专门的 `/channel/feishu/card-callback` 路由只处理审批卡片的自定义 action，不是官方推荐的通用卡片回调面，且未做签名/解密处理

#### 官方要求

官方“处理卡片回调”明确说明：

- 建议订阅新版 `card.action.trigger`
- 可通过长连接或开发者服务器接收
- 需要在 3 秒内响应
- 若走延时更新，需在响应成功后再用 callback token 更新

官方“流式更新卡片”明确说明：

- 开启流式模式后，收到交互回调前应先关闭 `streaming_mode`

#### 判断

##### CardKit 流式发送主链

**对齐度高**

##### 卡片交互回调

**高优先级缺口**

不是“完全没有卡片能力”，而是：

- 已有“发送和流式更新卡片”的能力
- 但“官方推荐的通用新版卡片回调面”没有接全

#### 影响

- 飞书内交互卡片能力目前是“部分可用”
- 审批卡场景有自定义闭环
- 但通用交互卡、长连接卡片回调、tenant 级卡片事件接入都不完整

#### 结论

- `CardKit streaming`：**已基本对齐**
- `interactive card callback`：**高风险缺口**

---

### D. Tasks 能力

#### 现状

`backend/app/services/agent_tool_domains/feishu_tasks.py` 里：

- 任务创建、更新、完成整体路径大体合理
- 任务评论 OpenAPI 路径实现为：

```text
POST /task/v2/tasks/{task_id}/comments
body={"content": content}
```

#### 官方要求

官方“创建评论”文档明确要求：

- URL 为 `POST /open-apis/task/v2/comments`
- body 需要 `content`
- 如评论属于任务，则通过
  - `resource_type="task"`
  - `resource_id=<task guid>`

#### 判断

**高优先级不一致**

- 当前 `feishu_task_comment` 的 OpenAPI 路径是错的。
- 现有测试之所以是绿的，是因为只覆盖了 CLI fallback，没有覆盖这个 OpenAPI 分支。

#### 影响

- 在启用 OpenAPI token 的环境里，任务评论能力大概率会直接失败。

#### 结论

`feishu_task_comment`：**明确需要修正**

---

### E. Docs / Wiki / Base / Approval

#### Docs

本地实现覆盖了：

- 创建 docx
- 读取 raw content
- 追加块内容
- 创建后自动加协作者

这些主链与官方 Docs/Drive 文档是基本一致的。

#### Base

本地实现覆盖了：

- 创建 Base App
- 列表数据表
- 列字段
- 新增字段
- 新增记录
- 更新记录
- 删除记录
- 附件上传并写回字段

主干 CRUD 和官方文档总体一致。

#### Approval

本地实现覆盖了：

- 创建审批实例
- 查询审批实例列表
- 获取审批实例详情

其服务层路径与官方审批 v4 文档是匹配的。

#### 判断

**这些主干能力基本成立。**

需要保守说明的点：

- Docs / Base 这类能力是否真正“生产可用”，还受文档权限、应用挂载、企业高级权限等条件影响。
- 这不是代码逻辑错误，而是飞书平台本身的权限模型要求。

#### 结论

- `Docs`：**基本对齐**
- `Base`：**基本对齐**
- `Approval`：**基本对齐**

---

### F. Calendar 能力

#### 现状

`backend/app/services/agent_tool_domains/feishu_calendar.py` 的行为是：

- `list`
  - 先查指定用户或消息发送人的 freebusy
  - 再列 bot 自己主日历上的事件
- `create`
  - 创建在 agent/bot 的主日历上
  - `user_email` 更多是拿来解析 attendee
- `update/delete`
  - 也操作 bot 的主日历
  - `user_email` 被要求传入，但并不真正用于选择目标用户的日历

#### 官方要求

官方 Calendar API 要点是：

- 事件是建在具体 calendar 上的
- 对目标 calendar 需要对应身份的写权限
- freebusy、event create、attendee invite 是不同层面的能力

#### 判断

**中高优先级语义偏差**

不是路径错，而是工具语义比名字暗示的更窄：

- 它不是“通用用户日历管理”
- 更准确地说，它是“bot 日历事件管理 + 用户 freebusy 查询 + attendee 邀请”

#### 影响

- 容易误导 agent 或操作者，以为它能直接管理任意用户的日历。
- `update/delete` 的 `user_email` 参数设计是误导性的。

#### 结论

`Calendar`：**条件性可用，但语义和能力面需要重新定义或补强**

---

### G. Runtime 状态判断

#### 现状

`backend/app/api/tools.py` 中的 `cardkit_ready` 判断逻辑只是：

- Lark SDK 已安装
- 存在 channel auth 或 tenant auth

#### 官方要求

CardKit 真正可用，还依赖：

- 机器人能力
- `im:message`
- `im:message:send_as_bot`
- `cardkit:card:write`
- 应用发布/租户安装/权限状态

#### 判断

**中优先级问题**

这不是 API 错误，而是 runtime diagnostics 过于乐观。

#### 影响

- UI 可能会显示 `cardkit_ready=true`
- 但真实发送或更新卡片时仍会因为权限不够失败

#### 结论

`runtime readiness`：**需要收紧，当前会高估可用性**

## 最重要的 4 个必须修复项

如果目标是“系统性对齐官方文档，并能有把握地说飞书模块没问题”，以下 4 项必须修：

1. OAuth/SSO 升级到官方当前 OAuth 入口
   - 授权页改为 `accounts.feishu.cn/open-apis/authen/v1/authorize`
   - 参数改为 `client_id` / `response_type=code`
   - token exchange 改为 `authen/v2/oauth/token`
   - 补 scope / prompt / refresh token 语义测试

2. Tenant webhook 补完整安全链路
   - 先校验再处理 challenge
   - 支持 `encrypt` 解密
   - 解密后再解析 event
   - 增加加密 callback 的 Red/Green 测试

3. 补全新版通用卡片回调面
   - 长连接注册 `card.action.trigger`
   - tenant / developer webhook 支持该事件
   - 通用 card callback 路由做安全校验与响应时限控制
   - 审批卡片交互保留为业务层，不要替代通用回调面

4. 修正 `feishu_task_comment` OpenAPI 实现
   - 改为 `POST /task/v2/comments`
   - body 带 `resource_type=task` 和 `resource_id`
   - 先写失败测试，再修实现

## 第二梯队优化项

这些不是立即阻断，但建议补：

1. `cardkit_ready` 改成真实能力探测，而不是仅看 auth + SDK。
2. Calendar 工具重命名或拆分：
   - `feishu_bot_calendar_*`
   - `feishu_calendar_freebusy_*`
   - 如果要支持真实用户日历，再单独做 user-calendar 模式。
3. 为 Docs / Base / Approval 补“权限不足”专项测试和诊断文案。
4. 为 tenant webhook 和 card callback 增加更多事件类型测试，而不是只测消息收发。

## 最终结论

我对最终结论的置信度是 **93%**：

- 当前飞书模块 **不是整体失效**。
- 但当前飞书模块 **不能被诚实地称为“已经系统性对齐官方文档、能力没问题”**。
- 现阶段更准确的表述是：
  - **消息发送、CardKit 流式主链、身份体系、Docs/Base/审批主干基本成立**
  - **OAuth、tenant webhook 安全模式、通用卡片回调、Tasks 评论 OpenAPI 仍存在明确缺口**

如果只允许一句结论，那就是：

> 当前模块主干可用，但离“官方文档对齐且能力没问题”还差 4 个必须修复项；在这些项补齐前，我不会给出“已经没问题”的结论。
