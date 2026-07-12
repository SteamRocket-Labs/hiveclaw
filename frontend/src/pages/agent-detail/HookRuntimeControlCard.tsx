import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { ccParityApi } from '../../api/domains/ccParity';

type Props = {
  agentId: string;
  canManage: boolean;
};

export const updateHookEnabled = (agentId: string, hookKey: string, enabled: boolean) =>
  ccParityApi.updateHookRuntimeConfig(agentId, hookKey, { enabled });

export default function HookRuntimeControlCard({ agentId, canManage }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [busyKey, setBusyKey] = React.useState<string | null>(null);
  const [mutationError, setMutationError] = React.useState('');
  const { data, isLoading, error } = useQuery({
    queryKey: ['agent-hooks', agentId],
    queryFn: () => ccParityApi.listHooks(agentId),
    enabled: Boolean(agentId),
    refetchInterval: 15_000,
  });

  const registrations = data?.registrations ?? [];
  const failedReceipts = (data?.recent_receipts ?? []).filter((receipt) =>
    ['error', 'failed', 'timeout'].includes(receipt.status),
  );

  const setEnabled = async (key: string, enabled: boolean) => {
    setBusyKey(key);
    setMutationError('');
    try {
      await updateHookEnabled(agentId, key, enabled);
      await queryClient.invalidateQueries({ queryKey: ['agent-hooks', agentId] });
    } catch (nextError) {
      setMutationError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <section className="card agent-settings-card" data-testid="hook-runtime-control">
      <div className="agent-settings-card-head">
        <div>
          <h4 className="agent-settings-card-title-flush">
            {t('agent.settings.hooks.title', 'Runtime hooks')}
          </h4>
          <p className="agent-settings-card-desc agent-settings-card-desc-flush">
            {t(
              'agent.settings.hooks.description',
              'Required hooks stop the turn when their policy dependency fails. Advisory hooks record the failure and continue.',
            )}
          </p>
        </div>
      </div>

      {isLoading && <div className="agent-settings-hint">{t('common.loading', 'Loading…')}</div>}
      {error && (
        <div className="agent-settings-hint agent-settings-hint-error">
          {t('agent.settings.hooks.loadError', 'Hook status could not be loaded. Runtime defaults remain enforced.')}
        </div>
      )}
      {mutationError && <div className="agent-settings-hint agent-settings-hint-error">{mutationError}</div>}

      <div className="agent-settings-hook-list">
        {registrations.map((registration) => {
          const key = String(registration.key ?? '');
          const config = registration.runtime_config ?? {};
          const enabled = config.enabled !== false;
          const mode = config.effective_failure_mode ?? registration.failure_mode ?? 'advisory';
          return (
            <div className="agent-settings-inset-row" key={`${registration.event}:${key || registration.handler_name}`}>
              <div className="agent-settings-inset-grow">
                <div className="agent-settings-inset-name">{registration.handler_name}</div>
                <div className="agent-settings-inset-desc">
                  {registration.event} · {mode === 'required'
                    ? t('agent.settings.hooks.required', 'Required blocker')
                    : t('agent.settings.hooks.advisory', 'Advisory observer')}
                  {!enabled ? ` · ${t('agent.settings.hooks.disabled', 'Disabled by manager')}` : ''}
                </div>
                {config.migration_preview && (
                  <div className="agent-settings-hint">
                    {t('agent.settings.hooks.migrated', 'Legacy continue policy now inherits the typed registration default.')}
                  </div>
                )}
              </div>
              {canManage && key && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={busyKey === key}
                  onClick={() => void setEnabled(key, !enabled)}
                >
                  {enabled
                    ? t('agent.settings.hooks.disable', 'Disable hook')
                    : t('agent.settings.hooks.enable', 'Enable hook')}
                </button>
              )}
            </div>
          );
        })}
      </div>

      {failedReceipts.length > 0 && (
        <div className="agent-settings-hook-receipts">
          <strong>{t('agent.settings.hooks.recentFailures', 'Recent hook failures')}</strong>
          {failedReceipts.map((receipt) => (
            <div className="agent-settings-hook-receipt" key={receipt.id}>
              <span>{receipt.hook_key}: {receipt.error || receipt.status}</span>
              {receipt.retryable && (
                <small>{t('agent.settings.hooks.retryTurn', 'Retry the original turn after recovery.')}</small>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
