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
      '<Route path="knowledge" element={<OrgAdminGuard><CompanyKnowledgeControlPlane /></OrgAdminGuard>} />',
    );
  });

  it('keeps platform administrators out of the company Plaza surface', () => {
    const source = readFileSync(new URL('./App.tsx', import.meta.url), 'utf8');

    expect(source).toContain('<Route path="plaza" element={<CompanyMemberGuard><Plaza /></CompanyMemberGuard>} />');
  });

  it('requires an Agent action capability and a live authority shell before enabling management', () => {
    const source = readFileSync(new URL('./pages/AgentDetail.tsx', import.meta.url), 'utf8');
    const authority = readFileSync(
      new URL('./pages/agent-detail/useOperatorAuthorityLifecycle.ts', import.meta.url),
      'utf8',
    );

    expect(source).not.toContain("currentUser?.role === 'platform_admin' || currentUser?.role === 'org_admin'");
    expect(authority).toContain('const canManage = !effectiveAgentAuthorityLost');
    expect(authority).toContain('action_capabilities?.can_manage === true');
    expect(source).toContain('if (effectiveAgentAuthorityLost)');
  });

  it('does not advertise Company Knowledge management to a platform-only principal', () => {
    const source = readFileSync(new URL('./pages/CompanyKnowledgeLibrary.tsx', import.meta.url), 'utf8');

    expect(source).toContain("canManage={user?.role === 'org_admin'}");
  });
});
