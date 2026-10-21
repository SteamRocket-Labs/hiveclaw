// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { ArtifactCards } from './ArtifactSurface';
import { parseUserAttachment } from './chatAttachments';
import type { AgentChatMessage } from './chatRuntime';
import * as downloads from '../../utils/authenticatedResource';
import * as dialogs from '../../components/AppDialogs';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }) }));

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

function restoredInput(actor = 'owner') {
  const parts = [{ type: 'text', text: 'Continue this work.' }, { type: 'attachment_ref', attachment: {
    path: 'workspace/uploads/source.bin', name: 'source.bin', size: 8,
    original_attachment: { id: 'original-ref', owner_user_id: 'owner', content_sha256: 'a'.repeat(64), size_bytes: 8 },
  } }];
  const message: AgentChatMessage = { role: 'user', content: JSON.stringify(parts), sessionItem: {
    id: 'input', kind: 'human_input', scope: { level: 'session', session_id: 'work', thread_id: 'work' },
    lifecycle: 'accepted', terminal: false, revision: 0, content: JSON.stringify(parts), payload: { content_parts: parts },
    actor: { type: 'user', id: actor }, visibility: { audience: 'private' }, occurredAt: '2026-10-21T00:00:00Z',
    first_sequence: 1, last_sequence: 1,
  } };
  return parseUserAttachment(message, 'employee').inputAttachments;
}

it('downloads the saved original with current authentication while retaining current-file actions', async () => {
  localStorage.setItem('token', 'current-owner-token');
  localStorage.setItem('current_tenant_id', 'current-company');
  const original = new Blob(['ORIGINAL']);
  const current = new Blob(['EDITED']);
  const fetcher = vi.fn().mockImplementation(async (path: string) => ({ ok: true, blob: async () => path.includes('/chat/attachments/') ? original : current }));
  vi.stubGlobal('fetch', fetcher);
  const saved = vi.spyOn(downloads, 'saveBlob').mockImplementation(() => {});
  const open = vi.fn();
  render(<ArtifactCards agentId="employee" viewerUserId="owner" artifacts={restoredInput()} onOpenArtifact={open} />);
  fireEvent.click(screen.getByRole('button', { name: 'Original upload' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(original, 'source.bin'));
  expect(fetcher).toHaveBeenNthCalledWith(1, '/api/chat/attachments/original-ref/download', expect.objectContaining({
    headers: { Authorization: 'Bearer current-owner-token', 'X-Tenant-Id': 'current-company' },
  }));
  fireEvent.click(screen.getByRole('button', { name: 'Download' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(current, 'source.bin'));
  expect(fetcher.mock.calls[1][0]).toContain('/api/agents/employee/files/download?');
  fireEvent.click(screen.getByRole('button', { name: 'Open' }));
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ path: 'workspace/uploads/source.bin' }));
});

it('retains the reference after a typed download failure without substituting workspace bytes', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 503,
    json: async () => ({ detail: { code: 'original_attachment_unavailable', retryable: true } }) });
  vi.stubGlobal('fetch', fetcher);
  const saved = vi.spyOn(downloads, 'saveBlob').mockImplementation(() => {});
  const toast = vi.spyOn(dialogs, 'showAppToast').mockImplementation(() => {});
  render(<ArtifactCards agentId="employee" viewerUserId="owner" artifacts={restoredInput()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Original upload' }));
  await waitFor(() => expect(toast).toHaveBeenCalledOnce());
  expect(saved).not.toHaveBeenCalled();
  expect(fetcher).toHaveBeenCalledOnce();
  expect(screen.getByRole('button', { name: 'Original upload' })).toBeTruthy();
});

it.each([['other', 'owner'], ['owner', 'forged-owner']])('hides private original actions for viewer %s and actor %s', (viewer, actor) => {
  render(<ArtifactCards agentId="employee" viewerUserId={viewer} artifacts={restoredInput(actor)} />);
  expect(screen.queryByRole('button', { name: 'Original upload' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy();
});
