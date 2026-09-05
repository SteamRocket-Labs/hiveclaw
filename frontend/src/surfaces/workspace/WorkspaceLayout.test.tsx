import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({ role: 'org_admin' }));

vi.mock('../../stores', () => ({
  useAuthStore: (selector: (state: any) => unknown) => selector({
    user: { id: `${auth.role}-1`, role: auth.role, tenant_id: 'tenant-1' },
  }),
}));

vi.mock('../shared/SurfaceLayout', () => ({
  default: ({ headingKey, headingFallback, navItems }: { headingKey: string; headingFallback: string; navItems: Array<{ to: string; labelKey: string }> }) => (
    <div data-heading-key={headingKey} data-heading={headingFallback}>
      {navItems.map((item) => <a key={item.to} href={item.to} data-label-key={item.labelKey}>{item.to}</a>)}
    </div>
  ),
}));

import WorkspaceLayout from './WorkspaceLayout';

describe('WorkspaceLayout audience navigation', () => {
  beforeEach(() => {
    auth.role = 'org_admin';
  });

  it('renders the full company workspace for an organization administrator', () => {
    const markup = renderToStaticMarkup(<WorkspaceLayout />);

    expect(markup).toContain('data-heading-key="nav.enterprise"');
    expect(markup).toContain('data-heading="Company Admin"');
    expect(markup).toContain('href="/enterprise/knowledge"');
    expect(markup).toContain('href="/enterprise/users"');
    expect(markup).toContain('href="/enterprise/invitations"');
  });

  it('renders the full selected-company workspace with a truthful platform heading for a platform administrator', () => {
    auth.role = 'platform_admin';

    const markup = renderToStaticMarkup(<WorkspaceLayout />);

    expect(markup).toContain('data-heading-key="nav.superAdmin"');
    expect(markup).toContain('data-heading="Platform Admin"');
    expect(markup).toContain('href="/enterprise/runtime-budgets"');
    expect(markup).toContain('href="/enterprise/knowledge"');
    expect(markup).toContain('href="/enterprise/users"');
    expect(markup).toContain('href="/enterprise/invitations"');
  });

  it.each(['org_admin', 'platform_admin'])('keeps a direct Back to App return for %s', (role) => {
    auth.role = role;

    const markup = renderToStaticMarkup(<WorkspaceLayout />);

    expect(markup).toContain('href="/home"');
    expect(markup).toContain('data-label-key="nav.backToApp"');
  });
});
