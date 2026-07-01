import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    if (String(queryKey[0]) === 'enterprise-stats') {
      return {
        data: {
          total_users: 7,
          total_agents: 5,
          running_agents: 3,
          pending_approvals: 2,
        },
      };
    }
    return { data: [] };
  },
}));

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, className }: any) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
}));

vi.mock('./EnterpriseSettings', () => ({
  default: ({ forcedTab, chrome }: { forcedTab: string; chrome?: string }) => (
    <div>
      Enterprise section {forcedTab} chrome={chrome}
    </div>
  ),
}));

import ControlPlane from './ControlPlane';

describe('ControlPlane', () => {
  it('renders a real control-plane overview that links every old admin capability into the new shell', () => {
    const markup = renderToStaticMarkup(<ControlPlane />);

    expect(markup).toContain('Control Plane');
    expect(markup).toContain('Agent Governance');
    expect(markup).toContain('Models &amp; Budget');
    expect(markup).toContain('Capabilities &amp; Tools');
    expect(markup).toContain('Team &amp; Delegation');
    expect(markup).toContain('Memory Governance');
    expect(markup).toContain('Channels &amp; Integrations');
    expect(markup).toContain('Approval Center');
    expect(markup).toContain('Audit Log');
    expect(markup).toContain('Assets &amp; Automation');
    expect(markup).toContain('Local Agent Channel');
    expect(markup).toContain('href="/enterprise/hr"');
    expect(markup).toContain('href="/local-agents"');
  });

  it('embeds legacy workspace sections inside the new control-plane shell', () => {
    const markup = renderToStaticMarkup(<ControlPlane tab="tools" />);

    expect(markup).toContain('Capabilities &amp; Tools');
    expect(markup).toContain('Enterprise section tools chrome=embedded');
    expect(markup).not.toContain('Company Admin');
  });

  it('opens the behavior evaluation section instead of falling back to the overview dashboard', () => {
    const markup = renderToStaticMarkup(<ControlPlane tab="eval_ci" />);

    expect(markup).toContain('Behavior Evaluation');
    expect(markup).toContain('Enterprise section eval_ci chrome=embedded');
    expect(markup).not.toContain('Operating areas');
  });
});
