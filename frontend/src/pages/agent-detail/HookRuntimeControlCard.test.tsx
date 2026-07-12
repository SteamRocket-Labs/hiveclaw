import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import HookRuntimeControlCard, { updateHookEnabled } from './HookRuntimeControlCard';
import { ccParityApi } from '../../api/domains/ccParity';

const hookData = {
  schema: 'hive.ccplus.hooks_control_plane.v2',
  agent_id: 'agent-1',
  events: [],
  registered_events: ['user_prompt_submit'],
  registrations: [{
    event: 'user_prompt_submit',
    handler_name: 'policy_guard',
    key: 'hook.prompt',
    failure_mode: 'required',
    runtime_config: {
      key: 'hook.prompt',
      enabled: true,
      failure_policy: 'inherit',
      effective_failure_mode: 'required',
    },
  }],
  recent_receipts: [{
    id: 'receipt-1',
    hook_key: 'hook.prompt',
    event: 'user_prompt_submit',
    status: 'error',
    failure_mode: 'required',
    retryable: true,
    error: 'TimeoutError',
    created_at: '2026-07-12T12:00:00Z',
  }],
};

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({ data: hookData, isLoading: false, error: null }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock('../../api/domains/ccParity', () => ({
  ccParityApi: {
    listHooks: vi.fn(),
    updateHookRuntimeConfig: vi.fn(),
  },
}));

describe('HookRuntimeControlCard', () => {
  beforeEach(() => {
    vi.mocked(ccParityApi.updateHookRuntimeConfig).mockResolvedValue({ ok: true, config: {} });
  });

  it('shows required blockers, retry guidance, and authorized disable control', () => {
    const markup = renderToStaticMarkup(<HookRuntimeControlCard agentId="agent-1" canManage />);

    expect(markup).toContain('Required blocker');
    expect(markup).toContain('TimeoutError');
    expect(markup).toContain('Retry the original turn after recovery.');
    expect(markup).toContain('Disable hook');
  });

  it('does not expose mutation controls without manage authority', () => {
    const markup = renderToStaticMarkup(<HookRuntimeControlCard agentId="agent-1" canManage={false} />);

    expect(markup).toContain('Required blocker');
    expect(markup).not.toContain('Disable hook');
  });

  it('uses the governed API for hook disable', async () => {
    await updateHookEnabled('agent-1', 'hook.prompt', false);

    expect(ccParityApi.updateHookRuntimeConfig).toHaveBeenCalledWith('agent-1', 'hook.prompt', { enabled: false });
  });
});
