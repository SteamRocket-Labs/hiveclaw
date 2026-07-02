import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let automationAgentIdsKey = '';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
  useQuery: (options: { queryKey: unknown[] }) => {
    const key = String(options.queryKey[0]);
    if (key === 'agents') {
      return {
        data: [
          {
            id: 'agent-1',
            name: 'Research Lead',
            role_description: 'Market research',
            status: 'running',
            creator_id: 'user-1',
            created_at: '2026-06-20T00:00:00Z',
          },
          {
            id: 'agent-shared',
            name: 'Shared Analyst',
            role_description: 'Company shared research',
            status: 'running',
            creator_id: 'user-2',
            created_at: '2026-06-21T00:00:00Z',
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'workflow-definitions') {
      return {
        data: [
          {
            id: 'workflow-1',
            name: 'Weekly market sweep',
            description: 'Collect and synthesize market signals.',
            status: 'active',
            definition_version: 1,
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'feature-hub-automation-rows') {
      automationAgentIdsKey = String(options.queryKey[1] || '');
      return {
        data: [
          {
            id: 'agent-1:trigger-current',
            agentId: 'agent-1',
            agentName: 'Research Lead',
            name: 'Daily railway log check',
            scheduleText: 'Tuesday at 10:00',
            status: 'running',
            statusText: 'running',
            section: 'current',
            href: '/agents/agent-1#aware',
          },
          {
            id: 'agent-1:trigger-paused',
            agentId: 'agent-1',
            agentName: 'Research Lead',
            name: 'Hive H7 Evidence Loop',
            scheduleText: 'Every day at 09:00',
            status: 'paused',
            statusText: 'paused',
            section: 'paused',
            href: '/agents/agent-1#aware',
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'feature-hub-plans') {
      return {
        data: [
          {
            agentId: 'agent-1',
            agentName: 'Research Lead',
            id: 'plan-1',
            title: 'Confirm product research scope',
            status: 'awaiting_confirmation',
            updatedAt: '2026-06-23T08:00:00Z',
            href: '/agents/agent-1#chat',
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'feature-hub-approvals') {
      return {
        data: [
          {
            id: 'approval-1',
            actionType: 'send_external_message',
            agentName: 'Research Lead',
            status: 'pending',
            createdAt: '2026-06-23T08:30:00Z',
          },
        ],
        isLoading: false,
      };
    }
    if (key === 'feature-hub-memory') {
      return {
        data: [
          {
            agentId: 'agent-1',
            agentName: 'Research Lead',
            active: 12,
            stale: 1,
            pendingSoulCandidates: 2,
            skillCandidates: 3,
            href: '/agents/agent-1#knowledge',
          },
        ],
        isLoading: false,
      };
    }
    return { data: [], isLoading: false };
  },
}));

vi.mock('../stores', () => ({
  useAuthStore: (selector?: any) => {
    const state = {
      user: {
        id: 'user-1',
        username: 'rocky',
        email: 'rocky@example.com',
        display_name: 'rocky',
        role: 'member',
        is_active: true,
        created_at: '2026-06-01T00:00:00Z',
      },
    };
    return selector ? selector(state) : state;
  },
}));

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, className }: any) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
}));

import WorkspaceFeatureHub from './WorkspaceFeatureHub';

describe('WorkspaceFeatureHub', () => {
  beforeEach(() => {
    automationAgentIdsKey = '';
  });

  it('renders automation assets from the real workflow definition adapter', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="automations" />);

    expect(markup).toContain('Automations');
    expect(markup).toContain('Current');
    expect(markup).toContain('Paused');
    expect(markup).toContain('Daily railway log check');
    expect(markup).toContain('Hive H7 Evidence Loop');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Tuesday at 10:00');
    expect(markup).toContain('paused');
    expect(markup).toContain('Manual create task');
    expect(markup).toContain('href="/agents/agent-1#aware"');
    expect(markup).not.toContain('Weekly market sweep');
    expect(markup).not.toContain('Skill registry');
  });

  it('scopes automation aggregation to agents owned by the current user', () => {
    renderToStaticMarkup(<WorkspaceFeatureHub kind="automations" />);

    expect(automationAgentIdsKey).toBe('agent-1');
    expect(automationAgentIdsKey).not.toContain('agent-shared');
  });

  it('opens the manual automation create dialog on the automation hub', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="automations" initialAutomationCreateOpen />);

    expect(markup).toContain('role="dialog"');
    expect(markup).toContain('Manual create task');
    expect(markup).toContain('Automation title');
    expect(markup).toContain('Select agent');
    expect(markup).toContain('Every hour');
    expect(markup).toContain('Every day');
    expect(markup).toContain('Every week');
    expect(markup).toContain('Custom');
    expect(markup).not.toContain('Work Number');
    expect(markup).not.toContain('Reasoning strength');
    expect(markup).not.toContain('Mode');
  });

  it('renders memory governance links without inventing a separate memory product surface', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="memory" />);

    expect(markup).toContain('Memory &amp; Knowledge');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Memory entries');
    expect(markup).toContain('Failure modes (active)');
    expect(markup).toContain('Soul candidates');
    expect(markup).toContain('Skill candidates');
    expect(markup).toContain('href="/agents/agent-1#knowledge"');
    expect(markup).toContain('href="/enterprise/memory"');
  });

  it('renders a cross-agent plan queue instead of only routing users away', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="plans" />);

    expect(markup).toContain('Confirm product research scope');
    expect(markup).toContain('awaiting_confirmation');
    expect(markup).toContain('href="/agents/agent-1#chat"');
  });

  it('renders the company approval queue from the enterprise approval adapter', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="approvals" />);

    expect(markup).toContain('send_external_message');
    expect(markup).toContain('pending');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('href="/enterprise/approvals"');
  });

  it('renders A2A and Team as a real front-end surface over A2A collaborators, subagents, and local channel entry points', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="team" />);

    expect(markup).toContain('A2A / Team');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Session-local team');
    expect(markup).toContain('A2A collaborators');
    expect(markup).toContain('Local Agent Channel');
    expect(markup).toContain('href="/agents/agent-1#a2a"');
    expect(markup).toContain('href="/agents/agent-1#subagents"');
    expect(markup).toContain('href="/enterprise/subagents"');
    expect(markup).toContain('href="/local-agents"');
  });
});
