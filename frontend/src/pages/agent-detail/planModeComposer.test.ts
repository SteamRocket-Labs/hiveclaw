import { describe, expect, it } from 'vitest';

import { buildPlanModeScopeKey, nextPlanModeRequestedForScope } from './planModeComposer';

describe('planModeComposer', () => {
  it('scopes an explicit Plan Mode request to the current agent and session', () => {
    expect(buildPlanModeScopeKey('agent-1', 'session-1')).toBe('agent-1:session-1');
    expect(buildPlanModeScopeKey('agent-1', null)).toBe('agent-1:');
    expect(buildPlanModeScopeKey(undefined, 'session-1')).toBe(':session-1');
  });

  it('clears a pending explicit Plan Mode request when the agent/session scope changes', () => {
    expect(
      nextPlanModeRequestedForScope({
        currentRequested: true,
        previousScopeKey: 'agent-1:session-1',
        nextScopeKey: 'agent-1:session-2',
      }),
    ).toBe(false);
  });

  it('keeps a pending explicit Plan Mode request inside the same agent/session scope', () => {
    expect(
      nextPlanModeRequestedForScope({
        currentRequested: true,
        previousScopeKey: 'agent-1:session-1',
        nextScopeKey: 'agent-1:session-1',
      }),
    ).toBe(true);
  });
});
