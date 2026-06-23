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
        created_at: '2026-06-20T00:00:00Z',
        last_active_at: '2026-06-23T07:00:00Z',
        execution_mode: 'standard',
        agent_type: 'openclaw',
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
    expect(markup).toContain('Create employee');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Local Runner');
    expect(markup).toContain('Market research and synthesis');
    expect(markup).toContain('Owned by me');
    expect(markup).toContain('Company shared');
    expect(markup).toContain('Recommended');
    expect(markup).toContain('Coordinator');
    expect(markup).toContain('Local runtime');
    expect(markup).toContain('href="/agents/agent-1#chat"');
    expect(markup).toContain('href="/agents/agent-1#knowledge"');
    expect(markup).toContain('href="/agents/agent-1#workflows"');
    expect(markup).toContain('href="/agents/agent-1#relationships"');
    expect(markup).toContain('href="/local-agents"');
  });
});
