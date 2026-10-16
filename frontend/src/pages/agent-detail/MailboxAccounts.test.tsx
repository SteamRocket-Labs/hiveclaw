/** @vitest-environment jsdom */
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import MailboxAccounts from './MailboxAccounts';
import InlineToolConfigurationCard from './InlineToolConfigurationCard';

vi.mock('react-i18next', () => {
  const t = (key: string, value: unknown) => typeof value === 'string' ? value : key;
  return { useTranslation: () => ({ t }) };
});
vi.mock('../../stores', () => {
  const state = { user: { id: 'requester', role: 'member' } };
  return { useAuthStore: Object.assign((selector: (value: typeof state) => unknown) => selector(state), { getState: () => state }) };
});
const api = vi.hoisted(() => ({ listMailboxes: vi.fn(), createMailbox: vi.fn(), getMailbox: vi.fn(), saveMailbox: vi.fn(),
  setMailboxGrant: vi.fn(), revokeMailbox: vi.fn(), getSessionToolConfiguration: vi.fn(), confirmSessionMailbox: vi.fn(), saveSessionToolConfiguration: vi.fn() }));
vi.mock('../../api/domains/tools', () => ({ toolsApi: api }));
const company = { id: null, owner_user_id: null, name: 'Company mailbox', is_configured: true, account_use_enabled: true, revision: 'company-revision' };
const personal = { id: 'personal', owner_user_id: 'requester', name: 'My mailbox', is_configured: true, account_use_enabled: true, version: 2, grant_version: 1 };
const tool = { id: 'email', name: 'send_email', display_name: 'Email', agent_config: {}, global_config: {},
  config_schema: { fields: [{ key: 'email_address', label: 'Email address' }, { key: 'auth_code', label: 'App password', type: 'password' }] } };
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it('saves an owned mailbox without confirming or continuing and retains a failed credential draft', async () => {
  api.listMailboxes.mockResolvedValue([company, personal]);
  api.getMailbox.mockResolvedValue({ ...personal, config: { email_address: 'private@fixture.test', auth_code: '__HIVE_SECRET_SET__' } });
  let failSave: (error: Error) => void;
  api.saveMailbox.mockReturnValueOnce(new Promise((_resolve, reject) => { failSave = reject; })).mockResolvedValue({});
  const confirm = vi.fn(), saveCompany = vi.fn();
  render(<MailboxAccounts agentId="employee" tool={tool as any} canManageCompany={false} onSaveCompany={saveCompany} onConfirm={confirm} />);
  await screen.findByRole('option', { name: /My mailbox/ });
  fireEvent.change(screen.getByLabelText('agent.mcpAccounts.choose'), { target: { value: 'personal' } });
  fireEvent.click(await screen.findByRole('button', { name: 'common.edit' }));
  fireEvent.change(await screen.findByLabelText('App password'), { target: { value: 'synthetic-new-password' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect((screen.getByLabelText('App password') as HTMLInputElement).disabled).toBe(true));
  failSave!(new Error('Save unavailable'));
  await screen.findByText('Save unavailable');
  expect((screen.getByLabelText('App password') as HTMLInputElement).value).toBe('synthetic-new-password');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(api.saveMailbox).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByLabelText('App password')).toBeNull());
  expect(api.saveMailbox).toHaveBeenLastCalledWith('personal', 2, { email_address: 'private@fixture.test', auth_code: 'synthetic-new-password' }, 'employee');
  expect(confirm).not.toHaveBeenCalled(); expect(saveCompany).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'agent.mailboxAccounts.confirm' }));
  await waitFor(() => expect(confirm).toHaveBeenCalledWith(personal));
});

it('retries only the exact answer after confirmed mailbox selection and failed continuation', async () => {
  const response = { state: 'waiting', target_agent_id: 'employee', target_agent_name: 'Analyst', tool, can_manage_company_mailbox: false };
  api.getSessionToolConfiguration.mockResolvedValue(response);
  api.listMailboxes.mockResolvedValue([company, personal]);
  api.confirmSessionMailbox.mockResolvedValue({ ...response, state: 'saved', receipt_ref: 'mailbox-selection:receipt' });
  const answer = vi.fn().mockRejectedValueOnce(new Error('Continuation unavailable')).mockResolvedValue({});
  render(<InlineToolConfigurationCard agentId="source" sessionId="work" questionId="question" question="Choose mailbox" onAnswer={answer} />);
  await screen.findByText('Analyst');
  await waitFor(() => expect(screen.getByRole('button', { name: 'agent.mailboxAccounts.confirm' }).hasAttribute('disabled')).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'agent.mailboxAccounts.confirm' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Continue original work' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Continue original work' }));
  await screen.findByText('Continuation unavailable');
  fireEvent.click(screen.getByRole('button', { name: 'Continue original work' }));
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
  expect(answer).toHaveBeenLastCalledWith('question', 'Continue after saving configuration.');
  expect(api.confirmSessionMailbox).toHaveBeenCalledTimes(1);
  expect(api.saveMailbox).not.toHaveBeenCalled(); expect(api.saveSessionToolConfiguration).not.toHaveBeenCalled();
});

it('restores the saved personal selection and requires review after choosing a different mailbox', async () => {
  const response = { state: 'saved', target_agent_id: 'employee', target_agent_name: 'Analyst', tool,
    mailbox_selection: { account_id: 'personal' }, can_manage_company_mailbox: false };
  api.getSessionToolConfiguration.mockResolvedValue(response);
  api.listMailboxes.mockResolvedValue([company, personal]);
  api.confirmSessionMailbox.mockResolvedValue({ ...response, mailbox_selection: { account_id: null } });
  render(<InlineToolConfigurationCard agentId="source" sessionId="work" questionId="question" question="Choose mailbox" onAnswer={vi.fn()} />);
  await screen.findByRole('option', { name: /My mailbox/ });
  const selection = screen.getByLabelText('agent.mcpAccounts.choose') as HTMLSelectElement;
  expect(selection.value).toBe('personal');
  fireEvent.change(selection, { target: { value: 'company' } });
  expect((screen.getByRole('button', { name: 'Continue original work' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'agent.mailboxAccounts.confirm' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Continue original work' }) as HTMLButtonElement).disabled).toBe(false));
});

it('deactivates only the reviewed mailbox version', async () => {
  api.listMailboxes.mockResolvedValue([company, personal]);
  api.revokeMailbox.mockResolvedValue({ ...personal, is_configured: false, version: 3 });
  render(<MailboxAccounts agentId="employee" tool={tool as any} initialAccountId="personal" canManageCompany={false} onSaveCompany={vi.fn()} />);
  await screen.findByRole('option', { name: /My mailbox/ });
  fireEvent.click(screen.getByRole('button', { name: 'agent.mcpAccounts.revoke' }));
  fireEvent.click(screen.getByRole('button', { name: 'agent.mcpAccounts.confirmRevoke' }));
  await waitFor(() => expect(api.revokeMailbox).toHaveBeenCalledWith('personal', 2));
});
