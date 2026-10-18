import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { employeeReadinessApi, type EmployeeReadinessCheck } from '../api/domains/employeeReadiness';

const active = (status?: string) => ['pending', 'running', 'resumable', 'suspended'].includes(status || '');

export default function EmployeeReadinessControls({ agentId, teamId, scopeKey }: { agentId: string; teamId?: string; scopeKey: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [checkId, setCheckId] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState<EmployeeReadinessCheck | null>(null);
  const [cancelled, setCancelled] = useState<string | null>(null);
  const command = useRef<string | null>(null);
  const query = useQuery({ queryKey: ['employee-readiness', scopeKey, agentId, teamId, checkId],
    queryFn: () => checkId ? employeeReadinessApi.get(agentId, checkId, teamId) : employeeReadinessApi.latest(agentId, teamId),
    enabled: open, refetchInterval: (value) => active(value.state.data?.execution_status) ? 1000 : false });
  const start = useMutation({ mutationFn: (id: string) => employeeReadinessApi.start(agentId, id, teamId),
    onSuccess: (receipt) => { setAcknowledged(receipt); setCheckId(receipt.id); command.current = null; setCancelled(null); void query.refetch(); },
    onError: () => { void query.refetch(); } });
  const cancel = useMutation({ mutationFn: (id: string) => employeeReadinessApi.cancel(agentId, id, teamId),
    onSuccess: (receipt) => { setCancelled(receipt.execution_status); start.reset(); void query.refetch(); } });
  const labels: Record<string, string> = {
    identity: t('employeeReadiness.identity', 'Current identity and company'),
    worker: t('employeeReadiness.worker', 'Native worker executed this check'),
    storage: t('employeeReadiness.storage', 'Workspace write and read'),
    model_configuration: t('employeeReadiness.model', 'Approved model configuration'),
    provider_inference: t('employeeReadiness.provider', 'Model provider response'),
    capability_effects: t('employeeReadiness.effects', 'Exact tool and connection permissions'),
    external_runtime: t('employeeReadiness.external', 'Connected external runtime'),
  };
  const states = {
    passed: t('employeeReadiness.passed', 'Verified'), blocked: t('employeeReadiness.blocked', 'Needs attention'),
    unverified: t('employeeReadiness.unverified', 'Not verified by this check'), not_applicable: t('employeeReadiness.notApplicable', 'Not applicable'),
  };
  useEffect(() => {
    if (start.isError && !query.isError && command.current && query.data?.request_id === command.current) {
      setAcknowledged(query.data); setCheckId(query.data.id); command.current = null; start.reset();
    }
  }, [query.data, query.isError, start.isError, start.reset]);
  const current = query.data || acknowledged;
  const receipt = query.isError || (command.current && current?.request_id !== command.current) ? null : current;
  const knownStatus = cancelled || current?.execution_status;
  function executionLabel(status: string) {
    if (status === 'pending' || status === 'resumable' || status === 'suspended') return t('employeeReadiness.queued', 'Waiting for a worker; an administrator may need to check worker capacity.');
    if (status === 'running') return t('employeeReadiness.running', 'A worker is checking.');
    if (status === 'completed') return t('employeeReadiness.recorded', 'Checks recorded. Review each result.');
    if (status === 'killed') return t('employeeReadiness.cancelled', 'Check cancelled.');
    return t('employeeReadiness.failed', 'The check could not finish. Refresh your access and run a new check.');
  }
  return <div>
    <button type="button" className="btn btn-ghost" aria-expanded={open} onClick={() => setOpen(!open)}>{t('employeeReadiness.title', 'Employee readiness')}</button>
    {open && <section aria-label={t('employeeReadiness.title', 'Employee readiness')}>
      <p>{t('employeeReadiness.boundary', 'This records native platform checks without calling a model or external application. Provider availability and each consequential effect still require their real execution checks. You can continue other authorized work.')}</p>
      {(query.isError || start.isError || cancel.isError) && <div role="alert">
        <p>{t('employeeReadiness.unknown', 'Could not confirm the current check. Refresh or retry the same request.')}</p>
        <button type="button" className="btn btn-ghost" onClick={() => { void query.refetch(); cancel.reset(); }}>{t('common.refresh', 'Refresh')}</button>
      </div>}
      {receipt && <>
        {!cancelled && <p role="status">{executionLabel(receipt.execution_status)}</p>}
        {receipt.observed_at && <p>{t('employeeReadiness.observed', 'Observed at {{time}}. Run again after configuration changes.', { time: new Date(receipt.observed_at).toLocaleString() })}</p>}
        <dl>{receipt.checks.map((check) => <div key={check.key}><dt>{labels[check.key]}</dt><dd>
          {states[check.status]}{check.model_label ? ` · ${check.model_label}` : ''}
          {check.key === 'identity' && check.status === 'blocked' && <p>{t('employeeReadiness.fixIdentity', 'Ask the company administrator to review the employee’s active identity and responsibility.')}</p>}
          {check.key === 'model_configuration' && check.status === 'blocked' && <p>{t('employeeReadiness.fixModel', 'Ask the employee owner or company administrator to select an enabled approved model.')}</p>}
          {check.key === 'storage' && check.status === 'blocked' && <p>{t('employeeReadiness.fixStorage', 'Ask the administrator to restore the employee workspace, then run the check again.')}</p>}
        </dd></div>)}</dl>
      </>}
      {cancelled && <p role="status">{cancelled === 'killed' ? executionLabel(cancelled) : t('employeeReadiness.alreadyFinished', 'The check already finished. Refresh your access to view its results.')}</p>}
      <button type="button" className="btn btn-secondary" disabled={start.isPending || (!command.current && active(knownStatus))}
        onClick={() => {
          if (!command.current) { command.current = crypto.randomUUID(); setCheckId(null); setAcknowledged(null); setCancelled(null); }
          start.mutate(command.current);
        }}>
        {command.current ? t('employeeReadiness.retry', 'Retry this check request') : t('employeeReadiness.run', 'Run a new check')}
      </button>
      {(checkId || receipt?.id) && (active(receipt?.execution_status) || query.isError) && <button type="button" className="btn btn-ghost" disabled={cancel.isPending}
        onClick={() => cancel.mutate(checkId || receipt!.id)}>{t('employeeReadiness.cancel', 'Cancel my check')}</button>}
    </section>}
  </div>;
}
