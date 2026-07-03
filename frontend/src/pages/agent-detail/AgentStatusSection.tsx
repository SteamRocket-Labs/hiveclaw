import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import type { AgentCapabilityInstall, AgentChannelCapability } from '../../api/domains/agents';
import { runtimeBudgetApi } from '../../api/domains/runtimeBudgets';
import './AgentStatusSection.css';

type AgentStatusSectionProps = {
  agent: any;
  llmModels: any[];
  metrics?: any;
  activityLogs?: any[];
  capabilityInstalls?: AgentCapabilityInstall[];
  channelCapabilities?: AgentChannelCapability[];
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
  channelCapabilities = [],
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
  const runtimeBudgetQuery = useQuery({
    queryKey: ['agent-runtime-budget-runs', agent.id],
    queryFn: () => runtimeBudgetApi.listRuns({ agentId: agent.id, limit: 5 }),
    enabled: !!agent.id,
    staleTime: 30_000,
  });
  const runtimeRuns = runtimeBudgetQuery.data || [];
  const protectedRun = runtimeRuns.find((run) => ['exhausted', 'hard_stopped', 'expired', 'cancelled'].includes(run.status));
  const latestRun = protectedRun || runtimeRuns[0];
  const renderCapabilityValue = (value: boolean | string) => {
    if (value === true) return t('agent.status.capabilitySupported', 'Supported');
    if (value === false) return t('agent.status.capabilityUnsupported', 'Unsupported');
    return String(value);
  };

  return (
    <div>
      <div className="agent-status-metrics-grid">
        <div className="card">
          <div className="agent-status-metric-label">📋 {t('agent.tabs.status')}</div>
          <div className="agent-status-status-row">
            <span className={`status-dot ${statusKey}`} />
            <span className="agent-status-status-value">{t(`agent.status.${statusKey}`)}</span>
          </div>
        </div>
        <div className="card">
          <div className="agent-status-metric-label">🗓️ {t('agent.settings.today')} Token</div>
          <div className="agent-status-metric-value">{formatTokens(agent.tokens_used_today)}</div>
          {agent.max_tokens_per_day && (
            <div className="agent-status-metric-sub">
              {t('agent.settings.noLimit')} {formatTokens(agent.max_tokens_per_day)}
            </div>
          )}
        </div>
        <div className="card">
          <div className="agent-status-metric-label">📅 {t('agent.settings.month')} Token</div>
          <div className="agent-status-metric-value">{formatTokens(agent.tokens_used_month)}</div>
          {agent.max_tokens_per_month && (
            <div className="agent-status-metric-sub">
              {t('agent.settings.noLimit')} {formatTokens(agent.max_tokens_per_month)}
            </div>
          )}
        </div>
        <div className="card">
          <div className="agent-status-metric-label">{t('agent.status.llmCallsToday')}</div>
          <div className="agent-status-metric-value">{(agent.llm_calls_today || 0).toLocaleString()}</div>
          <div className="agent-status-metric-sub">
            {t('agent.status.max')}: {(agent.max_llm_calls_per_day || 100).toLocaleString()}
          </div>
        </div>
        <div className="card">
          <div className="agent-status-metric-label">{t('agent.status.totalToken')}</div>
          <div className="agent-status-metric-value">{formatTokens(agent.tokens_used_total || 0)}</div>
        </div>
        {metrics && (
          <>
            <div className="card">
              <div className="agent-status-metric-label">✅ {t('agent.tasks.done')}</div>
              <div className="agent-status-metric-value">
                {metrics.tasks?.done || 0}/{metrics.tasks?.total || 0}
              </div>
              <div className="u-meta u-tertiary">{metrics.tasks?.completion_rate || 0}%</div>
            </div>
            <div className="card">
              <div className="agent-status-metric-label">{t('agent.status.pending')}</div>
              <div
                className="agent-status-metric-value"
                style={{ color: metrics.approvals?.pending > 0 ? 'var(--warning)' : 'inherit' }}
              >
                {metrics.approvals?.pending || 0}
              </div>
            </div>
            <div className="card agent-status-card-tooltip">
              <div className="metric-tooltip-trigger agent-status-tooltip-trigger">
                {t('agent.status.24hActions')}
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <circle cx="8" cy="8" r="6.5" />
                  <path d="M8 7v4M8 5.5v0" />
                </svg>
                <span className="metric-tooltip">{t('agent.status.24hActionsTooltip')}</span>
              </div>
              <div className="agent-status-metric-value">{metrics.activity?.actions_last_24h || 0}</div>
            </div>
          </>
        )}
      </div>

      <div className={`card agent-status-runtime-card ${protectedRun ? 'is-warning' : 'is-healthy'}`}>
        <div>
          <h3 className="agent-status-card-title">{t('agent.status.runtimeProtection', 'Runtime Protection')}</h3>
          <div className="agent-status-runtime-main">
            {latestRun
              ? latestRun.user_status
              : t('agent.status.runtimeHealthy', 'No protected runs')}
          </div>
          <div className="agent-status-runtime-reason">
            {latestRun
              ? latestRun.user_reason
              : t('agent.status.runtimeHealthyDesc', 'This employee has no recent run stopped by the platform guard.')}
          </div>
          {latestRun && (
            <div className="agent-status-runtime-next">
              {t('agent.status.nextAction', 'Next action')}: {latestRun.user_next_action}
            </div>
          )}
        </div>
        <span className={`badge ${protectedRun ? 'badge-warning' : 'badge-success'}`}>
          {protectedRun ? t('agent.status.needsAttention', 'Needs attention') : t('agent.status.ok', 'OK')}
        </span>
      </div>

      <div className="agent-status-two-col">
        <div className="card">
          <h3 className="agent-status-card-title">{t('agent.profile.title')}</h3>
          <div className="agent-status-field-list">
            <div className="agent-status-field">
              <span className="agent-status-field-label">{t('agent.fields.role')}</span>
              <span title={agent.role_description || ''} className="agent-status-field-clamp">
                {agent.role_description || '—'}
              </span>
            </div>
            <div className="agent-status-field">
              <span className="agent-status-field-label">{t('agent.profile.created')}</span>
              <span>{agent.created_at ? formatDate(agent.created_at) : '—'}</span>
            </div>
            {agent.creator_username && (
              <div className="agent-status-field">
                <span className="agent-status-field-label">{t('agent.fields.createdBy', 'Created by')}</span>
                <span className="u-secondary">@{agent.creator_username}</span>
              </div>
            )}
            <div className="agent-status-field">
              <span className="agent-status-field-label">{t('agent.profile.lastActive')}</span>
              <span>{agent.last_active_at ? formatDate(agent.last_active_at) : '—'}</span>
            </div>
            <div className="agent-status-field">
              <span className="agent-status-field-label">{t('agent.profile.timezone')}</span>
              <span>{agent.effective_timezone || agent.timezone || 'UTC'}</span>
            </div>
          </div>
        </div>
        <div className="card">
          <h3 className="agent-status-card-title">{t('agent.modelConfig.title')}</h3>
          <div className="agent-status-field-list">
            <div className="agent-status-field">
              <span className="agent-status-field-label">{t('agent.modelConfig.model')}</span>
              <span className="u-mono u-row">{modelLabel}</span>
            </div>
            <div className="agent-status-field">
              <span className="agent-status-field-label">{t('agent.modelConfig.provider')}</span>
              <span className="agent-status-capitalize">{modelProvider}</span>
            </div>
          </div>
        </div>
      </div>

      {capabilityInstalls.length > 0 && (
        <div className="card agent-status-section">
          <div className="agent-status-section-header">
            <h3 className="agent-status-panel-title">
              {t('agent.status.capabilityInstallTitle', 'Capability Install Status')}
            </h3>
            <span className="u-row u-tertiary">
              {capabilityInstalls.length} {t('agent.status.capabilityItems', 'items')}
            </span>
          </div>

          <div className="agent-status-stat-grid">
            <div className="agent-status-stat">
              <div className="agent-status-stat-label">
                {t('agent.status.installedCount', 'Installed')}
              </div>
              <div className="agent-status-stat-value">{installedCount}</div>
            </div>
            <div className="agent-status-stat">
              <div className="agent-status-stat-label">
                {t('agent.status.pendingCount', 'Pending')}
              </div>
              <div className="agent-status-stat-value">{pendingCount}</div>
            </div>
            <div className="agent-status-stat">
              <div className="agent-status-stat-label">
                {t('agent.status.failedCount', 'Failed')}
              </div>
              <div
                className="agent-status-stat-value"
                style={{ color: failedItems.length > 0 ? 'var(--error)' : 'inherit' }}
              >
                {failedItems.length}
              </div>
            </div>
          </div>

          {failedItems.length > 0 ? (
            <div className="agent-status-stack">
              <div className="agent-status-issues-title">
                {t('agent.status.installIssues', 'Install Issues')}
              </div>
              {failedItems.slice(0, 3).map((item) => (
                <div key={item.id} className="agent-status-issue">
                  <div className="agent-status-issue-name">{item.display_name || item.source_key || item.kind}</div>
                  <div className="agent-status-issue-msg">
                    {item.error_message || t('agent.status.installFailed', 'Installation failed')}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="u-row u-secondary">
              {t('agent.status.installHealthy', 'All requested capabilities are currently installed or pending.')}
            </div>
          )}
        </div>
      )}

      {channelCapabilities.length > 0 && (
        <div className="card agent-status-section">
          <div className="agent-status-section-header">
            <h3 className="agent-status-panel-title">
              {t('agent.status.channelCapabilityTitle', 'Channel Capabilities')}
            </h3>
            <span className="u-row u-tertiary">
              {channelCapabilities.length} {t('agent.status.channelCapabilityItems', 'channels')}
            </span>
          </div>

          <div className="agent-status-table-scroll">
            <table className="agent-status-table">
              <thead>
                <tr className="agent-status-table-head">
                  <th className="agent-status-th">{t('agent.status.channelColumn', 'Channel')}</th>
                  <th className="agent-status-th">{t('agent.status.connectionColumn', 'Connection')}</th>
                  <th className="agent-status-th">{t('agent.status.apiColumn', 'API')}</th>
                  <th className="agent-status-th">{t('agent.status.textColumn', 'Text')}</th>
                  <th className="agent-status-th">{t('agent.status.fileColumn', 'Files')}</th>
                  <th className="agent-status-th">{t('agent.status.deferredColumn', 'Deferred')}</th>
                  <th className="agent-status-th">{t('agent.status.waitColumn', 'Wait Reply')}</th>
                </tr>
              </thead>
              <tbody>
                {channelCapabilities.map((item) => (
                  <tr key={item.channel} className="agent-status-tr">
                    <td className="agent-status-td">
                      <div className="agent-status-td-strong">{item.channel}</div>
                      {item.limitations?.length > 0 && (
                        <div className="agent-status-td-note">
                          {item.limitations.join(' ')}
                        </div>
                      )}
                    </td>
                    <td className="agent-status-td">{item.connected ? t('agent.status.connected', 'Connected') : t('agent.status.disconnected', 'Disconnected')}</td>
                    <td className="agent-status-td">
                      {item.official_api ? t('agent.status.officialApi', 'Official') : item.third_party_transport || t('agent.status.thirdParty', 'Third-party')}
                    </td>
                    <td className="agent-status-td">{renderCapabilityValue(item.capabilities.live_text)}</td>
                    <td className="agent-status-td">
                      {`${renderCapabilityValue(item.capabilities.inbound_file)} / ${renderCapabilityValue(item.capabilities.outbound_file)}`}
                    </td>
                    <td className="agent-status-td">
                      {`${renderCapabilityValue(item.capabilities.deferred_text)} / ${renderCapabilityValue(item.capabilities.deferred_file)}`}
                    </td>
                    <td className="agent-status-td">
                      {`${renderCapabilityValue(item.capabilities.on_message_current_sender)} / ${renderCapabilityValue(item.capabilities.on_message_by_name)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {activityLogs.length > 0 && (
        <div className="card">
          <div className="agent-status-activity-header">
            <h3 className="agent-status-panel-title">📊 Recent Activity</h3>
            <button className="btn btn-ghost" onClick={() => onSelectTab('activityLog')}>
              View All →
            </button>
          </div>
          <div className="agent-status-stack">
            {activityLogs.slice(0, 5).map((log: any, index: number) => (
              <div
                key={log.id || index}
                className={`agent-status-activity-row${index < 4 ? ' u-hairline-bottom' : ''}`}
              >
                <span className="agent-status-activity-time">
                  {new Date(log.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </span>
                <span className="u-body u-secondary">{log.summary || log.action_type}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="agent-status-actions">
        <button className="btn btn-secondary" onClick={() => onSelectTab('chat')}>
          {t('agent.actions.chat')}
        </button>
        <button className="btn btn-secondary" onClick={() => onSelectTab('aware')}>
          {t('agent.tabs.aware')}
        </button>
        <button className="btn btn-secondary" onClick={() => onSelectTab('settings')}>
          {t('agent.tabs.settings')}
        </button>
      </div>
    </div>
  );
}
