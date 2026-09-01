import { describe, expect, it, vi } from 'vitest';

vi.mock('../core', () => ({
  get: vi.fn(async (url: string) => ({ url })),
}));

describe('activityApi resource authority', () => {
  it('requests tenant-wide telemetry only through explicit operator view', async () => {
    const { get } = await import('../core');
    const { activityApi } = await import('./activity');
    const authority = { operatorView: true, reason: 'Agent activity administration' };

    await activityApi.list('agent-1', 100, authority);
    await activityApi.getToolFailureSummary('agent-1', 24, 200, authority);

    expect(vi.mocked(get).mock.calls[0][0]).toContain('operator_view=true');
    expect(vi.mocked(get).mock.calls[0][0]).toContain('operator_reason=Agent+activity+administration');
    expect(vi.mocked(get).mock.calls[1][0]).toContain('operator_view=true');
  });

  it('refuses operator view without an exact audit reason', async () => {
    const { activityApi } = await import('./activity');

    expect(() => activityApi.list('agent-1', 100, { operatorView: true })).toThrow(
      'Operator View requires an audit reason',
    );
  });
});
