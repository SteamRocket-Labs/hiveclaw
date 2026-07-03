import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import './AgentWorkflowsSection.css';
import {
  activateWorkflowDefinition,
  approveWorkflowPromotion,
  cancelWorkflowRun,
  deprecateWorkflowDefinition,
  forkWorkflowDefinition,
  getWorkflowRun,
  listPromoteSuggestions,
  listWorkflowDefinitions,
  listWorkflowRuns,
  previewWorkflow,
  promoteWorkflowRun,
  repairWorkflowRun,
  revokeWorkflowDefinition,
  startWorkflow,
  type WorkflowDefinitionRecord,
  type WorkflowPreview,
  type WorkflowRunStatus,
  type WorkflowRunSummary,
  type StartWorkflowOptions,
  type WorkflowStartResult,
} from '../../api/domains/workflows';

type AgentWorkflowsSectionProps = {
  agentId: string;
  canManage?: boolean;
};

// ── badges ─────────────────────────────────────────────────────────

type Tone = 'ok' | 'warn' | 'error' | 'muted';

const BADGE_TONE_CLASS: Record<Tone, string> = {
  ok: 'badge-success',
  warn: 'badge-warning',
  error: 'badge-error',
  muted: 'badge-neutral',
};

const badgeClass = (tone: Tone): string => `badge ${BADGE_TONE_CLASS[tone]}`;

export function definitionTone(status: WorkflowDefinitionRecord['status']): Tone {
  if (status === 'active') return 'ok';
  if (status === 'draft') return 'warn';
  if (status === 'revoked') return 'error';
  return 'muted';
}

export function runStatusTone(status: WorkflowRunStatus): Tone {
  if (status === 'completed') return 'ok';
  if (status === 'failed' || status === 'killed') return 'error';
  if (status === 'running' || status === 'suspended') return 'warn';
  return 'muted';
}

const RUN_STATUS_KEYS: Record<string, string> = {
  completed: 'workflows.runCompleted',
  failed: 'workflows.runFailed',
  running: 'workflows.runRunning',
  suspended: 'workflows.runSuspended',
  killed: 'workflows.runKilled',
  pending: 'workflows.runPending',
};

const DEFINITION_STATUS_KEYS: Record<string, string> = {
  draft: 'workflows.statusDraft',
  active: 'workflows.statusActive',
  deprecated: 'workflows.statusDeprecated',
  revoked: 'workflows.statusRevoked',
};

const VISIBILITY_KEYS: Record<string, string> = {
  agent: 'workflows.visibilityAgent',
  tenant: 'workflows.visibilityTenant',
  org: 'workflows.visibilityOrg',
  platform: 'workflows.visibilityPlatform',
};

function formatTimestamp(value: string | null): string {
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toLocaleString();
}

// ── preview binding + optional plan handoff for confirmation-required starts ──

export type WorkflowPlanHandoffFields = {
  confirmedPlanId: string;
  planVersion: string;
  planHash: string;
};

export function buildWorkflowStartOptions(
  preview: WorkflowPreview | null,
  handoff: WorkflowPlanHandoffFields,
): StartWorkflowOptions | null {
  if (!preview) return null;
  const binding = {
    previewId: preview.preview_id,
    definitionHash: preview.definition_hash,
    argsHash: preview.args_hash,
  };
  if (!preview.confirmation_required) return binding;
  const planVersion = Number(handoff.planVersion);
  if (!handoff.confirmedPlanId.trim() || !Number.isInteger(planVersion) || planVersion <= 0 || !handoff.planHash.trim()) {
    return null;
  }
  return {
    ...binding,
    confirmedPlanId: handoff.confirmedPlanId.trim(),
    planVersion,
    planHash: handoff.planHash.trim(),
  };
}

export default function AgentWorkflowsSection({ agentId, canManage = false }: AgentWorkflowsSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // ── asset view state ──
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // ── advanced (manual run) state — original P12 flow, demoted ──
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [definitionText, setDefinitionText] = useState('');
  const [argsText, setArgsText] = useState('{}');
  const [preview, setPreview] = useState<WorkflowPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<WorkflowStartResult | null>(null);
  const [planHandoff, setPlanHandoff] = useState<WorkflowPlanHandoffFields>({
    confirmedPlanId: '',
    planVersion: '',
    planHash: '',
  });

  const invalidateAssets = () => {
    queryClient.invalidateQueries({ queryKey: ['workflow-runs', agentId] });
    queryClient.invalidateQueries({ queryKey: ['workflow-definitions', agentId] });
    queryClient.invalidateQueries({ queryKey: ['workflow-promote-suggestions', agentId] });
  };

  // ── queries ──
  const { data: runs = [] } = useQuery({
    queryKey: ['workflow-runs', agentId],
    queryFn: () => listWorkflowRuns(agentId),
    enabled: !!agentId,
    refetchInterval: (query) =>
      (query.state.data ?? []).some((run: WorkflowRunSummary) => run.status === 'running') ? 8000 : false,
  });

  const { data: definitions = [] } = useQuery({
    queryKey: ['workflow-definitions', agentId],
    queryFn: () => listWorkflowDefinitions(agentId),
    enabled: !!agentId,
  });

  const { data: suggestions = [] } = useQuery({
    queryKey: ['workflow-promote-suggestions', agentId],
    queryFn: () => listPromoteSuggestions(agentId),
    enabled: !!agentId,
  });

  const { data: expandedRun } = useQuery({
    queryKey: ['workflow-run', agentId, expandedRunId],
    queryFn: () => getWorkflowRun(agentId, expandedRunId!),
    enabled: !!agentId && !!expandedRunId,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 5000 : false),
  });

  const { data: lastRunDetail } = useQuery({
    queryKey: ['workflow-run', agentId, lastRun?.run_id],
    queryFn: () => getWorkflowRun(agentId, lastRun!.run_id),
    enabled: !!agentId && !!lastRun?.run_id,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 5000 : false),
  });

  // ── mutations ──
  const promoteMutation = useMutation({
    mutationFn: async (runId: string) => promoteWorkflowRun(agentId, runId),
    onSuccess: () => {
      setActionError(null);
      invalidateAssets();
    },
    onError: (error: unknown) => setActionError(error instanceof Error ? error.message : String(error)),
  });

  const cancelMutation = useMutation({
    mutationFn: async (runId: string) => cancelWorkflowRun(agentId, runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['workflow-run', agentId] });
      queryClient.invalidateQueries({ queryKey: ['workflow-runs', agentId] });
    },
  });

  const repairMutation = useMutation({
    mutationFn: async (runId: string) => repairWorkflowRun(agentId, runId),
    onSuccess: () => {
      setActionError(null);
      queryClient.invalidateQueries({ queryKey: ['workflow-run', agentId] });
      queryClient.invalidateQueries({ queryKey: ['workflow-runs', agentId] });
      queryClient.invalidateQueries({ queryKey: ['workflow-promote-suggestions', agentId] });
    },
    onError: (error: unknown) => setActionError(error instanceof Error ? error.message : String(error)),
  });

  const lifecycleMutation = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: 'activate' | 'deprecate' | 'revoke' | 'promote' }) => {
      if (action === 'activate') return activateWorkflowDefinition(id);
      if (action === 'deprecate') return deprecateWorkflowDefinition(id);
      if (action === 'revoke') return revokeWorkflowDefinition(id);
      return approveWorkflowPromotion(id);
    },
    onSuccess: () => {
      setActionError(null);
      queryClient.invalidateQueries({ queryKey: ['workflow-definitions', agentId] });
    },
    onError: (error: unknown) => setActionError(error instanceof Error ? error.message : String(error)),
  });

  const forkMutation = useMutation({
    mutationFn: async (record: WorkflowDefinitionRecord) => forkWorkflowDefinition(record.id, agentId),
    onSuccess: (result) => {
      // fork lands in the advanced editor for adjustment before a manual run
      setDefinitionText(JSON.stringify(result.definition, null, 2));
      setPreview(null);
      setAdvancedOpen(true);
    },
  });

  const parsePayload = (): { definition: Record<string, unknown>; args: Record<string, unknown> } | null => {
    try {
      const definition = JSON.parse(definitionText) as Record<string, unknown>;
      const args = JSON.parse(argsText || '{}') as Record<string, unknown>;
      setPreviewError(null);
      return { definition, args };
    } catch (error) {
      setPreviewError(t('workflows.invalidJson'));
      return null;
    }
  };

  const previewMutation = useMutation({
    mutationFn: async () => {
      const payload = parsePayload();
      if (!payload) throw new Error('invalid json');
      return previewWorkflow(agentId, payload.definition, payload.args);
    },
    onSuccess: (result) => {
      setPreview(result);
      setPreviewError(null);
    },
    onError: (error: unknown) => {
      setPreview(null);
      setPreviewError(error instanceof Error ? error.message : String(error));
    },
  });

  const startMutation = useMutation({
    mutationFn: async () => {
      const payload = parsePayload();
      if (!payload) throw new Error('invalid json');
      const options = buildWorkflowStartOptions(preview, planHandoff);
      if (!options) {
        throw new Error(t('workflows.missingPlanHandoff'));
      }
      return startWorkflow(agentId, payload.definition, payload.args, options);
    },
    onSuccess: (result) => {
      setLastRun(result);
      queryClient.invalidateQueries({ queryKey: ['workflow-run', agentId] });
      invalidateAssets();
    },
    onError: (error: unknown) => {
      setPreviewError(error instanceof Error ? error.message : String(error));
    },
  });

  const startOptions = buildWorkflowStartOptions(preview, planHandoff);

  return (
    <div className="agent-workflows-root">
      <p className="agent-workflows-intro">{t('workflows.intro')}</p>

      {actionError && (
        <div data-testid="workflow-action-error" className="agent-workflows-error">
          {actionError}
        </div>
      )}

      {/* ── promote suggestions: repeated-run evidence (§4 / §6.6) ── */}
      {suggestions.length > 0 && (
        <div data-testid="workflow-suggestions-banner" className="agent-workflows-suggestions">
          {suggestions.map((suggestion) => (
            <div key={suggestion.definition_hash} className="agent-workflows-suggestion-row">
              <span className="u-body">
                {t('workflows.suggestionBody', { name: suggestion.name, count: suggestion.run_count })}
              </span>
              {canManage && suggestion.sample_run_ids.length > 0 && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => promoteMutation.mutate(suggestion.sample_run_ids[0])}
                  disabled={promoteMutation.isPending}
                >
                  {t('workflows.promote')}
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* ── ① templates: 固化资产 ── */}
      <section data-testid="workflow-templates-section">
        <h3 className="agent-workflows-section-title">{t('workflows.registeredTitle')}</h3>
        <p className="agent-workflows-hint">{t('workflows.registeredHint')}</p>
        {definitions.length === 0 ? (
          <p className="u-body u-tertiary">{t('workflows.noDefinitions')}</p>
        ) : (
          <div className="agent-workflows-list">
            {definitions.map((record) => (
              <div
                key={record.id}
                data-testid={`workflow-definition-${record.name}-v${record.definition_version}`}
                className="agent-workflows-card"
              >
                <div className="agent-workflows-card-head">
                  <strong className="u-emphasis">{record.name}</strong>
                  <span className="u-row u-tertiary">v{record.definition_version}</span>
                  <span className={badgeClass(definitionTone(record.status))}>
                    {t(DEFINITION_STATUS_KEYS[record.status] ?? record.status)}
                  </span>
                  <span className="u-row u-secondary">
                    {t(VISIBILITY_KEYS[record.visibility_scope] ?? record.visibility_scope)}
                  </span>
                  {record.promoted_from_run_id && (
                    <span className="u-row u-tertiary">
                      {t('workflows.promotedFrom')} {record.promoted_from_run_id.slice(0, 8)}…
                    </span>
                  )}
                </div>
                {record.description && <p className="agent-workflows-card-desc">{record.description}</p>}
                {canManage && (
                  <div className="agent-workflows-actions">
                    {record.status === 'draft' && (
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => lifecycleMutation.mutate({ id: record.id, action: 'promote' })}
                      >
                        {t('workflows.approvePromotion')}
                      </button>
                    )}
                    {record.status === 'active' && (
                      <>
                        <button type="button" className="btn btn-ghost" onClick={() => forkMutation.mutate(record)}>
                          {t('workflows.fork')}
                        </button>
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() => lifecycleMutation.mutate({ id: record.id, action: 'deprecate' })}
                        >
                          {t('workflows.deprecate')}
                        </button>
                      </>
                    )}
                    {record.status !== 'revoked' && (
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => lifecycleMutation.mutate({ id: record.id, action: 'revoke' })}
                      >
                        {t('workflows.revoke')}
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ── ② run history: archived ephemeral flows ── */}
      <section data-testid="workflow-runs-section">
        <h3 className="agent-workflows-section-title">{t('workflows.runsTitle')}</h3>
        <p className="agent-workflows-hint">{t('workflows.runsHint')}</p>
        {runs.length === 0 ? (
          <p className="u-body u-tertiary">{t('workflows.runsEmpty')}</p>
        ) : (
          <div className="agent-workflows-list">
            {runs.map((run) => {
              const expanded = expandedRunId === run.run_id;
              const dynamicWorkflow = run.dynamic_workflow ?? null;
              const outcomeEvidence = run.outcome_evidence ?? dynamicWorkflow?.outcome_evidence ?? null;
              const repairPlan = run.repair_plan ?? dynamicWorkflow?.repair_plan ?? null;
              return (
                <div key={run.run_id} data-testid={`workflow-run-row-${run.run_id}`} className="agent-workflows-card">
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setExpandedRunId(expanded ? null : run.run_id)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        setExpandedRunId(expanded ? null : run.run_id);
                      }
                    }}
                    className="agent-workflows-run-head"
                  >
                    <div className="agent-workflows-card-head">
                      <strong className="u-emphasis">{run.name}</strong>
                      <span className={badgeClass(runStatusTone(run.status))}>
                        {t(RUN_STATUS_KEYS[run.status] ?? run.status)}
                      </span>
                      {dynamicWorkflow && <span className={badgeClass('muted')}>{t('workflows.dynamicWorkflow')}</span>}
                      {run.steps_total > 0 && (
                        <span className="u-row u-secondary">
                          {t('workflows.stepsProgress', { done: run.steps_done, total: run.steps_total })}
                          {run.steps_failed > 0 ? ` · ${t('workflows.stepsFailed', { count: run.steps_failed })}` : ''}
                        </span>
                      )}
                      <span className="agent-workflows-run-time">{formatTimestamp(run.created_at)}</span>
                    </div>
                    {run.description && <p className="agent-workflows-card-desc">{run.description}</p>}
                    {dynamicWorkflow && (
                      <div className="agent-workflows-dynamic-meta">
                        {dynamicWorkflow.proposal_id && (
                          <span>
                            {t('workflows.proposal')} {dynamicWorkflow.proposal_id}
                          </span>
                        )}
                        {dynamicWorkflow.candidate_id && (
                          <span>
                            {t('workflows.candidate')} {dynamicWorkflow.candidate_id}
                          </span>
                        )}
                        {outcomeEvidence?.leaf_total !== undefined && (
                          <span>
                            {t('workflows.leafEvidence', {
                              failed: outcomeEvidence.leaf_failed ?? 0,
                              total: outcomeEvidence.leaf_total ?? 0,
                            })}
                          </span>
                        )}
                      </div>
                    )}
                  </div>

                  <div className="agent-workflows-actions">
                    {run.promoted_definition_id ? (
                      <span className={badgeClass('ok')}>{t('workflows.promoted')}</span>
                    ) : (
                      canManage &&
                      run.status === 'completed' &&
                      run.definition_source === 'ephemeral' && (
                        <button
                          type="button"
                          className="btn btn-ghost"
                          data-testid={`workflow-promote-${run.run_id}`}
                          onClick={() => promoteMutation.mutate(run.run_id)}
                          disabled={promoteMutation.isPending}
                        >
                          {t('workflows.promote')}
                        </button>
                      )
                    )}
                    {run.status === 'running' && (
                      <button type="button" className="btn btn-ghost" onClick={() => cancelMutation.mutate(run.run_id)}>
                        {t('workflows.cancel')}
                      </button>
                    )}
                    {canManage && repairPlan?.repairable && (
                      <button
                        type="button"
                        className="btn btn-ghost"
                        data-testid={`workflow-repair-${run.run_id}`}
                        onClick={() => repairMutation.mutate(run.run_id)}
                        disabled={repairMutation.isPending}
                      >
                        {t('workflows.repair')}
                      </button>
                    )}
                  </div>

                  {expanded && (
                    <div className="agent-workflows-expanded">
                      {!expandedRun ? (
                        <span className="u-body u-secondary">{t('workflows.loadingDetail')}</span>
                      ) : expandedRun.steps.length === 0 ? (
                        <span className="u-body u-secondary">{t('workflows.emptySteps')}</span>
                      ) : (
                        <ul className="agent-workflows-step-list">
                          {expandedRun.steps.map((step) => (
                            <li key={step.step_id} className="agent-workflows-step">
                              <span className={badgeClass(step.status === 'done' ? 'ok' : step.status === 'failed' ? 'error' : 'muted')}>
                                {step.status}
                              </span>
                              <span>{step.step_id}</span>
                              <span className="u-row u-tertiary">{step.step_type}</span>
                              {step.error ? <span className="agent-workflows-error-text">— {step.error}</span> : null}
                            </li>
                          ))}
                        </ul>
                      )}
                      {expandedRun && expandedRun.leaf_calls.length > 0 && (
                        <div className="agent-workflows-leaf-block">
                          <div className="agent-workflows-leaf-label">{t('workflows.leafCalls')}</div>
                          <ul className="agent-workflows-step-list">
                            {expandedRun.leaf_calls.map((leaf) => (
                              <li key={`${leaf.step_id}:${leaf.leaf_id}`} className="agent-workflows-step">
                                <span className={badgeClass(leaf.status === 'done' ? 'ok' : leaf.status === 'failed' ? 'error' : 'muted')}>
                                  {leaf.status}
                                </span>
                                <span>{leaf.step_id}</span>
                                <span className="u-row u-tertiary">{leaf.leaf_id}</span>
                                {leaf.error ? <span className="agent-workflows-error-text">— {leaf.error}</span> : null}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* ── ③ advanced: manual run (original P12 flow, collapsed by default) ── */}
      <section>
        <button
          type="button"
          data-testid="workflow-advanced-toggle"
          aria-expanded={advancedOpen}
          onClick={() => setAdvancedOpen((open) => !open)}
          className="agent-workflows-advanced-toggle"
        >
          {advancedOpen ? '▾' : '▸'} {t('workflows.advancedTitle')}
        </button>

        {advancedOpen && (
          <div className="agent-workflows-advanced-body">
            <p className="agent-workflows-hint">{t('workflows.ephemeralHint')}</p>
            <textarea
              data-testid="workflow-definition-input"
              value={definitionText}
              onChange={(event) => setDefinitionText(event.target.value)}
              placeholder='{"name": "...", "steps": [...]}'
              rows={8}
              className="agent-workflows-code-input"
            />
            <textarea
              data-testid="workflow-args-input"
              value={argsText}
              onChange={(event) => setArgsText(event.target.value)}
              placeholder="{}"
              rows={2}
              className="agent-workflows-code-input agent-workflows-code-input--args"
            />
            <div className="agent-workflows-advanced-actions">
              <button
                type="button"
                className="btn btn-secondary"
                data-testid="workflow-preview-button"
                onClick={() => previewMutation.mutate()}
                disabled={previewMutation.isPending || !definitionText.trim()}
              >
                {t('workflows.preview')}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                data-testid="workflow-start-button"
                onClick={() => startMutation.mutate()}
                disabled={startMutation.isPending || !preview || (preview.confirmation_required && !startOptions)}
                title={preview?.confirmation_required ? t('workflows.highRiskNeedsPlan') : undefined}
              >
                {t('workflows.confirmAndRun')}
              </button>
            </div>
            {previewError && (
              <div data-testid="workflow-preview-error" className="agent-workflows-error agent-workflows-error--spaced">
                {previewError}
              </div>
            )}
            {preview && (
              <div data-testid="workflow-preview-card" className="agent-workflows-preview">
                <span className={badgeClass(preview.confirmation_required ? 'warn' : 'ok')}>
                  {t(preview.confirmation_required ? 'workflows.confirmationRequired' : 'workflows.confirmationNotRequired')}
                </span>
                <span className="agent-workflows-ml">
                  {t('workflows.plannedLeaves', { count: preview.planned_leaf_calls })}
                </span>
                <span className="agent-workflows-ml u-secondary">
                  hash: {preview.definition_hash.slice(0, 12)}…
                </span>
                {preview.confirmation_required && (
                  <div data-testid="workflow-plan-required" className="agent-workflows-plan-required">
                    {t('workflows.highRiskNeedsPlan')}
                    <ul className="agent-workflows-reason-list">
                      {preview.confirmation_reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                    <div className="agent-workflows-handoff-grid">
                      <input
                        data-testid="workflow-confirmed-plan-id"
                        value={planHandoff.confirmedPlanId}
                        onChange={(event) => setPlanHandoff((current) => ({ ...current, confirmedPlanId: event.target.value }))}
                        placeholder={t('workflows.confirmedPlanId')}
                      />
                      <input
                        data-testid="workflow-plan-version"
                        value={planHandoff.planVersion}
                        onChange={(event) => setPlanHandoff((current) => ({ ...current, planVersion: event.target.value }))}
                        placeholder={t('workflows.planVersion')}
                      />
                      <input
                        data-testid="workflow-plan-hash"
                        value={planHandoff.planHash}
                        onChange={(event) => setPlanHandoff((current) => ({ ...current, planHash: event.target.value }))}
                        placeholder={t('workflows.planHash')}
                      />
                    </div>
                  </div>
                )}
              </div>
            )}

            {lastRun && (
              <div data-testid="workflow-run-panel" className="agent-workflows-run-panel">
                <h4 className="agent-workflows-run-panel-title">{t('workflows.runTitle')}</h4>
                <div className="u-body">
                  <span className={badgeClass(runStatusTone((lastRunDetail?.status ?? lastRun.status) as WorkflowRunStatus))}>
                    {lastRunDetail?.status ?? lastRun.status}
                  </span>
                  <span className="agent-workflows-ml u-secondary">run: {lastRun.run_id.slice(0, 8)}…</span>
                  {lastRunDetail?.status === 'running' && (
                    <button type="button" className="btn btn-ghost agent-workflows-ml" onClick={() => cancelMutation.mutate(lastRun.run_id)}>
                      {t('workflows.cancel')}
                    </button>
                  )}
                </div>
                <ul className="agent-workflows-run-panel-steps">
                  {(lastRunDetail?.steps ?? []).map((step) => (
                    <li key={step.step_id} data-testid={`workflow-step-${step.step_id}`}>
                      <span className={badgeClass(step.status === 'done' ? 'ok' : step.status === 'failed' ? 'error' : 'muted')}>
                        {step.status}
                      </span>{' '}
                      {step.step_id}
                      {step.error ? <span className="agent-workflows-error-text"> — {step.error}</span> : null}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
