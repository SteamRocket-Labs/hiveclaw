// @vitest-environment jsdom
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import { ArtifactCards } from './ArtifactSurface';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages, type RunTimelineSnapshot } from './chatDisclosureReducer';
import { formatComposerAttachments, parseUserAttachment } from './chatAttachments';
import { buildThreadTimeline } from '../session-workbench/timelineModel';
import { mergePendingUserMessages } from './chatRuntime';

const translate = (key: string, fallback?: string, values?: Record<string, unknown>) =>
  translateFromCatalog(zh, key, fallback, values);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
beforeEach(() => { vi.spyOn(i18n, 't').mockImplementation(translate as never); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); sessionStorage.clear(); });
const css = readFileSync('src/index.css', 'utf8');

it('R16a.3 anchors an in-flight send after its prior history when a refetch restores an older prefix', () => {
  const terminal = { role: 'event' as const, id: 'old-terminal', content: '', eventType: 'run_cancelled',
    sessionItem: { id: 'old-run', kind: 'run', lifecycle: 'cancelled', scope: { level: 'run', run_id: 'old-run', session_id: 'work' },
      payload: {}, actor: { type: 'runtime' }, last_sequence: 5, occurredAt: new Date().toISOString() } as never };
  const messages = mergePendingUserMessages([
    { role: 'user', id: 'older', content: 'Older request' },
    { role: 'assistant', id: 'older-answer', content: 'Earlier answer' },
    { role: 'user', id: 'previous', content: 'Previous request' }, terminal,
  ], [{ anchorMessageCount: 2, anchorMessageId: 'old-terminal', message: {
    role: 'user', id: 'next', content: 'Fresh request', sendState: 'sending', timestamp: new Date().toISOString(),
  } } as never]).messages;
  expect(messages.at(-1)?.id).toBe('next');
  const model = buildThreadTimeline({ activeSession: { id: 'work' }, isWaiting: false, isStreaming: false,
    activeRunStatus: null, runtimePhase: 'cancelled', messages });
  expect(model.header.status).toBe('running');
  expect(model.cells.at(-1)).toMatchObject({ kind: 'active_run', timeline: { status: 'running' } });
});

it('R16a.1 gives uploaded cards their own phone layout with a two-line name and retained extension', () => {
  const { container } = render(<ArtifactCards agentId="bot" artifacts={[
    { name: 'sales-run12.csv', path: 'workspace/uploads/sales-run12.csv', source: 'human_input', mimeType: 'text/csv', size: 849 },
  ]} />);
  expect(container.querySelector('.chat-artifact-row.is-upload')).toBeTruthy();
  expect(container.querySelector('.chat-artifact-meta')?.textContent).toBe('text/csv · 849 B');
  expect(css).toMatch(/@media\s*\(max-width:\s*600px\)[\s\S]*\.chat-artifact-row\.is-upload\s*\{[^}]*flex-wrap:\s*wrap/);
  expect(css).toMatch(/\.is-upload\s+\.chat-artifact-stem\s*\{[^}]*-webkit-line-clamp:\s*2/);
  expect(container.querySelector('.chat-artifact-extension')?.textContent).toBe('.csv');
});

it('R16a.1 retains available upload size in the composer card', () => {
  const formatted = formatComposerAttachments('Analyze', [{ name: 'sales.csv', text: '', path: 'workspace/uploads/sales.csv', size: 849 } as never], false);
  expect(formatted.inputAttachments[0]).toMatchObject({ size: 849 });
});

it('R16a.2 makes all file actions visible on no-hover devices without changing hover defaults', () => {
  expect(css).toMatch(/@media\s*\(hover:\s*none\)\s*\{\s*\.chat-artifact-actions\s*\{[^}]*opacity:\s*1/);
  expect(css).toMatch(/\.chat-artifact-actions\s*\{[^}]*display:\s*inline-flex[^}]*flex-shrink:\s*0[^}]*opacity:\s*0/);
});

it('R16a.5 uses the Chinese duration unit from zero through the first tick', () => {
  vi.useFakeTimers();
  const timeline: RunTimelineSnapshot = { id: 'zero', status: 'running', startedAt: new Date().toISOString(),
    steps: [{ id: 'think', kind: 'reasoning', title: 'Thinking', status: 'running', visibility: 'visible' }] };
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expect(container.querySelector('.run-disclosure-duration')?.textContent).toBe('0 秒');
  act(() => vi.advanceTimersByTime(1000));
  expect(container.querySelector('.run-disclosure-duration')?.textContent).toBe('1 秒');
});

it.each([
  ['**新增的"七、下一步建议"**采用 P0/P1/P2 …', '新增的"七、下一步建议"'],
  ['**“建议”**后', '“建议”'], ['**「建议」**后', '「建议」'], ['**建议。**后', '建议。'],
])('R16a.6 renders Chinese adjacent strong through the real renderer: %s', (content, strong) => {
  const { container } = render(<MarkdownRenderer content={content} />);
  expect(container.querySelector('strong')?.textContent).toBe(strong);
  expect(container.textContent).toBe(content.replaceAll('**', ''));
});

it('R16a.6 preserves native English, escapes and code', () => {
  const { container } = render(<MarkdownRenderer content={'**ordinary English** and *emphasis*; **"quote"**after; `**“code”**后` \\*\\*“literal”\\*\\*后'} />);
  expect([...container.querySelectorAll('strong')].map(node => node.textContent)).toEqual(['ordinary English']);
  expect(container.querySelector('em')?.textContent).toBe('emphasis');
  expect(container.querySelector('code')?.textContent).toBe('**“code”**后');
  expect(container.textContent).toContain('**“literal”**后');
});

it('R16a.7 counts only reads shown in steps and retains a failed read in the total', () => {
  const tools = ['list_files', 'fs_list', 'write_file', 'fs_write', 'edit_file', 'delete_file', 'read_file', 'fs_read', 'read_document'];
  const messages = tools.map((toolName, i) => ({ id: String(i), role: 'tool_call' as const, content: '', toolName, toolStatus: 'done' as const }));
  expect(buildRunTimelineFromMessages(messages).summary?.files).toBe(3);
  messages[6] = { ...messages[6], toolStatus: 'failed' as never };
  const timeline = buildRunTimelineFromMessages(messages);
  expect(timeline.summary?.files).toBe(3);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  expect(container.querySelector('.run-disclosure-summary')?.textContent).toContain('读取 3 个文件');
  expect(timeline.steps.filter(step => ['read_file', 'fs_read', 'read_document'].includes((step.details as {toolName: string}).toolName))).toHaveLength(3);
});

it('R16a.8 does not offer an empty detail toggle', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'empty', status: 'running', steps: [
    { id: 'empty-step', kind: 'event', title: 'Runtime event', status: 'done', visibility: 'visible', details: {} },
  ] }} />);
  const toggle = container.querySelector('.run-step-toggle, .run-tool-history-toggle')!;
  expect(toggle.hasAttribute('disabled')).toBe(true);
  expect(toggle.hasAttribute('aria-expanded')).toBe(false);
  expect(toggle.classList.contains('has-details')).toBe(false);
});

it('R16a.8 names the tool supplied by event data', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'named', status: 'running', steps: [
    { id: 'named-step', kind: 'event', title: 'Runtime event', status: 'done', visibility: 'visible', details: { name: 'read_file' } },
  ] }} />);
  expect(container.querySelector('.run-step-title, .run-tool-history-title')?.textContent).toBe('读取文件');
  fireEvent.click(container.querySelector('.run-step-toggle, .run-tool-history-toggle')!);
  expect(container.querySelector('p')?.textContent).toContain('读取文件');
});

it('R16a.9 uses member wording for loading and delivered versions', () => {
  expect(zh.agent.chat.transport.initializing).toBe('正在加载对话…');
  expect(zh.agent.chat.artifacts.snapshotPreview).toBe('正在预览交付的版本');
  expect(zh.agent.chat.artifacts.backToDelivery).toBe('返回交付的版本');
  expect(zh.agent.chat.artifacts.continueSaved).not.toContain('快照');
});

it('retains stored attachment metadata without inventing missing size', () => {
  const parsed = parseUserAttachment({ role: 'user', content: 'Analyze', sessionItem: { kind: 'human_input', actor: { type: 'user' }, payload: { content_parts: [
    { type: 'attachment_ref', attachment: { name: 'sales.csv', path: 'workspace/uploads/sales.csv', content_type: 'text/csv', size: 849 } },
  ] } } as never });
  expect(parsed.inputAttachments?.[0]).toMatchObject({ size: 849, mimeType: 'text/csv' });
});
