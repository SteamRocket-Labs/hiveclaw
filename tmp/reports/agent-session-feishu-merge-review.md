# feature/agent-session-feishu 合并与对标审查报告

审查时间：2026-04-27  
审查对象：`origin/feature/agent-session-feishu` -> `origin/main`  
本地参考：`/Users/example-owner/Context Engineering/claude-code`、`/Users/example-owner/vc-saas/hermes-agent`

## 1. 结论

不建议把当前远程 `origin/feature/agent-session-feishu` 原样合入 `origin/main`。

理由不是 feature 自身质量差。相反，该分支在自己的分支头上验证结果很强：后端 1223 个 pytest 通过、ruff 通过、前端 67 个 vitest 通过、前端 build 通过。真正问题是它已经明显落后并分叉于当前 `origin/main`：`origin/main...origin/feature/agent-session-feishu` 显示 main 侧 63 个独有提交、feature 侧 52 个独有提交，`git merge-tree` 已确认存在真实合并冲突。

综合判断：该分支具备重要工程价值，但当前形态不是“直接合并态”，而是“需要做一次集成分支/rebase 修复后再合并”的状态。

## 2. 当前 Git 状态

- 当前 `hiveclaw-main` 工作区在 `main`，HEAD 为 `f8883df`，与 `origin/main` 对齐。
- 另一个 worktree `/Users/example-owner/vc-saas/hiveclaw` 在 `feature/agent-session-feishu`，HEAD 为 `fc356d6`，与 `origin/feature/agent-session-feishu` 对齐。
- feature worktree 有本地未提交运行时文件：
  - `.ultra/memory/chroma/.../data_level0.bin`
  - `.ultra/memory/chroma/chroma.sqlite3`
  - `.tmp/`
- 这些本地脏文件不属于远程分支评审，但说明合并前应先清理工作区。

关键命令结果：

```bash
git rev-list --left-right --count origin/main...origin/feature/agent-session-feishu
# 63 52
```

```bash
git diff --stat origin/main...origin/feature/agent-session-feishu
# 198 files changed, 18693 insertions(+), 3468 deletions(-)
```

## 3. 验证结果

在 feature worktree `/Users/example-owner/vc-saas/hiveclaw` 上已执行：

```bash
backend/.venv/bin/python -m pytest backend/tests
# 1223 passed, 4 warnings in 13.28s
```

```bash
backend/.venv/bin/python -m ruff check backend/app backend/tests
# All checks passed!
```

```bash
npm run test
# Test Files 17 passed (17), Tests 67 passed (67)
```

```bash
npm run build
# built successfully
```

```bash
git diff --check origin/main...origin/feature/agent-session-feishu
# 失败：4 个 docs/backend-trunk-governance/*.md 存在 EOF 空白行问题
```

`diff --check` 失败不是架构风险，但它是可自动化 gate 的失败，应先修。

## 4. 合并阻塞点

`git merge-tree --write-tree origin/main origin/feature/agent-session-feishu` 已报告冲突。高风险冲突包括：

- `.ultra/memory/chroma/.../data_level0.bin`
- `.ultra/memory/chroma/chroma.sqlite3`
- `.ultra/memory/daemon-errors.log`
- `.ultra/memory/sessions.jsonl`
- `backend/app/agents/orchestrator.py`
- `backend/app/memory/store.py`：main 已删除，feature 修改
- `backend/app/services/agent_tool_domains/messaging.py`
- `backend/app/services/memory_service.py`
- `backend/app/services/org_sync_service.py`
- `backend/app/services/t0_logger.py`
- `backend/app/services/task_executor.py`
- `backend/app/templates/HEARTBEAT.md`
- `backend/app/templates/system_skills/delegation-guide/SKILL.md`
- 多个对应测试文件

其中 `.ultra/memory/chroma` 二进制冲突是明显的仓库卫生问题。它们是运行时记忆/索引数据，不应继续作为源代码合并判断的一部分。

## 5. feature 的工程意义

该 feature 的主线是“后端单主干化”，价值明确：

- schedule/supervision 旧链路被收口到 trigger daemon。
- ChatSession 写入入口被集中到 `session_service`。
- gateway/Feishu/channel session identity 开始 canonical 化。
- `app.services.agent_tools` 厚 facade 被删除，工具 surface 与 execution entry 下沉到 `app.tools.surface`、`app.tools.execution_entry`。
- prompt/memory 入口去掉手工 `memory_context` 注入，收回到统一 runtime 构建链。
- 新增大量架构测试，防止旧入口回流。

这对长期对标 Claude Code 与 Hermes Agent 是必要地基：没有统一 session、tool runtime、prompt/memory 主干，后续做 compaction、skill 自进化、任务达成率评估都会继续被旧链路拖垮。

## 6. 对标结论

相对 Claude Code，本分支对齐了部分基础设施方向：工具注册/执行单入口、skill/system prompt 入口治理、session 与任务流收口。但仍未完全对齐：

- 缺少 Claude Code 式多层 context compaction：microcompact、snip compact、autocompact、reactive compact。
- 缺少成熟的并发工具执行调度模型：读安全工具并发、写工具串行、流式 tool result buffering。
- 权限模型还未达到多层策略：结构校验、工具权限、规则、hook、分类器、交互确认。
- 缺少任务达成率/失败模式的系统性基准闭环。

相对 Hermes Agent，当前 main 已经在 prompt/memory 方向走得更前：T0/T2、Hindsight backend、prompt cache、提示词与 skill 模板大幅升级。feature 的价值更多在 trunk cleanup，而不是超过 Hermes 的自进化闭环。仍缺口：

- 模型族特定 tool-use guidance 与 execution discipline。
- 大工具结果持久化到文件并给模型可读路径，而不是单纯截断/压缩。
- 背景 memory/skill review agent 自动沉淀。
- 插件生命周期 hook 与 request-scoped API hook。
- 自动行为基准驱动的 prompt/skill 自修复。

## 7. 推荐合并路径

建议不要从 `feature/agent-session-feishu` 直接点 merge。应新建集成分支：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
git switch main
git pull --ff-only
git switch -c codex/integrate-agent-session-feishu
git merge origin/feature/agent-session-feishu
```

冲突处理原则：

1. `.ultra/memory/*` 不进入最终合并结果；后续应把运行时 `.ultra/memory/chroma` 从版本控制中移除。
2. `backend/app/memory/store.py` 冲突优先保留 current main 的新 memory backend / md_store / Hindsight 结构，再把 feature 中仍有价值的 session/prompt 调用点迁移过来。
3. prompt、HEARTBEAT、DREAM、system skills 冲突优先保留 main 上较新的 best-practice 版本，再补回 feature 的 interaction_type/session contract 要求。
4. tool runtime 方向保留 feature 的 `app.tools.surface` 与 `app.tools.execution_entry` 收口，但必须确认不会破坏 main 新增的 audit/workspace/memory handler。
5. session/gateway/Feishu canonicalization 是 feature 的核心资产，应完整迁移并保留架构测试。

合并后必须重新执行：

```bash
cd /Users/example-owner/vc-saas/hiveclaw
backend/.venv/bin/python -m pytest backend/tests
backend/.venv/bin/python -m ruff check backend/app backend/tests
cd frontend
npm run test
npm run build
```

以及：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
git diff --check origin/main...HEAD
```

## 8. 95%+ 判断

95%+ 结论：当前远程 feature 不适合原样合并。

95%+ 结论：feature 的优化方向有意义，而且是后续对标 Claude Code/Hermes 的前置工程债清理。

95%+ 结论：真正要做的是集成，而不是否定该分支。它应被拆成“可移植资产”进入 main：session canonicalization、trigger 主干、tool runtime 单入口、prompt/memory 单入口、架构测试护栏。

95%+ 结论：当前 main 已在 prompt/memory/skill 自进化方向包含大量更新，直接合入 feature 会冲突甚至覆盖这些较新成果。必须以 main 为基底，把 feature 的 trunk cleanup 迁入，而不是让 feature 反向覆盖 main。
