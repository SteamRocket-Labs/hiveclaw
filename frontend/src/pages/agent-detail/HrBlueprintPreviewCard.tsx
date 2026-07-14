import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { hrCreationApi, type HrCreationDraft } from '../../api/domains/hrCreation';
import type { HrPreviewToolResult } from './toolResultEnvelope';
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
  const provisioningSteps = draft?.provisioning_steps || [];
  const canonicalReady = Boolean(agentId && preview.blueprintId);
  const durableAction = hrCreationActionForStatus(status);

  const confirmMutation = useMutation({
    mutationFn: () => hrCreationApi.confirm(agentId!, preview.blueprintId!, {
      blueprint_version: preview.blueprintVersion,
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
    && preview.missingGates.length === 0
    && durableAction !== 'none';
  const canReviseOrReject = canonicalReady && status === 'awaiting_confirmation';
  const canCancel = canonicalReady && ['confirmed', 'creating', 'provisioning'].includes(status);

  return (
    <article className="hr-blueprint-card" data-status={status}>
      <header>
        <div>
          <span>{t('agent.chat.toolResults.blueprintPreviewTitle', 'Agent Blueprint Preview')}</span>
          <h3>{preview.name || t('agent.chat.toolResults.unnamedAgent', 'Unnamed digital employee')}</h3>
        </div>
        <span className="hr-blueprint-status">{status.replace(/_/g, ' ')}</span>
      </header>

      {preview.mission && <p className="hr-blueprint-mission">{preview.mission}</p>}
      <div className="hr-blueprint-facts">
        {preview.permissionScope && <span>{t('agent.chat.toolResults.permissionScope', 'Access')}: {preview.permissionScope}</span>}
        {preview.riskClass && <span>{t('agent.chat.toolResults.risk', 'Risk')}: {preview.riskClass}</span>}
      </div>
      <PreviewList label={t('agent.chat.toolResults.primaryUsers', 'Primary users')} items={preview.primaryUsers} />
      <PreviewList label={t('agent.chat.toolResults.coreOutputs', 'Core outputs')} items={preview.coreOutputs} />
      {preview.boundaries && (
        <section className="hr-blueprint-boundaries">
          <h4>{t('agent.chat.toolResults.boundaries', 'Boundaries')}</h4>
          <p>{preview.boundaries}</p>
        </section>
      )}
      <PreviewList label={t('agent.chat.toolResults.readyNow', 'Ready now')} items={preview.readyNow} />
      <PreviewList label={t('agent.chat.toolResults.willInstall', 'Will install')} items={preview.willInstall} />
      <PreviewList label={t('agent.chat.toolResults.deferredCapabilities', 'Deferred capabilities')} items={preview.deferredCapabilities} />
      <PreviewList label={t('agent.chat.toolResults.knowledgeDebt', 'Knowledge debt')} items={preview.knowledgeDebt} />
      <PreviewList label={t('agent.chat.toolResults.missingGates', 'Missing gates')} items={preview.missingGates} />
      <PreviewList label={t('agent.chat.toolResults.warnings', 'Warnings')} items={preview.warnings} />
      <PreviewList label={t('agent.chat.toolResults.manualSteps', 'Manual steps')} items={preview.manualSteps} />

      {provisioningSteps.length > 0 && (
        <section className="hr-blueprint-provisioning" aria-label={t('agent.chat.toolResults.provisioningProgress', 'Provisioning progress')}>
          <h4>{t('agent.chat.toolResults.provisioningProgress', 'Provisioning progress')}</h4>
          <ol>
            {provisioningSteps.map((step) => (
              <li key={step.step_key} data-status={step.status}>
                <span>{step.source_key || step.step_kind}</span>
                <strong>{step.status.replace(/_/g, ' ')}</strong>
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
