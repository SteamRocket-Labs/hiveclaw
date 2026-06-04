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
  listWorkflowDefinitions,
  previewWorkflow,
  revokeWorkflowDefinition,
  startWorkflow,
  type WorkflowDefinitionRecord,
  type WorkflowPreview,
  type WorkflowStartResult,
} from '../../api/domains/workflows';

type AgentWorkflowsSectionProps = {
  agentId: string;
  canManage?: boolean;
};

const badgeStyle = (tone: 'ok' | 'warn' | 'error' | 'muted'): React.CSSProperties => ({
  padding: '2px 8px',
  borderRadius: '4px',
  fontSize: '11px',
  fontWeight: 600,
  background:
    tone === 'ok'
      ? 'rgba(0,180,120,0.12)'
      : tone === 'warn'
        ? 'rgba(255,180,0,0.12)'
        : tone === 'error'
          ? 'rgba(255,80,80,0.12)'
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

function definitionTone(status: WorkflowDefinitionRecord['status']): 'ok' | 'warn' | 'error' | 'muted' {
  if (status === 'active') return 'ok';
  if (status === 'draft') return 'warn';
  if (status === 'revoked') return 'error';
  return 'muted';
}

export default function AgentWorkflowsSection({ agentId, canManage = false }: AgentWorkflowsSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [definitionText, setDefinitionText] = useState('');
  const [argsText, setArgsText] = useState('{}');
  const [preview, setPreview] = useState<WorkflowPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<WorkflowStartResult | null>(null);

  const { data: definitions = [] } = useQuery({
    queryKey: ['workflow-definitions', agentId],
    queryFn: () => listWorkflowDefinitions(agentId),
    enabled: !!agentId,
  });

  const { data: runDetail } = useQuery({
    queryKey: ['workflow-run', agentId, lastRun?.run_id],
    queryFn: () => getWorkflowRun(agentId, lastRun!.run_id),
    enabled: !!agentId && !!lastRun?.run_id,
    refetchInterval: 5000,
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
      return startWorkflow(agentId, payload.definition, payload.args);
    },
    onSuccess: (result) => {
      setLastRun(result);
      queryClient.invalidateQueries({ queryKey: ['workflow-run', agentId] });
    },
    onError: (error: unknown) => {
      setPreviewError(error instanceof Error ? error.message : String(error));
    },
  });

  const cancelMutation = useMutation({
    mutationFn: async (runId: string) => cancelWorkflowRun(agentId, runId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workflow-run', agentId] }),
  });

  const lifecycleMutation = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: 'activate' | 'deprecate' | 'revoke' | 'promote' }) => {
      if (action === 'activate') return activateWorkflowDefinition(id);
      if (action === 'deprecate') return deprecateWorkflowDefinition(id);
      if (action === 'revoke') return revokeWorkflowDefinition(id);
      return approveWorkflowPromotion(id);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workflow-definitions', agentId] }),
  });

  const forkMutation = useMutation({
    mutationFn: async (record: WorkflowDefinitionRecord) => forkWorkflowDefinition(record.id, agentId),
    onSuccess: (result) => {
      setDefinitionText(JSON.stringify(result.definition, null, 2));
      setPreview(null);
    },
  });

  return (
    <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 24 }}>
      {/* ── ephemeral launch: preview → confirm → run (§4 第一阶段) ── */}
      <section>
        <h3 style={{ margin: '0 0 8px' }}>{t('workflows.ephemeralTitle')}</h3>
        <p style={{ margin: '0 0 12px', color: 'var(--text-secondary)', fontSize: 13 }}>
          {t('workflows.ephemeralHint')}
        </p>
        <textarea
          data-testid="workflow-definition-input"
          value={definitionText}
          onChange={(event) => setDefinitionText(event.target.value)}
          placeholder='{"name": "...", "steps": [...]}'
          rows={8}
          style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
        />
        <textarea
          data-testid="workflow-args-input"
          value={argsText}
          onChange={(event) => setArgsText(event.target.value)}
          placeholder="{}"
          rows={2}
          style={{ width: '100%', fontFamily: 'monospace', fontSize: 12, marginTop: 8 }}
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
            disabled={startMutation.isPending || !preview || preview.risk === 'high'}
            title={preview?.risk === 'high' ? t('workflows.highRiskNeedsPlan') : undefined}
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
            <span style={badgeStyle(preview.risk === 'high' ? 'warn' : 'ok')}>
              {t(preview.risk === 'high' ? 'workflows.riskHigh' : 'workflows.riskLow')}
            </span>
            <span style={{ marginLeft: 12 }}>
              {t('workflows.plannedLeaves', { count: preview.planned_leaf_calls })}
            </span>
            <span style={{ marginLeft: 12, color: 'var(--text-secondary)' }}>
              hash: {preview.definition_hash.slice(0, 12)}…
            </span>
            {preview.risk === 'high' && (
              <div data-testid="workflow-plan-required" style={{ marginTop: 8, color: 'var(--warning)' }}>
                {t('workflows.highRiskNeedsPlan')}
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {preview.risk_reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </section>

      {/* ── run progress + cancel ── */}
      {lastRun && (
        <section data-testid="workflow-run-panel">
          <h3 style={{ margin: '0 0 8px' }}>{t('workflows.runTitle')}</h3>
          <div style={{ fontSize: 13 }}>
            <span style={badgeStyle(runDetail?.status === 'completed' ? 'ok' : runDetail?.status === 'failed' ? 'error' : 'warn')}>
              {runDetail?.status ?? lastRun.status}
            </span>
            <span style={{ marginLeft: 12, color: 'var(--text-secondary)' }}>run: {lastRun.run_id.slice(0, 8)}…</span>
            {runDetail?.status === 'running' && (
              <button type="button" style={{ marginLeft: 12 }} onClick={() => cancelMutation.mutate(lastRun.run_id)}>
                {t('workflows.cancel')}
              </button>
            )}
          </div>
          <ul style={{ marginTop: 8, fontSize: 13 }}>
            {(runDetail?.steps ?? []).map((step) => (
              <li key={step.step_id} data-testid={`workflow-step-${step.step_id}`}>
                <span style={badgeStyle(step.status === 'done' ? 'ok' : step.status === 'failed' ? 'error' : 'muted')}>
                  {step.status}
                </span>{' '}
                {step.step_id}
                {step.error ? <span style={{ color: 'var(--error)' }}> — {step.error}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* ── registered templates (§4 第二/三阶段) ── */}
      <section>
        <h3 style={{ margin: '0 0 8px' }}>{t('workflows.registeredTitle')}</h3>
        {definitions.length === 0 ? (
          <p style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{t('workflows.noDefinitions')}</p>
        ) : (
          <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ textAlign: 'left', color: 'var(--text-secondary)' }}>
                <th>{t('workflows.colName')}</th>
                <th>v</th>
                <th>{t('workflows.colStatus')}</th>
                <th>{t('workflows.colVisibility')}</th>
                <th>hash</th>
                {canManage && <th />}
              </tr>
            </thead>
            <tbody>
              {definitions.map((record) => (
                <tr key={record.id} data-testid={`workflow-definition-${record.name}-v${record.definition_version}`}>
                  <td>{record.name}</td>
                  <td>{record.definition_version}</td>
                  <td>
                    <span style={badgeStyle(definitionTone(record.status))}>{record.status}</span>
                  </td>
                  <td>{record.visibility_scope}</td>
                  <td style={{ color: 'var(--text-secondary)' }}>{record.definition_hash.slice(0, 10)}…</td>
                  {canManage && (
                    <td style={{ whiteSpace: 'nowrap' }}>
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
                          </button>{' '}
                          <button
                            type="button"
                            onClick={() => lifecycleMutation.mutate({ id: record.id, action: 'deprecate' })}
                          >
                            {t('workflows.deprecate')}
                          </button>
                        </>
                      )}
                      {record.status !== 'revoked' && (
                        <>
                          {' '}
                          <button
                            type="button"
                            onClick={() => lifecycleMutation.mutate({ id: record.id, action: 'revoke' })}
                          >
                            {t('workflows.revoke')}
                          </button>
                        </>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
