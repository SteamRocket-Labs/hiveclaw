// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Agent } from '../../types';
import { ApiError } from '../../api/core/errors';
import { WorkspaceBusinessTeams, entryAllowed, privateWorkSessionHref, staleTenantResponse } from './WorkspaceBusinessTeams';

const list = vi.fn();
const read = vi.fn();
const create = vi.fn();
const share = vi.fn();
const createSession = vi.fn();
const updateMember = vi.fn();
const listApprovals = vi.fn();
const resolveApproval = vi.fn();
let signedIn: {
  id: string;
  role: string;
  tenant_id: string | null;
  department_id: string | null;
  display_name: string;
} = { id: 'user-1', role: 'member', tenant_id: 'tenant-1', department_id: 'dept-1', display_name: 'Avery' };

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string, values?: Record<string, unknown>) => {
      const template = fallback ?? key;
      return Object.entries(values || {}).reduce(
        (text, [name, value]) => text.replaceAll(`{{${name}}}`, String(value)),
        template,
      );
    },
  }),
}));

vi.mock('../../stores', () => ({
  useAuthStore: Object.assign(
    (selector?: (state: { user: typeof signedIn }) => unknown) => {
      const state = { user: signedIn };
      return selector ? selector(state) : state;
    },
    { getState: () => ({ user: signedIn }) },
  ),
}));

vi.mock('../../api/domains/businessTeams', () => ({
  businessTeamsApi: {
    list: (...args: unknown[]) => list(...args),
    read: (...args: unknown[]) => read(...args),
    create: (...args: unknown[]) => create(...args),
    install: vi.fn(),
    update: vi.fn(),
    memberCandidates: vi.fn(async () => ({ tenant_id: 'tenant-1', people: [{ id: 'user-2', display_name: 'Blair', email: 'blair@example.test', department_name: 'Research' }] })),
    leadCandidates: vi.fn(async () => ({ tenant_id: 'tenant-1', people: [] })),
    addMember: vi.fn(),
    updateMember: (...args: unknown[]) => updateMember(...args),
    shareAgent: (...args: unknown[]) => share(...args),
    unshareAgent: vi.fn(),
  },
}));

vi.mock('../../api/domains/chat', () => ({
  chatApi: { createSession: (...args: unknown[]) => createSession(...args) },
}));
vi.mock('../../api/domains/enterprise', () => ({ enterpriseApi: {
  listApprovals: (...args: unknown[]) => listApprovals(...args),
  resolveApproval: (...args: unknown[]) => resolveApproval(...args),
} }));

const analyst: Agent = {
  id: 'agent-1',
  name: 'Analyst',
  role_description: 'Research',
  status: 'idle',
  creator_id: 'user-1',
  owner_user_id: 'user-1',
  is_owner: true,
  access_level: 'manage',
  action_capabilities: {
    can_use: true,
    can_manage: true,
    can_manage_schedule: true,
    can_manage_channel: true,
    can_manage_permissions: true,
    can_transfer_ownership: true,
  },
  tokens_used_today: 0,
  tokens_used_month: 0,
  heartbeat_enabled: false,
  heartbeat_interval_minutes: 30,
  heartbeat_active_hours: '09:00-18:00',
  created_at: '2026-10-16T00:00:00Z',
};

function team(overrides: Record<string, unknown> = {}) {
  return {
    id: 'team-1',
    tenant_id: 'tenant-1',
    name: 'Delivery',
    status: 'active',
    department_name: 'Research',
    created_by_display_name: 'Avery',
    my_role: 'lead',
    can_manage: true,
    member_count: 1,
    active_registered_count: 1,
    private_sessions: true,
    members: [{
      id: 'membership-1', role: 'lead', status: 'active', registered: true, authority: true,
      display_name: 'Avery', email: 'avery@example.test', user_id: 'user-1', org_member_id: null,
      joined_at: '2026-10-16T00:00:00Z', left_at: null,
    }],
    affiliations: [],
    ...overrides,
  };
}

function renderTeams() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceBusinessTeams agents={[analyst]} />
    </QueryClientProvider>,
  );
}

describe('WorkspaceBusinessTeams', () => {
  afterEach(() => {
    cleanup();
    signedIn = { id: 'user-1', role: 'member', tenant_id: 'tenant-1', department_id: 'dept-1', display_name: 'Avery' };
    localStorage.clear();
    vi.clearAllMocks();
  });

  it('opens the selected team approval queue without entering an owner private conversation', async () => {
    list.mockResolvedValue({ tenant_id: 'tenant-1', caller: { can_create: true }, teams: [team()] });
    read.mockResolvedValue(team());
    listApprovals.mockResolvedValue([{ id: 'approval-1', agent_id: 'agent-1', agent_name: 'Analyst',
      action_type: 'workspace.write', tool_name: 'write_file', details: { path: 'report.md' },
      status: 'pending', created_at: '2026-10-18T00:00:00Z' }]);
    resolveApproval.mockResolvedValue({ status: 'approved', execution_status: 'queued' });
    renderTeams();
    fireEvent.click(await screen.findByRole('button', { name: /Delivery/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Approvals' }));
    expect(await screen.findByText('report.md')).toBeTruthy();
    expect(listApprovals).toHaveBeenCalledWith('tenant-1', 'team-1');
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(resolveApproval).toHaveBeenCalledWith('approval-1', { action: 'approve' }));
    expect(createSession).not.toHaveBeenCalled();
  });

  it('keeps session links private and refuses read-only entry', () => {
    expect(privateWorkSessionHref('agent-1', 'session-1')).toBe('/agents/agent-1?session_id=session-1#chat');
    expect(entryAllowed(analyst, 'blocked')).toBe(true);
    expect(entryAllowed(analyst, 'blocked', false)).toBe(false);
    expect(entryAllowed(analyst, 'blocked', true)).toBe(true);
    expect(entryAllowed(analyst, 'allowed')).toBe(true);
    expect(entryAllowed({ ...analyst, action_capabilities: { ...analyst.action_capabilities!, can_use: false } }, 'allowed')).toBe(false);
    expect(entryAllowed(undefined, 'allowed')).toBe(true);
    expect(entryAllowed(undefined, 'blocked')).toBe(false);
    expect(staleTenantResponse('tenant-1', 'tenant-2', 'tenant-1', 'member')).toBe(true);
    expect(staleTenantResponse('tenant-1', 'tenant-1', 'tenant-2', 'member')).toBe(true);
    expect(staleTenantResponse(null, null, 'tenant-2', 'platform_admin')).toBe(false);
  });

  it('creates a team, shares a named employee, and opens the caller private conversation', async () => {
    list.mockResolvedValue({
      tenant_id: 'tenant-1',
      caller: { id: 'user-1', display_name: 'Avery', role: 'member', tenant_id: 'tenant-1', department_id: 'dept-1', department_name: 'Research', can_create: true, can_install: false },
      teams: [],
    });
    create.mockResolvedValue(team());
    share.mockResolvedValue(team({
      affiliations: [{
        agent_id: 'agent-1', agent_name: 'Analyst', owner_display_name: 'Avery', actions: ['read', 'execute'],
        effect: 'allow', use_access: 'read_and_execute', execution: 'allowed', manage_granted: false,
        home_team: true, owner_unchanged: true,
      }],
    }));
    createSession.mockResolvedValue({ id: 'session-9' });
    renderTeams();

    fireEvent.change(await screen.findByLabelText('Team name'), { target: { value: 'Delivery' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Align this team with my department' }));
    fireEvent.click(screen.getByRole('button', { name: 'Create team' }));
    expect(await screen.findByText((_, element) => element?.tagName === 'STRONG' && element.textContent === 'Avery')).toBeTruthy();
    expect(screen.getByText(/Can act for this team/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Install team' })).toBeNull();

    fireEvent.change(screen.getByLabelText('Employee'), { target: { value: 'agent-1' } });
    expect(screen.getByText('Delivery members get use access to Analyst. Ownership does not move, and existing conversations stay private.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Share with this team' }));
    expect(await screen.findByText(/Use access: members can read and start their own work/, { selector: 'small' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open my conversation' }));
    const link = await screen.findByRole('link', { name: 'Continue in my private conversation' });
    expect(link.getAttribute('href')).toBe('/agents/agent-1?session_id=session-9#chat');
    expect(createSession).toHaveBeenCalledWith('agent-1');
    expect(screen.getByText('This is your private conversation. Opening it from the team does not share it with other members.')).toBeTruthy();
  });

  it('shows a readable denial and keeps a blocked employee from opening work', async () => {
    list.mockResolvedValue({
      tenant_id: 'tenant-1',
      caller: { id: 'user-2', display_name: 'Blair', role: 'member', tenant_id: 'tenant-1', department_id: null, department_name: null, can_create: true, can_install: false },
      teams: [team({ my_role: 'member', can_manage: false })],
    });
    read.mockResolvedValue(team({
      my_role: 'member',
      can_manage: false,
      affiliations: [{
        agent_id: 'agent-1', agent_name: 'Analyst', owner_display_name: 'Avery', actions: ['read'],
        effect: 'deny', use_access: 'limited', execution: 'blocked', caller_can_use: false, manage_granted: false,
        home_team: true, owner_unchanged: true,
      }],
    }));
    signedIn = { ...signedIn, id: 'user-2' };
    renderTeams();
    fireEvent.click(await screen.findByRole('button', { name: /Delivery/ }));
    expect(await screen.findByText('You can see this employee, but use is denied.')).toBeTruthy();
    expect(screen.getByText(/Limited access: use is not currently allowed/, { selector: 'small' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Open my conversation' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add to team' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Share with this team' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove from this team' })).toBeNull();
    expect(screen.getAllByRole('button', { name: 'Refresh' }).length).toBeGreaterThan(0);
  });

  it('discards a late response after the company changes and shows the server message', async () => {
    list.mockResolvedValue({
      tenant_id: 'tenant-1',
      caller: { id: 'user-1', display_name: 'Avery', role: 'member', tenant_id: 'tenant-1', department_id: 'dept-1', department_name: 'Research', can_create: true, can_install: false },
      teams: [],
    });
    create.mockImplementation(async () => {
      signedIn = { ...signedIn, tenant_id: 'tenant-2' };
      return team({ tenant_id: 'tenant-1' });
    });
    renderTeams();
    fireEvent.change(await screen.findByLabelText('Team name'), { target: { value: 'Delivery' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create team' }));
    expect(await screen.findByText('The company changed before this result arrived. Refresh to load the current company.')).toBeTruthy();
    expect(screen.queryByText('Can act for this team')).toBeNull();

    cleanup();
    signedIn = { id: 'user-1', role: 'member', tenant_id: 'tenant-1', department_id: 'dept-1', display_name: 'Avery' };
    create.mockRejectedValue(new ApiError(409, 'That person is inactive and cannot receive team authority.', { code: 'inactive_user', message: 'That person is inactive and cannot receive team authority.' }));
    renderTeams();
    fireEvent.change(await screen.findByLabelText('Team name'), { target: { value: 'Delivery' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create team' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('That person is inactive and cannot receive team authority.');
    expect(screen.getByRole('button', { name: 'Refresh' }).hasAttribute('disabled')).toBe(false);
  });

  it('keeps a company grant usable when the team execution grant is blocked', async () => {
    list.mockResolvedValue({
      tenant_id: 'tenant-1',
      caller: { id: 'user-2', display_name: 'Blair', role: 'member', tenant_id: 'tenant-1', department_id: null, department_name: null, can_create: true, can_install: false },
      teams: [team({ my_role: 'member', can_manage: false })],
    });
    read.mockResolvedValue(team({
      my_role: 'member',
      can_manage: false,
      affiliations: [{
        agent_id: 'agent-1', agent_name: 'Analyst', owner_display_name: 'Avery', actions: ['read'],
        effect: 'allow', use_access: 'limited', execution: 'blocked', caller_can_use: true, manage_granted: false,
        home_team: true, owner_unchanged: true,
      }],
    }));
    signedIn = { ...signedIn, id: 'user-2' };
    renderTeams();
    fireEvent.click(await screen.findByRole('button', { name: /Delivery/ }));
    expect(await screen.findByRole('button', { name: 'Open my conversation' })).toBeTruthy();
    expect(screen.getByText(/Use access: members can read and start their own work/, { selector: 'small' })).toBeTruthy();
    expect(screen.queryByText('You can see this employee, but use is denied.')).toBeNull();
  });

  it('clears the team after a saved change removes the caller and ignores an old company detail', async () => {
    list.mockResolvedValue({
      tenant_id: 'tenant-1',
      caller: { id: 'user-1', display_name: 'Avery', role: 'member', tenant_id: 'tenant-1', department_id: 'dept-1', department_name: 'Research', can_create: true, can_install: false },
      teams: [team()],
    });
    read.mockResolvedValue(team());
    updateMember.mockResolvedValue({
      tenant_id: 'tenant-1', id: 'team-1', visible: false, access: 'lost', membership_id: 'membership-1', status: 'left',
    });
    renderTeams();
    fireEvent.click(await screen.findByRole('button', { name: /Delivery/ }));
    expect(await screen.findByText(/Can act for this team/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Mark left' }));
    expect(await screen.findByText('Your change was saved, but this team is no longer available to you. Refresh to continue.')).toBeTruthy();
    expect(screen.queryByText(/Can act for this team/)).toBeNull();

    cleanup();
    signedIn = { id: 'admin', role: 'platform_admin', tenant_id: null, department_id: null, display_name: 'Admin' };
    localStorage.setItem('current_tenant_id', 'company-a');
    let resolveRead: (value: ReturnType<typeof team>) => void = () => {};
    list.mockResolvedValue({
      tenant_id: 'company-a',
      caller: { id: 'admin', display_name: 'Admin', role: 'platform_admin', tenant_id: null, department_id: null, department_name: null, can_create: false, can_install: true },
      teams: [team({ tenant_id: 'company-a', name: 'Company A team' })],
    });
    read.mockImplementation(() => new Promise((resolve) => { resolveRead = resolve; }));
    renderTeams();
    fireEvent.click(await screen.findByRole('button', { name: /Company A team/ }));
    await act(async () => {
      localStorage.setItem('current_tenant_id', 'company-b');
      window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'company-b' }));
      resolveRead(team({
        tenant_id: 'company-a',
        name: 'Company A team',
        members: [{
          id: 'member-a', role: 'member', status: 'active', registered: true, authority: true,
          display_name: 'Company A private person', email: null, user_id: 'person-a', org_member_id: null,
          joined_at: null, left_at: null,
        }],
      }));
    });
    expect(screen.queryByText('Company A private person')).toBeNull();
  });

  it('does not restore company A detail if the company changes during the awaited refetch', async () => {
    signedIn = { id: 'admin', role: 'platform_admin', tenant_id: null, department_id: null, display_name: 'Admin' };
    localStorage.setItem('current_tenant_id', 'company-a');
    const person = {
      id: 'member-a', role: 'member', status: 'active', registered: true, authority: true,
      display_name: 'Company A private person', email: null, user_id: 'person-a', org_member_id: null,
      joined_at: null, left_at: null,
    };
    const companyA = team({
      id: 'team-a', tenant_id: 'company-a', name: 'Company A team', my_role: null, members: [person],
    });
    const caller = {
      id: 'admin', display_name: 'Admin', role: 'platform_admin', tenant_id: null,
      department_id: null, department_name: null, can_create: false, can_install: true,
    };
    let aListCalls = 0;
    let bListCalls = 0;
    let resolveRefetch: (value: { tenant_id: string; caller: typeof caller; teams: Array<typeof companyA> }) => void = () => {};
    list.mockImplementation(() => {
      const company = localStorage.getItem('current_tenant_id');
      if (company === 'company-b') {
        bListCalls += 1;
        return Promise.resolve({ tenant_id: 'company-b', caller, teams: [] });
      }
      aListCalls += 1;
      if (aListCalls === 1) return Promise.resolve({ tenant_id: 'company-a', caller, teams: [companyA] });
      return new Promise((resolve) => { resolveRefetch = resolve; });
    });
    read.mockResolvedValue(companyA);
    renderTeams();
    fireEvent.click(await screen.findByRole('button', { name: /Company A team/ }));
    await waitFor(() => expect(aListCalls).toBeGreaterThan(1));
    await act(async () => {
      localStorage.setItem('current_tenant_id', 'company-b');
      window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'company-b' }));
    });
    await waitFor(() => expect(bListCalls).toBeGreaterThan(0));
    await act(async () => {
      resolveRefetch({ tenant_id: 'company-a', caller, teams: [companyA] });
    });
    expect(screen.queryByText('Company A private person')).toBeNull();
  });
});
