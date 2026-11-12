// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(zh, key, fallback, values) }),
}));

afterEach(cleanup);

function toolGroupToggle(container: HTMLElement) {
  return container.querySelector('[data-testid="run-disclosure-tool-group-toggle"]') as HTMLElement;
}

it('a step preview never shows raw markdown markers', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [{
    id: 'receipt', kind: 'subagent', title: 'Delegation update', status: 'done', visibility: 'visible',
    summary: '## 一、三条销售要点 **1.（规模与结构）本周合计 63,845 元**',
    systemEvent: { type: 'child_session', metadata: {} },
  }] }} />);
  expect(container.textContent).toContain('三条销售要点');
  expect(container.textContent).not.toContain('##');
  expect(container.textContent).not.toContain('**');
});

it('a long raw provider dump renders as one short plain line, not a wall of markdown', () => {
  const dump = [
    '已改用其他方式获取以下结果：',
    '',
    '**Firecrawl content from: https://deepwiki.com/facebook/react**',
    '',
    'Index your code with Devin',
  ].concat(Array.from({ length: 120 }, (_v, i) => `- line ${i} of the raw page markdown with [links](https://example.test/${i})`)).join('\n');
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [{
    id: 'fetch', kind: 'search', title: 'Fetch web page', status: 'done', visibility: 'visible',
    summary: '', details: dump,
  }] }} />);
  fireEvent.click(toolGroupToggle(container));
  fireEvent.click(container.querySelector('.run-tool-history-toggle') as HTMLButtonElement);
  const summary = container.querySelector('.run-detail-summary');
  expect(summary).toBeTruthy();
  const text = summary?.textContent || '';
  expect(text.length).toBeLessThanOrEqual(200);
  expect(text.endsWith('…')).toBe(true);
  expect(container.textContent).not.toContain('line 119');
});

it('a short plain tool result keeps its line layout', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [{
    id: 'fetch', kind: 'search', title: 'Fetch web page', status: 'done', visibility: 'visible',
    summary: '', details: '第一行结果\n第二行结果',
  }] }} />);
  fireEvent.click(toolGroupToggle(container));
  fireEvent.click(container.querySelector('.run-tool-history-toggle') as HTMLButtonElement);
  expect(container.textContent).toContain('第一行结果');
  expect(container.textContent).toContain('第二行结果');
  expect(container.querySelector('.run-detail-summary')).toBeNull();
});
