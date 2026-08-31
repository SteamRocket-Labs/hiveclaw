// @vitest-environment jsdom
//
// Mounted interaction tests for AdminRuntimeReconciliationSection: the real
// component is rendered in jsdom with @testing-library/react while only the
// API domain boundary is mocked (real i18n catalog). These tests prove the
// projection-repair operator control (RC-10B): the exact adapter arguments,
// the truthful server receipt, and the in-place queue reload.

import { StrictMode } from 'react';
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
  it('requires an evidence note and exposes only acknowledgement for an unresolved tool effect', async () => {
    const held: RuntimeReconciliationTask = {
      ...makeTask('task-tool-effect', 'writer-effect'),
      status: 'failed',
      reason: 'tool_effect_outcome_unknown',
      side_effect_risk: 'effect_outcome_unknown',
      tool_effect_reconciliation_required: true,
      unsettled_tool_effect_count: 1,
      supported_actions: ['acknowledge_tool_effect'],
    };
    api.applyRuntimeReconciliationAction.mockResolvedValue({
      ...held,
      tool_effect_reconciliation_required: false,
    });
    api.listRuntimeReconciliation.mockResolvedValue([]);

    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[held]}
      />,
    );

    expect(screen.queryByRole('button', { name: 'Resolve' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Archive' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    const acknowledge = screen.getByRole('button', {
      name: 'Acknowledge effect and stop — writer-effect',
    }) as HTMLButtonElement;
    expect(acknowledge.disabled).toBe(true);

    fireEvent.change(screen.getByPlaceholderText('Required effect evidence note'), {
      target: { value: 'Verified the synthetic file; retain it and do not replay.' },
    });
    expect(acknowledge.disabled).toBe(false);
    fireEvent.click(acknowledge);

    await waitFor(() => expect(api.applyRuntimeReconciliationAction).toHaveBeenCalledWith(
      'task-tool-effect',
      {
        tenantId: 'tenant-1',
        action: 'acknowledge_tool_effect',
        reason: 'Verified the synthetic file; retain it and do not replay.',
      },
    ));
    await screen.findByText('0 open items');
  });

  it('sends an explicit disposition for trigger reconciliation and never exposes generic retry', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const heldTrigger: RuntimeReconciliationTask = {
      ...makeTask('task-trigger-hold', 'trigger-agent'),
      task_type: 'trigger',
      reason: 'effect_outcome_unknown',
      retry_allowed: false,
      supported_actions: ['mark_resolved', 'archive'],
      supported_trigger_dispositions: ['confirmed_success', 'confirmed_failure', 'release'],
      trigger_disposition_readiness: {
        schema: 'runtime_trigger_disposition_readiness.v1',
        ready: true,
        blocker: null,
        terminal_projection_id: 'terminal-projection-1',
      },
      parent_agent_id: '11111111-1111-4111-8111-111111111111',
      child_session_id: '22222222-2222-4222-8222-222222222222',
      trace_id: 'trace-trigger-1',
      output_artifact: {
        schema: 'trigger_output_artifact.v1',
        path: 'runtime_artifacts/triggers/task-trigger-hold.json',
      },
      completion_outbox_id: 'completion-outbox-1',
      settlement_audit_ref: { kind: 'audit_log', id: 'audit-log-1' },
      metadata: {
        workflow_trigger_results: [{
          trigger_id: 'workflow-trigger-1',
          trigger_name: 'weekly-report',
          status: 'needs_reconciliation',
          run_id: 'workflow-run-1',
          run_status: 'completed',
          session_id: 'workflow-session-1',
          reason: 'workflow_asset_usage_evidence_commit_failed',
        }],
        trigger_settlement_overrides: {
          'workflow-trigger-1': 'hold',
          'react-trigger-1': 'success',
          'legacy-trigger-ignored': 'failure',
        },
        trigger_settlement: {
          trigger_outcomes: {
            'workflow-trigger-1': 'hold',
            'react-trigger-1': 'success',
          },
        },
      },
    };
    api.applyRuntimeReconciliationAction.mockResolvedValue({
      ...heldTrigger,
      status: 'completed',
    });
    api.listRuntimeReconciliation.mockResolvedValue([]);

    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[heldTrigger]}
      />,
    );

    expect(screen.queryByRole('button', { name: 'Resolve' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Archive' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(screen.getByText(/weekly-report: needs_reconciliation/)).toBeTruthy();
    expect(screen.getByText(/workflow-run-1/)).toBeTruthy();
    expect(screen.getByText(/workflow-session-1/)).toBeTruthy();
    expect(screen.getByText(/workflow_asset_usage_evidence_commit_failed/)).toBeTruthy();
    expect(screen.getByText(/workflow-trigger-1: hold/)).toBeTruthy();
    expect(screen.getByText(/react-trigger-1: success/)).toBeTruthy();
    expect(screen.queryByText(/legacy-trigger-ignored/)).toBeNull();
    expect(screen.getByText('Disposition evidence is ready.')).toBeTruthy();
    expect(screen.getByText('trace-trigger-1')).toBeTruthy();
    expect(screen.getByText(/trigger_output_artifact.v1/)).toBeTruthy();
    expect(screen.getByText('completion-outbox-1')).toBeTruthy();
    expect(screen.getByText('audit-log-1')).toBeTruthy();
    expect(
      (screen.getByRole('link', { name: 'Open Child session ID — trigger-agent' }) as HTMLAnchorElement).href,
    ).toContain(
      '/agents/11111111-1111-4111-8111-111111111111/sessions/22222222-2222-4222-8222-222222222222',
    );
    expect(screen.queryByRole('link', { name: 'Open Trace ID — trigger-agent' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Copy Trace ID — trigger-agent' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('trace-trigger-1'));

    const confirmSuccess = screen.getByRole('button', {
      name: 'Confirm success — trigger-agent',
    }) as HTMLButtonElement;
    const confirmFailure = screen.getByRole('button', {
      name: 'Confirm failure — trigger-agent',
    }) as HTMLButtonElement;
    const release = screen.getByRole('button', {
      name: 'Release hold — trigger-agent',
    }) as HTMLButtonElement;
    expect(confirmSuccess.disabled).toBe(true);
    expect(confirmFailure.disabled).toBe(true);
    expect(release.disabled).toBe(true);
    fireEvent.change(screen.getByPlaceholderText('Required trigger evidence note'), {
      target: { value: 'Verified child evidence and canonical artifact receipt.' },
    });
    expect(confirmSuccess.disabled).toBe(false);
    expect(confirmFailure.disabled).toBe(false);
    expect(release.disabled).toBe(false);
    fireEvent.click(confirmSuccess);

    await waitFor(() => expect(api.applyRuntimeReconciliationAction).toHaveBeenCalledWith(
      'task-trigger-hold',
      {
        tenantId: 'tenant-1',
        action: 'mark_resolved',
        reason: 'Verified child evidence and canonical artifact receipt.',
        triggerDisposition: 'confirmed_success',
      },
    ));
    await screen.findByText('0 open items');
  });

  it('keeps legacy trigger evidence visible while fail-closing not-ready dispositions', () => {
    const heldTrigger: RuntimeReconciliationTask = {
      ...makeTask('task-trigger-legacy', 'legacy-trigger-agent'),
      task_type: 'trigger',
      parent_agent_id: '11111111-1111-4111-8111-111111111111',
      child_session_id: 'legacy-session-not-routable',
      supported_trigger_dispositions: ['confirmed_success', 'confirmed_failure', 'release'],
      trigger_disposition_readiness: {
        schema: 'runtime_trigger_disposition_readiness.v1',
        ready: false,
        blocker: 'canonical_trigger_settlement_missing',
        terminal_projection_id: null,
      },
      metadata: {
        trigger_settlement_overrides: { 'legacy-trigger-1': 'hold' },
      },
    };

    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[heldTrigger]}
      />,
    );

    expect(screen.getByText(/legacy-trigger-1: hold/)).toBeTruthy();
    expect(screen.getByText(/canonical_trigger_settlement_missing/)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Open Child session ID — legacy-trigger-agent' })).toBeNull();
    const confirmSuccess = screen.getByRole('button', {
      name: 'Confirm success — legacy-trigger-agent',
    }) as HTMLButtonElement;
    fireEvent.change(screen.getByLabelText('Required trigger evidence note'), {
      target: { value: '   ' },
    });
    expect(confirmSuccess.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Required trigger evidence note'), {
      target: { value: 'Verified only legacy evidence.' },
    });
    expect(confirmSuccess.disabled).toBe(true);
    fireEvent.click(confirmSuccess);
    expect(api.applyRuntimeReconciliationAction).not.toHaveBeenCalled();
  });

  it.each([
    ['confirmed_failure', 'Confirm failure', 'mark_resolved'],
    ['release', 'Release hold', 'archive'],
  ] as const)(
    'sends the %s disposition with its exact action and operator evidence',
    async (triggerDisposition, buttonLabel, action) => {
      const heldTrigger: RuntimeReconciliationTask = {
        ...makeTask(`task-${triggerDisposition}`, 'typed-trigger-agent'),
        task_type: 'trigger',
        supported_trigger_dispositions: ['confirmed_success', 'confirmed_failure', 'release'],
        trigger_disposition_readiness: {
          schema: 'runtime_trigger_disposition_readiness.v1',
          ready: true,
          blocker: null,
          terminal_projection_id: 'terminal-projection-1',
        },
      };
      api.applyRuntimeReconciliationAction.mockResolvedValue({ ...heldTrigger, status: 'completed' });
      api.listRuntimeReconciliation.mockResolvedValue([]);

      render(
        <AdminRuntimeReconciliationSection
          initialTenantId="tenant-1"
          initialTasks={[heldTrigger]}
        />,
      );
      fireEvent.change(screen.getByLabelText('Required trigger evidence note'), {
        target: { value: `Verified evidence for ${triggerDisposition}.` },
      });
      fireEvent.click(screen.getByRole('button', { name: `${buttonLabel} — typed-trigger-agent` }));

      await waitFor(() => expect(api.applyRuntimeReconciliationAction).toHaveBeenCalledWith(
        `task-${triggerDisposition}`,
        {
          tenantId: 'tenant-1',
          action,
          reason: `Verified evidence for ${triggerDisposition}.`,
          triggerDisposition,
        },
      ));
    },
  );

  it.each([
    ['confirmed_success', 'Confirm success'],
    ['confirmed_failure', 'Confirm failure'],
    ['release', 'Release hold'],
  ] as const)(
    'uses the explicit atomic tool-effect acknowledgement for %s',
    async (triggerDisposition, buttonLabel) => {
      const heldTrigger: RuntimeReconciliationTask = {
        ...makeTask(`task-tool-${triggerDisposition}`, 'tool-trigger-agent'),
        task_type: 'trigger',
        tool_effect_reconciliation_required: true,
        supported_actions: ['acknowledge_tool_effect'],
        supported_trigger_dispositions: ['confirmed_success', 'confirmed_failure', 'release'],
        trigger_disposition_readiness: {
          schema: 'runtime_trigger_disposition_readiness.v1',
          ready: true,
          blocker: null,
          terminal_projection_id: 'terminal-projection-1',
        },
      };
      api.applyRuntimeReconciliationAction.mockResolvedValue({ ...heldTrigger, status: 'completed' });
      api.listRuntimeReconciliation.mockResolvedValue([]);

      render(
        <AdminRuntimeReconciliationSection
          initialTenantId="tenant-1"
          initialTasks={[heldTrigger]}
        />,
      );
      fireEvent.change(screen.getByLabelText('Required effect evidence note'), {
        target: { value: `Verified atomic evidence for ${triggerDisposition}.` },
      });
      fireEvent.click(screen.getByRole('button', { name: `${buttonLabel} — tool-trigger-agent` }));

      await waitFor(() => expect(api.applyRuntimeReconciliationAction).toHaveBeenCalledWith(
        `task-tool-${triggerDisposition}`,
        {
          tenantId: 'tenant-1',
          action: 'acknowledge_tool_effect',
          reason: `Verified atomic evidence for ${triggerDisposition}.`,
          triggerDisposition,
        },
      ));
    },
  );

  it('labels required inputs, scopes action names to the task, and announces API errors', async () => {
    api.applyRuntimeReconciliationAction.mockRejectedValue(new Error('operator evidence rejected'));
    const task = makeTask('task-accessible', 'writer-accessible');

    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[task]}
      />,
    );

    const tenant = screen.getByLabelText('Tenant ID') as HTMLInputElement;
    const evidence = screen.getByLabelText('Required reconciliation evidence note') as HTMLInputElement;
    expect(tenant.required).toBe(true);
    expect(tenant.getAttribute('aria-required')).toBe('true');
    expect(evidence.required).toBe(true);
    expect(evidence.getAttribute('aria-required')).toBe('true');
    const resolve = screen.getByRole('button', { name: 'Resolve — writer-accessible' }) as HTMLButtonElement;
    expect(resolve.disabled).toBe(true);

    fireEvent.change(evidence, { target: { value: 'Verified the canonical task evidence.' } });
    expect(resolve.disabled).toBe(false);
    fireEvent.click(resolve);

    expect((await screen.findByRole('alert')).textContent).toContain('operator evidence rejected');
  });

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
    // The initial auto-load fails too; wait for it to settle before operating
    // (the unified busy boundary disables controls while it is in flight).
    await screen.findByText('reload failed');
    fireEvent.click(screen.getByRole('button', { name: 'Repair projections' }));

    await screen.findByText('Projection repair finished: examined 2 candidates, repaired 0 projections.');
    await screen.findByText('reload failed');
    // Exactly two loads: the initial auto-load and the post-repair reload.
    expect(api.listRuntimeReconciliation).toHaveBeenCalledTimes(2);
  });

  it('shows the API failure and no success receipt when the repair itself fails', async () => {
    api.repairRuntimeReconciliationProjections.mockRejectedValue(new Error('platform_admin role required'));

    api.listRuntimeReconciliation.mockResolvedValue([]);

    render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" />);
    // The initial auto-load settles to an authoritative empty queue first.
    await screen.findByText('0 open items');
    fireEvent.click(screen.getByRole('button', { name: 'Repair projections' }));

    await screen.findByText('platform_admin role required');
    expect(screen.queryByText(/Projection repair finished/)).toBeNull();
    // A failed repair never reloads the queue; the only load is the initial one.
    expect(api.listRuntimeReconciliation).toHaveBeenCalledTimes(1);
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
    expect((screen.getByRole('button', { name: 'Resolve — writer-a' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Archive — writer-a' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Retry — writer-a' }) as HTMLButtonElement).disabled).toBe(true);
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
    // Wait for the initial auto-load to settle before operating.
    await screen.findByText('0 open items');
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
    // Wait for the initial auto-load to settle before operating.
    await screen.findByText('0 open items');
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

describe('AdminRuntimeReconciliationSection — truthful queue loading (RC-10B production finding)', () => {
  // Production reproduction (deployed HEAD ec509c86, /admin/platform-settings):
  // PlatformDashboard mounts <AdminRuntimeReconciliationSection /> with no
  // props, the tenant resolves from localStorage, but nothing loads on mount —
  // the seeded default [] renders as an authoritative "0 open items" /
  // "No runtime tasks need reconciliation." until a manual Refresh.

  it('auto-loads the resolved localStorage tenant on the no-props production mount path, with trimmed tenant and limit 50', async () => {
    localStorage.setItem('current_tenant_id', '  tenant-prod  ');
    const pending = deferred<RuntimeReconciliationTask[]>();
    api.listRuntimeReconciliation.mockReturnValue(pending.promise);

    render(<AdminRuntimeReconciliationSection />);

    // The real production mount path must load automatically — no manual
    // Refresh may be required to see the current tenant's queue.
    await waitFor(() =>
      expect(api.listRuntimeReconciliation).toHaveBeenCalledWith({ tenantId: 'tenant-prod', limit: 50 }),
    );
    expect(api.listRuntimeReconciliation).toHaveBeenCalledTimes(1);

    // While the load is deferred the UI must be truthful about not having
    // loaded yet — never a false authoritative empty.
    expect(screen.queryByText('0 open items')).toBeNull();
    expect(screen.queryByText('No runtime tasks need reconciliation.')).toBeNull();

    pending.resolve([makeTask('task-1', 'writer-1'), makeTask('task-2', 'writer-2')]);
    await screen.findByText('2 open items');
    await screen.findByText('writer-1');
    await screen.findByText('writer-2');
  });

  it('pins a rejected initial load as an unavailable/error state, never as an empty queue', async () => {
    localStorage.setItem('current_tenant_id', 'tenant-prod');
    api.listRuntimeReconciliation.mockRejectedValue(new Error('service unavailable'));

    render(<AdminRuntimeReconciliationSection />);

    await screen.findByText('service unavailable');
    expect(screen.queryByText('0 open items')).toBeNull();
    expect(screen.queryByText('No runtime tasks need reconciliation.')).toBeNull();
  });

  it('clears old-tenant rows immediately on tenant edit and loads the new tenant only via explicit Refresh', async () => {
    localStorage.setItem('current_tenant_id', 'tenant-1');
    api.listRuntimeReconciliation.mockResolvedValue([makeTask('task-a', 'writer-a')]);

    render(<AdminRuntimeReconciliationSection />);
    await screen.findByText('writer-a');
    expect(screen.getByText('1 open items')).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText('Tenant ID'), { target: { value: 'tenant-2' } });

    // Old-tenant truth disappears immediately; editing never fetches.
    expect(screen.queryByText('writer-a')).toBeNull();
    expect(screen.queryByText('1 open items')).toBeNull();
    expect(screen.queryByText('0 open items')).toBeNull();
    expect(screen.queryByText('No runtime tasks need reconciliation.')).toBeNull();
    expect(screen.getByText("Refresh to load this tenant's reconciliation queue.")).toBeTruthy();
    expect(api.listRuntimeReconciliation).toHaveBeenCalledTimes(1);

    // Manual Refresh remains the explicit load for the edited tenant.
    api.listRuntimeReconciliation.mockResolvedValue([makeTask('task-b', 'writer-b')]);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('writer-b');
    await screen.findByText('1 open items');
    expect(api.listRuntimeReconciliation).toHaveBeenLastCalledWith({ tenantId: 'tenant-2', limit: 50 });
  });

  it('issues exactly one initial request under React.StrictMode and the surviving setup consumes it', async () => {
    // frontend/src/main.tsx mounts the app under React.StrictMode, which
    // double-invokes mount effects (setup → synthetic cleanup → setup). The
    // initial load must still be single-flight: exactly one request per real
    // mount, with the surviving second setup consuming the shared result.
    localStorage.setItem('current_tenant_id', '  tenant-prod  ');
    const pending = deferred<RuntimeReconciliationTask[]>();
    api.listRuntimeReconciliation.mockReturnValue(pending.promise);

    render(
      <StrictMode>
        <AdminRuntimeReconciliationSection />
      </StrictMode>,
    );

    await waitFor(() => expect(api.listRuntimeReconciliation).toHaveBeenCalledTimes(1));
    expect(api.listRuntimeReconciliation).toHaveBeenCalledWith({ tenantId: 'tenant-prod', limit: 50 });

    // While deferred: still no false authoritative empty.
    expect(screen.queryByText('0 open items')).toBeNull();
    expect(screen.queryByText('No runtime tasks need reconciliation.')).toBeNull();

    pending.resolve([makeTask('task-strict', 'writer-strict')]);
    await screen.findByText('1 open items');
    await screen.findByText('writer-strict');
  });

  it('keeps explicitly seeded initialTasks authoritative without an automatic duplicate fetch', () => {
    localStorage.setItem('current_tenant_id', 'tenant-1');

    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[makeTask('task-seed', 'writer-seed')]}
      />,
    );

    expect(screen.getByText('1 open items')).toBeTruthy();
    expect(screen.getByText('writer-seed')).toBeTruthy();
    expect(api.listRuntimeReconciliation).not.toHaveBeenCalled();
  });

  it('treats an explicitly seeded empty list as an authoritative empty queue without fetching', () => {
    render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" initialTasks={[]} />);

    expect(screen.getByText('0 open items')).toBeTruthy();
    expect(screen.getByText('No runtime tasks need reconciliation.')).toBeTruthy();
    expect(api.listRuntimeReconciliation).not.toHaveBeenCalled();
  });
});
