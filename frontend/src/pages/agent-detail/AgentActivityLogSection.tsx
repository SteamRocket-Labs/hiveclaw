import React from 'react';
import { useTranslation } from 'react-i18next';
import type { ToolFailureSummary } from '../../api/domains/activity';

type AgentActivityLogSectionProps = {
  agentType?: string;
  activityLogs: any[];
  toolFailureSummary?: ToolFailureSummary;
  logFilter: string;
  expandedLogId: string | null;
  onFilterChange: (value: string) => void;
  onToggleExpandedLog: (value: string | null) => void;
};

export default function AgentActivityLogSection({
  agentType,
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
      style={{
        padding: indent ? '4px 10px 4px 20px' : '6px 14px',
        fontSize: indent ? '11px' : '12px',
        fontWeight: logFilter === key ? 600 : 400,
        color: logFilter === key ? 'var(--accent-primary)' : 'var(--text-secondary)',
        background: logFilter === key ? 'rgba(99,102,241,0.1)' : 'transparent',
        border: logFilter === key ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
        borderRadius: '6px',
        cursor: 'pointer',
        transition: 'all 0.15s',
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </button>
  );

  return (
    <div>
      <h3 style={{ marginBottom: '12px' }}>{t('agent.activityLog.title')}</h3>

      {toolFailureSummary && (
        <div
          className="card"
          style={{ marginBottom: '16px', padding: '14px', display: 'flex', flexDirection: 'column', gap: '10px' }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: '12px', flexWrap: 'wrap' }}>
            <div style={{ fontWeight: 600 }}>
              {t('agent.activityLog.toolFailuresTitle', 'Tool Failure Summary')}
            </div>
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
              {t('agent.activityLog.last24h', 'Last 24h')}: {toolFailureSummary.total_errors}
            </div>
          </div>

          {toolFailureSummary.total_errors > 0 ? (
            <>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '10px' }}>
                <div style={{ padding: '10px', borderRadius: '8px', background: 'var(--bg-secondary)' }}>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>
                    {t('agent.activityLog.topTool', 'Top Tool')}
                  </div>
                  <div style={{ fontWeight: 600 }}>{toolFailureSummary.by_tool[0]?.tool_name || '—'}</div>
                </div>
                <div style={{ padding: '10px', borderRadius: '8px', background: 'var(--bg-secondary)' }}>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>
                    {t('agent.activityLog.topProvider', 'Top Provider')}
                  </div>
                  <div style={{ fontWeight: 600 }}>{toolFailureSummary.by_provider[0]?.provider || '—'}</div>
                </div>
                <div style={{ padding: '10px', borderRadius: '8px', background: 'var(--bg-secondary)' }}>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>
                    {t('agent.activityLog.topErrorClass', 'Top Error')}
                  </div>
                  <div style={{ fontWeight: 600 }}>{toolFailureSummary.by_error_class[0]?.error_class || '—'}</div>
                </div>
                <div style={{ padding: '10px', borderRadius: '8px', background: 'var(--bg-secondary)' }}>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>
                    {t('agent.activityLog.topHttpStatus', 'Top HTTP Status')}
                  </div>
                  <div style={{ fontWeight: 600 }}>
                    {toolFailureSummary.by_http_status[0]?.http_status ?? '—'}
                  </div>
                </div>
              </div>

              {toolFailureSummary.recent_errors.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)' }}>
                    {t('agent.activityLog.recentToolFailures', 'Recent Tool Failures')}
                  </div>
                  {toolFailureSummary.recent_errors.slice(0, 3).map((entry, index) => (
                    <div
                      key={`${entry.tool_name || 'tool'}-${entry.created_at || index}`}
                      style={{ padding: '10px', borderRadius: '8px', background: 'var(--bg-secondary)', fontSize: '12px' }}
                    >
                      <div style={{ fontWeight: 600 }}>{entry.tool_name || 'unknown_tool'}</div>
                      <div style={{ color: 'var(--text-secondary)', marginTop: '2px' }}>
                        {entry.provider || 'unknown_provider'} · {entry.error_class || 'unknown_error'}
                        {entry.http_status ? ` · HTTP ${entry.http_status}` : ''}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
              {t('agent.activityLog.noToolFailures', 'No recent tool failures')}
            </div>
          )}
        </div>
      )}

      <div style={{ display: 'flex', gap: '6px', marginBottom: '16px', flexWrap: 'wrap', alignItems: 'center' }}>
        {filterButton('user', `👤 ${t('agent.activityLog.userActions', 'User Actions')}`)}
        {agentType !== 'openclaw' && (
          <>
            {filterButton('backend', `⚙️ ${t('agent.activityLog.backendServices', 'Backend Services')}`)}
            {(logFilter === 'backend' || logFilter === 'heartbeat' || logFilter === 'schedule' || logFilter === 'messages') && (
              <>
                <span style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>│</span>
                {filterButton('heartbeat', `💓 ${t('agent.mind.heartbeatTitle')}`)}
                {filterButton('schedule', `⏰ ${t('agent.activityLog.scheduleCron')}`, true)}
                {filterButton('messages', `📨 ${t('agent.activityLog.messages')}`, true)}
              </>
            )}
          </>
        )}
      </div>

      {filteredLogs.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
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
                style={{
                  padding: '10px 14px',
                  borderRadius: '8px',
                  cursor: 'pointer',
                  background: isExpanded ? 'var(--bg-elevated)' : 'var(--bg-secondary)',
                  fontSize: '13px',
                  border: isExpanded ? '1px solid var(--accent-primary)' : '1px solid transparent',
                  transition: 'all 0.15s ease',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                  <span style={{ fontSize: '16px', flexShrink: 0, marginTop: '1px' }}>{icons[log.action_type] || '·'}</span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 500, marginBottom: '2px' }}>{log.summary}</div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                      {time} · {log.action_type}
                      {log.detail && !isExpanded && <span style={{ marginLeft: '8px', color: 'var(--accent-primary)' }}>▸ Details</span>}
                    </div>
                  </div>
                </div>
                {isExpanded && log.detail && (
                  <div style={{ marginTop: '8px', padding: '10px', borderRadius: '6px', background: 'var(--bg-primary)', fontSize: '12px', fontFamily: 'monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-all', lineHeight: '1.6', color: 'var(--text-secondary)', maxHeight: '300px', overflowY: 'auto' }}>
                    {Object.entries(log.detail).map(([key, value]: [string, any]) => (
                      <div key={key} style={{ marginBottom: '6px' }}>
                        <span style={{ color: 'var(--accent-primary)', fontWeight: 600 }}>{key}:</span>{' '}
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
        <div className="card" style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
          {t('agent.activityLog.noRecords')}
        </div>
      )}
    </div>
  );
}
