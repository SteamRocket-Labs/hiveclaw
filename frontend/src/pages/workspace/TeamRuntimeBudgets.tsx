import { useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { runtimeBudgetApi, type BudgetIncreaseInput, type BudgetLimitName, type TeamBudgetRun } from '../../api/domains/runtimeBudgets';
import BudgetIncreaseRequestSummary, { useBudgetLimitLabels } from '../../components/BudgetIncreaseRequestSummary';
import { useAuthStore } from '../../stores';

export default function TeamRuntimeBudgets({ teamId, tenantId }: { teamId: string; tenantId: string }) {
  const { t } = useTranslation();
  const labels = useBudgetLimitLabels();
  const userId = useAuthStore((state) => state.user?.id);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState(false);
  const commands = useRef(new Map<string, { signature: string; body: BudgetIncreaseInput }>());
  const query = useQuery({ queryKey: ['team-runtime-budgets', tenantId, userId, teamId, offset],
    queryFn: () => runtimeBudgetApi.listTeamRuns(teamId, offset), refetchInterval: 5000 });
  const request = useMutation({ mutationFn: ({ runId, body }: { runId: string; body: BudgetIncreaseInput }) =>
    runtimeBudgetApi.requestIncrease(teamId, runId, body), onSuccess: () => { setError(false); void query.refetch(); },
    onError: () => { setError(true); void query.refetch(); } });
  const withdraw = useMutation({ mutationFn: ({ runId, id }: { runId: string; id: string }) =>
    runtimeBudgetApi.withdrawIncrease(teamId, runId, id), onSuccess: (_result, { runId }) => {
      commands.current.delete(runId); setError(false); void query.refetch();
    }, onError: () => { setError(true); void query.refetch(); } });
  function statusLabel(value: string) {
    if (value === 'waiting_budget_approval') return t('teamBudgets.waiting', 'Waiting for budget approval');
    if (value === 'active') return t('teamBudgets.running', 'Running');
    if (value === 'resuming') return t('teamBudgets.resuming', 'Resuming');
    if (value === 'completed') return t('teamBudgets.completed', 'Completed');
    return t('teamBudgets.attention', 'Needs attention');
  }
  function submit(run: TeamBudgetRun, form: HTMLFormElement) {
    const fields = new FormData(form);
    const limits = Object.fromEntries(Object.keys(labels).filter((field) => String(fields.get(field) || '').trim())
      .map((field) => [field, Number(fields.get(field))]));
    if (!run.approval_episode_id || !Object.keys(limits).length
      || Object.values(limits).some((value) => !Number.isSafeInteger(value) || value < 0)) { setError(true); return; }
    const proposal = { approval_episode_id: run.approval_episode_id,
      limits, reason: String(fields.get('reason') || '') };
    const signature = JSON.stringify(proposal);
    let command = commands.current.get(run.id);
    if (!command || command.signature !== signature) {
      command = { signature, body: { ...proposal, request_id: crypto.randomUUID() } };
      commands.current.set(run.id, command);
    }
    request.mutate({ runId: run.id, body: command.body });
  }
  return <section aria-label={t('teamBudgets.title', 'Team runtime budgets')}>
    <h4>{t('teamBudgets.title', 'Team runtime budgets')}</h4>
    <p>{t('teamBudgets.boundary', 'Requests leave limits unchanged until an administrator decides. Company limits still apply; monetary cost is unavailable here.')}</p>
    {(error || query.isError) && <div role="alert">
      <p>{t('teamBudgets.confirmError', 'Could not confirm the change. Refresh to check the current run.')}</p>
      <button type="button" className="btn btn-ghost" onClick={() => { setError(false); void query.refetch(); }}>
        {t('common.refresh', 'Refresh')}
      </button>
    </div>}
    {query.isLoading && <p>{t('common.loading', 'Loading...')}</p>}
    {!query.isError && query.data?.runs.map((run) => <div key={run.id} className="card card-pad-sm">
      <strong>{run.agent_name}</strong> · {statusLabel(run.status)}
      <dl>{Object.entries(run.limits).map(([field, value]) => {
        const usage = run.usage[field.slice(4)];
        return <div key={field}><dt>{labels[field as BudgetLimitName]}</dt><dd>
          {t('teamBudgets.usage', '{{used}} used · {{reserved}} reserved · limit {{limit}}', {
            used: usage?.used.toLocaleString(), reserved: usage?.reserved.toLocaleString(),
            limit: value === null ? t('teamBudgets.noLimit', 'No run limit') : value.toLocaleString(),
          })}
        </dd></div>;
      })}</dl>
      {run.increase_requests.map((proposal) => <div key={proposal.id}>
        <BudgetIncreaseRequestSummary request={proposal} />
        {proposal.can_withdraw && <button type="button" className="btn btn-ghost" disabled={withdraw.isPending}
          onClick={() => withdraw.mutate({ runId: run.id, id: proposal.id })}>
          {t('teamBudgets.withdraw', 'Withdraw request')}
        </button>}
      </div>)}
      {run.status === 'waiting_budget_approval' && run.approval_episode_id
        && !run.increase_requests.some((proposal) => proposal.can_withdraw) && (Object.values(run.limits).some((value) => value !== null) ? <form onSubmit={(event) => {
          event.preventDefault(); submit(run, event.currentTarget);
        }}>
          <p>{t('teamBudgets.disclosure', 'Your request is visible to this team’s leads and company administrators.')}</p>
          <fieldset><legend>{t('teamBudgets.newLimits', 'Requested new limits')}</legend>
            <p>{t('teamBudgets.limitsHint', 'Enter each limit that needs an increase. Leave other limits blank.')}</p>
            {Object.entries(run.limits).filter(([, value]) => value !== null).map(([field, value]) => <label key={field}>
              {labels[field as BudgetLimitName]}<input className="form-input" name={field} type="number" min={value! + 1} step={1}
                max={['max_tokens', 'max_cache_miss_tokens'].includes(field) ? Number.MAX_SAFE_INTEGER : 2147483647} />
            </label>)}
          </fieldset>
          <label>{t('teamBudgets.reason', 'Reason')}<input className="form-input" name="reason" maxLength={1000} required /></label>
          <button type="submit" className="btn btn-secondary" disabled={request.isPending}>
            {t('teamBudgets.request', 'Request an increase')}
          </button>
        </form> : <p>{t('teamBudgets.adminReview', 'Ask an administrator to review this run’s other protection limits.')}</p>)}
    </div>)}
    {!query.isError && query.data?.runs.length === 0 && <p>{t('common.noData', 'No data')}</p>}
    {offset > 0 && <button type="button" className="btn btn-ghost" onClick={() => setOffset(Math.max(0, offset - 10))}>
      {t('teamBudgets.previous', 'Previous runs')}
    </button>}
    {!query.isError && query.data?.next_offset != null && <button type="button" className="btn btn-ghost"
      onClick={() => setOffset(query.data!.next_offset!)}>{t('teamBudgets.more', 'More runs')}</button>}
  </section>;
}
