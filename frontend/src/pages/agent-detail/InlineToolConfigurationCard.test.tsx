/** @vitest-environment jsdom */
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { StructuredToolResultBody } from './StructuredToolResult';
import { normalizeToolCallResult } from './toolResultEnvelope';

vi.mock('react-i18next', () => {
  const t = (key: string, value: unknown) => typeof value === 'string' ? value : key;
  return { useTranslation: () => ({ t }) };
});
const api = vi.hoisted(() => ({ getSessionToolConfiguration: vi.fn(), saveSessionToolConfiguration: vi.fn() }));
vi.mock('../../api/domains/tools', () => ({ toolsApi: api }));
const tool = { id: 'tool', display_name: 'Search account', config: {}, global_config: { endpoint: 'company' },
  agent_config: {}, config_schema: { fields: [{ key: 'api_key', label: 'Key', type: 'password' }] } };
const response = { state: 'waiting', target_agent_id: 'created-employee', target_agent_name: 'Researcher', tool };
const meta = normalizeToolCallResult('ask_user_question', { status: 'awaiting_user_clarification',
  questions: [{ question: 'Connect the search account?', options: [] }],
  configuration_request: { target_agent_id: 'created-employee', tool_name: 'search' } }).toolMeta;
const props = { agentId: 'source', sessionId: 'work', questionItemId: 'question', toolMeta: meta };
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('sends credentials only to settings, then retries continuation without repeating a successful save', async () => {
  api.getSessionToolConfiguration.mockResolvedValue(response);
  api.saveSessionToolConfiguration.mockResolvedValue({ ...response, state: 'saved', receipt_ref: 'receipt' });
  const answer = vi.fn().mockRejectedValueOnce(new Error('Continuation unavailable')).mockResolvedValue({});
  render(<StructuredToolResultBody {...props} onAnswerQuestion={answer} />);
  expect(await screen.findByText('Researcher')).toBeTruthy();
  fireEvent.change(await screen.findByLabelText('Key'), { target: { value: 'synthetic-form-secret' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save and continue' }));
  await screen.findByText('Continuation unavailable');
  expect(api.saveSessionToolConfiguration).toHaveBeenCalledWith('source', 'work', 'question', { api_key: 'synthetic-form-secret' });
  expect(answer).toHaveBeenCalledWith('question', 'Continue after saving configuration.');
  expect(screen.queryByLabelText('Key')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Continue original work' }));
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
  expect(api.saveSessionToolConfiguration).toHaveBeenCalledTimes(1);
});

it('restores a saved request after reload and only resumes the original question', async () => {
  api.getSessionToolConfiguration.mockResolvedValue({ ...response, state: 'saved', receipt_ref: 'receipt' });
  const answer = vi.fn().mockResolvedValue({});
  render(<StructuredToolResultBody {...props} onAnswerQuestion={answer} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Continue original work' }));
  await waitFor(() => expect(answer).toHaveBeenCalledWith('question', 'Continue after saving configuration.'));
  expect(api.saveSessionToolConfiguration).not.toHaveBeenCalled();
  expect(screen.queryByLabelText('Key')).toBeNull();
});

it('shows denied target access with retry and never offers an ordinary chat credential prompt', async () => {
  api.getSessionToolConfiguration.mockRejectedValue(new Error('Manage access required'));
  const answer = vi.fn(), send = vi.fn();
  render(<StructuredToolResultBody {...props} onAnswerQuestion={answer} onSendMessage={send} />);
  await screen.findByText('Manage access required');
  expect(screen.queryByLabelText('Key')).toBeNull();
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Continue without configuration' }));
  await waitFor(() => expect(answer).toHaveBeenCalledWith('question', 'Continue without changing configuration.'));
  expect(send).not.toHaveBeenCalled();
});
