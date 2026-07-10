import { useTranslation } from 'react-i18next';

import type { SessionWorkbenchHeaderModel } from './timelineModel';
import './SessionWorkbenchChrome.css';

function statusColor(status: SessionWorkbenchHeaderModel['status']): string {
  if (status === 'running' || status === 'streaming') return 'var(--accent-primary)';
  if (status === 'waiting') return 'var(--warning)';
  if (status === 'failed') return 'var(--error)';
  return 'var(--text-tertiary)';
}

export function SessionWorkbenchHeader({ model }: { model: SessionWorkbenchHeaderModel }) {
  const { t } = useTranslation();
  const statusLabel = t(`sessionWorkbench.status.${model.status}`, model.status);
  const guidance = model.status === 'waiting'
    ? t('sessionWorkbench.waitingGuidance', 'Review the request below and choose the next action.')
    : model.status === 'failed'
      ? t('sessionWorkbench.failedGuidance', 'Review the failed step below to retry or inspect details.')
      : null;
  return (
    <div data-testid="session-workbench-header" className="swb-header">
      <div className="swb-header-title-row">
        <h2 className="swb-header-title">{model.title}</h2>
        <span className="swb-header-status">
          <span className="swb-status-dot" style={{ background: statusColor(model.status) }} />
          <span>{statusLabel}</span>
          {model.modelLabel && <span>· {model.modelLabel}</span>}
        </span>
      </div>
      {guidance && <div className="swb-header-guidance" role="status">{guidance}</div>}
    </div>
  );
}
