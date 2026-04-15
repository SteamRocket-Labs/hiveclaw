# 分支修复顺序与接入规则

## 1. 前提

只有当以下条件全部满足，才允许进入分支修复：

1. 主干局部回归全部通过
2. 全量主干回归通过
3. Legacy 删除完成到可控状态

---

## 2. 分支修复原则

每个分支只做三件事：

1. 接主干契约
2. 删除绕过主干的私有实现
3. 补本分支的集成测试

分支禁止事项：

- 自己定义新的执行语义
- 自己定义新的 session 语义
- 自己定义新的 delegation 语义
- 自己定义新的 trigger 语义

---

## 3. 修复顺序

### B1 渠道分支

优先级最高，因为最容易绕过 session / trigger / tool runtime 主干。

包括：

- Feishu
- Slack
- Discord
- Dingtalk
- WeCom
- Telegram
- WeChat Personal
- Teams
- Email

### B2 Desktop 分支

原因：

- 它直接碰 agent hierarchy / sync / audit

### B3 产品分支

- Plaza
- Notification
- Enterprise
- Admin

### B4 扩展能力分支

- MCP
- Packs
- Capability policy
- Feature flags
- Role templates

---

## 4. 分支验收问题单

每个分支必须回答：

1. 这个分支依赖哪条主干？
2. 是否还有自己私有的核心语义？
3. 是否还存在旧写路径？
4. 是否有直接绕过主干的调用？
5. 是否有独立 legacy merge 逻辑？

只要其中任一答案为“是”，就不能算完成。

---

## 5. 分支结束标准

1. 分支完全接主干契约
2. 无绕过主干的实现
3. 分支集成测试通过
4. 不新增新的兼容层

