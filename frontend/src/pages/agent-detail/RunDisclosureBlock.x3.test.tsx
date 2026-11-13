// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';

// X3: an expanded step of a summarized tool shows one plain sentence, never
// the model-facing result bytes, and offers no way to view the raw text.

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
}) }));
afterEach(cleanup);

function expand(container: HTMLElement) {
  const group = container.querySelector('[data-testid="run-disclosure-tool-group-toggle"]');
  if (group) fireEvent.click(group);
  const history = container.querySelector('.run-tool-history-toggle');
  if (history) fireEvent.click(history);
}

function stepText(step: Record<string, unknown>): string {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [step as never] }} />);
  expand(container);
  return container.textContent || '';
}

describe.each([['zh', 'zh'] as const, ['en', 'en'] as const])('member tool summary sentence (%s)', (_name, language) => {
  it('a memory step renders one plain sentence, not the management notes', () => {
    locale.language = language;
    const text = stepText({
      id: 'memory', kind: 'tool', title: 'Load memory', status: 'done', visibility: 'visible',
      summary: '',
      details: {
        toolName: 'load_memory',
        args: { ids: ['explicit_284ff1e71ca75b9f92800d2c6fc40ab9'] },
        result: '', rawResult: '',
        memberSummary: { kind: 'memory_read', failed: false },
      },
    });
    expect(text).toContain(language === 'zh' ? '读取了记忆' : 'Read memory');
    for (const leak of ['不要向用户展示', '284ff1e71ca75b9f92800d2c6fc40ab9', 'load_memory', 'scope=sessions', '## 读取到的记忆']) {
      expect(text).not.toContain(leak);
    }
  });

  it('a web fetch step names the host from the typed summary only', () => {
    locale.language = language;
    const text = stepText({
      id: 'fetch', kind: 'search', title: 'Fetch web page', status: 'done', visibility: 'visible',
      summary: 'https://deepwiki.com/react',
      details: {
        toolName: 'web_fetch',
        args: { url: 'https://deepwiki.com/react' },
        result: '', rawResult: '',
        memberSummary: { kind: 'web_fetch', failed: false, host: 'deepwiki.com' },
      },
    });
    expect(text).toContain(language === 'zh' ? '打开了网页 deepwiki.com' : 'Opened the web page deepwiki.com');
    for (const leak of ['Firecrawl', 'Syntax error', '[Packages]']) {
      expect(text).not.toContain(leak);
    }
  });

  it('a refused colleague result says it was not read, agreeing with the typed status', () => {
    locale.language = language;
    const text = stepText({
      id: 'read', kind: 'tool', title: 'Read task result', status: 'done', visibility: 'visible',
      summary: '',
      details: {
        toolName: 'read_runtime_result',
        args: { result_ref: 'hive.runtime-result://r/abc' },
        result: '', rawResult: '',
        memberSummary: { kind: 'colleague_result', failed: true },
      },
    });
    expect(text).toContain(language === 'zh' ? '同事的结果这次没读到' : "The colleague's result could not be read this time");
    expect(text).not.toContain('source_access_denied');
    expect(text).not.toContain('runtime-result');
  });

  it('a web search step and a capability lookup step render their sentences', () => {
    locale.language = language;
    const search = stepText({
      id: 'search', kind: 'search', title: 'Search web', status: 'done', visibility: 'visible',
      summary: 'react reconciler',
      details: { toolName: 'web_search', args: { query: 'react reconciler' }, result: '', rawResult: '',
        memberSummary: { kind: 'web_search', failed: false } },
    });
    expect(search).toContain(language === 'zh' ? '搜索了网页' : 'Searched the web');
    const lookup = stepText({
      id: 'lookup', kind: 'tool', title: 'Loading tools', status: 'done', visibility: 'visible',
      summary: '',
      details: { toolName: 'tool_search', args: { query: 'wiki' }, result: '', rawResult: '',
        memberSummary: { kind: 'capability_lookup', failed: false } },
    });
    expect(lookup).toContain(language === 'zh' ? '查找了可用的能力' : 'Looked up available capabilities');
    expect(lookup).not.toContain('mcp_server_id');
  });
});

it('a summarized tool exposes no way to view the raw text (no raw fold)', () => {
  locale.language = 'zh';
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [{
    id: 'memory', kind: 'tool', title: 'Search memory', status: 'done', visibility: 'visible',
    summary: '周报',
    details: {
      toolName: 'search_memory',
      args: { query: '周报 格式' },
      result: '', rawResult: '',
      memberSummary: { kind: 'memory_search', failed: false },
    },
  }] }} />);
  expand(container);
  expect(container.textContent).toContain('查了记忆');
  // The expanded step holds only the sentence — no nested fold that could
  // reveal raw bytes.
  expect(container.querySelectorAll('.run-tool-history-toggle, .run-step-toggle').length).toBeLessThan(2);
});

it('a step without a typed summary keeps the production display (owner ruling 11-10)', () => {
  locale.language = 'zh';
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [{
    id: 'file', kind: 'file', title: 'Read file', status: 'done', visibility: 'visible',
    summary: 'sales.csv',
    details: { toolName: 'read_file', args: { path: 'uploads/sales.csv' },
      result: 'date,sales\n2026-11-01,35', rawResult: 'date,sales\n2026-11-01,35' },
  }] }} />);
  expand(container);
  expect(container.textContent).toContain('2026-11-01,35');
});
