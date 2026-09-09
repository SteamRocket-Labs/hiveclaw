// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { aiAssetsApi, type AIAssetDetail, type AIAssetRecord } from '../../api/domains/aiAssets';
import '../../i18n';
import WorkspaceAIAssetsSection from './WorkspaceAIAssetsSection';

vi.mock('../../api/domains/aiAssets', () => ({ aiAssetsApi: {
  list: vi.fn(), detail: vi.fn(), reconcile: vi.fn(), rollback: vi.fn(),
} }));
vi.mock('../../components/AppDialogs', () => ({ showAppToast: vi.fn(), requestAppConfirm: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

function asset(id: string): AIAssetRecord {
  return {
    id, tenant_id: 'tenant', asset_type: 'skill', native_entity_id: id, native_key: `skill:${id}`,
    display_name: id, owner: { type: 'agent', id: 'agent' }, visibility_scope: 'agent',
    lifecycle_status: 'active', active_revision_id: null, content_hash: 'hash', source: { type: 'native', ref: id },
    trust_state: 'trusted', dependencies: [], compatibility: {}, admission_state: 'admitted', quarantine_reason: null,
    usage: { count: 0, last_used_at: null, evidence: [] }, projection: { status: 'applied', error: null },
    created_at: null, updated_at: null,
  };
}

it('hides old actions during failed selection and refreshes only the selected detail', async () => {
  const rows = [asset('first'), asset('second')];
  const detail = (row: AIAssetRecord): AIAssetDetail => ({ asset: row, active_revision: null, history: [], usage_events: [] });
  const api = vi.mocked(aiAssetsApi);
  api.list.mockResolvedValue(rows);
  let rejectSecond!: (error: Error) => void;
  api.detail.mockResolvedValueOnce(detail(rows[0])).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectSecond = reject; }));
  render(<WorkspaceAIAssetsSection selectedTenantId="tenant" />);
  await screen.findByRole('heading', { name: 'first' });
  fireEvent.click(screen.getByText('second', { exact: true }));
  expect(screen.queryByRole('button', { name: 'Reconcile' })).toBeNull();
  await act(async () => rejectSecond(new Error('Detail unavailable')));
  expect(screen.queryByRole('heading', { name: 'first' })).toBeNull();
  expect(api.reconcile).not.toHaveBeenCalled();
  api.detail.mockResolvedValue(detail(rows[1]));
  api.reconcile.mockResolvedValue({ status: 'applied' });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByRole('heading', { name: 'second' });
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }));
  await waitFor(() => expect(api.reconcile).toHaveBeenCalledExactlyOnceWith('second'));
});
