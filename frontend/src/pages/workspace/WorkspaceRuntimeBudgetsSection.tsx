import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconAlertTriangle, IconPlayerPause, IconShieldCheck, IconShieldOff } from '@tabler/icons-react';

import { runtimeBudgetApi, type RuntimeBudgetPolicy, type RuntimeBudgetRun } from '../../api/domains/runtimeBudgets';
import './WorkspaceRuntimeBudgetsSection.css';

type RuntimeControlProfile = 'interactive' | 'scheduled' | 'workflow' | 'agent_team';
type Translate = ReturnType<typeof useTranslation>['t'];

const PROFILE_DEFAULTS: Record<RuntimeControlProfile, RuntimeBudgetPolicy> = {
  interactive: {
    id: 'built-in-interactive-runtime-default',
    tenant_id: null,
    name: 'Interactive runtime protection',
    enabled: true,
    priority: 0,
    scope_type: 'source_profile',
    source: 'interactive',
    profile: 'interactive',
    agent_id: null,
    trigger_id: null,
    enforcement_mode: 'enforce',
    fail_mode: 'require_confirmation',
    max_tokens: 50_000_000,
    max_cache_miss_tokens: 10_000_000,
    max_subagents: 24,
    max_team_sessions: 4,
    max_delegations: 16,
    max_background_tasks: 24,
    max_continuation_wakes: 64,
    max_provider_calls: 300,
    default_child_token_reservation: 200_000,
    default_llm_call_token_reservation: 200_000,
    created_at: null,
    updated_at: null,
  },
  scheduled: {
    id: 'built-in-scheduled-runtime-default',
    tenant_id: null,
    name: 'Daily runtime protection',
    enabled: true,
    priority: 0,
    scope_type: 'source_profile',
    source: 'scheduled',
    profile: 'scheduled',
    agent_id: null,
    trigger_id: null,
    enforcement_mode: 'enforce',
    fail_mode: 'summary_only',
    max_tokens: 40_000_000,
    max_cache_miss_tokens: 8_000_000,
    max_subagents: 32,
    max_team_sessions: 0,
    max_delegations: 12,
    max_background_tasks: 32,
    max_continuation_wakes: 64,
    max_provider_calls: 240,
    default_child_token_reservation: 250_000,
    default_llm_call_token_reservation: 250_000,
    created_at: null,
    updated_at: null,
  },
  workflow: {
    id: 'built-in-workflow-runtime-default',
    tenant_id: null,
    name: 'Dynamic Workflow',
    enabled: true,
    priority: 0,
    scope_type: 'source_profile',
    source: 'workflow',
    profile: 'workflow',
    agent_id: null,
    trigger_id: null,
    enforcement_mode: 'enforce',
    fail_mode: 'hard_stop',
    max_tokens: 250_000_000,
    max_cache_miss_tokens: 80_000_000,
    max_subagents: 256,
    max_team_sessions: 0,
    max_delegations: 64,
    max_background_tasks: 256,
    max_continuation_wakes: 512,
    max_provider_calls: 2_000,
    default_child_token_reservation: 300_000,
    default_llm_call_token_reservation: 300_000,
    created_at: null,
    updated_at: null,
  },
  agent_team: {
    id: 'built-in-agent-team-runtime-default',
    tenant_id: null,
    name: 'Agent Team',
    enabled: true,
    priority: 0,
    scope_type: 'source_profile',
    source: 'agent_team',
    profile: 'agent_team',
    agent_id: null,
    trigger_id: null,
    enforcement_mode: 'enforce',
    fail_mode: 'require_confirmation',
    max_tokens: 80_000_000,
    max_cache_miss_tokens: 16_000_000,
    max_subagents: 16,
    max_team_sessions: 4,
    max_delegations: 16,
    max_background_tasks: 16,
    max_continuation_wakes: 96,
    max_provider_calls: 500,
    default_child_token_reservation: 250_000,
    default_llm_call_token_reservation: 250_000,
    created_at: null,
    updated_at: null,
  },
};

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
  if (['waiting_budget_approval', 'resuming', 'exhausted', 'hard_stopped'].includes(status)) return 'warning';
  return 'muted';
}

function policySummary(policy: RuntimeBudgetPolicy, t: Translate) {
  return [
    `${t('runtimeBudgets.summarySubagents', 'Subagents')} ${formatNumber(policy.max_subagents)}`,
    `${t('runtimeBudgets.summaryTeamSessions', 'Team sessions')} ${formatNumber(policy.max_team_sessions)}`,
    `${t('runtimeBudgets.summaryWakes', 'Wakes')} ${formatNumber(policy.max_continuation_wakes)}`,
    `${t('runtimeBudgets.summaryCacheMiss', 'Cache miss')} ${formatNumber(policy.max_cache_miss_tokens)}`,
  ].join(' · ');
}

function FieldHelp({ children }: { children: string }) {
  return <span className="workspace-runtime-field-help">{children}</span>;
}

type Props = {
  agentId?: string;
};

export default function WorkspaceRuntimeBudgetsSection({ agentId }: Props) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [profile, setProfile] = useState<RuntimeControlProfile>('scheduled');
  const profileDefault = PROFILE_DEFAULTS[profile];
  const [draft, setDraft] = useState({
    name: profileDefault.name,
    source: profileDefault.source || 'scheduled',
    profile: profileDefault.profile || 'scheduled',
    max_subagents: profileDefault.max_subagents || 0,
    max_team_sessions: profileDefault.max_team_sessions || 0,
    max_continuation_wakes: profileDefault.max_continuation_wakes || 0,
    max_provider_calls: profileDefault.max_provider_calls || 0,
    max_tokens: profileDefault.max_tokens || 0,
    max_cache_miss_tokens: profileDefault.max_cache_miss_tokens || 0,
    fail_mode: profileDefault.fail_mode || 'fail_closed',
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
    () =>
      policies.find(
        (policy) =>
          policy.scope_type === 'source_profile' &&
          (policy.source === profile || policy.profile === profile),
      ),
    [policies, profile],
  );
  const builtInPolicy = useMemo<RuntimeBudgetPolicy>(
    () => ({
      ...profileDefault,
      name:
        profile === 'scheduled'
          ? t('runtimeBudgets.builtInScheduledPolicyName', 'Built-in daily runtime protection')
          : profile === 'interactive'
            ? t('runtimeBudgets.builtInInteractivePolicyName', 'Built-in interactive runtime protection')
          : profileDefault.name,
    }),
    [profile, profileDefault, t],
  );
  const effectivePolicy = activePolicy || builtInPolicy;
  const protectedRuns = runs.filter((run) =>
    ['waiting_budget_approval', 'resuming', 'exhausted', 'hard_stopped', 'stopped', 'expired', 'cancelled'].includes(
      run.status,
    ),
  );

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['runtime-budget-policies'] });
    qc.invalidateQueries({ queryKey: ['runtime-budget-runs'] });
  };

  const createPolicy = useMutation({
    mutationFn: () =>
      runtimeBudgetApi.createPolicy({
        name: draft.name.trim() || profileDefault.name,
        scope_type: 'source_profile',
        source: draft.source,
        profile: draft.profile,
        enforcement_mode: draft.enforcement_mode,
        max_subagents: draft.max_subagents,
        max_team_sessions: draft.max_team_sessions,
        max_continuation_wakes: draft.max_continuation_wakes,
        max_provider_calls: draft.max_provider_calls,
        max_tokens: draft.max_tokens,
        max_cache_miss_tokens: draft.max_cache_miss_tokens,
        max_background_tasks: profileDefault.max_background_tasks,
        max_delegations: profileDefault.max_delegations,
        fail_mode: draft.fail_mode,
        default_child_token_reservation: profileDefault.default_child_token_reservation,
        default_llm_call_token_reservation: profileDefault.default_llm_call_token_reservation,
      }),
    onSuccess: invalidate,
  });
  const selectProfile = (nextProfile: RuntimeControlProfile) => {
    const nextDefault = PROFILE_DEFAULTS[nextProfile];
    setProfile(nextProfile);
    setDraft({
      name: nextDefault.name,
      source: nextDefault.source || nextProfile,
      profile: nextDefault.profile || nextProfile,
      max_subagents: nextDefault.max_subagents || 0,
      max_team_sessions: nextDefault.max_team_sessions || 0,
      max_continuation_wakes: nextDefault.max_continuation_wakes || 0,
      max_provider_calls: nextDefault.max_provider_calls || 0,
      max_tokens: nextDefault.max_tokens || 0,
      max_cache_miss_tokens: nextDefault.max_cache_miss_tokens || 0,
      fail_mode: nextDefault.fail_mode || 'fail_closed',
      enforcement_mode: 'enforce',
    });
  };
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
        enforcement_mode: 'enforce',
      }),
    onSuccess: invalidate,
  });
  const rejectRun = useMutation({
    mutationFn: (run: RuntimeBudgetRun) =>
      runtimeBudgetApi.rejectOverrun(run.id, 'operator rejected continued run after review'),
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
              'Protection is on by default. The platform stops abnormal autonomous work from spreading, and users only see whether the task paused and what to do next.',
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
            {t('runtimeBudgets.observeMode', 'Observe only')}
          </button>
          <button
            className="btn btn-primary"
            onClick={() => switchMode.mutate('enforce')}
            disabled={switchMode.isPending}
          >
            <IconShieldCheck size={16} stroke={1.7} />
            {t('runtimeBudgets.enforceMode', 'Enforce protection')}
          </button>
          <p className="workspace-runtime-action-help">
            {t(
              'runtimeBudgets.modeHelp',
              'Observe only records what would be stopped. Enforce protection is the default hard-stop mode.',
            )}
          </p>
        </div>
      </section>

      <div className="workspace-runtime-grid">
        <div className="card workspace-runtime-card">
          <div className="workspace-runtime-card-header">
            <div>
              <h3>{t('runtimeBudgets.activePolicy', 'Effective policy')}</h3>
              <div className="workspace-runtime-policy-name">{effectivePolicy.name}</div>
              <p>
                {activePolicy
                  ? policySummary(effectivePolicy, t)
                  : t('runtimeBudgets.noPolicy', 'Built-in default protection is active.')}
              </p>
              {!activePolicy && <p className="workspace-runtime-policy-summary">{policySummary(effectivePolicy, t)}</p>}
            </div>
            <span className={`badge ${effectivePolicy.enforcement_mode === 'enforce' ? 'badge-success' : 'badge-warning'}`}>{effectivePolicy.enforcement_mode}</span>
          </div>
          <div className="workspace-runtime-form">
            <p className="workspace-runtime-form-note">
              {t(
                'runtimeBudgets.overrideNote',
                'Saving creates a company policy that takes priority over the platform default. It does not turn protection on; protection is already active.',
              )}
            </p>
            <div className="workspace-runtime-profile-tabs" aria-label={t('runtimeBudgets.profileTabs', 'Runtime budget policy groups')}>
              <button className={profile === 'interactive' ? 'active' : ''} onClick={() => selectProfile('interactive')} type="button">
                {t('runtimeBudgets.interactiveRuntime', 'Interactive')}
              </button>
              <button className={profile === 'scheduled' ? 'active' : ''} onClick={() => selectProfile('scheduled')} type="button">
                {t('runtimeBudgets.dailyRuntime', 'Daily runtime')}
              </button>
              <button className={profile === 'workflow' ? 'active' : ''} onClick={() => selectProfile('workflow')} type="button">
                {t('runtimeBudgets.dynamicWorkflow', 'Dynamic Workflow')}
              </button>
              <button className={profile === 'agent_team' ? 'active' : ''} onClick={() => selectProfile('agent_team')} type="button">
                {t('runtimeBudgets.agentTeam', 'Agent Team')}
              </button>
            </div>
            <label>
              {t('runtimeBudgets.policyName', 'Policy name')}
              <input className="form-input" value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} />
            </label>
            <div className="workspace-runtime-form-row">
              <label>
                {t('runtimeBudgets.maxSubagents', 'Max subagents')}
                <input className="form-input" type="number" min={0} value={draft.max_subagents} onChange={(event) => setDraft((current) => ({ ...current, max_subagents: Number(event.target.value) }))} />
                <FieldHelp>{t('runtimeBudgets.maxSubagentsHelp', 'Maximum child workers this run may start.')}</FieldHelp>
              </label>
              <label>
                {t('runtimeBudgets.maxTeamSessions', 'Max team sessions')}
                <input className="form-input" type="number" min={0} value={draft.max_team_sessions} onChange={(event) => setDraft((current) => ({ ...current, max_team_sessions: Number(event.target.value) }))} />
                <FieldHelp>{t('runtimeBudgets.maxTeamSessionsHelp', 'Maximum teammate sessions for explicit Agent Team runs.')}</FieldHelp>
              </label>
            </div>
            <div className="workspace-runtime-form-row">
              <label>
                {t('runtimeBudgets.maxWakes', 'Max wakes')}
                <input className="form-input" type="number" min={0} value={draft.max_continuation_wakes} onChange={(event) => setDraft((current) => ({ ...current, max_continuation_wakes: Number(event.target.value) }))} />
                <FieldHelp>{t('runtimeBudgets.maxWakesHelp', 'Maximum times this run chain may resume after background signals.')}</FieldHelp>
              </label>
              <label>
                {t('runtimeBudgets.maxProviderCalls', 'Max model calls')}
                <input className="form-input" type="number" min={0} value={draft.max_provider_calls} onChange={(event) => setDraft((current) => ({ ...current, max_provider_calls: Number(event.target.value) }))} />
                <FieldHelp>{t('runtimeBudgets.maxProviderCallsHelp', 'Maximum model calls allowed in this run chain.')}</FieldHelp>
              </label>
            </div>
            <label>
              {t('runtimeBudgets.totalTokenLimit', 'Total token limit')}
              <input className="form-input" type="number" min={0} value={draft.max_tokens} onChange={(event) => setDraft((current) => ({ ...current, max_tokens: Number(event.target.value) }))} />
              <FieldHelp>{t('runtimeBudgets.totalTokenLimitHelp', 'Maximum total tokens allowed for this run chain, including cached and non-cached tokens.')}</FieldHelp>
            </label>
            <label>
              {t('runtimeBudgets.cacheMissLimit', 'Cache-miss token limit')}
              <input className="form-input" type="number" min={0} value={draft.max_cache_miss_tokens} onChange={(event) => setDraft((current) => ({ ...current, max_cache_miss_tokens: Number(event.target.value) }))} />
              <FieldHelp>{t('runtimeBudgets.cacheMissLimitHelp', 'Maximum non-cached input tokens allowed for this run chain.')}</FieldHelp>
            </label>
            <label>
              {t('runtimeBudgets.failMode', 'When a limit is reached')}
              <select className="form-input" value={draft.fail_mode} onChange={(event) => setDraft((current) => ({ ...current, fail_mode: event.target.value }))}>
                <option value="summary_only">{t('runtimeBudgets.failModeSummaryOnly', 'Pause and summarize')}</option>
                <option value="hard_stop">{t('runtimeBudgets.failModeHardStop', 'Stop queued work')}</option>
                <option value="require_confirmation">{t('runtimeBudgets.failModeRequireConfirmation', 'Ask for approval')}</option>
                <option value="fail_closed">{t('runtimeBudgets.failModeFailClosed', 'Stop safely')}</option>
              </select>
              <FieldHelp>{t('runtimeBudgets.failModeHelp', 'What the platform should do when this run hits the protection limit.')}</FieldHelp>
            </label>
            <button className="btn btn-primary" onClick={() => createPolicy.mutate()} disabled={createPolicy.isPending}>
              {createPolicy.isPending ? t('common.loading', 'Loading...') : t('runtimeBudgets.savePolicy', 'Save company policy')}
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
                  <div className="workspace-runtime-run-next">{run.user_next_action}</div>
                  <div className="workspace-runtime-run-meta">{formatDate(run.created_at)} · {run.source || run.root_run_kind}</div>
                </div>
                <div className="workspace-runtime-run-actions">
                  {['waiting_budget_approval', 'exhausted', 'hard_stopped'].includes(run.status) && (
                    <button className="btn btn-secondary" onClick={() => approveRun.mutate(run)} disabled={approveRun.isPending}>
                      {t('runtimeBudgets.approveContinue', 'Approve')}
                    </button>
                  )}
                  {run.status === 'waiting_budget_approval' && (
                    <button className="btn btn-secondary" onClick={() => rejectRun.mutate(run)} disabled={rejectRun.isPending}>
                      {t('runtimeBudgets.rejectContinue', 'Reject')}
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
