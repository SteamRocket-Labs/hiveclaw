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

it('a long raw provider dump is replaced by a sentence (X3b item 2)', () => {
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
  expect(container.querySelector('.run-detail-summary')).toBeNull();
  // X3b item 2 supersedes raw-output display even when no summary is available.
  expect(container.querySelector('p')?.textContent).toBe('读取网页。已返回结果');
  expect(container.textContent).not.toContain('line 0 of the raw page markdown');
  expect(container.textContent).not.toContain('line 119 of the raw page markdown');
});

it('a short plain tool result is replaced by a sentence (X3b item 2)', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [{
    id: 'fetch', kind: 'search', title: 'Fetch web page', status: 'done', visibility: 'visible',
    summary: '', details: '第一行结果\n第二行结果',
  }] }} />);
  fireEvent.click(toolGroupToggle(container));
  fireEvent.click(container.querySelector('.run-tool-history-toggle') as HTMLButtonElement);
  // X3b item 2 applies to short raw results too.
  expect(container.querySelector('p')?.textContent).toBe('读取网页。已返回结果');
  expect(container.textContent).not.toContain('第一行结果');
  expect(container.textContent).not.toContain('第二行结果');
  expect(container.querySelector('.run-detail-summary')).toBeNull();
});

it('the marked in-run copy of the answer renders nowhere in the block', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'done', steps: [{
    id: 'copy', kind: 'prose', title: 'Assistant update', status: 'done', visibility: 'visible',
    details: '最终答复原文', repeatsAnswer: true,
  }] }} />);
  expect(container.textContent).not.toContain('最终答复原文');
});
