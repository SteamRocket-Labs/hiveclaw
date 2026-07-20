import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

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
    const state = { user: { id: 'admin-1', role: 'org_admin', tenant_id: 'tenant-1' } };
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
    return { data: [], isLoading: false, error: null };
  },
  useMutation: () => ({
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
  it('renders the standalone admin-only digital employee list with guarded delete actions', () => {
    const markup = renderToStaticMarkup(<WorkspaceDigitalEmployeesSection selectedTenantId="tenant-1" />);

    expect(markup).toContain('Digital Employee Management');
    expect(markup).toContain('AI Product Manager');
    expect(markup).toContain('Agent Owner');
    expect(markup).toContain('href="/agents/agent-1"');
    expect(markup).toContain('Change owner');
    expect(markup).toContain('Delete employee');
    expect(markup).toContain('System protected');
    expect(markup).not.toContain('Delete __system_hr__');
  });
});
