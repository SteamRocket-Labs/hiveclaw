// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import { MemberMessageBody } from '../session-workbench/PastedCredentialNotice';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import type { AgentChatMessage } from './chatRuntime';
import RunDisclosureBlock from './RunDisclosureBlock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
}) }));
afterEach(cleanup);

function expand(container: HTMLElement) {
  for (const selector of ['.run-disclosure-header', '.run-tool-group-toggle', '.run-step-toggle', '.run-tool-history-toggle']) {
    for (const toggle of container.querySelectorAll<HTMLElement>(selector)) fireEvent.click(toggle);
  }
}

// Run 8 output snippets plus a None stdout counterexample from the brief.
it.each([
  ['execute_code', { code: 'print(summary)' }, "📤 Output:\n{'file': 'sales-run8.csv', 'rows': 20, 'cols': ['日期', '地区', '产品', '数量', '单价', '销售额'], 'dates': ('2026-10-01', '2026-10-20'), 'region_product_1to1': True, 'qty_diffs': {'华南': [4.0], '华北': [4.0], '华东': [4.0], '西部': [4.0]}, 'qty_is_arith': True, 'price_constant_per_product': True, 'prices': {'基础版': ['100'], '进阶版': ['150'], '专业版': ['200'], '企业版': ['250']}}", '运行命令', '结果尚未确认。'],
  ['execute_code', { code: "print({'error': None})" }, "📤 Output:\n{'error': None}", '运行命令', '结果尚未确认。'],
  ['run_command', { command: 'which gh git curl 2>/dev/null; echo "---gh auth---"; gh auth status 2>&1 | head -20; echo "---gh api---"; gh api notifications --jq \'.[].subject.title\' 2>&1 | head -20' }, '📤 Output:\n/opt/git/bin/git\n/usr/bin/curl\n---gh auth---\nbash: line 1: gh: command not found\n---gh api---\nbash: line 1: gh: command not found', '运行命令', '失败'],
] as const)('projects recorded %s output into a member summary without mutating the model payload', (toolName, toolArgs, toolResult, label, status) => {
  const message: AgentChatMessage = { id: toolName, role: 'tool_call', content: '', toolName, toolArgs, toolResult, toolStatus: 'done' };
  const before = JSON.stringify(message);
  const timeline = buildRunTimelineFromMessages([message]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expand(container);
  expect(container.textContent).toContain(label);
  expect(container.textContent).toContain(status);
  expect(container.textContent).not.toContain('已返回结果');
  expect(container.textContent).not.toMatch(/Written to|Saved memory|True|None|workspace|\/opt\/git|command not found|pwd;|3123/);
  expect(JSON.stringify(message)).toBe(before);
  expect((timeline.steps[0].details as { result: string }).result).toBe(toolResult);
});

it.each([
  ['write_file', { path: 'workspace/sales-report-sales-run8.md' }, '✅ Written to workspace/sales-report-sales-run8.md (3123 chars)'],
  ['save_memory', { content: '用户的 E2E 测试代号是 白鹭-8。' }, 'Saved memory [user] (user): 用户的 E2E 测试代号是 白鹭-8。'],
] as const)('shows the recorded non-command %s result as in production without mutating the model payload', (toolName, toolArgs, toolResult) => {
  const message: AgentChatMessage = { id: toolName, role: 'tool_call', content: '', toolName, toolArgs, toolResult, toolStatus: 'done' };
  const before = JSON.stringify(message);
  const timeline = buildRunTimelineFromMessages([message]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expand(container);
  // owner ruling 11-10: non-command results show as on 2182d216
  expect(container.querySelector('p')?.textContent).toBe(toolResult);
  expect(JSON.stringify(message)).toBe(before);
  expect((timeline.steps[0].details as { result: string }).result).toBe(toolResult);
});

it.each([
  ['write_file', { path: 'workspace/run8-count.md' }, '已写入文件「run8-count.md」。', '✅ Written to workspace/run8-count.md (106 chars)'],
  ['save_memory', { content: '用户的 E2E 测试代号是 白鹭-8。' }, '已记住：用户的 E2E 测试代号是 白鹭-8。', 'Saved memory [user] (user): 用户的 E2E 测试代号是 白鹭-8。'],
] as const)('shows the member receipt of %s and preserves its model envelope', (toolName, toolArgs, text, model_visible_text) => {
  const envelope = { text, model_visible_text };
  const message: AgentChatMessage = { role: 'tool_call', content: '', toolName, toolArgs,
    toolResult: envelope.text, toolRawResult: JSON.stringify(envelope), toolStatus: 'done' };
  const before = JSON.stringify(message);
  const timeline = buildRunTimelineFromMessages([message]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expand(container);
  expect(container.querySelector('p')?.textContent).toBe(text);
  expect(container.textContent).not.toMatch(/Written to|Saved memory|workspace|106|model_visible_text/);
  expect(JSON.stringify(message)).toBe(before);
  expect((timeline.steps[0].details as { rawResult: string }).rawResult).toBe(JSON.stringify(envelope));
});

it.each([
  ['run_command', { exit_code: 0, output: 'PRIVATE_STDOUT', stderr: 'PRIVATE_STDERR' }, '成功'],
  ['execute_code', { exit_code: 7, output: 'PRIVATE_STDOUT' }, '失败'],
  ['run_skill_tool', { exit_code: 0, error: 'PRIVATE_ERROR' }, '失败'],
  ['run_command', { ok: false, error: 'PRIVATE_ERROR' }, '失败'],
  ['execute_code', { timed_out: true }, '结果尚未确认。'],
  ['run_command', { exit_code: '0', output: 'PRIVATE_STDOUT' }, '结果尚未确认。'],
  ['run_command', '{broken PRIVATE_STDOUT', '结果尚未确认。'],
  ['run_command', '📤 Output:\nExit code: 0', '结果尚未确认。'],
  ['run_command', undefined, '结果尚未确认。'],
  ['run_skill_tool', '报告内容已读取。', '结果尚未确认。'],
  ['execute_code', '📤 Output:\nPRIVATE_STDOUT\n\nExit code: 1', '失败'],
] as const)('uses execution facts for %s with result %j', (toolName, rawResult, status) => {
  const message: AgentChatMessage = { role: 'tool_call', content: '', toolName,
    toolArgs: { command: 'PRIVATE_COMMAND' }, toolStatus: 'done',
    toolResult: typeof rawResult === 'object' ? JSON.stringify(rawResult) : rawResult,
    toolRawResult: typeof rawResult === 'object' ? JSON.stringify(rawResult) : rawResult };
  const before = JSON.stringify(message);
  const timeline = buildRunTimelineFromMessages([message]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expand(container);
  expect(container.querySelector('p')?.textContent).toContain(status);
  expect(container.textContent).not.toMatch(/PRIVATE_|报告内容已读取|已返回结果|exit_code|Exit code|timed_out/);
  expect(JSON.stringify(message)).toBe(before);
});

it.each([
  '/vercel/sandbox/workspace/report.md', '已读取：/private/tmp/report.md', 'C:\\workspace\\report.md',
  '报告位于/var/tmp/report.md',
  "{'rows': 20, 'ok': True}", '{"rows": 20, "ok": True}', 'Traceback (most recent call last):\nValueError: PRIVATE_ERROR',
  'send_message_to_agent failed', 'call_private123',
])('shows non-JSON non-command result text as in production: %s', toolResult => {
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([
    { role: 'tool_call', content: '', toolName: 'send_message_to_agent', toolResult, toolStatus: 'done' },
  ])} />);
  expand(container);
  // owner ruling 11-10: non-command results show as on 2182d216
  expect(container.querySelector('p')?.textContent).toBe(toolResult.replace(/\r?\n/g, ' '));
});

it.each([
  'fatal: not a git repository (or any of the parent directories): .git',
  "head: cannot open 'workspace/uploads/*.csv' for reading: No such file or directory",
  'gh: command not found',
  'file:///private/tmp/secret',
  '见 </var/tmp/secret>',
  '目录／private/tmp/报告.md',
  'prefix {"rows": 20, "ok": True}',
  '(True, None)',
  '请对比输入/输出。',
  '和/或',
  '对方数字员工暂不可用',
  '报告内容已读取。',
] as const)('preserves the recorded non-command result sentence: %s', toolResult => {
  const timeline = buildRunTimelineFromMessages([
    { role: 'tool_call', content: '', toolName: 'send_message_to_agent', toolResult, toolStatus: 'done' },
  ]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expand(container);
  // owner ruling 11-10: non-command results show as on 2182d216
  expect(container.querySelector('p')?.textContent).toBe(toolResult);
  expect((timeline.steps[0].details as { result: string }).result).toBe(toolResult);
});

it('shows dates, ratios, A/B groups and a URL verbatim in a non-command result', () => {
  const toolResult = '截至 2026/11/10 完成 3/4，A/B 两组：见 https://example.com';
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([
    { role: 'tool_call', content: '', toolName: 'read_file', toolResult, toolStatus: 'done' },
  ])} />);
  expand(container);
  expect(container.querySelector('p')?.textContent).toBe(toolResult);
});

it('names the recorded generic step and readiness event with everyday words', () => {
  const timeline = buildRunTimelineFromMessages([
    { id: 'unknown', role: 'tool_call', toolName: 'search_personal_kb', content: '', toolResult: '{"ok":true}', toolStatus: 'done' },
    { id: 'ready', role: 'event', eventType: 'tool_group_activation', content: 'Activated MCP runtime tool group.' },
  ]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expand(container);
  expect(container.textContent).toContain('查找个人资料');
  expect(container.textContent).toContain('准备所需功能');
  expect(container.textContent).not.toMatch(/已调用工具|工具已就绪|MCP/);
  expect(container.querySelector('.run-tool-history-title')?.textContent).not.toBe('工具');
});

it.each(['assistant_commentary', 'report_progress'])('renders bold in the %s progress path without changing the prose', channel => {
  const content = '**核心结论：**2026-10-01';
  const message: AgentChatMessage = channel === 'report_progress'
    ? { role: 'tool_call', content: '', toolName: channel, toolArgs: { message: content }, toolStatus: 'done' }
    : { role: 'assistant', eventType: channel, content };
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([message])} />);
  expand(container);
  expect(container.querySelector('strong')?.textContent).toBe('核心结论：');
  expect(container.textContent).not.toContain('**');
  expect(channel === 'report_progress' ? message.toolArgs?.message : message.content).toBe(content);
});

it.each(['reply', 'delegation brief'])('renders the recorded %s markdown in its shared renderer', kind => {
  const content = '**核心结论：**2026-10-01\n\n**已知素材（我方工作区已核验）**：报告.md';
  const { container } = render(kind === 'reply' ? <MarkdownRenderer content={content} />
    : <MemberMessageBody message={{ role: 'user', content }} />);
  expect(container.querySelectorAll('strong')).toHaveLength(2);
  expect(container.textContent).not.toContain('**');
});

it('keeps escaped markers and code literal while parsing Chinese punctuation', () => {
  const { container } = render(<MarkdownRenderer content={'\\*\\*核心结论：\\*\\*2026\n\n`**核心结论：**2026`\n\n```text\n**核心结论：**2026\n```'} />);
  expect(container.querySelector('strong')).toBeNull();
  expect(container.querySelectorAll('code')).toHaveLength(2);
  expect(container.textContent).toContain('**核心结论：**2026');
});

it('uses chat history in the recovery banner', () => {
  expect(zh.agent.chat.sessionBackfillIncomplete).toBe('最新消息已显示，较早的聊天记录仍在恢复。');
});

it('keeps model-authored words intact even when they contain technical vocabulary', () => {
  const content = "I'll check Personal KB access. A2A execute_code run_command pick() 落盘 退出码";
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([
    { role: 'assistant', eventType: 'assistant_commentary', content },
  ])} />);
  expand(container);
  expect(container.querySelector('.run-commentary-content')?.textContent).toBe(content);
});

it('preserves sibling lines when summarizing a mixed non-command memory result', () => {
  const toolResult = 'INTERNAL_HEADER /vercel/sandbox/workspace\n{"schema":"hive.explicit_memory_availability.v1"}\nPRIVATE_RAW_LINE';
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([
    { role: 'tool_call', content: '', toolName: 'search_memory', toolResult, toolStatus: 'done' },
  ])} />);
  expand(container);
  // owner ruling 11-10: non-command results show as on 2182d216
  expect(container.querySelector('p')?.textContent).toBe('INTERNAL_HEADER /vercel/sandbox/workspace\n目前只读到了部分记忆，暂不可用的部分不能用来下结论，可以稍后重试。\nPRIVATE_RAW_LINE');
  expect(container.textContent).not.toContain('schema');
});

it.each(['search_memory', 'load_memory'])('preserves the memory body beside the availability notice from %s', toolName => {
  const body = '## 你要求记住的内容\n- 偏好深色界面\n\n## 可用情况\n';
  const message = '目前只读到了部分记忆，暂不可用的部分不能用来下结论，可以稍后重试。';
  const toolResult = body + JSON.stringify({ schema: 'hive.explicit_memory_availability.v1', status: 'partial',
    issues: [{ code: 'explicit_memory_source_unverified' }], message });
  const timeline = buildRunTimelineFromMessages([
    { role: 'tool_call', content: '', toolName, toolResult, toolStatus: 'done' },
  ]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expand(container);
  expect(container.querySelector('p')?.textContent).toBe(body + message);
  expect(container.textContent).not.toMatch(/schema|explicit_memory_source_unverified/);
  expect((timeline.steps[0].details as { result: string }).result).toBe(toolResult);
});

it('preserves recalled conversation lines beside a recall error notice', () => {
  const toolResult = '已找到的对话内容。\n## 相关对话\n{"schema":"hive.session_recall_error.v1"}';
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([
    { role: 'tool_call', content: '', toolName: 'search_memory', toolResult, toolStatus: 'done' },
  ])} />);
  expand(container);
  expect(container.querySelector('p')?.textContent).toBe('已找到的对话内容。\n## 相关对话\n暂时无法读取这部分历史对话，请稍后重试。');
  expect(container.textContent).not.toContain('schema');
});

it.each([
  ['command', undefined, 0, '成功'],
  ['tool', 'run_command', 0, '成功'],
  ['tool', 'execute_code', 0, '成功'],
  ['tool', 'run_skill_tool', 0, '成功'],
  ['tool', 'run_command', 7, '失败'],
  ['tool', 'execute_code', undefined, '结果尚未确认。'],
] as const)('uses only command execution facts for %s / %s beside an envelope (exit %s)', (kind, toolName, exit_code, status) => {
  const result = 'PRIVATE_STDOUT\n{"schema":"hive.explicit_memory_availability.v1"}\nPRIVATE_STDERR';
  const details = { toolName, result, exit_code };
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'command-envelope', status: 'done', steps: [{
    id: 'step', kind, title: 'Run command', status: 'done', visibility: 'visible', details,
  }] }} />);
  expand(container);
  expect(container.querySelector('p')?.textContent).toContain(status);
  expect(container.textContent).not.toMatch(/PRIVATE_|schema|目前只读到了部分记忆|已返回结果/);
  expect(details.result).toBe(result);
});

it.each(['search_memory', 'run_command', 'execute_code', 'run_skill_tool'])(
  'keeps whole-result memory JSON summaries for %s', toolName => {
    const toolResult = '{"schema":"hive.explicit_memory_availability.v1"}';
    const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([
      { role: 'tool_call', content: '', toolName, toolResult, toolStatus: 'done' },
    ])} />);
    expand(container);
    expect(container.querySelector('p')?.textContent).toBe('目前只读到了部分记忆，暂不可用的部分不能用来下结论，可以稍后重试。');
    expect(container.textContent).not.toContain('schema');
  },
);
