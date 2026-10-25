// @vitest-environment jsdom

import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback: string) => fallback, i18n: { language: 'en' } }),
}));

import PlanCard from './PlanCard';
import type { PlanRequest } from '../../api/domains/plans';

afterEach(cleanup);

function plan(status: string): PlanRequest {
  return {
    id: 'plan-1', agent_id: 'agent-1', session_id: 'work-1', status: 'planning', plan_version: 1,
    tenant_id: 'tenant-1', runtime_task_id: null, requested_by_user_id: 'user-1',
    plan_hash: null, plan_markdown_path: null, handoff_status: 'not_started', handoff_payload: null,
    confirmed_by_user_id: null, confirmed_at: null, rejected_by_user_id: null, rejected_at: null,
    superseded_by_plan_id: null, expires_at: null, created_at: null, updated_at: null,
    plan_json: {}, source: 'system_plan_run', original_request: 'Prepare a plan.', intent_type: 'long_task',
    metadata: { system_plan_authoring: { status },
      planning_errors: ['The existing request needs operator reconciliation and will not be replayed.'] },
  };
}

describe('native plan authoring recovery', () => {
  it.each(['queued', 'running', 'suspended', 'needs_reconciliation'])(
    'opens the original work for %s instead of dispatching another generation', (status) => {
      render(<PlanCard agentId="agent-1" plan={plan(status)} />);
      expect(screen.getByRole('link', { name: 'Open original work' }).getAttribute('href'))
        .toBe('/agents/agent-1/sessions/work-1');
      expect(screen.queryByRole('button', { name: 'Retry plan generation' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Implement this plan' })).toBeNull();
      if (status === 'needs_reconciliation') {
        expect(screen.getByText('Needs admin review')).toBeTruthy();
        expect(screen.getByText(/will not be replayed/)).toBeTruthy();
      }
    },
  );
});
