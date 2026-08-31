import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({ role: 'org_admin' }));

vi.mock('../../stores', () => ({
  useAuthStore: (selector: (state: any) => unknown) => selector({
    user: { id: `${auth.role}-1`, role: auth.role, tenant_id: 'tenant-1' },
  }),
}));

vi.mock('../shared/SurfaceLayout', () => ({
  default: ({ headingKey, headingFallback, navItems }: { headingKey: string; headingFallback: string; navItems: Array<{ to: string }> }) => (
    <div data-heading-key={headingKey} data-heading={headingFallback}>
      {navItems.map((item) => <a key={item.to} href={item.to}>{item.to}</a>)}
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

  it('renders only platform sections and a truthful platform heading for a platform administrator', () => {
    auth.role = 'platform_admin';

    const markup = renderToStaticMarkup(<WorkspaceLayout />);

    expect(markup).toContain('data-heading-key="nav.superAdmin"');
    expect(markup).toContain('data-heading="Platform Admin"');
    expect(markup).toContain('href="/enterprise/runtime-budgets"');
    expect(markup).not.toContain('href="/enterprise/knowledge"');
    expect(markup).not.toContain('href="/enterprise/users"');
    expect(markup).not.toContain('href="/enterprise/invitations"');
  });
});
