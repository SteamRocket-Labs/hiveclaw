import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import AgentWorkflowsSection, {
  buildWorkflowStartOptions,
  runStatusTone,
  type WorkflowPlanHandoffFields,
} from './AgentWorkflowsSection';
import type { WorkflowDefinitionRecord, WorkflowPreview, WorkflowRunSummary } from '../../api/domains/workflows';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => {
      const leaf = key.split('.').pop() ?? key;
      if (options && Object.keys(options).length > 0) {
        return `${leaf}:${Object.values(options).join(',')}`;
      }
      return leaf;
    },
    i18n: { language: 'en' },
  }),
}));

const runRows: WorkflowRunSummary[] = [
  {
    run_id: 'run-unpromoted',
    status: 'completed',
    name: 'contract-batch',
    description: 'OCR → extract → risk table',
    definition_source: 'ephemeral',
    definition_hash: 'h1',
    created_at: '2026-06-05T12:00:00Z',
    completed_at: '2026-06-05T12:05:00Z',
    steps_total: 3,
    steps_done: 3,
    steps_failed: 0,
    promoted_definition_id: null,
  },
  {
    run_id: 'run-promoted',
    status: 'completed',
    name: 'weekly-report',
    description: 'collect → summarize → send draft',
    definition_source: 'ephemeral',
    definition_hash: 'h2',
    created_at: '2026-06-04T09:00:00Z',
    completed_at: '2026-06-04T09:03:00Z',
    steps_total: 2,
    steps_done: 2,
    steps_failed: 0,
    promoted_definition_id: 'def-1',
  },
];

const definitionRows: WorkflowDefinitionRecord[] = [
  {
    id: 'def-1',
    name: 'weekly-report',
    description: 'collect → summarize → send draft',
    definition_version: 1,
    definition_hash: 'h2',
    status: 'active',
    visibility_scope: 'agent',
    owner_type: 'agent',
    owner_id: 'agent-1',
    call_policy: null,
    promoted_from_run_id: 'run-promoted',
  },
];

const suggestionRows = [{ definition_hash: 'h1', name: 'contract-batch', run_count: 3, sample_run_ids: ['run-unpromoted'] }];

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey, enabled }: { queryKey: unknown[]; enabled?: boolean }) => {
    if (enabled === false) return { data: undefined, isLoading: false };
    const key = String(queryKey[0]);
    if (key === 'workflow-runs') return { data: runRows, isLoading: false };
    if (key === 'workflow-definitions') return { data: definitionRows, isLoading: false };
    if (key === 'workflow-promote-suggestions') return { data: suggestionRows, isLoading: false };
    return { data: undefined, isLoading: false };
  },
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useMutation: () => ({ mutate: vi.fn(), isPending: false }),
}));

describe('AgentWorkflowsSection (asset view)', () => {
  it('renders templates with description, status, and provenance', () => {
    const html = renderToStaticMarkup(<AgentWorkflowsSection agentId="agent-1" canManage={true} />);
    expect(html).toContain('weekly-report');
    expect(html).toContain('collect → summarize → send draft');
    expect(html).toContain('statusActive');
    expect(html).toContain('visibilityAgent');
    expect(html).toContain('promotedFrom');
  });

  it('renders run history with promote action for proven unpromoted runs', () => {
    const html = renderToStaticMarkup(<AgentWorkflowsSection agentId="agent-1" canManage={true} />);
    expect(html).toContain('contract-batch');
    expect(html).toContain('OCR → extract → risk table');
    expect(html).toContain('workflow-promote-run-unpromoted'); // promote button testid
    expect(html).toContain('>promoted<'); // 已固化 badge on the promoted run
    expect(html).toContain('stepsProgress:3,3');
  });

  it('hides promote actions without manage access', () => {
    const html = renderToStaticMarkup(<AgentWorkflowsSection agentId="agent-1" canManage={false} />);
    expect(html).not.toContain('workflow-promote-run-unpromoted');
    expect(html).not.toContain('approvePromotion');
  });

  it('surfaces the repeated-run suggestion banner', () => {
    const html = renderToStaticMarkup(<AgentWorkflowsSection agentId="agent-1" canManage={true} />);
    expect(html).toContain('workflow-suggestions-banner');
    expect(html).toContain('suggestionBody:contract-batch,3');
  });

  it('keeps the manual JSON flow collapsed behind the advanced toggle', () => {
    const html = renderToStaticMarkup(<AgentWorkflowsSection agentId="agent-1" canManage={true} />);
    expect(html).toContain('workflow-advanced-toggle');
    expect(html).not.toContain('workflow-definition-input');
  });
});

describe('runStatusTone', () => {
  it('maps run statuses onto badge tones', () => {
    expect(runStatusTone('completed')).toBe('ok');
    expect(runStatusTone('failed')).toBe('error');
    expect(runStatusTone('killed')).toBe('error');
    expect(runStatusTone('running')).toBe('warn');
    expect(runStatusTone('suspended')).toBe('warn');
    expect(runStatusTone('pending')).toBe('muted');
  });
});

function preview(risk: WorkflowPreview['risk']): WorkflowPreview {
  return {
    definition_hash: 'hash-1',
    risk,
    risk_reasons: risk === 'high' ? ['external effects'] : [],
    planned_leaf_calls: 1,
    budget_tokens: 1000,
  };
}

const emptyHandoff: WorkflowPlanHandoffFields = {
  confirmedPlanId: '',
  planVersion: '',
  planHash: '',
};

describe('buildWorkflowStartOptions', () => {
  it('does not require a plan handoff for low-risk workflows', () => {
    expect(buildWorkflowStartOptions(preview('low'), emptyHandoff)).toEqual({});
  });

  it('blocks high-risk workflows until a confirmed plan handoff is present', () => {
    expect(buildWorkflowStartOptions(preview('high'), emptyHandoff)).toBeNull();
  });

  it('threads confirmed plan fields for high-risk starts', () => {
    expect(
      buildWorkflowStartOptions(preview('high'), {
        confirmedPlanId: ' plan-1 ',
        planVersion: '2',
        planHash: ' hash-plan ',
      }),
    ).toEqual({
      confirmedPlanId: 'plan-1',
      planVersion: 2,
      planHash: 'hash-plan',
    });
  });
});
