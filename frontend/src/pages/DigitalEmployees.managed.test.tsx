import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({ role: 'org_admin' as string }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useMutation: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    if (queryKey[0] === 'hr-agent') return { data: null, isLoading: false };
    if (queryKey[0] === 'hr-recoverable-drafts') return { data: [], isLoading: false };
    return {
      // Truthful administrator projection (PDEC-013): every in-scope row
      // carries access_level="manage" with a truthful is_owner flag.
      data: [
        {
          id: 'agent-own',
          name: 'Own Assistant',
          role_description: 'Mine',
          status: 'running',
          creator_id: 'admin-1',
          owner_user_id: 'admin-1',
          is_owner: true,
          access_level: 'manage',
          action_capabilities: { can_manage_permissions: true },
          created_at: '2026-06-20T00:00:00Z',
          agent_type: 'native',
        },
        {
          id: 'agent-employee-private',
          name: 'Payroll Clerk',
          role_description: 'Employee-private Agent',
          status: 'idle',
          creator_id: 'employee-9',
          owner_user_id: 'employee-9',
          is_owner: false,
          access_level: 'manage',
          action_capabilities: { can_manage_permissions: true },
          created_at: '2026-06-19T00:00:00Z',
          agent_type: 'native',
        },
      ],
      isLoading: false,
    };
  },
}));

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, className }: any) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
}));

vi.mock('../stores', () => ({
  useAuthStore: (selector?: any) => {
    const state = { user: { id: 'admin-1', role: auth.role } };
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

import DigitalEmployees from './DigitalEmployees';

describe('DigitalEmployees managed-agent labeling', () => {
  beforeEach(() => {
    auth.role = 'org_admin';
  });

  it('labels a non-owned in-scope Agent as managed for administrators, never as company-shared', () => {
    const markup = renderToStaticMarkup(<DigitalEmployees />);

    expect(markup).toContain('Payroll Clerk');
    // metric + filter + chip all use the managed label for the admin view
    expect(markup).toContain('>Managed</span>');
    expect(markup).toContain('>Managed</button>');
    expect(markup).not.toContain('Company shared');
    expect(markup).toContain('Owned by me');
  });

  it('keeps the company-shared label for the employee projection', () => {
    auth.role = 'member';

    const markup = renderToStaticMarkup(<DigitalEmployees />);

    expect(markup).toContain('Payroll Clerk');
    expect(markup).toContain('Company shared');
    expect(markup).not.toContain('>Managed</span>');
  });
});
