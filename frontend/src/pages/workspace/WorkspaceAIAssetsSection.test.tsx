import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { AIAssetDetailPanel } from './WorkspaceAIAssetsSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key.split('.').pop() ?? key,
  }),
}));

describe('AIAssetDetailPanel', () => {
  it('renders authority, trust, dependencies, usage evidence, diffs, and rollback actions', () => {
    const html = renderToStaticMarkup(
      <AIAssetDetailPanel
        detail={{
          asset: {
            id: 'asset-1', tenant_id: 'tenant-1', asset_type: 'workflow', native_entity_id: 'native-1',
            native_key: 'workflow:deploy', display_name: 'Deploy', owner: { type: 'agent', id: 'agent-1' },
            visibility_scope: 'tenant', lifecycle_status: 'active', active_revision_id: 'rev-3', content_hash: 'abc',
            source: { type: 'workflow_registry', ref: 'workflow:deploy@3' }, trust_state: 'trusted',
            dependencies: ['skill:review'], compatibility: { runtime: 'v1' }, admission_state: 'admitted',
            quarantine_reason: null, usage: { count: 2, last_used_at: null, evidence: [{ span_id: 'span-1' }] },
            projection: { status: 'applied', error: null }, created_at: null, updated_at: null,
          },
          active_revision: { version: 3, id: 'rev-3', content_hash: 'abc', diff_from_prev: { set: { control: { status: 'active' } }, removed: [] }, change_source: 'publish', changed_by_user_id: 'user-1', changed_by_agent_id: null, change_message: 'published', is_active: true, parent_revision_id: 'rev-2', rollback_of_revision_id: null, created_at: null },
          history: [
            { version: 3, id: 'rev-3', content_hash: 'abc', diff_from_prev: { set: { control: { status: 'active' } }, removed: [] }, change_source: 'publish', changed_by_user_id: 'user-1', changed_by_agent_id: null, change_message: 'published', is_active: true, parent_revision_id: 'rev-2', rollback_of_revision_id: null, created_at: null },
            { version: 2, id: 'rev-2', content_hash: 'def', diff_from_prev: { set: {}, removed: ['legacy'] }, change_source: 'update', changed_by_user_id: 'user-1', changed_by_agent_id: null, change_message: 'updated', is_active: false, parent_revision_id: 'rev-1', rollback_of_revision_id: null, created_at: null },
          ],
        }}
        busy={false}
        onRollback={() => undefined}
        onReconcile={() => undefined}
      />,
    );

    expect(html).toContain('workflow:deploy');
    expect(html).toContain('trusted');
    expect(html).toContain('skill:review');
    expect(html).toContain('span-1');
    expect(html).toContain('legacy');
    expect(html).toContain('Rollback to v2');
    expect(html).toContain('Reconcile');
  });
});
