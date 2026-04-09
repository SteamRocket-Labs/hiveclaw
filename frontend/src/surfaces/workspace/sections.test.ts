import { describe, expect, it } from 'vitest';

import {
  WORKSPACE_DEFAULT_PATH,
  WORKSPACE_LEGACY_REDIRECTS,
  WORKSPACE_SECTIONS,
} from './sections';

describe('workspace section routing', () => {
  it('uses company info as the default workspace landing page', () => {
    expect(WORKSPACE_DEFAULT_PATH).toBe('/enterprise/info');
  });

  it('defines stable enterprise subroutes for the main workspace sections', () => {
    expect(WORKSPACE_SECTIONS.map((section) => section.path)).toEqual([
      '/enterprise/info',
      '/enterprise/llm',
      '/enterprise/memory',
      '/enterprise/hr',
      '/enterprise/tools',
      '/enterprise/skills',
      '/enterprise/quotas',
      '/enterprise/users',
      '/enterprise/org',
      '/enterprise/approvals',
      '/enterprise/audit',
      '/enterprise/invitations',
    ]);
  });

  it('keeps legacy workspace entry points redirected to the new subroutes', () => {
    expect(WORKSPACE_LEGACY_REDIRECTS).toEqual([
      { from: '/enterprise', to: '/enterprise/info' },
      { from: '/invitations', to: '/enterprise/invitations' },
    ]);
  });
});
