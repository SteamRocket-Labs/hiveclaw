// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { ArtifactCards } from './ArtifactSurface';
import OriginalUploadAction from './OriginalUploadAction';
import { parseUserAttachment } from './chatAttachments';
import type { AgentChatMessage } from './chatRuntime';
import * as downloads from '../../utils/authenticatedResource';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });
const source = { name: 'source.bin', path: 'workspace/uploads/dingtalk/source.bin', source: 'human_input' };

it('discovers a recorded channel original through the current adapter before downloading exact bytes', async () => {
  localStorage.setItem('token', 'current-reader-token');
  localStorage.setItem('current_tenant_id', 'current-company');
  const body = new Blob(['ORIGINAL']);
  const fetcher = vi.fn().mockImplementation(async (path: string) => path.includes('workspace-original')
    ? { ok: true, json: async () => ({ id: 'channel-ref', content_sha256: 'a'.repeat(64), size_bytes: 8 }) }
    : { ok: true, blob: async () => body });
  vi.stubGlobal('fetch', fetcher);
  const saved = vi.spyOn(downloads, 'saveBlob').mockImplementation(() => {});
  render(<ArtifactCards agentId="employee" viewerUserId="reader" artifacts={[source]} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Original upload' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(body, 'source.bin'));
  expect(fetcher.mock.calls[0][0]).toBe('/api/chat/attachments/workspace-original?agent_id=employee&path=workspace%2Fuploads%2Fdingtalk%2Fsource.bin');
  expect(fetcher.mock.calls[1]).toEqual(['/api/chat/attachments/channel-ref/download', expect.objectContaining({
    headers: { Authorization: 'Bearer current-reader-token', 'X-Tenant-Id': 'current-company' },
  })]);
  expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy();
});

it('does not invent an original for unrecorded historical files', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({ detail: { code: 'original_attachment_not_recorded' } }) });
  vi.stubGlobal('fetch', fetcher);
  render(<ArtifactCards agentId="employee" viewerUserId="reader" artifacts={[source]} />);
  await waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
  expect(screen.queryByRole('button', { name: 'Original upload' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy();
});

it('ignores a late lookup and download after an A-B-A reader switch', async () => {
  let lookup: (value: unknown) => void = () => {};
  let download: (value: unknown) => void = () => {};
  const fetcher = vi.fn()
    .mockImplementationOnce(() => new Promise(resolve => { lookup = resolve; }))
    .mockResolvedValueOnce({ ok: true, json: async () => ({ id: 'other-ref' }) })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ id: 'new-ref' }) })
    .mockImplementationOnce(() => new Promise(resolve => { download = resolve; }))
    .mockResolvedValue({ ok: false, status: 403, json: async () => ({ detail: 'Access denied' }) });
  vi.stubGlobal('fetch', fetcher);
  const saved = vi.spyOn(downloads, 'saveBlob').mockImplementation(() => {});
  const ui = (viewer: string) => <ArtifactCards agentId="employee" viewerUserId={viewer} artifacts={[source]} />;
  const view = render(ui('reader'));
  view.rerender(ui('other'));
  await screen.findByRole('button', { name: 'Original upload' });
  view.rerender(ui('reader'));
  fireEvent.click(await screen.findByRole('button', { name: 'Original upload' }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(4));
  lookup({ ok: true, json: async () => ({ id: 'obsolete-ref' }) });
  view.rerender(ui('other'));
  download({ ok: true, blob: async () => new Blob(['OLD_READER_BYTES']) });
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(5));
  expect(saved).not.toHaveBeenCalled();
  expect(fetcher.mock.calls[3][0]).toBe('/api/chat/attachments/new-ref/download');
});

it('retains the qualified legacy channel path and discovers its recorded original', async () => {
  const msg = parseUserAttachment({ role: 'user', content: '[file:workspace/uploads/feishu/source/file.bin]' } as AgentChatMessage, 'employee');
  expect(msg.fileName).toBe('file.bin');
  expect(msg.originalWorkspacePath).toBe('workspace/uploads/feishu/source/file.bin');
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 'legacy-ref' }) });
  vi.stubGlobal('fetch', fetcher);
  render(<OriginalUploadAction agentId="employee" path={msg.originalWorkspacePath!} name={msg.fileName!} viewerUserId="reader" discover />);
  expect(await screen.findByRole('button', { name: 'Original upload' })).toBeTruthy();
});

it('discards a completed download when the authenticated company changed without a source rerender', async () => {
  localStorage.setItem('current_tenant_id', 'first-company');
  let finish: (value: unknown) => void = () => {};
  const fetcher = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ id: 'first-ref' }) })
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  vi.stubGlobal('fetch', fetcher);
  const saved = vi.spyOn(downloads, 'saveBlob').mockImplementation(() => {});
  render(<ArtifactCards agentId="employee" viewerUserId="reader" artifacts={[source]} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Original upload' }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  localStorage.setItem('current_tenant_id', 'second-company');
  finish({ ok: true, blob: async () => new Blob(['FIRST_COMPANY_BYTES']) });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Original upload' })).not.toHaveProperty('disabled', true));
  expect(saved).not.toHaveBeenCalled();
});
