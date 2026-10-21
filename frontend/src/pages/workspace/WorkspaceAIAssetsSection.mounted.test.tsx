// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { aiAssetsApi, type AIAssetDetail, type AIAssetRecord } from '../../api/domains/aiAssets';
import '../../i18n';
import WorkspaceAIAssetsSection from './WorkspaceAIAssetsSection';
import { requestAppConfirm } from '../../components/AppDialogs';

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

it.each([false, true])('retains the exact rollback command on response loss (recovered receipt: %s)', async (recovered) => {
  const row: AIAssetRecord = { ...asset('learned'), active_revision_id: 'current-revision' };
  if (recovered) row.projection = { status: 'pending', error: 'Resume the pending rollback.', owner_operation: {
    operation_id: 'recovered-operation', expected_revision_id: 'original-revision', target_version: 1,
  } };
  const detail: AIAssetDetail = { asset: row, active_revision: null, usage_events: [], history: [{
    version: 1, id: 'old-revision', content_hash: 'old-hash', diff_from_prev: null, change_source: 'native_lifecycle',
    changed_by_user_id: null, changed_by_agent_id: null, change_message: '', is_active: false,
    parent_revision_id: null, rollback_of_revision_id: null, created_at: null,
  }] };
  const api = vi.mocked(aiAssetsApi);
  api.list.mockResolvedValue([row]);
  api.detail.mockResolvedValue(detail);
  vi.mocked(requestAppConfirm).mockResolvedValue(true);
  api.rollback.mockRejectedValueOnce(new Error('Commit response lost')).mockResolvedValueOnce({
    asset_id: row.id, revision_id: 'restored-revision', version: 3,
  });
  render(<WorkspaceAIAssetsSection selectedTenantId="tenant" />);
  fireEvent.click(await screen.findByRole('button', { name: /Rollback to v1/i }));
  await waitFor(() => expect(api.detail).toHaveBeenCalledTimes(2));
  fireEvent.click(await screen.findByRole('button', { name: /Rollback to v1/i }));
  await waitFor(() => expect(api.rollback).toHaveBeenCalledTimes(2));
  const command = api.rollback.mock.calls[0][2];
  expect(command).toEqual({ operationId: recovered ? 'recovered-operation' : expect.any(String),
    expectedRevisionId: recovered ? 'original-revision' : 'current-revision' });
  expect(api.rollback.mock.calls[1]).toEqual(api.rollback.mock.calls[0]);
});
