import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({
    data: [
      {
        id: 'agent-1',
        name: 'Research Lead',
        role_description: 'Market research and synthesis',
        status: 'running',
        creator_id: 'user-1',
        owner_user_id: 'user-3',
        is_owner: true,
        access_level: 'manage',
        action_capabilities: {
          can_use: true,
          can_manage: true,
          can_manage_permissions: true,
          can_manage_schedule: true,
          can_manage_channel: true,
          can_transfer_ownership: true,
        },
        created_at: '2026-06-20T00:00:00Z',
        last_active_at: '2026-06-23T08:00:00Z',
        execution_mode: 'coordinator',
        agent_type: 'native',
      },
      {
        id: 'agent-2',
        name: 'Local Runner',
        role_description: 'Local runtime bridge',
        status: 'idle',
        creator_id: 'user-2',
        owner_user_id: 'user-2',
        is_owner: false,
        access_level: 'use',
        action_capabilities: {
          can_use: true,
          can_manage: false,
          can_manage_permissions: false,
          can_manage_schedule: false,
          can_manage_channel: false,
          can_transfer_ownership: false,
        },
        created_at: '2026-06-20T00:00:00Z',
        last_active_at: '2026-06-23T07:00:00Z',
        execution_mode: 'standard',
        agent_type: 'local_agent',
      },
    ],
    isLoading: false,
  }),
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
    const state = { user: { id: 'user-1', role: 'org_admin' } };
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

import DigitalEmployees from './DigitalEmployees';

describe('DigitalEmployees page', () => {
  it('renders a real employee directory entry with deep links into existing agent surfaces', () => {
    const markup = renderToStaticMarkup(<DigitalEmployees />);

    expect(markup).toContain('Digital Employees');
    expect(markup).toContain('Create via HR');
    expect(markup).toContain('employee-directory-create');
    expect(markup).toContain('class="employee-grid employee-grid-flat"');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Local Runner');
    expect(markup).toContain('class="employee-card-body"');
    expect(markup).toContain('class="employee-card-copy"');
    expect(markup).toContain('Market research and synthesis');
    expect(markup).toContain('Owned by me');
    expect(markup).toContain('Company shared');
    expect(markup).toContain('Recommended');
    expect(markup).not.toContain('Coordinator');
    expect(markup).toContain('Local runtime');
    expect(markup).toContain('href="/agents/agent-1#chat"');
    expect(markup).toContain('href="/agents/agent-1#knowledge"');
    expect(markup).toContain('href="/agents/agent-1#workflows"');
    expect(markup).toContain('href="/agents/agent-1#a2a"');
    expect(markup).toContain('href="/agents/agent-2#workspace"');
    expect(markup).not.toContain('href="/local-agents"');
  });
});
