import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }),
}));

import {
  SessionAgentTeamCloseControl,
  SessionAgentTeamMemberControls,
  teamMemberActionState,
} from './SessionAgentTeamControls';
import type { RuntimeSectionItemModel } from './timelineModel';

function item(overrides: Partial<RuntimeSectionItemModel> = {}): RuntimeSectionItemModel {
  return {
    id: 'member-1',
    label: 'Critic',
    status: 'idle',
    state: 'idle',
    runtimeKind: 'team_member',
    summary: 'Review complete',
    childSessionId: 'session-1',
    enterable: true,
    metrics: {
      elapsedSeconds: null,
      elapsedLabel: null,
      tokenCount: null,
      tokenLabel: null,
      toolUseCount: null,
      toolUseLabel: null,
      lastActivityLabel: null,
    },
    members: [],
    steps: [],
    leafCalls: [],
    raw: { last_turn_status: 'completed' },
    ...overrides,
  };
}

describe('SessionAgentTeamControls', () => {
  it('enables Send and Resume for an idle completed member without exposing ids', () => {
    const member = item();
    const state = teamMemberActionState('active', member);
    const markup = renderToStaticMarkup(
      <SessionAgentTeamMemberControls
        agentId="agent-1"
        teamId="team-1"
        teamStatus="active"
        member={member}
        onEnter={() => undefined}
      />,
    );

    expect(state).toEqual({ canEnter: true, canSend: true, canResume: true });
    expect(markup).toContain('>Enter<');
    expect(markup).toContain('>Send<');
    expect(markup).toContain('>Resume<');
    expect(markup).not.toContain('member-1');
    expect(markup).not.toContain('team-1');
  });

  it('allows Team close only when no member is running and shows closing state', () => {
    const idleTeam = item({ id: 'team-1', runtimeKind: 'agent_team', status: 'active', members: [item()] });
    const closingTeam = { ...idleTeam, status: 'closing' };
    const failedTeam = { ...idleTeam, raw: { close_status: 'failed', close_failure: 'Provider timeout' } };
    const idleMarkup = renderToStaticMarkup(
      <SessionAgentTeamCloseControl agentId="agent-1" team={idleTeam} />,
    );
    const closingMarkup = renderToStaticMarkup(
      <SessionAgentTeamCloseControl agentId="agent-1" team={closingTeam} />,
    );
    const failedMarkup = renderToStaticMarkup(
      <SessionAgentTeamCloseControl agentId="agent-1" team={failedTeam} />,
    );

    expect(idleMarkup).toContain('>Close team<');
    expect(idleMarkup).not.toContain('disabled=""');
    expect(closingMarkup).toContain('Closing…');
    expect(closingMarkup).toContain('disabled=""');
    expect(failedMarkup).toContain('Provider timeout');
    expect(failedMarkup).toContain('>Close team<');
  });
});
