import { describe, expect, it, vi } from 'vitest';

vi.mock('../core', () => ({
  get: vi.fn(async (url: string) => ({ url })),
  post: vi.fn(async (url: string, body?: unknown) => ({ url, body })),
  put: vi.fn(async (url: string, body?: unknown) => ({ url, body })),
}));

describe('adminApi runtime reconciliation adapter', () => {
  it('assigns a tenantless user by email through the platform tenant API', async () => {
    const { put } = await import('../core');
    const { adminApi } = await import('./admin');
    vi.mocked(put).mockClear();

    await adminApi.assignUserToTenant('tenant/1', {
      email: 'new-admin@example.com',
      role: 'org_admin',
    });

    expect(vi.mocked(put)).toHaveBeenCalledWith('/tenants/tenant%2F1/assign-user', {
      email: 'new-admin@example.com',
      role: 'org_admin',
    });
  });

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
    await adminApi.applyRuntimeReconciliationAction('task-2', {
      tenantId: 'tenant-1',
      action: 'acknowledge_tool_effect',
      reason: 'verified effect evidence',
    });

    expect(vi.mocked(get).mock.calls[0][0]).toBe(
      '/admin/runtime-reconciliation?tenant_id=tenant-1&status=needs_reconciliation&limit=25',
    );
    expect(vi.mocked(get).mock.calls[1][0]).toBe('/admin/runtime-reconciliation/task-1?tenant_id=tenant-1');
    expect(vi.mocked(post).mock.calls[0]).toEqual([
      '/admin/runtime-reconciliation/task-1/action?tenant_id=tenant-1',
      { action: 'mark_resolved', reason: 'verified' },
    ]);
    expect(vi.mocked(post).mock.calls[1]).toEqual([
      '/admin/runtime-reconciliation/task-2/action?tenant_id=tenant-1',
      { action: 'acknowledge_tool_effect', reason: 'verified effect evidence' },
    ]);
  });

  it('builds the projection-repair URL with encoded tenant, default and explicit limits, and no body', async () => {
    const { post } = await import('../core');
    const { adminApi } = await import('./admin');
    vi.mocked(post).mockClear();

    await adminApi.repairRuntimeReconciliationProjections({ tenantId: 'tenant-1' });
    await adminApi.repairRuntimeReconciliationProjections({ tenantId: 'tenant/ 2', limit: 250 });

    // Exact backend contract: POST /admin/runtime-reconciliation/projection-repair
    // with tenant_id + limit query params and no request body.
    expect(vi.mocked(post).mock.calls).toEqual([
      ['/admin/runtime-reconciliation/projection-repair?tenant_id=tenant-1&limit=100'],
      ['/admin/runtime-reconciliation/projection-repair?tenant_id=tenant%2F+2&limit=250'],
    ]);
  });
});
