// @vitest-environment jsdom
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

import { ArtifactCards } from './ArtifactSurface';
import { parseUserAttachment } from './chatAttachments';
import * as downloads from '../../utils/authenticatedResource';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }) }));
const file = process.env.HIVE_CHANNEL_ORIGINAL_BRIDGE;
const bridge = file ? JSON.parse(readFileSync(file, 'utf8')) : null;

it.skipIf(!bridge)('downloads the real native channel original through the mounted file card', async () => {
  localStorage.setItem('token', bridge.token);
  localStorage.setItem('current_tenant_id', bridge.tenant);
  const transport = globalThis.fetch.bind(globalThis);
  const calls: string[] = [];
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push(`${init?.method || 'GET'} ${input}`);
    return transport(new URL(String(input), bridge.origin), init);
  });
  let saved: Blob | null = null;
  vi.spyOn(downloads, 'saveBlob').mockImplementation((blob, name) => { expect(name).toBe('source.bin'); saved = blob; });
  const legacy = parseUserAttachment({ role: 'user', content: `[file:${bridge.path}]` }, bridge.agent);
  expect(legacy.originalWorkspacePath).toBe(bridge.path);
  render(<ArtifactCards agentId={bridge.agent} viewerUserId={bridge.actor} artifacts={[
    { path: legacy.originalWorkspacePath!, name: legacy.fileName!, source: 'human_input' },
  ]} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Original upload' }));
  await waitFor(() => expect(saved).not.toBeNull());
  const bytes = Buffer.from(await (saved as unknown as Blob).arrayBuffer());
  expect(createHash('sha256').update(bytes).digest('hex')).toBe(bridge.digest);
  expect(calls).toHaveLength(2);
  expect(calls[0]).toContain('GET /api/chat/attachments/workspace-original?');
  expect(calls[1]).toMatch(/^GET \/api\/chat\/attachments\/[^/]+\/download$/);
  writeFileSync(bridge.output, JSON.stringify({ calls, digest: createHash('sha256').update(bytes).digest('hex'), size: bytes.length }));
});
