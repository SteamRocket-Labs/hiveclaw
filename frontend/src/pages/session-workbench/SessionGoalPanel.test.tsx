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
