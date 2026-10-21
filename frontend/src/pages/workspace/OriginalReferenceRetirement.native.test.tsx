// @vitest-environment jsdom
import { readFileSync, writeFileSync } from 'node:fs';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { useAuthStore } from '../../stores';
import WorkspaceStorageSection from './WorkspaceStorageSection';

const profilePath = process.env.HIVE_REFERENCE_RETIREMENT_BRIDGE;
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); useAuthStore.setState({ user: null, token: null }); vi.unstubAllGlobals(); });

it.skipIf(!profilePath)('retires closed work and its withdrawn source through real authenticated storage controls', async () => {
  const profile = JSON.parse(readFileSync(profilePath!, 'utf8')) as { origin: string; actor: string; tenant: string; token: string; output: string };
  const wire = globalThis.fetch;
  const calls: { method: string; path: string; body?: Record<string, unknown> }[] = [];
  let dropped = false;
  vi.stubGlobal('fetch', async (path: string, options: RequestInit) => {
    const body = options.body ? JSON.parse(options.body as string) as Record<string, unknown> : undefined;
    calls.push({ method: options.method || 'GET', path, body });
    const response = await wire(profile.origin + path, options);
    if (options.method === 'POST' && path.includes('/references?') && response.ok && !dropped) {
      dropped = true;
      throw new TypeError('Synthetic lost acknowledgement after actual reference commit');
    }
    return response;
  });
  useAuthStore.getState().setAuth({ id: profile.actor, username: 'operator', email: 'operator@fixture.test', display_name: 'Operator',
    role: 'org_admin', tenant_id: profile.tenant, is_active: true, created_at: '2026-01-01' }, profile.token);
  const i18n = createInstance();
  await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  async function mount() {
    const view = render(<I18nextProvider i18n={i18n}><WorkspaceStorageSection selectedTenantId={profile.tenant} /></I18nextProvider>);
    fireEvent.click(await screen.findByRole('button', { name: 'Manage original attachments' }));
    await screen.findByRole('region', { name: 'Original attachments' });
    return view;
  }
  async function retire() {
    fireEvent.click(await screen.findByRole('button', { name: 'Retained references' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Retire this reference' }));
    const modal = await screen.findByRole('dialog', { name: 'Review reference retirement' });
    fireEvent.click(within(modal).getByRole('button', { name: 'Confirm reference retirement' }));
  }
  const first = await mount();
  await retire();
  await screen.findByText(en.enterprise.originals.outcomeUnknown);
  first.unmount();
  await mount();
  fireEvent.click(screen.getByRole('button', { name: en.enterprise.originals.checkSubmitted }));
  await screen.findByText(en.enterprise.originals.refRecorded);
  await retire();
  await screen.findByText(en.enterprise.originals.refEmpty);
  const mutations = calls.filter(call => call.method === 'POST');
  expect(mutations).toHaveLength(2);
  expect(mutations.every(call => call.path.includes('/references?') && call.body?.action === 'retire_reference')).toBe(true);
  expect(calls.some(call => call.path.includes('/operations/references/'))).toBe(true);
  writeFileSync(profile.output, JSON.stringify({ calls, retired_references: mutations.map(call => call.body!.reference_id),
    post_count: mutations.length, lost_acknowledgement_recovered: dropped }));
}, 60000);
