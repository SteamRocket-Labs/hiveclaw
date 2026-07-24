import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import AdminCompaniesSection from './AdminCompaniesSection';
import AdminPlatformSection from './AdminPlatformSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback || key.split('.').pop() || key,
  }),
}));

vi.mock('@tabler/icons-react', () => {
  const iconStub = (name: string) => () => <span>{name} Icon</span>;
  return {
    IconAlertTriangle: iconStub('AlertTriangle'),
    IconFilter: iconStub('Filter'),
    IconPlayerPause: iconStub('PlayerPause'),
    IconShieldCheck: iconStub('ShieldCheck'),
    IconShieldOff: iconStub('ShieldOff'),
  };
});

describe('Admin companies extracted sections', () => {
  it('renders AdminPlatformSection as a standalone platform settings module', () => {
    // AdminPlatformSection embeds WorkspaceRuntimeBudgetsSection, which reads
    // the query client — provide a real one instead of mocking the section away.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <AdminPlatformSection />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Notification Bar');
    expect(markup).toContain('Public URL');
    expect(markup).toContain('Allow users to create their own companies');
    expect(markup).toContain('Feature Rollout');
  });

  it('renders AdminCompaniesSection as a standalone tenant management module', () => {
    const markup = renderToStaticMarkup(
      <AdminCompaniesSection
        initialCompanies={[
          {
            id: 'company-1',
            name: 'Acme',
            slug: 'acme',
            org_admin_email: 'admin@acme.test',
            user_count: 12,
            agent_count: 4,
            total_tokens: 123456,
            created_at: '2026-03-01T00:00:00Z',
            is_active: true,
          },
        ]}
      />,
    );

    expect(markup).toContain('Create Company');
    expect(markup).toContain('Acme');
    expect(markup).toContain('admin@acme.test');
    expect(markup).toContain('123K');
  });
});
