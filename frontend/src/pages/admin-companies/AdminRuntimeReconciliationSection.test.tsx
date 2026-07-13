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
});
