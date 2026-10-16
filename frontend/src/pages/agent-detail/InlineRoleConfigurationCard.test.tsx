// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { StructuredToolResultBody } from './StructuredToolResult';
import { normalizeToolCallResult } from './toolResultEnvelope';

const translate = (key: string, fallback?: string) => translateFromCatalog(en, key, fallback);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const response = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
const initial = { target_agent_id: 'new-employee', target_agent_name: 'Research employee', state: 'waiting',
  role_description: 'Original responsibilities', role_revision: 'a'.repeat(64) };
const updated = { ...initial, state: 'ready', role_description: 'Review original evidence.', role_revision: 'b'.repeat(64), receipt_ref: 'recorded-review' };
const normalized = normalizeToolCallResult('ask_user_question', { status: 'awaiting_user_clarification',
  request_item_id: 'question', questions: [{ question: 'Review employee responsibilities?', options: [] }],
  configuration_request: { target_agent_id: 'new-employee', employee_setting: 'role' } });

function card(onAnswer?: (id: string, text: string) => Promise<unknown>) {
  return <QueryClientProvider client={new QueryClient()}><StructuredToolResultBody agentId="source" sessionId="work"
    questionItemId="question" toolName="ask_user_question" toolMeta={normalized.toolMeta} toolRawResult={normalized.raw}
    onAnswerQuestion={onAnswer} /></QueryClientProvider>;
}

it('saves through canonical profile settings, confirms the exact original question and retries only continuation', async () => {
  const writes: Array<{ url: string; method: string; body: unknown }> = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    if (init?.body) {
      writes.push({ url: String(url), method: init.method || '', body: JSON.parse(String(init.body)) });
      return response(updated);
    }
    return response(initial);
  });
  const answer = vi.fn().mockRejectedValueOnce(new Error('Continuation unavailable')).mockResolvedValueOnce(undefined);
  render(card(answer));
  expect(normalized.toolMeta?.kind).toBe('user_clarification');
  const input = await screen.findByRole('textbox', { name: 'Employee responsibilities' });
  fireEvent.change(input, { target: { value: updated.role_description } });
  fireEvent.click(screen.getByRole('button', { name: 'Save and review responsibilities' }));
  await screen.findByText('Current responsibilities reviewed. Continue the original work when ready.');
  expect(answer).not.toHaveBeenCalled();
  expect(writes).toEqual([
    { url: '/api/agents/new-employee', method: 'PATCH', body: { role_description: updated.role_description, expected_role_revision: initial.role_revision } },
    { url: '/api/tools/agents/source/sessions/work/questions/question/role/confirm', method: 'POST', body: { role_revision: updated.role_revision } },
  ]);
  fireEvent.click(screen.getByRole('button', { name: 'Continue original work' }));
  await screen.findByText('Continuation unavailable');
  fireEvent.click(screen.getByRole('button', { name: 'Continue original work' }));
  await waitFor(() => expect(answer).toHaveBeenCalledTimes(2));
  expect(answer).toHaveBeenLastCalledWith('question', 'Continue with the reviewed responsibilities.');
  expect(writes).toHaveLength(2);
});

it('keeps historical role questions read-only without loading employee settings', () => {
  const fetch = vi.spyOn(globalThis, 'fetch');
  render(card());
  expect(screen.queryByRole('textbox')).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
});
