// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';
import InlineMcpConfigurationCard from './InlineMcpConfigurationCard';
import { StructuredToolResultBody } from './StructuredToolResult';
import { normalizeToolCallResult } from './toolResultEnvelope';
const mocks = vi.hoisted(() => ({ get: vi.fn(), confirm: vi.fn(), grant: vi.fn(), create: vi.fn(), oauth: vi.fn() }));
const translate = (key: string, fallback?: string) => translateFromCatalog(en, key, fallback);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
vi.mock('../../api/domains/tools', () => ({ toolsApi: { getSessionMcpConfiguration: mocks.get, confirmSessionMcpConfiguration: mocks.confirm } }));
vi.mock('../../api/domains/extensions', () => ({ extensionsApi: { setAgentMcpAssignment: mocks.grant, createMcpAccount: mocks.create, getMcpOAuthStatus: mocks.oauth } }));
const configured = { state: 'waiting', target_agent_id: 'target', target_agent_name: 'Research colleague', server: { id: 'server', name: 'Documents' },
  authorization: 'configured', usage: { enabled: true, default_tool_mode: 'auto', always_load: false }, revision: 'revision-1', usable_tool_count: 1, can_authorize: false };
const props = { agentId: 'source', sessionId: 'work', questionId: 'question', question: 'Connect documents to find the report?' };
afterEach(cleanup);
beforeEach(() => { vi.clearAllMocks(); mocks.get.mockResolvedValue(configured); mocks.confirm.mockResolvedValue({ ...configured, state: 'ready', receipt_ref: 'confirmed' }); });
it('automatically continues the original work and retries delivery without repeating confirmation', async () => {
  // S1/Grok 1: recheck fresh server state after a refused or uncertain answer.
  mocks.get.mockImplementation(async () => ({ ...configured, state: 'ready' })).mockResolvedValueOnce(configured);
  const answer = vi.fn().mockRejectedValueOnce(new Error('internal failure')).mockResolvedValueOnce({});
  render(<InlineMcpConfigurationCard {...props} onAnswer={answer} />);
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
  expect(mocks.confirm).toHaveBeenCalledExactlyOnceWith('source', 'work', 'question', { revision: 'revision-1' });
  expect(screen.queryByText('internal failure')).toBeNull();
  const recheck = await screen.findByRole('button', { name: 'I have connected' });
  await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
  await waitFor(() => expect((recheck as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(recheck);
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
  expect(answer).toHaveBeenLastCalledWith('question', 'Continue with the authorized MCP connection.');
  expect(mocks.confirm).toHaveBeenCalledTimes(1); expect(mocks.grant).not.toHaveBeenCalled();
});
it('preserves explicit employee denial without granting use or resuming', async () => {
  mocks.get.mockResolvedValue({ ...configured, usage: { ...configured.usage, enabled: false }, usable_tool_count: 0, permitted_tool_count: 0 });
  const answer = vi.fn(); render(<InlineMcpConfigurationCard {...props} onAnswer={answer} />);
  await screen.findByRole('alert');
  expect(mocks.grant).not.toHaveBeenCalled(); expect(mocks.confirm).not.toHaveBeenCalled(); expect(answer).not.toHaveBeenCalled();
});
it('collapses a confirmed connection to one translated line after history reload', () => {
  render(<InlineMcpConfigurationCard {...props} answered onAnswer={vi.fn()} />);
  expect(screen.getByText('Continued your work')).toBeTruthy();
  expect(screen.queryByRole('button')).toBeNull();
  expect(mocks.get).not.toHaveBeenCalled();
});
it('continues a recorded request after reload without confirming again and never writes from read-only history', async () => {
  mocks.get.mockResolvedValue({ ...configured, state: 'ready', receipt_ref: 'confirmed' });
  const answer = vi.fn().mockResolvedValue({}); const view = render(<InlineMcpConfigurationCard {...props} onAnswer={answer} />);
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(1)); expect(mocks.confirm).not.toHaveBeenCalled();
  view.unmount(); mocks.get.mockClear(); render(<InlineMcpConfigurationCard {...props} />);
  expect(mocks.get).not.toHaveBeenCalled(); expect(screen.queryByRole('button')).toBeNull();
  expect(screen.queryByText('Continued your work')).toBeNull();
});
it('routes the native question to its exact session and item without sending a new turn', async () => {
  const value = normalizeToolCallResult('ask_user_question', { status: 'awaiting_user_clarification', questions: [{ question: props.question }],
    configuration_request: { target_agent_id: 'target', mcp_server_id: 'server' } });
  expect(value.toolMeta).toMatchObject({ kind: 'user_clarification', configurationRequest: { targetAgentId: 'target', mcpServerId: 'server' } });
  const answer = vi.fn().mockResolvedValue({}); const send = vi.fn();
  const view = render(<StructuredToolResultBody agentId="source" sessionId="work" questionItemId="question" toolMeta={value.toolMeta} onAnswerQuestion={answer} onSendMessage={send} />);
  await waitFor(() => expect(answer).toHaveBeenCalledExactlyOnceWith('question', 'Continue with the authorized MCP connection.'));
  expect(mocks.get).toHaveBeenCalledWith('source', 'work', 'question'); expect(send).not.toHaveBeenCalled();
  view.unmount(); mocks.get.mockClear(); render(<StructuredToolResultBody agentId="source" sessionId="work" questionItemId="question" toolMeta={value.toolMeta} onSendMessage={send} />);
  expect(screen.queryByRole('button')).toBeNull(); expect(mocks.get).not.toHaveBeenCalled();
});
it('uses the returned requester account without per-employee grants or another selection', async () => {
  const personal = { id: 'personal', name: 'My documents', owner_user_id: 'requester', parent_server_id: 'server', auth_status: 'configured' };
  const chosen = { ...configured, account: personal, revision: 'personal-revision' };
  mocks.get.mockResolvedValue(chosen); mocks.confirm.mockResolvedValue({ ...chosen, state: 'ready' });
  const answer = vi.fn().mockResolvedValue({}); render(<InlineMcpConfigurationCard {...props} onAnswer={answer} />);
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
  expect(mocks.confirm).toHaveBeenCalledExactlyOnceWith('source', 'work', 'question', { revision: 'personal-revision', account_id: 'personal' });
  expect(mocks.grant).not.toHaveBeenCalled(); expect(screen.queryByRole('combobox')).toBeNull();
});
it('creates the personal account in place without exposing registration fields or URLs', async () => {
  const personal = { id: 'personal', name: 'Documents', owner_user_id: 'requester', parent_server_id: 'server', auth_status: 'none' };
  const missing = { ...configured, authorization: 'required' };
  // R3 item 5: a fresh snapshot picks the created row without a selection request.
  mocks.get.mockResolvedValueOnce(missing).mockResolvedValue({ ...missing, account: personal });
  mocks.create.mockResolvedValue(personal);
  mocks.oauth.mockResolvedValue({ auth_status: 'none', registration: { client_id: 'internal-client', token_endpoint: 'https://auth.test/token' } });
  render(<InlineMcpConfigurationCard {...props} onAnswer={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Connect my account' }));
  await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(mocks.oauth).toHaveBeenCalledWith('personal', true));
  expect(mocks.create).toHaveBeenCalledWith('target', 'server', { id: expect.any(String), name: 'Documents' });
  expect(screen.queryByRole('textbox')).toBeNull(); expect(screen.queryByText('internal-client')).toBeNull(); expect(screen.queryByText('https://auth.test/token')).toBeNull();
});

it.each(['visibilitychange', 'button', 'focus'])('rechecks the connection on %s and continues after a static OAuth return', async (event) => {
  // S1: the callback has no inline script. Returning or the plain button drives readback.
  mocks.get.mockResolvedValueOnce({ ...configured, authorization: 'required' }).mockResolvedValue(configured);
  const answer = vi.fn().mockResolvedValue({});
  render(<InlineMcpConfigurationCard {...props} onAnswer={answer} />);
  const button = await screen.findByRole('button', { name: 'I have connected' });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  expect(answer).not.toHaveBeenCalled();
  if (event === 'button') fireEvent.click(button);
  else if (event === 'focus') fireEvent.focus(window);
  else fireEvent(document, new Event('visibilitychange'));
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
  expect(mocks.get).toHaveBeenCalledTimes(2);
});

it('refetches a refused answer and collapses when another tab has already continued', async () => {
  // Grok 1: a losing tab must not offer a continuation that can never succeed.
  mocks.get.mockResolvedValueOnce(configured).mockResolvedValue({ ...configured, state: 'ready', answered: true });
  const answer = vi.fn().mockRejectedValue(new Error('user_question_not_waiting'));
  render(<InlineMcpConfigurationCard {...props} onAnswer={answer} />);
  await screen.findByText('Continued your work');
  expect(mocks.get).toHaveBeenCalledTimes(2);
  expect(answer).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button')).toBeNull();
});

it('shows the connection explanation without raw model question text', async () => {
  mocks.get.mockResolvedValue({ ...configured, authorization: 'required' });
  const value = normalizeToolCallResult('ask_user_question', { status: 'awaiting_user_clarification',
    questions: [{ question: 'Call list_mcp_tools then grant_id=private' }],
    configuration_request: { target_agent_id: 'target', mcp_server_id: 'server' } });
  render(<StructuredToolResultBody agentId="source" sessionId="work" questionItemId="question"
    toolMeta={value.toolMeta} onAnswerQuestion={vi.fn()} />);
  await screen.findByRole('button', { name: 'I have connected' });
  expect(document.body.textContent).not.toContain('list_mcp_tools');
  expect(document.body.textContent).not.toContain('grant_id');
});
