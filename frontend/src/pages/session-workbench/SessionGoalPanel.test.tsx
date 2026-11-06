import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string, values?: Record<string, unknown>) => {
      if (!fallback) return _key;
      return Object.entries(values || {}).reduce(
        (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
        fallback,
      );
    },
  }),
}));

import { SessionGoalPanel, goalProgressFacts } from './SessionGoalPanel';
import { normalizeStoredChatMessage } from '../agent-detail/chatRuntime';

const goal = {
  id: 'goal-1',
  agent_id: 'agent-1',
  session_id: 'session-1',
  objective: 'Deliver the final atomic report',
  status: 'active',
  token_budget: 1000,
  tokens_used: 250,
  remaining_tokens: 750,
  time_budget_seconds: 3600,
  time_used_seconds: 120,
  remaining_time_seconds: 3480,
  max_continuation_turns: 5,
  continuation_count: 2,
  remaining_continuation_turns: 3,
  blocked_count: 0,
  blocked_reason: null,
  completion_summary: null,
  controls: { can_pause: true, can_resume: false, can_stop: true },
  created_at: null,
  updated_at: null,
  completed_at: null,
};

describe('SessionGoalPanel', () => {
  it.each(['started', 'updated'])('uses the server goal %s event for its objective mark and notice', action => {
    const objective = 'Deliver [REDACTED_SECRET] safely';
    const markup = renderToStaticMarkup(<SessionGoalPanel agentId="agent-1" sessionId="session-1"
      goals={[{ ...goal, objective }]} messages={[normalizeStoredChatMessage({ role: 'system', event_type: 'goal',
        content: objective, metadata: { command: action === 'started' ? 'goal_start' : 'goal_update', goal_id: goal.id,
          pasted_credential_notice: true } })]} />);
    expect(markup).toContain('Hidden key');
    expect(markup).toContain('The key you pasted was hidden. It will not be saved or sent to the model.');
    expect(markup).not.toContain('[REDACTED_SECRET]');
    expect(objective).toBe('Deliver [REDACTED_SECRET] safely');
  });

  it('marks an unflagged goal by text without reusing another goal notice', () => {
    const objective = 'Literal [REDACTED_SECRET]';
    const markup = renderToStaticMarkup(<SessionGoalPanel agentId="agent-1" sessionId="session-1"
      goals={[{ ...goal, objective }]} messages={[{ role: 'event', eventType: 'goal', eventGoalId: 'other-goal',
        content: objective, pastedCredentialNotice: true }]} />);
    expect(markup).toContain('Hidden key');
    expect(markup).not.toContain('[REDACTED_SECRET]');
    expect(markup).not.toContain('pasted-credential-notice');
  });

  it('clears the notice when a newer objective update has no flag', () => {
    const objective = 'A new ordinary objective';
    const markup = renderToStaticMarkup(<SessionGoalPanel agentId="agent-1" sessionId="session-1"
      goals={[{ ...goal, objective }]} messages={[
        { role: 'event', eventType: 'goal', eventGoalId: goal.id, eventAction: 'started', content: 'Old [REDACTED_SECRET]', pastedCredentialNotice: true },
        { role: 'event', eventType: 'goal', eventGoalId: goal.id, eventAction: 'updated', content: objective },
      ]} />);
    expect(markup).toContain(objective);
    expect(markup).not.toContain('pasted-credential-notice');
  });
  it('keeps the objective mark after an update without a new pasted key, dropping only the sentence', () => {
    const objective = 'Deliver [REDACTED_SECRET] safely';
    const messages = [
      { role: 'event' as const, eventType: 'goal', eventGoalId: goal.id, eventAction: 'started', content: objective, pastedCredentialNotice: true },
      { role: 'event' as const, eventType: 'goal', eventGoalId: goal.id, eventAction: 'updated', content: 'Budget updated' },
    ];
    const markup = renderToStaticMarkup(<SessionGoalPanel agentId="agent-1" sessionId="session-1"
      goals={[{ ...goal, objective }]} messages={messages} />);
    expect(markup).toContain('hidden-credential-mark');
    expect(markup).toContain('Hidden key');
    expect(markup).not.toContain('[REDACTED_SECRET]');
    expect(markup).not.toContain('pasted-credential-notice');
  });
  it('projects only user-meaningful progress and semantic controls', () => {
    const markup = renderToStaticMarkup(
      <SessionGoalPanel agentId="agent-1" sessionId="session-1" goals={[goal]} />,
    );

    expect(markup).toContain('Deliver the final atomic report');
    expect(markup).toContain('750 tokens left');
    expect(markup).toContain('3 turns left');
    expect(markup).toContain('58m left');
    expect(markup).toContain('Pause');
    expect(markup).toContain('Stop');
    expect(markup).not.toContain('goal-1');
    expect(markup).not.toContain('agent-1');
  });

  it('keeps progress formatting deterministic and omits unlimited dimensions', () => {
    expect(goalProgressFacts({ ...goal, remaining_tokens: null, remaining_time_seconds: null })).toEqual([
      '3 turns left',
    ]);
  });
});
