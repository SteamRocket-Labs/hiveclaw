# Hiveclaw vs Clawith 飞书模块审计

审计时间：2026-04-09

范围：
- `backend/app/api/feishu.py`
- `backend/app/services/feishu_service.py`
- `backend/app/services/feishu_ws.py`
- `backend/app/services/agent_tools.py`
- `backend/app/tools/handlers/feishu.py`
- `backend/app/services/agent_tool_domains/feishu_*.py`
- `backend/app/api/tenant_channels.py`
- `backend/app/services/org_sync_service.py`
- `frontend/src/components/ChannelConfig.tsx`
- `frontend/src/components/FeishuRuntimeStatusCard.tsx`

## 一、先纠正参考结论里的错误

以下结论经代码核对后确认不准确：

1. `Hiveclaw` 不是“没有 OrgMember / 组织映射概念”。
   - 证据：`backend/app/models/org.py`
   - 证据：`backend/app/services/org_sync_service.py`
   - 证据：`backend/app/api/feishu.py`
   - 实际情况：`Hiveclaw` 仍然保留 `OrgMember`，只是没有 `Clawith` 那种 `IdentityProvider + SSOScanSession + ChannelUserService + AuthProvider` 的统一身份层。

2. `Hiveclaw` 不是“完全缺少多租户飞书体系”。
   - 证据：`backend/app/models/tenant_channel_config.py`
   - 证据：`backend/app/api/tenant_channels.py`
   - 实际情况：它新增了企业级单租户单 Bot 路由，但身份解析仍然依赖 `User.feishu_*` 字段，不是 provider-driven。

3. `Hiveclaw` 的问题不只是“能力缺失”，更大问题是“能力分散”。
   - 认证在 `api/feishu.py` + `feishu_service.py`
   - 组织同步在 `org_sync_service.py`
   - 路由在 `tenant_channels.py`
   - 工具门控在 `agent_tools.py`
   - 工具实现拆到了 `agent_tool_domains/`

## 二、确认存在的关键回退

### 1. 服务层被明显削薄

`Clawith` 的 `backend/app/services/feishu_service.py` 包含：
- `_parse_api_response()`
- `get_tenant_access_token()`
- `_get_lark_client()`
- CardKit 相关 5 个方法
- Bitable / Docs / Approval 服务方法

`Hiveclaw` 的同文件只保留：
- OAuth 基础
- 登录/绑定
- send/patch
- open_id / user_id 解析
- 审批卡片发送
- 文件下载上传

结论：
- `Clawith` 是“飞书能力服务层”
- `Hiveclaw` 更像“飞书消息与登录薄封装”

### 2. CardKit 流式链路真实缺失

`Clawith` 在 `backend/app/api/feishu.py` 中：
- 先 `create_card_entity()`
- 再 `send_card_by_card_id()`
- 流式阶段 `stream_card_content()`
- 完成后 `set_card_streaming_mode()` + `update_cardkit_card()`

`Hiveclaw` 只做：
- 发送一张普通 interactive card
- 周期性 `patch_message()`

影响：
- 无原生 CardKit 流式能力
- 无稳定的工具状态区
- 无 `SerialPatchQueue`
- 无 heartbeat 刷新防抖
- 高并发下更容易出现 patch 覆盖和展示抖动

### 3. API 响应校验回退

`Clawith` 的 `send_message()` / `patch_message()` 会统一走 `_parse_api_response()`。

`Hiveclaw` 当前直接 `resp.json()` 返回，业务错误码可能被静默透传。

这是实打实的工程回退，不是架构差异。

### 4. 认证从 provider-driven 退回到 user-field-driven

`Clawith`：
- `FeishuAuthProvider`
- `IdentityProvider`
- `SSOScanSession`
- `ChannelUserService`
- `Identity` / tenant-scoped `User`

`Hiveclaw`：
- `User.feishu_open_id`
- `User.feishu_union_id`
- `User.feishu_user_id`
- `login_or_register()` 直接写 `User`
- 入站消息身份解析大量写在 `api/feishu.py`

影响：
- OAuth、组织同步、消息路由、审批回调共享同一批“用户字段”，耦合度高
- 多 app / 多身份源 / 跨租户演进难度大
- 去重与绑定逻辑容易在不同入口重复实现

## 三、确认是增强而不是退化的部分

### 1. Webhook 安全显著增强

`Hiveclaw` 有：
- `X-Lark-Signature` 验签
- AES 解密
- `verification_token` 回退校验

`Clawith` 对 webhook 安全几乎没有防护。

这个不能回退。

### 2. 企业级单 Bot 路由是新增能力

`Hiveclaw` 新增：
- `TenantChannelConfig`
- `/api/tenant-channels/*`
- `/api/channel/feishu/tenant/{tenant_id}/webhook`

这不是坏设计，本质上是对企业部署更合理的补充。
但它目前仍建立在脆弱身份解析上。

### 3. 工具体系拆分和 CLI 旁路是新增能力

`Hiveclaw` 新增：
- `feishu_sheet_info`
- `feishu_sheet_read`
- `feishu_task_list/create/complete/comment`
- `feishu_base_record_upload_attachment`
- `lark-cli` 运行时旁路
- 运行时状态卡

这部分不该回滚。

## 四、确认的缺口清单

### P0

1. 缺少统一 Feishu API 响应校验
2. 缺少 CardKit 流式主链路
3. 缺少 patch 串行化队列与 heartbeat 防抖
4. 工具状态无法稳定反映到飞书卡片

### P1

1. 缺少 `get_tenant_access_token(app_id, app_secret)` 这种标准化 token 获取入口
2. 缺少 Lark SDK client 缓存层
3. OAuth / inbound message / org sync 三套身份逻辑没有统一抽象
4. macOS WebSocket 代理绕过被删，开发环境兼容性下降

### P2

1. 缺少 `feishu_approval_create/query/get`
2. 缺少 `feishu_drive_delete`
3. Bitable 旧接口名不兼容，迁移说明不足
4. 前端缺少“先完成组织同步再配飞书”的明显引导
5. 前端缺少 basic/full 权限包切换

## 五、当前更危险的根因

最危险的不是“少了几个 API”，而是下面三件事叠加：

1. 身份体系不统一
   - OAuth 直接写 `User.feishu_*`
   - org sync 也写 `User.feishu_*`
   - tenant webhook 路由靠 `User.feishu_*`
   - inbound message 还在 `api/feishu.py` 内重复做匹配和自动建人

2. 消息流式展示不统一
   - 主链只有 `patch_message`
   - 无 CardKit 主路径
   - 无 tool status 生命周期模型

3. 工具体系已经拆域，但服务层和认证层没同步抽象
   - 结果就是“调用点多、边界不清、排障困难”

## 六、90% 以上置信度的优化方案

### 方案原则

不建议全量回滚到 `Clawith`。

原因：
- 会丢失 webhook 安全增强
- 会丢失 tenant-level channel 设计
- 会丢失 CLI 旁路与新工具
- 会把当前已经存在的企业部署路径打碎

建议路线：

### Phase 1：先补护栏，不动大架构

目标：1 到 2 天内把最容易出事故的点补齐。

动作：
1. 从 `Clawith` 迁回 `_parse_api_response()`，统一收敛到 `FeishuService`
2. 给 `send_message()` / `patch_message()` 补 `stage` 参数和错误日志
3. 补 `get_tenant_access_token(app_id, app_secret)`
4. 恢复 `_get_lark_client()` LRU 缓存
5. 恢复 `_SerialPatchQueue`
6. 给当前 patch 流式链路补 `on_tool_call`、heartbeat、hash 去抖

预期收益：
- 先把观测性和稳定性补起来
- 不改数据库模型
- 风险最低

### Phase 2：恢复 CardKit，但保留 patch fallback

目标：恢复上游最成熟的飞书流式体验，但不牺牲当前兼容性。

动作：
1. 将 `create_card_entity()` / `send_card_by_card_id()` / `stream_card_content()` / `set_card_streaming_mode()` / `update_cardkit_card()` 迁回 `FeishuService`
2. 在 `api/feishu.py` 恢复“CardKit 优先，patch 降级”的双路径
3. 保留现有 webhook 安全校验
4. 保留现有文件发送 fallback
5. 给 CardKit 路径补测试：
   - 创建失败时自动降级
   - 流式 sequence 单调递增
   - 最终 finalize 正常关闭 streaming mode

预期收益：
- 飞书端交互体验回到可接受水平
- 降低 patch 覆盖/闪烁问题

### Phase 3：抽出统一 FeishuIdentityResolver

目标：解决当前最根本的“身份逻辑四处分叉”。

建议新增：
- `backend/app/services/feishu_identity_resolver.py`

统一职责：
1. `resolve_from_oauth()`
2. `resolve_from_inbound_message()`
3. `resolve_from_org_sync_member()`
4. `link_existing_user_by_email()`
5. `create_or_update_user_binding()`

注意：
- 这一阶段先不要一次性引入 `IdentityProvider + Identity + SSOScanSession` 全套数据库迁移
- 先把逻辑抽象统一，再决定是否升级模型

原因：
- 当前仓库已有 `OrgMember`
- 但没有完整 identity schema
- 贸然照搬 `Clawith` 身份模型，迁移代价和兼容风险太高

### Phase 4：补回缺失工具，但遵守当前命名体系

目标：补能力，不制造第二套接口。

建议：
1. 保持现有 `feishu_base_*` 命名，不回滚成 `bitable_*`
2. 新增：
   - `feishu_approval_create`
   - `feishu_approval_query`
   - `feishu_approval_get`
   - `feishu_doc_delete` 或 `feishu_drive_delete`
3. 若要兼容上游 prompt/skill，可增加别名层，不要改现有主名字

### Phase 5：前端补正确引导，不只是“能填表”

动作：
1. 恢复 “先完成组织同步” 提示
2. 恢复 Feishu 权限 JSON 的 basic/full 切换
3. 保留 `FeishuRuntimeStatusCard`
4. 在状态卡中明确区分：
   - channel auth
   - tenant channel auth
   - CLI auth
   - CardKit enabled

## 七、不建议照搬的部分

以下内容不建议原样搬回：

1. `Clawith` 的 webhook 安全缺失
2. 完全回到 per-agent single-bot 设计
3. 直接把全部工具重新塞回单文件 `agent_tools.py`
4. 在当前阶段直接引入全套 identity schema 改造

## 八、推荐实施顺序

1. `feishu_service.py` 护栏补齐
2. `api/feishu.py` 流式链路恢复 CardKit + patch fallback
3. 测试补齐
4. 身份解析统一抽象
5. 缺失工具回补
6. 前端配置体验补完

## 九、验证命令

```bash
cd /Users/rocky243/vc-saas/hiveclaw/backend
pytest backend/tests/api/test_feishu_webhook_security.py
pytest backend/tests/services/test_feishu_base_tasks_runtime.py
pytest backend/tests/services/test_feishu_cli_runtime.py
pytest backend/tests/services/test_feishu_handler_runtime.py
pytest backend/tests/services/test_feishu_sheets_runtime.py
```

建议在补 Phase 1 和 Phase 2 后新增：

```bash
cd /Users/rocky243/vc-saas/hiveclaw/backend
pytest backend/tests/services/test_feishu_service_api.py
pytest backend/tests/api/test_feishu_streaming_cards.py
pytest backend/tests/api/test_feishu_identity_resolution.py
```

## 十、审计结论

我的结论不是“回滚到 Clawith 就行”，而是：

1. `Clawith` 在飞书消息流式、响应校验、统一身份抽象上更成熟
2. `Hiveclaw` 在 webhook 安全、企业级 tenant channel、CLI 旁路、新工具和运行时诊断上更先进
3. 当前最优解不是二选一，而是：
   - 保留 `Hiveclaw` 的安全与企业能力
   - 回迁 `Clawith` 的消息护栏与 CardKit 主链
   - 再统一身份解析层

置信度：> 90%

理由：
- 关键结论都来自直接代码核对，不依赖参考文档
- 已核对 API、服务层、WS、工具、身份、前端、测试六个维度
- 参考结论中的错误项已被明确排除
