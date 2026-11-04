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

  it('renders the five company admin areas for an organization administrator', () => {
    const markup = renderToStaticMarkup(<WorkspaceLayout />);

    expect(markup).toContain('data-heading-key="nav.enterprise"');
    expect(markup).toContain('data-heading="Company Admin"');
    expect(markup).toContain('href="/enterprise/users"');
    expect(markup).toContain('href="/enterprise/llm"');
    expect(markup).toContain('href="/enterprise/info"');
    expect(markup).toContain('href="/enterprise/approvals"');
    expect(markup).toContain('href="/enterprise/audit"');
    // The dashboard and the other sections left the navigation (B4); their
    // routes stay reachable by URL.
    expect(markup).not.toContain('href="/enterprise/dashboard"');
    expect(markup).not.toContain('href="/enterprise/knowledge"');
    expect(markup).not.toContain('href="/enterprise/invitations"');
    expect(markup).not.toContain('href="/enterprise/runtime-budgets"');
  });

  it('renders the same five areas for a platform administrator with a truthful platform heading', () => {
    auth.role = 'platform_admin';

    const markup = renderToStaticMarkup(<WorkspaceLayout />);

    expect(markup).toContain('data-heading-key="nav.superAdmin"');
    expect(markup).toContain('data-heading="Platform Admin"');
    expect(markup).toContain('href="/enterprise/users"');
    expect(markup).toContain('href="/enterprise/audit"');
    expect(markup).not.toContain('href="/enterprise/dashboard"');
  });

  it.each(['org_admin', 'platform_admin'])('keeps a direct Back to App return for %s', (role) => {
    auth.role = role;

    const markup = renderToStaticMarkup(<WorkspaceLayout />);

    expect(markup).toContain('href="/"');
    expect(markup).toContain('data-label-key="nav.backToApp"');
  });
});
