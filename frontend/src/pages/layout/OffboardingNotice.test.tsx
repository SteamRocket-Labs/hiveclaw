// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import NotificationCenter from './NotificationCenter';
import { taskApi } from '../../api/domains/tasks';
import { triggerApi } from '../../api/domains/triggers';

vi.mock('react-i18next', () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t }) };
});
vi.mock('../../api/domains/tasks', () => ({ taskApi: {
  get: vi.fn().mockResolvedValue({ task: { actions: { can_reconcile: true } } }),
  reconcile: vi.fn().mockResolvedValue({}), retry: vi.fn(),
} }));
vi.mock('../../api/domains/triggers', () => ({ triggerApi: { reclaim: vi.fn().mockResolvedValue({ is_enabled: false }), update: vi.fn() } }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const body = JSON.stringify({
  agents: [{ id: 'bot', name: 'Research bot' }], knowledge: [{ id: 'doc', title: 'Company guide' }],
  work: [{ id: 'run', agent_id: 'bot', task_id: 'task', title: 'Send report',
    known_results: ['Report created: ORCHID42.pdf'], requires_confirmation: true }],
  triggers: [{ id: 'daily', agent_id: 'bot', name: 'Daily report' }, { id: 'weekly', agent_id: 'bot', name: 'Weekly report' }],
});

function notice() {
  render(<MemoryRouter><NotificationCenter isOpen={false} unreadCount={1} notifications={[]}
    notifCategory="all" onSetNotifCategory={vi.fn()} onMarkAllRead={vi.fn()} onClose={vi.fn()}
    onNotificationClick={vi.fn()} onCloseDetail={vi.fn()}
    selectedNotification={{ id: 'notice', type: 'offboarding_handover', title: 'Handover', body }} /></MemoryRouter>);
}

describe('offboarding handover notice', () => {
  it('renders known results before asking for a decision and never retries on confirmation', async () => {
    notice();
    expect(screen.getByText('Research bot').getAttribute('href')).toBe('/agents/bot#chat');
    expect(screen.getByText('Company guide')).toBeTruthy();
    const result = screen.getByText('Report created: ORCHID42.pdf');
    const review = await screen.findByText('handover.reviewFirst');
    expect(result.compareDocumentPosition(review) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const confirm = screen.getByRole('button', { name: 'handover.confirmSafe' });
    expect(confirm.hasAttribute('disabled')).toBe(true);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Recipient already has the report.' } });
    fireEvent.click(screen.getByRole('button', { name: 'handover.closeWork' }));
    await waitFor(() => expect(taskApi.reconcile).toHaveBeenCalledWith('bot', 'task', {
      decision: 'close_without_retry', reason: 'Recipient already has the report.',
    }));
    expect(taskApi.retry).not.toHaveBeenCalled();
  });

  it('reclaims each trigger separately and keeps enabling on the existing settings path', async () => {
    notice();
    fireEvent.click(screen.getAllByRole('button', { name: 'handover.reclaim' })[0]);
    await waitFor(() => expect(triggerApi.reclaim).toHaveBeenCalledWith('bot', 'daily'));
    expect(triggerApi.reclaim).toHaveBeenCalledTimes(1);
    expect(triggerApi.update).not.toHaveBeenCalled();
    expect(screen.getByRole('link', { name: 'handover.settings' }).getAttribute('href')).toBe('/agents/bot#aware');
    fireEvent.click(screen.getByRole('button', { name: 'handover.keepOff' }));
    await waitFor(() => expect(triggerApi.reclaim).toHaveBeenCalledWith('bot', 'weekly'));
    expect(triggerApi.update).not.toHaveBeenCalled();
  });

  it('reads the saved task decision when the persisted notice is opened again', async () => {
    vi.mocked(taskApi.get).mockResolvedValueOnce({ task: { status: 'cancelled', actions: { can_reconcile: false } } } as any);
    notice();
    expect(await screen.findByText('handover.closed')).toBeTruthy();
    expect(screen.getByText('Report created: ORCHID42.pdf')).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(taskApi.retry).not.toHaveBeenCalled();
  });
});
