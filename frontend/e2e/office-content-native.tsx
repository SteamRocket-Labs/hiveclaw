/** Native Office editing through the mounted workspace and the actual owned HTTP service. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import { URL as NodeURL } from 'node:url';
import { Blob as NodeBlob } from 'node:buffer';
import { ArtifactPreviewPanel } from '../src/pages/agent-detail/ArtifactSurface';
import { officeApi } from '../src/api/domains/office';
import { chatApi } from '../src/api/domains/chat';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';
import en from '../src/i18n/en.json';

it('saves three Office carriers, reconciles a lost save ACK and continues the original work', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  const agent = process.env.HIVE_G90_AGENT_ID || '';
  const tenant = process.env.HIVE_G90_TENANT_ID || '';
  const user = process.env.HIVE_G90_USER_ID || '';
  const session = process.env.HIVE_OFFICE_SESSION_ID || '';
  const artifacts = JSON.parse(process.env.HIVE_OFFICE_ARTIFACTS || '{}') as Record<string, string>;
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  expect(agent && tenant && user && session).toBeTruthy();
  const original = globalThis.fetch;
  const createObjectURL = URL.createObjectURL;
  const revokeObjectURL = URL.revokeObjectURL;
  URL.createObjectURL = NodeURL.createObjectURL;
  URL.revokeObjectURL = NodeURL.revokeObjectURL;
  let loseAck = true;
  const requests: Array<{ method: string; route: string; status: number; milliseconds: number }> = [];
  globalThis.fetch = async (input, options) => {
    if (typeof input !== 'string' || !input.startsWith('/api/')) throw new Error('Only the owned loopback API is permitted');
    const started = performance.now();
    const response = await original(new URL(input, base), options);
    requests.push({ method: options?.method || 'GET', route: new URL(input, base).pathname,
      status: response.status, milliseconds: Math.round(performance.now() - started) });
    if (loseAck && options?.method === 'PUT' && input.includes('/office/content') && response.ok) {
      loseAck = false; throw new Error('Simulated lost Office save acknowledgement');
    }
    // jsdom's response Blob and Node's object URL registry use different realms.
    // Preserve the actual HTTP bytes and media type at this headless host bridge.
    const readBlob = response.blob.bind(response);
    response.blob = async () => {
      const blob = await readBlob();
      return new NodeBlob([await blob.arrayBuffer()], { type: blob.type }) as unknown as Blob;
    };
    return response;
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  useAuthStore.getState().setAuth({ id: user, tenant_id: tenant, role: 'org_admin', username: 'Office owner' } as User, 'synthetic-office-owner');
  let inputId = '';
  try {
    for (const kind of ['docx', 'xlsx', 'pptx']) {
      const path = `workspace/human.${kind}`;
      const snapshot = await officeApi.getArtifactPreview(agent, artifacts[path]);
      expect(await snapshot.text()).not.toContain('决定性尾部');
      const snapshotUrl = URL.createObjectURL(snapshot);
      const view = render(<I18nextProvider i18n={language}><ArtifactPreviewPanel agentId={agent}
        preview={{ artifact: { id: artifacts[path], name: `human.${kind}`, path }, url: snapshotUrl, usingSnapshot: true }}
        onClose={() => undefined} t={language.t as never} continuation={{ sessionId: session, send: async (content, options) => {
          expect(options?.expectedSessionId).toBe(session);
          expect(content).not.toContain('_UI_SAVED');
          inputId = options!.inputId!;
          return chatApi.startSessionRun(agent, session, { content, display_content: options?.displayContent,
            input_id: inputId, idempotency_key: `session-input:${inputId}`, permission_mode: 'bypassPermissions' });
        } }} /></I18nextProvider>);
      fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
      fireEvent.click(screen.getByRole('button', { name: 'Edit contents' }));
      const name = kind === 'docx' ? 'Text block 1' : kind === 'xlsx' ? 'Evidence/A1' : 'TextBox 1';
      const field = await screen.findByRole('textbox', { name }, { timeout: 10000 });
      fireEvent.change(field, { target: { value: `Human ${kind} correction 决定性尾部_UI_SAVED` } });
      const followUp = screen.getByRole('button', { name: 'Continue in this work' }) as HTMLButtonElement;
      if (kind === 'pptx') fireEvent.change(screen.getByRole('textbox', { name: 'Continue with this file' }), {
        target: { value: 'Read my saved corrections from workspace/human.docx, workspace/human.xlsx and workspace/human.pptx, and deliver a report.' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
      if (kind === 'docx') {
        await screen.findByText(en.agent.workspace.fileSave.unknown, {}, { timeout: 10000 });
        expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
        expect(screen.getByDisplayValue(`Human ${kind} correction 决定性尾部_UI_SAVED`)).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Load current version' }));
        await screen.findByRole('button', { name: 'Discard my edits and use current version' }, { timeout: 10000 });
        fireEvent.click(screen.getByRole('button', { name: 'Discard my edits and use current version' }));
      }
      await waitFor(() => expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true), { timeout: 10000 });
      await screen.findByText(kind === 'xlsx' ? /Saved\. 1 existing/ : 'Saved', {}, { timeout: 10000 });
      expect((await officeApi.readContent(agent, path)).fields[0].value).toBe(`Human ${kind} correction 决定性尾部_UI_SAVED`);
      if (kind === 'pptx') {
        await waitFor(() => expect(followUp.disabled).toBe(false));
        fireEvent.click(followUp);
        await screen.findByText('Your input is recorded. Follow progress in this work.', {}, { timeout: 10000 });
      }
      view.unmount(); URL.revokeObjectURL(snapshotUrl);
    }
    expect(inputId).toBeTruthy();
    console.info(`HIVE_OFFICE_INPUT_ID=${inputId}`);
  } finally {
    console.info(`HIVE_OFFICE_HTTP_TIMINGS=${JSON.stringify(requests)}`);
    cleanup(); globalThis.fetch = original; useAuthStore.getState().logout();
    URL.createObjectURL = createObjectURL; URL.revokeObjectURL = revokeObjectURL;
  }
});
