// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { fileApi } from '../../api/domains/files';
import { saveBlob } from '../../utils/authenticatedResource';
import { ArtifactCards } from './ArtifactSurface';
import { parseUserAttachment } from './chatAttachments';
import type { AgentChatMessage } from './chatRuntime';

vi.mock('../../api/domains/files', () => ({ fileApi: { download: vi.fn(), downloadArtifact: vi.fn() } }));
vi.mock('../../utils/authenticatedResource', async () => ({
  ...await vi.importActual<typeof import('../../utils/authenticatedResource')>('../../utils/authenticatedResource'), saveBlob: vi.fn(),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (_key: string, fallback: string, variables: Record<string, unknown> = {}) => Object.entries(variables)
    .reduce((text, [key, value]) => text.replace(`{{${key}}}`, String(value)), fallback),
}) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it.each([false, true])('opens and downloads the exact input with operator scope %s', async (operatorView) => {
  const path = 'workspace/uploads/dingtalk/receipt/amount]report.csv';
  const parts = [{ type: 'attachment_ref', attachment: { path, name: 'amount]report.csv', size: 12 } }];
  const message: AgentChatMessage = { role: 'user', content: JSON.stringify(parts), sessionItem: {
    id: 'input', kind: 'human_input', scope: { level: 'session', session_id: 'work', thread_id: 'work' },
    lifecycle: 'accepted', terminal: false, revision: 0, content: JSON.stringify(parts), payload: { content_parts: parts },
    actor: { type: 'user', id: 'owner' }, visibility: { audience: 'private' }, occurredAt: '2026-10-19T00:00:00Z',
    first_sequence: 1, last_sequence: 1,
  } };
  const parsed = parseUserAttachment(message, 'employee');
  const blob = new Blob(['amount\n17\n']);
  vi.mocked(fileApi.download).mockResolvedValue(blob);
  const open = vi.fn();
  render(<><div>{parsed.content}</div><ArtifactCards agentId="employee" artifacts={parsed.inputAttachments}
    onOpenArtifact={open} operatorView={operatorView} operatorReason={operatorView ? 'Review original input' : undefined} /></>);
  expect(screen.queryByText(/attachment_ref/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Open amount]report.csv' }));
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ path, name: 'amount]report.csv', source: 'human_input' }));
  fireEvent.click(screen.getByRole('button', { name: 'Download' }));
  await waitFor(() => expect(saveBlob).toHaveBeenCalledWith(blob, 'amount]report.csv'));
  expect(fileApi.download).toHaveBeenCalledWith('employee', path,
    operatorView ? { operatorView: true, reason: 'Review original input' } : undefined);
  expect(fileApi.downloadArtifact).not.toHaveBeenCalled();
});
