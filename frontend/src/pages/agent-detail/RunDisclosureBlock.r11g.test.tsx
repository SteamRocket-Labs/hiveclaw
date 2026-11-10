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
  ['write_file', { path: 'workspace/sales-report-sales-run8.md' }, '✅ Written to workspace/sales-report-sales-run8.md (3123 chars)', 'sales-report-sales-run8.md'],
  ['execute_code', { code: 'print(summary)' }, "📤 Output:\n{'file': 'sales-run8.csv', 'rows': 20, 'cols': ['日期', '地区', '产品', '数量', '单价', '销售额'], 'dates': ('2026-10-01', '2026-10-20'), 'region_product_1to1': True, 'qty_diffs': {'华南': [4.0], '华北': [4.0], '华东': [4.0], '西部': [4.0]}, 'qty_is_arith': True, 'price_constant_per_product': True, 'prices': {'基础版': ['100'], '进阶版': ['150'], '专业版': ['200'], '企业版': ['250']}}", '运行命令'],
  ['execute_code', { code: "print({'error': None})" }, "📤 Output:\n{'error': None}", '运行命令'],
  ['run_command', { command: 'pwd; which gh git' }, '📤 Output:\n/vercel/sandbox/workspace\n/opt/git/bin/git\nbash: line 1: gh: command not found', '运行命令'],
  ['save_memory', { content: '用户的 E2E 测试代号是 白鹭-8。' }, 'Saved memory [user] (user): 用户的 E2E 测试代号是 白鹭-8。', '保存记忆'],
] as const)('projects recorded %s output into a member summary without mutating the model payload', (toolName, toolArgs, toolResult, label) => {
  const message: AgentChatMessage = { id: toolName, role: 'tool_call', content: '', toolName, toolArgs, toolResult, toolStatus: 'done' };
  const before = JSON.stringify(message);
  const timeline = buildRunTimelineFromMessages([message]);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expand(container);
  expect(container.textContent).toContain(label);
  expect(container.textContent).toContain('已返回结果');
  expect(container.textContent).not.toMatch(/Written to|Saved memory|True|None|workspace|\/opt\/git|command not found|pwd;|3123/);
  expect(JSON.stringify(message)).toBe(before);
  expect((timeline.steps[0].details as { result: string }).result).toBe(toolResult);
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

it('summarizes a mixed memory envelope without passing through adjacent raw output', () => {
  const toolResult = 'INTERNAL_HEADER /vercel/sandbox/workspace\n{"schema":"hive.explicit_memory_availability.v1"}\nPRIVATE_RAW_LINE';
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages([
    { role: 'tool_call', content: '', toolName: 'search_memory', toolResult, toolStatus: 'done' },
  ])} />);
  expand(container);
  expect(container.textContent).toContain('目前只读到了部分记忆');
  expect(container.textContent).not.toMatch(/INTERNAL_HEADER|workspace|PRIVATE_RAW_LINE|schema/);
});
