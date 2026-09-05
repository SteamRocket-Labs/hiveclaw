import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

describe('application surface routes', () => {
  it('mounts a real user home instead of redirecting members to the employee list', () => {
    const source = readFileSync(new URL('./App.tsx', import.meta.url), 'utf8');

    expect(source).toContain("const Dashboard = lazy(() => import('./pages/Dashboard'))");
    expect(source).toContain('<Route path="home" element={<Dashboard />} />');
    expect(source).not.toContain('<Route path="home" element={<Navigate to="/agents" replace />} />');
  });

  it('keeps the design gallery out of the production public surface', () => {
    const source = readFileSync(new URL('./App.tsx', import.meta.url), 'utf8');

    expect(source).not.toContain("const DesignGallery = lazy(() => import('./pages/DesignGallery'))");
    expect(source).toContain('const DesignGallery = import.meta.env.DEV');
    expect(source).toContain('DesignGallery ? (');
    expect(source).toContain('<Route path="/design-gallery" element={<Navigate to="/" replace />} />');
  });

  it('mounts separate employee Company Library and governed Company Knowledge control-plane routes', () => {
    const source = readFileSync(new URL('./App.tsx', import.meta.url), 'utf8');

    expect(source).toContain("const CompanyKnowledgeLibrary = lazy(() => import('./pages/CompanyKnowledgeLibrary'))");
    expect(source).toContain(
      "const CompanyKnowledgeControlPlane = lazy(() => import('./pages/CompanyKnowledgeControlPlane'))",
    );
    expect(source).toContain('<Route path="knowledge/company" element={<CompanyKnowledgeLibrary />} />');
    expect(source).toContain(
      '<Route path="knowledge" element={<ScopedAdminGuard><CompanyKnowledgeControlPlane /></ScopedAdminGuard>} />',
    );
  });

  it('admits platform administrators to the company Plaza surface as scoped business actors', () => {
    const source = readFileSync(new URL('./App.tsx', import.meta.url), 'utf8');

    // PDEC-013: the guard no longer excludes platform_admin; the server
    // enforces the selected-company boundary with typed responses.
    expect(source).toContain('<Route path="plaza" element={<CompanyBusinessGuard><Plaza /></CompanyBusinessGuard>} />');
  });

  it('requires an Agent action capability and a live authority shell before enabling management', () => {
    const source = readFileSync(new URL('./pages/AgentDetail.tsx', import.meta.url), 'utf8');
    const authority = readFileSync(
      new URL('./pages/agent-detail/useOperatorAuthorityLifecycle.ts', import.meta.url),
      'utf8',
    );
    const roles = readFileSync(new URL('./roles.ts', import.meta.url), 'utf8');

    // Management authority comes from the server-sent action capabilities,
    // never from a raw client-side role claim.
    expect(source).not.toContain("currentUser?.role === 'platform_admin'");
    expect(source).not.toContain("currentUser?.role === 'org_admin'");
    expect(authority).toContain('const canManage = !effectiveAgentAuthorityLost');
    expect(authority).toContain('action_capabilities?.can_manage === true');
    expect(source).toContain('if (effectiveAgentAuthorityLost)');
    // The scoped business-administrator session lane lives in the authority
    // hook and delegates the role × projection predicate to the shared
    // roles.ts helper — never a reimplemented copy that could drift; the
    // inventory loader never fabricates an operator reason for it.
    expect(authority).toContain('isScopedBusinessAdminForAgent(currentUser, agent)');
    expect(authority).not.toContain('isAdministratorRole(currentUserRole)');
    expect(roles).toContain('isAdministratorRole(user?.role)');
    expect(roles).toContain("action_capabilities?.can_manage_permissions === true");
    expect(authority).toContain('operatorAuthorityScopeRef.current');
    expect(source).toContain('createSessionInventoryLoader');
  });

  it('clears only a rejected selected company on cold start and never treats it as an expired login', () => {
    const source = readFileSync(new URL('./App.tsx', import.meta.url), 'utf8');

    // A stored selection the server rejects with a typed 400/403/404 clears
    // only current_tenant_id and re-validates the bearer; logout remains the
    // path for a genuine 401 or a selection-less failure.
    expect(source).toContain("localStorage.removeItem('current_tenant_id')");
    expect(source).toContain("localStorage.getItem('current_tenant_id')");
    expect(source).toContain('authApi.getMe()');
    expect(source).toContain('useAuthStore.getState().logout()');
  });

  it('grants Company Knowledge management to both scoped administrator roles', () => {
    const source = readFileSync(new URL('./pages/CompanyKnowledgeLibrary.tsx', import.meta.url), 'utf8');

    expect(source).toContain('canManage={isAdministratorRole(user?.role)}');
  });
});
