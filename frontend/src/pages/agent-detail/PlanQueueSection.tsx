/**
 * Plan Mode queue (§12.2) — the "before execution" view in the Aware/Autonomy tab.
 *
 * Lists an agent's plan requests grouped by status:
 *   - Awaiting confirmation (actionable — full PlanCard with Confirm/Revise/Reject)
 *   - Confirmed / handoff pending
 *   - Rejected / expired / superseded
 *
 * Existing Objectives + Wake Policies show the world *after* execution; this
 * queue shows the world *before* it. Self-fetching (like AgentApprovalsSection)
 * so the parent only mounts <PlanQueueSection agentId=… /> with no extra wiring.
 */

import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { planApi, type PlanRequest } from '../../api/domains/plans';
import PlanCard from './PlanCard';

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
    <div
      key={plan.id}
      style={{
        border: '1px solid var(--border-subtle)',
        borderRadius: '8px',
        padding: '10px 12px',
        background: 'var(--bg-primary)',
        opacity: 0.7,
        display: 'flex',
        justifyContent: 'space-between',
        gap: '12px',
        alignItems: 'center',
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--text-primary)' }}>
          {plan.plan_json?.title || plan.original_request}
        </div>
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
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
      <span
        style={{
          fontSize: '11px',
          color: 'var(--text-tertiary)',
          background: 'var(--bg-secondary)',
          border: '1px solid var(--border-subtle)',
          borderRadius: '6px',
          padding: '2px 7px',
          whiteSpace: 'nowrap',
        }}
      >
        {t(`agent.plan.status.${plan.status}`, String(plan.status).replace(/_/g, ' '))}
      </span>
    </div>
  );

  const renderConfirmedRow = (plan: PlanRequest) => (
    <div
      key={plan.id}
      style={{
        border: '1px solid var(--border-subtle)',
        borderRadius: '8px',
        padding: '10px 12px',
        background: 'var(--bg-primary)',
        display: 'flex',
        justifyContent: 'space-between',
        gap: '12px',
        alignItems: 'center',
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--text-primary)' }}>
          {plan.plan_json?.title || plan.original_request}
        </div>
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
          {t('agent.plan.handoffState', 'Handoff: {{state}}', {
            state: String(plan.handoff_status || 'not_started').replace(/_/g, ' '),
          })}
        </div>
      </div>
      <span
        style={{
          fontSize: '11px',
          color: 'var(--success, #059669)',
          background: 'var(--bg-secondary)',
          border: '1px solid var(--border-subtle)',
          borderRadius: '6px',
          padding: '2px 7px',
          whiteSpace: 'nowrap',
        }}
      >
        {t('agent.plan.status.confirmed', 'confirmed')}
      </span>
    </div>
  );

  return (
    <div className="card" style={{ marginBottom: '16px', padding: '16px' }} data-testid="plan-queue-section">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
        <div>
          <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>{t('agent.plan.queueTitle', 'Plan Queue')}</h4>
          <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
            {t('agent.plan.queueDesc', 'Plans awaiting your confirmation before autonomous work can begin.')}
          </span>
        </div>
        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{list.length}</span>
      </div>

      {list.length === 0 && (
        <div
          style={{
            padding: '24px',
            textAlign: 'center',
            color: 'var(--text-tertiary)',
            border: '1px dashed var(--border-subtle)',
            borderRadius: '8px',
            fontSize: '13px',
          }}
        >
          {t('agent.plan.queueEmpty', 'No plans yet. Plans appear here when the agent proposes autonomous work.')}
        </div>
      )}

      {awaiting.length > 0 && (
        <div style={{ display: 'grid', gap: '10px', marginBottom: closed.length || confirmed.length || planning.length ? '16px' : 0 }}>
          <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--warning, #b45309)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>
            {t('agent.plan.groupAwaiting', 'Awaiting confirmation')} · {awaiting.length}
          </div>
          {awaiting.map((plan) => (
            <PlanCard key={plan.id} agentId={agentId} plan={plan} onChanged={refresh} />
          ))}
        </div>
      )}

      {planning.length > 0 && (
        <div style={{ display: 'grid', gap: '6px', marginBottom: closed.length || confirmed.length ? '16px' : 0 }}>
          <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>
            {t('agent.plan.groupPlanning', 'Planning')} · {planning.length}
          </div>
          {planning.map(renderClosedRow)}
        </div>
      )}

      {confirmed.length > 0 && (
        <div style={{ display: 'grid', gap: '6px', marginBottom: closed.length ? '16px' : 0 }}>
          <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--success, #059669)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>
            {t('agent.plan.groupConfirmed', 'Confirmed / handoff pending')} · {confirmed.length}
          </div>
          {confirmed.map(renderConfirmedRow)}
        </div>
      )}

      {closed.length > 0 && (
        <div style={{ display: 'grid', gap: '6px' }}>
          <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>
            {t('agent.plan.groupClosed', 'Rejected / expired')} · {closed.length}
          </div>
          {closed.map(renderClosedRow)}
        </div>
      )}
    </div>
  );
}
