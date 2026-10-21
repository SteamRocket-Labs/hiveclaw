// @vitest-environment jsdom

import { readFileSync, writeFileSync } from 'node:fs';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { useAuthStore } from '../../stores';
import WorkspaceStorageSection from './WorkspaceStorageSection';

const profilePath = process.env.HIVE_ORIGINAL_OPERATIONS_BRIDGE;
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); useAuthStore.setState({ user: null, token: null }); vi.unstubAllGlobals(); });

it.skipIf(!profilePath)('consumes the current storage entry against native authentication, RLS and provider effects', async () => {
  const profile = JSON.parse(readFileSync(profilePath!, 'utf8')) as {
    origin: string; token: string; tenant: string; actor: string; digest: string; output: string;
  };
  const wire = globalThis.fetch;
  const calls: { method: string; path: string; body?: Record<string, unknown> }[] = [];
  let dropped = false;
  let committed: Record<string, unknown> | null = null;
  vi.stubGlobal('fetch', async (path: string, options: RequestInit) => {
    const body = options.body ? JSON.parse(options.body as string) as Record<string, unknown> : undefined;
    calls.push({ method: options.method || 'GET', path, body });
    const response = await wire(profile.origin + path, options);
    if (options.method === 'POST' && path.includes('/sweep?') && body?.action === 'sweep' && response.ok && !dropped) {
      committed = await response.clone().json() as Record<string, unknown>;
      dropped = true;
      throw new TypeError('Synthetic acknowledgement loss after actual server commit');
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
  async function button(name: string) {
    const target = await within(screen.getByRole('region', { name: 'Original attachments' })).findByRole('button', { name });
    await waitFor(() => expect((target as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(target);
  }
  const mounted = await mount();
  const section = screen.getByRole('region', { name: 'Original attachments' });
  const enabled = await within(section).findByLabelText('Allow retention of unreferenced originals');
  expect((enabled as HTMLInputElement).checked).toBe(false);
  fireEvent.click(enabled);
  fireEvent.change(within(section).getByLabelText('Original retention (days)'), { target: { value: '0' } });
  fireEvent.change(within(section).getByLabelText('Grace period (days)'), { target: { value: '0' } });
  await button('Save original policy');
  await within(section).findByText('Company policy · version 1');
  await button('Inspect original');
  await button('Recover verified upload');
  await button('Inspect original');
  await button('Quarantine original');
  await button('Inspect original');
  await button('Delete original');
  const review = await screen.findByRole('dialog');
  expect(within(review).getByText(profile.digest)).toBeTruthy();
  expect(calls.filter((call) => call.path.includes('/sweep?'))).toHaveLength(0);
  const confirm = within(review).getByRole('button', { name: 'Confirm deletion' });
  act(() => { fireEvent.click(confirm); fireEvent.click(confirm); });
  await screen.findByText(/operation response is unknown/i);
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(calls.filter((call) => call.path.includes('/sweep?') && call.method === 'POST')).toHaveLength(1);
  const operation = calls.find((call) => call.path.includes('/sweep?'))!.body!.operation_id;
  expect(sessionStorage.length).toBe(1);
  mounted.unmount();
  await mount();
  await button('Check submitted operation');
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Check submitted operation' })).toBeNull());
  expect(calls.filter((call) => call.method === 'GET' && call.path.includes(`/operations/sweep/${operation}`))).toHaveLength(1);
  expect(calls.filter((call) => call.path.includes('/sweep?') && call.method === 'POST')).toHaveLength(1);
  expect(sessionStorage.length).toBe(0);
  expect(committed).toMatchObject({ state: 'deleted', outcome: 'absent', physical_bytes_reclaimed: null });
  writeFileSync(profile.output, JSON.stringify({ operation_id: operation, result: committed, calls, html: document.body.innerHTML }));
}, 60000);
