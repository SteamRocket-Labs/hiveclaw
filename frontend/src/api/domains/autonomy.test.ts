import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('autonomy API adapters', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds agent-scoped autonomy URLs without exposing diagnostics by default', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
      };
    });

    const { autonomyApi } = await import('./autonomy');
    const { get } = await import('../core');
    vi.mocked(get).mockResolvedValue({} as never);

    await autonomyApi.getOverview('agent-1', { lookbackHours: 12 });
    await autonomyApi.getDiagnostics('agent-1', { lookbackHours: 6 });
    await autonomyApi.listRuntimeTasks('agent-1', { taskType: 'trigger', status: 'failed', diagnostics: true });
    await autonomyApi.getRuntimeArtifact('agent-1', 'task-1');
    await autonomyApi.getRuntimeWorkLedger('agent-1', 'task-1');
    await autonomyApi.getSessionWorkLedger('agent-1', 'session-1');

    expect(get).toHaveBeenNthCalledWith(1, '/agents/agent-1/autonomy/overview?lookback_hours=12');
    expect(get).toHaveBeenNthCalledWith(2, '/agents/agent-1/autonomy/diagnostics?lookback_hours=6');
    expect(get).toHaveBeenNthCalledWith(
      3,
      '/agents/agent-1/runtime-tasks?task_type=trigger&status=failed&diagnostics=true',
    );
    expect(get).toHaveBeenNthCalledWith(4, '/agents/agent-1/runtime-artifacts/task-1');
    expect(get).toHaveBeenNthCalledWith(5, '/agents/agent-1/runtime-work-ledgers/task-1');
    expect(get).toHaveBeenNthCalledWith(6, '/agents/agent-1/sessions/session-1/work-ledger');
  });

  it('uses the business action-policy endpoints for read, update, history, and rollback', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        put: vi.fn(),
        post: vi.fn(),
      };
    });

    const { autonomyApi } = await import('./autonomy');
    const { get, put, post } = await import('../core');
    vi.mocked(get).mockResolvedValue({} as never);
    vi.mocked(put).mockResolvedValue({} as never);
    vi.mocked(post).mockResolvedValue({} as never);

    const actions = {
      'tool.external_effect': 'confirm_first',
      'tool.local_read': 'full_authority',
      'tool.local_write': 'never_do',
    } as const;
    await autonomyApi.getActionPolicy('agent-1');
    await autonomyApi.updateActionPolicy('agent-1', { actions, expected_version: 2 });
    await autonomyApi.getActionPolicyHistory('agent-1', 7);
    await autonomyApi.rollbackActionPolicy('agent-1', {
      target_version: 1,
      expected_version: 3,
      reason: 'Restore approved policy',
    });

    expect(get).toHaveBeenNthCalledWith(1, '/agents/agent-1/autonomy/action-policy');
    expect(put).toHaveBeenCalledWith('/agents/agent-1/autonomy/action-policy', {
      actions,
      expected_version: 2,
    });
    expect(get).toHaveBeenNthCalledWith(2, '/agents/agent-1/autonomy/action-policy/history?limit=7');
    expect(post).toHaveBeenCalledWith('/agents/agent-1/autonomy/action-policy/rollback', {
      target_version: 1,
      expected_version: 3,
      reason: 'Restore approved policy',
    });
  });

  it('refuses operator work-ledger reads without an exact audit reason', async () => {
    const { autonomyApi } = await import('./autonomy');

    expect(() => autonomyApi.getRuntimeWorkLedger('agent-1', 'task-1', {
      operatorView: true,
    })).toThrow('Operator View requires an audit reason');
  });
});
