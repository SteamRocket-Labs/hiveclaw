/** @vitest-environment jsdom */
import { expect, it } from 'vitest';
import { isClarificationCardAnsweredByLaterUserMessage } from './AgentChatSection';

it('does not treat ordinary later messages or timestamps as a question answer', () => {
  const question = { role: 'tool_call' as const, content: '', timestamp: '2026-10-16T00:00:00Z',
    toolMeta: { kind: 'user_clarification' as const, questions: [], blocking: true, nextAction: null } };
  const update = { role: 'user' as const, content: 'Keep working on the other task', timestamp: '2026-10-16T00:01:00Z' };
  expect(isClarificationCardAnsweredByLaterUserMessage([question, update], 0)).toBe(false);
  expect(isClarificationCardAnsweredByLaterUserMessage([update, question], 1)).toBe(false);
});
