// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { useAuthStore } from '../../stores';
import WorkspaceStorageSection from './WorkspaceStorageSection';

const blobId = '11111111-1111-4111-8111-111111111111';
const digest = 'a'.repeat(64);
const manifest = 'b'.repeat(64);
const original = () => ({ blob_id: blobId, state: 'quarantined', provider: 'filesystem', content_sha256: digest,
  size_bytes: 18, reference_count: 0, active_sweep: null, operation_conflict: false,
  created_at: '2026-01-01T00:00:00+00:00', verified_at: '2026-01-01T00:00:00+00:00',
  quarantined_at: '2026-01-02T00:00:00+00:00', delete_after: '2026-01-03T00:00:00+00:00', deleted_at: null });
const policy = () => ({ enabled: true, retention_days: 0, grace_days: 0, version: 1, source: 'company_admin' });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function setupFetcher(options: { loseDeletion?: 'before' | 'after' } = {}) {
  const row = original();
  let saved: Record<string, unknown> | null = null;
  let dropped = false;
  const fetcher = vi.fn(async (url: string, request: RequestInit) => {
    if (url.includes('/storage-retention?')) return json({ policy: { enabled: true, retention_days: 30, version: 0, source: 'installation_default' },
      capacity: { status: 'operator_only', scope: 'shared_agent_volume' } });
    if (url.includes('/storage-original-retention?')) return json(policy());
    if (url.includes('/storage-original-catalog?')) return json({ items: [row], next_cursor: null });
    if (url.includes('/operations/sweep/')) return saved ? json({ status: 'completed', result: saved }) : json({ detail: { code: 'original_operation_not_found' } }, 404);
    if (url.includes('/sweep?')) {
      const body = JSON.parse(request.body as string);
      expect(Array.from({ length: sessionStorage.length }, (_, index) => sessionStorage.getItem(sessionStorage.key(index)!)).join('')).toContain(body.operation_id);
      if (options.loseDeletion === 'before' && !dropped) { dropped = true; throw new TypeError('Synthetic request not delivered'); }
      row.state = 'deleted';
      saved = { ...body, blob_id: blobId, state: 'deleted', outcome: 'absent', manifest_sha256: manifest,
        policy_version: 1, replayed: false, observed_at: '2026-01-04T00:00:00+00:00', recovery_action: 'none', physical_bytes_reclaimed: null };
      if (options.loseDeletion === 'after' && !dropped) { dropped = true; throw new TypeError('Synthetic acknowledgement lost'); }
      return json(saved);
    }
    if (url.includes('/retention?')) {
      const body = JSON.parse(request.body as string);
      return json({ ...body, blob_id: blobId, policy_version: 1, state: row.state, changed: false, hold_reason: null,
        observation: null, delete_after: row.delete_after, manifest_sha256: manifest, sweep_eligible: true,
        observed_at: '2026-01-04T00:00:00+00:00', replayed: false, recovery_action: 'none' });
    }
    throw new Error(`Unexpected HTTP request: ${request.method} ${url}`);
  });
  return { row, fetcher };
}
async function mount(locale = 'en') {
  const i18n = createInstance();
  await i18n.init({ lng: locale, resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
  const view = (tenant: string) => <I18nextProvider i18n={i18n}><WorkspaceStorageSection selectedTenantId={tenant} /></I18nextProvider>;
  const mounted = render(view('company-a'));
  fireEvent.click(await screen.findByRole('button', { name: locale === 'en' ? 'Manage original attachments' : '管理原件' }));
  return { ...mounted, view };
}
beforeEach(() => {
  localStorage.setItem('token', 'auth-token');
  localStorage.setItem('current_tenant_id', 'company-a');
  useAuthStore.setState({ token: 'auth-token', user: { id: 'actor-a', username: 'admin', email: 'admin@fixture.test',
    display_name: 'Administrator', role: 'org_admin', tenant_id: 'company-a', is_active: true, created_at: '2026-01-01' } });
});
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); useAuthStore.setState({ user: null, token: null }); vi.unstubAllGlobals(); });

describe('Original operations through the current company storage entry', () => {
  it.each(['en', 'zh'])('loads the policy/catalog and reviews exact deletion before any effect in %s', async (locale) => {
    const { fetcher } = setupFetcher();
    vi.stubGlobal('fetch', fetcher);
    await mount(locale);
    const section = await screen.findByRole('region', { name: locale === 'en' ? 'Original attachments' : '原件管理' });
    expect((await within(section).findByLabelText(locale === 'en' ? 'Grace period (days)' : '隔离宽限天数') as HTMLInputElement).value).toBe('0');
    fireEvent.click(await within(section).findByRole('button', { name: locale === 'en' ? 'Inspect original' : '检查原件' }));
    fireEvent.click(await within(section).findByRole('button', { name: locale === 'en' ? 'Delete original' : '删除原件' }));
    const review = await screen.findByRole('dialog');
    expect(within(review).getByText(digest)).toBeTruthy();
    expect(fetcher.mock.calls.filter(([url]) => url.includes('/sweep?'))).toHaveLength(0);
  });

  it('persists the reviewed nonce before send and reads a lost response after remount without re-deleting', async () => {
    const { fetcher } = setupFetcher({ loseDeletion: 'after' });
    vi.stubGlobal('fetch', fetcher);
    const mounted = await mount();
    const section = await screen.findByRole('region', { name: 'Original attachments' });
    fireEvent.click(await within(section).findByRole('button', { name: 'Inspect original' }));
    fireEvent.click(await within(section).findByRole('button', { name: 'Delete original' }));
    const confirm = within(await screen.findByRole('dialog')).getByRole('button', { name: 'Confirm deletion' });
    act(() => { fireEvent.click(confirm); fireEvent.click(confirm); });
    await screen.findByText(/operation response is unknown/i);
    const first = fetcher.mock.calls.find(([url]) => url.includes('/sweep?'))!;
    const body = JSON.parse(first[1].body as string);
    expect(first[1].headers).toMatchObject({ Authorization: 'Bearer auth-token', 'X-Tenant-Id': 'company-a' });
    expect(body.expected_manifest_sha256).toBe(manifest);
    expect(fetcher.mock.calls.filter(([url]) => url.includes('/sweep?'))).toHaveLength(1);
    mounted.unmount();
    await mount();
    fireEvent.click(await screen.findByRole('button', { name: 'Check submitted operation' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Check submitted operation' })).toBeNull());
    expect(fetcher.mock.calls.some(([url, init]) => init.method === 'GET' && url.includes(`/operations/sweep/${body.operation_id}`))).toBe(true);
    expect(fetcher.mock.calls.filter(([url]) => url.includes('/sweep?'))).toHaveLength(1);
    expect(sessionStorage.length).toBe(0);
  });

  it('keeps a missing receipt unknown and resubmits only the explicitly reconfirmed exact body', async () => {
    const { fetcher } = setupFetcher({ loseDeletion: 'before' });
    vi.stubGlobal('fetch', fetcher);
    await mount();
    const section = await screen.findByRole('region', { name: 'Original attachments' });
    fireEvent.click(await within(section).findByRole('button', { name: 'Inspect original' }));
    fireEvent.click(await within(section).findByRole('button', { name: 'Delete original' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Confirm deletion' }));
    await screen.findByText(/operation response is unknown/i);
    fireEvent.click(await screen.findByRole('button', { name: 'Check submitted operation' }));
    await screen.findByText(/request may still arrive/i);
    const first = fetcher.mock.calls.find(([url]) => url.includes('/sweep?'))!;
    fireEvent.click(await screen.findByRole('button', { name: 'Review exact resubmission' }));
    expect(fetcher.mock.calls.filter(([url]) => url.includes('/sweep?'))).toHaveLength(1);
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Confirm deletion' }));
    await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.includes('/sweep?'))).toHaveLength(2));
    const requests = fetcher.mock.calls.filter(([url]) => url.includes('/sweep?'));
    expect(requests[1][1].body).toBe(first[1].body);
  });

  it('does not expose another actor/company pending record or consume its late acknowledgement', async () => {
    const { fetcher: normal } = setupFetcher();
    let finish!: (response: Response) => void;
    const fetcher = vi.fn((url: string, init: RequestInit) => url.includes('/sweep?')
      ? new Promise<Response>((resolve) => { finish = resolve; }) : normal(url, init));
    vi.stubGlobal('fetch', fetcher);
    const mounted = await mount();
    const section = await screen.findByRole('region', { name: 'Original attachments' });
    fireEvent.click(await within(section).findByRole('button', { name: 'Inspect original' }));
    fireEvent.click(await within(section).findByRole('button', { name: 'Delete original' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Confirm deletion' }));
    const old = JSON.parse(fetcher.mock.calls.find(([url]) => url.includes('/sweep?'))![1].body as string);
    await act(async () => {
      localStorage.setItem('current_tenant_id', 'company-b');
      useAuthStore.setState({ user: { ...useAuthStore.getState().user!, id: 'actor-b', tenant_id: 'company-b' } });
      mounted.rerender(mounted.view('company-b'));
    });
    await screen.findByRole('region', { name: 'Original attachments' });
    expect(screen.queryByRole('button', { name: 'Check submitted operation' })).toBeNull();
    await act(async () => { finish(json({ operation_id: old.operation_id, blob_id: blobId, state: 'deleted', replayed: false, observed_at: '2026-01-04', recovery_action: 'none' })); });
    expect(screen.queryByText(new RegExp(old.operation_id))).toBeNull();
    expect(fetcher.mock.calls.filter(([url]) => url.includes('/sweep?'))).toHaveLength(1);
  });

  it('does not erase a newer recovery record when an earlier A-B-A instance finishes late', async () => {
    const { fetcher: normal } = setupFetcher();
    let finish!: (response: Response) => void;
    const fetcher = vi.fn((url: string, init: RequestInit) => url.includes('/sweep?')
      ? new Promise<Response>((resolve) => { finish = resolve; }) : normal(url, init));
    vi.stubGlobal('fetch', fetcher);
    const mounted = await mount();
    const section = await screen.findByRole('region', { name: 'Original attachments' });
    fireEvent.click(await within(section).findByRole('button', { name: 'Inspect original' }));
    fireEvent.click(await within(section).findByRole('button', { name: 'Delete original' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Confirm deletion' }));
    const key = sessionStorage.key(0)!;
    const earlier = JSON.parse(sessionStorage.getItem(key)!);
    mounted.unmount();
    // Another instance has explicitly started newer work under the same actor
    // and company. Its recovery record must survive an old acknowledgement.
    const newer = { ...earlier, request: { ...earlier.request, operation_id: crypto.randomUUID() } };
    sessionStorage.setItem(key, JSON.stringify(newer));
    await mount();
    await act(async () => { finish(json({ operation_id: earlier.request.operation_id, blob_id: blobId, state: 'deleted',
      replayed: false, observed_at: '2026-01-04', recovery_action: 'none' })); });
    expect(JSON.parse(sessionStorage.getItem(key)!).request.operation_id).toBe(newer.request.operation_id);
    expect(screen.getByText(new RegExp(newer.request.operation_id))).toBeTruthy();
    expect(screen.queryByText(new RegExp(earlier.request.operation_id))).toBeNull();
  });

  it('requires original-policy readback after a lost save acknowledgement', async () => {
    const { fetcher: normal } = setupFetcher();
    let saved = false;
    const fetcher = vi.fn((url: string, init: RequestInit) => {
      if (url.includes('/storage-original-retention?') && init.method === 'PUT') {
        saved = true;
        return Promise.reject(new TypeError('Synthetic policy acknowledgement loss'));
      }
      if (url.includes('/storage-original-retention?') && saved) return Promise.resolve(json({ ...policy(), retention_days: 7, version: 2 }));
      return normal(url, init);
    });
    vi.stubGlobal('fetch', fetcher);
    await mount();
    const section = await screen.findByRole('region', { name: 'Original attachments' });
    await within(section).findByLabelText('Grace period (days)');
    fireEvent.click(within(section).getByRole('button', { name: 'Save original policy' }));
    await within(section).findByText(/save outcome is unknown/i);
    expect((within(section).getByRole('button', { name: 'Save original policy' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(section).getByRole('button', { name: 'Reload original policy and records' }));
    await within(section).findByText('Company policy · version 2');
    expect((within(section).getByLabelText('Original retention (days)') as HTMLInputElement).value).toBe('7');
    expect(fetcher.mock.calls.filter(([url, init]) => url.includes('/storage-original-retention?') && init.method === 'PUT')).toHaveLength(1);
  });
});
