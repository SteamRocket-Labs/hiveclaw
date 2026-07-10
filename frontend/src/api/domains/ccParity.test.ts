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

  it('lists user commands with optional packs when requested', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([{ name: 'diff', aliases: [], category: 'coding_pack' }]));

    await ccParityApi.listCommands('agent-1', { surface: 'user', includeOptionalPacks: true });

    expect(requestOf().url).toBe('/api/agents/agent-1/commands?surface=user&include_optional_packs=true');
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

    const command = await ccParityApi.getCommand('agent-1', 'goal_start', { includeOptionalPacks: true });

    expect(requestOf().url).toBe('/api/agents/agent-1/commands/goal_start?include_optional_packs=true');
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
      request_id: '72ea993a-64a7-4f88-8f20-2763f17f848b',
      content: 'Full model-visible request',
      display_content: 'Finish the work',
      attachments: [{ path: 'workspace/brief.md' }],
      start_immediately: true,
    });

    expect(requestOf().url).toBe('/api/agents/agent-1/sessions/session-1/goals');
    expect(JSON.parse(String(requestOf().init.body))).toEqual({
      objective: 'finish',
      request_id: '72ea993a-64a7-4f88-8f20-2763f17f848b',
      token_budget: 100,
      max_continuation_turns: 3,
      time_budget_seconds: null,
      content: 'Full model-visible request',
      display_content: 'Finish the work',
      file_name: '',
      attachments: [{ path: 'workspace/brief.md' }],
      parts: [],
      start_immediately: true,
    });
  });

  it('transitions a session goal through its semantic control surface', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 'goal-1', status: 'paused' }));

    await ccParityApi.transitionGoal('agent-1', 'session-1', 'goal-1', 'pause');

    expect(requestOf().url).toBe('/api/agents/agent-1/sessions/session-1/goals/goal-1/transition');
    expect(JSON.parse(String(requestOf().init.body))).toEqual({ action: 'pause' });
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
    });

    expect(requestOf().url).toBe('/api/agents/agent-1/agent-teams');
    expect(JSON.parse(String(requestOf().init.body))).toEqual({
      parent_session_id: 'session-1',
      name: 'research',
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

  it('loads CCPlus session workbench, context usage, hook management, JSON export, and team workbench surfaces', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ schema: 'hive.ccplus.session_workbench.v1' }))
      .mockResolvedValueOnce(jsonResponse({ schema: 'hive.ccplus.session_context_usage.v1' }))
      .mockResolvedValueOnce(jsonResponse({ schema: 'hive.ccplus.session_export.v1' }))
      .mockResolvedValueOnce(jsonResponse({ schema: 'hive.ccplus.hooks_control_plane.v1', events: [] }))
      .mockResolvedValueOnce(jsonResponse({ schema: 'hive.ccplus.agent_team_workbench.v1' }));

    await ccParityApi.getSessionWorkbench('agent-1', 'session-1');
    await ccParityApi.getSessionContextUsage('agent-1', 'session-1');
    await ccParityApi.exportSessionJson('agent-1', 'session-1');
    await ccParityApi.listHooks('agent-1');
    await ccParityApi.getTeamWorkbench('agent-1', 'team-1');

    expect(requestOf(0).url).toBe('/api/agents/agent-1/sessions/session-1/workbench');
    expect(requestOf(1).url).toBe('/api/agents/agent-1/sessions/session-1/context-usage');
    expect(requestOf(2).url).toBe('/api/agents/agent-1/sessions/session-1/export');
    expect(requestOf(3).url).toBe('/api/agents/agent-1/hooks');
    expect(requestOf(4).url).toBe('/api/agents/agent-1/agent-teams/team-1/workbench');
  });

  it('updates hook runtime config by stable hook key', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true, config: { key: 'hook.stop', enabled: false } }));

    await ccParityApi.updateHookRuntimeConfig('agent-1', 'hook.stop', { enabled: false, failure_policy: 'block' });

    expect(requestOf().url).toBe('/api/agents/agent-1/hooks/hook.stop');
    expect(requestOf().init.method).toBe('PATCH');
    expect(JSON.parse(String(requestOf().init.body))).toEqual({ enabled: false, failure_policy: 'block' });
  });
});
