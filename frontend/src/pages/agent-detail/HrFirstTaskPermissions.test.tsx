import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { HrCreationDraft, HrFirstTaskPermission } from '../../api/domains/hrCreation';
import { chatApi, type SessionPermissionResolutionReceipt } from '../../api/domains/chat';
import { resolveFirstTaskPermission } from './HrFirstTaskPermissions';
import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import { normalizeToolCallResult } from './toolResultEnvelope';

const state = vi.hoisted(() => ({ draft: undefined as HrCreationDraft | undefined, failed: false, viewer: 'owner' }));
afterEach(() => vi.restoreAllMocks());
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
vi.mock('../../stores', () => ({ useAuthStore: (select: (value: unknown) => unknown) => select({ user: { id: state.viewer } }) }));
vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({ data: state.draft, isError: state.failed }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useMutation: () => ({ isPending: false }),
}));

beforeEach(() => {
  state.failed = false;
  state.viewer = 'owner';
  state.draft = {
    status: 'preview', blueprint_id: 'draft-1', blueprint_version: 1, blueprint_hash: 'hash', draft_status: 'completed',
    blueprint: { name: 'Researcher' }, requested_by_user_id: 'owner', hr_agent_id: 'original-agent',
    session_id: 'original-work', created_agent_id: 'employee',
    creation_authorization: { mode: 'policy_direct', first_task: 'Draft the report' },
    first_task_work: { runtime_task_id: 'run-1', agent_id: 'employee', session_id: 'child-1', status: 'suspended',
      question: null, can_answer: false,
      permissions: [{ session_id: 'child-1', can_resolve: true, request: {
        permission_request_id: 'permission-1', requested_by_user_id: 'owner', session_id: 'child-1',
        runtime_task_id: 'run-1', tool_name: 'write_file', arguments: { path: 'workspace/report.md' },
        allow_session_allowed: false, risk_class: 'destructive_delete', confirmation_kind: 'destructive_once',
      } }],
    },
  } as HrCreationDraft;
});

function originalCard() {
  const preview = normalizeToolCallResult('create_employee_from_request', state.draft).toolMeta;
  if (preview?.kind !== 'hr_preview') throw new Error('Missing original creation preview');
  return renderToStaticMarkup(<HrBlueprintPreviewCard agentId="original-agent" preview={preview} />);
}

it('shows the exact native operation and existing once-only controls in the original creation card', () => {
  const markup = originalCard();
  expect(markup).toContain('workspace/report.md');
  expect(markup).toContain('Allow once');
  expect(markup).toContain('Deny');
  expect(markup).toContain('Delete actions can only be allowed once.');
  expect(markup).not.toContain('Allow for this session');
});

const receipt: SessionPermissionResolutionReceipt = { schema: 'hive.session_permission_resolution.v2', status: 'resolved',
  permission_request_id: 'permission-1', invocation_id: 'invocation-1', control_id: 'control-1', run_id: 'run-1',
  run_status: 'resumable', result_event_id: 'result-1', retryable: false, recovery_action: 'continue' };

function current() {
  const draft = state.draft!;
  return { draft, permission: draft.first_task_work!.permissions![0] as HrFirstTaskPermission };
}

it.each(['allow_once', 'allow_session', 'deny'] as const)('routes %s to the same child permission', async (action) => {
  const send = vi.spyOn(chatApi, 'resolveSessionPermission').mockResolvedValue(receipt);
  const { draft, permission } = current();
  permission.request.allow_session_allowed = true;
  delete permission.request.risk_class;
  delete permission.request.confirmation_kind;
  expect(await resolveFirstTaskPermission('original-agent', draft, permission, 'owner', action)).toBe(receipt);
  expect(send).toHaveBeenCalledExactlyOnceWith('employee', 'child-1', 'permission-1', { action });
});

it.each(['source', 'employee', 'run', 'session', 'requester', 'cancelled', 'authority'] as const)('rejects a stale %s before sending a decision', async (change) => {
  const send = vi.spyOn(chatApi, 'resolveSessionPermission').mockResolvedValue(receipt);
  const { draft, permission } = current();
  if (change === 'source') draft.hr_agent_id = 'another-source';
  if (change === 'employee') draft.created_agent_id = 'another-employee';
  if (change === 'run') draft.first_task_work!.runtime_task_id = 'another-run';
  if (change === 'session') permission.session_id = 'another-session';
  if (change === 'requester') draft.requested_by_user_id = 'another-user';
  if (change === 'cancelled') draft.first_task_work!.status = 'killed';
  if (change === 'authority') permission.can_resolve = false;
  await expect(resolveFirstTaskPermission('original-agent', draft, permission, 'owner', 'allow_once')).rejects.toThrow('no longer current');
  expect(send).not.toHaveBeenCalled();
});

it('returns the unknown native receipt without retrying or dispatching new work', async () => {
  const unknown: SessionPermissionResolutionReceipt = { ...receipt, status: 'needs_reconciliation',
    run_status: 'needs_reconciliation', result_event_id: null, recovery_action: 'reconcile_tool_effect' };
  const send = vi.spyOn(chatApi, 'resolveSessionPermission').mockResolvedValue(unknown);
  const { draft, permission } = current();
  expect(await resolveFirstTaskPermission('original-agent', draft, permission, 'owner', 'allow_once')).toBe(unknown);
  expect(send).toHaveBeenCalledTimes(1);
});

it.each(['failed', 'viewer', 'cancelled'] as const)('closes the original card controls on %s evidence', (change) => {
  if (change === 'failed') state.failed = true;
  if (change === 'viewer') state.viewer = 'another-user';
  if (change === 'cancelled') state.draft!.first_task_work!.status = 'killed';
  expect(originalCard()).not.toContain('workspace/report.md');
  expect(originalCard()).not.toContain('Allow once');
});
