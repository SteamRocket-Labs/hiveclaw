import { describe, expect, it } from 'vitest';

import {
  WORKSPACE_DEFAULT_PATH,
  WORKSPACE_LEGACY_REDIRECTS,
  WORKSPACE_SECTIONS,
  workspaceSectionsForRole,
} from './sections';

describe('workspace section routing', () => {
  it('uses the company workbench as the default workspace landing page', () => {
    expect(WORKSPACE_DEFAULT_PATH).toBe('/enterprise/dashboard');
  });

  it('defines stable enterprise subroutes for the main workspace sections', () => {
    expect(WORKSPACE_SECTIONS.map((section) => section.path)).toEqual([
      '/enterprise/dashboard',
      '/enterprise/info',
      '/enterprise/llm',
      '/enterprise/memory',
      '/enterprise/knowledge',
      '/enterprise/digital-employees',
      '/enterprise/hr',
      '/enterprise/extensions',
      '/enterprise/runtime-budgets',
      '/enterprise/quotas',
      '/enterprise/users',
      '/enterprise/org',
      '/enterprise/approvals',
      '/enterprise/audit',
      '/enterprise/action-guardrails',
      '/enterprise/invitations',
    ]);
  });

  it('keeps legacy workspace entry points redirected to the new subroutes', () => {
    expect(WORKSPACE_LEGACY_REDIRECTS).toEqual([
      { from: '/enterprise', to: '/enterprise/dashboard' },
      { from: '/invitations', to: '/enterprise/invitations' },
      { from: '/enterprise/tools', to: '/enterprise/extensions' },
      { from: '/enterprise/skills', to: '/enterprise/extensions' },
      { from: '/enterprise/subagents', to: '/enterprise/extensions' },
    ]);
  });

  it('gives platform administrators the same selected-company workspace sections as organization administrators', () => {
    // PDEC-013: inside the authenticated selected company a platform
    // administrator holds the complete business workspace; the server answers
    // with typed selection errors when no valid company is selected.
    expect(workspaceSectionsForRole('platform_admin')).toEqual(WORKSPACE_SECTIONS);
  });

  it('keeps every company workspace section for organization administrators', () => {
    expect(workspaceSectionsForRole('org_admin')).toEqual(WORKSPACE_SECTIONS);
    expect(workspaceSectionsForRole('member')).toEqual([]);
  });
});
