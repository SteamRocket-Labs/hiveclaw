// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import AppDialogs from '../../components/AppDialogs';
import * as dialogs from '../../components/AppDialogs';
import { ArtifactCards } from './ArtifactSurface';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });
const digest = 'a'.repeat(64);
const active = { reference_id: 'source-ref', state: 'active', can_withdraw: true, content_sha256: digest, size_bytes: 8 };
const artifact = { path: 'workspace/source.txt', name: 'source.txt', originalUpload: { id: 'source-ref', ownerUserId: 'owner' } };
function mount() {
  localStorage.setItem('token', 'owner-token');
  localStorage.setItem('current_tenant_id', 'company');
  return render(<><AppDialogs /><ArtifactCards agentId="employee" viewerUserId="owner" artifacts={[artifact]} /></>);
}
const response = (value: unknown) => ({ ok: true, json: async () => value });

it('queries current authority on demand and withdraws through a confirmed authenticated action', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response(active)).mockResolvedValueOnce(response({ state: 'withdrawn' }));
  vi.stubGlobal('fetch', fetcher);
  mount();
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Original status' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Withdraw original' }));
  const modal = await screen.findByRole('dialog');
  expect(fetcher).toHaveBeenCalledOnce();
  fireEvent.click(within(modal).getByRole('button', { name: 'Withdraw original' }));
  await screen.findByText('Original withdrawn. Select an authorized source to continue.');
  expect(fetcher.mock.calls[1]).toEqual(['/api/chat/attachments/source-ref/withdraw', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ expected_content_sha256: digest }),
    headers: expect.objectContaining({ Authorization: 'Bearer owner-token', 'X-Tenant-Id': 'company' }),
  })]);
  expect(screen.queryByRole('button', { name: 'Original upload' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Open' })).toBeTruthy();
});

it('reopens withdrawn status and never offers another withdrawal or a substitute download', async () => {
  const fetcher = vi.fn().mockResolvedValue(response({ ...active, state: 'withdrawn', can_withdraw: false }));
  vi.stubGlobal('fetch', fetcher);
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Original status' }));
  await screen.findByText('Original withdrawn. Select an authorized source to continue.');
  expect(fetcher).toHaveBeenCalledOnce();
  expect(screen.queryByRole('button', { name: 'Withdraw original' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy();
});

it('uses server withdrawal authority independently from original visibility', async () => {
  const fetcher = vi.fn().mockResolvedValue(response({ ...active, can_withdraw: false }));
  vi.stubGlobal('fetch', fetcher);
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Original status' }));
  await screen.findByText('Original active');
  expect(screen.queryByRole('button', { name: 'Withdraw original' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Original upload' })).toBeTruthy();
});

it('keeps a qualified retained inspection visible without reactivating the withdrawn source', async () => {
  const bytes = new Blob(['RETAINED_INSPECTION']);
  const fetcher = vi.fn().mockResolvedValueOnce(response({ ...active, state: 'withdrawn', can_withdraw: false, can_inspect_retained: true }))
    .mockResolvedValueOnce({ ok: true, blob: async () => bytes });
  vi.stubGlobal('fetch', fetcher);
  const downloads = await import('../../utils/authenticatedResource');
  const save = vi.spyOn(downloads, 'saveBlob').mockImplementation(() => {});
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Original status' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Inspect retained original' }));
  await waitFor(() => expect(save).toHaveBeenCalledWith(bytes, 'source.txt'));
  expect(screen.getByText('Original withdrawn. Select an authorized source to continue.')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Withdraw original' })).toBeNull();
  expect(fetcher.mock.calls[1][0]).toBe('/api/chat/attachments/source-ref/download');
});

it('does not submit a confirmation completed under a different authenticated company', async () => {
  let confirm: (value: boolean) => void = () => {};
  vi.spyOn(dialogs, 'requestAppConfirm').mockImplementation(() => new Promise(resolve => { confirm = resolve; }));
  const fetcher = vi.fn().mockResolvedValue(response(active));
  vi.stubGlobal('fetch', fetcher);
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Original status' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Withdraw original' }));
  localStorage.setItem('current_tenant_id', 'another-company');
  confirm(true);
  await waitFor(() => expect(screen.getByRole('button', { name: 'Withdraw original' })).not.toHaveProperty('disabled', true));
  expect(fetcher).toHaveBeenCalledOnce();
});

it('reads back a lost withdrawal receipt without submitting another mutation', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(response(active)).mockRejectedValueOnce(new TypeError('Receipt unavailable'))
    .mockResolvedValueOnce(response({ ...active, state: 'withdrawn', can_withdraw: false }));
  vi.stubGlobal('fetch', fetcher);
  vi.spyOn(dialogs, 'requestAppConfirm').mockResolvedValue(true);
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Original status' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Withdraw original' }));
  await screen.findByText('Could not confirm withdrawal. Check Original status before retrying.');
  fireEvent.click(screen.getByRole('button', { name: 'Original status' }));
  await screen.findByText('Original withdrawn. Select an authorized source to continue.');
  expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/chat/attachments/source-ref/status',
    '/api/chat/attachments/source-ref/withdraw', '/api/chat/attachments/source-ref/status']);
});
