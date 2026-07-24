import { useTranslation } from 'react-i18next';

import type {
  FileVersionContent,
  FileVersionPage,
  FileVersionRestoreRequest,
  FileVersionSummary,
} from '../api/domains/files';
import './FileVersionHistoryPanel.css';

type FileVersionHistoryPanelProps = {
  path: string;
  page: FileVersionPage | null;
  selected?: FileVersionContent | null;
  selectedVersionId?: string | null;
  restoreCandidate?: FileVersionSummary | null;
  loading?: boolean;
  selectedLoading?: boolean;
  restoring?: boolean;
  error?: string | null;
  onClose: () => void;
  onRetry: () => void;
  onSelect: (version: FileVersionSummary) => void;
  onRequestRestore: (version: FileVersionSummary) => void;
  onConfirmRestore: () => void;
  onCancelRestore: () => void;
  onLoadMore: () => void;
  onDownload: (version: FileVersionSummary) => void;
};

export function buildFileVersionRestoreRequest(page: FileVersionPage): FileVersionRestoreRequest {
  return {
    expected_current_exists: page.current.exists,
    expected_current_hash: page.current.exists ? page.current.content_hash : null,
  };
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed);
}

export default function FileVersionHistoryPanel({
  path,
  page,
  selected = null,
  selectedVersionId = null,
  restoreCandidate = null,
  loading = false,
  selectedLoading = false,
  restoring = false,
  error = null,
  onClose,
  onRetry,
  onSelect,
  onRequestRestore,
  onConfirmRestore,
  onCancelRestore,
  onLoadMore,
  onDownload,
}: FileVersionHistoryPanelProps) {
  const { t } = useTranslation();
  const selectedSummary = page?.versions.find((version) => version.version_id === selectedVersionId);

  const stateLabel = (version: FileVersionSummary) => {
    if (version.state === 'deleted') {
      return t('agent.workspace.fileHistory.deleted', 'File absent at this checkpoint');
    }
    if (version.state === 'unavailable') {
      return t('agent.workspace.fileHistory.unavailable', 'Checkpoint unavailable');
    }
    return t('agent.workspace.fileHistory.available', 'Available');
  };

  return (
    <section className="file-version-panel" aria-label={t('agent.workspace.fileHistory.title', 'Version history')}>
      <header className="file-version-panel-header">
        <div>
          <h3>{t('agent.workspace.fileHistory.title', 'Version history')}</h3>
          <p>{path}</p>
        </div>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
          {t('common.close', 'Close')}
        </button>
      </header>

      {loading ? (
        <div className="file-version-panel-state">{t('common.loading', 'Loading…')}</div>
      ) : error ? (
        <div className="file-version-panel-state is-error" role="alert">
          <span>{error}</span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
            {t('common.retry', 'Retry')}
          </button>
        </div>
      ) : page?.versions.length ? (
        <div className="file-version-layout">
          <div className="file-version-list">
            {page.versions.map((version) => (
              <button
                type="button"
                key={version.version_id}
                className={`file-version-row${selectedVersionId === version.version_id ? ' is-selected' : ''}`}
                disabled={version.state === 'unavailable'}
                onClick={() => onSelect(version)}
              >
                <span className="file-version-row-time">{formatTimestamp(version.created_at)}</span>
                <span className={`file-version-row-state is-${version.state}`}>{stateLabel(version)}</span>
                {version.state === 'available' ? (
                  <span className="file-version-row-size">{formatBytes(version.size)}</span>
                ) : null}
              </button>
            ))}
            {page.has_more ? (
              <button type="button" className="btn btn-secondary btn-sm" onClick={onLoadMore}>
                {t('agent.workspace.fileHistory.loadMore', 'Load more versions')}
              </button>
            ) : null}
            {!page.coverage_complete ? (
              <p className="file-version-coverage-warning" role="status">
                {t(
                  'agent.workspace.fileHistory.coverageWarning',
                  'Older sessions were not scanned. Refine the owner scope before relying on this as complete history.',
                )}
              </p>
            ) : null}
          </div>

          <div className="file-version-preview">
            {selectedLoading ? (
              <div className="file-version-panel-state">{t('common.loading', 'Loading…')}</div>
            ) : selected?.state === 'deleted' ? (
              <div className="file-version-panel-state">
                {t('agent.workspace.fileHistory.deletedPreview', 'This file did not exist at this checkpoint.')}
              </div>
            ) : selected?.is_binary ? (
              <div className="file-version-panel-state">
                <span>{t('agent.workspace.fileHistory.binaryPreview', 'Binary versions are available by download.')}</span>
                {selectedSummary ? (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    onClick={() => onDownload(selectedSummary)}
                  >
                    {t('common.download', 'Download')}
                  </button>
                ) : null}
              </div>
            ) : selected ? (
              <pre className="file-version-preview-content">{selected.content || ''}</pre>
            ) : (
              <div className="file-version-panel-state">
                {t('agent.workspace.fileHistory.selectVersion', 'Select a checkpoint to inspect its content.')}
              </div>
            )}
            {selected && selectedSummary?.restorable ? (
              <button
                type="button"
                className="btn btn-primary file-version-restore-action"
                onClick={() => onRequestRestore(selectedSummary)}
              >
                {t('agent.workspace.fileHistory.restoreAction', 'Restore this version')}
              </button>
            ) : null}
          </div>
        </div>
      ) : (
        <div className="file-version-panel-state">
          {t('agent.workspace.fileHistory.empty', 'No durable checkpoints contain this file yet.')}
        </div>
      )}

      {restoreCandidate ? (
        <div className="ui-modal-overlay" role="presentation" onClick={onCancelRestore}>
          <div
            className="file-browser-modal file-browser-modal-sm"
            role="dialog"
            aria-modal="true"
            aria-label={t('agent.workspace.fileHistory.confirmTitle', 'Restore this version?')}
            onClick={(event) => event.stopPropagation()}
          >
            <h4>{t('agent.workspace.fileHistory.confirmTitle', 'Restore this version?')}</h4>
            <p>
              {t(
                'agent.workspace.fileHistory.confirmBody',
                'The current file will be replaced only if it has not changed.',
              )}
            </p>
            <div className="file-browser-modal-actions">
              <button type="button" className="btn btn-secondary" disabled={restoring} onClick={onCancelRestore}>
                {t('common.cancel', 'Cancel')}
              </button>
              <button type="button" className="btn btn-primary" disabled={restoring} onClick={onConfirmRestore}>
                {restoring
                  ? t('agent.workspace.fileHistory.restoring', 'Restoring…')
                  : t('agent.workspace.fileHistory.confirmAction', 'Confirm restore')}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
