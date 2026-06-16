import { describe, expect, it, vi } from 'vitest';

vi.mock('../core', () => ({
  get: vi.fn(async (url: string) => ({ url })),
  post: vi.fn(async (url: string, body?: unknown) => ({ url, body })),
  put: vi.fn(async (url: string, body?: unknown) => ({ url, body })),
}));

describe('adminApi runtime reconciliation adapter', () => {
  it('builds list/get/action URLs for runtime reconciliation', async () => {
    const { get, post } = await import('../core');
    const { adminApi } = await import('./admin');

    await adminApi.listRuntimeReconciliation({ tenantId: 'tenant-1', limit: 25 });
    await adminApi.getRuntimeReconciliation('task-1', { tenantId: 'tenant-1' });
    await adminApi.applyRuntimeReconciliationAction('task-1', {
      tenantId: 'tenant-1',
      action: 'mark_resolved',
      reason: 'verified',
    });

    expect(vi.mocked(get).mock.calls[0][0]).toBe(
      '/admin/runtime-reconciliation?tenant_id=tenant-1&status=needs_reconciliation&limit=25',
    );
    expect(vi.mocked(get).mock.calls[1][0]).toBe('/admin/runtime-reconciliation/task-1?tenant_id=tenant-1');
    expect(vi.mocked(post).mock.calls[0]).toEqual([
      '/admin/runtime-reconciliation/task-1/action?tenant_id=tenant-1',
      { action: 'mark_resolved', reason: 'verified' },
    ]);
  });
});
