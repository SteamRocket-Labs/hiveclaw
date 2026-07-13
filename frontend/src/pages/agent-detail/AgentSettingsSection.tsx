import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import ChannelConfig from '../../components/ChannelConfig';
import ConfirmModal from '../../components/ConfirmModal';
import { agentApi, type AgentPermissions } from '../../api/domains/agents';
import { planApi, type PlanRecommendationCreateInput } from '../../api/domains/plans';
import { triggerApi } from '../../api/domains/triggers';
import HookRuntimeControlCard from './HookRuntimeControlCard';
import './AgentSettingsSection.css';

type AgentSettingsForm = {
  primary_model_id: string;
  fallback_model_id: string;
  max_triggers: number;
  min_poll_interval_min: number;
  webhook_rate_limit: number;
  default_session_permission_mode: 'default' | 'auto' | 'bypassPermissions';
  smart_model_routing_enabled: boolean;
  security_zone: string;
};

const SETTINGS_PATROL_TRIGGER_SOURCE = 'settings_patrol';
const DEFAULT_PATROL_INTERVAL_MINUTES = 120;
const DEFAULT_PATROL_ACTIVE_HOURS = '09:00-18:00';

type AgentTriggerSummary = {
  id: string;
  name?: string;
  type?: string;
  is_enabled?: boolean;
  last_fired_at?: string | null;
  config?: Record<string, any>;
};

type PatrolFormState = {
  enabled: boolean;
  intervalMinutes: number;
  activeStart: string;
  activeEnd: string;
};

type PatrolPlanDecision = 'enable_without_plan';
export type PatrolSaveDisposition = 'review_required' | 'apply' | 'apply_with_opt_out';

type AgentAccessVisibility = 'private' | 'company';

const clampPatrolInterval = (value: number) => Math.max(15, Math.min(1440, Math.round(value) || DEFAULT_PATROL_INTERVAL_MINUTES));
const normalizeActiveHours = (value: string) => value.trim().replace(/\s+/g, '');
const isValidActiveTime = (value: string) => /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
const splitActiveHours = (value: string) => {
  const [start, end] = normalizeActiveHours(value || DEFAULT_PATROL_ACTIVE_HOURS).split('-');
  return {
    activeStart: isValidActiveTime(start) ? start : '09:00',
    activeEnd: isValidActiveTime(end) ? end : '18:00',
  };
};
const buildActiveHours = (form: PatrolFormState) => `${form.activeStart}-${form.activeEnd}`;

export const patrolSaveDisposition = (
  enabled: boolean,
  persistedEnabled: boolean,
  decision?: PatrolPlanDecision,
): PatrolSaveDisposition => {
  if (!enabled || persistedEnabled) return 'apply';
  return decision === 'enable_without_plan' ? 'apply_with_opt_out' : 'review_required';
};

export const patrolEnabledUpdateValue = (
  enabled: boolean,
  persistedEnabled: boolean,
): boolean | undefined => (enabled === persistedEnabled ? undefined : enabled);

export const buildPatrolPlanReviewRequest = ({
  minutes,
  activeHours,
  timezone,
}: {
  minutes: number;
  activeHours: string;
  timezone?: string | null;
}) => [
  `Plan how to enable this employee's patrol every ${minutes} minutes`,
  `during ${activeHours}${timezone ? ` (${timezone})` : ''}.`,
  'Review scope, permissions, expected actions, failure recovery, and budget before enabling autonomous wake.',
  'Do not enable it until I confirm the plan.',
].join(' ');

const isSettingsPatrolTrigger = (trigger: AgentTriggerSummary) =>
  trigger.type === 'interval' &&
  ((trigger.config || {}).source === SETTINGS_PATROL_TRIGGER_SOURCE || trigger.name === SETTINGS_PATROL_TRIGGER_SOURCE);

export const buildPatrolPlanRecommendationInput = ({
  agentId,
  reason,
  actionKind,
}: {
  agentId: string;
  reason: string;
  actionKind: 'create_enabled_trigger' | 'enable_autonomous_wake';
}): PlanRecommendationCreateInput => ({
  original_request: reason,
  title: 'Settings patrol schedule',
  session_id: `${SETTINGS_PATROL_TRIGGER_SOURCE}:${agentId}`,
  source: 'settings',
  intent_type: 'autonomous_wake',
  action_kind: actionKind,
  tool_name: 'trigger_rest',
  metadata: { surface: 'agent_settings_patrol' },
});

const derivePatrolForm = (trigger?: AgentTriggerSummary | null): PatrolFormState => {
  const config = trigger?.config || {};
  const minutes = Number(config.minutes ?? config.interval ?? DEFAULT_PATROL_INTERVAL_MINUTES);
  const activeHours = splitActiveHours(String(config.active_hours || DEFAULT_PATROL_ACTIVE_HOURS));
  return {
    enabled: trigger ? trigger.is_enabled !== false : false,
    intervalMinutes: clampPatrolInterval(minutes),
    ...activeHours,
  };
};

export const agentAccessVisibilityFromPermissions = (
  permissions?: AgentPermissions | null,
): AgentAccessVisibility => (permissions?.scope_type === 'company' ? 'company' : 'private');

interface AgentSettingsSectionProps {
  agentId: string;
  agent: any;
  llmModels: any[];
  canManage: boolean;
  settingsForm: AgentSettingsForm;
  onSettingsFormChange: React.Dispatch<React.SetStateAction<AgentSettingsForm>>;
  settingsSaving: boolean;
  settingsSaved: boolean;
  settingsError: string;
  onSetSettingsSaving: (value: boolean) => void;
  onSetSettingsSaved: (value: boolean) => void;
  onSetSettingsError: (value: string) => void;
  onResetSettingsInit: () => void;
  wmDraft: string;
  wmSaved: boolean;
  onSetWmDraft: (value: string) => void;
  onSetWmSaved: (value: boolean) => void;
  onReviewPatrolPlan?: (request: string) => void | Promise<void>;
}

const formatTokens = (n: number) => {
  if (!n) return '0';
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
};

export default function AgentSettingsSection({
  agentId,
  agent,
  llmModels,
  canManage,
  settingsForm,
  onSettingsFormChange,
  settingsSaving,
  settingsSaved,
  settingsError,
  onSetSettingsSaving,
  onSetSettingsSaved,
  onSetSettingsError,
  onResetSettingsInit,
  wmDraft,
  wmSaved,
  onSetWmDraft,
  onSetWmSaved,
  onReviewPatrolPlan,
}: AgentSettingsSectionProps) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const { data: triggerData = [], isLoading: patrolLoading } = useQuery({
    queryKey: ['triggers', agentId],
    queryFn: () => triggerApi.list(agentId),
    enabled: !!agentId,
  });
  const triggers = Array.isArray(triggerData) ? (triggerData as AgentTriggerSummary[]) : [];
  const patrolTrigger = React.useMemo(() => triggers.find(isSettingsPatrolTrigger) || null, [triggers]);
  const persistedPatrolForm = React.useMemo(() => derivePatrolForm(patrolTrigger), [patrolTrigger]);
  const [patrolForm, setPatrolForm] = React.useState<PatrolFormState>(() => persistedPatrolForm);
  const [patrolSaving, setPatrolSaving] = React.useState(false);
  const [patrolSaved, setPatrolSaved] = React.useState(false);
  const [patrolError, setPatrolError] = React.useState('');
  const [patrolPlanDecisionPending, setPatrolPlanDecisionPending] = React.useState(false);
  const [permissionSaving, setPermissionSaving] = React.useState(false);
  const [permissionSaved, setPermissionSaved] = React.useState(false);
  const [permissionError, setPermissionError] = React.useState('');
  const [fullAccessSavePending, setFullAccessSavePending] = React.useState(false);
  const { data: permissionData, isLoading: permissionsLoading } = useQuery({
    queryKey: ['agent-permissions', agentId],
    queryFn: () => agentApi.getPermissions(agentId),
    enabled: !!agentId,
  });
  const accessVisibility = agentAccessVisibilityFromPermissions(permissionData as AgentPermissions | undefined);

  React.useEffect(() => {
    setPatrolForm(persistedPatrolForm);
    setPatrolPlanDecisionPending(false);
  }, [persistedPatrolForm]);

  const hasChanges =
    settingsForm.primary_model_id !== (agent?.primary_model_id || '') ||
    settingsForm.fallback_model_id !== (agent?.fallback_model_id || '') ||
    settingsForm.max_triggers !== ((agent as any)?.max_triggers ?? 20) ||
    settingsForm.min_poll_interval_min !== ((agent as any)?.min_poll_interval_min ?? 5) ||
    settingsForm.webhook_rate_limit !== ((agent as any)?.webhook_rate_limit ?? 5) ||
    settingsForm.default_session_permission_mode !== ((agent as any)?.default_session_permission_mode || 'default') ||
    settingsForm.smart_model_routing_enabled !== !!((agent as any)?.smart_model_routing?.enabled) ||
    settingsForm.security_zone !== ((agent as any)?.security_zone || 'standard');
  const patrolHasChanges =
    patrolForm.enabled !== persistedPatrolForm.enabled ||
    patrolForm.intervalMinutes !== persistedPatrolForm.intervalMinutes ||
    patrolForm.activeStart !== persistedPatrolForm.activeStart ||
    patrolForm.activeEnd !== persistedPatrolForm.activeEnd;

  const persistSettings = async () => {
    onSetSettingsSaving(true);
    onSetSettingsError('');
    try {
      const result: any = await agentApi.update(agentId, {
        primary_model_id: settingsForm.primary_model_id || null,
        fallback_model_id: settingsForm.fallback_model_id || null,
        max_triggers: settingsForm.max_triggers,
        min_poll_interval_min: settingsForm.min_poll_interval_min,
        webhook_rate_limit: settingsForm.webhook_rate_limit,
        default_session_permission_mode: settingsForm.default_session_permission_mode,
        security_zone: settingsForm.security_zone,
        smart_model_routing: settingsForm.smart_model_routing_enabled
          ? { enabled: true, max_simple_chars: 160, max_simple_words: 28 }
          : null,
      } as any);
      queryClient.setQueryData(['agent', agentId], (current: any) => ({ ...(current || {}), ...(result || {}) }));
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
      onResetSettingsInit();

      const clamped = result?._clamped_fields;
      if (clamped && clamped.length > 0) {
        const fieldNames: Record<string, string> = {
          min_poll_interval_min: t('agent.settings.clampedField.minPollInterval'),
          webhook_rate_limit: t('agent.settings.clampedField.webhookRateLimit'),
        };
        const msgs = clamped.map((c: any) => {
          const name = fieldNames[c.field] || c.field;
          return t('agent.settings.clampedMessage', { name, requested: c.requested, applied: c.applied });
        });
        onSetSettingsError(`Some values were adjusted:\n${msgs.join('\n')}`);
      }

      onSetSettingsSaved(true);
      setTimeout(() => onSetSettingsSaved(false), 2000);
    } catch (e: any) {
      onSetSettingsError(e?.message || 'Failed to save');
    } finally {
      onSetSettingsSaving(false);
    }
  };

  const handleSaveSettings = () => {
    const enablesFullAccess =
      settingsForm.default_session_permission_mode === 'bypassPermissions'
      && (agent?.default_session_permission_mode || 'default') !== 'bypassPermissions';
    if (enablesFullAccess) {
      setFullAccessSavePending(true);
      return;
    }
    void persistSettings();
  };

  const handleSaveAccessVisibility = async (visibility: AgentAccessVisibility) => {
    if (!canManage || permissionSaving || visibility === accessVisibility) return;
    setPermissionSaving(true);
    setPermissionSaved(false);
    setPermissionError('');
    try {
      await agentApi.updatePermissions(
        agentId,
        visibility === 'company'
          ? { scope_type: 'company', scope_ids: [], access_level: 'use' }
          : { scope_type: 'user', scope_ids: [], access_level: 'manage' },
      );
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['agent-permissions', agentId] }),
        queryClient.invalidateQueries({ queryKey: ['agent', agentId] }),
        queryClient.invalidateQueries({ queryKey: ['agents'] }),
        queryClient.invalidateQueries({ queryKey: ['a2a-collaborators'] }),
      ]);
      setPermissionSaved(true);
      setTimeout(() => setPermissionSaved(false), 2000);
    } catch (e: any) {
      setPermissionError(e?.message || t('agent.settings.access.saveError', 'Failed to save access permissions'));
    } finally {
      setPermissionSaving(false);
    }
  };

  const handleSavePatrolSettings = async (decision?: PatrolPlanDecision) => {
    if (!canManage) return;
    if (!isValidActiveTime(patrolForm.activeStart) || !isValidActiveTime(patrolForm.activeEnd)) {
      setPatrolError(t('agent.settings.patrol.invalidActiveHours', 'Select valid start and end times.'));
      return;
    }

    const minutes = clampPatrolInterval(patrolForm.intervalMinutes);
    const activeHours = buildActiveHours(patrolForm);
    const saveDisposition = patrolSaveDisposition(
      patrolForm.enabled,
      persistedPatrolForm.enabled,
      decision,
    );
    if (saveDisposition === 'review_required') {
      setPatrolPlanDecisionPending(true);
      setPatrolError('');
      return;
    }
    const nextConfig: Record<string, any> = {
      ...(patrolTrigger?.config || {}),
      source: SETTINGS_PATROL_TRIGGER_SOURCE,
      trigger_class: 'scheduled_job',
      minutes,
      active_hours: activeHours,
    };
    if (agent?.timezone) {
      nextConfig.timezone = agent.timezone;
    } else {
      delete nextConfig.timezone;
    }

    setPatrolSaving(true);
    setPatrolError('');
    try {
      const reason = t(
        'agent.settings.patrol.triggerReason',
        'Run scheduled patrols for messages, trigger state, and Agent Circle context.',
      );
      let planRecommendationId: string | undefined;
      if (saveDisposition === 'apply_with_opt_out') {
        const actionKind = patrolTrigger ? 'enable_autonomous_wake' : 'create_enabled_trigger';
        const recommendation = await planApi.createRecommendation(
          agentId,
          buildPatrolPlanRecommendationInput({ agentId, reason, actionKind }),
        );
        const declined = await planApi.declineRecommendation(agentId, recommendation.id);
        planRecommendationId = declined.id;
      }
      if (patrolTrigger) {
        const enabledPatch = patrolEnabledUpdateValue(patrolForm.enabled, persistedPatrolForm.enabled);
        await triggerApi.update(agentId, patrolTrigger.id, {
          ...(enabledPatch === undefined ? {} : { is_enabled: enabledPatch }),
          config: nextConfig,
          reason,
          trigger_class: 'scheduled_job',
          cooldown_seconds: 60,
          ...(planRecommendationId
            ? { plan_mode_decision: 'declined', plan_recommendation_id: planRecommendationId }
            : {}),
        });
      } else if (patrolForm.enabled) {
        await triggerApi.create(agentId, {
          name: SETTINGS_PATROL_TRIGGER_SOURCE,
          type: 'interval',
          config: nextConfig,
          reason,
          trigger_class: 'scheduled_job',
          cooldown_seconds: 60,
          plan_mode_decision: 'declined',
          plan_recommendation_id: planRecommendationId,
        });
      }
      await queryClient.invalidateQueries({ queryKey: ['triggers', agentId] });
      setPatrolForm({
        enabled: patrolForm.enabled,
        intervalMinutes: minutes,
        activeStart: patrolForm.activeStart,
        activeEnd: patrolForm.activeEnd,
      });
      setPatrolPlanDecisionPending(false);
      setPatrolSaved(true);
      setTimeout(() => setPatrolSaved(false), 2000);
    } catch (e: any) {
      setPatrolError(e?.message || t('agent.settings.patrol.saveError', 'Failed to save patrol settings'));
    } finally {
      setPatrolSaving(false);
    }
  };

  const handleReviewPatrolPlan = async () => {
    if (!onReviewPatrolPlan) return;
    setPatrolSaving(true);
    setPatrolError('');
    try {
      await onReviewPatrolPlan(buildPatrolPlanReviewRequest({
        minutes: clampPatrolInterval(patrolForm.intervalMinutes),
        activeHours: buildActiveHours(patrolForm),
        timezone: agent?.timezone,
      }));
      setPatrolPlanDecisionPending(false);
    } catch (e: any) {
      setPatrolError(e?.message || t('agent.settings.patrol.reviewError', 'Failed to open Plan Mode'));
    } finally {
      setPatrolSaving(false);
    }
  };

  const saveWelcomeMessage = async () => {
    try {
      await agentApi.update(agentId, { welcome_message: wmDraft } as any);
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
      onSetWmSaved(true);
      setTimeout(() => onSetWmSaved(false), 2000);
    } catch {}
  };

  return (
    <div>
      <div className="agent-settings-header">
        <h3 className="agent-settings-title">{t('agent.settings.title')}</h3>
        <div className="agent-settings-header-actions">
          {settingsSaved && <span className="agent-settings-status is-success">{t('agent.settings.saved', 'Saved')}</span>}
          {settingsError && (
            <span className={`agent-settings-msg ${settingsError.includes('adjusted') ? 'is-warning' : 'is-error'}`}>
              {settingsError}
            </span>
          )}
          <button
            className="btn btn-primary agent-settings-save-btn"
            disabled={!canManage || !hasChanges || settingsSaving}
            onClick={handleSaveSettings}
            style={{ opacity: hasChanges ? 1 : 0.5, cursor: hasChanges ? 'pointer' : 'default' }}
          >
            {settingsSaving ? t('agent.settings.saving', 'Saving...') : t('agent.settings.save', 'Save')}
          </button>
        </div>
      </div>

      <HookRuntimeControlCard agentId={agentId} canManage={canManage} />

      <div className="card agent-settings-card">
        <div className="agent-settings-card-head">
          <div>
            <h4 className="agent-settings-card-title-flush">
              {t('agent.settings.access.title', 'Access Permissions')}
            </h4>
            <p className="agent-settings-card-desc agent-settings-card-desc-flush">
              {t('agent.settings.access.description', 'Control whether this employee is private to you or visible to the whole company.')}
            </p>
          </div>
          <div className="agent-settings-access-status">
            {permissionSaving && (
              <span className="agent-settings-status">
                {t('agent.settings.access.saving', 'Saving access...')}
              </span>
            )}
            {permissionSaved && (
              <span className="agent-settings-status is-success">
                {t('agent.settings.access.saved', 'Access saved')}
              </span>
            )}
          </div>
        </div>
        <div className="agent-settings-access-options" role="radiogroup" aria-label={t('agent.settings.access.title', 'Access Permissions')}>
          <label className={`agent-settings-access-option${accessVisibility === 'private' ? ' is-selected' : ''}${!canManage ? ' is-disabled' : ''}`}>
            <input
              type="radio"
              name="perm_scope"
              value="private"
              checked={accessVisibility === 'private'}
              disabled={!canManage || permissionsLoading || permissionSaving}
              onChange={() => handleSaveAccessVisibility('private')}
            />
            <span>
              <strong>{t('agent.settings.access.privateTitle', 'Private to me')}</strong>
              <small>{t('agent.settings.access.privateDesc', 'Only you and admins can manage this employee.')}</small>
            </span>
          </label>
          <label className={`agent-settings-access-option${accessVisibility === 'company' ? ' is-selected' : ''}${!canManage ? ' is-disabled' : ''}`}>
            <input
              type="radio"
              name="perm_scope"
              value="company"
              checked={accessVisibility === 'company'}
              disabled={!canManage || permissionsLoading || permissionSaving}
              onChange={() => handleSaveAccessVisibility('company')}
            />
            <span>
              <strong>{t('agent.settings.access.companyTitle', 'Company shared')}</strong>
              <small>{t('agent.settings.access.companyDesc', 'Everyone in the company can use this employee; only owners and admins can change settings.')}</small>
            </span>
          </label>
        </div>
        <div className="agent-settings-hint">
          {t('agent.settings.access.defaultAccessLevel', 'Default Access Level')}: {' '}
          {accessVisibility === 'company'
            ? t('agent.settings.access.companyAccessLevel', 'company users can use')
            : t('agent.settings.access.privateAccessLevel', 'owner manage')}
        </div>
        {permissionError && <div className="agent-settings-hint agent-settings-hint-error">{permissionError}</div>}
      </div>

      <div className="card agent-settings-card">
        <h4 className="agent-settings-card-title">
          {t('agent.settings.sessionPermissionDefault.title', 'New conversation default')}
        </h4>
        <p className="agent-settings-card-desc">
          {t(
            'agent.settings.sessionPermissionDefault.description',
            'Choose the permission mode used when this employee starts a new conversation.',
          )}
        </p>
        <label className="agent-settings-label" htmlFor="default-session-permission-mode">
          {t('agent.settings.sessionPermissionDefault.label', 'Default permission mode')}
        </label>
        <select
          id="default-session-permission-mode"
          name="default_session_permission_mode"
          className="input agent-settings-input-full"
          value={settingsForm.default_session_permission_mode}
          disabled={!canManage}
          onChange={(event) => onSettingsFormChange((form) => ({
            ...form,
            default_session_permission_mode: event.target.value as AgentSettingsForm['default_session_permission_mode'],
          }))}
        >
          <option value="default">{t('agent.chat.composer.permissionMode.default', 'Ask first')}</option>
          <option value="auto">{t('agent.chat.composer.permissionMode.auto', 'Approve for me')}</option>
          <option value="bypassPermissions">
            {t('agent.chat.composer.permissionMode.bypassPermissions', 'Full access')}
          </option>
        </select>
        <div className="agent-settings-hint">
          {settingsForm.default_session_permission_mode === 'bypassPermissions'
            ? t(
                'agent.settings.sessionPermissionDefault.fullAccessHint',
                'Enterprise access, safety, and destructive-action rules always apply.',
              )
            : t(
                'agent.settings.sessionPermissionDefault.hint',
                'The mode can still be changed from the conversation composer.',
              )}
        </div>
      </div>

      <div className="card agent-settings-card">
        <h4 className="agent-settings-card-title">{t('agent.settings.modelConfig')}</h4>
        <div className="agent-settings-field-col">
          <div>
            <label className="agent-settings-label">{t('agent.settings.primaryModel')}</label>
            <select
              className="input"
              value={settingsForm.primary_model_id}
              onChange={(e) => onSettingsFormChange((f) => ({ ...f, primary_model_id: e.target.value }))}
            >
              <option value="">--</option>
              {llmModels.filter((m: any) => m.enabled || m.id === settingsForm.primary_model_id).map((m: any) => (
                <option key={m.id} value={m.id}>
                  {m.label} ({m.provider}/{m.model}){!m.enabled ? ` [${t('enterprise.llm.disabled', 'Disabled')}]` : ''}
                </option>
              ))}
            </select>
            {settingsForm.primary_model_id && llmModels.some((m: any) => m.id === settingsForm.primary_model_id && !m.enabled) && (
              <div className="agent-settings-hint agent-settings-hint-error">
                {t('agent.settings.modelDisabledWarning', 'This model has been disabled by admin. The agent will automatically use the fallback model.')}
              </div>
            )}
            {!settingsForm.primary_model_id && settingsForm.fallback_model_id && (() => {
              const fb = llmModels.find((m: any) => m.id === settingsForm.fallback_model_id);
              return fb ? (
                <div className="agent-settings-hint agent-settings-hint-accent">
                  {t('agent.settings.usingFallback', { model: fb.label })}
                </div>
              ) : null;
            })()}
            {!settingsForm.primary_model_id && !settingsForm.fallback_model_id && llmModels.length > 0 && (
              <div className="agent-settings-hint agent-settings-hint-warning">
                {t('agent.settings.noModelWarning')}
              </div>
            )}
            <div className="agent-settings-hint">{t('agent.settings.primaryModel')}</div>
          </div>
          <div>
            <label className="agent-settings-label">{t('agent.settings.fallbackModel')}</label>
            <select
              className="input"
              value={settingsForm.fallback_model_id}
              onChange={(e) => onSettingsFormChange((f) => ({ ...f, fallback_model_id: e.target.value }))}
            >
              <option value="">--</option>
              {llmModels.filter((m: any) => m.enabled || m.id === settingsForm.fallback_model_id).map((m: any) => (
                <option key={m.id} value={m.id}>
                  {m.label} ({m.provider}/{m.model}){!m.enabled ? ` [${t('enterprise.llm.disabled', 'Disabled')}]` : ''}
                </option>
              ))}
            </select>
            {settingsForm.fallback_model_id && llmModels.some((m: any) => m.id === settingsForm.fallback_model_id && !m.enabled) && (
              <div className="agent-settings-hint agent-settings-hint-error">
                {t('agent.settings.modelDisabledWarning', 'This model has been disabled by admin. The agent will automatically use the fallback model.')}
              </div>
            )}
            <div className="agent-settings-hint">{t('agent.settings.fallbackModel')}</div>
          </div>
          {settingsForm.fallback_model_id && (
            <div className="agent-settings-inset-row">
              <div className="agent-settings-inset-grow">
                <div className="agent-settings-inset-name">{t('agent.settings.smartRouting', 'Smart Model Routing')}</div>
                <div className="agent-settings-inset-desc agent-settings-inset-desc-spaced">
                  {t('agent.settings.smartRoutingDesc', 'Automatically use the fallback model for simple conversational turns to save costs. Complex tasks always use the primary model.')}
                </div>
              </div>
              <label className="agent-settings-toggle agent-settings-toggle-sm">
                <input
                  type="checkbox"
                  checked={settingsForm.smart_model_routing_enabled}
                  onChange={(e) => onSettingsFormChange((f) => ({ ...f, smart_model_routing_enabled: e.target.checked }))}
                  className="agent-settings-toggle-input"
                />
                <span className={`agent-settings-toggle-track${settingsForm.smart_model_routing_enabled ? ' is-on' : ''}`}>
                  <span className="agent-settings-toggle-knob" />
                </span>
              </label>
            </div>
          )}
        </div>
      </div>


      <div className="card agent-settings-card">
        <h4 className="agent-settings-card-title">{t('agent.settings.tokenStats')}</h4>
        <div className="agent-settings-stats-grid">
          <div>
            <div className="agent-settings-stat-label">{t('agent.settings.tokenToday')}</div>
            <div className="agent-settings-stat-value">{formatTokens(agent?.tokens_used_today || 0)}</div>
          </div>
          <div>
            <div className="agent-settings-stat-label">{t('agent.settings.tokenMonth')}</div>
            <div className="agent-settings-stat-value">{formatTokens(agent?.tokens_used_month || 0)}</div>
          </div>
          <div>
            <div className="agent-settings-stat-label">{t('agent.settings.tokenTotal')}</div>
            <div className="agent-settings-stat-value">{formatTokens(agent?.tokens_used_total || 0)}</div>
          </div>
        </div>
        <div className="agent-settings-quota-hint">
          {t('agent.settings.tokenQuotaHint')}
        </div>
      </div>

      <div className="card agent-settings-card">
        <h4 className="agent-settings-card-title-tight">{t('agent.settings.triggerLimits')}</h4>
        <p className="agent-settings-card-desc">
          {t('agent.settings.triggerLimitsDesc')}
        </p>
        <div className="agent-settings-stats-grid">
          <div>
            <label className="agent-settings-label">
              {t('agent.settings.maxTriggers')}
            </label>
            <input
              className="input agent-settings-input-full"
              type="number"
              min={1}
              max={100}
              value={settingsForm.max_triggers}
              onChange={(e) =>
                onSettingsFormChange((f) => ({ ...f, max_triggers: Math.max(1, Math.min(100, parseInt(e.target.value, 10) || 20)) }))
              }
            />
            <div className="agent-settings-hint">
              {t('agent.settings.maxTriggersDesc')}
            </div>
          </div>
          <div>
            <label className="agent-settings-label">
              {t('agent.settings.minPollInterval')}
            </label>
            <input
              className="input agent-settings-input-full"
              type="number"
              min={1}
              max={60}
              value={settingsForm.min_poll_interval_min}
              onChange={(e) =>
                onSettingsFormChange((f) => ({ ...f, min_poll_interval_min: Math.max(1, Math.min(60, parseInt(e.target.value, 10) || 5)) }))
              }
            />
            <div className="agent-settings-hint">
              {t('agent.settings.minPollIntervalDesc')}
            </div>
          </div>
          <div>
            <label className="agent-settings-label">
              {t('agent.settings.webhookRateLimit')}
            </label>
            <input
              className="input agent-settings-input-full"
              type="number"
              min={1}
              max={60}
              value={settingsForm.webhook_rate_limit}
              onChange={(e) =>
                onSettingsFormChange((f) => ({ ...f, webhook_rate_limit: Math.max(1, Math.min(60, parseInt(e.target.value, 10) || 5)) }))
              }
            />
            <div className="agent-settings-hint">
              {t('agent.settings.webhookRateLimitDesc')}
            </div>
          </div>
        </div>
      </div>

      <div className="card agent-settings-card">
        <div className="agent-settings-card-head">
          <h4 className="agent-settings-card-title-flush">{t('agent.settings.welcomeMessage')}</h4>
          {wmSaved && <span className="agent-settings-status is-success">✓ {t('agent.settings.saved')}</span>}
        </div>
        <p className="agent-settings-card-desc">
          {t('agent.settings.welcomeMessageDesc')}
        </p>
        <textarea
          className="input agent-settings-textarea"
          rows={4}
          value={wmDraft}
          onChange={(e) => onSetWmDraft(e.target.value)}
          onBlur={saveWelcomeMessage}
          placeholder={t('agent.settings.welcomeMessagePlaceholder')}
        />
      </div>

      <div className="card agent-settings-card">
        <h4 className="agent-settings-card-title-row">{t('agent.settings.timezone.title', '🌐 Timezone')}</h4>
        <p className="agent-settings-card-desc agent-settings-card-desc-lg">
          {t('agent.settings.timezone.description', "The timezone used for this agent's scheduling, active hours, and time awareness. Defaults to the company timezone if not set.")}
        </p>
        <div className="agent-settings-inset-row">
          <div>
            <div className="agent-settings-inset-name">{t('agent.settings.timezone.current', 'Agent Timezone')}</div>
            <div className="agent-settings-inset-desc">
              {agent?.timezone
                ? t('agent.settings.timezone.override', 'Custom timezone for this agent')
                : t('agent.settings.timezone.inherited', 'Using company default timezone')}
            </div>
          </div>
          <select
            className={`input agent-settings-tz-select${canManage ? '' : ' is-dimmed'}`}
            disabled={!canManage}
            value={agent?.timezone || ''}
            onChange={async (e) => {
              if (!canManage) return;
              const val = e.target.value || null;
              await agentApi.update(agentId, { timezone: val } as any);
              queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
            }}
          >
            <option value="">{t('agent.settings.timezone.default', '↩ Company default')}</option>
            {[
              'UTC',
              'Asia/Shanghai',
              'Asia/Tokyo',
              'Asia/Seoul',
              'Asia/Singapore',
              'Asia/Kolkata',
              'Asia/Dubai',
              'Europe/London',
              'Europe/Paris',
              'Europe/Berlin',
              'Europe/Moscow',
              'America/New_York',
              'America/Chicago',
              'America/Denver',
              'America/Los_Angeles',
              'America/Sao_Paulo',
              'Australia/Sydney',
              'Pacific/Auckland',
            ].map((tz) => (
              <option key={tz} value={tz}>
                {tz}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="card agent-settings-card">
        <h4 className="agent-settings-card-title-row">{t('agent.settings.patrol.title', 'Patrol & Agent Circle')}</h4>
        <p className="agent-settings-card-desc agent-settings-card-desc-lg">
          {t('agent.settings.patrol.description', 'Configure the user-facing patrol trigger and Agent Circle permissions. Internal maintenance stays platform-managed.')}
        </p>
        <div className="agent-settings-inset-row agent-settings-inset-row-mb">
          <div>
            <div className="agent-settings-inset-name">{t('agent.settings.patrol.enabled', 'Enable patrol')}</div>
            <div className="agent-settings-inset-desc">
              {t('agent.settings.patrol.enabledDesc', 'Creates or pauses the user-facing interval trigger. Internal maintenance is always managed by the platform.')}
            </div>
          </div>
          <label className="agent-settings-toggle agent-settings-toggle-md">
            <input
              type="checkbox"
              checked={patrolForm.enabled}
              disabled={!canManage || patrolLoading}
              onChange={(e) => {
                setPatrolPlanDecisionPending(false);
                setPatrolForm((prev) => ({ ...prev, enabled: e.target.checked }));
              }}
              className="agent-settings-toggle-input"
            />
            <span
              className={`agent-settings-toggle-track${patrolForm.enabled ? ' is-on' : ''}${
                !canManage || patrolLoading ? ' is-disabled' : ''
              }`}
            >
              <span className="agent-settings-toggle-knob" />
            </span>
          </label>
        </div>
        <div className="agent-settings-patrol-grid">
          <div>
            <label className="agent-settings-label">
              {t('agent.settings.patrol.interval', 'Patrol interval')}
            </label>
            <div className="agent-settings-inline">
              <input
                className="input agent-settings-input-narrow"
                type="number"
                min={15}
                max={1440}
                value={patrolForm.intervalMinutes}
                disabled={!canManage || patrolLoading}
                onChange={(e) =>
                  setPatrolForm((prev) => ({
                    ...prev,
                    intervalMinutes: clampPatrolInterval(parseInt(e.target.value, 10)),
                  }))
                }
              />
              <span className="agent-settings-unit">{t('common.minutes', 'min')}</span>
            </div>
            <div className="agent-settings-hint">
              {t('agent.settings.patrol.intervalDesc', 'How often this employee wakes up for patrol work.')}
            </div>
          </div>
          <div>
            <label className="agent-settings-label">
              {t('agent.settings.patrol.activeHours', 'Active hours')}
            </label>
            <div className="agent-settings-inline-wrap">
              <label className="agent-settings-time-label">
                <span>{t('agent.settings.patrol.activeStart', 'Start')}</span>
                <input
                  className="input agent-settings-input-time"
                  type="time"
                  value={patrolForm.activeStart}
                  disabled={!canManage || patrolLoading}
                  onChange={(e) => setPatrolForm((prev) => ({ ...prev, activeStart: e.target.value }))}
                />
              </label>
              <label className="agent-settings-time-label">
                <span>{t('agent.settings.patrol.activeEnd', 'End')}</span>
                <input
                  className="input agent-settings-input-time"
                  type="time"
                  value={patrolForm.activeEnd}
                  disabled={!canManage || patrolLoading}
                  onChange={(e) => setPatrolForm((prev) => ({ ...prev, activeEnd: e.target.value }))}
                />
              </label>
            </div>
            <div className="agent-settings-hint">
              {t('agent.settings.patrol.activeHoursDesc', 'Only run patrol triggers inside this local time window.')}
            </div>
          </div>
          <div>
            <div className="agent-settings-lastrun-label">
              {t('agent.settings.patrol.lastRun', 'Last patrol')}
            </div>
            <div className="agent-settings-lastrun-value">
              {patrolTrigger?.last_fired_at
                ? new Date(patrolTrigger.last_fired_at).toLocaleString(i18n.language || undefined)
                : t('agent.settings.patrol.neverRun', 'Not run yet')}
            </div>
          </div>
        </div>
        <div className="agent-settings-patrol-actions">
          <button
            className={`btn btn-secondary agent-settings-patrol-save-btn${
              !canManage || !patrolHasChanges ? ' is-dimmed' : ''
            }`}
            disabled={!canManage || patrolLoading || patrolSaving || (!patrolTrigger && !patrolForm.enabled) || !patrolHasChanges}
            onClick={() => handleSavePatrolSettings()}
          >
            {patrolSaving ? t('agent.settings.patrol.saving', 'Saving patrol...') : t('agent.settings.patrol.save', 'Save patrol settings')}
          </button>
          {patrolSaved && <span className="agent-settings-status is-success">{t('agent.settings.patrol.saved', 'Patrol settings saved')}</span>}
          {patrolError && <span className="agent-settings-status is-error">{patrolError}</span>}
        </div>
        {patrolPlanDecisionPending && patrolForm.enabled ? (
          <section className="agent-settings-patrol-decision" data-testid="patrol-plan-decision">
            <div>
              <strong>{t('agent.settings.patrol.planDecisionTitle', 'Review before autonomous patrol')}</strong>
              <p>
                {t(
                  'agent.settings.patrol.planDecisionDesc',
                  'Patrol wakes this employee and may use tools without a new message. Review a plan, or explicitly enable it without one.',
                )}
              </p>
            </div>
            <div className="agent-settings-patrol-decision-actions">
              <button
                type="button"
                className="btn btn-primary"
                disabled={patrolSaving || !onReviewPatrolPlan}
                onClick={handleReviewPatrolPlan}
              >
                {t('agent.settings.patrol.reviewPlan', 'Review in Plan Mode')}
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={patrolSaving}
                onClick={() => handleSavePatrolSettings('enable_without_plan')}
              >
                {t('agent.settings.patrol.enableWithoutPlan', 'Enable without plan')}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={patrolSaving}
                onClick={() => setPatrolPlanDecisionPending(false)}
              >
                {t('common.cancel', 'Cancel')}
              </button>
            </div>
          </section>
        ) : null}
      </div>

      <div className="agent-settings-channel">
        <ChannelConfig mode="edit" agentId={agentId} />
      </div>
      <ConfirmModal
        open={fullAccessSavePending}
        title={t('agent.settings.sessionPermissionDefault.fullAccessConfirmTitle', 'Use Full access by default?')}
        message={t(
          'agent.settings.sessionPermissionDefault.fullAccessConfirmMessage',
          'New conversations will skip routine session approval prompts. Enterprise access, safety, and destructive-action rules always apply.',
        )}
        confirmLabel={t('agent.settings.sessionPermissionDefault.fullAccessConfirm', 'Save Full access default')}
        onCancel={() => setFullAccessSavePending(false)}
        onConfirm={() => {
          setFullAccessSavePending(false);
          void persistSettings();
        }}
      />
    </div>
  );
}
