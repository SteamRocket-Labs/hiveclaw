// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { useAuthStore } from '../../stores';
import WorkspaceOriginalStorageSection from './WorkspaceOriginalStorageSection';

const actor = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const tenant = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const blob = '11111111-1111-4111-8111-111111111111';
const reference = '22222222-2222-4222-8222-222222222222';
const digest = 'a'.repeat(64);
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); useAuthStore.setState({ user: null, token: null }); vi.unstubAllGlobals(); });
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

function transport({ loseReceipt = false, held = false, mismatchedReceipt = false, withdrawal = false } = {}) {
  let present = true;
  let withdrawn = false;
  let result: Record<string, unknown> | null = null;
  const calls = vi.fn(async (url: string, request: RequestInit) => {
    if (url.includes('/storage-original-retention?')) return json({ enabled: false, retention_days: 30, grace_days: 7, version: 1, source: 'company_admin' });
    if (url.includes('/storage-original-catalog?')) return json({ items: [{ blob_id: blob, provider: 'filesystem', content_sha256: digest,
      size_bytes: 18, state: 'available', reference_count: present ? 1 : 0, created_at: '2026-01-01T00:00:00Z', active_sweep: null }], next_cursor: null });
    if (url.includes('/operations/references/')) return json({ operation_id: result?.operation_id, status: 'completed',
      result: { ...result, ...(mismatchedReceipt ? { reference_id: '33333333-3333-4333-8333-333333333333' } : {}) } });
    if (url.includes('/references?') && request.method !== 'POST') return json({ items: present ? [{ reference_id: reference,
      owner_type: withdrawal ? 'chat_upload' : 'session_input', retirable: !held, hold_reason: held ? 'legal_hold' : null, snapshot_sha256: 'b'.repeat(64),
      withdrawable: withdrawal && !withdrawn, withdrawal_snapshot_sha256: 'c'.repeat(64), withdrawal_hold_reason: null,
      created_at: '2026-01-01T00:00:00Z', pinned_until: null, legal_hold: held }] : [], next_cursor: null });
    if (url.includes('/references?') && request.method === 'POST') {
      const body = JSON.parse(request.body as string);
      result = { operation_id: body.operation_id, blob_id: blob, reference_id: reference, state: 'available',
        outcome: body.action === 'withdraw_source' ? 'source_withdrawn' : 'reference_retired',
        changed: true, replayed: false, hold_reason: null, observed_at: '2026-01-02T00:00:00Z', physical_bytes_reclaimed: null,
        recovery_action: 'inspect_original_retention' };
      if (body.action === 'withdraw_source') withdrawn = true;
      else present = false;
      if (loseReceipt) throw new TypeError('Synthetic post-commit receipt loss');
      return json(result);
    }
    throw new Error(`Unexpected source operation: ${request.method} ${url}`);
  });
  vi.stubGlobal('fetch', calls);
  return calls;
}

async function mount() {
  useAuthStore.getState().setAuth({ id: actor, username: 'operator', email: 'operator@fixture.test', display_name: 'Operator',
    role: 'org_admin', tenant_id: tenant, is_active: true, created_at: '2026-01-01' }, 'synthetic-token');
  localStorage.setItem('current_tenant_id', tenant);
  const i18n = createInstance();
  await i18n.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  const view = render(<I18nextProvider i18n={i18n}><WorkspaceOriginalStorageSection selectedTenantId={tenant} /></I18nextProvider>);
  await screen.findByRole('button', { name: 'Retained references' });
  return view;
}
async function review() {
  fireEvent.click(screen.getByRole('button', { name: 'Retained references' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Retire this reference' }));
  return screen.findByRole('dialog', { name: 'Review reference retirement' });
}

it('requires explicit review and submits only the exact original/reference snapshots', async () => {
  const calls = transport();
  await mount();
  const modal = await review();
  expect(calls.mock.calls.some(([, request]) => request.method === 'POST')).toBe(false);
  fireEvent.click(within(modal).getByRole('button', { name: 'Confirm reference retirement' }));
  await screen.findByText('Reference retirement recorded. The original has not been physically deleted.');
  await screen.findByText('No retained references remain for this original.');
  const posts = calls.mock.calls.filter(([, request]) => request.method === 'POST');
  expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0][1].body as string)).toEqual({ operation_id: expect.any(String), action: 'retire_reference',
    expected_content_sha256: digest, reference_id: reference, expected_reference_sha256: 'b'.repeat(64) });
  expect(posts[0][1].headers).toEqual(expect.objectContaining({ Authorization: 'Bearer synthetic-token', 'X-Tenant-Id': tenant }));
  expect(calls.mock.calls.some(([url]) => url.includes('/sweep?') || url.includes('/retention?'))).toBe(false);
});

it.each([false, true])('recovers an acknowledged-lost operation after remount without a new mutation (mismatch=%s)', async (mismatch) => {
  const calls = transport({ loseReceipt: true, mismatchedReceipt: mismatch });
  const first = await mount();
  const modal = await review();
  fireEvent.click(within(modal).getByRole('button', { name: 'Confirm reference retirement' }));
  await screen.findByText(en.enterprise.originals.outcomeUnknown);
  first.unmount();
  await mount();
  fireEvent.click(screen.getByRole('button', { name: en.enterprise.originals.checkSubmitted }));
  if (mismatch) {
    await screen.findByText(en.enterprise.originals.operationFailed);
    expect(screen.queryByText('Reference retirement recorded. The original has not been physically deleted.')).toBeNull();
    expect(sessionStorage.length).toBeGreaterThan(0);
  } else {
    await screen.findByText('Reference retirement recorded. The original has not been physically deleted.');
    await waitFor(() => expect(sessionStorage.length).toBe(0));
  }
  expect(calls.mock.calls.filter(([, request]) => request.method === 'POST')).toHaveLength(1);
  expect(calls.mock.calls.some(([url]) => url.includes('/operations/references/'))).toBe(true);
});

it('keeps a held reference visible without offering retirement', async () => {
  transport({ held: true });
  await mount();
  fireEvent.click(screen.getByRole('button', { name: 'Retained references' }));
  await screen.findByText('Legal retention is active.');
  expect(screen.queryByRole('button', { name: 'Retire this reference' })).toBeNull();
  expect(screen.queryByRole('button', { name: en.enterprise.originals.withdrawInactive })).toBeNull();
});

it.each([false, true])('reviews inactive-source disposition separately and recovers its exact request (lost=%s)', async (lost) => {
  const calls = transport({ held: true, withdrawal: true, loseReceipt: lost });
  const first = await mount();
  fireEvent.click(screen.getByRole('button', { name: en.enterprise.originals.manageReferences }));
  fireEvent.click(await screen.findByRole('button', { name: en.enterprise.originals.withdrawInactive }));
  const modal = await screen.findByRole('dialog', { name: en.enterprise.originals.dispositionReview });
  expect(within(modal).getByText(en.enterprise.originals.dispositionWarning)).toBeTruthy();
  expect(calls.mock.calls.filter(([, request]) => request.method === 'POST')).toHaveLength(0);
  fireEvent.click(within(modal).getByRole('button', { name: en.enterprise.originals.dispositionConfirm }));
  if (lost) {
    await screen.findByText(en.enterprise.originals.outcomeUnknown);
    first.unmount();
    await mount();
    fireEvent.click(screen.getByRole('button', { name: en.enterprise.originals.checkSubmitted }));
  }
  await screen.findByText(en.enterprise.originals.dispositionRecorded);
  const posts = calls.mock.calls.filter(([, request]) => request.method === 'POST');
  expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0][1].body as string)).toEqual({ operation_id: expect.any(String), action: 'withdraw_source',
    expected_content_sha256: digest, reference_id: reference, expected_reference_sha256: 'c'.repeat(64) });
  expect(calls.mock.calls.some(([url]) => url.includes('/sweep?') || url.includes('/retention?'))).toBe(false);
});
