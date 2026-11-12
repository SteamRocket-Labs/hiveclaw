// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';

import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(zh, key, fallback, values) }),
}));

afterEach(() => { cleanup(); window.localStorage.clear(); });

const receiptStep = {
  id: 'receipt', kind: 'subagent' as const, title: 'Delegation result', status: 'done' as const,
  visibility: 'visible' as const, details: '「周报助手」已完成委派任务。',
  systemEvent: {
    type: 'child_session',
    metadata: {
      to_agent: 'agent-colleague',
      child_session_id: 'session-colleague',
      to_agent_name: '周报助手',
    },
  },
};

function renderBlock(step: typeof receiptStep, viewerOwnsSession: boolean) {
  return render(
    <MemoryRouter>
      <RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [step] }}
        viewerOwnsSession={viewerOwnsSession} />
    </MemoryRouter>,
  );
}

it('titles the receipt by its direction: the result came back from the colleague', () => {
  const { container } = renderBlock(receiptStep, false);
  expect(container.textContent).toContain('同事交回的结果');
  expect(container.textContent).not.toContain('交给其他数字员工');
});

it('links to the colleague conversation inside the viewer\'s own session (X1-r2 ruling 6)', () => {
  const { container } = renderBlock(receiptStep, true);
  fireEvent.click(container.querySelector('.run-step-toggle') as HTMLButtonElement);
  const link = container.querySelector(`a[href="/agents/agent-colleague/sessions/session-colleague"]`);
  expect(link).toBeTruthy();
  expect(link?.textContent).toContain('查看这位同事的对话');
});

it('renders no colleague link for a viewer who does not own the session (a group member)', () => {
  const { container } = renderBlock(receiptStep, false);
  fireEvent.click(container.querySelector('.run-step-toggle') as HTMLButtonElement);
  expect(container.querySelector('a[href^="/agents/agent-colleague"]')).toBeNull();
});

it('a started delegation keeps the outgoing title', () => {
  const { container } = renderBlock({ ...receiptStep, title: 'Delegation update' }, false);
  expect(container.textContent).toContain('交给其他数字员工');
});

it('a failed delegation reads as the colleague not finishing (X1-r2 ruling 8)', () => {
  const { container } = renderBlock({ ...receiptStep, title: 'Delegation unfinished' }, false);
  expect(container.textContent).toContain('同事没有完成这项工作');
  expect(container.textContent).not.toContain('同事交回的结果');
});
