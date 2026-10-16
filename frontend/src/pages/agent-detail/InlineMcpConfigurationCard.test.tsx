// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';
import InlineMcpConfigurationCard from './InlineMcpConfigurationCard';
import { StructuredToolResultBody } from './StructuredToolResult';
import { normalizeToolCallResult } from './toolResultEnvelope';

const mocks = vi.hoisted(() => ({ get: vi.fn(), confirm: vi.fn(), grant: vi.fn() }));
const translate = (key: string, fallback?: string) => translateFromCatalog(en, key, fallback);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
vi.mock('../../api/domains/tools', () => ({ toolsApi: { getSessionMcpConfiguration: mocks.get, confirmSessionMcpConfiguration: mocks.confirm } }));
vi.mock('../../api/domains/extensions', () => ({ extensionsApi: { setAgentMcpAssignment: mocks.grant } }));
const configured = { state: 'waiting', target_agent_id: 'target', target_agent_name: 'Research colleague', server: { id: 'server', name: 'Documents' },
  authorization: 'configured', usage: { enabled: true, default_tool_mode: 'auto', always_load: false }, revision: 'revision-1', usable_tool_count: 1, can_authorize: false };
const props = { agentId: 'source', sessionId: 'work', questionId: 'question', question: 'Authorize documents for this work?' };
afterEach(cleanup);
beforeEach(() => { vi.clearAllMocks(); mocks.get.mockResolvedValue(configured); mocks.confirm.mockResolvedValue({ ...configured, state: 'ready', receipt_ref: 'confirmed' }); });

it('retries exact original-work continuation without repeating confirmation or granting use', async () => {
  const answer = vi.fn().mockRejectedValueOnce(new Error('Continuation unavailable')).mockResolvedValueOnce({});
  render(<InlineMcpConfigurationCard {...props} onAnswer={answer} />);
  const button = await screen.findByRole('button', { name: 'Continue original work' });
  fireEvent.click(button);
  await screen.findByText('Continuation unavailable');
  expect(mocks.confirm).toHaveBeenCalledExactlyOnceWith('source', 'work', 'question', { revision: 'revision-1' });
  fireEvent.click(screen.getByRole('button', { name: 'Continue original work' }));
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
  expect(answer).toHaveBeenLastCalledWith('question', 'Continue with the authorized MCP connection.');
  expect(mocks.confirm).toHaveBeenCalledTimes(1);
  expect(mocks.grant).not.toHaveBeenCalled();
});

it('requires an explicit employee-use grant and review; company login never resumes work', async () => {
  mocks.get.mockResolvedValueOnce({ ...configured, usage: { ...configured.usage, enabled: false }, usable_tool_count: 0 })
    .mockResolvedValue(configured);
  mocks.grant.mockResolvedValue({});
  const answer = vi.fn();
  render(<InlineMcpConfigurationCard {...props} onAnswer={answer} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Grant use to this employee' }));
  await waitFor(() => expect(mocks.grant).toHaveBeenCalledExactlyOnceWith('target', 'server', { enabled: true, default_tool_mode: 'auto', always_load: false }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Continue original work' }) as HTMLButtonElement).disabled).toBe(false));
  expect(answer).not.toHaveBeenCalled();
  expect(mocks.confirm).not.toHaveBeenCalled();
});

it('can continue a recorded request after reload without confirming again and never writes from a read-only view', async () => {
  mocks.get.mockResolvedValue({ ...configured, state: 'ready', receipt_ref: 'confirmed' });
  const answer = vi.fn().mockResolvedValue({});
  const view = render(<InlineMcpConfigurationCard {...props} onAnswer={answer} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Continue original work' }));
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
  expect(mocks.confirm).not.toHaveBeenCalled();
  view.unmount(); mocks.get.mockClear();
  render(<InlineMcpConfigurationCard {...props} />);
  expect(mocks.get).not.toHaveBeenCalled();
  expect(screen.queryByRole('button')).toBeNull();
});

it('routes the native MCP question through its exact source session and item, including read-only history', async () => {
  const value = normalizeToolCallResult('ask_user_question', { status: 'awaiting_user_clarification',
    questions: [{ question: props.question }], configuration_request: { target_agent_id: 'target', mcp_server_id: 'server' } });
  expect(value.toolMeta).toMatchObject({ kind: 'user_clarification', configurationRequest: { targetAgentId: 'target', mcpServerId: 'server' } });
  const answer = vi.fn().mockResolvedValue({}); const send = vi.fn();
  const view = render(<StructuredToolResultBody agentId="source" sessionId="work" questionItemId="question"
    toolMeta={value.toolMeta} onAnswerQuestion={answer} onSendMessage={send} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Continue original work' }));
  await waitFor(() => expect(answer).toHaveBeenCalledExactlyOnceWith('question', 'Continue with the authorized MCP connection.'));
  expect(mocks.get).toHaveBeenCalledWith('source', 'work', 'question'); expect(send).not.toHaveBeenCalled();
  view.unmount(); mocks.get.mockClear();
  render(<StructuredToolResultBody agentId="source" sessionId="work" questionItemId="question" toolMeta={value.toolMeta} onSendMessage={send} />);
  expect(screen.queryByRole('button')).toBeNull(); expect(mocks.get).not.toHaveBeenCalled();
});
