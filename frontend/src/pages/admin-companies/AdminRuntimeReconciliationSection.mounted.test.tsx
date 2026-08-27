// @vitest-environment jsdom
//
// Mounted interaction tests for AdminRuntimeReconciliationSection: the real
// component is rendered in jsdom with @testing-library/react while only the
// API domain boundary is mocked (real i18n catalog). These tests prove the
// projection-repair operator control (RC-10B): the exact adapter arguments,
// the truthful server receipt, and the in-place queue reload.

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  RuntimeProjectionRepairReceipt,
  RuntimeReconciliationTask,
} from '../../api/domains/admin';

function repairButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Repair projections' }) as HTMLButtonElement;
}
import { adminApi } from '../../api/domains/admin';
import '../../i18n';

vi.mock('../../api/domains/admin', () => ({
  adminApi: {
    listRuntimeReconciliation: vi.fn(),
    getRuntimeReconciliation: vi.fn(),
    applyRuntimeReconciliationAction: vi.fn(),
    repairRuntimeReconciliationProjections: vi.fn(),
  },
}));

import AdminRuntimeReconciliationSection from './AdminRuntimeReconciliationSection';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const api = vi.mocked(adminApi);

function makeTask(taskId: string, agentName: string): RuntimeReconciliationTask {
  return {
    task_id: taskId,
    tenant_id: 'tenant-1',
    task_type: 'web_chat_run',
    status: 'needs_reconciliation',
    child_agent_name: agentName,
    reason: 'ambiguous_provider_send',
    side_effect_risk: 'unknown',
    retry_allowed: false,
    created_at: '2026-08-26T00:00:00Z',
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
});

describe('AdminRuntimeReconciliationSection — projection repair (mounted)', () => {
  it('repairs projections through the authenticated adapter, shows the truthful receipt, and reloads the queue in place', async () => {
    api.repairRuntimeReconciliationProjections.mockResolvedValue({
      examined: 3,
      repaired_task_ids: ['task-a'],
    });
    api.listRuntimeReconciliation.mockResolvedValue([makeTask('task-b', 'writer-b')]);

    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="  tenant-1  "
        initialTasks={[makeTask('task-a', 'writer-a'), makeTask('task-b-stale', 'writer-stale')]}
      />,
    );

    expect(screen.getByText('2 open items')).toBeTruthy();
    expect(repairButton().disabled).toBe(false);
    fireEvent.click(repairButton());

    // The receipt is the server truth: examined count + repaired count.
    await screen.findByText('Projection repair finished: examined 3 candidates, repaired 1 projections.');

    // The trimmed tenant reached the adapter; repair never resolves/archives/retries.
    expect(api.repairRuntimeReconciliationProjections).toHaveBeenCalledTimes(1);
    expect(api.repairRuntimeReconciliationProjections).toHaveBeenCalledWith({ tenantId: 'tenant-1' });
    expect(api.applyRuntimeReconciliationAction).not.toHaveBeenCalled();

    // The queue reloaded in place and now renders the post-repair truth.
    await waitFor(() =>
      expect(api.listRuntimeReconciliation).toHaveBeenCalledWith({ tenantId: 'tenant-1', limit: 50 }),
    );
    await screen.findByText('1 open items');
    await screen.findByText('writer-b');
    expect(screen.queryByText('writer-stale')).toBeNull();
    expect(screen.queryByText('writer-a')).toBeNull();
  });

  it('keeps the successful receipt visible when the follow-up queue reload fails', async () => {
    api.repairRuntimeReconciliationProjections.mockResolvedValue({
      examined: 2,
      repaired_task_ids: [],
    });
    api.listRuntimeReconciliation.mockRejectedValue(new Error('reload failed'));

    render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Repair projections' }));

    await screen.findByText('Projection repair finished: examined 2 candidates, repaired 0 projections.');
    await screen.findByText('reload failed');
  });

  it('shows the API failure and no success receipt when the repair itself fails', async () => {
    api.repairRuntimeReconciliationProjections.mockRejectedValue(new Error('platform_admin role required'));

    render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Repair projections' }));

    await screen.findByText('platform_admin role required');
    expect(screen.queryByText(/Projection repair finished/)).toBeNull();
    expect(api.listRuntimeReconciliation).not.toHaveBeenCalled();
  });

  it('shows a truthful in-progress label and disables every operator control while the repair is busy', async () => {
    const repair = deferred<RuntimeProjectionRepairReceipt>();
    api.repairRuntimeReconciliationProjections.mockReturnValue(repair.promise);
    api.listRuntimeReconciliation.mockResolvedValue([]);

    // retry_allowed fixture so the Retry control is present and pinned too.
    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[{ ...makeTask('task-a', 'writer-a'), retry_allowed: true }]}
      />,
    );
    fireEvent.click(repairButton());

    const busyButton = (await screen.findByRole('button', { name: 'Repairing...' })) as HTMLButtonElement;
    // Unified busy boundary: no tenant change and no semantic action can race
    // the in-flight repair or its follow-up queue reload.
    expect(busyButton.disabled).toBe(true);
    expect((screen.getByPlaceholderText('Tenant ID') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Resolve' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Archive' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.applyRuntimeReconciliationAction).not.toHaveBeenCalled();

    repair.resolve({ examined: 1, repaired_task_ids: ['task-a'] });
    await screen.findByText('Projection repair finished: examined 1 candidates, repaired 1 projections.');
    expect((screen.getByPlaceholderText('Tenant ID') as HTMLInputElement).disabled).toBe(false);
    expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false);
    expect(repairButton().disabled).toBe(false);
  });

  it('keeps the repair control disabled for a missing or blank tenant', () => {
    render(<AdminRuntimeReconciliationSection initialTenantId="" />);

    expect(repairButton().disabled).toBe(true);

    fireEvent.change(screen.getByPlaceholderText('Tenant ID'), { target: { value: '   ' } });
    expect(repairButton().disabled).toBe(true);
    expect(api.repairRuntimeReconciliationProjections).not.toHaveBeenCalled();
  });

  it('clears a stale receipt when the tenant changes', async () => {
    api.repairRuntimeReconciliationProjections.mockResolvedValue({
      examined: 1,
      repaired_task_ids: ['task-a'],
    });
    api.listRuntimeReconciliation.mockResolvedValue([]);

    render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Repair projections' }));
    await screen.findByText(/Projection repair finished/);

    // Tenant change drops the previous tenant's receipt.
    fireEvent.change(screen.getByPlaceholderText('Tenant ID'), { target: { value: 'tenant-2' } });
    expect(screen.queryByText(/Projection repair finished/)).toBeNull();

    // The next repair goes to the new tenant.
    fireEvent.click(screen.getByRole('button', { name: 'Repair projections' }));
    await waitFor(() => expect(api.repairRuntimeReconciliationProjections).toHaveBeenCalledTimes(2));
    expect(api.repairRuntimeReconciliationProjections).toHaveBeenLastCalledWith({ tenantId: 'tenant-2' });
  });

  it('clears the previous receipt while a new repair is in flight', async () => {
    const second = deferred<RuntimeProjectionRepairReceipt>();
    api.repairRuntimeReconciliationProjections
      .mockResolvedValueOnce({ examined: 1, repaired_task_ids: ['task-a'] })
      .mockReturnValueOnce(second.promise);
    api.listRuntimeReconciliation.mockResolvedValue([]);

    render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Repair projections' }));
    await screen.findByText(/Projection repair finished/);

    fireEvent.click(screen.getByRole('button', { name: 'Repair projections' }));
    await waitFor(() => expect(api.repairRuntimeReconciliationProjections).toHaveBeenCalledTimes(2));
    // The stale receipt is gone while the new repair is pending.
    expect(screen.queryByText(/Projection repair finished/)).toBeNull();
    await screen.findByRole('button', { name: 'Repairing...' });

    second.resolve({ examined: 2, repaired_task_ids: [] });
    await screen.findByText('Projection repair finished: examined 2 candidates, repaired 0 projections.');
  });
});
