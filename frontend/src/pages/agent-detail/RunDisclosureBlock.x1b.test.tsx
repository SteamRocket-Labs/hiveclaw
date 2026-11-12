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
      target_agent_id: 'agent-colleague',
      child_session_id: 'session-colleague',
      target_agent_name: '周报助手',
    },
  },
};

function renderBlock(step: typeof receiptStep, listedAgents: Array<{ id: string }>) {
  const client = new QueryClient();
  client.setQueryData(['agents', 'tenant-1'], listedAgents);
  window.localStorage.setItem('current_tenant_id', 'tenant-1');
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [step] }} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

it('titles the receipt by its direction: the result came back from the colleague', () => {
  const { container } = renderBlock(receiptStep, []);
  expect(container.textContent).toContain('同事交回的结果');
  expect(container.textContent).not.toContain('交给其他数字员工');
});

it('links to the colleague conversation only when the member already sees that employee', () => {
  const { container } = renderBlock(receiptStep, [{ id: 'agent-colleague' }]);
  fireEvent.click(container.querySelector('.run-step-toggle') as HTMLButtonElement);
  const link = container.querySelector(`a[href="/agents/agent-colleague/sessions/session-colleague"]`);
  expect(link).toBeTruthy();
  expect(link?.textContent).toContain('查看这位同事的对话');
});

it('renders no colleague link when the employee is not in the member list', () => {
  const { container } = renderBlock(receiptStep, [{ id: 'agent-other' }]);
  fireEvent.click(container.querySelector('.run-step-toggle') as HTMLButtonElement);
  expect(container.querySelector('a[href^="/agents/agent-colleague"]')).toBeNull();
});

it('a started delegation keeps the outgoing title', () => {
  const { container } = renderBlock({ ...receiptStep, title: 'Delegation update' }, []);
  expect(container.textContent).toContain('交给其他数字员工');
});
