import { describe, expect, it } from 'vitest';

import { approvalRequestPresentation } from './approvalRequestPresentation';

describe('approvalRequestPresentation', () => {
  it('exposes only the business fields required to approve an exact command', () => {
    expect(
      approvalRequestPresentation({
        action_type: 'workspace.command.escalation',
        details: {
          reason: 'Release the verified frontend build.',
          command: 'railway up --service frontend',
          requested_by: 'user-internal-123',
          args: { command: 'railway up --service frontend' },
          execution_envelope: { bearer_token: 'secret-token' },
          policy_snapshot_hash: 'hash-internal-456',
        },
      }),
    ).toEqual({
      actionKey: 'commandEscalation',
      actionFallback: 'Run one workspace command',
      description: 'Release the verified frontend build.',
      fields: [
        {
          key: 'command',
          value: 'railway up --service frontend',
          code: true,
        },
      ],
    });
  });

  it('keeps local-runtime metadata and unknown machine fields out of the product card', () => {
    expect(
      approvalRequestPresentation({
        action_type: 'local_agent.execute',
        details: {
          reason: 'Send the approved brief to the connected local employee.',
          attachment_count: 2,
          local_agent_message_id: 'message-internal-123',
          replay_key: 'replay-internal-456',
          request_hash: 'hash-internal-789',
        },
      }),
    ).toEqual({
      actionKey: 'localAgentDispatch',
      actionFallback: 'Send work to a connected local employee',
      description: 'Send the approved brief to the connected local employee.',
      fields: [
        {
          key: 'attachments',
          value: 2,
          code: false,
        },
      ],
    });
  });

  it('uses a neutral product label instead of exposing an unknown internal action identifier', () => {
    expect(
      approvalRequestPresentation({
        action_type: 'internal.capability.secret_name',
        details: {
          requested_by: 'user-internal-123',
          execution_envelope: { bearer_token: 'secret-token' },
        },
      }),
    ).toEqual({
      actionKey: 'employeeAction',
      actionFallback: 'Review an employee action',
      description: null,
      fields: [],
    });
  });
});
