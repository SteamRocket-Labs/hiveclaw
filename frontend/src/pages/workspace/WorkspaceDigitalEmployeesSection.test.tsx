import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({ role: 'org_admin' }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string, values?: Record<string, unknown>) => {
      if (!fallback) return _key;
      return fallback.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(values?.[name] ?? ''));
    },
  }),
}));

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, className }: any) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
}));

vi.mock('../../stores', () => ({
  useAuthStore: (selector?: any) => {
    const state = { user: { id: 'admin-1', role: auth.role, tenant_id: 'tenant-1' } };
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    if (String(queryKey[0]) === 'hr-agent') {
      return {
        data: { id: 'hr-agent-1', name: '__system_hr__', status: 'running' },
        isLoading: false,
        error: null,
      };
    }
    if (String(queryKey[0]) === 'agents') {
      return {
        data: [
          {
            id: 'hr-agent-1',
            name: '__system_hr__',
            role_description: 'System HR',
            status: 'running',
            creator_id: 'admin-1',
            owner_user_id: 'admin-1',
            created_at: '2026-06-20T00:00:00Z',
            agent_type: 'native',
          },
          {
            id: 'agent-1',
            name: 'AI Product Manager',
            role_description: 'Product analysis',
            status: 'idle',
            creator_id: 'admin-1',
            owner_user_id: 'owner-2',
            created_at: '2026-06-20T00:00:00Z',
            agent_type: 'native',
          },
        ],
        isLoading: false,
        error: null,
      };
    }
    if (String(queryKey[0]) === 'users') {
      return {
        data: [
          {
            id: 'admin-1',
            username: 'admin',
            display_name: 'Company Admin',
            email: 'admin@example.com',
            role: 'org_admin',
            is_active: true,
            tokens_used_today: 0,
            tokens_used_month: 0,
            tokens_used_total: 0,
            agents_count: 1,
          },
          {
            id: 'owner-2',
            username: 'owner',
            display_name: 'Agent Owner',
            email: 'owner@example.com',
            role: 'member',
            is_active: true,
            tokens_used_today: 0,
            tokens_used_month: 0,
            tokens_used_total: 0,
            agents_count: 1,
          },
        ],
        isLoading: false,
        error: null,
      };
    }
    if (String(queryKey[0]) === 'agent-retirement-pending') {
      return {
        data: [
          {
            id: 'deactivated-1',
            name: 'Pending Recall Employee',
            remote_work_count: 1,
            local_work_count: 0,
            inactive_requester_work_count: 0,
          },
          {
            id: 'deactivated-2',
            name: 'Settled Employee',
            remote_work_count: 0,
            local_work_count: 0,
            inactive_requester_work_count: 0,
          },
        ],
        isLoading: false,
        error: null,
      };
    }
    return { data: [], isLoading: false, error: null };
  },
  useMutation: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
  }),
}));

vi.mock('../../components/AppDialogs', () => ({
  showAppToast: vi.fn(),
  requestAppConfirm: vi.fn(),
}));

import WorkspaceDigitalEmployeesSection from './WorkspaceDigitalEmployeesSection';

describe('WorkspaceDigitalEmployeesSection', () => {
  beforeEach(() => {
    auth.role = 'org_admin';
  });

  it('renders the standalone admin-only digital employee list with guarded deactivate actions', () => {
    const markup = renderToStaticMarkup(<WorkspaceDigitalEmployeesSection selectedTenantId="tenant-1" />);

    expect(markup).toContain('Digital Employee Management');
    expect(markup).toContain('AI Product Manager');
    expect(markup).toContain('Agent Owner');
    expect(markup).toContain('href="/agents/agent-1"');
    expect(markup).toContain('Change owner');
    expect(markup).toContain('Deactivate');
    expect(markup).not.toContain('Delete');
    expect(markup).toContain('System protected');
  });

  it('describes the governed surface as review and deactivate, never review and remove', () => {
    const markup = renderToStaticMarkup(<WorkspaceDigitalEmployeesSection selectedTenantId="tenant-1" />);

    expect(markup).toContain('review and deactivate tenant digital employees');
    expect(markup).not.toContain('review and remove');
  });

  it('offers retry deactivation only for deactivated rows with unconfirmed outward work, and links to the employee page', () => {
    const markup = renderToStaticMarkup(<WorkspaceDigitalEmployeesSection selectedTenantId="tenant-1" />);

    expect(markup).toContain('Pending Recall Employee');
    expect(markup).toContain('Settled Employee');
    expect(markup).toContain('Retry deactivation');
    expect(markup).toContain('href="/agents/deactivated-1"');
    expect(markup).toContain('href="/agents/deactivated-2"');
    const settled = markup.split('Settled Employee')[1].split('</li>')[0];
    expect(settled).not.toContain('Retry deactivation');
  });

  it('states the committed deactivation first in the retirement-pending copy', async () => {
    const en = (await import('../../i18n/en.json')).default;
    const zh = (await import('../../i18n/zh.json')).default;
    // A committed deactivation with unsettled outward work must not read like
    // a failed, retry-looking retirement (G1 round 2 review).
    expect(en.workspace.digitalEmployees.retirementPending.startsWith('Deactivated')).toBe(true);
    expect(zh.workspace.digitalEmployees.retirementPending.startsWith('已停用')).toBe(true);
  });

  it('mounts the same selected-company employee management for a platform administrator (PDEC-013)', () => {
    auth.role = 'platform_admin';

    const markup = renderToStaticMarkup(<WorkspaceDigitalEmployeesSection selectedTenantId="tenant-1" />);

    expect(markup).not.toContain('Only company administrators can manage digital employees here.');
    expect(markup).toContain('AI Product Manager');
    expect(markup).toContain('Deactivate');
    expect(markup).toContain('Change owner');
    expect(markup).toContain('System protected');
  });

  it('keeps plain members out of the company employee management surface', () => {
    auth.role = 'member';

    const markup = renderToStaticMarkup(<WorkspaceDigitalEmployeesSection selectedTenantId="tenant-1" />);

    expect(markup).toContain('Only company administrators can manage digital employees here.');
    expect(markup).not.toContain('AI Product Manager');
    expect(markup).not.toContain('Deactivate');
  });
});
