// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import WorkspaceOrgSection from './WorkspaceOrgSection';

const api = vi.hoisted(() => ({
  getSetting: vi.fn(), getDepartments: vi.fn(), getOrgMembers: vi.fn(),
  listIdentityLinks: vi.fn(), listDepartmentLinks: vi.fn(), syncOrg: vi.fn(),
  repairDepartmentAssignment: vi.fn(),
}));
vi.mock('../../api/domains/enterprise', () => ({ enterpriseApi: api }));
vi.mock('../../api/domains/tools', () => ({ toolsApi: { getFeishuRuntimeStatus: async () => null } }));
const language = createInstance();
const words = en.enterprise.org;
beforeAll(async () => { await language.init({ lng: 'en', resources: { en: { translation: en } } }); });
afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  api.getSetting.mockResolvedValue({ value: { app_id: 'fixture' } });
  api.getDepartments.mockResolvedValue([]);
  api.getOrgMembers.mockResolvedValue([]);
  api.listIdentityLinks.mockResolvedValue({ members: [], accounts: [], providers: [] });
  api.listDepartmentLinks.mockImplementation(async (tenant: string) => ({
    tenant_id: tenant, departments: [{ id: `target-${tenant}`, name: `Target ${tenant}` }], org_departments: [], links: [],
  }));
  api.syncOrg.mockImplementation(async (tenant: string) => ({
    status: 'complete', applied: 1, skipped: 0, conflicts: 0,
    assignment_counts: { changed: 0, noop: 0, skipped: 0, conflict: 1, unavailable: 0 },
    assignment_rows: [{ object_kind: 'user', object_id: `user-${tenant}`, user_id: `user-${tenant}`,
      person_label: `Person ${tenant}`, outcome: 'conflict', reason: 'department_id_points_elsewhere',
      department_name: `Current ${tenant}`, mapped_department_name: `Target ${tenant}` }],
  }));
});
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (tenant: string) => <QueryClientProvider client={client}><I18nextProvider i18n={language}>
    <WorkspaceOrgSection selectedTenantId={tenant} />
  </I18nextProvider></QueryClientProvider>;
  const view = render(tree('a'));
  return { ...view, tenant: (value: string) => view.rerender(tree(value)) };
}
async function requestCorrection(tenant = 'a') {
  await screen.findByDisplayValue('fixture');
  fireEvent.click(screen.getByRole('button', { name: 'Sync Now' }));
  await screen.findByRole('option', { name: `Target ${tenant}` });
  fireEvent.change(screen.getByLabelText(words.cutoverChooseDepartment), { target: { value: `target-${tenant}` } });
  fireEvent.click(screen.getByRole('button', { name: words.cutoverCorrect }));
  await waitFor(() => expect(api.repairDepartmentAssignment).toHaveBeenCalled());
}

it('keeps a refused HTTP-200 correction available and shows its outcome', async () => {
  api.repairDepartmentAssignment.mockResolvedValue({
    schema: 'hive.department_assignment_repair.v1', tenant_id: 'a', status: 'complete', applied: false,
    counts: { user_assignments: { changed: 0, noop: 0, skipped: 0, conflict: 0, unavailable: 1 } },
    rows: [{ object_kind: 'user', object_id: 'user-a', outcome: 'unavailable', reason: 'department_not_in_tenant' }],
  });
  mount();
  await requestCorrection();
  await act(async () => {});
  expect(screen.queryByRole('button', { name: words.cutoverCorrect })).not.toBeNull();
});

it('discards a late correction failure after the selected tenant changes', async () => {
  let reject!: (reason: Error) => void;
  api.repairDepartmentAssignment.mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
  const view = mount();
  await requestCorrection();
  view.tenant('b');
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: 'Sync Now' }));
  await screen.findByText('Person b');
  await act(async () => { reject(new Error('Only tenant A correction failed')); });
  expect(screen.queryByText('Only tenant A correction failed')).toBeNull();
});
