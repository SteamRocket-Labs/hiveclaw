// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import HrCreationRecoveryPanel from './HrCreationRecoveryPanel';
import { useAuthStore } from '../../stores';
import type { User } from '../../types';

const mocks = vi.hoisted(() => ({
  getHr: vi.fn(), listMine: vi.fn(), listRecoverable: vi.fn(), retry: vi.fn(), abandon: vi.fn(),
}));
vi.mock('../../api/domains/agents', () => ({ agentApi: { getHrAgent: mocks.getHr } }));
vi.mock('../../api/domains/hrCreation', () => ({ hrCreationApi: mocks }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, fallback?: string) => fallback || key }) }));
vi.mock('../../components/AppDialogs', () => ({ requestAppConfirm: vi.fn(), showAppToast: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const draft = {
  blueprint_id: 'draft-origin', hr_agent_id: 'my-assistant', session_id: 'original-work',
  draft_status: 'failed', blueprint: { name: 'Research colleague' },
  recovery: { can_resume: true, can_retry: true },
};

function mount(records = [draft]) {
  useAuthStore.setState({ user: { id: "owner", tenant_id: "company", role: "member" } as User });
  mocks.getHr.mockResolvedValue({ id: 'system-hr' });
  mocks.listMine.mockResolvedValue(records);
  mocks.listRecoverable.mockResolvedValue([draft]);
  mocks.retry.mockResolvedValue(draft);
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><HrCreationRecoveryPanel /></MemoryRouter>
  </QueryClientProvider>);
}

it('reopens the actual originating work without requiring a System HR session', async () => {
  mount();
  const link = await screen.findByRole('link');
  expect(link.getAttribute('href')).toBe('/agents/my-assistant?session_id=original-work#chat');
  expect(mocks.listMine).toHaveBeenCalled();
  expect(screen.getByText('Needs attention')).toBeTruthy();
  expect(mocks.getHr).not.toHaveBeenCalled();
});

it('retries the draft under its stored originating employee', async () => {
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Retry provisioning' }));
  await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith('my-assistant', 'draft-origin'));
});

it('recovers the first task without removing or reopening employee provisioning', async () => {
  const completed = { ...draft, draft_status: 'completed', provisioning: { first_task: { status: 'blocked' } } };
  mount([completed]);
  fireEvent.click(await screen.findByRole('button', { name: 'Retry first task' }));
  await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith('my-assistant', 'draft-origin'));
  expect(screen.queryByRole('button', { name: 'Remove unfinished employee' })).toBeNull();
});
