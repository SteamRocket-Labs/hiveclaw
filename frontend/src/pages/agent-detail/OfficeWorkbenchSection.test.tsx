import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import OfficeWorkbenchSection from './OfficeWorkbenchSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({
    data: {
      enabled: false,
      reason: 'onlyoffice_not_configured',
      required_env: ['ONLYOFFICE_DOCS_URL', 'ONLYOFFICE_JWT_SECRET'],
    },
    isFetching: false,
  }),
  useMutation: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

describe('OfficeWorkbenchSection', () => {
  it('renders a disabled state when ONLYOFFICE is not configured', () => {
    const markup = renderToStaticMarkup(<OfficeWorkbenchSection agentId="agent-1" />);

    expect(markup).toContain('Office');
    expect(markup).toContain('ONLYOFFICE is not configured');
    expect(markup).toContain('ONLYOFFICE_DOCS_URL');
    expect(markup).toContain('workspace/demo.docx');
    expect(markup).toContain('Save');
  });
});
