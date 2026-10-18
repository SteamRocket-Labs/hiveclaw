import { useTranslation } from 'react-i18next';
import type { BudgetIncreaseRequest, BudgetLimitName } from '../api/domains/runtimeBudgets';

export function useBudgetLimitLabels(): Record<BudgetLimitName, string> {
  const { t } = useTranslation();
  return {
    max_tokens: t('runtimeBudgets.totalTokenLimit', 'Total token limit'),
    max_cache_miss_tokens: t('runtimeBudgets.cacheMissLimit', 'Cache-miss token limit'),
    max_subagents: t('runtimeBudgets.maxSubagents', 'Max subagents'),
    max_team_sessions: t('runtimeBudgets.maxTeamSessions', 'Max team sessions'),
    max_delegations: t('teamBudgets.delegations', 'Delegations'),
    max_background_tasks: t('teamBudgets.backgroundTasks', 'Background tasks'),
    max_continuation_wakes: t('runtimeBudgets.maxWakes', 'Max wakes'),
    max_provider_calls: t('runtimeBudgets.maxProviderCalls', 'Max model calls'),
  };
}

export default function BudgetIncreaseRequestSummary({ request }: { request: BudgetIncreaseRequest }) {
  const { t } = useTranslation();
  const labels = useBudgetLimitLabels();
  const states = {
    pending: t('teamBudgets.pending', 'Awaiting administrator decision'),
    approved: t('teamBudgets.approved', 'Requested increase approved'),
    rejected: t('teamBudgets.rejected', 'Increase rejected and waiting work stopped'),
    withdrawn: t('teamBudgets.withdrawn', 'Request withdrawn'),
    superseded: t('teamBudgets.superseded', 'Run changed; review its current budget'),
  };
  return <div>
    <strong>{states[request.status]}</strong>
    <p>{request.requester_name || t('teamBudgets.formerMember', 'Former member')} · {request.reason}</p>
    <ul>{Object.entries(request.limits).map(([field, value]) => (
      <li key={field}>{labels[field as BudgetLimitName]}: {value?.toLocaleString()}</li>
    ))}</ul>
  </div>;
}
