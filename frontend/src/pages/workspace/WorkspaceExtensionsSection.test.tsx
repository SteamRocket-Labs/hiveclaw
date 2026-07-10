import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceExtensionsSection from './WorkspaceExtensionsSection';

vi.mock('./WorkspaceExtensionCatalogSection', () => ({ default: () => null }));
vi.mock('./WorkspaceAIAssetsSection', () => ({ default: () => <div data-testid="ai-assets" /> }));
vi.mock('./WorkspaceToolsSection', () => ({ default: () => null }));
vi.mock('./WorkspaceSkillsSection', () => ({ default: () => null }));
vi.mock('./WorkspaceSubagentsSection', () => ({ default: () => null }));
vi.mock('./WorkspaceCapabilityFactorsSection', () => ({ default: () => null }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key.split('.').pop() ?? key,
  }),
}));

describe('WorkspaceExtensionsSection', () => {
  it('renders one workspace-level extension entry with AI assets as the default subview', () => {
    const html = renderToStaticMarkup(<WorkspaceExtensionsSection selectedTenantId="tenant-1" />);

    expect(html).toContain('data-testid="workspace-extensions-section"');
    expect(html).toContain('AI Assets');
    expect(html).toContain('Catalog');
    expect(html).toContain('MCP &amp; Plugins');
    expect(html).toContain('Skills');
    expect(html).toContain('Sub-agents');
    expect(html).toContain('Factor Intake');
    expect(html).toContain('data-testid="workspace-extensions-assets-view"');
  });
});
