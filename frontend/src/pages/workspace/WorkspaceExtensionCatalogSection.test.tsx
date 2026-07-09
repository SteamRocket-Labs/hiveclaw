import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { type LegacyPackMigrationReport } from '../../api/domains/extensions';
import {
  LegacyPackMigrationPanel,
  MARKETPLACE_SOURCE_TYPE_OPTIONS,
  marketplaceSourceDefaults,
} from './WorkspaceExtensionCatalogSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

describe('LegacyPackMigrationPanel', () => {
  it('renders migration-only counts without presenting legacy packs as runtime entries', () => {
    const report: LegacyPackMigrationReport = {
      migration_only: true,
      blocks_new_entrypoint: true,
      runtime_writes: [],
      counts: {
        plugins: 3,
        assignments: 5,
        enabled_assignments: 4,
      },
    };

    const html = renderToStaticMarkup(
      <LegacyPackMigrationPanel report={report} running={false} onDryRun={vi.fn()} />,
    );

    expect(html).toContain('Legacy migration dry-run');
    expect(html).toContain('migration-only');
    expect(html).toContain('Plugins');
    expect(html).toContain('3');
    expect(html).toContain('Assignments');
    expect(html).toContain('5');
    expect(html).toContain('Runtime writes');
    expect(html).toContain('0');
  });
});

describe('marketplace source type defaults', () => {
  it('exposes remote marketplace source types without forcing manual-only config', () => {
    expect(MARKETPLACE_SOURCE_TYPE_OPTIONS.map((option) => option.value)).toEqual([
      'manual',
      'github',
      'cc_marketplace',
      'codex_marketplace',
    ]);

    expect(marketplaceSourceDefaults('manual').uri).toBe('manual://workspace');
    expect(marketplaceSourceDefaults('github').uri).toBe('https://raw.githubusercontent.com/org/repo/main/marketplace.json');
    expect(marketplaceSourceDefaults('cc_marketplace').config).toContain('marketplace.json');
    expect(marketplaceSourceDefaults('codex_marketplace').config).toContain('marketplace.json');
  });
});
