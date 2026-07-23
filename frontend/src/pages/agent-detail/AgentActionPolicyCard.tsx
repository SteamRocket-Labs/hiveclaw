import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import {
  autonomyApi,
  type OwnerActionId,
  type OwnerActionPolicyActions,
  type OwnerActionZone,
} from '../../api/domains/autonomy';

const DEFAULT_ACTIONS: OwnerActionPolicyActions = {
  'tool.external_effect': 'confirm_first',
  'tool.local_read': 'full_authority',
  'tool.local_write': 'full_authority',
};

const ACTION_ROWS: Array<{
  id: OwnerActionId;
  title: string;
  description: string;
}> = [
  {
    id: 'tool.external_effect',
    title: 'External actions',
    description: 'Messages, posts, deployments, purchases, and other actions visible outside this employee.',
  },
  {
    id: 'tool.local_read',
    title: 'Internal read-only work',
    description: 'Read, search, inspect, and summarize authorized internal information.',
  },
  {
    id: 'tool.local_write',
    title: 'Internal changes',
    description: 'Create or update files and other authorized internal working state.',
  },
];

const ZONE_OPTIONS: Array<{
  value: OwnerActionZone;
  label: string;
}> = [
  { value: 'full_authority', label: 'Do directly' },
  { value: 'confirm_first', label: 'Ask first' },
  { value: 'never_do', label: 'Never do' },
];

const normalizeActions = (
  actions?: Partial<OwnerActionPolicyActions> | null,
): OwnerActionPolicyActions => ({
  ...DEFAULT_ACTIONS,
  ...(actions || {}),
});

export const persistOwnerActionPolicy = (
  agentId: string,
  actions: OwnerActionPolicyActions,
  expectedVersion: number,
) => autonomyApi.updateActionPolicy(agentId, {
  actions,
  expected_version: expectedVersion,
});

export const restorePreviousOwnerActionPolicy = (
  agentId: string,
  targetVersion: number,
  expectedVersion: number,
) => autonomyApi.rollbackActionPolicy(agentId, {
  target_version: targetVersion,
  expected_version: expectedVersion,
  reason: 'Restore previous action policy from employee settings.',
});

export default function AgentActionPolicyCard({
  agentId,
  canManage,
}: {
  agentId: string;
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: policy, isLoading, error } = useQuery({
    queryKey: ['owner-action-policy', agentId],
    queryFn: () => autonomyApi.getActionPolicy(agentId),
    enabled: !!agentId,
  });
  const { data: history } = useQuery({
    queryKey: ['owner-action-policy-history', agentId],
    queryFn: () => autonomyApi.getActionPolicyHistory(agentId),
    enabled: !!agentId && canManage && Boolean(policy?.can_manage),
  });
  const [draft, setDraft] = React.useState<OwnerActionPolicyActions>(() =>
    normalizeActions(policy?.actions),
  );
  const [saving, setSaving] = React.useState(false);
  const [restoring, setRestoring] = React.useState(false);
  const [confirmRestore, setConfirmRestore] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [saveError, setSaveError] = React.useState('');

  React.useEffect(() => {
    if (policy?.actions) {
      setDraft(normalizeActions(policy.actions));
    }
  }, [policy?.actions, policy?.version]);

  const editable = Boolean(canManage && policy?.can_manage);
  const persistedActions = normalizeActions(policy?.actions);
  const hasChanges = ACTION_ROWS.some(({ id }) => draft[id] !== persistedActions[id]);
  const previousVersion = history?.items.find((item) => !item.is_active)?.version ?? null;

  const save = async () => {
    if (!editable || !policy || saving || !hasChanges) return;
    setSaving(true);
    setSaved(false);
    setSaveError('');
    try {
      const updated = await persistOwnerActionPolicy(agentId, draft, policy.version);
      queryClient.setQueryData(['owner-action-policy', agentId], updated);
      setDraft(normalizeActions(updated.actions));
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (cause: any) {
      setSaveError(
        cause?.message
          || t('agent.settings.actionPolicy.saveError', 'Failed to save action policy. Refresh and try again.'),
      );
    } finally {
      setSaving(false);
    }
  };

  const restorePrevious = async () => {
    if (!editable || !policy || previousVersion === null || restoring) return;
    setRestoring(true);
    setSaved(false);
    setSaveError('');
    try {
      const updated = await restorePreviousOwnerActionPolicy(
        agentId,
        previousVersion,
        policy.version,
      );
      queryClient.setQueryData(['owner-action-policy', agentId], updated);
      setDraft(normalizeActions(updated.actions));
      setConfirmRestore(false);
      setSaved(true);
      void queryClient.invalidateQueries({ queryKey: ['owner-action-policy-history', agentId] });
      setTimeout(() => setSaved(false), 2000);
    } catch (cause: any) {
      setSaveError(
        cause?.message
          || t(
            'agent.settings.actionPolicy.restoreError',
            'Failed to restore the previous policy. Refresh and try again.',
          ),
      );
    } finally {
      setRestoring(false);
    }
  };

  return (
    <div className="card agent-settings-card agent-action-policy-card">
      <div className="agent-settings-card-head">
        <div>
          <h4 className="agent-settings-card-title-flush">
            {t('agent.settings.actionPolicy.title', 'Action boundaries')}
          </h4>
          <p className="agent-settings-card-desc agent-settings-card-desc-flush">
            {t(
              'agent.settings.actionPolicy.description',
              'Choose what this employee can do directly, what needs your approval, and what is always prohibited.',
            )}
          </p>
        </div>
        <div className="agent-action-policy-status">
          {saved && (
            <span className="agent-settings-status is-success">
              {t('agent.settings.actionPolicy.saved', 'Policy saved')}
            </span>
          )}
        </div>
      </div>

      {isLoading ? (
        <div className="agent-settings-hint">
          {t('agent.settings.actionPolicy.loading', 'Loading action boundaries...')}
        </div>
      ) : error ? (
        <div className="agent-settings-msg is-error">
          {t('agent.settings.actionPolicy.loadError', 'Action boundaries could not be loaded.')}
        </div>
      ) : (
        <>
          {!policy?.valid && (
            <div className="agent-action-policy-warning" role="alert">
              {t(
                'agent.settings.actionPolicy.invalid',
                'The saved policy needs repair. Effectful actions remain blocked until a manager saves a valid policy.',
              )}
            </div>
          )}
          <div className="agent-action-policy-rows">
            {ACTION_ROWS.map((row) => (
              <div className="agent-action-policy-row" key={row.id}>
                <div className="agent-action-policy-copy">
                  <strong>{t(`agent.settings.actionPolicy.actions.${row.id}.title`, row.title)}</strong>
                  <span>{t(`agent.settings.actionPolicy.actions.${row.id}.description`, row.description)}</span>
                </div>
                {editable ? (
                  <select
                    className="input agent-action-policy-select"
                    name={`owner-action-policy-${row.id}`}
                    aria-label={row.title}
                    value={draft[row.id]}
                    disabled={saving}
                    onChange={(event) => {
                      const zone = event.target.value as OwnerActionZone;
                      setDraft((current) => ({ ...current, [row.id]: zone }));
                      setSaved(false);
                      setSaveError('');
                    }}
                  >
                    {ZONE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {t(`agent.settings.actionPolicy.zones.${option.value}`, option.label)}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className={`agent-action-policy-value is-${draft[row.id]}`}>
                    {t(
                      `agent.settings.actionPolicy.zones.${draft[row.id]}`,
                      ZONE_OPTIONS.find((option) => option.value === draft[row.id])?.label || 'Ask first',
                    )}
                  </span>
                )}
              </div>
            ))}
          </div>
          {saveError && <div className="agent-settings-msg is-error">{saveError}</div>}
          {editable && (
            <div className="agent-action-policy-actions">
              {previousVersion !== null && !confirmRestore && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={saving || restoring}
                  onClick={() => setConfirmRestore(true)}
                >
                  {t('agent.settings.actionPolicy.restorePrevious', 'Restore previous policy')}
                </button>
              )}
              {previousVersion !== null && confirmRestore && (
                <div className="agent-action-policy-restore-confirm" role="group">
                  <span>
                    {t(
                      'agent.settings.actionPolicy.restoreConfirm',
                      'Restore the previous action boundaries as the new current policy?',
                    )}
                  </span>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={restoring}
                    onClick={() => setConfirmRestore(false)}
                  >
                    {t('common.cancel', 'Cancel')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={restoring}
                    onClick={() => void restorePrevious()}
                  >
                    {restoring
                      ? t('agent.settings.actionPolicy.restoring', 'Restoring...')
                      : t('agent.settings.actionPolicy.restore', 'Restore')}
                  </button>
                </div>
              )}
              <button
                type="button"
                className="btn btn-primary"
                disabled={!hasChanges || saving || restoring}
                onClick={() => void save()}
              >
                {saving
                  ? t('agent.settings.actionPolicy.saving', 'Saving...')
                  : t('agent.settings.actionPolicy.save', 'Save action policy')}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
