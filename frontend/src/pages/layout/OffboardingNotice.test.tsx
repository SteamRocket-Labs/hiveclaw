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
vi.mock('../../api/domains/triggers', () => ({ triggerApi: {
  list: vi.fn().mockResolvedValue([
    { id: 'daily', config: { authority_state: 'quarantined' } },
    { id: 'weekly', config: { authority_state: 'quarantined' } },
  ]),
  reclaim: vi.fn().mockResolvedValue({ is_enabled: false }), update: vi.fn(),
} }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const body = JSON.stringify({
  agents: [{ id: 'bot', name: 'Research bot' }], knowledge: [{ id: 'doc', title: 'Company guide' }],
  work: [{ id: 'run', agent_id: 'bot', task_id: 'task', title: 'Send report',
    known_results: [{ tool_name: 'read_file', state: 'completed', time: '2026-11-06T00:59:00Z' },
      { tool_name: 'send_email', state: 'started', time: '2026-11-06T01:00:00Z' }],
    side_effect_state: 'unknown', requires_confirmation: true }],
  triggers: [{ id: 'daily', agent_id: 'bot', name: 'Daily report' }, { id: 'weekly', agent_id: 'bot', name: 'Weekly report' }],
});

function notice(noticeBody = body) {
  const onCloseDetail = vi.fn();
  render(<MemoryRouter><NotificationCenter isOpen={false} unreadCount={1} notifications={[]}
    notifCategory="all" onSetNotifCategory={vi.fn()} onMarkAllRead={vi.fn()} onClose={vi.fn()}
    onNotificationClick={vi.fn()} onCloseDetail={onCloseDetail}
    selectedNotification={{ id: 'notice', type: 'offboarding_handover', title: 'Handover', body: noticeBody }} /></MemoryRouter>);
  return { onCloseDetail };
}

describe('offboarding handover notice', () => {
  it.each(['agents', 'knowledge', 'work', 'triggers'])('hides the %s section when it has no items', section => {
    notice(JSON.stringify({ ...JSON.parse(body), work: [], [section]: [] }));
    const title = section === 'agents' ? 'bots' : section;
    expect(screen.queryByRole('heading', { name: `handover.${title}` })).toBeNull();
    if (section === 'triggers') expect(screen.queryByText('handover.triggersPaused')).toBeNull();
  });

  it('renders known results before asking for a decision and never retries on confirmation', async () => {
    const { onCloseDetail } = notice();
    expect(screen.getByText('Research bot').getAttribute('href')).toBe('/agents/bot#chat');
    expect(screen.getByText('Company guide')).toBeTruthy();
    const result = screen.getByText(/agent.chat.disclosure.toolTitle.readFile/);
    expect(screen.getByText(/sessionWorkbench.approvalAction.sendEmail/)).toBeTruthy();
    expect(screen.queryByText(/read_file|send_email/)).toBeNull();
    expect(screen.getByText('handover.step.started')).toBeTruthy();
    expect(screen.getByText('handover.effect.unknown')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Send report' }).getAttribute('href')).toBe('/agents/bot?task_id=task#tasks');
    fireEvent.click(screen.getByRole('link', { name: 'Send report' }));
    expect(onCloseDetail).toHaveBeenCalledTimes(1);
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

  it('links each trigger to the existing reclaim view without a duplicate control or fetch', () => {
    const { onCloseDetail } = notice(JSON.stringify({ ...JSON.parse(body), work: [] }));
    const links = screen.getAllByRole('link', { name: 'handover.settings' });
    expect(links).toHaveLength(2);
    for (const link of links) expect(link.getAttribute('href')).toBe('/agents/bot#aware');
    expect(screen.queryByRole('button', { name: 'handover.reclaim' })).toBeNull();
    fireEvent.click(links[0]);
    expect(onCloseDetail).toHaveBeenCalledTimes(1);
    expect(triggerApi.list).not.toHaveBeenCalled();
    expect(triggerApi.reclaim).not.toHaveBeenCalled();
    expect(triggerApi.update).not.toHaveBeenCalled();
  });

  it('reads the saved task decision when the persisted notice is opened again', async () => {
    vi.mocked(taskApi.get).mockResolvedValueOnce({ task: { status: 'cancelled', actions: { can_reconcile: false } } } as any);
    notice();
    expect(await screen.findByText('handover.closed')).toBeTruthy();
    expect(screen.getByText(/agent.chat.disclosure.toolTitle.readFile/)).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(taskApi.retry).not.toHaveBeenCalled();
  });

  it('keeps settings links on a reopened notice and leaves the live trigger state to that view', () => {
    notice(JSON.stringify({ ...JSON.parse(body), work: [] }));
    expect(screen.getAllByRole('link', { name: 'handover.settings' })).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'handover.reclaim' })).toBeNull();
    expect(triggerApi.list).not.toHaveBeenCalled();
    expect(triggerApi.reclaim).not.toHaveBeenCalled();
  });

  it('uses the translated error presentation instead of raw exceptions', async () => {
    vi.mocked(taskApi.get).mockRejectedValueOnce(new Error('PRIVATE_BACKEND_DETAIL'));
    notice(JSON.stringify({ ...JSON.parse(body), work: [{ ...JSON.parse(body).work[0], known_results: [] }] }));
    expect(await screen.findAllByText('assistant.actionFailed')).not.toHaveLength(0);
    expect(screen.queryByText(/PRIVATE_/)).toBeNull();
  });

  it('lists ownerless work for an administrator without offering the successor access', () => {
    notice(JSON.stringify({ ...JSON.parse(body), triggers: [],
      work: [{ ...JSON.parse(body).work[0], needs_admin: true }] }));
    expect(screen.getByText('handover.needsAdmin')).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Send report' })).toBeNull();
    expect(taskApi.get).not.toHaveBeenCalled();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('shows an unknown outcome when a terminal task has no recorded effect state', () => {
    notice(JSON.stringify({ ...JSON.parse(body), triggers: [], work: [{ ...JSON.parse(body).work[0],
      known_results: [], side_effect_state: null, requires_confirmation: false }] }));
    expect(screen.getByText('handover.effect.unknown')).toBeTruthy();
    expect(screen.queryByText('handover.effect.null')).toBeNull();
    expect(taskApi.get).not.toHaveBeenCalled();
  });
});
