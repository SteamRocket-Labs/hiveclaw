import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { McpToolTrustBadge } from './McpToolTrustBadge';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

describe('McpToolTrustBadge', () => {
  it('shows the safe trust state without exposing administrator raw metadata', () => {
    const html = renderToStaticMarkup(
      <McpToolTrustBadge
        trustStatus="pending_review"
        trustTier="external_unreviewed"
        runtimeApproved={false}
      />,
    );

    expect(html).toContain('Metadata review required');
    expect(html).toContain('pending_review');
    expect(html).toContain('external_unreviewed');
    expect(html).not.toContain('raw_description');
    expect(html).not.toContain('raw_schema');
  });
});
