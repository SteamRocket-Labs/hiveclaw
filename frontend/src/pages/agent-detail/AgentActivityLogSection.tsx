import { useTranslation } from 'react-i18next';
import type { ToolFailureSummary } from '../../api/domains/activity';
import './AgentActivityLogSection.css';

type AgentActivityLogSectionProps = {
  activityLogs: any[];
  toolFailureSummary?: ToolFailureSummary;
  logFilter: string;
  expandedLogId: string | null;
  onFilterChange: (value: string) => void;
  onToggleExpandedLog: (value: string | null) => void;
};

export default function AgentActivityLogSection({
  activityLogs,
  toolFailureSummary,
  logFilter,
  expandedLogId,
  onFilterChange,
  onToggleExpandedLog,
}: AgentActivityLogSectionProps) {
  const { t } = useTranslation();

  const userActionTypes = ['chat_reply', 'tool_call', 'task_created', 'task_updated', 'file_written', 'error'];
  const heartbeatTypes = ['heartbeat', 'plaza_post'];
  const scheduleTypes = ['schedule_run'];
  const messageTypes = ['feishu_msg_sent', 'agent_msg_sent', 'web_msg_sent'];

  let filteredLogs = activityLogs;
  if (logFilter === 'user') {
    filteredLogs = activityLogs.filter((log: any) => userActionTypes.includes(log.action_type));
  } else if (logFilter === 'backend') {
    filteredLogs = activityLogs.filter((log: any) => !userActionTypes.includes(log.action_type));
  } else if (logFilter === 'heartbeat') {
    filteredLogs = activityLogs.filter((log: any) => heartbeatTypes.includes(log.action_type));
  } else if (logFilter === 'schedule') {
    filteredLogs = activityLogs.filter((log: any) => scheduleTypes.includes(log.action_type));
  } else if (logFilter === 'messages') {
    filteredLogs = activityLogs.filter((log: any) => messageTypes.includes(log.action_type));
  }

  const filterButton = (key: string, label: string, indent = false) => (
    <button
      key={key}
      onClick={() => onFilterChange(key)}
      className={`agent-activity-filter${logFilter === key ? ' is-active' : ''}${indent ? ' is-indent' : ''}`}
    >
      {label}
    </button>
  );

  return (
    <div>
      <h3 className="agent-activity-title">{t('agent.activityLog.title')}</h3>

      {toolFailureSummary && (
        <div className="card agent-activity-failure-card">
          <div className="agent-activity-failure-head">
            <div className="agent-activity-strong">
              {t('agent.activityLog.toolFailuresTitle', 'Tool Failure Summary')}
            </div>
            <div className="agent-activity-note">
              {t('agent.activityLog.last24h', 'Last 24h')}: {toolFailureSummary.total_errors}
            </div>
          </div>

          {toolFailureSummary.total_errors > 0 ? (
            <>
              <div className="agent-activity-stat-grid">
                <div className="agent-activity-stat">
                  <div className="agent-activity-stat-label">
                    {t('agent.activityLog.topTool', 'Top Tool')}
                  </div>
                  <div className="agent-activity-strong">{toolFailureSummary.by_tool[0]?.tool_name || '—'}</div>
                </div>
                <div className="agent-activity-stat">
                  <div className="agent-activity-stat-label">
                    {t('agent.activityLog.topProvider', 'Top Provider')}
                  </div>
                  <div className="agent-activity-strong">{toolFailureSummary.by_provider[0]?.provider || '—'}</div>
                </div>
                <div className="agent-activity-stat">
                  <div className="agent-activity-stat-label">
                    {t('agent.activityLog.topErrorClass', 'Top Error')}
                  </div>
                  <div className="agent-activity-strong">{toolFailureSummary.by_error_class[0]?.error_class || '—'}</div>
                </div>
                <div className="agent-activity-stat">
                  <div className="agent-activity-stat-label">
                    {t('agent.activityLog.topHttpStatus', 'Top HTTP Status')}
                  </div>
                  <div className="agent-activity-strong">
                    {toolFailureSummary.by_http_status[0]?.http_status ?? '—'}
                  </div>
                </div>
              </div>

              {toolFailureSummary.recent_errors.length > 0 && (
                <div className="agent-activity-recent">
                  <div className="agent-activity-recent-title">
                    {t('agent.activityLog.recentToolFailures', 'Recent Tool Failures')}
                  </div>
                  {toolFailureSummary.recent_errors.slice(0, 3).map((entry, index) => (
                    <div
                      key={`${entry.tool_name || 'tool'}-${entry.created_at || index}`}
                      className="agent-activity-recent-item"
                    >
                      <div className="agent-activity-strong">{entry.tool_name || 'unknown_tool'}</div>
                      <div className="agent-activity-recent-meta">
                        {entry.provider || 'unknown_provider'} · {entry.error_class || 'unknown_error'}
                        {entry.http_status ? ` · HTTP ${entry.http_status}` : ''}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="agent-activity-note">
              {t('agent.activityLog.noToolFailures', 'No recent tool failures')}
            </div>
          )}
        </div>
      )}

      <div className="agent-activity-filter-bar">
        {filterButton('user', `👤 ${t('agent.activityLog.userActions', 'User Actions')}`)}
        {filterButton('backend', `⚙️ ${t('agent.activityLog.backendServices', 'Backend Services')}`)}
        {(logFilter === 'backend' || logFilter === 'heartbeat' || logFilter === 'schedule' || logFilter === 'messages') && (
          <>
            <span className="agent-activity-divider">│</span>
            {filterButton('heartbeat', `💓 ${t('agent.mind.heartbeatTitle')}`)}
            {filterButton('schedule', `⏰ ${t('agent.activityLog.scheduleCron')}`, true)}
            {filterButton('messages', `📨 ${t('agent.activityLog.messages')}`, true)}
          </>
        )}
      </div>

      {filteredLogs.length > 0 ? (
        <div className="agent-activity-log-list">
          {filteredLogs.map((log: any) => {
            const icons: Record<string, string> = {
              chat_reply: '💬',
              tool_call: '⚡',
              feishu_msg_sent: '📤',
              agent_msg_sent: '🤖',
              web_msg_sent: '🌐',
              task_created: '📋',
              task_updated: '✅',
              file_written: '📝',
              error: '❌',
              schedule_run: '⏰',
              heartbeat: '💓',
              plaza_post: '🏛️',
            };
            const time = log.created_at
              ? new Date(log.created_at).toLocaleString('zh-CN', {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                })
              : '';
            const isExpanded = expandedLogId === log.id;

            return (
              <div
                key={log.id}
                onClick={() => onToggleExpandedLog(isExpanded ? null : log.id)}
                className={`agent-activity-log-row${isExpanded ? ' is-expanded' : ''}`}
              >
                <div className="agent-activity-log-main">
                  <span className="agent-activity-log-icon">{icons[log.action_type] || '·'}</span>
                  <div className="agent-activity-log-text">
                    <div className="agent-activity-log-summary">{log.summary}</div>
                    <div className="agent-activity-log-meta">
                      {time} · {log.action_type}
                      {log.detail && !isExpanded && <span className="agent-activity-log-details-hint">▸ Details</span>}
                    </div>
                  </div>
                </div>
                {isExpanded && log.detail && (
                  <div className="agent-activity-log-detail">
                    {Object.entries(log.detail).map(([key, value]: [string, any]) => (
                      <div key={key} className="agent-activity-detail-row">
                        <span className="agent-activity-detail-key">{key}:</span>{' '}
                        <span>{typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <div className="card agent-activity-empty">
          {t('agent.activityLog.noRecords')}
        </div>
      )}
    </div>
  );
}
