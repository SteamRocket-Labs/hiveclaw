import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { FileVersionContent, FileVersionPage } from '../api/domains/files';
import FileVersionHistoryPanel, {
  buildFileVersionRestoreRequest,
} from './FileVersionHistoryPanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string | Record<string, unknown>) =>
      typeof fallback === 'string' ? fallback : _key,
  }),
}));

const page: FileVersionPage = {
  path: 'workspace/report.md',
  current: {
    exists: true,
    content_hash: 'a'.repeat(64),
    size: 128,
  },
  versions: [
    {
      version_id: 'opaque-version-1',
      created_at: '2026-07-24T08:00:00+00:00',
      state: 'available',
      size: 96,
      content_hash: 'b'.repeat(64),
      restorable: true,
    },
    {
      version_id: 'opaque-version-2',
      created_at: '2026-07-23T08:00:00+00:00',
      state: 'deleted',
      size: 0,
      content_hash: null,
      restorable: true,
    },
    {
      version_id: 'opaque-version-3',
      created_at: '2026-07-22T08:00:00+00:00',
      state: 'unavailable',
      size: 0,
      content_hash: null,
      restorable: false,
    },
  ],
  total: 3,
  offset: 0,
  limit: 20,
  has_more: false,
  coverage_complete: true,
};

const selected: FileVersionContent = {
  path: 'workspace/report.md',
  version_id: 'opaque-version-1',
  state: 'available',
  content: '<verified>previous content</verified>',
  content_hash: 'b'.repeat(64),
  size: 96,
  is_binary: false,
};

describe('FileVersionHistoryPanel', () => {
  it('renders business states and exact text without exposing internal ids or hashes', () => {
    const markup = renderToStaticMarkup(
      <FileVersionHistoryPanel
        path="workspace/report.md"
        page={page}
        selected={selected}
        selectedVersionId="opaque-version-1"
        onClose={vi.fn()}
        onRetry={vi.fn()}
        onSelect={vi.fn()}
        onRequestRestore={vi.fn()}
        onConfirmRestore={vi.fn()}
        onCancelRestore={vi.fn()}
        onLoadMore={vi.fn()}
        onDownload={vi.fn()}
      />,
    );

    expect(markup).toContain('Version history');
    expect(markup).toContain('Available');
    expect(markup).toContain('File absent at this checkpoint');
    expect(markup).toContain('Checkpoint unavailable');
    expect(markup).toContain('&lt;verified&gt;previous content&lt;/verified&gt;');
    expect(markup).not.toContain('opaque-version-1');
    expect(markup).not.toContain('aaaaaaaa');
    expect(markup).not.toContain('bbbbbbbb');
  });

  it('builds an exact-current-state restore request and renders explicit confirmation', () => {
    expect(buildFileVersionRestoreRequest(page)).toEqual({
      expected_current_exists: true,
      expected_current_hash: 'a'.repeat(64),
    });

    const markup = renderToStaticMarkup(
      <FileVersionHistoryPanel
        path="workspace/report.md"
        page={page}
        selected={selected}
        selectedVersionId="opaque-version-1"
        restoreCandidate={page.versions[0]}
        onClose={vi.fn()}
        onRetry={vi.fn()}
        onSelect={vi.fn()}
        onRequestRestore={vi.fn()}
        onConfirmRestore={vi.fn()}
        onCancelRestore={vi.fn()}
        onLoadMore={vi.fn()}
        onDownload={vi.fn()}
      />,
    );

    expect(markup).toContain('Restore this version?');
    expect(markup).toContain('The current file will be replaced only if it has not changed.');
    expect(markup).toContain('Confirm restore');
  });
});
