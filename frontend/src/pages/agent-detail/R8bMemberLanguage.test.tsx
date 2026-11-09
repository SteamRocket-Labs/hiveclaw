// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import RunDisclosureBlock from './RunDisclosureBlock';
import SlashCommandMenu from './SlashCommandMenu';
import { AssistantMessageBody } from './CanonicalCardAssistantSupplement';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: i18n.t.bind(i18n), i18n }) }));
vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: [
  { name: 'compact', canonical_name: 'compact', category: 'session', source: 'builtin',
    description: 'Run manual compaction with pre/post compaction hooks.' },
  { name: 'team', canonical_name: 'team_create', category: 'team', source: 'builtin',
    description: 'Create a CC-style Team container under the current session.' },
  { name: 'usage', canonical_name: 'usage', category: 'diagnostic', source: 'diagnostic',
    description: 'Read-only usage diagnostic command.' },
] }) }));
beforeAll(async () => { await i18n.init({ lng: 'zh', fallbackLng: 'en', resources: {
  zh: { translation: zh }, en: { translation: en },
}, interpolation: { escapeValue: false } }); });
afterEach(async () => { cleanup(); await i18n.changeLanguage('zh'); });

const recall = { schema: 'hive.session_recall_error.v1', status: 'unavailable', code: 'session_recall_evidence_unavailable',
  retryable: false, message: '暂时无法读取这部分历史对话，请稍后重试。',
  diagnostic_message: 'T0 history failed integrity verification (t0_record_too_large).' };
const availability = { schema: 'hive.explicit_memory_availability.v1', status: 'partial',
  issues: [{ memory_id: 'explicit_private_id', code: 'explicit_memory_source_unverified' }],
  message: '目前只读到了部分记忆，暂不可用的部分不能用来下结论，可以稍后重试。' };

it.each([JSON.stringify(recall), JSON.stringify(availability, null, 2),
  `## 相关对话\n${JSON.stringify(recall)}\n\n## 可用情况\n${JSON.stringify(availability)}`])(
  'projects known memory envelopes through the expanded tool view: %s', result => {
    const details = { toolName: 'search_memory', result };
    const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [{
      id: 'step', kind: 'tool', title: 'Tool call', status: 'done', visibility: 'visible', details,
    }] }} />);
    fireEvent.click(container.querySelector('.run-tool-group-toggle')!);
    fireEvent.click(container.querySelector('.run-tool-history-toggle')!);
    expect(container.textContent).toContain(result.includes(recall.schema) ? recall.message : availability.message);
    if (result.includes(recall.schema) && result.includes(availability.schema)) expect(container.textContent).toContain(availability.message);
    expect(container.textContent).not.toMatch(/hive\.|schema|status|code|retryable|T0|explicit_|[{}]/);
    expect(details.result).toBe(result);
  },
);

it.each(['', '❌ save_memory failed.\n\n'])('projects a save-memory error even when the transport marked the step completed (%s)', prefix => {
  const result = prefix + '<tool_error>{"ok":false,"error_class":"governance_dependency_unavailable","message":"English diagnostic","retryable":true}</tool_error>';
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'save', status: 'running', steps: [{
    id: 'step', kind: 'tool', title: 'Tool call', status: 'done', visibility: 'visible', details: { toolName: 'save_memory', result },
  }] }} />);
  fireEvent.click(container.querySelector('.run-tool-group-toggle')!);
  fireEvent.click(container.querySelector('.run-tool-history-toggle')!);
  expect(container.textContent).toContain(zh.agent.chat.disclosure.failure.unavailable);
  expect(container.textContent).not.toMatch(/tool_error|English|retryable|[{}]/);
});

it.each([[301000, '5 分 1 秒'], [921000, '15 分 21 秒'], [60000, '1 分钟'], [1000, '1 秒']] as const)(
  'formats the mounted run duration %s in Chinese', (durationMs, expected) => {
    const { container } = render(<RunDisclosureBlock timeline={{ id: 'elapsed', status: 'done', durationMs, steps: [] }} />);
    expect(container.textContent).toContain(expected);
  },
);
it('keeps English duration units in the English UI', async () => {
  await i18n.changeLanguage('en');
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'elapsed', status: 'done', durationMs: 301000, steps: [] }} />);
  expect(container.textContent).toContain('5m 1s');
});

it('localizes the mounted slash menu descriptions and categories', () => {
  const { container } = render(<SlashCommandMenu agentId="employee" sessionId="conversation" inputValue="/"
    disabled={false} onPickCommand={() => undefined} />);
  expect(container.textContent).toContain('整理对话上下文，保留重要信息。');
  expect(container.textContent).toContain('创建协作小组。');
  expect(container.textContent).toContain('查看当前用量。');
  expect(container.textContent).not.toMatch(/Run manual|hooks|session|SESSION|CC-style|diagnostic/);
});

it.each(['DELIVERABLE: workspace/report.md', '**DELIVERABLE**: workspace/report.md',
  '- `DELIVERABLE`: workspace/report.md', 'deliverable：workspace/report.md'])(
  'keeps platform declarations out of rendered and streamed replies: %s', marker => {
    const content = `报告已完成。\n\n${marker}\n\n销售额为 76000。`;
    const { container } = render(<AssistantMessageBody content={content} streaming supplemental={false} supplementalLabel="补充说明" />);
    expect(container.textContent).toContain('销售额为 76000。');
    expect(container.textContent).not.toMatch(/deliverable|workspace\/report/i);
    expect(content).toContain(marker);
  },
);
