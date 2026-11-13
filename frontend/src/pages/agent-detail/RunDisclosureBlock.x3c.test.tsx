// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages, type RunStepSnapshot } from './chatDisclosureReducer';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';

// X3c member sentences over the production event shapes: the expanded row
// names the file, the web host and the connected app, and a failed command
// row carries the not-done marker. History rows written before X3c keep the
// generic sentence.

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
  i18n: { language: locale.language },
}) }));
afterEach(() => { cleanup(); locale.language = 'zh'; window.localStorage.clear(); x3cSequence = 0; });

function expand(container: HTMLElement) {
  // Two passes: opening the header re-renders the step rows, so the item
  // toggles must be re-queried before they are clicked.
  for (const button of container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')) fireEvent.click(button);
  for (const button of container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')) fireEvent.click(button);
}

function renderSteps(steps: RunStepSnapshot[]) {
  const view = render(<RunDisclosureBlock timeline={{ id: 'run-x3c', status: 'done', steps }} />);
  expand(view.container);
  return view;
}

let x3cSequence = 0;

function native(kind: 'tool_call' | 'tool_result', lifecycle: string, invocation: string, payload: Record<string, unknown>) {
  const sequence = ++x3cSequence;
  return { schema: 'hive.session_event', schema_version: 2, event_id: `event-x3c-${sequence}`, sequence,
    tenant_id: 'tenant', scope: { level: 'round', session_id: 'session', thread_id: 'session', turn_id: 'turn', run_id: 'run', round_id: 'round' },
    item_id: `${kind}-${invocation}`, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`, actor: { type: kind === 'tool_call' ? 'assistant' : 'tool' },
    visibility: { audience: 'direct_user' }, invocation_id: invocation, payload,
    occurred_at: '2026-11-13T16:20:38Z', persisted_at: '2026-11-13T16:20:38Z' };
}

/** A settled production call: args_hash-only started, typed completed/result. */
function settledCall(toolName: string, resultPayload: Record<string, unknown>, callPayload: Record<string, unknown> = {}) {
  const invocation = `invocation-x3c-${toolName}-${x3cSequence}`;
  return [
    native('tool_call', 'started', invocation, { tool_name: toolName, args_hash: 'a'.repeat(64), ...callPayload }),
    native('tool_call', 'completed', invocation, { outcome: 'success', retryable: false, ...callPayload }),
    native('tool_result', 'completed', invocation, { tool_name: toolName, outcome: 'success', retryable: false,
      content: '', content_hash: 'h'.repeat(8), parts: [], ...resultPayload }),
  ];
}

function renderedRows(events: unknown[]) {
  const messages = hydrateSessionTranscriptEvents(events as never, 0).messages;
  const timeline = buildRunTimelineFromMessages(messages);
  const view = renderSteps(timeline.steps);
  return { container: view.container, timeline };
}

it('names the file of read, write and edit rows (base name only)', () => {
  const events = [
    ...settledCall('read_file', { member_summary: { kind: 'file_read', state: 'done', filename: 'sales-run19.csv' } }),
    ...settledCall('write_file', { member_summary: { kind: 'file_write', state: 'done', filename: 'weekly-report.md' } }),
    ...settledCall('edit_file', { member_summary: { kind: 'file_edit', state: 'done', filename: 'weekly-report.md' } }),
  ];
  const { container } = renderedRows(events);
  const text = container.textContent || '';
  expect(text).toContain('读取了文件「sales-run19.csv」');
  expect(text).toContain('已写入文件「weekly-report.md」');
  expect(text).toContain('编辑了文件「weekly-report.md」');
  expect(text).not.toContain('workspace/');
  expect(text).not.toContain('uploads/');
});

it('names the host of web rows and of a fetch that did not open', () => {
  const events = [
    ...settledCall('web_fetch', { member_summary: { kind: 'web_fetch', state: 'done', host: 'deepwiki.com' } }),
    ...settledCall('exa_fetch', { member_summary: { kind: 'web_fetch', state: 'not_done', host: 'example.org' } }),
  ];
  const { container } = renderedRows(events);
  const text = container.textContent || '';
  expect(text).toContain('打开了网页 deepwiki.com');
  expect(text).toContain('没有打开网页 example.org');
  expect(text).not.toContain('http');
});

it('names the trusted connection label of a company MCP row', () => {
  const { container } = renderedRows(
    settledCall('mcp_ask_wiki_question', { member_summary: { kind: 'mcp_app', state: 'done', app_label: 'DeepWiki' } }),
  );
  const text = container.textContent || '';
  expect(text).toContain('使用了已连接的应用「DeepWiki」');
  expect(text).not.toContain('ask_wiki_question');
});

it('marks a non-zero-exit command failed with the accessible marker; a zero exit stays done', () => {
  const events = [
    ...settledCall('execute_code', {}, { code_execution_result: { exit_code: 1, timed_out: false } }),
    ...settledCall('run_command', {}, { code_execution_result: { exit_code: 0, timed_out: false } }),
  ];
  const { container, timeline } = renderedRows(events);
  const rows = [...container.querySelectorAll('[data-testid="run-disclosure-step"]')];
  expect(rows.map((row) => row.getAttribute('data-status'))).toContain('failed');
  const failedRow = rows.find((row) => row.getAttribute('data-status') === 'failed');
  expect(failedRow?.querySelector('[role="img"][aria-label="未完成"]')).toBeTruthy();
  expect(failedRow?.textContent).toContain('运行命令');
  expect(failedRow?.textContent).toContain('失败');
  expect(rows.some((row) => row.getAttribute('data-status') === 'done' && row.textContent?.includes('运行命令'))).toBe(true);
  // The timeline agrees: a failed command step is a failed step, not done.
  expect(timeline.steps.some((step) => step.status === 'failed')).toBe(true);
});


it('never renders a secret-shaped filename (X3c r2)', () => {
  const events = [
    ...settledCall('read_file', { member_summary: { kind: 'file_read', state: 'done', filename: 'client_secret.pem' } }),
    ...settledCall('write_file', { member_summary: { kind: 'file_write', state: 'done', filename: 'notes.txt?token=secret' } }),
    ...settledCall('read_file', { member_summary: { kind: 'file_read', state: 'done', filename: 'sales-run19.csv' } }),
  ];
  const { container } = renderedRows(events);
  const text = container.textContent || '';
  expect(text).toContain('读取了文件「sales-run19.csv」');
  expect(text).not.toContain('client_secret');
  expect(text).not.toContain('notes.txt?token');
});

it('a null exit code or a string exit code is not done, and keeps the unconfirmed sentence (X3c r2)', () => {
  for (const executionResult of [{ exit_code: null, timed_out: false }, { exit_code: '1', timed_out: false }]) {
    x3cSequence = 0; // each iteration is its own fresh session (sequences restart at 1)
    const events = settledCall('execute_code', {}, { code_execution_result: executionResult });
    const messages = hydrateSessionTranscriptEvents(events as never, 0).messages;
    const timeline = buildRunTimelineFromMessages(messages);
    expect(timeline.steps.at(-1)?.status).toBe('failed');
    const view = renderSteps(timeline.steps);
    const row = view.container.querySelector('[data-testid="run-disclosure-step"]');
    expect(row?.getAttribute('data-status')).toBe('failed');
    expect(row?.querySelector('[role="img"][aria-label="未完成"]')).toBeTruthy();
    expect(row?.textContent).toContain('结果尚未确认');
    cleanup();
  }
});

it('fs_write follows its mode: a delete is never a write sentence (X3c r2)', () => {
  const events = [
    ...settledCall('fs_write', { member_summary: { kind: 'file_delete', state: 'done' } }),
    ...settledCall('fs_write', { member_summary: { kind: 'file_edit', state: 'done', filename: 'weekly-report.md' } }),
  ];
  const { container } = renderedRows(events);
  const text = container.textContent || '';
  expect(text).toContain('编辑了文件「weekly-report.md」');
  expect(text).not.toContain('已写入文件');
  expect(text).not.toContain('写入了文件');
});

it('keeps the generic sentence for history rows written before X3c', () => {
  const { container } = renderedRows([
    ...settledCall('read_file', { member_summary: { kind: 'file_read', state: 'done' } }),
    ...settledCall('web_fetch', { member_summary: { kind: 'web_fetch', state: 'done' } }),
  ]);
  const text = container.textContent || '';
  expect(text).toContain('读取了文件');
  expect(text).toContain('打开了网页');
  expect(text).not.toContain('「');
});
