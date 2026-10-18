/** Artifact-side input and current-file editing consume the real RLS-backed API. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import { ArtifactPreviewPanel } from '../src/pages/agent-detail/ArtifactSurface';
import type { SessionMessageOptions } from '../src/pages/agent-detail/ArtifactContinuation';
import { fileApi } from '../src/api/domains/files';
import { chatApi } from '../src/api/domains/chat';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';
import en from '../src/i18n/en.json';

it('saves the canonical file and retries one original-work input after acknowledgement loss', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  const user = process.env.HIVE_G90_USER_ID || '';
  const tenant = process.env.HIVE_G90_TENANT_ID || '';
  const agent = process.env.HIVE_G90_AGENT_ID || '';
  const session = process.env.HIVE_ARTIFACT_SESSION_ID || '';
  const artifactId = process.env.HIVE_ARTIFACT_ID || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  expect(user && tenant && agent && session && artifactId).toBeTruthy();
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only the owned loopback API is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  useAuthStore.getState().setAuth({ id: user, tenant_id: tenant, role: 'org_admin', username: 'Artifact owner' } as User, 'synthetic-artifact-owner');
  const inputIds: string[] = [];
  let loseAck = true;
  try {
    const snapshot = await fileApi.readArtifact(agent, artifactId);
    expect(snapshot.content).toBe('CANONICAL_ENVIRONMENT_FILE');
    const send = async (content: string, options?: SessionMessageOptions) => {
      expect(options?.expectedSessionId).toBe(session);
      expect(content).not.toContain('_HUMAN_CORRECTION_');
      inputIds.push(options!.inputId!);
      const receipt = await chatApi.startSessionRun(agent, session, { content, display_content: options?.displayContent,
        input_id: options!.inputId!, idempotency_key: `session-input:${options!.inputId!}`, permission_mode: 'bypassPermissions' });
      if (loseAck) { loseAck = false; throw new Error('Simulated lost acknowledgement'); }
      return receipt;
    };
    render(<I18nextProvider i18n={language}><ArtifactPreviewPanel agentId={agent}
      preview={{ artifact: { id: artifactId, name: 'saved.txt', path: 'workspace/saved.txt', downloadAgentId: agent },
        content: snapshot.content, usingSnapshot: true }}
      onClose={() => undefined} t={language.t} continuation={{ sessionId: session, send }} /></I18nextProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
    const edit = screen.getByRole('button', { name: 'Edit' }) as HTMLButtonElement;
    await waitFor(() => expect(edit.disabled).toBe(false));
    fireEvent.click(edit);
    fireEvent.change(await screen.findByRole('textbox', { name: 'Your draft' }), { target: { value: 'CANONICAL_ENVIRONMENT_FILE_HUMAN_CORRECTION_决定性尾部' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Your draft' })).toBeNull());
    const followUp = screen.getByRole('textbox', { name: 'Continue with this file' });
    fireEvent.change(followUp, { target: { value: 'Apply my saved correction and continue this file.' } });
    await waitFor(() => expect((screen.getByRole('button', { name: 'Continue in this work' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Continue in this work' }));
    await screen.findByText('Simulated lost acknowledgement');
    expect((followUp as HTMLTextAreaElement).value).toBe('Apply my saved correction and continue this file.');
    fireEvent.click(screen.getByRole('button', { name: 'Continue in this work' }));
    await screen.findByText('Your input is recorded. Follow progress in this work.');
    expect(inputIds).toHaveLength(2);
    expect(inputIds[1]).toBe(inputIds[0]);
    expect((await fileApi.readArtifact(agent, artifactId)).content).toBe(snapshot.content);
    console.info(`HIVE_ARTIFACT_INPUT_IDS=${JSON.stringify(inputIds)}`);
  } finally { cleanup(); globalThis.fetch = original; useAuthStore.getState().logout(); }
});
