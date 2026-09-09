// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { adminApi, type RuntimeResultPage } from '../../api/domains/admin';
import '../../i18n';
import AdminResultPagesSection from './AdminResultPagesSection';

vi.mock('../../api/domains/admin', () => ({ adminApi: {
  listRuntimeResultPages: vi.fn(), redriveRuntimeResultPage: vi.fn(),
} }));
const api = vi.mocked(adminApi);
const row: RuntimeResultPage = {
  id: 'page-1', parent_session_id: 'session-1', parent_agent_id: 'agent-1', integration_epoch: 2,
  delivery_mode: 'parent_continuation', item_count: 2, bound_item_count: 2, manifest_sha256: 'a'.repeat(64), status: 'dead_letter',
  attempt_count: 8, last_error: 'RuntimeBudgetDenied', delivered_at: null, created_at: '', updated_at: '',
};
beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

async function load() {
  render(<AdminResultPagesSection initialTenantId="tenant-1" />);
  fireEvent.change(screen.getByLabelText('Parent session ID'), { target: { value: 'session-1' } });
  fireEvent.click(screen.getByRole('button', { name: 'Load result pages' }));
  await screen.findByText('RuntimeBudgetDenied');
}

it('requires reason and exact confirmation, then preserves requeue receipt if refresh fails', async () => {
  api.listRuntimeResultPages.mockResolvedValueOnce([row]).mockRejectedValueOnce(new Error('Refresh unavailable'));
  api.redriveRuntimeResultPage.mockResolvedValue({ ...row, status: 'prepared' });
  await load();
  expect(api.listRuntimeResultPages).toHaveBeenCalledWith({ tenantId: 'tenant-1', parentSessionId: 'session-1' });
  const button = screen.getByRole('button', { name: 'Redrive result page' }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('Recovery reason'), { target: { value: 'Delivery repair verified' } });
  fireEvent.click(button);
  expect(api.redriveRuntimeResultPage).not.toHaveBeenCalled();
  expect((screen.getByLabelText('Tenant ID') as HTMLInputElement).disabled).toBe(true);
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Redrive result page' }));
  await waitFor(() => expect(api.redriveRuntimeResultPage).toHaveBeenCalledTimes(1));
  expect(api.redriveRuntimeResultPage).toHaveBeenCalledWith('page-1', { tenantId: 'tenant-1', reason: 'Delivery repair verified' });
  await screen.findByText('Refresh unavailable');
  expect(screen.getByRole('status').textContent).toContain('not delivery or business completion');
  expect(screen.queryByRole('button', { name: 'Redrive result page' })).toBeNull();
});

it('drops old-company rows and audit reason when the tenant changes', async () => {
  api.listRuntimeResultPages.mockResolvedValue([row]);
  await load();
  fireEvent.change(screen.getByLabelText('Tenant ID'), { target: { value: 'tenant-2' } });
  expect(screen.queryByText('RuntimeBudgetDenied')).toBeNull();
  expect(screen.queryByLabelText('Recovery reason')).toBeNull();
  expect(api.redriveRuntimeResultPage).not.toHaveBeenCalled();
});
