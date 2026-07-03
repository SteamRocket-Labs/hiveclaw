/**
 * Plan Mode queue (§12.2) — the "before execution" view in the Aware/Autonomy tab.
 *
 * Lists an agent's plan requests grouped by status:
 *   - Awaiting confirmation (actionable — full PlanCard with Confirm/Revise/Reject)
 *   - Confirmed / handoff pending
 *   - Rejected / expired / superseded
 *
 * Existing Wake Policies / Triggers show the world *after* execution; this
 * queue shows the world *before* it. Self-fetching (like AgentApprovalsSection)
 * so the parent only mounts <PlanQueueSection agentId=… /> with no extra wiring.
 */

import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { planApi, type PlanRequest } from '../../api/domains/plans';
import PlanCard from './PlanCard';
import './PlanQueueSection.css';

type PlanQueueSectionProps = {
  agentId: string;
};

const TERMINAL_STATUSES = new Set(['rejected', 'expired', 'superseded']);

export default function PlanQueueSection({ agentId }: PlanQueueSectionProps) {
  const { t } = useTranslation();
  const { data: plans = [], refetch } = useQuery({
    queryKey: ['agent-plans', agentId],
    queryFn: () => planApi.list(agentId),
    enabled: !!agentId,
    refetchInterval: 15000,
  });

  const list = plans as PlanRequest[];
  const awaiting = list.filter((plan) => plan.status === 'awaiting_confirmation');
  const planning = list.filter((plan) => plan.status === 'planning' || plan.status === 'draft' || plan.status === 'planning_failed');
  const confirmed = list.filter((plan) => plan.status === 'confirmed');
  const closed = list.filter((plan) => TERMINAL_STATUSES.has(plan.status));

  const refresh = () => refetch();

  const renderClosedRow = (plan: PlanRequest) => (
    <div key={plan.id} className="plan-queue-row plan-queue-row--muted">
      <div className="plan-queue-row-body">
        <div className="plan-queue-row-title">
          {plan.plan_json?.title || plan.original_request}
        </div>
        <div className="plan-queue-row-meta">
          {t('agent.plan.version', 'v{{version}}', { version: plan.plan_version })}
          {' · '}
          {plan.rejected_at || plan.updated_at
            ? new Date(plan.rejected_at || plan.updated_at || '').toLocaleString(undefined, {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
              })
            : ''}
        </div>
      </div>
      <span className="plan-queue-status">
        {t(`agent.plan.status.${plan.status}`, String(plan.status).replace(/_/g, ' '))}
      </span>
    </div>
  );

  const renderConfirmedRow = (plan: PlanRequest) => (
    <div key={plan.id} className="plan-queue-row">
      <div className="plan-queue-row-body">
        <div className="plan-queue-row-title">
          {plan.plan_json?.title || plan.original_request}
        </div>
        <div className="plan-queue-row-meta">
          {t('agent.plan.handoffState', 'Handoff: {{state}}', {
            state: String(plan.handoff_status || 'not_started').replace(/_/g, ' '),
          })}
        </div>
      </div>
      <span className="plan-queue-status plan-queue-status--confirmed">
        {t('agent.plan.status.confirmed', 'confirmed')}
      </span>
    </div>
  );

  return (
    <div className="card plan-queue-card" data-testid="plan-queue-section">
      <div className="plan-queue-header">
        <div>
          <h4 className="plan-queue-title">{t('agent.plan.queueTitle', 'Plan Queue')}</h4>
          <span className="plan-queue-desc">
            {t('agent.plan.queueDesc', 'Plans awaiting your confirmation before autonomous work can begin.')}
          </span>
        </div>
        <span className="plan-queue-count">{list.length}</span>
      </div>

      {list.length === 0 && (
        <div className="plan-queue-empty">
          {t('agent.plan.queueEmpty', 'No plans yet. Plans appear here when the agent proposes autonomous work.')}
        </div>
      )}

      {awaiting.length > 0 && (
        <div className="plan-queue-group plan-queue-group--awaiting">
          <div className="plan-queue-group-label plan-queue-group-label--awaiting">
            {t('agent.plan.groupAwaiting', 'Awaiting confirmation')} · {awaiting.length}
          </div>
          {awaiting.map((plan) => (
            <PlanCard key={plan.id} agentId={agentId} plan={plan} onChanged={refresh} />
          ))}
        </div>
      )}

      {planning.length > 0 && (
        <div className="plan-queue-group">
          <div className="plan-queue-group-label">
            {t('agent.plan.groupPlanning', 'Planning')} · {planning.length}
          </div>
          {planning.map(renderClosedRow)}
        </div>
      )}

      {confirmed.length > 0 && (
        <div className="plan-queue-group">
          <div className="plan-queue-group-label plan-queue-group-label--confirmed">
            {t('agent.plan.groupConfirmed', 'Confirmed / handoff pending')} · {confirmed.length}
          </div>
          {confirmed.map(renderConfirmedRow)}
        </div>
      )}

      {closed.length > 0 && (
        <div className="plan-queue-group">
          <div className="plan-queue-group-label">
            {t('agent.plan.groupClosed', 'Rejected / expired')} · {closed.length}
          </div>
          {closed.map(renderClosedRow)}
        </div>
      )}
    </div>
  );
}
