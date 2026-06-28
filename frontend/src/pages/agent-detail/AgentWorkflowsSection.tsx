import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

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

const badgeStyle = (tone: Tone): React.CSSProperties => ({
  padding: '2px 8px',
  borderRadius: 'var(--radius-full, 999px)',
  fontSize: '11px',
  fontWeight: 600,
  whiteSpace: 'nowrap',
  background:
    tone === 'ok'
      ? 'var(--success-subtle, rgba(0,180,120,0.12))'
      : tone === 'warn'
        ? 'var(--warning-subtle, rgba(255,180,0,0.12))'
        : tone === 'error'
          ? 'var(--error-subtle, rgba(255,80,80,0.12))'
          : 'rgba(128,128,128,0.12)',
  color:
    tone === 'ok'
      ? 'var(--success)'
      : tone === 'warn'
        ? 'var(--warning)'
        : tone === 'error'
          ? 'var(--error)'
          : 'var(--text-secondary)',
});

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

const sectionTitleStyle: React.CSSProperties = { margin: '0 0 4px', fontSize: 15 };
const sectionHintStyle: React.CSSProperties = { margin: '0 0 12px', color: 'var(--text-secondary)', fontSize: 13 };
const cardStyle: React.CSSProperties = {
  border: '1px solid var(--border-subtle, rgba(128,128,128,0.2))',
  borderRadius: 'var(--radius-md, 8px)',
  padding: '12px 16px',
  background: 'var(--bg-secondary, transparent)',
};

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
    <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 28, maxWidth: 920 }}>
      <p style={{ ...sectionHintStyle, margin: 0 }}>{t('workflows.intro')}</p>

      {actionError && (
        <div data-testid="workflow-action-error" style={{ color: 'var(--error)', fontSize: 13 }}>
          {actionError}
        </div>
      )}

      {/* ── promote suggestions: repeated-run evidence (§4 / §6.6) ── */}
      {suggestions.length > 0 && (
        <div
          data-testid="workflow-suggestions-banner"
          style={{
            ...cardStyle,
            borderColor: 'var(--accent-primary, #6366f1)',
            background: 'var(--accent-subtle, rgba(99,102,241,0.08))',
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
          }}
        >
          {suggestions.map((suggestion) => (
            <div
              key={suggestion.definition_hash}
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}
            >
              <span style={{ fontSize: 13 }}>
                {t('workflows.suggestionBody', { name: suggestion.name, count: suggestion.run_count })}
              </span>
              {canManage && suggestion.sample_run_ids.length > 0 && (
                <button
                  type="button"
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
        <h3 style={sectionTitleStyle}>{t('workflows.registeredTitle')}</h3>
        <p style={sectionHintStyle}>{t('workflows.registeredHint')}</p>
        {definitions.length === 0 ? (
          <p style={{ color: 'var(--text-tertiary, var(--text-secondary))', fontSize: 13 }}>
            {t('workflows.noDefinitions')}
          </p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {definitions.map((record) => (
              <div
                key={record.id}
                data-testid={`workflow-definition-${record.name}-v${record.definition_version}`}
                style={cardStyle}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <strong style={{ fontSize: 14 }}>{record.name}</strong>
                  <span style={{ color: 'var(--text-tertiary, var(--text-secondary))', fontSize: 12 }}>
                    v{record.definition_version}
                  </span>
                  <span style={badgeStyle(definitionTone(record.status))}>
                    {t(DEFINITION_STATUS_KEYS[record.status] ?? record.status)}
                  </span>
                  <span style={{ color: 'var(--text-secondary)', fontSize: 12 }}>
                    {t(VISIBILITY_KEYS[record.visibility_scope] ?? record.visibility_scope)}
                  </span>
                  {record.promoted_from_run_id && (
                    <span style={{ color: 'var(--text-tertiary, var(--text-secondary))', fontSize: 12 }}>
                      {t('workflows.promotedFrom')} {record.promoted_from_run_id.slice(0, 8)}…
                    </span>
                  )}
                </div>
                {record.description && (
                  <p style={{ margin: '6px 0 0', color: 'var(--text-secondary)', fontSize: 13 }}>
                    {record.description}
                  </p>
                )}
                {canManage && (
                  <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                    {record.status === 'draft' && (
                      <button
                        type="button"
                        onClick={() => lifecycleMutation.mutate({ id: record.id, action: 'promote' })}
                      >
                        {t('workflows.approvePromotion')}
                      </button>
                    )}
                    {record.status === 'active' && (
                      <>
                        <button type="button" onClick={() => forkMutation.mutate(record)}>
                          {t('workflows.fork')}
                        </button>
                        <button
                          type="button"
                          onClick={() => lifecycleMutation.mutate({ id: record.id, action: 'deprecate' })}
                        >
                          {t('workflows.deprecate')}
                        </button>
                      </>
                    )}
                    {record.status !== 'revoked' && (
                      <button
                        type="button"
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
        <h3 style={sectionTitleStyle}>{t('workflows.runsTitle')}</h3>
        <p style={sectionHintStyle}>{t('workflows.runsHint')}</p>
        {runs.length === 0 ? (
          <p style={{ color: 'var(--text-tertiary, var(--text-secondary))', fontSize: 13 }}>
            {t('workflows.runsEmpty')}
          </p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {runs.map((run) => {
              const expanded = expandedRunId === run.run_id;
              const dynamicWorkflow = run.dynamic_workflow ?? null;
              const outcomeEvidence = run.outcome_evidence ?? dynamicWorkflow?.outcome_evidence ?? null;
              const repairPlan = run.repair_plan ?? dynamicWorkflow?.repair_plan ?? null;
              return (
                <div key={run.run_id} data-testid={`workflow-run-row-${run.run_id}`} style={cardStyle}>
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
                    style={{ cursor: 'pointer' }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <strong style={{ fontSize: 14 }}>{run.name}</strong>
                      <span style={badgeStyle(runStatusTone(run.status))}>
                        {t(RUN_STATUS_KEYS[run.status] ?? run.status)}
                      </span>
                      {dynamicWorkflow && (
                        <span style={badgeStyle('muted')}>
                          {t('workflows.dynamicWorkflow')}
                        </span>
                      )}
                      {run.steps_total > 0 && (
                        <span style={{ color: 'var(--text-secondary)', fontSize: 12 }}>
                          {t('workflows.stepsProgress', { done: run.steps_done, total: run.steps_total })}
                          {run.steps_failed > 0 ? ` · ${t('workflows.stepsFailed', { count: run.steps_failed })}` : ''}
                        </span>
                      )}
                      <span style={{ marginLeft: 'auto', color: 'var(--text-tertiary, var(--text-secondary))', fontSize: 12 }}>
                        {formatTimestamp(run.created_at)}
                      </span>
                    </div>
                    {run.description && (
                      <p style={{ margin: '6px 0 0', color: 'var(--text-secondary)', fontSize: 13 }}>
                        {run.description}
                      </p>
                    )}
                    {dynamicWorkflow && (
                      <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 8, fontSize: 12, color: 'var(--text-secondary)' }}>
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

                  <div style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                    {run.promoted_definition_id ? (
                      <span style={badgeStyle('ok')}>{t('workflows.promoted')}</span>
                    ) : (
                      canManage &&
                      run.status === 'completed' &&
                      run.definition_source === 'ephemeral' && (
                        <button
                          type="button"
                          data-testid={`workflow-promote-${run.run_id}`}
                          onClick={() => promoteMutation.mutate(run.run_id)}
                          disabled={promoteMutation.isPending}
                        >
                          {t('workflows.promote')}
                        </button>
                      )
                    )}
                    {run.status === 'running' && (
                      <button type="button" onClick={() => cancelMutation.mutate(run.run_id)}>
                        {t('workflows.cancel')}
                      </button>
                    )}
                    {canManage && repairPlan?.repairable && (
                      <button
                        type="button"
                        data-testid={`workflow-repair-${run.run_id}`}
                        onClick={() => repairMutation.mutate(run.run_id)}
                        disabled={repairMutation.isPending}
                      >
                        {t('workflows.repair')}
                      </button>
                    )}
                  </div>

                  {expanded && (
                    <div style={{ marginTop: 12, borderTop: '1px solid var(--border-subtle, rgba(128,128,128,0.2))', paddingTop: 10 }}>
                      {!expandedRun ? (
                        <span style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
                          {t('workflows.loadingDetail')}
                        </span>
                      ) : expandedRun.steps.length === 0 ? (
                        <span style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
                          {t('workflows.emptySteps')}
                        </span>
                      ) : (
                        <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 6 }}>
                          {expandedRun.steps.map((step) => (
                            <li key={step.step_id} style={{ fontSize: 13, display: 'flex', gap: 8, alignItems: 'baseline' }}>
                              <span style={badgeStyle(step.status === 'done' ? 'ok' : step.status === 'failed' ? 'error' : 'muted')}>
                                {step.status}
                              </span>
                              <span>{step.step_id}</span>
                              <span style={{ color: 'var(--text-tertiary, var(--text-secondary))', fontSize: 12 }}>
                                {step.step_type}
                              </span>
                              {step.error ? <span style={{ color: 'var(--error)' }}>— {step.error}</span> : null}
                            </li>
                          ))}
                        </ul>
                      )}
                      {expandedRun && expandedRun.leaf_calls.length > 0 && (
                        <div style={{ marginTop: 10 }}>
                          <div style={{ marginBottom: 6, color: 'var(--text-secondary)', fontSize: 12 }}>
                            {t('workflows.leafCalls')}
                          </div>
                          <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 6 }}>
                            {expandedRun.leaf_calls.map((leaf) => (
                              <li
                                key={`${leaf.step_id}:${leaf.leaf_id}`}
                                style={{ fontSize: 13, display: 'flex', gap: 8, alignItems: 'baseline' }}
                              >
                                <span style={badgeStyle(leaf.status === 'done' ? 'ok' : leaf.status === 'failed' ? 'error' : 'muted')}>
                                  {leaf.status}
                                </span>
                                <span>{leaf.step_id}</span>
                                <span style={{ color: 'var(--text-tertiary, var(--text-secondary))', fontSize: 12 }}>
                                  {leaf.leaf_id}
                                </span>
                                {leaf.error ? <span style={{ color: 'var(--error)' }}>— {leaf.error}</span> : null}
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
          style={{
            background: 'none',
            border: 'none',
            padding: 0,
            color: 'var(--text-secondary)',
            fontSize: 13,
            cursor: 'pointer',
          }}
        >
          {advancedOpen ? '▾' : '▸'} {t('workflows.advancedTitle')}
        </button>

        {advancedOpen && (
          <div style={{ marginTop: 12 }}>
            <p style={sectionHintStyle}>{t('workflows.ephemeralHint')}</p>
            <textarea
              data-testid="workflow-definition-input"
              value={definitionText}
              onChange={(event) => setDefinitionText(event.target.value)}
              placeholder='{"name": "...", "steps": [...]}'
              rows={8}
              style={{ width: '100%', fontFamily: 'var(--font-mono, monospace)', fontSize: 12 }}
            />
            <textarea
              data-testid="workflow-args-input"
              value={argsText}
              onChange={(event) => setArgsText(event.target.value)}
              placeholder="{}"
              rows={2}
              style={{ width: '100%', fontFamily: 'var(--font-mono, monospace)', fontSize: 12, marginTop: 8 }}
            />
            <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
              <button
                type="button"
                data-testid="workflow-preview-button"
                onClick={() => previewMutation.mutate()}
                disabled={previewMutation.isPending || !definitionText.trim()}
              >
                {t('workflows.preview')}
              </button>
              <button
                type="button"
                data-testid="workflow-start-button"
                onClick={() => startMutation.mutate()}
                disabled={startMutation.isPending || !preview || (preview.confirmation_required && !startOptions)}
                title={preview?.confirmation_required ? t('workflows.highRiskNeedsPlan') : undefined}
              >
                {t('workflows.confirmAndRun')}
              </button>
            </div>
            {previewError && (
              <div data-testid="workflow-preview-error" style={{ color: 'var(--error)', marginTop: 8, fontSize: 13 }}>
                {previewError}
              </div>
            )}
            {preview && (
              <div data-testid="workflow-preview-card" style={{ marginTop: 12, fontSize: 13 }}>
                <span style={badgeStyle(preview.confirmation_required ? 'warn' : 'ok')}>
                  {t(preview.confirmation_required ? 'workflows.confirmationRequired' : 'workflows.confirmationNotRequired')}
                </span>
                <span style={{ marginLeft: 12 }}>
                  {t('workflows.plannedLeaves', { count: preview.planned_leaf_calls })}
                </span>
                <span style={{ marginLeft: 12, color: 'var(--text-secondary)' }}>
                  hash: {preview.definition_hash.slice(0, 12)}…
                </span>
                {preview.confirmation_required && (
                  <div data-testid="workflow-plan-required" style={{ marginTop: 8, color: 'var(--warning)' }}>
                    {t('workflows.highRiskNeedsPlan')}
                    <ul style={{ margin: '4px 0 0 16px' }}>
                      {preview.confirmation_reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8, marginTop: 8 }}>
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
              <div data-testid="workflow-run-panel" style={{ marginTop: 16 }}>
                <h4 style={{ margin: '0 0 8px', fontSize: 14 }}>{t('workflows.runTitle')}</h4>
                <div style={{ fontSize: 13 }}>
                  <span style={badgeStyle(runStatusTone((lastRunDetail?.status ?? lastRun.status) as WorkflowRunStatus))}>
                    {lastRunDetail?.status ?? lastRun.status}
                  </span>
                  <span style={{ marginLeft: 12, color: 'var(--text-secondary)' }}>run: {lastRun.run_id.slice(0, 8)}…</span>
                  {lastRunDetail?.status === 'running' && (
                    <button type="button" style={{ marginLeft: 12 }} onClick={() => cancelMutation.mutate(lastRun.run_id)}>
                      {t('workflows.cancel')}
                    </button>
                  )}
                </div>
                <ul style={{ marginTop: 8, fontSize: 13 }}>
                  {(lastRunDetail?.steps ?? []).map((step) => (
                    <li key={step.step_id} data-testid={`workflow-step-${step.step_id}`}>
                      <span style={badgeStyle(step.status === 'done' ? 'ok' : step.status === 'failed' ? 'error' : 'muted')}>
                        {step.status}
                      </span>{' '}
                      {step.step_id}
                      {step.error ? <span style={{ color: 'var(--error)' }}> — {step.error}</span> : null}
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
