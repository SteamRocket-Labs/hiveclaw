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
      confirmed: true,
      evidenceDigest: 'a'.repeat(64),
      frameDecisions: [
        {
          runtime_task_id: 'run-1',
          tool_call_id: 'call-1',
          tool_name: 'send_email',
          decision: 'mark_resolved',
        },
      ],
      operationId: 'operation-1',
    });

    expect(vi.mocked(get).mock.calls[0][0]).toBe(
      '/admin/runtime-reconciliation?tenant_id=tenant-1&status=needs_reconciliation&limit=25',
    );
    expect(vi.mocked(get).mock.calls[1][0]).toBe('/admin/runtime-reconciliation/task-1?tenant_id=tenant-1');
    expect(vi.mocked(post).mock.calls[0]).toEqual([
      '/admin/runtime-reconciliation/task-1/action?tenant_id=tenant-1',
      {
        action: 'mark_resolved',
        reason: 'verified',
        confirmed: true,
        evidence_digest: 'a'.repeat(64),
        frame_decisions: [
          {
            runtime_task_id: 'run-1',
            tool_call_id: 'call-1',
            tool_name: 'send_email',
            decision: 'mark_resolved',
          },
        ],
        operation_id: 'operation-1',
      },
    ]);
  });

  it('builds independent Workflow completion delivery-only URLs', async () => {
    const { get, post } = await import('../core');
    const { adminApi } = await import('./admin');
    vi.clearAllMocks();

    await adminApi.listWorkflowCompletionDeliveries({ tenantId: 'tenant-1', limit: 25 });
    await adminApi.retryWorkflowCompletionDelivery('delivery-1', {
      tenantId: 'tenant-1',
      reason: 'coordination authority repaired',
      confirmed: true,
    });

    expect(vi.mocked(get).mock.calls[0][0]).toBe(
      '/admin/workflow-completion-deliveries?tenant_id=tenant-1&status=dead_letter&limit=25',
    );
    expect(vi.mocked(post).mock.calls[0]).toEqual([
      '/admin/workflow-completion-deliveries/delivery-1/retry?tenant_id=tenant-1',
      { reason: 'coordination authority repaired', confirmed: true },
    ]);
  });

  it('builds generic runtime notification delivery-only URLs', async () => {
    const { get, post } = await import('../core');
    const { adminApi } = await import('./admin');
    vi.clearAllMocks();

    await adminApi.listRuntimeNotificationDeliveries({ tenantId: 'tenant/one', limit: 25 });
    await adminApi.retryRuntimeNotificationDelivery('delivery generic', {
      tenantId: 'tenant/one',
      reason: 'parent session authority repaired',
      confirmed: true,
    });

    expect(vi.mocked(get).mock.calls[0][0]).toBe(
      '/admin/runtime-notification-deliveries?tenant_id=tenant%2Fone&status=dead_letter&limit=25',
    );
    expect(vi.mocked(post).mock.calls[0]).toEqual([
      '/admin/runtime-notification-deliveries/delivery generic/retry?tenant_id=tenant%2Fone',
      { reason: 'parent session authority repaired', confirmed: true },
    ]);
  });
});
