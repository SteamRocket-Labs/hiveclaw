/** @vitest-environment jsdom */
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { StructuredToolResultBody } from './StructuredToolResult';
import { normalizeToolCallResult } from './toolResultEnvelope';

vi.mock('react-i18next', () => {
  const t = (key: string, fallback: unknown) => typeof fallback === 'string' ? fallback : key;
  return { useTranslation: () => ({ t }) };
});
const api = vi.hoisted(() => ({ getSessionChannelConfiguration: vi.fn(), confirmSessionChannelConfiguration: vi.fn() }));
vi.mock('../../api/domains/tools', () => ({ toolsApi: api }));
vi.mock('../../components/ChannelConfig', () => ({ default: ({ agentId, channelType, onConfigured }: {
  agentId: string; channelType: string; onConfigured: () => void;
}) => <button onClick={onConfigured}>Secure editor for {agentId} / {channelType}</button> }));
const current = { id: 'configuration', updated_at: '2026-10-16T00:00:00+00:00', channel_type: 'feishu', scope: 'employee' };
const waiting = { state: 'waiting', target_agent_id: 'colleague', target_agent_name: 'Researcher', configuration: current };
const meta = normalizeToolCallResult('ask_user_question', { status: 'awaiting_user_clarification',
  questions: [{ question: 'Connect the colleague?', options: [] }],
  configuration_request: { target_agent_id: 'colleague', channel_type: 'feishu' } }).toolMeta;
const props = { agentId: 'source', sessionId: 'work', questionItemId: 'question', toolMeta: meta };
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('opens the existing target editor, confirms actual readback and retries continuation without saving credentials again', async () => {
  api.getSessionChannelConfiguration.mockResolvedValueOnce({ ...waiting, configuration: null }).mockResolvedValue(waiting);
  api.confirmSessionChannelConfiguration.mockResolvedValue({ ...waiting, state: 'ready', receipt_ref: 'receipt' });
  const answer = vi.fn().mockRejectedValueOnce(new Error('Continuation unavailable')).mockResolvedValue({});
  render(<StructuredToolResultBody {...props} onAnswerQuestion={answer} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Secure editor for colleague / feishu' }));
  await waitFor(() => expect(api.getSessionChannelConfiguration).toHaveBeenCalledTimes(2));
  fireEvent.click(await screen.findByRole('button', { name: 'Use this configuration and continue' }));
  await screen.findByText('Continuation unavailable');
  expect(api.confirmSessionChannelConfiguration).toHaveBeenCalledWith('source', 'work', 'question', { config_id: current.id, updated_at: current.updated_at });
  expect(answer).toHaveBeenCalledWith('question', 'Continue with the configured channel.');
  expect(screen.queryByText('Secure editor for colleague / feishu')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Continue original work' }));
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
  expect(api.confirmSessionChannelConfiguration).toHaveBeenCalledTimes(1);
});

it('restores confirmed company configuration without mounting a credential editor', async () => {
  api.getSessionChannelConfiguration.mockResolvedValue({ ...waiting, state: 'ready', receipt_ref: 'receipt', configuration: { ...current, scope: 'company' } });
  const answer = vi.fn().mockResolvedValue({});
  render(<StructuredToolResultBody {...props} onAnswerQuestion={answer} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Continue original work' }));
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(1));
  expect(api.confirmSessionChannelConfiguration).not.toHaveBeenCalled();
});

it('shows permission errors with review and an authorized alternative without mounting another employee settings form', async () => {
  api.getSessionChannelConfiguration.mockRejectedValue(new Error('Manage access required'));
  const answer = vi.fn();
  render(<StructuredToolResultBody {...props} onAnswerQuestion={answer} />);
  await screen.findByText('Manage access required');
  expect(screen.queryByText('Secure editor for colleague / feishu')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Continue without configuration' }));
  await waitFor(() => expect(answer).toHaveBeenCalledWith('question', 'Continue without changing configuration.'));
});
