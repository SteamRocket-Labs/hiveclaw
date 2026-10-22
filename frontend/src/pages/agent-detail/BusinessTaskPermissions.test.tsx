import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { chatApi, type SessionPermissionResolutionReceipt } from '../../api/domains/chat';
import type { BusinessTaskPermission } from '../../api/domains/tasks';
import type { Task } from '../../types';
import BusinessTaskPermissions, { resolveBusinessTaskPermission } from './BusinessTaskPermissions';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));

const task = { id: 'task-1', agent_id: 'agent-1', status: 'doing', last_execution_status: 'awaiting_permission',
  active_runtime_task_id: 'run-1', reflection_session_id: 'native-1' } as Task;
const permission: BusinessTaskPermission = { session_id: 'native-1', can_resolve: true, request: {
  permission_request_id: 'permission-1', requested_by_user_id: 'owner', session_id: 'native-1',
  runtime_task_id: 'run-1', tool_name: 'fs_write', arguments: { path: 'workspace/report.md' }, allow_session_allowed: true,
} };
const receipt: SessionPermissionResolutionReceipt = { schema: 'hive.session_permission_resolution.v2', status: 'resolved',
  permission_request_id: 'permission-1', invocation_id: 'invocation-1', control_id: 'control-1', run_id: 'run-1',
  run_status: 'resumable', result_event_id: 'result-1', retryable: false, recovery_action: 'continue' };

afterEach(() => vi.restoreAllMocks());

describe('original assignment permission consumer', () => {
  it.each(['allow_once', 'allow_session', 'deny'] as const)('routes %s to the exact native session permission', async (action) => {
    const send = vi.spyOn(chatApi, 'resolveSessionPermission').mockResolvedValue(receipt);
    expect(await resolveBusinessTaskPermission('agent-1', task, permission, 'owner', action)).toBe(receipt);
    expect(send).toHaveBeenCalledExactlyOnceWith('agent-1', 'native-1', 'permission-1', { action });
  });

  it.each(['run', 'session', 'agent', 'requester', 'cancelled', 'authority'] as const)('does not send a stale %s decision', async (changed) => {
    const send = vi.spyOn(chatApi, 'resolveSessionPermission').mockResolvedValue(receipt);
    const currentTask = { ...task };
    const current = { ...permission, request: { ...permission.request } };
    if (changed === 'run') currentTask.active_runtime_task_id = 'another-run';
    if (changed === 'session') current.session_id = 'another-session';
    if (changed === 'agent') currentTask.agent_id = 'another-agent';
    if (changed === 'requester') current.request.requested_by_user_id = 'another-requester';
    if (changed === 'cancelled') currentTask.status = 'cancelled';
    if (changed === 'authority') current.can_resolve = false;
    await expect(resolveBusinessTaskPermission('agent-1', currentTask, current, 'owner', 'allow_once')).rejects.toThrow('no longer current');
    expect(send).not.toHaveBeenCalled();
  });

  it('preserves an unknown tool outcome without retrying or starting another run', async () => {
    const unknown = { ...receipt, status: 'needs_reconciliation', run_status: 'needs_reconciliation',
      result_event_id: null, recovery_action: 'reconcile' } as const;
    const send = vi.spyOn(chatApi, 'resolveSessionPermission').mockResolvedValue(unknown);
    expect(await resolveBusinessTaskPermission('agent-1', task, permission, 'owner', 'allow_once')).toBe(unknown);
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('uses the existing once-only destructive permission controls in the original task', () => {
    const destructive = { ...permission, request: { ...permission.request, destructive: true,
      allow_session_allowed: false, risk_class: 'destructive_delete', confirmation_kind: 'destructive_once' } };
    const markup = renderToStaticMarkup(<BusinessTaskPermissions agentId="agent-1" task={task}
      permissions={[destructive]} userId="owner" onResolved={async () => {}} />);
    expect(markup).toContain('workspace/report.md');
    expect(markup).toContain('Allow once');
    expect(markup).toContain('Deny');
    expect(markup).toContain('Delete actions can only be allowed once.');
    expect(markup).not.toContain('Allow for this session');
  });

  it('shows authorized readers the original requester wait and hides stale run cards', () => {
    const markup = renderToStaticMarkup(<BusinessTaskPermissions agentId="agent-1" task={task}
      permissions={[permission]} userId="another-reader" onResolved={async () => {}} />);
    expect(markup).toContain('Waiting for the original requester to decide.');
    expect(markup).not.toContain('Allow once');
    const stale = renderToStaticMarkup(<BusinessTaskPermissions agentId="agent-1"
      task={{ ...task, active_runtime_task_id: 'another-run' }} permissions={[permission]} userId="owner" onResolved={async () => {}} />);
    expect(stale).toBe('');
  });
});
