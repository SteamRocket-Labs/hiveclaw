import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import LegacyCompanyFilesExportCard from './LegacyCompanyFilesExportCard';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string, values?: Record<string, string | number>) =>
      (fallback ?? '').replace('{{count}}', String(values?.count ?? '')).replace('{{size}}', String(values?.size ?? '')),
  }),
}));

describe('LegacyCompanyFilesExportCard', () => {
  it('shows only a read-only recovery action when legacy files exist', () => {
    const markup = renderToStaticMarkup(
      <LegacyCompanyFilesExportCard
        status={{
          available: true,
          file_count: 2,
          total_bytes: 1536,
          excluded_symlink_count: 0,
          read_only: true,
          retired: true,
          surface_kind: 'legacy_company_files_quarantine',
          company_kb_available: false,
          agent_consumable: false,
        }}
        exporting={false}
        onExport={() => {}}
      />,
    );

    expect(markup).toContain('Retired shared files');
    expect(markup).toContain('2 files · 1.5 KB');
    expect(markup).toContain('Export read-only archive');
    expect(markup).toContain('This is not a Company Knowledge Base');
    expect(markup).not.toContain('Upload');
    expect(markup).not.toContain('Edit');
    expect(markup).not.toContain('Delete');
  });

  it('does not add a product surface when no legacy files exist', () => {
    const markup = renderToStaticMarkup(
      <LegacyCompanyFilesExportCard
        status={{
          available: false,
          file_count: 0,
          total_bytes: 0,
          excluded_symlink_count: 0,
          read_only: true,
          retired: true,
          surface_kind: 'legacy_company_files_quarantine',
          company_kb_available: false,
          agent_consumable: false,
        }}
        exporting={false}
        onExport={() => {}}
      />,
    );

    expect(markup).toBe('');
  });
});
