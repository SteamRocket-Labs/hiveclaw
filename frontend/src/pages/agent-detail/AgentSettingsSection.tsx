import React from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import ChannelConfig from '../../components/ChannelConfig';
import { agentApi } from '../../api/domains/agents';

type AgentSettingsForm = {
  primary_model_id: string;
  fallback_model_id: string;
  max_triggers: number;
  min_poll_interval_min: number;
  webhook_rate_limit: number;
};

interface AgentSettingsSectionProps {
  agentId: string;
  agent: any;
  llmModels: any[];
  permData: any;
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
  showDeleteConfirm: boolean;
  onSetShowDeleteConfirm: (value: boolean) => void;
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
  permData,
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
  showDeleteConfirm,
  onSetShowDeleteConfirm,
}: AgentSettingsSectionProps) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const hasChanges =
    settingsForm.primary_model_id !== (agent?.primary_model_id || '') ||
    settingsForm.fallback_model_id !== (agent?.fallback_model_id || '') ||
    settingsForm.max_triggers !== ((agent as any)?.max_triggers ?? 20) ||
    settingsForm.min_poll_interval_min !== ((agent as any)?.min_poll_interval_min ?? 5) ||
    settingsForm.webhook_rate_limit !== ((agent as any)?.webhook_rate_limit ?? 5);

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
      } as any);
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
      onResetSettingsInit();

      const clamped = result?._clamped_fields;
      if (clamped && clamped.length > 0) {
        const fieldNames: Record<string, string> = {
          min_poll_interval_min: t('agent.settings.clampedField.minPollInterval'),
          webhook_rate_limit: t('agent.settings.clampedField.webhookRateLimit'),
          heartbeat_interval_minutes: t('agent.settings.clampedField.heartbeatInterval'),
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

  const saveWelcomeMessage = async () => {
    try {
      await agentApi.update(agentId, { welcome_message: wmDraft } as any);
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
      onSetWmSaved(true);
      setTimeout(() => onSetWmSaved(false), 2000);
    } catch {}
  };

  const handleScopeChange = async (newScope: string) => {
    try {
      await agentApi.updatePermissions(agentId, {
        scope_type: newScope,
        scope_ids: [],
        access_level: permData?.access_level || 'use',
      });
      queryClient.invalidateQueries({ queryKey: ['agent-permissions', agentId] });
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
    } catch (e) {
      console.error('Failed to update permissions', e);
    }
  };

  const handleAccessLevelChange = async (newLevel: string) => {
    try {
      await agentApi.updatePermissions(agentId, {
        scope_type: permData?.scope_type || 'company',
        scope_ids: permData?.scope_ids || [],
        access_level: newLevel,
      });
      queryClient.invalidateQueries({ queryKey: ['agent-permissions', agentId] });
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
    } catch (e) {
      console.error('Failed to update access level', e);
    }
  };

  const isOwner = permData?.is_owner ?? false;
  const currentScope = permData?.scope_type || 'company';
  const currentAccessLevel = permData?.access_level || 'use';
  const scopeNames = permData?.scope_names || [];
  const scopeLabels: Record<string, string> = {
    company: '🏢 ' + t('agent.settings.perm.companyWide', 'Company-wide'),
    user: '👤 ' + t('agent.settings.perm.onlyMe', 'Only Me'),
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
        <h4 style={{ marginBottom: '4px' }}>{t('agent.settings.autonomy.title')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>{t('agent.settings.autonomy.description')}</p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {[
            { key: 'read_files', label: t('agent.settings.autonomy.readFiles'), desc: t('agent.settings.autonomy.readFilesDesc') },
            { key: 'write_workspace_files', label: t('agent.settings.autonomy.writeFiles'), desc: t('agent.settings.autonomy.writeFilesDesc') },
            { key: 'delete_files', label: t('agent.settings.autonomy.deleteFiles'), desc: t('agent.settings.autonomy.deleteFilesDesc') },
            { key: 'execute_code', label: t('agent.settings.autonomy.executeCode'), desc: t('agent.settings.autonomy.executeCodeDesc') },
            { key: 'send_email', label: t('agent.settings.autonomy.sendEmail'), desc: t('agent.settings.autonomy.sendEmailDesc') },
            { key: 'import_mcp_server', label: t('agent.settings.autonomy.installMcp'), desc: t('agent.settings.autonomy.installMcpDesc') },
            { key: 'send_feishu_message', label: t('agent.settings.autonomy.sendFeishu'), desc: t('agent.settings.autonomy.sendFeishuDesc') },
            { key: 'web_search', label: t('agent.settings.autonomy.webSearch'), desc: t('agent.settings.autonomy.webSearchDesc') },
            { key: 'manage_tasks', label: t('agent.settings.autonomy.manageTasks'), desc: t('agent.settings.autonomy.manageTasksDesc') },
          ].map((action) => {
            const currentLevel = (agent?.autonomy_policy as any)?.[action.key] || 'L1';
            return (
              <div
                key={action.key}
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
                  <div style={{ fontWeight: 500, fontSize: '13px' }}>{action.label}</div>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{action.desc}</div>
                </div>
                <select
                  className="input"
                  value={currentLevel}
                  onChange={async (e) => {
                    const newPolicy = { ...(agent?.autonomy_policy as any || {}), [action.key]: e.target.value };
                    await agentApi.update(agentId, { autonomy_policy: newPolicy } as any);
                    queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
                  }}
                  style={{
                    width: '140px',
                    fontSize: '12px',
                    color: currentLevel === 'L1' ? 'var(--success)' : currentLevel === 'L2' ? 'var(--warning)' : 'var(--error)',
                    fontWeight: 600,
                  }}
                >
                  <option value="L1">{t('agent.settings.autonomy.l1Auto')}</option>
                  <option value="L2">{t('agent.settings.autonomy.l2Notify')}</option>
                  <option value="L3">{t('agent.settings.autonomy.l3Approve')}</option>
                </select>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '12px' }}>🔒 {t('agent.settings.perm.title', 'Access Permissions')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.perm.description', 'Control who can see and interact with this agent. Only the creator or admin can change this.')}
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '16px' }}>
          {(['company', 'user'] as const).map((scope) => (
            <label
              key={scope}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                padding: '12px 14px',
                borderRadius: '8px',
                cursor: isOwner ? 'pointer' : 'default',
                border: currentScope === scope ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                background: currentScope === scope ? 'rgba(99,102,241,0.06)' : 'transparent',
                opacity: isOwner ? 1 : 0.7,
                transition: 'all 0.15s',
              }}
            >
              <input
                type="radio"
                name="perm_scope"
                checked={currentScope === scope}
                disabled={!isOwner}
                onChange={() => handleScopeChange(scope)}
                style={{ accentColor: 'var(--accent-primary)' }}
              />
              <div>
                <div style={{ fontWeight: 500, fontSize: '13px' }}>{scopeLabels[scope]}</div>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                  {scope === 'company' && t('agent.settings.perm.companyWideDesc', 'All users in the organization can use this agent')}
                  {scope === 'user' && t('agent.settings.perm.onlyMeDesc', 'Only the creator can use this agent')}
                </div>
              </div>
            </label>
          ))}
        </div>

        {currentScope === 'company' && isOwner && (
          <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px' }}>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '8px' }}>
              {t('agent.settings.perm.defaultAccess', 'Default Access Level')}
            </label>
            <div style={{ display: 'flex', gap: '8px' }}>
              {[
                { val: 'use', label: '👁️ ' + t('agent.settings.perm.useAccess', 'Use'), desc: t('agent.settings.perm.useAccessDesc', 'Task, Chat, Tools, Skills, Workspace') },
                { val: 'manage', label: '⚙️ ' + t('agent.settings.perm.manageAccess', 'Manage'), desc: t('agent.settings.perm.manageAccessDesc', 'Full access including Settings, Mind, Relationships') },
              ].map((opt) => (
                <label
                  key={opt.val}
                  style={{
                    flex: 1,
                    padding: '10px 12px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    border: currentAccessLevel === opt.val ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                    background: currentAccessLevel === opt.val ? 'rgba(99,102,241,0.06)' : 'transparent',
                    transition: 'all 0.15s',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <input
                      type="radio"
                      name="access_level"
                      checked={currentAccessLevel === opt.val}
                      onChange={() => handleAccessLevelChange(opt.val)}
                      style={{ accentColor: 'var(--accent-primary)' }}
                    />
                    <span style={{ fontWeight: 500, fontSize: '13px' }}>{opt.label}</span>
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px', marginLeft: '20px' }}>{opt.desc}</div>
                </label>
              ))}
            </div>
          </div>
        )}

        {currentScope !== 'company' && scopeNames.length > 0 && (
          <div style={{ marginTop: '12px', fontSize: '12px', color: 'var(--text-secondary)' }}>
            <span style={{ fontWeight: 500 }}>{t('agent.settings.perm.currentAccess', 'Current access')}:</span> {scopeNames.map((s: any) => s.name).join(', ')}
          </div>
        )}

        {!isOwner && (
          <div style={{ marginTop: '12px', fontSize: '11px', color: 'var(--text-tertiary)', fontStyle: 'italic' }}>
            {t('agent.settings.perm.readOnly', 'Only the creator or admin can change permissions')}
          </div>
        )}
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
        <h4 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>{t('agent.settings.heartbeat.title', 'Heartbeat')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.heartbeat.description', 'Periodic awareness check — agent proactively monitors the plaza and work environment.')}
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
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
              <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.heartbeat.enabled', 'Enable Heartbeat')}</div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('agent.settings.heartbeat.enabledDesc', 'Agent will periodically check plaza and work status')}</div>
            </div>
            <label style={{ position: 'relative', display: 'inline-block', width: '44px', height: '24px', cursor: canManage ? 'pointer' : 'default' }}>
              <input
                type="checkbox"
                checked={agent?.heartbeat_enabled ?? true}
                disabled={!canManage}
                onChange={async (e) => {
                  if (!canManage) return;
                  await agentApi.update(agentId, { heartbeat_enabled: e.target.checked } as any);
                  queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
                }}
                style={{ opacity: 0, width: 0, height: 0 }}
              />
              <span
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  right: 0,
                  bottom: 0,
                  background: (agent?.heartbeat_enabled ?? true) ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                  borderRadius: '12px',
                  transition: 'background 0.2s',
                  opacity: canManage ? 1 : 0.6,
                }}
              >
                <span
                  style={{
                    position: 'absolute',
                    top: '3px',
                    left: (agent?.heartbeat_enabled ?? true) ? '23px' : '3px',
                    width: '18px',
                    height: '18px',
                    background: 'white',
                    borderRadius: '50%',
                    transition: 'left 0.2s',
                  }}
                />
              </span>
            </label>
          </div>

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
              <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.heartbeat.interval', 'Check Interval')}</div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('agent.settings.heartbeat.intervalDesc', 'How often the agent checks for updates')}</div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <input
                type="number"
                className="input"
                disabled={!canManage}
                min={1}
                defaultValue={agent?.heartbeat_interval_minutes ?? 120}
                key={agent?.heartbeat_interval_minutes}
                onBlur={async (e) => {
                  if (!canManage) return;
                  const val = Math.max(1, Number(e.target.value) || 120);
                  e.target.value = String(val);
                  await agentApi.update(agentId, { heartbeat_interval_minutes: val } as any);
                  queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
                }}
                style={{ width: '80px', fontSize: '12px', opacity: canManage ? 1 : 0.6 }}
              />
              <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('common.minutes', 'min')}</span>
            </div>
          </div>

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
              <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.heartbeat.activeHours', 'Active Hours')}</div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('agent.settings.heartbeat.activeHoursDesc', 'Only trigger heartbeat during these hours (HH:MM-HH:MM)')}</div>
            </div>
            <input
              className="input"
              disabled={!canManage}
              value={agent?.heartbeat_active_hours ?? '09:00-18:00'}
              onChange={async (e) => {
                if (!canManage) return;
                await agentApi.update(agentId, { heartbeat_active_hours: e.target.value } as any);
                queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
              }}
              style={{ width: '140px', fontSize: '12px', textAlign: 'center', opacity: canManage ? 1 : 0.6 }}
              placeholder="09:00-18:00"
            />
          </div>

          {agent?.last_heartbeat_at && (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', paddingLeft: '4px' }}>
              {t('agent.settings.heartbeat.lastRun', 'Last heartbeat')}: {new Date(agent.last_heartbeat_at).toLocaleString()}
            </div>
          )}
        </div>
      </div>

      <div style={{ marginBottom: '12px' }}>
        <ChannelConfig mode="edit" agentId={agentId} />
      </div>

      <div className="card" style={{ borderColor: 'var(--error)' }}>
        <h4 style={{ color: 'var(--error)', marginBottom: '12px' }}>{t('agent.settings.danger.title')}</h4>
        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '12px' }}>{t('agent.settings.danger.deleteWarning')}</p>
        {!showDeleteConfirm ? (
          <button className="btn btn-danger" onClick={() => onSetShowDeleteConfirm(true)}>
            × {t('agent.settings.danger.deleteAgent')}
          </button>
        ) : (
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <span style={{ fontSize: '13px', color: 'var(--error)', fontWeight: 600 }}>{t('agent.settings.danger.deleteWarning')}</span>
            <button
              className="btn btn-danger"
              onClick={async () => {
                try {
                  await agentApi.remove(agentId);
                  queryClient.invalidateQueries({ queryKey: ['agents'] });
                  navigate('/');
                } catch (err: any) {
                  alert(err?.message || 'Failed to delete agent');
                }
              }}
            >
              {t('agent.settings.danger.confirmDelete')}
            </button>
            <button className="btn btn-secondary" onClick={() => onSetShowDeleteConfirm(false)}>
              {t('common.cancel')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
