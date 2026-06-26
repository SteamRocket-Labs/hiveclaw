import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import ChannelConfig from '../../components/ChannelConfig';
import { agentApi } from '../../api/domains/agents';
import { planApi, type PlanRecommendationCreateInput } from '../../api/domains/plans';
import { triggerApi } from '../../api/domains/triggers';

type AgentSettingsForm = {
  primary_model_id: string;
  fallback_model_id: string;
  max_triggers: number;
  min_poll_interval_min: number;
  webhook_rate_limit: number;
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

  React.useEffect(() => {
    setPatrolForm(persistedPatrolForm);
  }, [persistedPatrolForm]);

  const hasChanges =
    settingsForm.primary_model_id !== (agent?.primary_model_id || '') ||
    settingsForm.fallback_model_id !== (agent?.fallback_model_id || '') ||
    settingsForm.max_triggers !== ((agent as any)?.max_triggers ?? 20) ||
    settingsForm.min_poll_interval_min !== ((agent as any)?.min_poll_interval_min ?? 5) ||
    settingsForm.webhook_rate_limit !== ((agent as any)?.webhook_rate_limit ?? 5) ||
    settingsForm.smart_model_routing_enabled !== !!((agent as any)?.smart_model_routing?.enabled) ||
    settingsForm.security_zone !== ((agent as any)?.security_zone || 'standard');
  const patrolHasChanges =
    patrolForm.enabled !== persistedPatrolForm.enabled ||
    patrolForm.intervalMinutes !== persistedPatrolForm.intervalMinutes ||
    patrolForm.activeStart !== persistedPatrolForm.activeStart ||
    patrolForm.activeEnd !== persistedPatrolForm.activeEnd;

  const handleSaveSettings = async () => {
    onSetSettingsSaving(true);
    onSetSettingsError('');
    try {
      const result: any = await agentApi.update(agentId, {
        primary_model_id: settingsForm.primary_model_id || null,
        fallback_model_id: settingsForm.fallback_model_id || null,
        max_triggers: settingsForm.max_triggers,
        min_poll_interval_min: settingsForm.min_poll_interval_min,
        webhook_rate_limit: settingsForm.webhook_rate_limit,
        security_zone: settingsForm.security_zone,
        smart_model_routing: settingsForm.smart_model_routing_enabled
          ? { enabled: true, max_simple_chars: 160, max_simple_words: 28 }
          : null,
      } as any);
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

  const handleSavePatrolSettings = async () => {
    if (!canManage) return;
    if (!isValidActiveTime(patrolForm.activeStart) || !isValidActiveTime(patrolForm.activeEnd)) {
      setPatrolError(t('agent.settings.patrol.invalidActiveHours', 'Select valid start and end times.'));
      return;
    }

    const minutes = clampPatrolInterval(patrolForm.intervalMinutes);
    const activeHours = buildActiveHours(patrolForm);
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
      const needsPlanModeOptOut = patrolForm.enabled;
      if (needsPlanModeOptOut) {
        const actionKind = patrolTrigger ? 'enable_autonomous_wake' : 'create_enabled_trigger';
        const recommendation = await planApi.createRecommendation(
          agentId,
          buildPatrolPlanRecommendationInput({ agentId, reason, actionKind }),
        );
        const declined = await planApi.declineRecommendation(agentId, recommendation.id);
        planRecommendationId = declined.id;
      }
      if (patrolTrigger) {
        await triggerApi.update(agentId, patrolTrigger.id, {
          is_enabled: patrolForm.enabled,
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
      setPatrolSaved(true);
      setTimeout(() => setPatrolSaved(false), 2000);
    } catch (e: any) {
      setPatrolError(e?.message || t('agent.settings.patrol.saveError', 'Failed to save patrol settings'));
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
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: '16px',
          position: 'sticky',
          top: 0,
          zIndex: 10,
          background: 'var(--bg-primary)',
          paddingTop: '4px',
          paddingBottom: '12px',
          borderBottom: '1px solid var(--border-subtle)',
        }}
      >
        <h3 style={{ margin: 0 }}>{t('agent.settings.title')}</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          {settingsSaved && <span style={{ fontSize: '12px', color: 'var(--success)' }}>{t('agent.settings.saved', 'Saved')}</span>}
          {settingsError && (
            <span
              style={{
                fontSize: '12px',
                color: settingsError.includes('adjusted') ? 'var(--warning)' : 'var(--error)',
                whiteSpace: 'pre-line',
              }}
            >
              {settingsError}
            </span>
          )}
          <button
            className="btn btn-primary"
            disabled={!hasChanges || settingsSaving}
            onClick={handleSaveSettings}
            style={{
              opacity: hasChanges ? 1 : 0.5,
              cursor: hasChanges ? 'pointer' : 'default',
              padding: '6px 20px',
              fontSize: '13px',
            }}
          >
            {settingsSaving ? t('agent.settings.saving', 'Saving...') : t('agent.settings.save', 'Save')}
          </button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '12px' }}>{t('agent.settings.modelConfig')}</h4>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>{t('agent.settings.primaryModel')}</label>
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
              <div style={{ fontSize: '11px', color: 'var(--error)', marginTop: '4px' }}>
                {t('agent.settings.modelDisabledWarning', 'This model has been disabled by admin. The agent will automatically use the fallback model.')}
              </div>
            )}
            {!settingsForm.primary_model_id && settingsForm.fallback_model_id && (() => {
              const fb = llmModels.find((m: any) => m.id === settingsForm.fallback_model_id);
              return fb ? (
                <div style={{ fontSize: '11px', color: 'var(--accent)', marginTop: '4px' }}>
                  {t('agent.settings.usingFallback', { model: fb.label })}
                </div>
              ) : null;
            })()}
            {!settingsForm.primary_model_id && !settingsForm.fallback_model_id && llmModels.length > 0 && (
              <div style={{ fontSize: '11px', color: 'var(--warning)', marginTop: '4px' }}>
                {t('agent.settings.noModelWarning')}
              </div>
            )}
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('agent.settings.primaryModel')}</div>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>{t('agent.settings.fallbackModel')}</label>
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
              <div style={{ fontSize: '11px', color: 'var(--error)', marginTop: '4px' }}>
                {t('agent.settings.modelDisabledWarning', 'This model has been disabled by admin. The agent will automatically use the fallback model.')}
              </div>
            )}
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('agent.settings.fallbackModel')}</div>
          </div>
          {settingsForm.fallback_model_id && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '10px 14px',
                background: 'var(--bg-elevated)',
                borderRadius: '8px',
                border: '1px solid var(--border-subtle)',
              }}
            >
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.smartRouting', 'Smart Model Routing')}</div>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                  {t('agent.settings.smartRoutingDesc', 'Automatically use the fallback model for simple conversational turns to save costs. Complex tasks always use the primary model.')}
                </div>
              </div>
              <label style={{ position: 'relative', display: 'inline-block', width: '36px', height: '20px', flexShrink: 0, marginLeft: '12px' }}>
                <input
                  type="checkbox"
                  checked={settingsForm.smart_model_routing_enabled}
                  onChange={(e) => onSettingsFormChange((f) => ({ ...f, smart_model_routing_enabled: e.target.checked }))}
                  style={{ opacity: 0, width: 0, height: 0 }}
                />
                <span
                  style={{
                    position: 'absolute',
                    cursor: 'pointer',
                    inset: 0,
                    background: settingsForm.smart_model_routing_enabled ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                    borderRadius: '10px',
                    transition: 'background 0.2s',
                  }}
                >
                  <span
                    style={{
                      position: 'absolute',
                      height: '14px',
                      width: '14px',
                      left: settingsForm.smart_model_routing_enabled ? '19px' : '3px',
                      bottom: '3px',
                      background: 'white',
                      borderRadius: '50%',
                      transition: 'left 0.2s',
                    }}
                  />
                </span>
              </label>
            </div>
          )}
        </div>
      </div>


      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '12px' }}>{t('agent.settings.tokenStats')}</h4>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px' }}>
          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>{t('agent.settings.tokenToday')}</div>
            <div style={{ fontSize: '18px', fontWeight: 600 }}>{formatTokens(agent?.tokens_used_today || 0)}</div>
          </div>
          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>{t('agent.settings.tokenMonth')}</div>
            <div style={{ fontSize: '18px', fontWeight: 600 }}>{formatTokens(agent?.tokens_used_month || 0)}</div>
          </div>
          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>{t('agent.settings.tokenTotal')}</div>
            <div style={{ fontSize: '18px', fontWeight: 600 }}>{formatTokens(agent?.tokens_used_total || 0)}</div>
          </div>
        </div>
        <div style={{ fontSize: '11px', color: 'var(--text-quaternary)', marginTop: '8px' }}>
          {t('agent.settings.tokenQuotaHint')}
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px' }}>{t('agent.settings.triggerLimits')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.settings.triggerLimitsDesc')}
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px' }}>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>
              {t('agent.settings.maxTriggers')}
            </label>
            <input
              className="input"
              type="number"
              min={1}
              max={100}
              value={settingsForm.max_triggers}
              onChange={(e) =>
                onSettingsFormChange((f) => ({ ...f, max_triggers: Math.max(1, Math.min(100, parseInt(e.target.value, 10) || 20)) }))
              }
              style={{ width: '100%' }}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('agent.settings.maxTriggersDesc')}
            </div>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>
              {t('agent.settings.minPollInterval')}
            </label>
            <input
              className="input"
              type="number"
              min={1}
              max={60}
              value={settingsForm.min_poll_interval_min}
              onChange={(e) =>
                onSettingsFormChange((f) => ({ ...f, min_poll_interval_min: Math.max(1, Math.min(60, parseInt(e.target.value, 10) || 5)) }))
              }
              style={{ width: '100%' }}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('agent.settings.minPollIntervalDesc')}
            </div>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>
              {t('agent.settings.webhookRateLimit')}
            </label>
            <input
              className="input"
              type="number"
              min={1}
              max={60}
              value={settingsForm.webhook_rate_limit}
              onChange={(e) =>
                onSettingsFormChange((f) => ({ ...f, webhook_rate_limit: Math.max(1, Math.min(60, parseInt(e.target.value, 10) || 5)) }))
              }
              style={{ width: '100%' }}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('agent.settings.webhookRateLimitDesc')}
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
          <h4 style={{ margin: 0 }}>{t('agent.settings.welcomeMessage')}</h4>
          {wmSaved && <span style={{ fontSize: '12px', color: 'var(--success)' }}>✓ {t('agent.settings.saved')}</span>}
        </div>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.settings.welcomeMessageDesc')}
        </p>
        <textarea
          className="input"
          rows={4}
          value={wmDraft}
          onChange={(e) => onSetWmDraft(e.target.value)}
          onBlur={saveWelcomeMessage}
          placeholder={t('agent.settings.welcomeMessagePlaceholder')}
          style={{
            width: '100%',
            minHeight: '80px',
            resize: 'vertical',
            fontFamily: 'inherit',
            fontSize: '13px',
          }}
        />
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>{t('agent.settings.timezone.title', '🌐 Timezone')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.timezone.description', "The timezone used for this agent's scheduling, active hours, and time awareness. Defaults to the company timezone if not set.")}
        </p>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '10px 14px',
            background: 'var(--bg-elevated)',
            borderRadius: '8px',
            border: '1px solid var(--border-subtle)',
          }}
        >
          <div>
            <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.timezone.current', 'Agent Timezone')}</div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {agent?.timezone
                ? t('agent.settings.timezone.override', 'Custom timezone for this agent')
                : t('agent.settings.timezone.inherited', 'Using company default timezone')}
            </div>
          </div>
          <select
            className="input"
            disabled={!canManage}
            value={agent?.timezone || ''}
            onChange={async (e) => {
              if (!canManage) return;
              const val = e.target.value || null;
              await agentApi.update(agentId, { timezone: val } as any);
              queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
            }}
            style={{ width: '200px', fontSize: '12px', opacity: canManage ? 1 : 0.6 }}
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

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>
          {t('agent.settings.executionMode.title', 'Execution Mode')}
        </h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.executionMode.description', 'Choose whether this agent runs as a normal worker or as a coordinator that primarily delegates to other agents.')}
        </p>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '10px 14px',
            background: 'var(--bg-elevated)',
            borderRadius: '8px',
            border: '1px solid var(--border-subtle)',
          }}
        >
          <div>
            <div style={{ fontWeight: 500, fontSize: '13px' }}>
              {t('agent.settings.executionMode.current', 'Current Mode')}
            </div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {(agent?.execution_mode || 'standard') === 'coordinator'
                ? t('agent.settings.executionMode.coordinatorDesc', 'Delegates and synthesizes work across worker agents')
                : t('agent.settings.executionMode.standardDesc', 'Uses the normal single-agent runtime')}
            </div>
          </div>
          <select
            className="input"
            disabled={!canManage}
            value={agent?.execution_mode || 'standard'}
            onChange={async (e) => {
              if (!canManage) return;
              await agentApi.update(agentId, { execution_mode: e.target.value as 'standard' | 'coordinator' });
              queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
            }}
            style={{ width: '220px', fontSize: '12px', opacity: canManage ? 1 : 0.6 }}
          >
            <option value="standard">{t('agent.settings.executionMode.standard', 'Standard')}</option>
            <option value="coordinator">{t('agent.settings.executionMode.coordinator', 'Coordinator')}</option>
          </select>
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>{t('agent.settings.patrol.title', 'Patrol & Agent Circle')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.patrol.description', 'Configure the user-facing patrol trigger and Agent Circle permissions. Internal maintenance stays platform-managed.')}
        </p>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '10px 14px',
            background: 'var(--bg-elevated)',
            borderRadius: '8px',
            border: '1px solid var(--border-subtle)',
            marginBottom: '12px',
          }}
        >
          <div>
            <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.patrol.enabled', 'Enable patrol')}</div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {t('agent.settings.patrol.enabledDesc', 'Creates or pauses the user-facing interval trigger. Internal maintenance is always managed by the platform.')}
            </div>
          </div>
          <label style={{ position: 'relative', display: 'inline-block', width: '42px', height: '24px', flexShrink: 0, marginLeft: '12px' }}>
            <input
              type="checkbox"
              checked={patrolForm.enabled}
              disabled={!canManage || patrolLoading}
              onChange={(e) => setPatrolForm((prev) => ({ ...prev, enabled: e.target.checked }))}
              style={{ opacity: 0, width: 0, height: 0 }}
            />
            <span
              style={{
                position: 'absolute',
                cursor: canManage ? 'pointer' : 'default',
                inset: 0,
                background: patrolForm.enabled ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                borderRadius: '12px',
                transition: 'background 0.2s',
                opacity: !canManage || patrolLoading ? 0.6 : 1,
              }}
            >
              <span
                style={{
                  position: 'absolute',
                  height: '18px',
                  width: '18px',
                  left: patrolForm.enabled ? '21px' : '3px',
                  bottom: '3px',
                  background: 'white',
                  borderRadius: '50%',
                  transition: 'left 0.2s',
                }}
              />
            </span>
          </label>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '12px', marginBottom: '12px' }}>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>
              {t('agent.settings.patrol.interval', 'Patrol interval')}
            </label>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input
                className="input"
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
                style={{ width: '120px' }}
              />
              <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('common.minutes', 'min')}</span>
            </div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('agent.settings.patrol.intervalDesc', 'How often this employee wakes up for patrol work.')}
            </div>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>
              {t('agent.settings.patrol.activeHours', 'Active hours')}
            </label>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--text-secondary)' }}>
                <span>{t('agent.settings.patrol.activeStart', 'Start')}</span>
                <input
                  className="input"
                  type="time"
                  value={patrolForm.activeStart}
                  disabled={!canManage || patrolLoading}
                  onChange={(e) => setPatrolForm((prev) => ({ ...prev, activeStart: e.target.value }))}
                  style={{ width: '118px' }}
                />
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--text-secondary)' }}>
                <span>{t('agent.settings.patrol.activeEnd', 'End')}</span>
                <input
                  className="input"
                  type="time"
                  value={patrolForm.activeEnd}
                  disabled={!canManage || patrolLoading}
                  onChange={(e) => setPatrolForm((prev) => ({ ...prev, activeEnd: e.target.value }))}
                  style={{ width: '118px' }}
                />
              </label>
            </div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('agent.settings.patrol.activeHoursDesc', 'Only run patrol triggers inside this local time window.')}
            </div>
          </div>
          <div>
            <div style={{ fontWeight: 500, fontSize: '13px', marginBottom: '6px' }}>
              {t('agent.settings.patrol.lastRun', 'Last patrol')}
            </div>
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', minHeight: '36px', display: 'flex', alignItems: 'center' }}>
              {patrolTrigger?.last_fired_at
                ? new Date(patrolTrigger.last_fired_at).toLocaleString(i18n.language || undefined)
                : t('agent.settings.patrol.neverRun', 'Not run yet')}
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px' }}>
          <button
            className="btn btn-secondary"
            disabled={!canManage || patrolLoading || patrolSaving || (!patrolTrigger && !patrolForm.enabled) || !patrolHasChanges}
            onClick={handleSavePatrolSettings}
            style={{ fontSize: '12px', padding: '6px 12px', opacity: !canManage || !patrolHasChanges ? 0.6 : 1 }}
          >
            {patrolSaving ? t('agent.settings.patrol.saving', 'Saving patrol...') : t('agent.settings.patrol.save', 'Save patrol settings')}
          </button>
          {patrolSaved && <span style={{ fontSize: '12px', color: 'var(--success)' }}>{t('agent.settings.patrol.saved', 'Patrol settings saved')}</span>}
          {patrolError && <span style={{ fontSize: '12px', color: 'var(--error)' }}>{patrolError}</span>}
        </div>
      </div>

      <div style={{ marginBottom: '12px' }}>
        <ChannelConfig mode="edit" agentId={agentId} />
      </div>
    </div>
  );
}
