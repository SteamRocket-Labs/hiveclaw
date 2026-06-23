import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
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

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, className }: any) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
}));

import WorkspaceFeatureHub from './WorkspaceFeatureHub';

describe('WorkspaceFeatureHub', () => {
  it('renders automation assets from the real workflow definition adapter', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="automations" />);

    expect(markup).toContain('Automations');
    expect(markup).toContain('Weekly market sweep');
    expect(markup).toContain('Collect and synthesize market signals.');
    expect(markup).toContain('href="/agents/agent-1#workflows"');
    expect(markup).toContain('href="/enterprise/skills"');
  });

  it('renders memory governance links without inventing a separate memory product surface', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="memory" />);

    expect(markup).toContain('Memory &amp; Knowledge');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Active memories');
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

  it('renders A2A and Team as a real front-end surface over relationships, subagents, and local channel entry points', () => {
    const markup = renderToStaticMarkup(<WorkspaceFeatureHub kind="team" />);

    expect(markup).toContain('A2A / Team');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('Session-local team');
    expect(markup).toContain('Org delegation');
    expect(markup).toContain('Local Agent Channel');
    expect(markup).toContain('href="/agents/agent-1#relationships"');
    expect(markup).toContain('href="/agents/agent-1#subagents"');
    expect(markup).toContain('href="/enterprise/subagents"');
    expect(markup).toContain('href="/local-agents"');
  });
});
