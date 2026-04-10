# Hiveclaw 飞书模块官方文档实态审计

日期：2026-04-10  
仓库：`/Users/rocky243/vc-saas/hiveclaw`

## 审计目标

本轮审计只回答一个问题：

> 以当前代码实态为准，对照飞书官方文档，Hiveclaw 的飞书模块现在是否已经基本对齐，是否还存在会影响真实工作流的缺口。

本报告基于：

1. 当前仓库中的真实实现。
2. 飞书官方文档正文。
3. 当日已经通过的飞书相关回归测试。

本轮不做新的业务代码修改，只固化最终审计结论。

## 本地核查的关键文件

- `backend/app/api/feishu.py`
- `backend/app/api/tenant_channels.py`
- `backend/app/api/tools.py`
- `backend/app/services/feishu_service.py`
- `backend/app/services/feishu_ws.py`
- `backend/app/services/auth_provider.py`
- `backend/app/services/agent_tool_domains/feishu_calendar.py`
- `backend/app/services/agent_tool_domains/feishu_tasks.py`
- `backend/app/services/agent_tool_domains/feishu_approval.py`
- `backend/app/templates/system_skills/feishu-integration/SKILL.md`
- `frontend/src/components/FeishuRuntimeStatusCard.tsx`
- `frontend/src/api/domains/tools.ts`

## 官方文档基线

本次判断直接对照以下官方页面：

- 获取授权码  
  https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code
- 获取 `user_access_token`  
  https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token
- 接收并处理回调  
  https://open.feishu.cn/document/event-subscription-guide/callback-subscription/receive-and-handle-callbacks
- 处理卡片回调  
  https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks
- 流式更新卡片  
  https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview
- 创建任务评论  
  https://open.feishu.cn/document/task-v2/comment/create
- 查询主日历忙闲  
  https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/calendar-v4/freebusy/list
- 创建日程  
  https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/calendar-v4/calendar-event/create-event
- 更新日程  
  https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/calendar-v4/calendar-event/patch
- 创建审批实例  
  https://open.feishu.cn/document/server-docs/approval-v4/instance/create

## 当日已通过的相关验证

```bash
cd /Users/rocky243/vc-saas/hiveclaw/backend
pytest tests/api/test_feishu_identity_auth.py \
  tests/api/test_feishu_webhook_security.py \
  tests/api/test_feishu_streaming_cards.py \
  tests/api/test_tools_api_surface.py \
  tests/services/test_feishu_service_api.py \
  tests/services/test_feishu_ws.py \
  tests/services/test_feishu_handler_runtime.py \
  tests/services/test_feishu_base_tasks_runtime.py \
  tests/services/test_pack_skill_alignment.py

cd /Users/rocky243/vc-saas/hiveclaw/frontend
npm run test -- FeishuRuntimeStatusCard ChannelConfig WorkspaceRemainingSections
npm run build
```

当日结果：

- 后端回归子集：通过
- 前端相关组件测试：通过
- 前端构建：通过

注意：测试通过说明现有改动自洽，不等于“所有飞书官方能力都已 100% 覆盖”。

## 总结结论

我对最终结论的置信度是 **92%**。

当前飞书模块已经不再是“主链存在明显官方偏差”的状态。更准确的判断是：

- `OAuth/SSO`、`tenant/per-agent webhook`、`Tasks comment OpenAPI`、`Calendar agent-first 语义`、`CardKit streaming 主链` 这些此前最关键的偏差，当前代码已经基本收口。
- 现在**还能明确指出的剩余问题只有 2 个高优先级和 2 个中优先级**，而且它们不再是“整个模块主链不可用”的级别，而是“交互面泛化程度”和“状态语义精度”上的剩余缺口。

如果只允许一句话总结：

> 当前飞书模块主干能力已经基本成立，可以支撑真实工作流；但还不能诚实地说“与官方文档完全对齐且没有明显剩余风险”，因为新版通用卡片回调面和 CardKit 真实验证语义仍有缺口。

## 已经基本对齐的部分

### 1. OAuth / SSO 主链

当前代码已经切到官方当前写法：

- 授权页使用 `https://accounts.feishu.cn/open-apis/authen/v1/authorize`
- 使用 `client_id`、`response_type=code`、`redirect_uri`、`state`
- token exchange 使用 `POST /open-apis/authen/v2/oauth/token`

这与官方当前 OAuth 文档一致。

### 2. Webhook 安全链路

当前 per-agent webhook 与 tenant webhook 都已经做到：

- 先验签或校验 verification token
- 在存在 `encrypt` 时先解密
- 再处理 `challenge`
- 再解析事件

这与官方“开发者服务器接收回调”的安全处理顺序一致。

### 3. Tasks 评论 OpenAPI

当前 `feishu_task_comment` 已改为：

- `POST /open-apis/task/v2/comments`
- body 带 `content`
- body 带 `resource_type="task"` 和 `resource_id=<task_id>`

这与官方任务评论接口一致。

### 4. Calendar 能力的“平台语义”

当前 Calendar 能力已经不再伪装成“直接管理用户个人日历”，而是明确收口为：

- 查候选参与人的忙闲
- 在 agent/bot 自己的主日历上建会
- 邀请参与人
- 后续只更新或取消 agent 自己创建的会议

这个模型和你们的平台语义是一致的，而且也与飞书日历 API 的权限现实更匹配。

### 5. CardKit 流式发送主链

当前主回复链路已经做到：

- 创建卡片实体
- 发送卡片实体
- 流式更新文本
- 结束后关闭 `streaming_mode`
- 再进入最终更新或交互更新阶段

这与官方流式更新卡片文档的主流程是一致的。

## 仍然存在的高优先级缺口

### A. 新版 `card.action.trigger` 仍然没有真正做成“通用回调面”

当前代码已经能分发新版 `card.action.trigger`：

- `process_feishu_event()` 能识别 `card.action.trigger`
- `feishu_ws.py` 也注册了 `card.action.trigger`

但分发后的实际处理仍然统一落到 `feishu_card_callback()`，而这个处理器本质上还是：

- 只理解审批卡的 `approval_id + action`
- 只返回审批卡场景需要的卡片响应
- 没有做新版通用卡片回调应有的通用解析与业务分流

所以现在的状态是：

- “事件类型路由”已经对齐
- 但“新版通用卡片交互能力”还没有完全对齐

这会限制后续非审批类交互卡片的扩展。

### B. 全局 `/channel/feishu/card-callback` 仍未做到官方意义上的安全处理

官方文档明确说明：

- 开发者服务器模式下，卡片回调可以做安全校验
- 配置了 Encrypt Key 时，应按回调安全流程处理

当前全局 `feishu_card_callback()` 仍然是：

- 直接 `await request.json()`
- 没有验签
- 没有解密
- 没有 verification token 校验

这在“审批卡已知闭环”里可能还能工作，但从官方文档对齐角度看，它还不是一个完整、通用、正式安全化的卡片回调入口。

## 中优先级缺口

### C. `cardkit_verified` 目前验证的是“能创建卡片实体”，不是“全链路可发可更”

当前 runtime probe 已经比过去准确很多，但它的主动验证仍然偏窄：

- 当前 probe 主要验证 `create_card_entity`
- 还没有覆盖：
  - 发送 interactive message
  - 流式更新文本
  - 关闭 streaming mode
  - 最终更新卡片

所以 `cardkit_verified=true` 当前更准确的含义是：

> CardKit create 能力已验证

而不是：

> CardKit 全链路发送与更新能力已验证

这不是主链错误，但会让运维语义仍然略显乐观。

### D. OAuth GET callback 还没有显式处理 `error=access_denied`

官方授权文档明确写了拒绝授权时：

- 浏览器会回调 `redirect_uri?error=access_denied&state=...`

当前 GET callback 主逻辑仍以 `code` 成功分支为中心，没有显式对这个失败分支做专门处理页面。

这不会影响成功授权主链，但它仍是一个标准兼容性缺口。

## 风险分级结论

### 可以认为已经过线的能力

- Feishu OAuth 成功授权主链
- provider-driven identity 与绑定链路
- per-agent webhook 安全链路
- tenant webhook 安全链路
- Tasks CRUD / comment 主链
- Docs / Base / Approval 主链
- Calendar 的 agent-first 会务工作流
- CardKit streaming 的发送主链

### 还不能说“完全收完”的能力

- 新版通用卡片交互回调
- 卡片回调统一安全处理
- CardKit runtime “verified” 语义精度
- OAuth 拒绝授权分支的用户态处理

## 最后判断

我不会再给出“当前飞书模块还有很多结构性问题”的结论。那已经不符合当前代码实态了。

我现在会给出的更准确判断是：

1. 当前飞书模块主干已经基本成立。
2. 以平台真实工作流为目标，它已经能支撑：
   - 身份绑定与登录
   - 入站消息
   - Agent 卡片式回复
   - Docs / Base / Approval / Tasks
   - Agent 发起会议并邀请人
3. 还剩下的主要问题已经收敛到“卡片交互面通用化”和“状态语义精度”。

因此，当前最诚实的结论是：

> 我对“飞书模块主干已经可用、官方高优先级差异已大幅收敛”这件事的置信度是 92%；但我对“已经 100% 完成官方文档实态对齐”不会给满分，因为新版通用卡片回调面和 CardKit 验证语义仍有剩余工作。

## 建议的最后收尾顺序

如果要把置信度从 92% 再推到 95% 以上，建议只做这 4 件事，且按这个顺序：

1. 把 `/channel/feishu/card-callback` 做成真正的通用、安全化入口。
2. 将 `card.action.trigger` 从“审批专用分支”改成“通用分发 + 审批子处理器”。
3. 把 `cardkit_verified` 拆成更细的 probe，至少区分 `create_verified` 与 `send_verified`。
4. 为 OAuth GET callback 补 `access_denied` 的用户态处理页面与测试。
