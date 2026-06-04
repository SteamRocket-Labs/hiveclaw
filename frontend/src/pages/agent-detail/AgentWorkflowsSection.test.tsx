import { describe, expect, it } from 'vitest';

import { buildWorkflowStartOptions, type WorkflowPlanHandoffFields } from './AgentWorkflowsSection';
import type { WorkflowPreview } from '../../api/domains/workflows';

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
