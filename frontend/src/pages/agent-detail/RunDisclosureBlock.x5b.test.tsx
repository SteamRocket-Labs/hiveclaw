// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { TFunction } from 'i18next';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import { toolStepLabel } from './toolStepLabel';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
}) }));
afterEach(() => { cleanup(); locale.language = 'zh'; });

/** Run20 tool-events.json shape: args_hash-only call, failed/completed call,
 * args-absent result with redacted content and the backend member summary.
 * Identifiers are replaced; no private source, page body or URL is copied. */
function settled(toolName: string, summary?: Record<string, unknown>, outcome = 'success', metadata = {}) {
  const parts = [
    { item_kind: 'tool_call', lifecycle: 'started', payload: { tool_name: toolName, args_hash: 'a'.repeat(64), effect_state: 'prepared_not_started' } },
    { item_kind: 'tool_call', lifecycle: outcome === 'success' ? 'completed' : 'failed', payload: { outcome, retryable: false } },
    { item_kind: 'tool_result', lifecycle: 'completed', payload: { tool_name: toolName, outcome, retryable: false,
      content: '', result: '', content_hash: 'h'.repeat(64), parts: [], metadata, ...(summary ? { member_summary: summary } : {}) } },
  ];
  return parts.map((part, index) => ({ ...part, schema: 'hive.session_event', schema_version: 2,
    event_id: `event-${index}`, sequence: index + 1, tenant_id: 'tenant', item_id: `${part.item_kind}-invocation`,
    invocation_id: 'invocation', provider_tool_use_id: 'provider-call',
    kind: `${part.item_kind}.${part.lifecycle}`, payload_schema: `hive.session.payload.${part.item_kind}.${part.lifecycle}.v2`,
    scope: { level: 'round', session_id: 'session', thread_id: 'session', turn_id: 'turn', run_id: 'run', round_id: 'round' },
    actor: { type: part.item_kind === 'tool_call' ? 'assistant' : 'tool' }, visibility: { audience: 'direct_user' },
    occurred_at: '2026-11-14T04:06:52Z', persisted_at: '2026-11-14T04:06:52Z',
  }));
}

function renderEvents(events: unknown[]) {
  const messages = hydrateSessionTranscriptEvents(events as never, 0).messages;
  const timeline = buildRunTimelineFromMessages(messages);
  const view = render(<RunDisclosureBlock timeline={timeline} />);
  const header = view.container.querySelector<HTMLButtonElement>('.run-disclosure-header');
  if (header?.getAttribute('aria-expanded') === 'false') fireEvent.click(header);
  for (const group of view.container.querySelectorAll<HTMLDetailsElement>('details.run-tool-group')) group.open = true;
  return view;
}

function expandRows(container: HTMLElement) {
  for (const button of container.querySelectorAll<HTMLButtonElement>('.run-step-toggle, .run-tool-history-toggle')) {
    if (button.getAttribute('aria-expanded') === 'false') fireEvent.click(button);
  }
}

it.each(['zh', 'en'] as const)('renders a typed memory timeout as a timeout (%s)', language => {
  locale.language = language;
  for (const [toolName, kind] of [['search_memory', 'memory_search'], ['load_memory', 'memory_read']]) {
    // Step4 rows 3/4 used search_memory. load_memory shares the memory kind
    // contract. The new backend projection adds the existing error_class.
    const { container } = renderEvents(settled(toolName, { kind, state: 'not_done', error_class: 'timeout' }, 'failed', {
      native_tool_timeout_receipt: { schema: 'hive.native_tool_timeout_receipt.v1', status: 'verified', effect_outcome: 'unknown' },
    }));
    expandRows(container);
    const catalog = language === 'zh' ? zh : en;
    // X5b: retrievals can live in tool history; assert the actual row's
    // failed marker and sentence without requiring a particular container.
    const row = container.querySelector('[data-testid="run-disclosure-step"], .run-tool-history-item');
    expect(row).not.toBeNull();
    expect(row?.getAttribute('data-status')).toBe('failed');
    expect(row?.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe(catalog.agent.chat.disclosure.failedMarker);
    expect(row?.querySelector('p')?.textContent).toBe(catalog.agent.chat.disclosure.failure.timeout);
    expect(row?.textContent).not.toContain(catalog.agent.chat.toolResults.memberSummary.memorySearchFailed);
    expect(row?.textContent).not.toMatch(/tool_error|error_class|retryable|native_tool_timeout/);
    cleanup();
  }
});

it.each([
  ['not_done', 'auth_or_permission', undefined, '记忆这次没有读到'],
  ['not_done', undefined, undefined, '记忆这次没有读到'],
  // X5b: pin the current empty-result copy; this case is not a timeout.
  ['done', undefined, false, '查了记忆，没有找到相关内容'],
] as const)('preserves the memory refusal/empty sentence (%s, %s)', (state, error_class, found, sentence) => {
  const { container } = renderEvents(settled('search_memory', { kind: 'memory_search', state, error_class, found }, state === 'not_done' ? 'failed' : 'success'));
  expandRows(container);
  expect(container.querySelector('p')?.textContent).toBe(sentence);
  expect(container.textContent).not.toContain('超时');
});

it.each([
  [8, 'read_file', 'weekly-report-template.md', 'auth_or_permission'],
  [9, 'read_file', 'weekly-report-2026-11-09_2026-11-15.md', 'timeout'],
  [13, 'fs_read', 'weekly-report-template.md', 'auth_or_permission'],
  [21, 'read_file', 'sales-analysis-sales-run20.md', 'timeout'],
  [28, 'read_file', 'sales-analysis-sales-run20.md', 'timeout'],
  [32, 'read_file', 'weekly-report-2026-11-09_2026-11-15.md', 'timeout'],
  [35, 'read_file', 'weekly-report-2026-11-02_2026-11-08.md', 'auth_or_permission'],
])('keeps the failed-read basename expanded (run20 step4 row %s)', (_row, tool, filename, error_class) => {
  const { container } = renderEvents(settled(tool, { kind: 'file_read', state: 'not_done', filename, error_class }, 'failed'));
  const subtitle = () => container.querySelector('.run-step-summary')?.textContent;
  expect(subtitle()).toBe(filename);
  expandRows(container);
  expect(subtitle()).toBe(filename);
  expect(container.querySelector('[data-status="failed"] [role="img"][aria-label="未完成"]')).toBeTruthy();
  expect(container.textContent).not.toContain('workspace/');
});

it.each(['.env', 'client_secret.pem', 'api_key.txt', 'notes.txt?token=secret', 'id_rsa', 'server.key'])(
  'never restores a secret-shaped failed-read name on expansion (%s)', filename => {
    const { container } = renderEvents(settled('read_file', { kind: 'file_read', state: 'not_done', filename, error_class: 'timeout' }, 'failed'));
    expect(container.textContent).not.toContain(filename);
    expandRows(container);
    expect(container.textContent).not.toContain(filename);
    expect(container.querySelector('.run-step-summary')).toBeNull();
    expect(container.querySelector('p')?.textContent).toBe(zh.agent.chat.disclosure.failure.timeout);
  },
);

it.each([
  ['get_current_time', undefined, '查看时间', 'Check time'],
  ['load_skill', { kind: 'skill_load', state: 'done' }, '加载技能', 'Load skill'],
  ['plaza_get_new_posts', undefined, '浏览广场', 'Browse Plaza'],
])('names the native %s row and group in both languages', (tool, summary, chinese, english) => {
  for (const language of ['zh', 'en'] as const) {
    locale.language = language;
    const title = language === 'zh' ? chinese : english;
    const { container } = renderEvents(settled(tool, summary));
    expect(container.querySelector('.run-tool-history-title')?.textContent).toBe(title);
    expect(container.querySelector('.run-tool-group-label')?.textContent).toContain(title);
    expandRows(container);
    expect(container.textContent).not.toMatch(/处理请求|其他操作/);
    cleanup();
  }
});

const WEB_FETCH_TOOLS = ['web_fetch', 'advanced_web_fetch', 'anysearch_extract', 'exa_fetch', 'tavily_extract', 'firecrawl_fetch', 'xcrawl_scrape'];
it.each(WEB_FETCH_TOOLS)('titles every %s channel as a web-page read, with its host', tool => {
  for (const state of ['done', 'not_done']) {
    const host = state === 'done' ? 'example.com' : 'no-such-host-e2e.invalid';
    const { container } = renderEvents(settled(tool, { kind: 'web_fetch', state, host }, state === 'done' ? 'success' : 'failed'));
    const title = container.querySelector('.run-tool-history-title, .run-step-title');
    expect(title?.textContent).toBe('读取网页');
    expect(container.querySelector('.run-tool-history-summary, .run-step-summary')?.textContent).toBe(host);
    expandRows(container);
    expect(container.querySelector('p')?.textContent).toBe(state === 'done' ? `打开了网页 ${host}` : `没有打开网页 ${host}`);
    expect(container.textContent).not.toMatch(/处理请求|其他操作|https?:\/\//);
    if (state === 'done') expect(container.querySelector('.run-tool-group-label')?.textContent).toContain('读取网页');
    cleanup();
  }
});

it.each(WEB_FETCH_TOOLS)('keeps the web-page title and host on legacy %s rows without a backend summary', toolName => {
  const timeline = buildRunTimelineFromMessages([{ id: 'legacy', role: 'tool_call', content: '', toolName,
    toolArgs: { url: 'https://example.com/private/path?private=value' }, toolStatus: 'done' }]);
  expect(timeline.steps[0].summary).toBe('example.com');
  const t = ((key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values)) as unknown as TFunction;
  expect(toolStepLabel(toolName, t)).toBe('读取网页');
});
