import { useTranslation } from 'react-i18next';

import type { LegacyCompanyFilesStatus } from '../../api/domains/enterprise';

interface LegacyCompanyFilesExportCardProps {
  status?: LegacyCompanyFilesStatus;
  exporting: boolean;
  onExport: () => void;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export default function LegacyCompanyFilesExportCard({
  status,
  exporting,
  onExport,
}: LegacyCompanyFilesExportCardProps) {
  const { t } = useTranslation();
  if (!status?.available) return null;

  return (
    <div className="card ws-info-card ws-info-legacy-export" role="status">
      <div>
        <h3 className="ws-info-legacy-title">
          {t('enterprise.legacyCompanyFiles.title', 'Retired shared files')}
        </h3>
        <p className="ws-info-desc">
          {t(
            'enterprise.legacyCompanyFiles.description',
            'These files came from the retired shared-folder feature. Agents cannot access them. Export the read-only archive before removing the legacy data.',
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
