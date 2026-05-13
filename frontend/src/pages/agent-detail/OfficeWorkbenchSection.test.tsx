import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { OfficeEditorConfig } from '../../api/domains/office';
import OfficeWorkbenchSection from './OfficeWorkbenchSection';

let mockQueryState: {
  data?: OfficeEditorConfig;
  isFetching: boolean;
  isError: boolean;
  error: Error | null;
} = {
  data: {
    enabled: false,
    reason: 'onlyoffice_not_configured',
    required_env: ['ONLYOFFICE_DOCS_URL', 'ONLYOFFICE_JWT_SECRET'],
  },
  isFetching: false,
  isError: false,
  error: null,
};
let lastUseQueryOptions: { enabled?: boolean } | null = null;

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { enabled?: boolean }) => {
    lastUseQueryOptions = options;
    return mockQueryState;
  },
  useMutation: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

describe('OfficeWorkbenchSection', () => {
  it('does not auto-fetch the demo document before the user opens or creates it', () => {
    mockQueryState = {
      data: undefined,
      isFetching: false,
      isError: false,
      error: null,
    };
    lastUseQueryOptions = {};

    renderToStaticMarkup(<OfficeWorkbenchSection agentId="agent-1" />);

    expect(lastUseQueryOptions.enabled).toBe(false);
  });

  it('renders a disabled state when ONLYOFFICE is not configured', () => {
    mockQueryState = {
      data: {
        enabled: false,
        reason: 'onlyoffice_not_configured',
        required_env: ['ONLYOFFICE_DOCS_URL', 'ONLYOFFICE_JWT_SECRET'],
      },
      isFetching: false,
      isError: false,
      error: null,
    };

    const markup = renderToStaticMarkup(<OfficeWorkbenchSection agentId="agent-1" />);

    expect(markup).toContain('Office');
    expect(markup).toContain('ONLYOFFICE is not configured');
    expect(markup).toContain('ONLYOFFICE_DOCS_URL');
    expect(markup).toContain('workspace/demo.docx');
    expect(markup).toContain('Save');
  });

  it('renders ONLYOFFICE in a full-width editor shell when config is enabled', () => {
    mockQueryState = {
      data: {
        enabled: true,
        documentServerUrl: 'https://docs.example.com',
        config: {
          document: {
            key: 'doc-key',
            title: 'demo.docx',
          },
        },
      },
      isFetching: false,
      isError: false,
      error: null,
    };

    const markup = renderToStaticMarkup(<OfficeWorkbenchSection agentId="agent-1" />);

    expect(markup).toContain('class="office-workbench__editorShell"');
    expect(markup).toContain('height:clamp(560px, calc(100vh - 360px), 820px)');
    expect(markup).toContain('id="onlyoffice-editor-doc-key"');
    expect(markup).not.toContain('grid-template-columns:minmax(240px, 360px) minmax(0, 1fr)');
  });

  it('shows a document missing state instead of a broken editor on 404', () => {
    mockQueryState = {
      data: undefined,
      isFetching: false,
      isError: true,
      error: new Error('Office document not found'),
    };

    const markup = renderToStaticMarkup(<OfficeWorkbenchSection agentId="agent-1" />);

    expect(markup).toContain('Office document not found');
    expect(markup).toContain('Create it first, then open it again.');
    expect(markup).not.toContain('onlyoffice-editor-');
  });
});
