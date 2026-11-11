// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(zh, key, fallback, values) }),
}));

afterEach(cleanup);

// Run13 defect: the member stopped the run while two issued web searches had
// no result. The stopped block must mark them 已中断, and the per-tool count
// must not read them as completed searches.
it('marks calls cut off by the stop as interrupted and counts only returned results', () => {
  const timeline = buildRunTimelineFromMessages([
    { role: 'tool_call', toolName: 'web_search', toolStatus: 'done', content: '',
      toolResult: '{"results":["industry news"]}', eventToolCallId: 'search-returned' },
    { role: 'assistant', eventType: 'assistant_commentary', content: '开始检索，先并行跑几组关键词' },
    { role: 'tool_call', toolName: 'web_search', toolStatus: 'running', content: '', eventToolCallId: 'search-cut-1' },
    { role: 'tool_call', toolName: 'web_search', toolStatus: 'running', content: '', eventToolCallId: 'search-cut-2' },
  ]);
  const stopped = { ...timeline, status: 'cancelled' as const, durationIsFinal: true };

  const { container } = render(<RunDisclosureBlock timeline={stopped} />);

  // Collapsed header: the aggregate count presents only the returned search.
  expect(container.textContent).toContain('搜索网页 1 次');
  expect(container.textContent).not.toContain('搜索网页 2 次');
  expect(container.textContent).not.toContain('搜索网页 3 次');

  // Expanded: the completed group keeps its label; the two cut-off calls are
  // labelled 已中断, both in the collapsed group row and on each history item.
  fireEvent.click(container.querySelector('.run-disclosure-header')!);
  expect(screen.getByText('本次操作：搜索网页')).toBeTruthy();
  expect(screen.getByText('已中断：搜索网页')).toBeTruthy();
  const badges = container.querySelectorAll('.run-tool-history-status');
  expect(badges).toHaveLength(2);
  expect(Array.from(badges).every((badge) => badge.textContent === '已中断')).toBe(true);
});
