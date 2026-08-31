import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import AdminRuntimeReconciliationSection from './AdminRuntimeReconciliationSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

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
          },
        ]}
      />,
    );

    expect(markup).toContain('Runtime Reconciliation');
    expect(markup).toContain('missing_completion_journal');
    expect(markup).toContain('writer');
    expect(markup).toContain('Archive');
    expect(markup).not.toContain('Retry');
  });

  it('exposes non-retryable trigger and heartbeat restart blockers to platform operators', () => {
    const markup = renderToStaticMarkup(
      <AdminRuntimeReconciliationSection
        initialTenantId="tenant-1"
        initialTasks={[
          {
            task_id: 'trigger-task',
            tenant_id: 'tenant-1',
            task_type: 'trigger',
            status: 'needs_reconciliation',
            reason: 'session_bound_mutating_trigger',
            side_effect_risk: 'mutating',
            retry_allowed: false,
            supported_trigger_dispositions: ['confirmed_success', 'confirmed_failure', 'release'],
            trigger_disposition_readiness: {
              schema: 'runtime_trigger_disposition_readiness.v1',
              ready: false,
              blocker: 'terminal_projection_pending',
              terminal_projection_id: 'projection-1',
            },
            trace_id: 'trace-static',
            created_at: '2026-07-24T00:00:00Z',
          },
          {
            task_id: 'heartbeat-task',
            tenant_id: 'tenant-1',
            task_type: 'heartbeat',
            status: 'needs_reconciliation',
            reason: 'direct_core_audit_session_bound',
            side_effect_risk: 'mutating',
            retry_allowed: false,
            created_at: '2026-07-24T00:01:00Z',
          },
        ]}
      />,
    );

    expect(markup).toContain('2 open items');
    expect(markup).toContain('trigger');
    expect(markup).toContain('session_bound_mutating_trigger');
    expect(markup).toContain('heartbeat');
    expect(markup).toContain('direct_core_audit_session_bound');
    expect(markup).toContain('Confirm success');
    expect(markup).toContain('Confirm failure');
    expect(markup).toContain('Release hold');
    expect(markup).toContain('Disposition actions blocked: terminal_projection_pending');
    expect(markup).toContain('Trace ID');
    expect(markup).toContain('trace-static');
    expect(markup).toContain('aria-required="true"');
    expect(markup).toContain('Resolve');
    expect(markup).toContain('Archive');
    expect(markup).not.toContain('Retry');
  });
});
