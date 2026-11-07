import { describe, expect, it } from 'vitest';

import {
  WORKSPACE_DEFAULT_PATH,
  WORKSPACE_LEGACY_REDIRECTS,
  WORKSPACE_SECTIONS,
  workspaceNavItemsForRole,
  workspaceSectionsForRole,
} from './sections';

describe('workspace section routing', () => {
  it('lands company admin on Members & Login instead of the dashboard (B4)', () => {
    expect(WORKSPACE_DEFAULT_PATH).toBe('/enterprise/users');
  });

  it('shows the six product areas in product order for administrators only', () => {
    const expectedPaths = [
      '/enterprise/users',
      '/enterprise/digital-employees',
      '/enterprise/llm',
      '/enterprise/info',
      '/enterprise/approvals',
      '/enterprise/audit',
    ];
    expect(workspaceNavItemsForRole('org_admin').map((item) => item.to)).toEqual(expectedPaths);
    expect(workspaceNavItemsForRole('platform_admin').map((item) => item.to)).toEqual(expectedPaths);
    expect(workspaceNavItemsForRole('member')).toEqual([]);
    expect(workspaceNavItemsForRole(undefined)).toEqual([]);
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
      { from: '/enterprise', to: '/enterprise/users' },
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
