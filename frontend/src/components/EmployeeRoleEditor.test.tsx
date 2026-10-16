// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';
import EmployeeRoleEditor from './EmployeeRoleEditor';

const translate = (key: string, fallback?: string) => translateFromCatalog(en, key, fallback);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const initial = { role_description: 'Original role', role_revision: 'a'.repeat(64) };
const current = { role_description: 'A colleague saved this role', role_revision: 'b'.repeat(64) };
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

it('preserves a conflicting draft while reading the actual stored responsibilities', async () => {
  const requests: Array<{ url: string; body?: unknown }> = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    requests.push({ url: String(url), ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}) });
    return init?.method === 'PATCH' ? response({ detail: 'Responsibilities changed' }, 409) : response(current);
  });
  const saved = vi.fn();
  render(<EmployeeRoleEditor agentId="employee" value={initial} onSaved={saved} />);
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'My unsaved draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByRole('alert');
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('My unsaved draft');
  expect(saved).not.toHaveBeenCalled();
  expect(requests[0]).toEqual({ url: '/api/agents/employee', body: { role_description: 'My unsaved draft', expected_role_revision: initial.role_revision } });
  fireEvent.click(screen.getByRole('button', { name: 'Read current responsibilities' }));
  await screen.findByText(current.role_description);
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('My unsaved draft');
  fireEvent.click(screen.getByRole('button', { name: 'Use current content' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(current));
  expect(requests).toHaveLength(2);
});

it('retries the recorded save confirmation without repeating the canonical role write', async () => {
  const patch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(current));
  const saved = vi.fn().mockRejectedValueOnce(new Error('Confirmation unavailable')).mockResolvedValueOnce(undefined);
  render(<EmployeeRoleEditor agentId="employee" value={initial} onSaved={saved} />);
  fireEvent.change(screen.getByRole('textbox'), { target: { value: current.role_description } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText('Confirmation unavailable');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(2));
  expect(patch).toHaveBeenCalledTimes(1);
  expect(saved).toHaveBeenLastCalledWith(current);
});
