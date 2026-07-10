/**
 * §9 P12 red tests: workflow domain adapter — preview / run status /
 * registered list / trigger pin-mismatch classification.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  activateWorkflowDefinition,
  cancelWorkflowRun,
  classifyTriggerPin,
  getWorkflowPreview,
  getWorkflowRun,
  listWorkflowDefinitions,
  previewWorkflow,
  repairWorkflowRun,
  startWorkflow,
  type WorkflowDefinitionRecord,
} from './workflows';

const _localStore: Record<string, string> = {};
const _localStorageStub = {
  getItem: (key: string) => (key in _localStore ? _localStore[key] : null),
  setItem: (key: string, value: string) => {
    _localStore[key] = value;
  },
  removeItem: (key: string) => {
    delete _localStore[key];
  },
  clear: () => {
    for (const key of Object.keys(_localStore)) delete _localStore[key];
  },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>;

beforeEach(() => {
  vi.stubGlobal('localStorage', _localStorageStub);
  _localStorageStub.setItem('token', 'test-token');
  fetchMock = vi.fn<typeof fetch>();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  _localStorageStub.clear();
});

function requestOf(callIndex = 0): { url: string; init: RequestInit } {
  const call = fetchMock.mock.calls[callIndex];
  return { url: String(call?.[0] ?? ''), init: (call?.[1] ?? {}) as RequestInit };
}

describe('previewWorkflow', () => {
  it('POSTs definition+args and returns preview binding/confirmation notes', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        preview_id: 'preview-1',
        definition_hash: 'h-1',
        args_hash: 'args-1',
        confirmation_required: false,
        confirmation_reasons: [],
        planned_leaf_calls: 3,
        budget_tokens: 50_000,
      }),
    );

    const preview = await previewWorkflow('agent-1', { name: 'wf' }, { week: 'W23' });

    const { url, init } = requestOf();
    expect(url).toBe('/api/agents/agent-1/workflows/preview');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ definition: { name: 'wf' }, args: { week: 'W23' } });
    expect(preview.preview_id).toBe('preview-1');
    expect(preview.definition_hash).toBe('h-1');
    expect(preview.args_hash).toBe('args-1');
    expect(preview.confirmation_required).toBe(false);
    expect(preview.planned_leaf_calls).toBe(3);
  });
});

describe('startWorkflow', () => {
  it('threads the preview binding and confirmed plan provenance', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        run_id: 'r-1',
        status: 'completed',
        reason: null,
        definition_hash: 'h',
        confirmation_required: true,
        confirmation_reasons: ['external effects'],
      }),
    );

    await startWorkflow('agent-1', {
      previewId: 'preview-1',
      confirmedPlanId: 'plan-9',
      planVersion: 2,
      planHash: 'ph',
    });

    const body = JSON.parse(String(requestOf().init.body));
    expect(body.preview_id).toBe('preview-1');
    expect(body.definition).toBeUndefined();
    expect(body.args).toBeUndefined();
    expect(body.definition_hash).toBeUndefined();
    expect(body.args_hash).toBeUndefined();
    expect(body.confirmed_plan_id).toBe('plan-9');
    expect(body.plan_version).toBe(2);
    expect(body.plan_hash).toBe('ph');
  });
});

describe('getWorkflowPreview', () => {
  it('reloads durable preview status for an inline confirmation card', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        preview_id: 'preview-1',
        session_id: 'session-1',
        preview_status: 'started',
        run_id: 'run-1',
      }),
    );

    const preview = await getWorkflowPreview('agent-1', 'preview-1');

    expect(requestOf().url).toBe('/api/agents/agent-1/workflows/previews/preview-1');
    expect(preview.preview_status).toBe('started');
    expect(preview.run_id).toBe('run-1');
  });
});

describe('getWorkflowRun', () => {
  it('returns the run with its step journal statuses', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        run_id: 'r-1',
        status: 'suspended',
        definition_hash: 'h',
        definition_source: 'dynamic_workflow',
        dynamic_workflow: { proposal_id: 'proposal-1', candidate_id: 'fanout-critic' },
        outcome_evidence: { leaf_total: 2, leaf_done: 1, leaf_failed: 1, promotion_eligible: false },
        repair_plan: { repairable: true, strategy: 'resume_failed_leaves', failed_leaf_count: 1 },
        leaf_calls: [{ step_id: 'scan', leaf_id: 'item-1', status: 'failed', error: 'timeout' }],
        steps: [
          { step_id: 'scan', step_type: 'agent_step', status: 'done', error: null },
          { step_id: 'approve', step_type: 'gate_step', status: 'suspended', error: 'awaiting approval' },
        ],
      }),
    );

    const run = await getWorkflowRun('agent-1', 'r-1');

    expect(requestOf().url).toBe('/api/agents/agent-1/workflows/runs/r-1');
    expect(run.status).toBe('suspended');
    expect(run.dynamic_workflow?.proposal_id).toBe('proposal-1');
    expect(run.outcome_evidence?.leaf_failed).toBe(1);
    expect(run.repair_plan?.repairable).toBe(true);
    expect(run.leaf_calls[0].leaf_id).toBe('item-1');
    expect(run.steps.map((step) => step.status)).toEqual(['done', 'suspended']);
  });
});

describe('cancelWorkflowRun', () => {
  it('POSTs the cancel endpoint', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ run_id: 'r-1', status: 'killed' }));
    const result = await cancelWorkflowRun('agent-1', 'r-1');
    expect(requestOf().url).toBe('/api/agents/agent-1/workflows/runs/r-1/cancel');
    expect(result.status).toBe('killed');
  });
});

describe('repairWorkflowRun', () => {
  it('POSTs the repair endpoint and returns the resumed status', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ run_id: 'r-1', status: 'completed', reason: 'repaired' }));
    const result = await repairWorkflowRun('agent-1', 'r-1');
    expect(requestOf().url).toBe('/api/agents/agent-1/workflows/runs/r-1/repair');
    expect(result.status).toBe('completed');
  });
});

describe('registered definitions', () => {
  it('lists definitions scoped to an agent', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse([
        {
          id: 'd-1',
          name: 'weekly-report',
          definition_version: 2,
          definition_hash: 'h-2',
          status: 'active',
          visibility_scope: 'tenant',
          owner_type: 'user',
          owner_id: null,
          call_policy: null,
          promoted_from_run_id: null,
        },
      ]),
    );

    const records = await listWorkflowDefinitions('agent-1');

    expect(requestOf().url).toBe('/api/workflow-definitions?agent_id=agent-1');
    expect(records[0].name).toBe('weekly-report');
    expect(records[0].status).toBe('active');
  });

  it('activates a draft', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: 'd-1',
        name: 'weekly-report',
        definition_version: 1,
        definition_hash: 'h',
        status: 'active',
        visibility_scope: 'agent',
        owner_type: 'user',
        owner_id: null,
        call_policy: null,
        promoted_from_run_id: null,
      }),
    );
    const record = await activateWorkflowDefinition('d-1');
    expect(requestOf().url).toBe('/api/workflow-definitions/d-1/activate');
    expect(record.status).toBe('active');
  });
});

describe('classifyTriggerPin (§6.2 mismatch surfacing)', () => {
  const records: WorkflowDefinitionRecord[] = [
    {
      id: 'd-1',
      name: 'weekly-report',
      description: '',
      definition_version: 2,
      definition_hash: 'hash-v2',
      status: 'active',
      visibility_scope: 'tenant',
      owner_type: 'user',
      owner_id: null,
      call_policy: null,
      promoted_from_run_id: null,
    },
  ];

  it('reports pinned when version+hash both match', () => {
    expect(
      classifyTriggerPin(
        { definition_name: 'weekly-report', definition_version: 2, definition_hash: 'hash-v2' },
        records,
      ),
    ).toBe('pinned');
  });

  it('reports hash_mismatch when the stored content changed', () => {
    expect(
      classifyTriggerPin(
        { definition_name: 'weekly-report', definition_version: 2, definition_hash: 'hash-from-creation' },
        records,
      ),
    ).toBe('hash_mismatch');
  });

  it('reports version_mismatch when the pinned version is gone', () => {
    expect(
      classifyTriggerPin(
        { definition_name: 'weekly-report', definition_version: 1, definition_hash: 'hash-v1' },
        records,
      ),
    ).toBe('version_mismatch');
  });

  it('reports missing when no record carries the name', () => {
    expect(
      classifyTriggerPin(
        { definition_name: 'ghost', definition_version: 1, definition_hash: 'h' },
        records,
      ),
    ).toBe('missing');
  });
});

describe('run history + promote (asset view)', () => {
  it('GETs the run history with limit', async () => {
    const { listWorkflowRuns } = await import('./workflows');
    fetchMock.mockResolvedValueOnce(
      jsonResponse([
        {
          run_id: 'r1',
          status: 'completed',
          name: 'contract-batch',
          description: 'OCR → extract → risk table',
          definition_source: 'ephemeral',
          definition_hash: 'h',
          created_at: '2026-06-05T12:00:00Z',
          completed_at: '2026-06-05T12:05:00Z',
          steps_total: 3,
          steps_done: 3,
          steps_failed: 0,
          promoted_definition_id: null,
        },
      ]),
    );
    const runs = await listWorkflowRuns('agent-1', 20);
    expect(requestOf().url).toContain('/agents/agent-1/workflows/runs?limit=20');
    expect(runs[0].name).toBe('contract-batch');
    expect(runs[0].description).toContain('OCR');
  });

  it('POSTs promote and returns the draft record with provenance', async () => {
    const { promoteWorkflowRun } = await import('./workflows');
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: 'd1',
        name: 'contract-batch',
        description: 'OCR → extract → risk table',
        definition_version: 1,
        definition_hash: 'h',
        status: 'draft',
        visibility_scope: 'agent',
        owner_type: 'agent',
        owner_id: 'agent-1',
        call_policy: null,
        promoted_from_run_id: 'r1',
      }),
    );
    const record = await promoteWorkflowRun('agent-1', 'r1');
    const { url, init } = requestOf();
    expect(url).toContain('/agents/agent-1/workflows/runs/r1/promote');
    expect(init.method).toBe('POST');
    expect(record.status).toBe('draft');
    expect(record.promoted_from_run_id).toBe('r1');
  });

  it('GETs promote suggestions', async () => {
    const { listPromoteSuggestions } = await import('./workflows');
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ definition_hash: 'h', name: 'contract-batch', run_count: 3, sample_run_ids: ['r1'] }]),
    );
    const suggestions = await listPromoteSuggestions('agent-1');
    expect(requestOf().url).toContain('/agents/agent-1/workflows/promote-suggestions');
    expect(suggestions[0].run_count).toBe(3);
  });
});
