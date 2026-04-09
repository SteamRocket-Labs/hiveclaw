import React from 'react';
import { useTranslation } from 'react-i18next';
import type { AgentCapabilityInstall } from '../../api/domains/agents';

type AgentStatusSectionProps = {
  agent: any;
  llmModels: any[];
  metrics?: any;
  activityLogs?: any[];
  capabilityInstalls?: AgentCapabilityInstall[];
  statusKey: string;
  onSelectTab: (tab: string) => void;
};

const formatTokens = (value: number) => {
  if (!value) return '0';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
};

const formatDate = (value: string) => {
  try {
    return new Date(value).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  } catch {
    return value;
  }
};

export default function AgentStatusSection({
  agent,
  llmModels,
  metrics,
  activityLogs = [],
  capabilityInstalls = [],
  statusKey,
  onSelectTab,
}: AgentStatusSectionProps) {
  const { t } = useTranslation();
  const primaryModel = llmModels.find((model: any) => model.id === agent.primary_model_id);
  const modelLabel = primaryModel ? primaryModel.label || primaryModel.model : '—';
  const modelProvider = primaryModel ? primaryModel.provider : '—';
  const installedCount = capabilityInstalls.filter((item) => item.status === 'installed').length;
  const pendingCount = capabilityInstalls.filter((item) => item.status === 'pending').length;
  const failedItems = capabilityInstalls.filter((item) => item.status === 'failed');

  return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '12px', marginBottom: '24px' }}>
        <div className="card">
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>📋 {t('agent.tabs.status')}</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span className={`status-dot ${statusKey}`} />
            <span style={{ fontSize: '16px', fontWeight: 500 }}>{t(`agent.status.${statusKey}`)}</span>
          </div>
        </div>
        <div className="card">
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>🗓️ {t('agent.settings.today')} Token</div>
          <div style={{ fontSize: '22px', fontWeight: 600 }}>{formatTokens(agent.tokens_used_today)}</div>
          {agent.max_tokens_per_day && (
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
              {t('agent.settings.noLimit')} {formatTokens(agent.max_tokens_per_day)}
            </div>
          )}
        </div>
        <div className="card">
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>📅 {t('agent.settings.month')} Token</div>
          <div style={{ fontSize: '22px', fontWeight: 600 }}>{formatTokens(agent.tokens_used_month)}</div>
          {agent.max_tokens_per_month && (
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
              {t('agent.settings.noLimit')} {formatTokens(agent.max_tokens_per_month)}
            </div>
          )}
        </div>
        {agent?.agent_type !== 'openclaw' && (
          <>
            <div className="card">
              <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.status.llmCallsToday')}</div>
              <div style={{ fontSize: '22px', fontWeight: 600 }}>{(agent.llm_calls_today || 0).toLocaleString()}</div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                {t('agent.status.max')}: {(agent.max_llm_calls_per_day || 100).toLocaleString()}
              </div>
            </div>
            <div className="card">
              <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.status.totalToken')}</div>
              <div style={{ fontSize: '22px', fontWeight: 600 }}>{formatTokens(agent.tokens_used_total || 0)}</div>
            </div>
            {metrics && (
              <>
                <div className="card">
                  <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>✅ {t('agent.tasks.done')}</div>
                  <div style={{ fontSize: '22px', fontWeight: 600 }}>
                    {metrics.tasks?.done || 0}/{metrics.tasks?.total || 0}
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{metrics.tasks?.completion_rate || 0}%</div>
                </div>
                <div className="card">
                  <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.status.pending')}</div>
                  <div style={{ fontSize: '22px', fontWeight: 600, color: metrics.approvals?.pending > 0 ? 'var(--warning)' : 'inherit' }}>
                    {metrics.approvals?.pending || 0}
                  </div>
                </div>
                <div className="card" style={{ position: 'relative' }}>
                  <div className="metric-tooltip-trigger" style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px', cursor: 'help', display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                    {t('agent.status.24hActions')}
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                      <circle cx="8" cy="8" r="6.5" />
                      <path d="M8 7v4M8 5.5v0" />
                    </svg>
                    <span className="metric-tooltip">{t('agent.status.24hActionsTooltip')}</span>
                  </div>
                  <div style={{ fontSize: '22px', fontWeight: 600 }}>{metrics.activity?.actions_last_24h || 0}</div>
                </div>
              </>
            )}
          </>
        )}
        {agent?.agent_type === 'openclaw' && (
          <div className="card">
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.openclaw.lastSeen')}</div>
            <div style={{ fontSize: '16px', fontWeight: 500 }}>
              {agent.openclaw_last_seen ? new Date(agent.openclaw_last_seen).toLocaleString() : t('agent.openclaw.notConnected')}
            </div>
          </div>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '24px' }}>
        <div className="card">
          <h3 style={{ fontSize: '14px', fontWeight: 600, marginBottom: '12px' }}>{t('agent.profile.title')}</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px', gap: '12px' }}>
              <span style={{ color: 'var(--text-tertiary)', flexShrink: 0 }}>{t('agent.fields.role')}</span>
              <span title={agent.role_description || ''} style={{ textAlign: 'right', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' as any }}>
                {agent.role_description || '—'}
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
              <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.profile.created')}</span>
              <span>{agent.created_at ? formatDate(agent.created_at) : '—'}</span>
            </div>
            {agent.creator_username && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.fields.createdBy', 'Created by')}</span>
                <span style={{ color: 'var(--text-secondary)' }}>@{agent.creator_username}</span>
              </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
              <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.profile.lastActive')}</span>
              <span>{agent.last_active_at ? formatDate(agent.last_active_at) : '—'}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
              <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.profile.timezone')}</span>
              <span>{agent.effective_timezone || agent.timezone || 'UTC'}</span>
            </div>
          </div>
        </div>
        {agent?.agent_type !== 'openclaw' ? (
          <div className="card">
            <h3 style={{ fontSize: '14px', fontWeight: 600, marginBottom: '12px' }}>{t('agent.modelConfig.title')}</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.modelConfig.model')}</span>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: '12px' }}>{modelLabel}</span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.modelConfig.provider')}</span>
                <span style={{ textTransform: 'capitalize' }}>{modelProvider}</span>
              </div>
            </div>
          </div>
        ) : (
          <div className="card">
            <h3 style={{ fontSize: '14px', fontWeight: 600, marginBottom: '12px' }}>{t('agent.openclaw.connection')}</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.openclaw.type')}</span>
                <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span style={{ fontSize: '10px', padding: '2px 6px', borderRadius: '4px', background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff', fontWeight: 600 }}>
                    OpenClaw
                  </span>
                  Lab
                </span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.openclaw.lastSeen')}</span>
                <span>{agent.openclaw_last_seen ? new Date(agent.openclaw_last_seen).toLocaleString() : t('agent.openclaw.never')}</span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span style={{ color: 'var(--text-tertiary)' }}>{t('agent.openclaw.model')}</span>
                <span style={{ color: 'var(--text-secondary)' }}>{t('agent.openclaw.managedBy')}</span>
              </div>
            </div>
          </div>
        )}
      </div>

      {capabilityInstalls.length > 0 && (
        <div className="card" style={{ marginBottom: '24px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: '12px', marginBottom: '12px', flexWrap: 'wrap' }}>
            <h3 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>
              {t('agent.status.capabilityInstallTitle', 'Capability Install Status')}
            </h3>
            <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {capabilityInstalls.length} {t('agent.status.capabilityItems', 'items')}
            </span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '10px', marginBottom: '14px' }}>
            <div style={{ padding: '10px', borderRadius: '8px', background: 'var(--bg-secondary)' }}>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>
                {t('agent.status.installedCount', 'Installed')}
              </div>
              <div style={{ fontWeight: 600, fontSize: '18px' }}>{installedCount}</div>
            </div>
            <div style={{ padding: '10px', borderRadius: '8px', background: 'var(--bg-secondary)' }}>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>
                {t('agent.status.pendingCount', 'Pending')}
              </div>
              <div style={{ fontWeight: 600, fontSize: '18px' }}>{pendingCount}</div>
            </div>
            <div style={{ padding: '10px', borderRadius: '8px', background: 'var(--bg-secondary)' }}>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>
                {t('agent.status.failedCount', 'Failed')}
              </div>
              <div style={{ fontWeight: 600, fontSize: '18px', color: failedItems.length > 0 ? 'var(--danger)' : 'inherit' }}>
                {failedItems.length}
              </div>
            </div>
          </div>

          {failedItems.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)' }}>
                {t('agent.status.installIssues', 'Install Issues')}
              </div>
              {failedItems.slice(0, 3).map((item) => (
                <div
                  key={item.id}
                  style={{ padding: '10px', borderRadius: '8px', background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)' }}
                >
                  <div style={{ fontSize: '13px', fontWeight: 600 }}>{item.display_name || item.source_key || item.kind}</div>
                  <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
                    {item.error_message || t('agent.status.installFailed', 'Installation failed')}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
              {t('agent.status.installHealthy', 'All requested capabilities are currently installed or pending.')}
            </div>
          )}
        </div>
      )}

      {activityLogs.length > 0 && (
        <div className="card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <h3 style={{ fontSize: '14px', fontWeight: 600 }}>📊 Recent Activity</h3>
            <button className="btn btn-ghost" style={{ fontSize: '12px' }} onClick={() => onSelectTab('activityLog')}>
              View All →
            </button>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {activityLogs.slice(0, 5).map((log: any, index: number) => (
              <div key={log.id || index} style={{ display: 'flex', gap: '12px', alignItems: 'flex-start', padding: '6px 0', borderBottom: index < 4 ? '1px solid var(--border-subtle)' : 'none' }}>
                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', minWidth: '60px', flexShrink: 0 }}>
                  {new Date(log.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </span>
                <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>{log.summary || log.action_type}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: 'flex', gap: '10px', marginTop: '20px' }}>
        <button className="btn btn-secondary" onClick={() => onSelectTab('chat')}>
          {t('agent.actions.chat')}
        </button>
        {agent?.agent_type !== 'openclaw' && (
          <button className="btn btn-secondary" onClick={() => onSelectTab('aware')}>
            {t('agent.tabs.aware')}
          </button>
        )}
        <button className="btn btn-secondary" onClick={() => onSelectTab('settings')}>
          {t('agent.tabs.settings')}
        </button>
      </div>
    </div>
  );
}
