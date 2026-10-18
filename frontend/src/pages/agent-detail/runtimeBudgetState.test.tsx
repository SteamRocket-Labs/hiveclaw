import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { RuntimeBudgetNotice } from './RuntimeBudgetNotice';
import { SessionTransportStatus } from './SessionTransportStatus';
import { sessionRunStateFromPayload, submittedSessionRunState } from './runtimeBudgetState';

describe('runtime budget degraded state', () => {
  it('projects typed backend state into the active run without exposing internal ids', () => {
    expect(sessionRunStateFromPayload({
      run_id: 'run-1',
      status: 'pending',
      runtime_budget: {
        schema: 'hive.runtime_budget_binding.v1',
        status: 'unavailable',
        reason: 'interactive_direct_response_budget_service_unavailable',
        retryable: true,
        interactive: true,
        work_amplifying_tools_disabled: true,
        error_class: 'OperationalError',
      },
    })).toEqual({
      runId: 'run-1',
      status: 'pending',
      runtimeBudget: {
        schema: 'hive.runtime_budget_binding.v1',
        status: 'unavailable',
        reason: 'interactive_direct_response_budget_service_unavailable',
        retryable: true,
        interactive: true,
        workAmplifyingToolsDisabled: true,
      },
    });
  });

  it('tells the user direct reply continues while recursive work is paused', () => {
    const markup = renderToStaticMarkup(<RuntimeBudgetNotice runtimeBudget={{
      schema: 'hive.runtime_budget_binding.v1',
      status: 'unavailable',
      reason: 'interactive_direct_response_budget_service_unavailable',
      retryable: true,
      interactive: true,
      workAmplifyingToolsDisabled: true,
    }} />);

    expect(markup).toContain('role="status"');
    expect(markup).toContain('当前回复仍会继续');
    expect(markup).toContain('子任务、工作流、跨 Agent 协作、目标续跑和自动唤醒已暂停');
    expect(markup).toContain('完成本轮后重试即可恢复');
    expect(markup).not.toMatch(/run-1|OperationalError/);
  });

  it('keeps the budget notice visible while live transport is connected', () => {
    const markup = renderToStaticMarkup(<SessionTransportStatus
      phase="connected"
      runtimeBudget={{
        schema: 'hive.runtime_budget_binding.v1',
        status: 'unavailable',
        reason: 'interactive_direct_response_budget_service_unavailable',
        retryable: true,
        interactive: true,
        workAmplifyingToolsDisabled: true,
      }}
    />);

    expect(markup).toContain('当前回复仍会继续');
  });
});


describe('session input admission', () => {
  it('leaves the active work intact when a receipt has no new run', () => {
    expect(submittedSessionRunState({ schema: 'hive.human_input_receipt', schema_version: 2,
      intent: 'queue_next_turn', status: 'accepted', input_id: 'input-2', run: null })).toBeNull();
  });

  it('consumes a real run directly or inside the admission receipt', () => {
    const run = { run_id: 'run-2', status: 'pending' };
    expect(submittedSessionRunState(run)).toEqual({ runId: 'run-2', status: 'pending' });
    expect(submittedSessionRunState({ schema: 'hive.human_input_receipt', schema_version: 2, run }))
      .toEqual({ runId: 'run-2', status: 'pending' });
  });
});
