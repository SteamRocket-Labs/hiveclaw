/**
 * §9 P12 red tests: workflow domain adapter — preview / run status /
 * registered list / trigger pin-mismatch classification.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  activateWorkflowDefinition,
  cancelWorkflowRun,
  classifyTriggerPin,
  getWorkflowRun,
  listWorkflowDefinitions,
  previewWorkflow,
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
  it('POSTs definition+args and returns hash/risk/planned leaves', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        definition_hash: 'h-1',
        risk: 'low',
        risk_reasons: [],
        planned_leaf_calls: 3,
        budget_tokens: 50_000,
      }),
    );

    const preview = await previewWorkflow('agent-1', { name: 'wf' }, { week: 'W23' });

    const { url, init } = requestOf();
    expect(url).toBe('/api/agents/agent-1/workflows/preview');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ definition: { name: 'wf' }, args: { week: 'W23' } });
    expect(preview.definition_hash).toBe('h-1');
    expect(preview.risk).toBe('low');
    expect(preview.planned_leaf_calls).toBe(3);
  });
});

describe('startWorkflow', () => {
  it('threads the confirmed plan binding for high-risk launches', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ run_id: 'r-1', status: 'completed', reason: null, definition_hash: 'h', risk: 'high' }),
    );

    await startWorkflow('agent-1', { name: 'wf' }, {}, {
      confirmedPlanId: 'plan-9',
      planVersion: 2,
      planHash: 'ph',
    });

    const body = JSON.parse(String(requestOf().init.body));
    expect(body.confirmed_plan_id).toBe('plan-9');
    expect(body.plan_version).toBe(2);
    expect(body.plan_hash).toBe('ph');
  });
});

describe('getWorkflowRun', () => {
  it('returns the run with its step journal statuses', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        run_id: 'r-1',
        status: 'suspended',
        definition_hash: 'h',
        definition_source: 'ephemeral',
        steps: [
          { step_id: 'scan', step_type: 'agent_step', status: 'done', error: null },
          { step_id: 'approve', step_type: 'gate_step', status: 'suspended', error: 'awaiting approval' },
        ],
      }),
    );

    const run = await getWorkflowRun('agent-1', 'r-1');

    expect(requestOf().url).toBe('/api/agents/agent-1/workflows/runs/r-1');
    expect(run.status).toBe('suspended');
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
