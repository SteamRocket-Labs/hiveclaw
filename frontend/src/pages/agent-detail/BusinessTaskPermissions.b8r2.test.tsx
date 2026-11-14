// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { chatApi } from '../../api/domains/chat';
import BusinessTaskPermissions from './BusinessTaskPermissions';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string) => translateFromCatalog(zh, key, fallback),
}) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it.each([
  ['requester', 'owner', true],
  ['read-only viewer', 'reader', true],
  ['requester without decision access', 'owner', false],
] as const)('B8-r2 keeps one exact proposal view for the business-task %s', async (_kind, userId, canResolve) => {
  const task = { id: 'task', agent_id: 'agent', status: 'doing', last_execution_status: 'awaiting_permission',
    active_runtime_task_id: 'run', reflection_session_id: 'session' };
  const permission = { session_id: 'session', can_resolve: canResolve, request: {
    permission_request_id: 'permission', requested_by_user_id: 'owner', session_id: 'session', runtime_task_id: 'run',
    tool_name: 'call_mcp_tool', arguments: { from: '/docs/contract.pdf', to: 's3://public-bucket/contract.pdf',
      fields: { 金额: 100, 状态: '已付款', to: '2026-12-31' } },
  } };
  const original = JSON.stringify(permission);
  const resolve = vi.spyOn(chatApi, 'resolveSessionPermission').mockResolvedValue({ status: 'resolved' } as never);
  const onResolved = vi.fn().mockResolvedValue(undefined);
  render(<BusinessTaskPermissions agentId="agent" task={task as never} permissions={[permission]} userId={userId} onResolved={onResolved} />);
  const summaries = screen.getAllByText('操作详情', { selector: 'summary' });
  expect(summaries).toHaveLength(1);
  const disclosure = summaries[0].closest('details')!;
  fireEvent.click(summaries[0]);
  expect(disclosure.open).toBe(true);
  const text = disclosure.querySelector('pre')!.textContent!;
  expect(text).toBe(JSON.stringify(permission.request.arguments, null, 2));
  expect(JSON.parse(text)).toEqual(permission.request.arguments);
  if (userId === 'owner' && canResolve) {
    fireEvent.click(screen.getByRole('button', { name: '本次允许' }));
    await waitFor(() => expect(resolve).toHaveBeenCalledExactlyOnceWith('agent', 'session', 'permission', { action: 'allow_once' }));
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
  } else {
    expect(screen.queryByRole('button')).toBeNull();
    expect(resolve).not.toHaveBeenCalled();
  }
  expect(JSON.stringify(permission)).toBe(original);
});
