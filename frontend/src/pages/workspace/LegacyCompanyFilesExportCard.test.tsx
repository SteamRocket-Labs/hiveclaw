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
        loading={false}
        error={null}
        exporting={false}
        onExport={() => {}}
        onRetry={() => {}}
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

  it('renders a verified empty quarantine result instead of hiding it', () => {
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
        loading={false}
        error={null}
        exporting={false}
        onExport={() => {}}
        onRetry={() => {}}
      />,
    );

    expect(markup).toContain('No retired shared files');
    expect(markup).toContain('verified empty result');
    expect(markup).not.toContain('Export read-only archive');
  });

  it('distinguishes loading from an authoritative empty result', () => {
    const markup = renderToStaticMarkup(
      <LegacyCompanyFilesExportCard
        loading
        error={null}
        exporting={false}
        onExport={() => {}}
        onRetry={() => {}}
      />,
    );

    expect(markup).toContain('Checking retired shared files');
    expect(markup).toContain('aria-busy="true"');
    expect(markup).not.toContain('No retired shared files');
  });

  it('does not claim an empty result before an authoritative status exists', () => {
    const markup = renderToStaticMarkup(
      <LegacyCompanyFilesExportCard
        loading={false}
        error={null}
        exporting={false}
        onExport={() => {}}
        onRetry={() => {}}
      />,
    );

    expect(markup).toBe('');
  });

  it('distinguishes forbidden from empty without leaking server details and wires Retry', () => {
    const retry = vi.fn();
    const error = { status: 403, message: 'private tenant policy detail' };
    const markup = renderToStaticMarkup(
      <LegacyCompanyFilesExportCard
        loading={false}
        error={error}
        exporting={false}
        onExport={() => {}}
        onRetry={retry}
      />,
    );

    expect(markup).toContain('Retired shared files access denied');
    expect(markup).toContain('This is not an empty result');
    expect(markup).toContain('Retry');
    expect(markup).not.toContain('private tenant policy detail');

    const element = LegacyCompanyFilesExportCard({
      loading: false,
      error,
      exporting: false,
      onExport: () => {},
      onRetry: retry,
    }) as React.ReactElement;
    const retryButton = findButton(element, 'Retry');
    retryButton.props.onClick();
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it('distinguishes service and network failures from empty without leaking internals', () => {
    for (const error of [
      { status: 503, message: 'database connection string' },
      new Error('network stack trace'),
    ]) {
      const markup = renderToStaticMarkup(
        <LegacyCompanyFilesExportCard
          loading={false}
          error={error}
          exporting={false}
          onExport={() => {}}
          onRetry={() => {}}
        />,
      );

      expect(markup).toContain('Retired shared files are temporarily unavailable');
      expect(markup).toContain('No empty-state conclusion was made');
      expect(markup).toContain('Retry');
      expect(markup).not.toContain('database connection string');
      expect(markup).not.toContain('network stack trace');
    }
  });
});

function findButton(node: React.ReactNode, label: string): React.ReactElement<{ onClick: () => void }> {
  if (!React.isValidElement(node)) throw new Error(`Button ${label} not found`);
  const props = node.props as { children?: React.ReactNode };
  if (node.type === 'button' && props.children === label) {
    return node as React.ReactElement<{ onClick: () => void }>;
  }
  const children = React.Children.toArray(props.children);
  for (const child of children) {
    try {
      return findButton(child, label);
    } catch {
      // Continue searching the remaining descendants.
    }
  }
  throw new Error(`Button ${label} not found`);
}
