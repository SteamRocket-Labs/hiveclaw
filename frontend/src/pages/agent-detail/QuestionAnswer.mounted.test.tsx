/** @vitest-environment jsdom */
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { StructuredToolResultBody } from './StructuredToolResult';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
afterEach(cleanup);

it('submits the exact question and retains the answer on a failed response', async () => {
  const answer = vi.fn().mockRejectedValueOnce(new Error('Connection lost')).mockResolvedValue({ status: 'queued' });
  const ordinaryMessage = vi.fn();
  render(<StructuredToolResultBody toolName="ask_user_question" questionItemId="actual-question"
    toolMeta={{ kind: 'user_clarification', blocking: true, nextAction: null,
      questions: [{ question: 'Which audience?', header: 'Audience', options: [{ label: 'Team', description: '' }], multiSelect: false }] }}
    onAnswerQuestion={answer} onSendMessage={ordinaryMessage} />);
  fireEvent.click(screen.getByRole('radio', { name: 'Team' }));
  fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }));
  expect((await screen.findByRole('alert')).textContent).toBe('Connection lost');
  expect((screen.getByRole('radio', { name: 'Team' }) as HTMLInputElement).checked).toBe(true);
  expect(screen.queryByText('Your answer was sent.')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }));
  await screen.findByText('Your answer was sent.');
  expect(answer).toHaveBeenNthCalledWith(1, 'actual-question', 'Audience: Team');
  expect(answer).toHaveBeenNthCalledWith(2, 'actual-question', 'Audience: Team');
  await waitFor(() => expect(ordinaryMessage).not.toHaveBeenCalled());
});
