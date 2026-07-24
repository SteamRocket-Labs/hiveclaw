import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import PlatformFeatureFlagsSection, {
  featureFlagAudienceSummary,
  normalizeFeatureFlagDraft,
} from './PlatformFeatureFlagsSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

const FLAGS = [
  {
    id: 'flag-1',
    key: 'runtime_continuity_v1',
    description: 'Durable runtime continuity',
    flag_type: 'percentage' as const,
    enabled: false,
    rollout_percentage: 25,
    allowed_tenant_ids: null,
    allowed_user_ids: null,
    overrides: {
      'tenant:00000000-0000-4000-8000-000000000001': true,
    },
    expires_at: '2026-08-01T00:00:00+00:00',
    created_at: '2026-07-01T00:00:00+00:00',
    updated_at: '2026-07-02T00:00:00+00:00',
  },
];

describe('PlatformFeatureFlagsSection', () => {
  it('renders a platform-only rollout surface with status, audience, expiry, and recovery actions', () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <PlatformFeatureFlagsSection initialFlags={FLAGS} />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Feature Rollout');
    expect(markup).toContain('runtime_continuity_v1');
    expect(markup).toContain('25% deterministic rollout');
    expect(markup).toContain('1 explicit override');
    expect(markup).toContain('Expires');
    expect(markup).toContain('Edit');
    expect(markup).toContain('Delete');
  });

  it('normalizes typed targeting fields without accepting a raw JSON editor', () => {
    expect(featureFlagAudienceSummary(FLAGS[0])).toEqual([
      '25% deterministic rollout',
      '1 explicit override',
    ]);
    expect(
      normalizeFeatureFlagDraft({
        key: 'runtime_continuity_v1',
        description: 'Durable runtime continuity',
        flagType: 'tenant_gate',
        enabled: false,
        rolloutPercentage: '',
        allowedTenantIds: '00000000-0000-4000-8000-000000000001\n',
        allowedUserIds: '',
        expiresAt: '',
        overrides: [
          {
            scope: 'user',
            id: '00000000-0000-4000-8000-000000000002',
            enabled: false,
          },
        ],
      }),
    ).toEqual({
      key: 'runtime_continuity_v1',
      description: 'Durable runtime continuity',
      flag_type: 'tenant_gate',
      enabled: false,
      rollout_percentage: null,
      allowed_tenant_ids: ['00000000-0000-4000-8000-000000000001'],
      allowed_user_ids: null,
      overrides: {
        'user:00000000-0000-4000-8000-000000000002': false,
      },
      expires_at: null,
    });
  });

  it('is wired only into the platform-admin settings surface', () => {
    const platformSource = readFileSync(new URL('./AdminPlatformSection.tsx', import.meta.url), 'utf8');
    const companySource = readFileSync(new URL('../AdminCompanies.tsx', import.meta.url), 'utf8');

    expect(platformSource).toContain('<PlatformFeatureFlagsSection');
    expect(companySource).toContain("user?.role !== 'platform_admin'");
  });
});
