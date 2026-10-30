import { useTranslation } from 'react-i18next';

import type { SessionWorkbenchHeaderModel } from './timelineModel';
import './SessionWorkbenchChrome.css';

function statusColor(status: SessionWorkbenchHeaderModel['status'] | 'needs_reconciliation'): string {
  if (status === 'running' || status === 'streaming') return 'var(--accent-primary)';
  if (status === 'waiting' || status === 'needs_reconciliation') return 'var(--warning)';
  if (status === 'failed') return 'var(--error)';
  return 'var(--text-tertiary)';
}

export function SessionWorkbenchHeader({ model }: { model: SessionWorkbenchHeaderModel }) {
  const { t } = useTranslation();
  const displayStatus = model.requiresReconciliation ? 'needs_reconciliation' : model.status;
  const statusLabel = t(`sessionWorkbench.status.${displayStatus}`, displayStatus);
  const guidance = displayStatus === 'needs_reconciliation'
    ? t('sessionWorkbench.reconciliationGuidance', 'Execution outcome is unconfirmed. Review the original operation before continuing.')
    : displayStatus === 'waiting'
    ? t('sessionWorkbench.waitingGuidance', 'Review the request below and choose the next action.')
    : displayStatus === 'failed'
      ? t('sessionWorkbench.failedGuidance', 'Review the failed step below and its recovery instructions.')
      : null;
  return (
    <div data-testid="session-workbench-header" className="swb-header">
      <div className="swb-header-title-row">
        <h2 className="swb-header-title">{model.title}</h2>
        <span className="swb-header-status">
          <span className="swb-status-dot" style={{ background: statusColor(displayStatus) }} />
          <span>{statusLabel}</span>
          {model.modelLabel && <span>· {model.modelLabel}</span>}
        </span>
      </div>
      {guidance && <div className="swb-header-guidance" role="status">{guidance}</div>}
    </div>
  );
}
