import { describe, expect, it } from 'vitest';

import { sessionToolEffectRecoveryModel } from './SessionToolEffectRecovery';

describe('sessionToolEffectRecoveryModel', () => {
  it('blocks composer and generic retry only for the exact tool-effect reconciliation code', () => {
    expect(sessionToolEffectRecoveryModel({
      runtime_tasks: [{
        id: 'run-1',
        status: 'failed',
        user_blocker: {
          kind: 'runtime_reconciliation',
          reason_code: 'tool_effect_outcome_unknown',
          retry_available: false,
        },
      }],
    } as any)).toEqual({
      blocked: true,
    });

    expect(sessionToolEffectRecoveryModel({
      runtime_tasks: [{
        id: 'run-2',
        status: 'failed',
        user_blocker: { kind: 'runtime_reconciliation', reason_code: 'other_failure' },
      }],
    } as any).blocked).toBe(false);
  });
});
