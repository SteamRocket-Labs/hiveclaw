import { useTranslation } from 'react-i18next';

import type { LegacyCompanyFilesStatus } from '../../api/domains/enterprise';

interface LegacyCompanyFilesExportCardProps {
  status?: LegacyCompanyFilesStatus;
  loading: boolean;
  error: unknown;
  exporting: boolean;
  onExport: () => void;
  onRetry: () => void;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export default function LegacyCompanyFilesExportCard({
  status,
  loading,
  error,
  exporting,
  onExport,
  onRetry,
}: LegacyCompanyFilesExportCardProps) {
  const { t } = useTranslation();

  if (loading) {
    return (
      <div
        className="card ws-info-card ws-info-legacy-export is-loading"
        role="status"
        aria-live="polite"
        aria-busy="true"
        data-legacy-company-files-state="loading"
      >
        <div>
          <h3 className="ws-info-legacy-title">
            {t('enterprise.legacyCompanyFiles.loadingTitle', 'Checking retired shared files…')}
          </h3>
          <p className="ws-info-desc">
            {t(
              'enterprise.legacyCompanyFiles.loadingBody',
              'Reading the authoritative quarantine status for this company.',
            )}
          </p>
        </div>
      </div>
    );
  }

  if (error) {
    const forbidden = classifyLegacyCompanyFilesError(error) === 'forbidden';
    return (
      <div
        className={`card ws-info-card ws-info-legacy-export is-${forbidden ? 'forbidden' : 'unavailable'}`}
        role="alert"
        data-legacy-company-files-state={forbidden ? 'forbidden' : 'unavailable'}
      >
        <div>
          <h3 className="ws-info-legacy-title">
            {forbidden
              ? t('enterprise.legacyCompanyFiles.forbiddenTitle', 'Retired shared files access denied')
              : t(
                'enterprise.legacyCompanyFiles.unavailableTitle',
                'Retired shared files are temporarily unavailable',
              )}
          </h3>
          <p className="ws-info-desc">
            {forbidden
              ? t(
                'enterprise.legacyCompanyFiles.forbiddenBody',
                'This is not an empty result. Company administrator access is required to inspect or export the retired-file quarantine.',
              )
              : t(
                'enterprise.legacyCompanyFiles.unavailableBody',
                'The quarantine status could not be verified. No empty-state conclusion was made; retry to restore the authoritative result.',
              )}
          </p>
        </div>
        <button className="btn btn-secondary" type="button" onClick={onRetry}>
          {t('enterprise.legacyCompanyFiles.retry', 'Retry')}
        </button>
      </div>
    );
  }

  if (!status) return null;

  if (!status.available) {
    return (
      <div
        className="card ws-info-card ws-info-legacy-export is-empty"
        role="status"
        data-legacy-company-files-state="empty"
      >
        <div>
          <h3 className="ws-info-legacy-title">
            {t('enterprise.legacyCompanyFiles.emptyTitle', 'No retired shared files')}
          </h3>
          <p className="ws-info-desc">
            {t(
              'enterprise.legacyCompanyFiles.emptyBody',
              'This is a verified empty result for the retired shared-file quarantine, not a Company Knowledge Base status.',
            )}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="card ws-info-card ws-info-legacy-export" role="status">
      <div>
        <h3 className="ws-info-legacy-title">
          {t('enterprise.legacyCompanyFiles.title', 'Retired shared files')}
        </h3>
        <p className="ws-info-desc">
          {t(
            'enterprise.legacyCompanyFiles.description',
            'These files came from the retired shared-folder feature. This is not a Company Knowledge Base, and Agents cannot access it. Export the read-only archive before removing the legacy data.',
          )}
        </p>
        <span className="ws-info-legacy-meta">
          {t('enterprise.legacyCompanyFiles.summary', '{{count}} files · {{size}}', {
            count: status.file_count,
            size: formatBytes(status.total_bytes),
          })}
        </span>
      </div>
      <button className="btn btn-secondary" type="button" onClick={onExport} disabled={exporting}>
        {exporting
          ? t('enterprise.legacyCompanyFiles.exporting', 'Preparing archive…')
          : t('enterprise.legacyCompanyFiles.export', 'Export read-only archive')}
      </button>
    </div>
  );
}

type LegacyCompanyFilesErrorKind = 'forbidden' | 'unavailable';

function classifyLegacyCompanyFilesError(error: unknown): LegacyCompanyFilesErrorKind {
  if (
    error
    && typeof error === 'object'
    && 'status' in error
    && Number((error as { status?: unknown }).status) === 403
  ) {
    return 'forbidden';
  }
  return 'unavailable';
}
