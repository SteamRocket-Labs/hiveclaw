import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { hrCreationApi, type HrCreationDraft } from '../../api/domains/hrCreation';
import { parsePreviewAgentBlueprintResult, type HrPreviewToolResult } from './toolResultEnvelope';
import './HrBlueprintPreviewCard.css';

interface HrBlueprintPreviewCardProps {
  agentId?: string;
  preview: HrPreviewToolResult;
  onSendMessage?: (text: string) => void | Promise<unknown>;
}

function displayItem(value: unknown): string {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return String(value ?? '');
  const record = value as Record<string, unknown>;
  return String(record.value_summary || record.reason || record.field || JSON.stringify(record));
}

function initialDraft(preview: HrPreviewToolResult): HrCreationDraft | undefined {
  if (!preview.blueprintId) return undefined;
  return {
    blueprint_id: preview.blueprintId,
    blueprint_version: preview.blueprintVersion,
    blueprint_hash: preview.blueprintHash || '',
    draft_status: preview.status,
    blueprint: {},
  };
}

export type HrCreationAction = 'confirm' | 'retry' | 'none';

export function hrCreationActionForStatus(status: string): HrCreationAction {
  if (status === 'awaiting_confirmation') return 'confirm';
  if (status === 'failed' || status === 'provisioning') return 'retry';
  return 'none';
}

function PreviewList({ label, items }: { label: string; items: unknown[] }) {
  const visible = items.map(displayItem).filter(Boolean);
  if (visible.length === 0) return null;
  return (
    <section className="hr-blueprint-list">
      <h4>{label}</h4>
      <ul>
        {visible.map((item) => <li key={`${label}-${item}`}>{item}</li>)}
      </ul>
    </section>
  );
}

type Translate = (key: string, fallback: string) => string;

function sourceAuthorityLabel(sourceType: unknown, t: Translate): string {
  switch (sourceType) {
    case 'confirmed_by_user':
      return t('agent.chat.toolResults.sourceConfirmedByUser', 'User confirmed');
    case 'supported_by_company_kb':
      return t('agent.chat.toolResults.sourceCompanyKnowledge', 'Company knowledge');
    case 'suggested_by_history':
      return t('agent.chat.toolResults.sourceHistorySuggestion', 'Past-work suggestion');
    case 'suggested_by_general_knowledge':
      return t('agent.chat.toolResults.sourceGeneralKnowledge', 'Role best practice');
    default:
      return t('agent.chat.toolResults.sourceNeedsReview', 'Source needs review');
  }
}

function SourceAttributionList({ items, t }: { items: Record<string, unknown>[]; t: Translate }) {
  const visible = items.map((item) => ({
    authority: sourceAuthorityLabel(item.source_type, t),
    summary: typeof item.value_summary === 'string' && item.value_summary.trim()
      ? item.value_summary.trim()
      : t('agent.chat.toolResults.sourceFieldFallback', 'Blueprint detail'),
  }));
  if (visible.length === 0) return null;
  return (
    <section className="hr-blueprint-list hr-blueprint-sources">
      <h4>{t('agent.chat.toolResults.sources', 'Sources')}</h4>
      <ul>
        {visible.map((item, index) => (
          <li key={`${item.authority}-${item.summary}-${index}`}>
            <strong>{item.authority}</strong>
            <span>{item.summary}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function blueprintStatusLabel(status: string, t: Translate): string {
  switch (status) {
    case 'awaiting_confirmation':
      return t('agent.chat.toolResults.statusNeedsConfirmation', 'Needs confirmation');
    case 'confirmed':
      return t('agent.chat.toolResults.statusConfirmed', 'Confirmed');
    case 'creating':
    case 'provisioning':
      return t('agent.chat.toolResults.statusProvisioning', 'Creating');
    case 'completed':
      return t('agent.chat.toolResults.statusCompleted', 'Created');
    case 'failed':
      return t('agent.chat.toolResults.statusFailed', 'Needs attention');
    case 'rejected':
      return t('agent.chat.toolResults.statusRejected', 'Rejected');
    case 'superseded':
      return t('agent.chat.toolResults.statusSuperseded', 'Revised');
    case 'expired':
      return t('agent.chat.toolResults.statusExpired', 'Expired');
    default:
      return t('agent.chat.toolResults.statusUnavailable', 'Status unavailable');
  }
}

function permissionScopeLabel(scope: string | null, t: Translate): string | null {
  if (!scope) return null;
  if (scope === 'company') return t('agent.chat.toolResults.scopeCompany', 'Company');
  if (scope === 'self') return t('agent.chat.toolResults.scopeSelf', 'Only me');
  return t('agent.chat.toolResults.scopeUnavailable', 'Access needs review');
}

function riskLabel(risk: string | null, t: Translate): string | null {
  if (!risk) return null;
  if (risk === 'standard') return t('agent.chat.toolResults.riskStandard', 'Standard');
  if (risk === 'high') return t('agent.chat.toolResults.riskHigh', 'High');
  return t('agent.chat.toolResults.riskUnavailable', 'Needs review');
}

type ProvisioningStep = NonNullable<HrCreationDraft['provisioning_steps']>[number];

function provisioningStepLabel(step: ProvisioningStep, t: Translate): string {
  switch (step.step_kind) {
    case 'workspace':
      return t('agent.chat.toolResults.stepWorkspace', 'Workspace');
    case 'core':
      return t('agent.chat.toolResults.stepCoreSetup', 'Core setup');
    case 'finalize':
      return t('agent.chat.toolResults.stepFinalize', 'Final checks');
    case 'mcp_server':
      return t('agent.chat.toolResults.stepMcpConnection', 'MCP connection');
    case 'skill':
      return t('agent.chat.toolResults.stepSkill', 'Skill setup');
    case 'external_skill':
      return t('agent.chat.toolResults.stepExternalSkill', 'External skill review');
    case 'clawhub':
      return t('agent.chat.toolResults.stepClawhub', 'ClawHub skill');
    default:
      return t('agent.chat.toolResults.stepSetup', 'Setup step');
  }
}

function provisioningStatusLabel(status: string, t: Translate): string {
  switch (status) {
    case 'pending':
      return t('agent.chat.toolResults.stepPending', 'Pending');
    case 'running':
      return t('agent.chat.toolResults.stepRunning', 'In progress');
    case 'completed':
      return t('agent.chat.toolResults.stepCompleted', 'Completed');
    case 'failed':
      return t('agent.chat.toolResults.stepFailed', 'Failed');
    case 'skipped':
      return t('agent.chat.toolResults.stepSkipped', 'Skipped');
    default:
      return t('agent.chat.toolResults.statusUnavailable', 'Status unavailable');
  }
}

export function HrBlueprintPreviewCard({ agentId, preview, onSendMessage }: HrBlueprintPreviewCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);
  const queryKey = ['hr-creation-draft', agentId, preview.blueprintId] as const;
  const canLoad = Boolean(agentId && preview.blueprintId);
  const { data: draft } = useQuery({
    queryKey,
    queryFn: () => hrCreationApi.get(agentId!, preview.blueprintId!),
    enabled: canLoad,
    initialData: initialDraft(preview),
    initialDataUpdatedAt: 0,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => {
      const currentStatus = (query.state.data as HrCreationDraft | undefined)?.draft_status;
      return currentStatus && ['confirmed', 'creating', 'provisioning'].includes(currentStatus) ? 1_500 : false;
    },
  });
  const status = draft?.draft_status || preview.status;
  const canonicalPreview = draft && Object.keys(draft.blueprint || {}).length > 0
    ? parsePreviewAgentBlueprintResult(draft) || preview
    : preview;
  const statusLabel = blueprintStatusLabel(status, t);
  const scopeLabel = permissionScopeLabel(canonicalPreview.permissionScope, t);
  const reviewRiskLabel = riskLabel(canonicalPreview.riskClass, t);
  const provisioningSteps = draft?.provisioning_steps || [];
  const canonicalReady = Boolean(agentId && preview.blueprintId);
  const durableAction = hrCreationActionForStatus(status);

  const confirmMutation = useMutation({
    mutationFn: () => hrCreationApi.confirm(agentId!, preview.blueprintId!, {
      blueprint_version: draft?.blueprint_version || preview.blueprintVersion,
    }),
    onSuccess: (nextDraft) => queryClient.setQueryData(queryKey, nextDraft),
    onError: () => queryClient.invalidateQueries({ queryKey }),
  });
  const rejectMutation = useMutation({
    mutationFn: () => hrCreationApi.reject(agentId!, preview.blueprintId!),
    onSuccess: (nextDraft) => queryClient.setQueryData(queryKey, nextDraft),
  });
  const retryMutation = useMutation({
    mutationFn: () => hrCreationApi.retry(agentId!, preview.blueprintId!),
    onSuccess: (nextDraft) => queryClient.setQueryData(queryKey, nextDraft),
  });
  const cancelMutation = useMutation({
    mutationFn: () => hrCreationApi.cancel(agentId!, preview.blueprintId!),
    onSuccess: (nextDraft) => queryClient.setQueryData(queryKey, nextDraft),
  });

  const runDurableAction = async () => {
    setActionError(null);
    try {
      if (durableAction === 'confirm') {
        await confirmMutation.mutateAsync();
      } else if (durableAction === 'retry') {
        await retryMutation.mutateAsync();
      }
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    }
  };
  const requestChanges = async () => {
    setActionError(null);
    try {
      await onSendMessage?.(
        'I want to revise this HR blueprint. Ask what I want to change, then revise the existing canonical draft instead of creating a new employee.',
      );
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    }
  };
  const reject = async () => {
    setActionError(null);
    try {
      await rejectMutation.mutateAsync();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    }
  };
  const cancel = async () => {
    setActionError(null);
    try {
      await cancelMutation.mutateAsync();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    }
  };
  const busy = confirmMutation.isPending
    || rejectMutation.isPending
    || retryMutation.isPending
    || cancelMutation.isPending;
  const canStartCreation = canonicalReady
    && canonicalPreview.missingGates.length === 0
    && durableAction !== 'none';
  const canReviseOrReject = canonicalReady && status === 'awaiting_confirmation';
  const canCancel = canonicalReady && ['confirmed', 'creating', 'provisioning'].includes(status);

  return (
    <article className="hr-blueprint-card">
      <header>
        <div>
          <span>{t('agent.chat.toolResults.blueprintPreviewTitle', 'Agent Blueprint Preview')}</span>
          <h3>{canonicalPreview.name || t('agent.chat.toolResults.unnamedAgent', 'Unnamed digital employee')}</h3>
        </div>
        <span className="hr-blueprint-status">{statusLabel}</span>
      </header>

      {canonicalPreview.mission && <p className="hr-blueprint-mission">{canonicalPreview.mission}</p>}
      <div className="hr-blueprint-facts">
        {scopeLabel && <span>{t('agent.chat.toolResults.permissionScope', 'Access')}: {scopeLabel}</span>}
        {reviewRiskLabel && <span>{t('agent.chat.toolResults.risk', 'Risk')}: {reviewRiskLabel}</span>}
      </div>
      <PreviewList label={t('agent.chat.toolResults.primaryUsers', 'Primary users')} items={canonicalPreview.primaryUsers} />
      <PreviewList label={t('agent.chat.toolResults.coreOutputs', 'Core outputs')} items={canonicalPreview.coreOutputs} />
      {canonicalPreview.firstMission && (
        <section className="hr-blueprint-boundaries">
          <h4>{t('agent.chat.toolResults.firstMission', 'First mission')}</h4>
          <p>{canonicalPreview.firstMission}</p>
        </section>
      )}
      {canonicalPreview.boundaries && (
        <section className="hr-blueprint-boundaries">
          <h4>{t('agent.chat.toolResults.boundaries', 'Boundaries')}</h4>
          <p>{canonicalPreview.boundaries}</p>
        </section>
      )}
      <PreviewList label={t('agent.chat.toolResults.readyNow', 'Ready now')} items={canonicalPreview.readyNow} />
      <PreviewList label={t('agent.chat.toolResults.willInstall', 'Will install')} items={canonicalPreview.willInstall} />
      <PreviewList label={t('agent.chat.toolResults.deferredCapabilities', 'Deferred capabilities')} items={canonicalPreview.deferredCapabilities} />
      <SourceAttributionList items={canonicalPreview.sourceAttributions} t={t} />
      <PreviewList label={t('agent.chat.toolResults.knowledgeDebt', 'Knowledge debt')} items={canonicalPreview.knowledgeDebt} />
      <PreviewList label={t('agent.chat.toolResults.missingGates', 'Missing gates')} items={canonicalPreview.missingGates} />
      <PreviewList label={t('agent.chat.toolResults.warnings', 'Warnings')} items={canonicalPreview.warnings} />
      <PreviewList label={t('agent.chat.toolResults.manualSteps', 'Manual steps')} items={canonicalPreview.manualSteps} />

      {provisioningSteps.length > 0 && (
        <section className="hr-blueprint-provisioning" aria-label={t('agent.chat.toolResults.provisioningProgress', 'Provisioning progress')}>
          <h4>{t('agent.chat.toolResults.provisioningProgress', 'Provisioning progress')}</h4>
          <ol>
            {provisioningSteps.map((step) => (
              <li key={step.step_key}>
                <span>{provisioningStepLabel(step, t)}</span>
                <strong>{provisioningStatusLabel(step.status, t)}</strong>
                {step.required && <em>{t('common.required', 'Required')}</em>}
                {step.error_message && <small>{step.error_message}</small>}
              </li>
            ))}
          </ol>
        </section>
      )}

      {!canonicalReady && (
        <p className="hr-blueprint-error">
          {t('agent.chat.toolResults.legacyBlueprint', 'This legacy preview cannot be confirmed. Ask HR to generate a fresh preview.')}
        </p>
      )}
      {actionError && <p className="hr-blueprint-error" role="alert">{actionError}</p>}
      <footer>
        <button type="button" className="btn btn-primary" disabled={!canStartCreation || busy} onClick={runDurableAction}>
          {confirmMutation.isPending
            ? t('common.confirming', 'Confirming…')
            : retryMutation.isPending
              ? t('agent.chat.toolResults.retryingProvisioning', 'Retrying…')
              : durableAction === 'retry'
                ? t('agent.chat.toolResults.retryProvisioning', 'Retry provisioning')
                : durableAction === 'confirm'
                  ? t('agent.chat.toolResults.confirmAndCreate', 'Confirm & create')
                  : ['confirmed', 'creating'].includes(status)
                    ? t('agent.chat.toolResults.provisioning', 'Provisioning…')
                    : t('agent.chat.toolResults.provisioned', 'Provisioned')}
        </button>
        <button type="button" className="btn btn-secondary" disabled={!canReviseOrReject || busy || !onSendMessage} onClick={requestChanges}>
          {t('agent.chat.toolResults.requestChanges', 'Request changes')}
        </button>
        <button type="button" className="btn btn-ghost" disabled={!canReviseOrReject || busy} onClick={reject}>
          {t('common.reject', 'Reject')}
        </button>
        {canCancel && (
          <button type="button" className="btn btn-ghost" disabled={busy} onClick={cancel}>
            {cancelMutation.isPending
              ? t('common.cancelling', 'Cancelling…')
              : t('agent.chat.toolResults.cancelProvisioning', 'Cancel provisioning')}
          </button>
        )}
      </footer>
    </article>
  );
}
