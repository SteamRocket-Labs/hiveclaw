// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import type { StorageRetention } from '../../api/domains/enterprise';
import WorkspaceStorageSection from './WorkspaceStorageSection';

const policy = (days = 30, version = 0): StorageRetention => ({
  policy: { enabled: true, retention_days: days, version, source: version ? 'company_admin' : 'installation_default' },
  capacity: { status: 'operator_only', scope: 'shared_agent_volume' },
  applies_to: 'unreferenced_chat_artifact_snapshots_and_orphan_office_previews',
  effective_on: 'next_maintenance_pass',
});
const response = (value: StorageRetention) => new Response(JSON.stringify(value), { status: 200 });
async function mount(locale = 'en') {
  const i18n = createInstance();
  await i18n.init({ lng: locale, resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
  const view = (tenant: string) => <I18nextProvider i18n={i18n}><WorkspaceStorageSection selectedTenantId={tenant} /></I18nextProvider>;
  return { ...render(view('company-a')), view };
}
beforeEach(() => { localStorage.setItem('token', 'auth-token'); localStorage.setItem('current_tenant_id', 'company-a'); });
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });

describe('Company storage policy through the real HTTP adapter', () => {
  it.each(['en', 'zh'])('loads, edits and confirms saved policy in %s', async (locale) => {
    const fetcher = vi.fn().mockResolvedValueOnce(response(policy())).mockResolvedValueOnce(response({
      ...policy(0.25, 1), policy: { ...policy(0.25, 1).policy, enabled: false },
      capacity: { status: 'available', scope: 'shared_agent_volume', total_bytes: 10 * 2 ** 30, used_bytes: 2 ** 30, free_bytes: 9 * 2 ** 30 },
    }));
    vi.stubGlobal('fetch', fetcher);
    await mount(locale);
    const days = await screen.findByRole('spinbutton');
    expect((days as HTMLInputElement).value).toBe('30');
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.change(days, { target: { value: '0.25' } });
    fireEvent.click(screen.getByRole('button', { name: locale === 'en' ? 'Save' : '保存' }));
    await screen.findByText(locale === 'en'
      ? 'Policy saved. It will be used by the next maintenance pass.' : '策略已保存，将由下一次维护使用。');
    const [url, options] = fetcher.mock.calls[1];
    expect(url).toBe('/api/enterprise/storage-retention?tenant_id=company-a');
    expect(options.headers).toMatchObject({ Authorization: 'Bearer auth-token', 'X-Tenant-Id': 'company-a' });
    expect(JSON.parse(options.body)).toEqual({ enabled: false, retention_days: 0.25, expected_version: 0 });
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText(locale === 'en' ? /shared physical capacity/ : /共享物理容量/)).toBeTruthy();
  });

  it.each(['lost_response', 'version_conflict'])('requires readback after %s without resubmitting a save', async (mode) => {
    const fetcher = vi.fn().mockResolvedValueOnce(response(policy()));
    if (mode === 'lost_response') fetcher.mockRejectedValueOnce(new TypeError('Connection lost after commit'));
    else fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'storage_retention_version_conflict' } }), { status: 409 }));
    fetcher.mockResolvedValueOnce(response(policy(7, 2)));
    vi.stubGlobal('fetch', fetcher);
    await mount();
    await screen.findByRole('spinbutton');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByRole('alert');
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
    expect(fetcher).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved policy' }));
    await waitFor(() => expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('7'));
    expect(screen.getByText('Company policy · version 2')).toBeTruthy();
    expect(fetcher.mock.calls.filter(([, options]) => options.method === 'PUT')).toHaveLength(1);
  });

  it('ignores an earlier company response across A-B-A switching', async () => {
    let finishOld!: (value: Response) => void;
    const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { finishOld = resolve; }))
      .mockResolvedValueOnce(response(policy(2, 1))).mockResolvedValueOnce(response(policy(9, 3)));
    vi.stubGlobal('fetch', fetcher);
    const mounted = await mount();
    localStorage.setItem('current_tenant_id', 'company-b');
    mounted.rerender(mounted.view('company-b'));
    await screen.findByRole('spinbutton');
    localStorage.setItem('current_tenant_id', 'company-a');
    mounted.rerender(mounted.view('company-a'));
    await waitFor(() => expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('9'));
    await act(async () => { finishOld(response(policy(30))); });
    expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('9');
    expect(screen.getByText('Company policy · version 3')).toBeTruthy();
  });

  it('does not display a late save acknowledgement in the next company', async () => {
    let finishSave!: (value: Response) => void;
    const fetcher = vi.fn().mockResolvedValueOnce(response(policy()))
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { finishSave = resolve; }))
      .mockResolvedValueOnce(response(policy(4, 2)));
    vi.stubGlobal('fetch', fetcher);
    const mounted = await mount();
    await screen.findByRole('spinbutton');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    localStorage.setItem('current_tenant_id', 'company-b');
    mounted.rerender(mounted.view('company-b'));
    await waitFor(() => expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('4'));
    await act(async () => { finishSave(response(policy(30, 1))); });
    expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('4');
    expect(screen.queryByText('Policy saved. It will be used by the next maintenance pass.')).toBeNull();
  });
});
