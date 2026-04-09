# Phase 7: 集成验证

> **依赖**: 全部前序阶段完成
> **交付**: E2E 测试 + 降级测试 + 恢复测试 + 性能基线

---

## 1. 验证目标

验证 4 层金字塔 + 3 个蒸馏器 + 16 个 hooks 的完整闭环:

```
用户发消息 → Agent 响应
  → RESPONSE_COMPLETE hook → 提取器 LLM agent → T2 learnings
  → 同时写 T0 logs/YYYY-MM-DD/chat-*.md
  → 45min 后心跳 tick (持续 session) → T2→T3 策展
  → 4h 后梦境 → T3 精简 + soul 提炼
  → 下一次对话 → frozen prompt 包含最新 T3 + soul
  → 长对话压缩 → PRE_COMPACTION → 提取 → 压缩 → 恢复注入
```

---

## 2. E2E 测试场景

### 2.1 场景 A: 基本记忆流转

```
1. 用户对 Agent 说: "记住，我喜欢用 snake_case"
2. 验证:
   - T0: logs/ 下有 chat-*.md, 包含完整对话
   - T2: learnings/insights.md 有 "- [2026-04-05] 用户偏好 snake_case"
   - (等 45min 或手动触发心跳)
   - T3: memory/feedback.md 有 "snake_case" 相关条目
   - (等 4h 或手动触发梦境)
   - soul.md: Learned Behaviors 有 "I use snake_case" (如果出现 3+ 次)
3. 下次对话:
   - 系统提示词中 Memory section 包含 feedback.md 内容
   - Agent 在生成代码时自动使用 snake_case
```

### 2.2 场景 B: 长对话压缩保全

```
1. 与 Agent 进行 20+ 轮对话 (技术讨论)
2. 触发 85% context 压缩
3. 验证:
   - PRE_COMPACTION hook 触发 → 提取器在压缩前运行
   - 压缩摘要包含 11 个 section
   - Post-compact 恢复注入 T3 内容 (feedback + blocked)
   - 压缩后 Agent 仍记得关键技术决策
```

### 2.3 场景 C: 心跳持续 session

```
1. 创建 Agent, 启用心跳
2. 写入 T2: learnings/insights.md 加几条手动条目
3. 等待第一次 tick:
   - 心跳读取 T2 全量 + T3 参考
   - 策展 T2→T3
   - lineage.md 新增 CUR-* 条目
4. 再写入 T2 几条新条目
5. 等待第二次 tick:
   - <tick> 注入, 只看到新增条目
   - Agent 引用上一次策展结果 ("上次我把 X 放到了 knowledge.md")
   - 不重复处理旧条目
6. 触发梦境:
   - 心跳 session 重置
   - 下次 tick = 首次 (full init)
```

### 2.4 场景 D: 空闲超时 + 关闭

```
1. 用户发一条消息, Agent 回复
2. 等待 180s (SESSION_IDLE 触发)
3. 验证: T0 日志写入 + 提取完成
4. 关闭浏览器 (SESSION_CLOSE 触发)
5. 验证: drain 完成 + 日志确认
```

### 2.5 场景 E: 触发器 + 委托回流

```
1. 配置 cron 触发器 "每天 9:00 查看邮件"
2. 触发器执行
3. 验证:
   - T0: logs/ 下有 trigger-0900-*.md
   - T2: learnings/ 有提取条目
   - TRIGGER_END hook 触发
4. 配置委托: Agent A 委托 Agent B 研究
5. 验证:
   - T0: logs/ 下有 delegation-*.md
   - T2: learnings/ 有提取条目
```

---

## 3. 降级测试

### 3.1 LLM 不可用

| 测试 | 操作 | 期望 |
|------|------|------|
| 提取器降级 | 断开 LLM + 用户说 "不要用 mock" | insights.md 有 pattern-based 提取 |
| 心跳降级 | 断开 LLM + tick 触发 | T2 新条目按 category 机械分配到 T3 |
| 梦境降级 | 断开 LLM + 梦境触发 | 程序化去重 + cap 截断 + 原文复制到 soul |
| 压缩降级 | 断开 LLM + 长对话 | `_extract_summary()` pattern 提取 → 压缩成功 |

### 3.2 DB 不可用

| 测试 | 操作 | 期望 |
|------|------|------|
| MD 完整 | 停 PostgreSQL | T0+T2+T3+soul MD 文件不受影响 |
| Session journal 丢失 | 停 PostgreSQL | session 继续, 无 session 摘要注入, 其他正常 |
| FTS5 不可用 | 删除 SQLite DB | recall 返回空, memory 完整 (MD 有), 梦境重建 FTS5 |

### 3.3 进程重启

| 测试 | 操作 | 期望 |
|------|------|------|
| 心跳 session 丢失 | 重启 backend | `_heartbeat_contexts` 清空 → 下次 tick = 首次 (full init) |
| T2 mtime 丢失 | 重启 backend | `_t2_mtimes` 清空 → 首次 tick 读全量 (安全) |
| T0/T2/T3 完整 | 重启 backend | MD 文件不受影响 |

---

## 4. 恢复测试

### 4.1 T2 损坏 → 从 T0 重提取

```
1. 手动删除 learnings/*.md
2. 调用恢复脚本: python -c "from services.extract_agent import reextract_from_t0; ..."
3. 验证: T0 日志被重新处理 → T2 重建
```

### 4.2 T3 损坏 → 从 T2 重策展

```
1. 手动删除 memory/*.md
2. 手动触发心跳 (全量初始化)
3. 验证: T2 learnings 被重新策展 → T3 重建
```

### 4.3 soul 损坏 → 从 T3 重提炼

```
1. 手动删除 soul.md 的 Learned Behaviors
2. 手动触发梦境
3. 验证: T3 feedback 高频条目 → soul 重建
```

---

## 5. 性能基线

| 指标 | 目标 | 测量方法 |
|------|------|---------|
| 提取延迟 | <3s (不阻塞响应) | RESPONSE_COMPLETE → MEMORY_EXTRACTED 时间差 |
| 心跳 tick 延迟 | <30s (含 LLM 调用) | tick start → tick end |
| 梦境延迟 | <60s (含 LLM 调用) | dream start → dream end |
| 压缩延迟 | <5s (LLM summarize) | PRE_COMPACTION → POST_COMPACTION |
| T0 写入 | <100ms | 纯文件 I/O |
| 空转保护 | 0 LLM calls when T2 empty | 日志确认 skip |

---

## 6. 验收清单 (所有阶段综合)

| # | 验收项 | Phase |
|---|--------|-------|
| 1 | hooks.py 有 16 events | 01 |
| 2 | RESPONSE_COMPLETE 每轮触发 | 01 |
| 3 | PRE/POST_COMPACTION 在压缩时触发 | 01 |
| 4 | SESSION_IDLE/CLOSE 正确触发 | 01 |
| 5 | logs/ 目录存在, T0 日志正确写入 | 02 |
| 6 | 5 种行为格式 (chat/trigger/delegation/heartbeat/dream) | 02 |
| 7 | T0 >30d 自动清理 | 02 |
| 8 | 提取器 LLM agent 工作 | 03 |
| 9 | 提取器 pattern 降级 | 03 |
| 10 | Coalescing 无重复 | 03 |
| 11 | 时间微压缩 (60min + 保留 5) | 04 |
| 12 | 有效窗口减去 20K 预留 | 04 |
| 13 | PTL round-group 3 次 | 04 |
| 14 | 11-section 压缩摘要 | 04 |
| 15 | 系统提示词 § System + § Memory | 05 |
| 16 | HEARTBEAT.md OBSERVE/CURATE/ACT/LOG | 05 |
| 17 | DREAM.md 4 阶段 MD→MD | 05 |
| 18 | 心跳 KAIROS 持续 session | 06 |
| 19 | 心跳增量 T2 + 空转保护 | 06 |
| 20 | 心跳 T2→T3 策展有效 | 06 |
| 21 | 梦境 T3 精简 + soul 提炼 | 07 |
| 22 | 梦境不读 T2 (心跳独占) | 07 |
| 23 | 梦境后 FTS5 从 MD 重建 | 07 |
| 24 | 梦境后心跳 session 重置 | 07 |
| 25 | E2E: 消息→T0→T2→T3→soul 完整流转 | 08 |
| 26 | 降级: LLM 不可用全链路 | 08 |
| 27 | 降级: DB 不可用 MD 完整 | 08 |
| 28 | 恢复: T0→T2→T3 可重建 | 08 |

---

## 7. 影响文件

集成测试本身不改源码, 但需要:
- 测试脚本: `tests/test_memory_e2e.py` (新建)
- 恢复工具: `scripts/memory_recovery.py` (新建, 从 T0 重建 T2/T3)
- 性能基线: `scripts/memory_benchmark.py` (新建)
