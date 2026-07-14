import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('plan mode API adapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds agent-scoped plan URLs and confirms by server-current version', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
      };
    });

    const { planApi } = await import('./plans');
    const { get, post } = await import('../core');
    vi.mocked(get).mockResolvedValue({} as never);
    vi.mocked(post).mockResolvedValue({} as never);

    await planApi.list('agent-1');
    await planApi.list('agent-1', 10);
    await planApi.get('agent-1', 'plan-1');
    await planApi.create('agent-1', {
      original_request: 'Send a daily brief',
      intent_type: 'autonomous_wake',
      session_id: 'sess-1',
    });
    await planApi.revise('agent-1', 'plan-1', { fill: { revision_request: 'tighten scope' } });
    await planApi.confirm('agent-1', 'plan-1', { plan_version: 2 });
    await planApi.confirmAndHandoff('agent-1', 'plan-1', { plan_version: 2 });
    await planApi.reject('agent-1', 'plan-1', { reason: 'not now' });
    await planApi.handoff('agent-1', 'plan-1');
    await planApi.createRecommendation('agent-1', {
      original_request: 'Enable scheduled patrol',
      session_id: 'settings-patrol',
      source: 'settings',
    });
    await planApi.declineRecommendation('agent-1', 'rec-1');

    expect(get).toHaveBeenNthCalledWith(1, '/agents/agent-1/plans?limit=50');
    expect(get).toHaveBeenNthCalledWith(2, '/agents/agent-1/plans?limit=10');
    expect(get).toHaveBeenNthCalledWith(3, '/agents/agent-1/plans/plan-1');

    expect(post).toHaveBeenNthCalledWith(1, '/agents/agent-1/plans', {
      original_request: 'Send a daily brief',
      intent_type: 'autonomous_wake',
      session_id: 'sess-1',
    });
    expect(post).toHaveBeenNthCalledWith(2, '/agents/agent-1/plans/plan-1/revise', {
      fill: { revision_request: 'tighten scope' },
    });
    expect(post).toHaveBeenNthCalledWith(3, '/agents/agent-1/plans/plan-1/confirm', {
      plan_version: 2,
    });
    expect(post).toHaveBeenNthCalledWith(4, '/agents/agent-1/plans/plan-1/confirm-and-handoff', {
      plan_version: 2,
    });
    expect(post).toHaveBeenNthCalledWith(5, '/agents/agent-1/plans/plan-1/reject', { reason: 'not now' });
    expect(post).toHaveBeenNthCalledWith(6, '/agents/agent-1/plans/plan-1/handoff', {});
    expect(post).toHaveBeenNthCalledWith(7, '/agents/agent-1/plan-recommendations', {
      original_request: 'Enable scheduled patrol',
      session_id: 'settings-patrol',
      source: 'settings',
    });
    expect(post).toHaveBeenNthCalledWith(8, '/agents/agent-1/plan-recommendations/rec-1/decline', {});
  });
});
