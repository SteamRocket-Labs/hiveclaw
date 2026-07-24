import { describe, expect, it, vi } from 'vitest';

vi.mock('../core', () => ({
  get: vi.fn(async (url: string) => ({ url })),
}));

describe('dashboardApi', () => {
  it('loads the whole dashboard through one bounded overview endpoint', async () => {
    const { get } = await import('../core');
    const { dashboardApi } = await import('./dashboard');

    await dashboardApi.getOverview('tenant-1');

    expect(vi.mocked(get)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(get).mock.calls[0][0]).toBe('/dashboard/overview?tenant_id=tenant-1');
  });
});
