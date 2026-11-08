// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import UserManagement from './UserManagement';
import { usersApi } from '../api/domains/users';
import { ApiError } from '../api/core';

vi.mock('react-i18next', () => {
  const t = (key: string) => key;
  return { useTranslation: () => ({ t, i18n: { language: 'en' } }) };
});
vi.mock('../stores', () => ({ useAuthStore: () => ({ user: { id: 'admin', role: 'org_admin' }, setUser: vi.fn() }) }));
vi.mock('./ExternalPrincipalBindingsPanel', () => ({ default: () => null }));
vi.mock('../api/domains/externalPrincipals', () => ({ externalPrincipalsApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock('../components/ui', () => ({ Modal: ({ open, children, footer }: any) => open
  ? <div role="dialog">{children}{footer}</div> : null }));
vi.mock('../api/domains/users', () => ({ usersApi: {
  list: vi.fn().mockResolvedValue([{ id: 'leaver', username: 'Leaver', email: 'leaver@example.test',
    role: 'member', is_active: true, agents_count: 1, created_at: '2026-11-06',
    tokens_used_today: 0, tokens_used_month: 0 }]),
  previewOffboarding: vi.fn().mockResolvedValue({ user_id: 'leaver', successor_user_id: 'successor', owned_agents: [{ id: 'bot', name: 'Company bot' }],
    handover: { knowledge: [{ id: 'guide', title: 'Company guide' }],
      work: [{ id: 'work', title: 'Pending report', receiver_user_id: null }], triggers: [{ id: 'daily', name: 'Daily report' }] },
    eligible_successors: [{ id: 'successor', display_name: 'Successor' }], default_successor_id: 'successor',
    revocations: { agent_permissions: 0, resource_permissions: 0, knowledge_grants: 0, refresh_tokens: 0,
      external_principals: 0, local_bridge_connections: 0, runtime_tasks: 0, pending_approvals: 0 }, blockers: [] }),
  offboard: vi.fn(),
} }));
const defaultPreview = vi.mocked(usersApi.previewOffboarding).getMockImplementation();
const defaultUsers = vi.mocked(usersApi.list).getMockImplementation();
afterEach(() => {
  cleanup(); vi.clearAllMocks();
  vi.mocked(usersApi.previewOffboarding).mockReset().mockImplementation(defaultPreview!);
  vi.mocked(usersApi.list).mockReset().mockImplementation(defaultUsers!);
});

it('shows processing as retryable and keeps the same offboarding form for retry', async () => {
  const message = '知识仍在处理中，请一两分钟后重试。';
  vi.mocked(usersApi.offboard).mockRejectedValueOnce(new ApiError(409, message, {
    code: 'offboarding_knowledge_processing', retryable: true, message,
  })).mockResolvedValueOnce({ transferred_agent_count: 1 } as any);
  render(<UserManagement />);
  fireEvent.click(await screen.findByRole('button', { name: 'userManagement.deactivateMember' }));
  await screen.findByText('Company bot');
  expect(screen.getByText('Company guide')).toBeTruthy();
  expect(screen.getByText('Pending report')).toBeTruthy();
  expect(screen.getByText('Daily report')).toBeTruthy();
  expect(screen.getByText('userManagement.handoverCredentials')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('userManagement.offboardingReason'), { target: { value: 'Employment ended' } });
  fireEvent.click(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }));
  const status = await screen.findByRole('status');
  expect(status.textContent).toBe(`⏳ ${message}`);
  expect(status.style.background).toBe('var(--warning)');
  expect(screen.getByRole('dialog')).toBeTruthy();
  expect((screen.getByLabelText('userManagement.offboardingReason') as HTMLTextAreaElement).value).toBe('Employment ended');
  await waitFor(() => expect(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }).hasAttribute('disabled')).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }));
  await waitFor(() => expect(usersApi.offboard).toHaveBeenCalledTimes(2));
  expect(vi.mocked(usersApi.offboard).mock.calls[1]).toEqual(vi.mocked(usersApi.offboard).mock.calls[0]);
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
});

it('refreshes the handover list for the chosen receiving member before submitting', async () => {
  const preview = { user_id: 'leaver', successor_user_id: 'first', owned_agents: [{ id: 'bot', name: 'Company bot' }],
    eligible_successors: [{ id: 'first', display_name: 'First' }, { id: 'receiver', display_name: 'Receiver' }],
    default_successor_id: 'first', blockers: [],
    revocations: { agent_permissions: 0, resource_permissions: 0, knowledge_grants: 0, refresh_tokens: 0,
      external_principals: 0, local_bridge_connections: 0, runtime_tasks: 0, pending_approvals: 0 },
    handover: { knowledge: [{ id: 'source', title: 'Original copy' }], work: [], triggers: [] } };
  vi.mocked(usersApi.previewOffboarding).mockResolvedValueOnce(preview as any).mockResolvedValueOnce({ ...preview, successor_user_id: 'receiver',
    handover: { ...preview.handover, knowledge: [{ id: 'existing', title: 'Receiver copy', merged: true }] } } as any);
  vi.mocked(usersApi.offboard).mockResolvedValue({ transferred_agent_count: 1 } as any);
  render(<UserManagement />);
  fireEvent.click(await screen.findByRole('button', { name: 'userManagement.deactivateMember' }));
  await screen.findByText('Original copy');
  fireEvent.change(screen.getByLabelText('userManagement.successorAdmin'), { target: { value: 'receiver' } });
  await screen.findByText('Receiver copy');
  expect(screen.getByText(/handover.merged/)).toBeTruthy();
  expect(screen.queryByText('Original copy')).toBeNull();
  expect(usersApi.previewOffboarding).toHaveBeenLastCalledWith('leaver', undefined, 'receiver');
  fireEvent.change(screen.getByLabelText('userManagement.offboardingReason'), { target: { value: 'Employment ended' } });
  fireEvent.click(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }));
  await waitFor(() => expect(usersApi.offboard).toHaveBeenCalledWith('leaver', expect.objectContaining({ successor_user_id: 'receiver' }), undefined));
});

it('loads the initially shown receiver when the preview has no default receiver', async () => {
  const preview = { user_id: 'leaver', successor_user_id: null, owned_agents: [{ id: 'bot', name: 'Company bot' }],
    eligible_successors: [{ id: 'receiver', display_name: 'Receiver' }], default_successor_id: null, blockers: [],
    revocations: { agent_permissions: 0, resource_permissions: 0, knowledge_grants: 0, refresh_tokens: 0,
      external_principals: 0, local_bridge_connections: 0, runtime_tasks: 0, pending_approvals: 0 },
    handover: { knowledge: [{ id: 'source', title: 'Unassigned copy' }], work: [], triggers: [] } };
  let finish: (value: any) => void = () => {};
  vi.mocked(usersApi.previewOffboarding).mockResolvedValueOnce(preview as any)
    .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  render(<UserManagement />);
  fireEvent.click(await screen.findByRole('button', { name: 'userManagement.deactivateMember' }));
  await waitFor(() => expect(usersApi.previewOffboarding).toHaveBeenCalledWith('leaver', undefined, 'receiver'));
  expect(screen.queryByText('Unassigned copy')).toBeNull();
  finish({ ...preview, successor_user_id: 'receiver', handover: { ...preview.handover, knowledge: [{ id: 'existing', title: 'Receiver copy' }] } });
  await screen.findByText('Receiver copy');
  expect((screen.getByLabelText('userManagement.successorAdmin') as HTMLSelectElement).value).toBe('receiver');
});

it('clearing the receiver hides the previous handover list without requesting a default receiver', async () => {
  const message = '知识仍在处理中，请一两分钟后重试。';
  vi.mocked(usersApi.previewOffboarding).mockResolvedValueOnce({
    ...await defaultPreview!('leaver'), blockers: [message],
  });
  render(<UserManagement />);
  fireEvent.click(await screen.findByRole('button', { name: 'userManagement.deactivateMember' }));
  await screen.findByText('Company guide');
  expect(screen.getByText(message)).toBeTruthy();
  fireEvent.change(screen.getByLabelText('userManagement.successorAdmin'), { target: { value: '' } });
  await waitFor(() => expect((screen.getByLabelText('userManagement.successorAdmin') as HTMLSelectElement).value).toBe(''));
  expect(screen.queryByText('Company guide')).toBeNull();
  expect(screen.queryByText('Pending report')).toBeNull();
  expect(screen.queryByText('Daily report')).toBeNull();
  expect(screen.queryByText(message)).toBeNull();
  expect(usersApi.previewOffboarding).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }).hasAttribute('disabled')).toBe(true);
});

it('keeps the no-active-receiver blocker when the preview was computed without a receiver', async () => {
  const message = '没有可接手的在职成员。';
  vi.mocked(usersApi.previewOffboarding).mockResolvedValueOnce({
    ...await defaultPreview!('leaver'), successor_user_id: null, default_successor_id: null, eligible_successors: [], blockers: [message],
  });
  render(<UserManagement />);
  fireEvent.click(await screen.findByRole('button', { name: 'userManagement.deactivateMember' }));
  await screen.findByText(message);
  expect((screen.getByLabelText('userManagement.successorAdmin') as HTMLSelectElement).value).toBe('');
  expect(screen.queryByText('Company guide')).toBeNull();
  expect(screen.queryByText('Pending report')).toBeNull();
  expect(screen.queryByText('Daily report')).toBeNull();
  expect(usersApi.previewOffboarding).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }).hasAttribute('disabled')).toBe(true);
});

it('uses digital employee and responsible person terms in Chinese management copy', async () => {
  const zh = (await import('../i18n/zh.json')).default;
  expect(zh.workspace.digitalEmployees).toMatchObject({ changeOwnerTitle: '更换数字员工负责人', newOwner: '新负责人' });
  for (const copy of [zh.userManagement.offboardingComplete, zh.userManagement.offboardingSummary,
    zh.userManagement.offboardingRecovery, ...Object.values(zh.workspace.digitalEmployees)]) {
    expect(copy).not.toMatch(/Agent|Owner|Creator|Sponsor/);
  }
});

it('labels the existing handover action as offboarding rather than deletion', async () => {
  const zh = (await import('../i18n/zh.json')).default;
  const en = (await import('../i18n/en.json')).default;
  expect(zh.userManagement.deactivateMember).toBe('办理离职');
  expect(zh.userManagement).toMatchObject({ deactivateMemberTitle: '办理成员离职', offboardingReason: '离职原因',
    offboardingPreviewFailed: '加载离职交接清单失败。', offboardingFailed: '办理成员离职失败。' });
  expect(en.userManagement.deactivateMember).toBe('Offboard member');
});

it('G3f review 1: drops member A preview after cancel and submits member B bots', async () => {
  const [user] = await defaultUsers!();
  vi.mocked(usersApi.list).mockResolvedValue([user, { ...user, id: 'second', username: 'Second' }]);
  let finishA!: (value: any) => void;
  const preview = await defaultPreview!('leaver');
  vi.mocked(usersApi.previewOffboarding).mockImplementationOnce(() => new Promise(resolve => { finishA = resolve; }))
    .mockResolvedValueOnce({ ...preview, user_id: 'second', owned_agents: [{ id: 'second-bot', name: 'Second bot', status: 'idle', agent_class: 'internal_tenant' }] });
  vi.mocked(usersApi.offboard).mockResolvedValue({ transferred_agent_count: 1 } as any);
  render(<UserManagement />);
  const buttons = await screen.findAllByRole('button', { name: 'userManagement.deactivateMember' });
  fireEvent.click(buttons[0]);
  fireEvent.click(screen.getByRole('button', { name: 'common.cancel' }));
  fireEvent.click(buttons[1]);
  await screen.findByText('Second bot');
  await act(async () => { finishA(preview); });
  expect(within(screen.getByRole('dialog')).queryByText('Company bot')).toBeNull();
  expect(within(screen.getByRole('dialog')).getByText('Second bot')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('userManagement.offboardingReason'), { target: { value: 'Employment ended' } });
  fireEvent.click(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }));
  await waitFor(() => expect(usersApi.offboard).toHaveBeenCalledWith('second', expect.objectContaining({
    successor_user_id: 'successor', expected_agent_ids: ['second-bot'],
  }), undefined));
});

it('G3f review 1: drops an older successor response and waits for the selected preview', async () => {
  const preview = { ...await defaultPreview!('leaver'), eligible_successors: [
    { id: 'successor', display_name: 'First' }, { id: 'second', display_name: 'Second' }, { id: 'third', display_name: 'Third' },
  ].map(user => ({ ...user, email: `${user.id}@review.test`, role: 'member' })) };
  let finishOld!: (value: any) => void;
  let finishNew!: (value: any) => void;
  vi.mocked(usersApi.previewOffboarding).mockResolvedValueOnce(preview)
    .mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }))
    .mockImplementationOnce(() => new Promise(resolve => { finishNew = resolve; }));
  render(<UserManagement />);
  fireEvent.click(await screen.findByRole('button', { name: 'userManagement.deactivateMember' }));
  await screen.findByText('Company guide');
  fireEvent.change(screen.getByLabelText('userManagement.offboardingReason'), { target: { value: 'Employment ended' } });
  fireEvent.change(screen.getByLabelText('userManagement.successorAdmin'), { target: { value: 'second' } });
  expect(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }).hasAttribute('disabled')).toBe(true);
  fireEvent.change(screen.getByLabelText('userManagement.successorAdmin'), { target: { value: 'third' } });
  await act(async () => { finishNew({ ...preview, successor_user_id: 'third', handover: { knowledge: [{ id: 'new', title: 'Newest copy' }], work: [], triggers: [] } }); });
  await act(async () => { finishOld({ ...preview, successor_user_id: 'second', handover: { knowledge: [{ id: 'old', title: 'Older copy' }], work: [], triggers: [] } }); });
  expect(screen.queryByText('Older copy')).toBeNull();
  expect(screen.getByText('Newest copy')).toBeTruthy();
  expect((screen.getByLabelText('userManagement.successorAdmin') as HTMLSelectElement).value).toBe('third');
  expect(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }).hasAttribute('disabled')).toBe(false);
});

it('G3f review 7: rechecks the same selection and enables submit after processing clears', async () => {
  const preview = await defaultPreview!('leaver');
  vi.mocked(usersApi.previewOffboarding).mockResolvedValueOnce({ ...preview, blockers: ['知识仍在处理中，请一两分钟后重试。'] })
    .mockResolvedValueOnce(preview);
  render(<UserManagement />);
  fireEvent.click(await screen.findByRole('button', { name: 'userManagement.deactivateMember' }));
  await screen.findByText('Company guide');
  fireEvent.change(screen.getByLabelText('userManagement.offboardingReason'), { target: { value: 'Employment ended' } });
  expect(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }).hasAttribute('disabled')).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'userManagement.recheckPreview' }));
  await waitFor(() => expect(usersApi.previewOffboarding).toHaveBeenLastCalledWith('leaver', undefined, 'successor'));
  await waitFor(() => expect(screen.getByRole('button', { name: 'userManagement.confirmDeactivate' }).hasAttribute('disabled')).toBe(false));
  expect(screen.queryByText('知识仍在处理中，请一两分钟后重试。')).toBeNull();
});
