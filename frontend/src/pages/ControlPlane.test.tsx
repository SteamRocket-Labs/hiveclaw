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
    expect(markup).toContain('Digital Employees');
    expect(markup).toContain('Models &amp; Budget');
    expect(markup).toContain('Extension Catalog');
    expect(markup).toContain('Memory Governance');
    expect(markup).toContain('Company Knowledge');
    expect(markup).toContain('Channels &amp; Integrations');
    expect(markup).toContain('Approval Center');
    expect(markup).toContain('Audit Log');
    expect(markup).toContain('Action Guardrails');
    expect(markup).toContain('Local Agent Channel');
    expect(markup).toContain('href="/enterprise/hr"');
    expect(markup).toContain('href="/enterprise/digital-employees"');
    expect(markup).toContain('href="/enterprise/action-guardrails"');
    expect(markup).toContain('href="/enterprise/knowledge"');
    expect(markup).toContain('href="/local-agents"');
    expect(markup).not.toContain('Company Knowledge Base is not implemented in this release');
    expect(markup).toContain('read-only export of retired shared files');
    expect(markup).toContain('Review, publish, authorize, retire, and restore governed knowledge for employees.');
  });

  it('embeds legacy workspace sections inside the new control-plane shell', () => {
    const markup = renderToStaticMarkup(<ControlPlane tab="extensions" />);

    expect(markup).toContain('Extension Catalog');
    expect(markup).toContain('Enterprise section extensions chrome=embedded');
    expect(markup).not.toContain('Company Admin');
  });

  it('labels runtime budgets as runtime protection rather than a technical budget page', () => {
    const markup = renderToStaticMarkup(<ControlPlane tab="runtime_budgets" />);

    expect(markup).toContain('Runtime Protection');
    expect(markup).toContain('Company-level limits that take priority over the platform defaults.');
    expect(markup).toContain('Enterprise section runtime_budgets chrome=embedded');
  });

});
