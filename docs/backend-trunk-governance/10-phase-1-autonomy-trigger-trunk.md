# Phase 1: 自主触发主干收口

## 1. 本阶段目标

把“何时触发 agent 执行”收口为一条主线：

- 保留：`AgentTrigger + trigger_daemon`
- 清退：`AgentSchedule + api/schedules.py + services/scheduler.py + supervision_reminder.py`

这阶段只收口“触发语义”和“后台循环”，不一次性解决所有 session 持久化细节。

---

## 2. 当前真实问题

当前仓库同时存在：

- `AgentTrigger`
- `AgentSchedule`
- `trigger_daemon`
- `scheduler.py`
- `supervision_reminder.py`

这会带来：

1. 同一“定时执行”语义有两套模型
2. 同一“后台轮询”语义有两套或更多循环
3. 产品/API 难以说明“到底哪套才是真的”

---

## 3. 本阶段保留与删除

### 保留

- `backend/app/models/trigger.py`
- `backend/app/api/triggers.py`
- `backend/app/services/trigger_daemon.py`

### 过渡期兼容壳

- `api/schedules.py` 若短期保留，只能转译到 trigger
- 不允许继续写 `AgentSchedule`

### 最终删除

- `backend/app/models/schedule.py`
- `backend/app/services/scheduler.py`
- `backend/app/services/supervision_reminder.py`

---

## 4. 明确边界

### 本阶段负责

- 统一“何时触发”
- 统一“后台循环”
- 统一调度模型

### 本阶段不负责

- 会话持久化主干最终收口
- A2A/Delegation 主干最终收口
- Prompt/Memory 主干最终收口

这样做是为了避免范围漂移。

---

## 5. 执行步骤

### W1 建架构测试

新增测试：

- `backend/tests/architecture/test_trigger_trunk.py`

测试必须先覆盖：

1. `main.py` 只启动 `trigger_daemon`
2. `AgentSchedule` 不再是主流程依赖
3. `api/schedules.py` 如果存在，只能做兼容转发，不得写独立模型

### W2 盘点旧系统

执行：

```bash
rg -n "AgentSchedule|start_scheduler|scheduler.py|supervision_reminder|AgentTrigger|trigger_daemon" backend/app
```

产出：

- 所有 schedule 读写点
- 所有 scheduler 启动点
- 所有 supervision reminder 启动点

### W3 定义唯一触发模型

写进代码与测试：

- cron / once / interval / poll / on_message / webhook 全部归 `AgentTrigger`

### W4 迁移 API

目标：

- 所有新写入路径都写 `AgentTrigger`
- `api/schedules.py` 如暂时存在，内部改为 trigger compatibility adapter

### W5 删除旧后台循环

删除条件：

- 没有剩余 schedule 独立写路径
- 没有剩余 scheduler 启动点
- supervision reminder 已迁移或下线

### W6 局部回归

至少跑：

```bash
pytest tests/architecture/test_trigger_trunk.py
pytest tests/api/test_triggers.py
pytest tests/services/test_trigger_daemon.py
```

---

## 6. 风险与下游影响

### 会影响 T4 会话主干

原因：

- trigger 执行会落 session/message

控制：

- 本阶段只改触发模型和后台循环，不重做 session 落盘
- 但要把所有 session 写点列成下一阶段跟单

### 会影响 T3 Prompt/Memory

原因：

- trigger source / metadata 影响 prompt 与 memory 路径

控制：

- 对 trigger session metadata 补 contract 测试

---

## 7. 退出条件

本阶段完成的最低标准：

1. `main.py` 只有 `trigger_daemon` 负责自主触发主循环
2. `AgentSchedule` 不再是主流程模型
3. schedule API 不再写 schedule 独立数据
4. `supervision_reminder.py` 不再作为主循环存在
5. 局部回归通过

---

## 8. 本轮实际进度（2026-04-14）

### 已完成

1. 已新增自主触发主干架构测试，明确：
   - `api/schedules.py` 不得继续依赖 `AgentSchedule` 与 `scheduler._execute_schedule`
   - `main.py` 主循环仍只启动 `trigger_daemon`
2. 已建立 legacy schedule 数据迁移边界（当前收口到 DB migration 层）
3. `api/schedules.py` 已完成第一轮收口：
   - 创建/更新/删除 schedule 改写 `AgentTrigger`
   - 旧 `AgentSchedule` 记录在访问时自动迁入 trigger surface
   - manual run 改为设置 manual pending，由 `trigger_daemon` 统一执行
4. `trigger_daemon.py` 已支持：
   - 识别 `schedule_api` surface 的 manual pending
   - 触发后自动清理 pending 状态
   - 兼容写入 `schedule_run` activity
5. 与本主干相关的回归已通过
6. 本轮进一步完成：
   - 新增 `backend/app/services/schedule_surface.py`
   - `api/schedules.py` 的 active schedule surface 已迁到 `schedule_surface`
   - `api/schedules.py` 已不再 import / 调用 `schedule_compat`
   - `trigger_daemon.py` 已不再 import `schedule_compat`
   - `main.py` 已不再承担 legacy schedule migration
   - `backend/app/services/scheduler.py` 已物理删除
   - `backend/tests/services/test_scheduler.py` 已同步删除
   - `backend/app/services/supervision_reminder.py` 已物理删除
   - `backend/tests/services/test_supervision_reminder.py` 已同步删除
   - `backend/app/scripts/migrate_schedules_to_triggers.py` 已物理删除
   - `backend/app/models/schedule.py` 已物理删除
   - `backend/app/services/schedule_compat.py` 已删除
   - `backend/app/services/legacy_schedule_migration.py` 已删除
   - 新增 `backend/app/db_legacy_schedule_migration.py`
   - legacy `agent_schedules` 数据迁移已下沉到：
     - `db_bootstrap.py`：处理“有旧表但无 alembic_version”的历史库
     - `backend/alembic/versions/drop_legacy_agent_schedules_0414.py`：处理正常 Alembic 升级链
   - 新增架构护栏，固定：
     - `backend/app` 运行时代码内不再 import legacy schedule migration 服务
     - 仓库内不再允许出现 `from app.models.schedule import AgentSchedule`
   - repo 级盘点已确认：
     - `backend/entrypoint.sh` 已不再执行外部 schedule 迁移脚本
     - `backend/app/main.py / backend/entrypoint.sh / backend/seed.py` 已不再把 `app.models.schedule` 注入 bootstrap `create_all`
     - `backend/alembic/env.py` 已不再 import `AgentSchedule`
     - Alembic 当前单头为 `drop_legacy_agent_schedules_0414`

### 本轮明确未完成

1. 代码层已无 Phase 1 legacy 尾巴
2. 运行层仍需对历史环境执行 Alembic 头 `drop_legacy_agent_schedules_0414`

### 判定

因此，Phase 1 当前状态应视为：

- `主干入口已收口`
- `代码层 legacy 已闭环`
- `运行层待完成数据库升级应用`

也就是说，这一阶段在代码仓内已经完成，可以进入下一主干；剩余动作是部署时应用数据库迁移。
