// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import SessionPermissionActions from './SessionPermissionActions';
import type { SessionPermissionRequest } from './chatRuntime';

afterEach(cleanup);
const t = (_key: string, fallback: string) => fallback;

it('lets the requester inspect the exact proposal before sending one decision', async () => {
  const request: SessionPermissionRequest = {
    permission_request_id: 'current-permission', tool_name: 'create_employee_from_request', allow_session_allowed: false,
    arguments: { name: 'Weekly reporter', first_task: '甲店120，乙店90，丙店150；甲店缺货两天。',
      sources: ['store facts'], dispatch_once: true, literal: '<script>example</script>' },
  };
  const original = JSON.stringify(request);
  const resolve = vi.fn().mockResolvedValue(undefined);
  const { container } = render(<SessionPermissionActions permissionRequest={request} onResolveSessionPermission={resolve} t={t} />);
  fireEvent.click(screen.getByText('Action details'));
  const preview = screen.getByLabelText('Action details');
  expect(JSON.parse(preview.textContent!)).toEqual(request.arguments);
  expect(container.querySelector('details')?.open).toBe(true);
  expect(container.querySelector('script')).toBeNull();
  expect(resolve).not.toHaveBeenCalled();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Allow once' })); });
  expect(resolve).toHaveBeenCalledExactlyOnceWith(request, 'allow_once');
  expect(JSON.stringify(request)).toBe(original);
});

it('keeps another shared-work reader outside the requester-only parameters and controls', () => {
  const resolve = vi.fn();
  render(<SessionPermissionActions permissionRequest={{ permission_request_id: 'other', arguments: { path: 'PRIVATE_ARGUMENT' } }}
    onResolveSessionPermission={resolve} readOnly t={t} />);
  expect(screen.getByRole('status').textContent).toBe('Waiting for the original requester to decide.');
  expect(screen.queryByText('Action details')).toBeNull();
  expect(screen.queryByText(/PRIVATE_ARGUMENT/)).toBeNull();
  expect(screen.queryByRole('button')).toBeNull();
});

it('distinguishes an explicitly empty proposal from missing legacy parameters', () => {
  const { rerender } = render(<SessionPermissionActions permissionRequest={{ permission_request_id: 'empty', arguments: {} }} t={t} />);
  expect(screen.getByLabelText('Action details').textContent).toBe('{}');
  rerender(<SessionPermissionActions permissionRequest={{ permission_request_id: 'legacy' }} t={t} />);
  expect(screen.queryByLabelText('Action details')).toBeNull();
});
