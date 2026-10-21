import { useTranslation } from 'react-i18next';

export default function RoutineWorkStatus({ work }: {
  work?: { status: string; work_url?: string; execution_target?: string; usage_status?: string } | null;
}) {
  const { t } = useTranslation();
  if (!work) return null;
  const labels: Record<string, string> = {
    queued: t('agent.aware.workQueued'),
    starting: t('agent.aware.workStarting'),
    running: t('agent.aware.workRunning'),
    awaiting_user_clarification: t('agent.aware.workWaitingInput'),
    completed: t('agent.aware.workCompleted'),
    failed: t('agent.aware.workFailed'),
    killed: t('agent.aware.workCancelled'),
    needs_reconciliation: t('agent.aware.workNeedsReconciliation'),
    delivered: t('agent.aware.workDispatched'),
    waiting_approval: t('agent.aware.workWaitingApproval'),
    unknown: t('agent.aware.workUnknown'),
  };
  return (
    <div className="agent-aware-trigger-summary">
      <span>{t('agent.aware.workExecution')}: {labels[work.status] || t('agent.aware.workUnavailable')}</span>
      {work.work_url && work.status !== 'unavailable' && (
        <> <a className="btn btn-ghost btn-sm" href={work.work_url}>{t('agent.aware.openWork')}</a></>
      )}
      {work.execution_target === 'local_agent' && work.usage_status === 'unknown' && (
        <span>{t('agent.aware.localUsageUnknown')}</span>
      )}
    </div>
  );
}
