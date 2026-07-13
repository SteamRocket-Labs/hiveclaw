// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  adminApi,
  type RuntimeNotificationDeliveryReconciliation,
} from '../../api/domains/admin';

import AdminRuntimeReconciliationSection, {
  buildFrameDecisions,
  visibleOperationRoots,
} from './AdminRuntimeReconciliationSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function delivery(
  sourceKind: string,
  retryable = true,
): RuntimeNotificationDeliveryReconciliation {
  return {
    delivery_id: `${sourceKind}-delivery`,
    tenant_id: 'tenant-1',
    source_kind: sourceKind,
    source_run_id: `${sourceKind}-run`,
    agent_id: sourceKind === 'workflow_completion' ? 'workflow-target-agent-full' : undefined,
    parent_agent_id: sourceKind === 'workflow_completion' ? undefined : 'parent-agent-full',
    parent_user_id: sourceKind === 'workflow_completion' ? undefined : 'parent-user-full',
    parent_session_id: sourceKind === 'workflow_completion' ? undefined : 'parent-session-full',
    child_session_id: sourceKind === 'workflow_completion' ? undefined : 'child-session-full',
    status: 'dead_letter',
    execution_terminal_status: sourceKind === 'system_plan_run' ? 'resumable' : 'completed',
    delivery_only: true,
    does_not_rerun_execution: true,
    retryable,
    attempt_count: 8,
    last_error: `${sourceKind} delivery failed`,
    authority_snapshot: sourceKind === 'workflow_completion' ? {
      valid: true,
      tenant_id: 'tenant-1',
      agent_id: 'workflow-target-agent-full',
    } : {
      valid: true,
      tenant_id: 'tenant-1',
      parent_agent_id: 'parent-agent-full',
      parent_user_id: 'parent-user-full',
      parent_session_id: 'parent-session-full',
      child_session_id: 'child-session-full',
    },
  };
}

describe('AdminRuntimeReconciliationSection', () => {
  it('renders runtime reconciliation queue rows and fail-closed actions', () => {
    const markup = renderToStaticMarkup(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[
          {
            task_id: 'task-1',
            tenant_id: 'tenant-1',
            task_type: 'delegation',
            status: 'needs_reconciliation',
            child_agent_name: 'writer',
            reason: 'missing_completion_journal',
            side_effect_risk: 'mutating',
            retry_allowed: false,
            created_at: '2026-06-16T00:00:00Z',
            recovery_evidence: {
              schema: 'runtime_recovery_evidence.v1',
              digest: 'a'.repeat(64),
              evidence_complete: true,
              incomplete_reasons: [],
              frames: [
                {
                  runtime_task_id: '11111111-2222-3333-4444-555555555555',
                  tool_name: 'send_email',
                  tool_call_id: 'call-email',
                  status: 'needs_reconciliation',
                  source: 'prior_run',
                },
              ],
              targets: [
                {
                  source: 'prior_run',
                  runtime_task_id: '11111111-2222-3333-4444-555555555555',
                  agent_id: 'agent-1',
                  session_id: 'session-1',
                  expected_sha256: 'a'.repeat(64),
                  expected_checkpoint_seq: 3,
                },
              ],
            },
          },
        ]}
      />,
    );

    expect(markup).toContain('Runtime Reconciliation');
    expect(markup).toContain('missing_completion_journal');
    expect(markup).toContain('writer');
    expect(markup).toContain('Archive');
    expect(markup).not.toContain('Retry');
    expect(markup).toContain('send_email');
    expect(markup).toContain('call-email');
    expect(markup).toContain('11111111');
    expect(markup).toContain('aaaaaaaaaaaa');
    expect(markup).toContain('Describe the evidence checked for every recovery target');
    expect(markup).toContain('I verified every listed target and frame');
    expect(markup).toContain('disabled=""');
    expect(markup).not.toContain('operator mark_resolved');
  });

  it('disables all actions when canonical evidence is incomplete', () => {
    const markup = renderToStaticMarkup(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[{
          task_id: 'task-incomplete',
          status: 'needs_reconciliation',
          recovery_evidence: {
            schema: 'runtime_recovery_evidence.v1',
            digest: 'b'.repeat(64),
            evidence_complete: false,
            incomplete_reasons: ['no_recovery_frames'],
            targets: [],
            frames: [],
          },
        }]}
      />,
    );

    expect(markup).toContain('Evidence incomplete');
    expect(markup).toContain('no_recovery_frames');
    expect(markup.match(/disabled=""/g)?.length).toBeGreaterThanOrEqual(3);
  });

  it('builds an exact action-bound decision for every canonical frame', () => {
    expect(buildFrameDecisions({
      schema: 'runtime_recovery_evidence.v1',
      digest: 'a'.repeat(64),
      evidence_complete: true,
      incomplete_reasons: [],
      targets: [],
      frames: [
        {
          runtime_task_id: 'run-b',
          tool_call_id: 'call-b',
          tool_name: 'write_file',
          source: 'legacy',
        },
        {
          runtime_task_id: 'run-a',
          tool_call_id: 'call-a',
          tool_name: 'send_email',
          source: 'prior_run',
        },
      ],
    }, 'archive')).toEqual([
      {
        runtime_task_id: 'run-a',
        tool_call_id: 'call-a',
        tool_name: 'send_email',
        decision: 'archive',
      },
      {
        runtime_task_id: 'run-b',
        tool_call_id: 'call-b',
        tool_name: 'write_file',
        decision: 'archive',
      },
    ]);
  });

  it('shows only the operation group root and offers only Resume for prepared work', () => {
    const operation = {
      schema: 'runtime_reconciliation_operation.v2',
      operation_id: 'operation-prepared',
      status: 'prepared' as const,
      action: 'mark_resolved' as const,
      reason: 'verified immutable evidence',
      actor_user_id: 'operator-original',
      evidence_digest: 'c'.repeat(64),
      frame_decisions: [{
        runtime_task_id: 'carrier',
        tool_call_id: 'call-1',
        tool_name: 'send_email',
        decision: 'mark_resolved' as const,
      }],
      group_root_task_id: 'carrier',
      group_member_task_ids: ['prior', 'carrier'],
    };
    const evidence = {
      schema: 'runtime_recovery_evidence.v1',
      digest: 'c'.repeat(64),
      evidence_complete: true,
      incomplete_reasons: [],
      targets: [],
      frames: [{
        runtime_task_id: 'carrier',
        tool_call_id: 'call-1',
        tool_name: 'send_email',
        source: 'legacy',
      }],
    };
    const tasks = visibleOperationRoots([
      { task_id: 'prior', status: 'needs_reconciliation', recovery_evidence: evidence, reconciliation_operation: operation },
      { task_id: 'carrier', status: 'needs_reconciliation', recovery_evidence: evidence, reconciliation_operation: operation },
    ]);
    const markup = renderToStaticMarkup(
      <AdminRuntimeReconciliationSection initialTenantId="tenant-1" initialTasks={tasks} />,
    );

    expect(tasks.map((task) => task.task_id)).toEqual(['carrier']);
    expect(markup).toContain('operation-prepared');
    expect(markup).toContain('verified immutable evidence');
    expect(markup).toContain('Resume');
    expect(markup).not.toContain('>Resolve<');
    expect(markup).not.toContain('>Archive<');
  });

  it('labels dead-letter completion retries as delivery-only and never as execution retry', () => {
    const markup = renderToStaticMarkup(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[]}
        initialDeliveries={[{
          delivery_id: 'delivery-1',
          tenant_id: 'tenant-1',
          source_kind: 'subagent',
          source_run_id: 'run-terminal',
          status: 'dead_letter',
          execution_terminal_status: 'completed',
          delivery_only: true,
          retryable: true,
          last_error: 'completion target authority no longer resolves',
          attempt_count: 8,
        }]}
      />,
    );

    expect(markup).toContain('Completion delivery reconciliation');
    expect(markup).toContain('Delivery only');
    expect(markup).toContain('Does not rerun the completed Subagent');
    expect(markup).toContain('run-terminal');
    expect(markup).toContain('Retry delivery');
    expect(markup).not.toContain('Retry execution');
  });

  it('offers delivery-only retry for a resumable System Plan projection', () => {
    const markup = renderToStaticMarkup(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[]}
        initialDeliveries={[{
          delivery_id: 'system-plan-delivery-1',
          tenant_id: 'tenant-1',
          source_kind: 'system_plan_run',
          source_run_id: 'system-plan-runtime-task-1',
          task_type: 'system_plan_run',
          status: 'dead_letter',
          execution_terminal_status: 'resumable',
          delivery_only: true,
          does_not_rerun_execution: true,
          retryable: true,
          last_error: 'session projection channel unavailable',
          attempt_count: 8,
        }]}
      />,
    );

    expect(markup).toContain('system_plan_run');
    expect(markup).toContain('Execution truth');
    expect(markup).toContain('resumable');
    expect(markup).toContain('Delivery only');
    expect(markup).toContain('Does not rerun the source execution');
    expect(markup).toContain('Retry delivery');
    expect(markup).not.toContain('Retry execution');
  });

  it('renders Workflow coordination signal failures as an independent delivery-only source', () => {
    const markup = renderToStaticMarkup(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[]}
        initialDeliveries={[{
          delivery_id: 'workflow-delivery-1',
          tenant_id: 'tenant-1',
          source_kind: 'workflow_completion',
          source_run_id: 'workflow-run-terminal',
          agent_id: 'workflow-target-agent-full',
          status: 'dead_letter',
          execution_terminal_status: 'completed',
          delivery_only: true,
          does_not_rerun_execution: true,
          retryable: true,
          last_error: 'coordination signal target unavailable',
          attempt_count: 8,
          authority_snapshot: {
            valid: true,
            tenant_id: 'tenant-1',
            agent_id: 'workflow-target-agent-full',
          },
        }]}
      />,
    );

    expect(markup).toContain('workflow_completion');
    expect(markup).toContain('workflow-run-terminal');
    expect(markup).toContain('workflow-target-agent-full');
    expect(markup).toContain('Authority valid');
    expect(markup).toContain('true');
    expect(markup).toContain('&quot;agent_id&quot;:&quot;workflow-target-agent-full&quot;');
    expect(markup).toContain('Does not rerun the completed Workflow');
    expect(markup).toContain('Retry delivery');
    expect(markup).not.toContain('Retry execution');
  });

  it('shows the complete generic delivery target authority before confirmation', () => {
    const markup = renderToStaticMarkup(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialDeliveries={[delivery('subagent')]}
      />,
    );

    for (const value of [
      'tenant-1',
      'parent-agent-full',
      'parent-user-full',
      'parent-session-full',
      'child-session-full',
      'Authority valid',
      'true',
    ]) {
      expect(markup).toContain(value);
    }
  });

  it('refreshes runtime reconciliation plus both delivery queues', async () => {
    const listTasks = vi.spyOn(adminApi, 'listRuntimeReconciliation').mockResolvedValue([{
      task_id: 'task-from-refresh',
      status: 'needs_reconciliation',
      reason: 'refresh-task-visible',
    }]);
    const listGeneric = vi.spyOn(adminApi, 'listRuntimeNotificationDeliveries')
      .mockResolvedValue([delivery('subagent')]);
    const listWorkflow = vi.spyOn(adminApi, 'listWorkflowCompletionDeliveries')
      .mockResolvedValue([delivery('workflow_completion')]);

    render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" />);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() => expect(listTasks).toHaveBeenCalledWith({ tenantId: 'tenant-1', limit: 50 }));
    expect(listGeneric).toHaveBeenCalledWith({ tenantId: 'tenant-1', limit: 50 });
    expect(listWorkflow).toHaveBeenCalledWith({ tenantId: 'tenant-1', limit: 50 });
    expect(await screen.findByText('refresh-task-visible')).toBeTruthy();
    expect(screen.getByText('subagent-run')).toBeTruthy();
    expect(screen.getByText('workflow_completion-run')).toBeTruthy();
  });

  it('renders a load error without replacing the existing queue', async () => {
    vi.spyOn(adminApi, 'listRuntimeReconciliation').mockRejectedValue(new Error('runtime queue unavailable'));
    vi.spyOn(adminApi, 'listRuntimeNotificationDeliveries').mockResolvedValue([]);
    vi.spyOn(adminApi, 'listWorkflowCompletionDeliveries').mockResolvedValue([]);

    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[{ task_id: 'existing-task', status: 'needs_reconciliation', reason: 'existing-visible' }]}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(await screen.findByText('runtime queue unavailable')).toBeTruthy();
    expect(screen.getByText('existing-visible')).toBeTruthy();
  });

  it.each([
    ['subagent', 'generic'],
    ['workflow_completion', 'workflow'],
  ] as const)(
    'dispatches %s retry to the %s endpoint and refreshes both delivery queues',
    async (sourceKind, _endpointKind) => {
    const item = delivery(sourceKind);
    const retryGeneric = vi.spyOn(adminApi, 'retryRuntimeNotificationDelivery').mockResolvedValue(item);
    const retryWorkflow = vi.spyOn(adminApi, 'retryWorkflowCompletionDelivery').mockResolvedValue(item);
    const listGeneric = vi.spyOn(adminApi, 'listRuntimeNotificationDeliveries').mockResolvedValue([]);
    const listWorkflow = vi.spyOn(adminApi, 'listWorkflowCompletionDeliveries').mockResolvedValue([]);

    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialDeliveries={[item]}
      />,
    );
    fireEvent.change(screen.getByLabelText('Delivery retry evidence'), {
      target: { value: 'delivery authority repaired' },
    });
    fireEvent.click(screen.getByLabelText(
      'I confirm this retries delivery only and will not rerun execution',
    ));
    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }));

    const expectedRetry = sourceKind === 'workflow_completion' ? retryWorkflow : retryGeneric;
    const unexpectedRetry = sourceKind === 'workflow_completion' ? retryGeneric : retryWorkflow;
    await waitFor(() => expect(expectedRetry).toHaveBeenCalledWith(item.delivery_id, {
      tenantId: 'tenant-1',
      reason: 'delivery authority repaired',
      confirmed: true,
    }));
    expect(unexpectedRetry).not.toHaveBeenCalled();
    expect(listGeneric).toHaveBeenCalledWith({ tenantId: 'tenant-1', limit: 50 });
    expect(listWorkflow).toHaveBeenCalledWith({ tenantId: 'tenant-1', limit: 50 });
    },
  );

  it.each([
    ['subagent', 'generic'],
    ['workflow_completion', 'workflow'],
  ] as const)(
    'removes a successfully retried %s row before a failed %s queue refresh can repeat the mutation',
    async (sourceKind, _queueKind) => {
      let rejectRefresh!: (reason: Error) => void;
      const blockedRefresh = new Promise<RuntimeNotificationDeliveryReconciliation[]>((_resolve, reject) => {
        rejectRefresh = reject;
      });
      const item = delivery(sourceKind);
      const retry = sourceKind === 'workflow_completion'
        ? vi.spyOn(adminApi, 'retryWorkflowCompletionDelivery').mockResolvedValue({ ...item, status: 'pending', retryable: false })
        : vi.spyOn(adminApi, 'retryRuntimeNotificationDelivery').mockResolvedValue({ ...item, status: 'pending', retryable: false });
      vi.spyOn(adminApi, 'listRuntimeNotificationDeliveries').mockImplementation(() => (
        sourceKind === 'subagent' ? blockedRefresh : Promise.resolve([])
      ));
      vi.spyOn(adminApi, 'listWorkflowCompletionDeliveries').mockImplementation(() => (
        sourceKind === 'workflow_completion' ? blockedRefresh : Promise.resolve([])
      ));

      render(
        <AdminRuntimeReconciliationSection
          initialTenantId="tenant-1"
          initialDeliveries={[item]}
        />,
      );
      fireEvent.change(screen.getByLabelText('Delivery retry evidence'), {
        target: { value: 'verified repaired delivery authority' },
      });
      fireEvent.click(screen.getByLabelText(
        'I confirm this retries delivery only and will not rerun execution',
      ));
      fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }));

      await waitFor(() => expect(retry).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(screen.queryByText(item.source_run_id)).toBeNull());
      expect(screen.queryByRole('button', { name: 'Retry delivery' })).toBeNull();

      rejectRefresh(new Error(`${sourceKind} refresh unavailable`));
      expect(await screen.findByText(
        `Delivery was retried, but refreshing the queue failed: ${sourceKind} refresh unavailable`,
      )).toBeTruthy();
      expect(retry).toHaveBeenCalledTimes(1);
    },
  );

  it('refreshes the runtime queue after a successful runtime action', async () => {
    const task = {
      task_id: 'runtime-action-success',
      status: 'needs_reconciliation',
      reason: 'runtime-action-visible',
      recovery_evidence: {
        schema: 'runtime_recovery_evidence.v1',
        digest: 'f'.repeat(64),
        evidence_complete: true,
        incomplete_reasons: [],
        targets: [{
          source: 'current_run',
          runtime_task_id: 'runtime-action-success',
          agent_id: 'agent-1',
          session_id: 'session-1',
        }],
        frames: [{
          runtime_task_id: 'runtime-action-success',
          tool_call_id: 'call-1',
          tool_name: 'send_email',
          source: 'current_run',
        }],
      },
    };
    const apply = vi.spyOn(adminApi, 'applyRuntimeReconciliationAction').mockResolvedValue(task);
    const list = vi.spyOn(adminApi, 'listRuntimeReconciliation').mockResolvedValue([]);
    render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" initialTasks={[task]} />);
    fireEvent.change(screen.getByLabelText('Operator evidence'), {
      target: { value: 'verified immutable runtime evidence' },
    });
    fireEvent.click(screen.getByLabelText('I verified every listed target and frame'));
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }));

    await waitFor(() => expect(apply).toHaveBeenCalled());
    expect(list).toHaveBeenCalledWith({ tenantId: 'tenant-1', limit: 50 });
    await waitFor(() => expect(screen.queryByText('runtime-action-visible')).toBeNull());
  });

  it.each([
    ['runtime', 'runtime reconciliation conflict', 'Resolve'],
    ['subagent', 'delivery retry forbidden', 'Retry delivery'],
    ['workflow_completion', 'coordination network unavailable', 'Retry delivery'],
  ] as const)(
    'shows %s action errors and preserves the queue row',
    async (kind, message, buttonName) => {
      if (kind === 'runtime') {
        const task = {
          task_id: 'runtime-error-task',
          status: 'needs_reconciliation',
          reason: 'runtime-row-preserved',
          recovery_evidence: {
            schema: 'runtime_recovery_evidence.v1',
            digest: 'e'.repeat(64),
            evidence_complete: true,
            incomplete_reasons: [],
            targets: [{
              source: 'current_run',
              runtime_task_id: 'runtime-error-task',
              agent_id: 'agent-1',
              session_id: 'session-1',
            }],
            frames: [{
              runtime_task_id: 'runtime-error-task',
              tool_call_id: 'call-1',
              tool_name: 'send_email',
              source: 'current_run',
            }],
          },
        };
        vi.spyOn(adminApi, 'applyRuntimeReconciliationAction').mockRejectedValue(new Error(message));
        const list = vi.spyOn(adminApi, 'listRuntimeReconciliation');
        render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" initialTasks={[task]} />);
        fireEvent.change(screen.getByLabelText('Operator evidence'), {
          target: { value: 'verified immutable runtime evidence' },
        });
        fireEvent.click(screen.getByLabelText('I verified every listed target and frame'));
        fireEvent.click(screen.getByRole('button', { name: buttonName }));
        expect(await screen.findByText(message)).toBeTruthy();
        expect(screen.getByText('runtime-row-preserved')).toBeTruthy();
        expect(list).not.toHaveBeenCalled();
        return;
      }

      const item = delivery(kind);
      const retry = kind === 'workflow_completion'
        ? vi.spyOn(adminApi, 'retryWorkflowCompletionDelivery')
        : vi.spyOn(adminApi, 'retryRuntimeNotificationDelivery');
      retry.mockRejectedValue(new Error(message));
      const listGeneric = vi.spyOn(adminApi, 'listRuntimeNotificationDeliveries');
      const listWorkflow = vi.spyOn(adminApi, 'listWorkflowCompletionDeliveries');
      render(<AdminRuntimeReconciliationSection initialTenantId="tenant-1" initialDeliveries={[item]} />);
      fireEvent.change(screen.getByLabelText('Delivery retry evidence'), {
        target: { value: 'verified repaired delivery authority' },
      });
      fireEvent.click(screen.getByLabelText(
        'I confirm this retries delivery only and will not rerun execution',
      ));
      fireEvent.click(screen.getByRole('button', { name: buttonName }));
      expect(await screen.findByText(message)).toBeTruthy();
      expect(screen.getByText(item.source_run_id)).toBeTruthy();
      expect(listGeneric).not.toHaveBeenCalled();
      expect(listWorkflow).not.toHaveBeenCalled();
    },
  );

  it('keeps retry disabled when the server marks a delivery non-retryable', () => {
    const retryGeneric = vi.spyOn(adminApi, 'retryRuntimeNotificationDelivery');
    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialDeliveries={[delivery('subagent', false)]}
      />,
    );
    fireEvent.change(screen.getByLabelText('Delivery retry evidence'), {
      target: { value: 'delivery authority repaired' },
    });
    fireEvent.click(screen.getByLabelText(
      'I confirm this retries delivery only and will not rerun execution',
    ));

    const retryButton = screen.getByRole('button', { name: 'Retry delivery' }) as HTMLButtonElement;
    expect(retryButton.disabled).toBe(true);
    fireEvent.click(retryButton);
    expect(retryGeneric).not.toHaveBeenCalled();
  });

  it.each([
    ['subagent', 'generic'],
    ['workflow_completion', 'workflow'],
  ] as const)('keeps %s retry disabled when its live authority snapshot is invalid', (sourceKind, _authorityKind) => {
    const retryGeneric = vi.spyOn(adminApi, 'retryRuntimeNotificationDelivery');
    const retryWorkflow = vi.spyOn(adminApi, 'retryWorkflowCompletionDelivery');
    const item = delivery(sourceKind, true);
    item.authority_snapshot = { ...item.authority_snapshot, valid: false };
    render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialDeliveries={[item]}
      />,
    );
    fireEvent.change(screen.getByLabelText('Delivery retry evidence'), {
      target: { value: 'delivery authority repaired' },
    });
    fireEvent.click(screen.getByLabelText(
      'I confirm this retries delivery only and will not rerun execution',
    ));

    const retryButton = screen.getByRole('button', { name: 'Retry delivery' }) as HTMLButtonElement;
    expect(retryButton.disabled).toBe(true);
    fireEvent.click(retryButton);
    expect(retryGeneric).not.toHaveBeenCalled();
    expect(retryWorkflow).not.toHaveBeenCalled();
  });

  it('shows every manifest state and the complete target CAS authority', () => {
    const states = ['present', 'missing', 'corrupt', 'nonregular', 'identity_mismatch'] as const;
    const { container } = render(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[{
          task_id: 'manifest-authority-task',
          status: 'needs_reconciliation',
          recovery_evidence: {
            schema: 'runtime_recovery_evidence.v1',
            digest: 'd'.repeat(64),
            evidence_complete: true,
            incomplete_reasons: [],
            frames: [],
            targets: states.map((state, index) => ({
              source: 'current_run',
              runtime_task_id: `runtime-${state}`,
              agent_id: `agent-${state}`,
              session_id: `session-${state}`,
              expected_manifest_state: state,
              expected_manifest_ref: `runtime_artifacts/recovery/${state}.json`,
              expected_sha256: `${state}-full-sha256`,
              expected_checkpoint_seq: index + 1,
              expected_claim_version: index + 11,
              expected_claim_worker_id: `worker-${state}`,
            })),
          },
        }]}
      />,
    );

    const text = container.textContent || '';
    for (const [index, state] of states.entries()) {
      expect(text).toContain(`state ${state}`);
      expect(text).toContain(`ref runtime_artifacts/recovery/${state}.json`);
      expect(text).toContain(`sha256 ${state}-full-sha256`);
      expect(text).toContain(`checkpoint ${index + 1}`);
      expect(text).toContain(`claim version ${index + 11}`);
      expect(text).toContain(`claim worker worker-${state}`);
    }
  });
});
