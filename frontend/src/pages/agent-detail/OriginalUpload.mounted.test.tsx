// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { ArtifactCards } from './ArtifactSurface';
import { parseUserAttachment } from './chatAttachments';
import type { AgentChatMessage } from './chatRuntime';
import * as downloads from '../../utils/authenticatedResource';

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

// An attachment is just an attachment (P3): the original-upload controls are
// gone from message rows and artifact cards; the current file keeps its
// ordinary download and open actions for every viewer.
it('keeps ordinary download and open actions on a restored attachment without original-upload controls', async () => {
  localStorage.setItem('token', 'current-owner-token');
  localStorage.setItem('current_tenant_id', 'current-company');
  const current = new Blob(['EDITED']);
  const fetcher = vi.fn().mockImplementation(async () => ({ ok: true, blob: async () => current }));
  vi.stubGlobal('fetch', fetcher);
  const saved = vi.spyOn(downloads, 'saveBlob').mockImplementation(() => {});
  const open = vi.fn();
  render(<ArtifactCards agentId="employee" viewerUserId="owner" artifacts={restoredInput()} onOpenArtifact={open} />);
  expect(screen.queryByRole('button', { name: 'Original upload' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Download' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(current, 'source.bin'));
  expect(fetcher).toHaveBeenNthCalledWith(1, expect.stringContaining('/api/agents/employee/files/download?'), expect.objectContaining({
    headers: { Authorization: 'Bearer current-owner-token', 'X-Tenant-Id': 'current-company' },
  }));
  fireEvent.click(screen.getByRole('button', { name: 'Open' }));
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ path: 'workspace/uploads/source.bin' }));
});

it.each([['other', 'owner'], ['owner', 'forged-owner'], ['owner', 'owner']])(
  'shows the same plain attachment actions for viewer %s and actor %s',
  (viewer, actor) => {
    render(<ArtifactCards agentId="employee" viewerUserId={viewer} artifacts={restoredInput(actor)} />);
    expect(screen.queryByRole('button', { name: 'Original upload' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Withdraw original' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy();
  },
);
