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

it.skipIf(!profilePath)('explicitly withdraws an inactive source while retaining its held original and recovers the actual lost acknowledgement', async () => {
  const profile = JSON.parse(readFileSync(profilePath!, 'utf8')) as { origin: string; actor: string; tenant: string; token: string; output: string };
  const wire = globalThis.fetch;
  const calls: { method: string; path: string; body?: Record<string, unknown> }[] = [];
  let dropped = false;
  let result: Record<string, unknown> | null = null;
  vi.stubGlobal('fetch', async (path: string, options: RequestInit) => {
    const body = options.body ? JSON.parse(options.body as string) as Record<string, unknown> : undefined;
    calls.push({ method: options.method || 'GET', path, body });
    const response = await wire(profile.origin + path, options);
    if (options.method === 'POST' && body?.action === 'withdraw_source' && response.ok && !dropped) {
      result = await response.clone().json() as Record<string, unknown>;
      dropped = true;
      throw new TypeError('Synthetic lost acknowledgement after actual inactive-source commit');
    }
    return response;
  });
  useAuthStore.getState().setAuth({ id: profile.actor, username: 'operator', email: 'operator@fixture.test', display_name: 'Operator',
    role: 'org_admin', tenant_id: profile.tenant, is_active: true, created_at: '2026-01-01' }, profile.token);
  const i18n = createInstance();
  await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  async function mount() {
    const view = render(<I18nextProvider i18n={i18n}><WorkspaceStorageSection selectedTenantId={profile.tenant} /></I18nextProvider>);
    fireEvent.click(await screen.findByRole('button', { name: en.enterprise.originals.open }));
    fireEvent.click(await screen.findByRole('button', { name: en.enterprise.originals.manageReferences }));
    return view;
  }
  const first = await mount();
  fireEvent.click(await screen.findByRole('button', { name: en.enterprise.originals.withdrawInactive }));
  const modal = await screen.findByRole('dialog', { name: en.enterprise.originals.dispositionReview });
  expect(within(modal).getByText(en.enterprise.originals.dispositionWarning)).toBeTruthy();
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(0);
  fireEvent.click(within(modal).getByRole('button', { name: en.enterprise.originals.dispositionConfirm }));
  await screen.findByText(en.enterprise.originals.outcomeUnknown);
  first.unmount();
  await mount();
  fireEvent.click(screen.getByRole('button', { name: en.enterprise.originals.checkSubmitted }));
  await screen.findByText(en.enterprise.originals.dispositionRecorded);
  await screen.findByText(en.enterprise.originals.refLegalHold);
  expect(screen.queryByRole('button', { name: en.enterprise.originals.withdrawInactive })).toBeNull();
  expect(screen.queryByRole('button', { name: en.enterprise.originals.retireReference })).toBeNull();
  const posts = calls.filter(call => call.method === 'POST');
  expect(posts).toHaveLength(1);
  expect(posts[0].body?.action).toBe('withdraw_source');
  expect(calls.some(call => call.path.includes('/operations/references/'))).toBe(true);
  writeFileSync(profile.output, JSON.stringify({ request: posts[0].body, result, post_count: posts.length,
    lost_acknowledgement_recovered: dropped, calls }));
}, 60000);
