// @vitest-environment jsdom
import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { useOriginalDraftRemoval } from './useOriginalDraftRemoval';

const draft = () => ({ name: 'source.bin', originalAttachment: { id: 'source-ref', owner_user_id: 'owner', content_sha256: 'a'.repeat(64), size_bytes: 8 } });
const failed = vi.fn();
function Composer({ identity = 'owner:company:employee' }: { identity?: string }) {
  const [files, setFiles] = useState([draft()]);
  const { remove, removing } = useOriginalDraftRemoval({ files, setFiles, identity, onError: failed });
  return <><span>{files.length} attached</span><button disabled={removing} onClick={() => void remove(0)}>Remove</button>
    <button disabled={removing}>Send</button></>;
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); failed.mockReset(); localStorage.clear(); });

it('uses the actual authenticated DELETE and removes the draft only after its response', async () => {
  localStorage.setItem('token', 'owner-token');
  localStorage.setItem('current_tenant_id', 'company');
  let finish: (value: unknown) => void = () => {};
  const fetcher = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  vi.stubGlobal('fetch', fetcher);
  render(<Composer />);
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  expect(fetcher).toHaveBeenCalledOnce();
  expect(screen.getByText('1 attached')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Send' })).toHaveProperty('disabled', true);
  expect(fetcher.mock.calls[0]).toEqual([`/api/chat/attachments/source-ref/draft?expected_content_sha256=${'a'.repeat(64)}`,
    expect.objectContaining({ method: 'DELETE', headers: expect.objectContaining({ Authorization: 'Bearer owner-token', 'X-Tenant-Id': 'company' }) })]);
  finish({ ok: true, status: 200, json: async () => ({ status: 'released', reference_id: 'source-ref' }) });
  await screen.findByText('0 attached');
});

it('keeps an uncertain draft and retries explicitly without requiring another provider effect', async () => {
  const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('Synthetic lost release response'))
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ status: 'already_absent', reference_id: 'source-ref' }) });
  vi.stubGlobal('fetch', fetcher);
  render(<Composer />);
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  await waitFor(() => expect(failed).toHaveBeenCalledOnce());
  expect(screen.getByText('1 attached')).toBeTruthy();
  expect(fetcher).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  await screen.findByText('0 attached');
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it('does not let an old A-B-A removal erase the current draft or unlock a newer removal', async () => {
  let first: (value: unknown) => void = () => {};
  let second: (value: unknown) => void = () => {};
  const fetcher = vi.fn().mockImplementationOnce(() => new Promise(resolve => { first = resolve; }))
    .mockImplementationOnce(() => new Promise(resolve => { second = resolve; }));
  vi.stubGlobal('fetch', fetcher);
  const view = render(<Composer />);
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  view.rerender(<Composer identity="other:company:employee" />);
  view.rerender(<Composer />);
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  first({ ok: true, status: 200, json: async () => ({ status: 'released', reference_id: 'source-ref' }) });
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  expect(screen.getByText('1 attached')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Send' })).toHaveProperty('disabled', true);
  second({ ok: true, status: 200, json: async () => ({ status: 'retained', reference_id: 'source-ref' }) });
  await screen.findByText('0 attached');
});

it('keeps the draft when a successful HTTP response does not confirm a known reference state', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ status: 'unknown' }) }));
  render(<Composer />);
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  await waitFor(() => expect(failed).toHaveBeenCalledOnce());
  expect(screen.getByText('1 attached')).toBeTruthy();
});
