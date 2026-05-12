import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import ToolIcon from './ToolIcon';

describe('ToolIcon', () => {
  it('renders dedicated Office action icons instead of the DOC text marker', () => {
    const markup = renderToStaticMarkup(
      <ToolIcon tool={{ name: 'office_document_create', category: 'office', icon: 'DOC' }} />,
    );

    expect(markup).toContain('data-tool-icon="office-create"');
    expect(markup).toContain('aria-label="Create Office document"');
    expect(markup).not.toContain('DOC');
  });

  it('renders distinct Deep Research action icons instead of the generic search emoji', () => {
    const markup = renderToStaticMarkup(
      <ToolIcon tool={{ name: 'deep_research_export', category: 'deep_research_pack', icon: '🔎' }} />,
    );

    expect(markup).toContain('data-tool-icon="deep-research-export"');
    expect(markup).toContain('aria-label="Export deep research"');
    expect(markup).not.toContain('🔎');
  });

  it('preserves existing icons for unrelated tools', () => {
    const markup = renderToStaticMarkup(
      <ToolIcon tool={{ name: 'read_file', category: 'filesystem', icon: '📄' }} />,
    );

    expect(markup).toContain('📄');
  });
});
