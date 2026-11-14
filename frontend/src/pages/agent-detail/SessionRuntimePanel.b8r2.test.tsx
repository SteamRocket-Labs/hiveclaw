// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { useAuthStore } from '../../stores';
import { buildRuntimeSectionsModel } from '../session-workbench/timelineModel';
import { SessionRuntimePanel, WorkflowRunFocusPanel } from './SessionRuntimePanel';

const locale = vi.hoisted(() => ({ language: 'zh' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: any, values?: any) => translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
  i18n: { language: locale.language },
}) }));
afterEach(() => { cleanup(); locale.language = 'zh'; useAuthStore.setState({ user: null }); localStorage.clear(); });

const workflow = { id: 'workflow-id', runtime_kind: 'workflow', task_type: 'workflow', label: '周报流程', status: 'running',
  summary: '整理周报', elapsed_seconds: 3480, token_count: 1200, tool_use_count: 3 };

it.each(['zh', 'en'])('B8-r2 renders focused workflow facts with one duration in %s', language => {
  locale.language = language;
  // Members can open the focus panel, even though the admin console is hidden.
  useAuthStore.setState({ user: { id: 'member', role: 'member' } as never });
  const item = buildRuntimeSectionsModel({ runtime_sections: { workflows: [workflow] } }).workflows[0];
  const original = JSON.stringify(item);
  const { container } = render(<WorkflowRunFocusPanel workflow={item} onClose={vi.fn()} />);
  const meta = container.querySelector('.session-runtime-section-header small')!.textContent;
  expect(meta).toBe(language === 'zh'
    ? '整理周报 · 58 分钟 · 模型用量 1,200 · 工具调用 3 次'
    : '整理周报 · 58m · 1.2K tokens · 3 tools');
  expect(JSON.stringify(item)).toBe(original);
});

it.each([0, 1500, 1500000])('B8-r2 shows exact grouped Chinese counts and localized elapsed time for %s tokens', tokenCount => {
  useAuthStore.setState({ user: { id: 'admin', role: 'org_admin' } as never });
  const raw = { ...workflow, token_count: tokenCount };
  const original = JSON.stringify(raw);
  const { container } = render(<SessionRuntimePanel messages={[]} activeSession={{ id: 'session', title: '周报' }}
    sessionWorkbench={{ runtime_sections: { workflows: [raw] } } as never} />);
  const metrics = screen.getByTestId('session-runtime-summary-strip').querySelector('.session-runtime-summary-metrics')!;
  expect([...metrics.children].map(node => node.textContent)).toEqual(['1 个运行中', '0 个等待中', '58 分钟', tokenCount.toLocaleString('zh'), '3']);
  expect(container.querySelector('.session-runtime-row-meta')!.textContent).toBe(`整理周报 · 58 分钟 · 模型用量 ${tokenCount.toLocaleString('zh')} · 工具调用 3 次`);
  expect(JSON.stringify(raw)).toBe(original);
});

it.each(['subagent_name', 'workflow_name', 'child_agent_name', 'task_name', 'name'])
('B8-r2 preserves a chosen name equal to the type word from metadata.%s', key => {
  useAuthStore.setState({ user: { id: 'admin', role: 'org_admin' } as never });
  const raw = { ...workflow, label: 'workflow', metadata: { [key]: 'workflow' } };
  const original = JSON.stringify(raw);
  const { container } = render(<SessionRuntimePanel messages={[]} activeSession={{ id: 'session' }}
    sessionWorkbench={{ runtime_sections: { workflows: [raw] } } as never} />);
  fireEvent.click(screen.getByTestId('session-runtime-segment-workflow'));
  expect(container.querySelector('.session-runtime-row-title')?.textContent).toBe('workflow');
  expect(container.textContent).not.toContain('工作流运行-1');
  expect(JSON.stringify(raw)).toBe(original);
});

it('B8-r2 still localizes a type fallback when the candidate names are only whitespace', () => {
  useAuthStore.setState({ user: { id: 'admin', role: 'org_admin' } as never });
  const raw = { ...workflow, label: 'workflow', metadata: { subagent_name: '', workflow_name: ' ', child_agent_name: '\n', task_name: '', name: '' } };
  const { container } = render(<SessionRuntimePanel messages={[]} activeSession={{ id: 'session' }}
    sessionWorkbench={{ runtime_sections: { workflows: [raw] } } as never} />);
  expect(container.querySelector('.session-runtime-row-title')?.textContent).toBe('工作流运行-1');
});
