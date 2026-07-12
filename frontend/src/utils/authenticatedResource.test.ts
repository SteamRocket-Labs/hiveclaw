import { describe, expect, it } from 'vitest';

import { apiPathFromBrowserUrl } from './authenticatedResource';

describe('apiPathFromBrowserUrl', () => {
  it('maps same-origin API resources to the authenticated request path', () => {
    expect(apiPathFromBrowserUrl('/api/agents/a/files/download?path=workspace%2Fr.md')).toBe(
      '/agents/a/files/download?path=workspace%2Fr.md',
    );
  });

  it('rejects cross-origin, non-API, and credential-bearing URLs', () => {
    expect(apiPathFromBrowserUrl('https://evil.example/api/agents/a/files')).toBeNull();
    expect(apiPathFromBrowserUrl('/assets/report.pdf')).toBeNull();
    expect(apiPathFromBrowserUrl('/api/agents/a/files/download?token=secret')).toBeNull();
    expect(apiPathFromBrowserUrl('/api/agents/a/files/download?access_token=secret')).toBeNull();
  });
});
