import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('runtime budget API adapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds policy, run, approval, and tenant mode endpoints', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        patch: vi.fn(),
        post: vi.fn(),
      };
    });

    const { runtimeBudgetApi } = await import('./runtimeBudgets');
    const { get, patch, post } = await import('../core');
    vi.mocked(get).mockResolvedValue([] as never);
    vi.mocked(post).mockResolvedValue({} as never);
    vi.mocked(patch).mockResolvedValue({} as never);

    await runtimeBudgetApi.listPolicies();
    await runtimeBudgetApi.createPolicy({ name: 'Scheduled', max_subagents: 16 });
    await runtimeBudgetApi.updatePolicy('policy-1', { enforcement_mode: 'observe' });
    await runtimeBudgetApi.listRuns({ status: 'exhausted', agentId: 'agent-1', limit: 10 });
    await runtimeBudgetApi.listEvents('run-1', 25);
    await runtimeBudgetApi.cancelRun('run-1', 'stop');
    await runtimeBudgetApi.approveOverrun('run-1', { reason: 'reviewed', enforcement_mode: 'observe' });
    await runtimeBudgetApi.setTenantEnforcementMode({ enforcement_mode: 'observe', reason: 'emergency' });

    expect(get).toHaveBeenNthCalledWith(1, '/runtime-budgets/policies');
    expect(post).toHaveBeenNthCalledWith(1, '/runtime-budgets/policies', { name: 'Scheduled', max_subagents: 16 });
    expect(patch).toHaveBeenCalledWith('/runtime-budgets/policies/policy-1', { enforcement_mode: 'observe' });
    expect(get).toHaveBeenNthCalledWith(2, '/runtime-budgets/runs?status=exhausted&agent_id=agent-1&limit=10');
    expect(get).toHaveBeenNthCalledWith(3, '/runtime-budgets/runs/run-1/events?limit=25');
    expect(post).toHaveBeenNthCalledWith(2, '/runtime-budgets/runs/run-1/cancel', { reason: 'stop' });
    expect(post).toHaveBeenNthCalledWith(3, '/runtime-budgets/runs/run-1/approve-overrun', {
      reason: 'reviewed',
      enforcement_mode: 'observe',
    });
    expect(post).toHaveBeenNthCalledWith(4, '/runtime-budgets/tenant/enforcement-mode', {
      enforcement_mode: 'observe',
      reason: 'emergency',
    });
  });
});
