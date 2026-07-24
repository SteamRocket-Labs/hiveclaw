import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import {
  governanceApi,
  type FeatureFlag,
  type FeatureFlagType,
  type FeatureFlagWrite,
} from '../../api/domains/governance';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import './PlatformFeatureFlagsSection.css';

export interface FeatureFlagOverrideDraft {
  scope: 'tenant' | 'user';
  id: string;
  enabled: boolean;
}

export interface FeatureFlagDraft {
  key: string;
  description: string;
  flagType: FeatureFlagType;
  enabled: boolean;
  rolloutPercentage: string;
  allowedTenantIds: string;
  allowedUserIds: string;
  expiresAt: string;
  overrides: FeatureFlagOverrideDraft[];
}

type NormalizedFeatureFlagWrite = Required<
  Pick<FeatureFlagWrite, 'description' | 'flag_type' | 'enabled'>
> & {
  key: string;
  rollout_percentage: number | null;
  allowed_tenant_ids: string[] | null;
  allowed_user_ids: string[] | null;
  overrides: Record<string, boolean> | null;
  expires_at: string | null;
};

interface PlatformFeatureFlagsSectionProps {
  initialFlags?: FeatureFlag[];
}

const EMPTY_DRAFT: FeatureFlagDraft = {
  key: '',
  description: '',
  flagType: 'boolean',
  enabled: false,
  rolloutPercentage: '',
  allowedTenantIds: '',
  allowedUserIds: '',
  expiresAt: '',
  overrides: [],
};

function splitIdentifiers(value: string): string[] | null {
  const identifiers = value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return identifiers.length > 0 ? Array.from(new Set(identifiers)) : null;
}

export function normalizeFeatureFlagDraft(
  draft: FeatureFlagDraft,
): NormalizedFeatureFlagWrite {
  const overrides = draft.overrides.reduce<Record<string, boolean>>((result, item) => {
    const id = item.id.trim();
    if (id) result[`${item.scope}:${id}`] = item.enabled;
    return result;
  }, {});
  const percentage = draft.rolloutPercentage.trim();
  return {
    key: draft.key.trim(),
    description: draft.description.trim(),
    flag_type: draft.flagType,
    enabled: draft.enabled,
    rollout_percentage: percentage ? Number(percentage) : null,
    allowed_tenant_ids: splitIdentifiers(draft.allowedTenantIds),
    allowed_user_ids: splitIdentifiers(draft.allowedUserIds),
    overrides: Object.keys(overrides).length > 0 ? overrides : null,
    expires_at: draft.expiresAt
      ? new Date(draft.expiresAt).toISOString()
      : null,
  };
}

type AudienceTranslator = (key: string, fallback: string) => string;

export function featureFlagAudienceSummary(
  flag: FeatureFlag,
  translate: AudienceTranslator = (_key, fallback) => fallback,
): string[] {
  const summary: string[] = [];
  if (flag.flag_type === 'percentage') {
    const percentage = flag.rollout_percentage ?? 0;
    summary.push(
      `${percentage}% ${translate('featureRollout.audience.percentage', 'deterministic rollout')}`,
    );
  } else if (flag.flag_type === 'tenant_gate') {
    const count = flag.allowed_tenant_ids?.length ?? 0;
    summary.push(
      `${count} ${translate(
        count === 1 ? 'featureRollout.audience.oneTenant' : 'featureRollout.audience.tenants',
        `allowed tenant${count === 1 ? '' : 's'}`,
      )}`,
    );
  } else if (flag.flag_type === 'allowlist') {
    const count = (flag.allowed_tenant_ids?.length ?? 0) + (flag.allowed_user_ids?.length ?? 0);
    summary.push(
      `${count} ${translate(
        count === 1 ? 'featureRollout.audience.oneSubject' : 'featureRollout.audience.subjects',
        `allowed subject${count === 1 ? '' : 's'}`,
      )}`,
    );
  } else {
    summary.push(flag.enabled
      ? translate('featureRollout.audience.everyone', 'Enabled for everyone')
      : translate('featureRollout.audience.disabled', 'Disabled'));
  }
  const overrideCount = Object.keys(flag.overrides ?? {}).length;
  if (overrideCount > 0) {
    summary.push(
      `${overrideCount} ${translate(
        overrideCount === 1
          ? 'featureRollout.audience.oneOverride'
          : 'featureRollout.audience.overrides',
        `explicit override${overrideCount === 1 ? '' : 's'}`,
      )}`,
    );
  }
  return summary;
}

function draftFromFlag(flag: FeatureFlag): FeatureFlagDraft {
  const overrides = Object.entries(flag.overrides ?? {}).flatMap<FeatureFlagOverrideDraft>(([key, enabled]) => {
    const separator = key.indexOf(':');
    if (separator <= 0) return [];
    const scope = key.slice(0, separator);
    if (scope !== 'tenant' && scope !== 'user') return [];
    return [{
      scope,
      id: key.slice(separator + 1),
      enabled,
    }];
  });
  return {
    key: flag.key,
    description: flag.description,
    flagType: flag.flag_type,
    enabled: flag.enabled,
    rolloutPercentage: flag.rollout_percentage === null ? '' : String(flag.rollout_percentage),
    allowedTenantIds: (flag.allowed_tenant_ids ?? []).join('\n'),
    allowedUserIds: (flag.allowed_user_ids ?? []).join('\n'),
    expiresAt: flag.expires_at ? new Date(flag.expires_at).toISOString().slice(0, 16) : '',
    overrides,
  };
}

function FeatureFlagEditor({
  flag,
  onClose,
}: {
  flag: FeatureFlag | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<FeatureFlagDraft>(
    flag ? draftFromFlag(flag) : EMPTY_DRAFT,
  );

  const saveMutation = useMutation({
    mutationFn: async () => {
      const payload = normalizeFeatureFlagDraft(draft);
      if (!payload.key) throw new Error(t('featureRollout.keyRequired', 'A flag key is required.'));
      const percentage = payload.rollout_percentage;
      if (
        payload.flag_type === 'percentage'
        && (percentage === null
          || percentage < 0
          || percentage > 100)
      ) {
        throw new Error(t('featureRollout.percentageInvalid', 'Rollout must be between 0 and 100.'));
      }
      if (flag) {
        if (!flag.updated_at) {
          throw new Error(t('featureRollout.versionMissing', 'The current rollout version is unavailable.'));
        }
        const { key: _key, ...update } = payload;
        return governanceApi.updateFeatureFlag(flag.id, {
          ...update,
          expected_updated_at: flag.updated_at,
        });
      }
      return governanceApi.createFeatureFlag(payload);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-feature-flags'] });
      showAppToast(t('featureRollout.saved', 'Feature rollout saved'), 'success');
      onClose();
    },
    onError: (error: Error) => {
      showAppToast(error.message || t('featureRollout.saveFailed', 'Could not save rollout'), 'error');
    },
  });

  const updateDraft = <Key extends keyof FeatureFlagDraft>(
    key: Key,
    value: FeatureFlagDraft[Key],
  ) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const updateOverride = (
    index: number,
    update: Partial<FeatureFlagOverrideDraft>,
  ) => {
    setDraft((current) => ({
      ...current,
      overrides: current.overrides.map((item, itemIndex) => (
        itemIndex === index ? { ...item, ...update } : item
      )),
    }));
  };

  return (
    <div className="feature-rollout-editor">
      <div className="feature-rollout-editor-header">
        <div>
          <h4>
            {flag
              ? t('featureRollout.editTitle', 'Edit rollout')
              : t('featureRollout.createTitle', 'Create rollout')}
          </h4>
          <p>
            {t(
              'featureRollout.editorDescription',
              'Changes apply globally after the platform audit receipt is recorded.',
            )}
          </p>
        </div>
        <button type="button" className="btn btn-ghost" onClick={onClose}>
          {t('common.cancel', 'Cancel')}
        </button>
      </div>

      <div className="feature-rollout-form-grid">
        <label>
          {t('featureRollout.key', 'Flag key')}
          <input
            className="form-input"
            value={draft.key}
            disabled={Boolean(flag)}
            placeholder="runtime_continuity_v1"
            onChange={(event) => updateDraft('key', event.target.value)}
          />
        </label>
        <label>
          {t('featureRollout.mode', 'Targeting mode')}
          <select
            className="form-input"
            value={draft.flagType}
            onChange={(event) => updateDraft('flagType', event.target.value as FeatureFlagType)}
          >
            <option value="boolean">{t('featureRollout.modes.boolean', 'Everyone on or off')}</option>
            <option value="percentage">{t('featureRollout.modes.percentage', 'Percentage rollout')}</option>
            <option value="tenant_gate">{t('featureRollout.modes.tenantGate', 'Selected companies')}</option>
            <option value="allowlist">{t('featureRollout.modes.allowlist', 'Selected companies and users')}</option>
          </select>
        </label>
      </div>

      <label>
        {t('featureRollout.description', 'Operator description')}
        <textarea
          className="form-input"
          rows={2}
          value={draft.description}
          onChange={(event) => updateDraft('description', event.target.value)}
        />
      </label>

      {draft.flagType === 'boolean' ? (
        <label className="feature-rollout-toggle">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) => updateDraft('enabled', event.target.checked)}
          />
          <span>{t('featureRollout.enabledGlobally', 'Enabled for everyone')}</span>
        </label>
      ) : null}

      {draft.flagType === 'percentage' ? (
        <label>
          {t('featureRollout.percentage', 'Rollout percentage')}
          <input
            className="form-input"
            type="number"
            min="0"
            max="100"
            value={draft.rolloutPercentage}
            onChange={(event) => updateDraft('rolloutPercentage', event.target.value)}
          />
        </label>
      ) : null}

      {draft.flagType === 'tenant_gate' || draft.flagType === 'allowlist' ? (
        <label>
          {t('featureRollout.tenantIds', 'Company identifiers')}
          <textarea
            className="form-input feature-rollout-identifiers"
            rows={3}
            value={draft.allowedTenantIds}
            placeholder={t('featureRollout.onePerLine', 'One identifier per line')}
            onChange={(event) => updateDraft('allowedTenantIds', event.target.value)}
          />
        </label>
      ) : null}

      {draft.flagType === 'allowlist' ? (
        <label>
          {t('featureRollout.userIds', 'User identifiers')}
          <textarea
            className="form-input feature-rollout-identifiers"
            rows={3}
            value={draft.allowedUserIds}
            placeholder={t('featureRollout.onePerLine', 'One identifier per line')}
            onChange={(event) => updateDraft('allowedUserIds', event.target.value)}
          />
        </label>
      ) : null}

      <label>
        {t('featureRollout.expiresAt', 'Automatic expiry')}
        <input
          className="form-input"
          type="datetime-local"
          value={draft.expiresAt}
          onChange={(event) => updateDraft('expiresAt', event.target.value)}
        />
        <small>
          {t(
            'featureRollout.expiresAtDesc',
            'Leave empty for no expiry. Expired flags evaluate as off.',
          )}
        </small>
      </label>

      <div className="feature-rollout-overrides">
        <div className="feature-rollout-overrides-header">
          <div>
            <strong>{t('featureRollout.overrides', 'Explicit overrides')}</strong>
            <small>
              {t(
                'featureRollout.overridesDesc',
                'Force one company or user on or off without editing raw configuration.',
              )}
            </small>
          </div>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => updateDraft('overrides', [
              ...draft.overrides,
              { scope: 'tenant', id: '', enabled: true },
            ])}
          >
            {t('featureRollout.addOverride', 'Add override')}
          </button>
        </div>
        {draft.overrides.map((override, index) => (
          <div className="feature-rollout-override-row" key={`${index}:${override.scope}`}>
            <select
              className="form-input"
              value={override.scope}
              onChange={(event) => updateOverride(
                index,
                { scope: event.target.value as FeatureFlagOverrideDraft['scope'] },
              )}
            >
              <option value="tenant">{t('featureRollout.company', 'Company')}</option>
              <option value="user">{t('featureRollout.user', 'User')}</option>
            </select>
            <input
              className="form-input"
              value={override.id}
              placeholder={t('featureRollout.identifier', 'Identifier')}
              onChange={(event) => updateOverride(index, { id: event.target.value })}
            />
            <select
              className="form-input"
              value={override.enabled ? 'on' : 'off'}
              onChange={(event) => updateOverride(index, { enabled: event.target.value === 'on' })}
            >
              <option value="on">{t('featureRollout.forceOn', 'Force on')}</option>
              <option value="off">{t('featureRollout.forceOff', 'Force off')}</option>
            </select>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => updateDraft(
                'overrides',
                draft.overrides.filter((_item, itemIndex) => itemIndex !== index),
              )}
            >
              {t('common.remove', 'Remove')}
            </button>
          </div>
        ))}
      </div>

      <div className="feature-rollout-editor-actions">
        <button
          type="button"
          className="btn btn-primary"
          disabled={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending
            ? t('common.saving', 'Saving…')
            : t('common.save', 'Save')}
        </button>
      </div>
    </div>
  );
}

export default function PlatformFeatureFlagsSection({
  initialFlags,
}: PlatformFeatureFlagsSectionProps) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [editingFlagId, setEditingFlagId] = useState<string | 'new' | null>(null);
  const {
    data: flags = [],
    error,
    isLoading,
    refetch,
  } = useQuery({
    queryKey: ['platform-feature-flags'],
    queryFn: governanceApi.listFeatureFlags,
    initialData: initialFlags,
  });
  const deleteMutation = useMutation({
    mutationFn: (flag: FeatureFlag) => {
      if (!flag.updated_at) {
        throw new Error(t('featureRollout.versionMissing', 'The current rollout version is unavailable.'));
      }
      return governanceApi.deleteFeatureFlag(flag.id, flag.updated_at);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['platform-feature-flags'] });
      showAppToast(t('featureRollout.deleted', 'Feature rollout deleted'), 'success');
    },
    onError: (deleteError: Error) => {
      showAppToast(deleteError.message || t('featureRollout.deleteFailed', 'Could not delete rollout'), 'error');
    },
  });

  const deleteFlag = async (flag: FeatureFlag) => {
    const confirmed = await requestAppConfirm({
      title: t('featureRollout.deleteTitle', 'Delete feature rollout'),
      message: t(
        'featureRollout.deleteConfirm',
        'Delete {{key}}? Runtime consumers will fall back to their documented default.',
        { key: flag.key },
      ),
      confirmLabel: t('common.delete', 'Delete'),
      danger: true,
    });
    if (confirmed) deleteMutation.mutate(flag);
  };

  const editingFlag = editingFlagId && editingFlagId !== 'new'
    ? flags.find((flag) => flag.id === editingFlagId) ?? null
    : null;

  return (
    <section className="platform-feature-rollout" data-testid="platform-feature-flags-section">
      <div className="platform-feature-rollout-header">
        <div>
          <span>{t('featureRollout.eyebrow', 'Platform operations')}</span>
          <h3>{t('featureRollout.title', 'Feature Rollout')}</h3>
          <p>
            {t(
              'featureRollout.subtitle',
              'Control global runtime releases with typed audiences, expiry, and a durable platform audit receipt.',
            )}
          </p>
        </div>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => setEditingFlagId('new')}
        >
          {t('featureRollout.create', 'Create rollout')}
        </button>
      </div>

      {editingFlagId ? (
        <FeatureFlagEditor
          key={editingFlagId}
          flag={editingFlag}
          onClose={() => setEditingFlagId(null)}
        />
      ) : null}

      {isLoading ? (
        <div className="empty-state">{t('common.loading', 'Loading…')}</div>
      ) : error ? (
        <div className="empty-state">
          <p>{t('featureRollout.loadFailed', 'Could not load platform rollouts.')}</p>
          <button type="button" className="btn btn-secondary" onClick={() => void refetch()}>
            {t('common.retry', 'Retry')}
          </button>
        </div>
      ) : flags.length === 0 ? (
        <div className="empty-state">
          {t('featureRollout.empty', 'No feature rollouts are configured.')}
        </div>
      ) : (
        <div className="platform-feature-rollout-list">
          {flags.map((flag) => {
            const expired = flag.expires_at ? new Date(flag.expires_at).getTime() <= Date.now() : false;
            return (
              <article className="platform-feature-rollout-row" key={flag.id}>
                <div className="platform-feature-rollout-main">
                  <div className="platform-feature-rollout-title">
                    <strong>{flag.key}</strong>
                    <span className={`badge ${expired ? 'badge-warning' : 'badge-info'}`}>
                      {expired
                        ? t('featureRollout.expired', 'Expired')
                        : t('featureRollout.active', 'Configured')}
                    </span>
                  </div>
                  {flag.description ? <p>{flag.description}</p> : null}
                  <div className="platform-feature-rollout-summary">
                    {featureFlagAudienceSummary(flag, (key, fallback) => t(key, fallback)).map(
                      (item) => <span key={item}>{item}</span>,
                    )}
                    {flag.expires_at ? (
                      <span>
                        {t('featureRollout.expires', 'Expires')}{' '}
                        {new Intl.DateTimeFormat(i18n?.language || undefined, {
                          dateStyle: 'medium',
                          timeStyle: 'short',
                        }).format(new Date(flag.expires_at))}
                      </span>
                    ) : null}
                  </div>
                </div>
                <div className="platform-feature-rollout-actions">
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={() => setEditingFlagId(flag.id)}
                  >
                    {t('common.edit', 'Edit')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={deleteMutation.isPending}
                    onClick={() => void deleteFlag(flag)}
                  >
                    {t('common.delete', 'Delete')}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
