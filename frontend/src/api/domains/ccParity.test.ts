import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ccParityApi } from './ccParity';

const localStore: Record<string, string> = {};
const localStorageStub = {
  getItem: (key: string) => (key in localStore ? localStore[key] : null),
  setItem: (key: string, value: string) => {
    localStore[key] = value;
  },
  removeItem: (key: string) => {
    delete localStore[key];
  },
  clear: () => {
    for (const key of Object.keys(localStore)) delete localStore[key];
  },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>;

beforeEach(() => {
  vi.stubGlobal('localStorage', localStorageStub);
  localStorageStub.setItem('token', 'test-token');
  fetchMock = vi.fn<typeof fetch>();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorageStub.clear();
});

function requestOf(callIndex = 0): { url: string; init: RequestInit } {
  const call = fetchMock.mock.calls[callIndex];
  return { url: String(call?.[0] ?? ''), init: (call?.[1] ?? {}) as RequestInit };
}

describe('ccParityApi', () => {
  it('lists compact commands without schemas', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([{ name: 'goal_start', aliases: [], category: 'goal' }]));

    const commands = await ccParityApi.listCommands('agent-1');

    expect(requestOf().url).toBe('/api/agents/agent-1/commands');
    expect(commands[0].name).toBe('goal_start');
    expect(commands[0]).not.toHaveProperty('input_schema');
  });

  it('loads a selected command schema on demand', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        name: 'goal_start',
        aliases: [],
        category: 'goal',
        source: 'builtin',
        execution_mode: 'runtime',
        permission_mode: 'default',
        bridge_safe: true,
        remote_safe: true,
        handler_ref: 'builtin:goal_start',
        input_schema: { properties: { objective: { type: 'string' } } },
        visible_to_model: true,
        visible_to_user: true,
      }),
    );

    const command = await ccParityApi.getCommand('agent-1', 'goal_start');

    expect(requestOf().url).toBe('/api/agents/agent-1/commands/goal_start');
    expect(command.input_schema.properties).toBeTruthy();
  });

  it('executes a builtin command', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true, command: 'advanced_plan', result: { ok: true } }));

    await ccParityApi.executeCommand('agent-1', 'advanced_plan', {
      arguments: { objective: 'plan' },
      session_id: 'session-1',
    });

    expect(requestOf().url).toBe('/api/agents/agent-1/commands/advanced_plan/execute');
    expect(JSON.parse(String(requestOf().init.body))).toEqual({
      arguments: { objective: 'plan' },
      session_id: 'session-1',
    });
  });

  it('starts a session goal', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'goal-1', status: 'active', objective: 'finish' }));

    await ccParityApi.startGoal('agent-1', 'session-1', {
      objective: 'finish',
      token_budget: 100,
      max_continuation_turns: 3,
    });

    expect(requestOf().url).toBe('/api/agents/agent-1/sessions/session-1/goals');
    expect(JSON.parse(String(requestOf().init.body))).toEqual({
      objective: 'finish',
      token_budget: 100,
      max_continuation_turns: 3,
      time_budget_seconds: null,
    });
  });

  it('continues a session goal', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true, run: { run_id: 'run-1' } }));

    await ccParityApi.continueGoal('agent-1', 'session-1', 'goal-1');

    expect(requestOf().url).toBe('/api/agents/agent-1/sessions/session-1/goals/goal-1/continue');
    expect(requestOf().init.method).toBe('POST');
  });

  it('starts an advanced plan runtime', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ run_id: 'plan-run-1', status: 'running' }));

    await ccParityApi.startAdvancedPlan('agent-1', 'session-1', {
      objective: 'design rollout',
      context: { source: 'freecode' },
    });

    expect(requestOf().url).toBe('/api/agents/agent-1/sessions/session-1/advanced-plan');
    expect(JSON.parse(String(requestOf().init.body))).toEqual({
      objective: 'design rollout',
      context: { source: 'freecode' },
    });
  });

  it('creates an enterable team', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'team-1', members: [] }));

    await ccParityApi.createTeam('agent-1', {
      parent_session_id: 'session-1',
      name: 'research',
      members: [{ name: 'critic', role: 'Review' }],
    });

    expect(requestOf().url).toBe('/api/agents/agent-1/agent-teams');
    expect(JSON.parse(String(requestOf().init.body))).toEqual({
      parent_session_id: 'session-1',
      name: 'research',
      members: [{ name: 'critic', role: 'Review' }],
    });
  });

  it('lists, enters, and closes an enterable team', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse([{ id: 'team-1', members: [] }]))
      .mockResolvedValueOnce(jsonResponse({ member_id: 'member-1', chat_session_id: 'session-2' }))
      .mockResolvedValueOnce(jsonResponse({ id: 'team-1', status: 'closed' }));

    await ccParityApi.listTeams('agent-1', 'session-1');
    await ccParityApi.enterTeamMember('agent-1', 'team-1', 'member-1');
    await ccParityApi.closeTeam('agent-1', 'team-1');

    expect(requestOf(0).url).toBe('/api/agents/agent-1/agent-teams?parent_session_id=session-1');
    expect(requestOf(1).url).toBe('/api/agents/agent-1/agent-teams/team-1/members/member-1/enter');
    expect(requestOf(2).url).toBe('/api/agents/agent-1/agent-teams/team-1/close');
    expect(requestOf(2).init.method).toBe('POST');
  });
});
