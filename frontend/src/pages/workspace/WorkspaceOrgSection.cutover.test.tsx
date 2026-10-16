// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createInstance, type i18n as I18nInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

const api = vi.hoisted(() => ({
  getSetting: vi.fn(),
  getDepartments: vi.fn(),
  getOrgMembers: vi.fn(),
  updateSetting: vi.fn(),
  syncOrg: vi.fn(),
  dryRunIdentityLinks: vi.fn(),
  listIdentityLinks: vi.fn(),
  createIdentityLink: vi.fn(),
  revokeIdentityLink: vi.fn(),
  replaceIdentityLink: vi.fn(),
  getFeishuRuntimeStatus: vi.fn(),
  previewIdentityBackfill: vi.fn(),
  applyIdentityBackfill: vi.fn(),
  listDepartmentLinks: vi.fn(),
  applyDepartmentLinks: vi.fn(),
  applyDepartmentCutover: vi.fn(),
  repairDepartmentAssignment: vi.fn(),
  listCutoverJournal: vi.fn(),
  rollbackCutover: vi.fn(),
}));

vi.mock('../../api/domains/enterprise', () => ({ enterpriseApi: api }));
vi.mock('../../api/domains/tools', () => ({ toolsApi: { getFeishuRuntimeStatus: api.getFeishuRuntimeStatus } }));

import WorkspaceOrgSection from './WorkspaceOrgSection';

const words = en.enterprise.org;
let i18n: I18nInstance;

const preview = {
  schema: 'hive.identity_backfill_preview.v1',
  tenant_id: 'tenant-1',
  applied: false,
  counts: {},
  rows: [
    {
      org_member_id: 'member-trusted',
      disposition: 'trusted',
      reason: 'provider_open_id',
      user_id: 'user-1',
      subject_kinds: ['provider_open_id'],
      selectable: true,
    },
    {
      org_member_id: 'member-email',
      disposition: 'email_only_candidate',
      reason: 'email_only_candidate',
      user_id: 'user-email',
      subject_kinds: [],
      selectable: false,
    },
  ],
};

const directory = {
  schema: 'hive.department_directory.v1',
  tenant_id: 'tenant-1',
  applied: false,
  org_departments: [
    {
      org_department_id: 'org-1',
      name: 'Research',
      provider_department_id: 'od-research',
      link: null,
      proposal: { action: 'create', selectable: true, reason: 'create' },
      same_name_department_ids: [],
    },
    {
      org_department_id: 'org-mapped',
      name: 'Mapped directory',
      provider_department_id: 'od-mapped',
      link: { id: 'link-finance', department_id: 'dept-finance', status: 'active' },
      proposal: null,
      same_name_department_ids: [],
    },
  ],
  departments: [
    { id: 'dept-finance', name: 'Finance', parent_id: null },
    { id: 'dept-research', name: 'Research mapped', parent_id: null },
  ],
  links: [],
};

const journal = {
  schema: 'hive.identity_cutover_journal.v1',
  tenant_id: 'tenant-1',
  applied: false,
  rows: [
    {
      id: 'journal-1',
      object_kind: 'user_department',
      object_id: 'user-1',
      before: { department_id: null },
      after: { department_id: 'dept-research' },
      applied_at: '2026-10-16T00:00:00+00:00',
      actor_id: 'actor-1',
      actor_label: 'Casey',
    },
    {
      id: 'journal-dept',
      object_kind: 'department_directory_link',
      object_id: 'link-dept',
      before: null,
      after: { status: 'active', department_id: 'dept-research', org_department_id: 'org-1' },
      applied_at: '2026-10-16T00:05:00+00:00',
      actor_label: 'Casey',
    },
  ],
};

function mount(tenantId = 'tenant-1') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <WorkspaceOrgSection selectedTenantId={tenantId} />
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

beforeAll(async () => {
  i18n = createInstance();
  await i18n.init({
    lng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
});

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  vi.clearAllMocks();
  api.getSetting.mockResolvedValue({ value: { app_id: 'cli_test' } });
  api.getDepartments.mockResolvedValue([]);
  api.getOrgMembers.mockResolvedValue([]);
  api.listIdentityLinks.mockResolvedValue({
    members: [
      { org_member_id: 'member-trusted', label: 'Alice', subject_kinds: ['provider_open_id'], reason: 'active', valid: true, links: [] },
      { org_member_id: 'member-email', label: 'Alice', subject_kinds: [], reason: 'email', valid: false, links: [] },
    ],
    accounts: [{ user_id: 'user-1', label: 'Alice Account', is_active: true }],
    providers: [{ id: 'provider-1', provider_type: 'feishu', name: 'Fixture', is_active: true }],
  });
  api.getFeishuRuntimeStatus.mockResolvedValue(null);
  api.previewIdentityBackfill.mockResolvedValue(preview);
  api.listDepartmentLinks.mockResolvedValue(directory);
  api.listCutoverJournal.mockResolvedValue(journal);
});

describe('organization cutover card', () => {
  it('previews stored people and keeps a department proposal unapplied', async () => {
    mount();
    expect(api.previewIdentityBackfill).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: words.cutoverReview }));
    expect(await screen.findByText(words.disposition.trusted)).toBeTruthy();
    expect(screen.getByText('Alice (membertr)')).toBeTruthy();
    expect(screen.getByText('Alice (memberem)')).toBeTruthy();
    expect(screen.getByText(words.reason.email_only_candidate)).toBeTruthy();
    expect(screen.getByText(words.cutoverProposal, { exact: false })).toBeTruthy();
    expect(screen.getByText('Current department: Finance')).toBeTruthy();
    const mappedSelect = document.getElementById('cutover-dept-org-mapped') as HTMLSelectElement;
    expect(mappedSelect.value).toBe('');
    expect(mappedSelect.labels?.[0]?.textContent).toBe(words.cutoverChooseDepartment);
    const boxes = screen.getAllByRole('checkbox');
    expect((boxes[0] as HTMLInputElement).disabled).toBe(false);
    expect((boxes[1] as HTMLInputElement).disabled).toBe(true);
  });

  it('shows separate link, department, and assignment counts', async () => {
    api.applyIdentityBackfill.mockResolvedValue({
      schema: 'hive.identity_backfill.v1',
      tenant_id: 'tenant-1',
      status: 'complete',
      applied: true,
      counts: {
        link_backfill: { applied: 2, noop: 0, skipped: 1, conflict: 0, unavailable: 0 },
        department_mappings: { created: 0, mapped: 0, revoked: 0, noop: 0, skipped: 0, conflict: 0, unavailable: 0 },
        user_assignments: { changed: 0, noop: 0, skipped: 0, conflict: 0, unavailable: 0 },
      },
      rows: [{ object_id: 'member-trusted', outcome: 'applied', reason: 'active' }],
    });
    mount();
    fireEvent.click(screen.getByRole('button', { name: words.cutoverReview }));
    await screen.findByText(words.disposition.trusted);
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getByRole('button', { name: words.cutoverApplyLinks }));
    expect(await screen.findByText('Identity links applied: 2. Unchanged: 0. Skipped: 1. Conflicts: 0. Unavailable: 0.')).toBeTruthy();
    expect(screen.getByText('Departments created: 0. Mapped: 0. Revoked: 0. Unchanged: 0. Skipped: 0. Conflicts: 0. Unavailable: 0.')).toBeTruthy();
    expect(screen.getByText('User assignments changed: 0. Unchanged: 0. Skipped: 0. Conflicts: 0. Unavailable: 0.')).toBeTruthy();
    expect(api.applyIdentityBackfill).toHaveBeenCalledWith(['member-trusted'], 'tenant-1');
  });

  it('shows a department conflict and keeps rollback refusal available for retry', async () => {
    api.applyDepartmentCutover.mockResolvedValue({
      schema: 'hive.department_cutover.v1',
      tenant_id: 'tenant-1',
      status: 'complete',
      applied: false,
      counts: {
        link_backfill: { applied: 0, noop: 0, skipped: 0, conflict: 0, unavailable: 0 },
        department_mappings: { created: 0, mapped: 0, revoked: 0, noop: 0, skipped: 0, conflict: 0, unavailable: 0 },
        user_assignments: { changed: 0, noop: 0, skipped: 0, conflict: 1, unavailable: 0 },
      },
      rows: [{ object_id: 'user-1', outcome: 'conflict', reason: 'department_id_points_elsewhere' }],
    });
    api.rollbackCutover.mockResolvedValue({
      schema: 'hive.identity_cutover_rollback.v1',
      tenant_id: 'tenant-1',
      status: 'complete',
      applied: false,
      counts: {
        link_backfill: { applied: 0, noop: 0, skipped: 0, conflict: 0, unavailable: 0 },
        department_mappings: { created: 0, mapped: 0, revoked: 0, noop: 0, skipped: 0, conflict: 1, unavailable: 0 },
        user_assignments: { changed: 0, noop: 0, skipped: 0, conflict: 1, unavailable: 0 },
      },
      rows: [
        {
          object_id: 'link-old',
          object_kind: 'org_member_user_link',
          outcome: 'conflict',
          reason: 'rollback_conflict',
          observed: { org_member_id: 'member-bob', user_id: 'user-bob', status: 'revoked' },
        },
        { object_id: 'link-1', outcome: 'conflict', reason: 'dependent_link_retained' },
      ],
    });
    api.listIdentityLinks.mockResolvedValue({
      members: [
        { org_member_id: 'member-trusted', label: 'Alice', subject_kinds: ['provider_open_id'], reason: 'active', valid: true, links: [] },
        { org_member_id: 'member-email', label: 'Alice', subject_kinds: [], reason: 'email', valid: false, links: [] },
        { org_member_id: 'member-bob', label: 'Bob Directory', subject_kinds: ['provider_open_id'], reason: 'active', valid: true, links: [] },
      ],
      accounts: [
        { user_id: 'user-1', label: 'Alice Account', is_active: true },
        { user_id: 'user-bob', label: 'Bob Account', is_active: true },
      ],
      providers: [{ id: 'provider-1', provider_type: 'feishu', name: 'Fixture', is_active: true }],
    });
    mount();
    fireEvent.click(screen.getByRole('button', { name: words.cutoverReview }));
    await screen.findByText(words.cutoverHistory);
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    fireEvent.click(screen.getByRole('button', { name: words.cutoverApplyAssignments }));
    expect(await screen.findByText(words.reason.department_id_points_elsewhere)).toBeTruthy();
    expect(screen.getByRole('button', { name: words.cutoverCorrect })).toBeTruthy();
    expect(screen.getByText('No department → Research mapped')).toBeTruthy();
    expect(screen.getByText('Applied by Casey at 2026-10-16 00:00:00 UTC · journal1')).toBeTruthy();
    expect(screen.getByRole('checkbox', { name: /Department mapping/ })).toBeTruthy();
    expect(screen.queryByText('Directory person ()')).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: /User department assignment/ }));
    fireEvent.click(screen.getByRole('button', { name: words.cutoverRollback }));
    expect(await screen.findByText(words.cutoverRollbackRefused)).toBeTruthy();
    expect(screen.getByText('Bob Directory')).toBeTruthy();
    expect(screen.getByText(words.cutoverDependentKept)).toBeTruthy();
    api.previewIdentityBackfill.mockResolvedValue({ ...preview, rows: [] });
    fireEvent.click(screen.getByRole('button', { name: words.cutoverRetry }));
    expect(await screen.findByText(words.cutoverEmptyPeople)).toBeTruthy();
  });

  it('drops a late preview from another tenant', async () => {
    let resolveFirst: (value: unknown) => void = () => {};
    api.previewIdentityBackfill.mockReturnValue(new Promise((resolve) => {
      resolveFirst = resolve;
    }));
    api.listDepartmentLinks.mockResolvedValue(directory);
    api.listCutoverJournal.mockResolvedValue(journal);
    const view = mount('tenant-1');
    fireEvent.click(screen.getByRole('button', { name: words.cutoverReview }));
    view.rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <I18nextProvider i18n={i18n}>
          <WorkspaceOrgSection selectedTenantId="tenant-2" />
        </I18nextProvider>
      </QueryClientProvider>,
    );
    resolveFirst({
      ...preview,
      tenant_id: 'tenant-1',
      rows: [{ ...preview.rows[0], reason: 'provider_open_id' }],
    });
    api.previewIdentityBackfill.mockResolvedValue({ ...preview, tenant_id: 'tenant-2', rows: [] });
    api.listDepartmentLinks.mockResolvedValue({ ...directory, tenant_id: 'tenant-2', org_departments: [] });
    api.listCutoverJournal.mockResolvedValue({ ...journal, tenant_id: 'tenant-2', rows: [] });
    fireEvent.click(screen.getByRole('button', { name: words.cutoverReview }));
    expect(await screen.findByText(words.cutoverEmptyPeople)).toBeTruthy();
    expect(screen.queryByText(words.disposition.trusted)).toBeNull();
    cleanup();
  });

  it('shows a sync department conflict with the person and a scoped repair', async () => {
    api.syncOrg.mockResolvedValue({
      status: 'complete',
      departments: 0,
      members: 1,
      applied: 1,
      skipped: 0,
      conflicts: 0,
      user_assignments: 0,
      assignment_counts: { changed: 0, noop: 0, skipped: 0, conflict: 1, unavailable: 0 },
      assignment_rows: [{
        object_kind: 'user',
        object_id: 'user-1',
        user_id: 'user-1',
        org_member_id: 'member-trusted',
        person_label: 'Bob',
        outcome: 'conflict',
        reason: 'department_id_points_elsewhere',
        department_name: 'Finance',
        mapped_department_name: 'Research mapped',
      }],
      rows: [{ object_id: 'member-trusted', reason: 'provider_open_id', disposition: 'trusted', applied: true }],
    });
    api.repairDepartmentAssignment.mockResolvedValue({
      schema: 'hive.department_assignment_repair.v1',
      tenant_id: 'tenant-1',
      status: 'complete',
      applied: true,
      counts: {
        link_backfill: { applied: 0, noop: 0, skipped: 0, conflict: 0, unavailable: 0 },
        department_mappings: { created: 0, mapped: 0, revoked: 0, noop: 0, skipped: 0, conflict: 0, unavailable: 0 },
        user_assignments: { changed: 1, noop: 0, skipped: 0, conflict: 0, unavailable: 0 },
      },
      rows: [{ object_id: 'user-1', outcome: 'changed', reason: 'department_repaired' }],
    });
    mount();
    await screen.findByDisplayValue('cli_test');
    fireEvent.click(screen.getByRole('button', { name: 'Sync Now' }));
    expect(await screen.findByText('Sync complete: 0 departments, 1 members imported')).toBeTruthy();
    expect(screen.getByText('Bob')).toBeTruthy();
    expect(screen.getByText(words.reason.department_id_points_elsewhere)).toBeTruthy();
    expect(screen.getByText('Current department: Finance')).toBeTruthy();
    expect(screen.getByText('Target department: Research mapped')).toBeTruthy();
    expect(screen.getByText('User assignments changed: 0. Unchanged: 0. Skipped: 0. Conflicts: 1. Unavailable: 0.')).toBeTruthy();
    expect(screen.queryByRole('link', { name: words.syncRepair })).toBeNull();
    const repair = screen.getByRole('link', { name: words.syncAssignmentRepair });
    expect(repair.getAttribute('href')).toBe('#org-department-cutover');
    expect(document.getElementById('org-department-cutover')).toBeTruthy();
    expect(await screen.findByRole('option', { name: 'Finance' })).toBeTruthy();
    fireEvent.change(document.getElementById('sync-repair-user-1') as HTMLSelectElement, { target: { value: 'dept-finance' } });
    const correct = screen.getByRole('button', { name: words.cutoverCorrect }) as HTMLButtonElement;
    expect(correct.disabled).toBe(false);
    fireEvent.click(correct);
    expect(api.repairDepartmentAssignment).toHaveBeenCalledWith('user-1', 'dept-finance', 'tenant-1');
    expect(await screen.findByText('Target department: Finance')).toBeTruthy();
  });

  it('distinguishes two journal rows that share a minute', async () => {
    api.listCutoverJournal.mockResolvedValue({
      ...journal,
      rows: [
        { ...journal.rows[0], id: 'journal-aaaa' },
        {
          ...journal.rows[0],
          id: 'journal-bbbb',
          actor_id: null,
          actor_label: null,
        },
      ],
    });
    mount();
    fireEvent.click(screen.getByRole('button', { name: words.cutoverReview }));
    expect(await screen.findByText('Applied by Casey at 2026-10-16 00:00:00 UTC · journala')).toBeTruthy();
    expect(screen.getByText('Unknown operator at 2026-10-16 00:00:00 UTC · journalb')).toBeTruthy();
  });
});
