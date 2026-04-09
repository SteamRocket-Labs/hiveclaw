# Hive 记忆系统重构 — 实施手册

> 总纲参考: `../memory-system-redesign.md` (v9, 设计文档)
> 本目录: 可执行的渐进式实施手册，每阶段独立验收

## 核心原则

**记忆是可选的。会话永远继续。每一层独立失败。**

## 目标架构

```
4 层金字塔: T0 → T2 → T3 → soul
3 个蒸馏器: 提取器 (T0→T2) → 心跳 (T2→T3) → 梦境 (T3→soul)
16 个 Hooks: 记忆系统的事件总线
MD = Source of Truth, DB = 辅助
```

## 阶段依赖图

```
01-hooks ──→ 02-t0 ──→ 03-extractor ──→ 05-prompts ──→ 06-heartbeat ──→ 07-dream ──→ 08-integration
    │                                       ↑
    └──────→ 04-compression (并行) ─────────┘
```

## 阶段总览

| # | 文件 | 阶段 | 依赖 | 核心交付 |
|---|------|------|------|---------|
| 01 | `01-hooks.md` | Hooks 系统 | 无 | hooks.py 16 events + 关键 emit 接入 |
| 02 | `02-t0-layer.md` | T0 原始日志层 | 01 | workspace logs/ + T0 logger + 5 种行为格式 |
| 03 | `03-extractor.md` | 提取器 (T0→T2) | 01, 02 | extract_agent.py + LLM 提取 + pattern 降级 |
| 04 | `04-compression.md` | 压缩体系对齐 | 01 | 5 差距修复 + 11-section 压缩提示词 |
| 05 | `05-prompts.md` | 提示词体系 | 03 | 系统提示词 section 化 + Memory section |
| 06 | `06-heartbeat.md` | 心跳重构 | 03, 05 | KAIROS 持续 session + HEARTBEAT.md 重写 |
| 07 | `07-dream.md` | 梦境重构 | 06 | DREAM.md + MD→MD + T3 精简 + soul |
| 08 | `08-integration.md` | 集成验证 | 全部 | E2E 测试 + 降级测试 + 恢复测试 |

## 并行可能性

- **Phase 3 (压缩) 和 Phase 2 (提取器)** 可并行，都只依赖 Phase 0 Hooks
- **Phase 5 (提示词)** 之后的心跳和梦境必须串行

## 最终验收: 一条消息的完整生命周期

```
用户发消息 → Agent 响应
  → RESPONSE_COMPLETE hook → 提取器 LLM agent → T2 learnings
  → 同时写 T0 logs/YYYY-MM-DD/chat-*.md
  → 45min 后心跳 tick (持续 session) → T2→T3 策展
  → 4h 后梦境 → T3 精简 + soul 提炼
  → 下一次对话 → frozen prompt 包含最新 T3 + soul
  → 压缩时 → PRE_COMPACTION → 提取 → 压缩 → 恢复注入
```

## 文件约定

每个阶段文件统一包含:
- **当前状态**: 代码位置 + 行号 + 实际内容 (基于源码, 不凭记忆)
- **Claude Code 对标**: 对应源码位置 + 机制说明
- **目标状态**: 具体改成什么
- **实现步骤**: 改哪些文件, 写什么
- **验收标准**: 怎么证明 done
- **影响文件**: 涉及的源文件列表
