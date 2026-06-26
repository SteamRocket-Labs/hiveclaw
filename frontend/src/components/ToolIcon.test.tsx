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

  it('preserves existing icons for unrelated tools', () => {
    const markup = renderToStaticMarkup(
      <ToolIcon tool={{ name: 'read_file', category: 'filesystem', icon: '📄' }} />,
    );

    expect(markup).toContain('📄');
  });
});
