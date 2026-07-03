import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconAlertTriangle, IconPlayerPause, IconShieldCheck, IconShieldOff } from '@tabler/icons-react';

import { runtimeBudgetApi, type RuntimeBudgetPolicy, type RuntimeBudgetRun } from '../../api/domains/runtimeBudgets';
import './WorkspaceRuntimeBudgetsSection.css';

function formatNumber(value?: number | null) {
  if (value == null) return '—';
  return value.toLocaleString();
}

function formatDate(value?: string | null) {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function statusTone(status: string) {
  if (status === 'active' || status === 'completed') return 'healthy';
  if (status === 'exhausted' || status === 'hard_stopped') return 'warning';
  return 'muted';
}

function policySummary(policy: RuntimeBudgetPolicy) {
  return [
    `Subagents ${formatNumber(policy.max_subagents)}`,
    `Wakes ${formatNumber(policy.max_continuation_wakes)}`,
    `Cache miss ${formatNumber(policy.max_cache_miss_tokens)}`,
  ].join(' · ');
}

type Props = {
  agentId?: string;
};

export default function WorkspaceRuntimeBudgetsSection({ agentId }: Props) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [draft, setDraft] = useState({
    name: 'Scheduled runtime default',
    source: 'scheduled',
    profile: 'scheduled',
    max_subagents: 32,
    max_continuation_wakes: 64,
    max_cache_miss_tokens: 250000,
    enforcement_mode: 'enforce',
  });

  const policiesQuery = useQuery({
    queryKey: ['runtime-budget-policies'],
    queryFn: runtimeBudgetApi.listPolicies,
  });
  const runsQuery = useQuery({
    queryKey: ['runtime-budget-runs', agentId || 'tenant'],
    queryFn: () => runtimeBudgetApi.listRuns({ agentId, limit: agentId ? 10 : 50 }),
  });

  const policies = policiesQuery.data || [];
  const runs = runsQuery.data || [];
  const activePolicy = useMemo(
    () => policies.find((policy) => policy.scope_type === 'source_profile' && policy.source === 'scheduled') || policies[0],
    [policies],
  );
  const protectedRuns = runs.filter((run) => ['exhausted', 'hard_stopped', 'expired', 'cancelled'].includes(run.status));

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['runtime-budget-policies'] });
    qc.invalidateQueries({ queryKey: ['runtime-budget-runs'] });
  };

  const createPolicy = useMutation({
    mutationFn: () =>
      runtimeBudgetApi.createPolicy({
        name: draft.name.trim() || 'Scheduled runtime default',
        scope_type: 'source_profile',
        source: draft.source,
        profile: draft.profile,
        enforcement_mode: draft.enforcement_mode,
        fail_mode: 'fail_closed',
        max_subagents: draft.max_subagents,
        max_continuation_wakes: draft.max_continuation_wakes,
        max_cache_miss_tokens: draft.max_cache_miss_tokens,
        max_background_tasks: draft.max_subagents,
        max_delegations: draft.max_subagents,
      }),
    onSuccess: invalidate,
  });
  const switchMode = useMutation({
    mutationFn: (mode: 'observe' | 'enforce') =>
      runtimeBudgetApi.setTenantEnforcementMode({
        enforcement_mode: mode,
        reason: mode === 'observe' ? 'operator emergency observe mode' : 'operator restore enforcement',
      }),
    onSuccess: invalidate,
  });
  const cancelRun = useMutation({
    mutationFn: (run: RuntimeBudgetRun) =>
      runtimeBudgetApi.cancelRun(run.id, 'operator stopped runtime budget run from control plane'),
    onSuccess: invalidate,
  });
  const approveRun = useMutation({
    mutationFn: (run: RuntimeBudgetRun) =>
      runtimeBudgetApi.approveOverrun(run.id, {
        reason: 'operator approved continued run after review',
        enforcement_mode: 'observe',
      }),
    onSuccess: invalidate,
  });

  return (
    <div className="workspace-runtime-budgets">
      <section className="workspace-runtime-band">
        <div>
          <div className="workspace-runtime-eyebrow">
            <IconShieldCheck size={16} stroke={1.7} />
            {t('runtimeBudgets.title', 'Runtime Budgets')}
          </div>
          <h2>{t('runtimeBudgets.heading', 'Autonomous run protection')}</h2>
          <p>
            {t(
              'runtimeBudgets.description',
              'Set hard guardrails for triggers, background work, subagents, delegation, wake loops, and provider calls. Users only see the outcome and next action.',
            )}
          </p>
        </div>
        <div className="workspace-runtime-actions">
          <button
            className="btn btn-secondary"
            onClick={() => switchMode.mutate('observe')}
            disabled={switchMode.isPending}
          >
            <IconShieldOff size={16} stroke={1.7} />
            {t('runtimeBudgets.observeMode', 'Observe mode')}
          </button>
          <button
            className="btn btn-primary"
            onClick={() => switchMode.mutate('enforce')}
            disabled={switchMode.isPending}
          >
            <IconShieldCheck size={16} stroke={1.7} />
            {t('runtimeBudgets.enforceMode', 'Enforce mode')}
          </button>
        </div>
      </section>

      <div className="workspace-runtime-grid">
        <div className="card workspace-runtime-card">
          <div className="workspace-runtime-card-header">
            <div>
              <h3>{t('runtimeBudgets.activePolicy', 'Active policy')}</h3>
              {activePolicy && <div className="workspace-runtime-policy-name">{activePolicy.name}</div>}
              <p>{activePolicy ? policySummary(activePolicy) : t('runtimeBudgets.noPolicy', 'No tenant policy yet.')}</p>
            </div>
            {activePolicy && <span className={`badge ${activePolicy.enforcement_mode === 'enforce' ? 'badge-success' : 'badge-warning'}`}>{activePolicy.enforcement_mode}</span>}
          </div>
          <div className="workspace-runtime-form">
            <label>
              {t('runtimeBudgets.policyName', 'Policy name')}
              <input className="form-input" value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} />
            </label>
            <div className="workspace-runtime-form-row">
              <label>
                {t('runtimeBudgets.maxSubagents', 'Max subagents')}
                <input className="form-input" type="number" min={0} value={draft.max_subagents} onChange={(event) => setDraft((current) => ({ ...current, max_subagents: Number(event.target.value) }))} />
              </label>
              <label>
                {t('runtimeBudgets.maxWakes', 'Max wakes')}
                <input className="form-input" type="number" min={0} value={draft.max_continuation_wakes} onChange={(event) => setDraft((current) => ({ ...current, max_continuation_wakes: Number(event.target.value) }))} />
              </label>
            </div>
            <label>
              {t('runtimeBudgets.cacheMissLimit', 'Cache-miss token limit')}
              <input className="form-input" type="number" min={0} value={draft.max_cache_miss_tokens} onChange={(event) => setDraft((current) => ({ ...current, max_cache_miss_tokens: Number(event.target.value) }))} />
            </label>
            <button className="btn btn-primary" onClick={() => createPolicy.mutate()} disabled={createPolicy.isPending}>
              {createPolicy.isPending ? t('common.loading', 'Loading...') : t('runtimeBudgets.savePolicy', 'Save policy')}
            </button>
          </div>
        </div>

        <div className="card workspace-runtime-card">
          <div className="workspace-runtime-card-header">
            <div>
              <h3>{t('runtimeBudgets.protectedRuns', 'Protected runs')}</h3>
              <p>{t('runtimeBudgets.protectedRunsDesc', 'Recent runs paused or stopped by the runtime guard.')}</p>
            </div>
            <span className="badge badge-warning">{protectedRuns.length}</span>
          </div>
          <div className="workspace-runtime-run-list">
            {(protectedRuns.length > 0 ? protectedRuns : runs.slice(0, 5)).map((run) => (
              <div key={run.id} className="workspace-runtime-run-row">
                <div>
                  <div className="workspace-runtime-run-title">
                    <span className={`workspace-runtime-dot ${statusTone(run.status)}`} />
                    {run.user_status}
                  </div>
                  <div className="workspace-runtime-run-reason">{run.user_reason}</div>
                  <div className="workspace-runtime-run-meta">{formatDate(run.created_at)} · {run.source || run.root_run_kind}</div>
                </div>
                <div className="workspace-runtime-run-actions">
                  {['exhausted', 'hard_stopped'].includes(run.status) && (
                    <button className="btn btn-secondary" onClick={() => approveRun.mutate(run)} disabled={approveRun.isPending}>
                      {t('runtimeBudgets.approveContinue', 'Approve')}
                    </button>
                  )}
                  {run.status === 'active' && (
                    <button className="btn btn-secondary" onClick={() => cancelRun.mutate(run)} disabled={cancelRun.isPending}>
                      <IconPlayerPause size={15} stroke={1.7} />
                      {t('runtimeBudgets.stopRun', 'Stop')}
                    </button>
                  )}
                </div>
              </div>
            ))}
            {!runsQuery.isLoading && runs.length === 0 && (
              <div className="workspace-runtime-empty">
                <IconAlertTriangle size={16} stroke={1.7} />
                {t('runtimeBudgets.emptyRuns', 'No runtime budget runs yet.')}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
