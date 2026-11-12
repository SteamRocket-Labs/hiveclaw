// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
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

function renderBlock(step: typeof receiptStep, viewerOwnsSession: boolean, roster: Array<{ id: string }> = [{ id: 'agent-colleague' }]) {
  // The roster gate reads the permission-filtered ['agents', tenantId] cache
  // Layout already holds (X1-r3 ruling 1); no component request is made.
  window.localStorage.setItem('current_tenant_id', 'tenant-1');
  const client = new QueryClient();
  client.setQueryData(['agents', 'tenant-1'], roster);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [step] }}
          viewerOwnsSession={viewerOwnsSession} />
      </MemoryRouter>
    </QueryClientProvider>,
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

it('renders no colleague link when the colleague is outside the member\'s roster (X1-r3 ruling 1)', () => {
  // Owning the session does not prove the member can open this colleague;
  // the roster cache says they cannot, so the link stays hidden.
  const { container } = renderBlock(receiptStep, true, [{ id: 'agent-someone-else' }]);
  fireEvent.click(container.querySelector('.run-step-toggle') as HTMLButtonElement);
  expect(container.querySelector('a[href^="/agents/agent-colleague"]')).toBeNull();
});

it('links an unfinished delegation too: the child session exists (X1-r3 ruling 1)', () => {
  const { container } = renderBlock({ ...receiptStep, title: 'Delegation unfinished' }, true);
  fireEvent.click(container.querySelector('.run-step-toggle') as HTMLButtonElement);
  const link = container.querySelector(`a[href="/agents/agent-colleague/sessions/session-colleague"]`);
  expect(link).toBeTruthy();
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
